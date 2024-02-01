# Copyright (c) 2024, RTE (https://www.rte-france.com)
#
# See AUTHORS.txt
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
# SPDX-License-Identifier: MPL-2.0
#
# This file is part of the Antares project.

"""
The optimization module contains the logic to translate the input model
into a mathematical optimization problem.
"""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, List, Optional

import ortools.linear_solver.pywraplp as lp

from andromede.expression import (
    EvaluationVisitor,
    ExpressionNode,
    ParameterValueProvider,
    ValueProvider,
    resolve_parameters,
    visit,
)
from andromede.expression.context_adder import add_component_context
from andromede.expression.indexing import IndexingStructureProvider, compute_indexation
from andromede.expression.indexing_structure import IndexingStructure
from andromede.expression.port_resolver import PortFieldKey, resolve_port
from andromede.expression.scenario_operator import Expectation
from andromede.expression.time_operator import TimeEvaluation, TimeShift, TimeSum
from andromede.model.common import ProblemContext
from andromede.model.constraint import Constraint
from andromede.model.model import PortFieldId
from andromede.simulation.linear_expression import LinearExpression, Term
from andromede.simulation.linearize import linearize_expression
from andromede.simulation.time_block import TimeBlock
from andromede.study.data import DataBase
from andromede.study.network import Component, Network
from andromede.utils import get_or_add, write_to_file


@dataclass(eq=True, frozen=True)
class TimestepComponentVariableKey:
    """
    Identifies the solver variable for one timestep and one component variable.
    """

    component_id: str
    variable_name: str
    block_timestep: Optional[int] = None
    scenario: Optional[int] = None


@dataclass(eq=True, frozen=True)
class TimestepFlowVariableKey:
    """
    Identifies the solver variable for one timestep and one link.
    """

    link_id: str
    block_timestep: Optional[int] = None
    scenario: Optional[int] = None


def _get_parameter_value(
    context: "OptimizationContext",
    block_timestep: int,
    scenario: int,
    component_id: str,
    name: str,
) -> float:
    data = context.database.get_data(component_id, name)
    absolute_timestep = context.block_timestep_to_absolute_timestep(block_timestep)
    return data.get_value(absolute_timestep, scenario)


# TODO: Maybe add the notion of constant parameter in the model
# TODO : And constant over scenarios ?
def _parameter_is_constant_over_time(
    component: Component,
    name: str,
    context: "OptimizationContext",
    block_timestep: int,
    scenario: int,
) -> bool:
    data = context.database.get_data(component.id, name)
    return data.get_value(block_timestep, scenario) == IndexingStructure(
        time=False, scenario=False
    )


class TimestepValueProvider(ABC):
    """
    Interface which provides numerical values for individual timesteps.
    """

    @abstractmethod
    def get_value(self, block_timestep: int, scenario: int) -> float:
        raise NotImplementedError()


def _make_value_provider(
    context: "OptimizationContext",
    block_timestep: int,
    scenario: int,
    component: Component,
) -> ValueProvider:
    """
    Create a value provider which takes its values from
    the parameter values as defined in the network data.

    Cannot evaluate expressions which contain variables.
    """

    class Provider(ValueProvider):
        def get_component_variable_value(self, component_id: str, name: str) -> float:
            raise NotImplementedError(
                "Cannot provide variable value at problem build time."
            )

        def get_component_parameter_value(self, component_id: str, name: str) -> float:
            return _get_parameter_value(
                context, block_timestep, scenario, component_id, name
            )

        def get_variable_value(self, name: str) -> float:
            raise NotImplementedError(
                "Cannot provide variable value at problem build time."
            )

        def get_parameter_value(self, name: str) -> float:
            raise ValueError(
                "Parameter must be associated to its component before resolution."
            )

        def parameter_is_constant_over_time(self, name: str) -> bool:
            return _parameter_is_constant_over_time(
                component, name, context, block_timestep, scenario
            )

    return Provider()


@dataclass(frozen=True)
class ExpressionTimestepValueProvider(TimestepValueProvider):
    context: "OptimizationContext"
    component: Component
    expression: ExpressionNode

    # OptimizationContext has knowledge of the block, so that get_value only needs block_timestep and scenario to get the correct data value

    def get_value(self, block_timestep: int, scenario: int) -> float:
        param_value_provider = _make_value_provider(
            self.context, block_timestep, scenario, self.component
        )
        visitor = EvaluationVisitor(param_value_provider)
        return visit(self.expression, visitor)


def _make_parameter_value_provider(
    context: "OptimizationContext",
    block_timestep: int,
    scenario: int,
) -> ParameterValueProvider:
    """
    A value provider which takes its values from
    the parameter values as defined in the network data.

    Cannot evaluate expressions which contain variables.
    """

    class Provider(ParameterValueProvider):
        def get_component_parameter_value(self, component_id: str, name: str) -> float:
            return _get_parameter_value(
                context, block_timestep, scenario, component_id, name
            )

        def get_parameter_value(self, name: str) -> float:
            raise ValueError(
                "Parameters should have been associated with their component before resolution."
            )

    return Provider()


def _make_data_structure_provider(
    network: Network, component: Component
) -> IndexingStructureProvider:
    """
    Retrieve information in data structure (parameter and variable) from the model
    """

    class Provider(IndexingStructureProvider):
        def get_component_variable_structure(
            self, component_id: str, name: str
        ) -> IndexingStructure:
            return network.get_component(component_id).model.variables[name].structure

        def get_component_parameter_structure(
            self, component_id: str, name: str
        ) -> IndexingStructure:
            return network.get_component(component_id).model.parameters[name].structure

        def get_parameter_structure(self, name: str) -> IndexingStructure:
            return component.model.parameters[name].structure

        def get_variable_structure(self, name: str) -> IndexingStructure:
            return component.model.variables[name].structure

    return Provider()


@dataclass(frozen=True)
class ComponentContext:
    """
    Helper class to fill the optimization problem with component-related equations and variables.
    """

    opt_context: "OptimizationContext"
    component: Component

    def get_values(self, expression: ExpressionNode) -> TimestepValueProvider:
        """
        The returned value provider will evaluate the provided expression.
        """
        return ExpressionTimestepValueProvider(
            self.opt_context, self.component, expression
        )

    def add_variable(
        self, block_timestep: int, scenario: int, variable: lp.Variable
    ) -> None:
        self.opt_context.register_component_variable(
            block_timestep, scenario, self.component.id, variable.name(), variable
        )

    def get_variable(
        self, block_timestep: int, scenario: int, variable_name: str
    ) -> lp.Variable:
        return self.opt_context.get_component_variable(
            block_timestep,
            scenario,
            self.component.id,
            variable_name,
            self.component.model.variables[variable_name].structure,
        )

    def linearize_expression(
        self,
        block_timestep: int,
        scenario: int,
        expression: ExpressionNode,
    ) -> LinearExpression:
        parameters_valued_provider = _make_parameter_value_provider(
            self.opt_context, block_timestep, scenario
        )
        evaluated_expr = resolve_parameters(expression, parameters_valued_provider)

        value_provider = _make_value_provider(
            self.opt_context, block_timestep, scenario, self.component
        )
        structure_provider = _make_data_structure_provider(
            self.opt_context.network, self.component
        )

        return linearize_expression(evaluated_expr, structure_provider, value_provider)


class BlockBorderManagement(Enum):
    """
    Class to specify the way of handling the time horizon (or time block) border.
        - IGNORE_OUT_OF_FRAME: Ignore terms in constraints that lead to out of horizon data
        - CYCLE: Consider all timesteps to be specified modulo the horizon length, this is the actual functioning of Antares
    """

    IGNORE_OUT_OF_FRAME = "IGNORE"
    CYCLE = "CYCLE"


class OptimizationContext:
    """
    Helper class to build the optimization problem.
    Maintains some mappings between model and solver objects.
    Also provides navigation method in the model (components by node ...).
    """

    def __init__(
        self,
        network: Network,
        database: DataBase,
        block: TimeBlock,
        scenarios: int,
        border_management: BlockBorderManagement,
    ):
        self._network = network
        self._database = database
        self._block = block
        self._scenarios = scenarios
        self._border_management = border_management
        self._component_variables: Dict[TimestepComponentVariableKey, lp.Variable] = {}
        self._connection_fields_expressions: Dict[
            PortFieldKey, List[ExpressionNode]
        ] = {}

    @property
    def network(self) -> Network:
        return self._network

    @property
    def scenarios(self) -> int:
        return self._scenarios

    def block_length(self) -> int:
        return len(self._block.timesteps)

    @property
    def connection_fields_expressions(self) -> Dict[PortFieldKey, List[ExpressionNode]]:
        return self._connection_fields_expressions

    # TODO: Need to think about data processing when creating blocks with varying or inequal time steps length (aggregation, sum ?, mean of data ?)
    def block_timestep_to_absolute_timestep(self, block_timestep: int) -> int:
        return self._block.timesteps[block_timestep]

    @property
    def database(self) -> DataBase:
        return self._database

    def _manage_border_timesteps(self, timestep: int) -> int:
        if self._border_management == BlockBorderManagement.CYCLE:
            return timestep % self.block_length()
        else:
            raise NotImplementedError

    def get_time_indices(self, index_structure: IndexingStructure) -> Iterable[int]:
        return range(self.block_length()) if index_structure.time else range(1)

    def get_scenario_indices(self, index_structure: IndexingStructure) -> Iterable[int]:
        return range(self.scenarios) if index_structure.scenario else range(1)

    # TODO: API to improve, variable_structure guides which of the indices block_timestep and scenario should be used
    def get_component_variable(
        self,
        block_timestep: int,
        scenario: int,
        component_id: str,
        variable_name: str,
        variable_structure: IndexingStructure,
    ) -> lp.Variable:
        block_timestep = self._manage_border_timesteps(block_timestep)

        # TODO: Improve design, variable_structure defines indexing
        if variable_structure.time == False:
            block_timestep = 0
        if variable_structure.scenario == False:
            scenario = 0

        return self._component_variables[
            TimestepComponentVariableKey(
                component_id, variable_name, block_timestep, scenario
            )
        ]

    def get_all_component_variables(
        self,
    ) -> Dict[TimestepComponentVariableKey, lp.Variable]:
        return self._component_variables

    def register_component_variable(
        self,
        block_timestep: int,
        scenario: int,
        component_id: str,
        variable_name: str,
        variable: lp.Variable,
    ) -> None:
        key = TimestepComponentVariableKey(
            component_id, variable_name, block_timestep, scenario
        )
        self._component_variables[key] = variable

    def get_component_context(self, component: Component) -> ComponentContext:
        return ComponentContext(self, component)

    def register_connection_fields_expressions(
        self,
        component_id: str,
        port_name: str,
        field_name: str,
        expression: ExpressionNode,
    ) -> None:
        key = PortFieldKey(component_id, PortFieldId(port_name, field_name))
        get_or_add(self._connection_fields_expressions, key, lambda: []).append(
            expression
        )


def _get_indexing(
    constraint: Constraint, provider: IndexingStructureProvider
) -> IndexingStructure:
    return (
        compute_indexation(constraint.expression, provider)
        or compute_indexation(constraint.lower_bound, provider)
        or compute_indexation(constraint.upper_bound, provider)
    )


def _compute_indexing_structure(
    context: ComponentContext, constraint: Constraint
) -> IndexingStructure:
    data_structure_provider = _make_data_structure_provider(
        context.opt_context.network, context.component
    )
    constraint_indexing = _get_indexing(constraint, data_structure_provider)
    return constraint_indexing


def _instantiate_model_expression(
    model_expression: ExpressionNode,
    component_id: str,
    optimization_context: OptimizationContext,
) -> ExpressionNode:
    """
    Performs common operations that are necessary on model expressions before their actual use:
     1. add component ID for variables and parameters of THIS component
     2. replace port fields by their definition
    """
    with_component = add_component_context(component_id, model_expression)
    with_component_and_ports = resolve_port(
        with_component, component_id, optimization_context.connection_fields_expressions
    )
    return with_component_and_ports


def _create_constraint(
    solver: lp.Solver,
    context: ComponentContext,
    constraint: Constraint,
) -> None:
    """
    Adds a component-related constraint to the solver.
    """
    constraint_indexing = _compute_indexing_structure(context, constraint)

    # Perf: Perform linearization (tree traversing) without timesteps so that we can get the number of instances for the expression (from the time_ids of operators)
    linear_expr = context.linearize_expression(0, 0, constraint.expression)
    # Will there be cases where instances > 1 ? If not, maybe just a check that get_number_of_instances == 1 is sufficient ? Anyway, the function should be implemented
    instances_per_time_step = linear_expr.number_of_instances()

    for block_timestep in context.opt_context.get_time_indices(constraint_indexing):
        for scenario in context.opt_context.get_scenario_indices(constraint_indexing):
            linear_expr_at_t = context.linearize_expression(
                block_timestep, scenario, constraint.expression
            )
            # What happens if there is some time_operator in the bounds ?
            constraint_data = ConstraintData(
                name=constraint.name,
                lower_bound=context.get_values(constraint.lower_bound).get_value(
                    block_timestep, scenario
                ),
                upper_bound=context.get_values(constraint.upper_bound).get_value(
                    block_timestep, scenario
                ),
                expression=linear_expr_at_t,
            )
            make_constraint(
                solver,
                context.opt_context,
                block_timestep,
                scenario,
                constraint_data,
                instances_per_time_step,
            )


def _create_objective(
    solver: lp.Solver,
    opt_context: OptimizationContext,
    component: Component,
    component_context: ComponentContext,
    objective_contribution: ExpressionNode,
) -> None:
    # We have already checked in the model creation that the objective contribution is neither indexed by time nor by scenario
    linear_expr = component_context.linearize_expression(0, 0, objective_contribution)
    obj: lp.Objective = solver.Objective()
    for term in linear_expr.terms.values():
        # TODO : How to handle the scenario operator in a general manner ?
        if isinstance(term.scenario_operator, Expectation):
            weight = 1 / opt_context.scenarios
            scenario_ids = range(opt_context.scenarios)
        else:
            weight = 1
            scenario_ids = range(1)

        for scenario in scenario_ids:
            solver_vars = _get_solver_vars(
                term,
                opt_context,
                0,
                scenario,
                0,
            )

            for solver_var in solver_vars:
                obj.SetCoefficient(
                    solver_var,
                    obj.GetCoefficient(solver_var) + weight * term.coefficient,
                )

    # This should have no effect on the optimization
    obj.SetOffset(linear_expr.constant + obj.offset())


@dataclass
class ConstraintData:
    name: str
    lower_bound: float
    upper_bound: float
    expression: LinearExpression


def _get_solver_vars(
    term: Term,
    context: OptimizationContext,
    block_timestep: int,
    scenario: int,
    instance: int,
) -> List[lp.Variable]:
    solver_vars = []
    if isinstance(term.time_aggregator, TimeSum):
        if isinstance(term.time_operator, TimeShift):
            for time_id in term.time_operator.time_ids:
                solver_vars.append(
                    context.get_component_variable(
                        block_timestep + time_id,
                        scenario,
                        term.component_id,
                        term.variable_name,
                        term.structure,
                    )
                )
        elif isinstance(term.time_operator, TimeEvaluation):
            for time_id in term.time_operator.time_ids:
                solver_vars.append(
                    context.get_component_variable(
                        time_id,
                        scenario,
                        term.component_id,
                        term.variable_name,
                        term.structure,
                    )
                )
        else:  # time_operator is None, retrieve variable for each time step of the block. What happens if we do x.sum() with x not being indexed by time ? Is there a check that it is a valid expression ?
            for time_id in range(context.block_length()):
                solver_vars.append(
                    context.get_component_variable(
                        block_timestep + time_id,
                        scenario,
                        term.component_id,
                        term.variable_name,
                        term.structure,
                    )
                )

    else:  # time_aggregator is None
        if isinstance(term.time_operator, TimeShift):
            solver_vars.append(
                context.get_component_variable(
                    block_timestep + term.time_operator.time_ids[instance],
                    scenario,
                    term.component_id,
                    term.variable_name,
                    term.structure,
                )
            )
        elif isinstance(term.time_operator, TimeEvaluation):
            solver_vars.append(
                context.get_component_variable(
                    term.time_operator.time_ids[instance],
                    scenario,
                    term.component_id,
                    term.variable_name,
                    term.structure,
                )
            )
        else:  # time_operator is None
            # TODO: horrible tous ces if/else
            solver_vars.append(
                context.get_component_variable(
                    block_timestep,
                    scenario,
                    term.component_id,
                    term.variable_name,
                    term.structure,
                )
            )
    return solver_vars


def make_constraint(
    solver: lp.Solver,
    context: OptimizationContext,
    block_timestep: int,
    scenario: int,
    data: ConstraintData,
    instances: int,
) -> Dict[str, lp.Constraint]:
    """
    Adds constraint to the solver.
    """
    solver_constraints = {}
    constraint_name = data.name
    for instance in range(instances):
        if instances > 1:
            constraint_name += f"_{instance}"

        solver_constraint: lp.Constraint = solver.Constraint(constraint_name)
        constant: float = 0
        for term in data.expression.terms.values():
            solver_vars = _get_solver_vars(
                term,
                context,
                block_timestep,
                scenario,
                instance,
            )
            for solver_var in solver_vars:
                coefficient = term.coefficient + solver_constraint.GetCoefficient(
                    solver_var
                )
                solver_constraint.SetCoefficient(solver_var, coefficient)
        # TODO: On pourrait aussi faire que l'objet Constraint n'ait pas de terme constant dans son expression et que les constantes soit déjà prises en compte dans les bornes, ça simplifierait le traitement ici
        constant += data.expression.constant

        solver_constraint.SetBounds(
            data.lower_bound - constant, data.upper_bound - constant
        )

        # TODO: this dictionary does not make sense, we override the content when there are multiple instances
        solver_constraints[constraint_name] = solver_constraint
    return solver_constraints


class OptimizationProblem:
    class Type(Enum):
        """
        Class to specify the type of the created problem:
            - simulator: Creates a Antares Simulator problem with only operational variables and constraints
            - xpansion_master: Creates a Xpansion master problem only
            - xpansion_subproblem: Creates Xpansion sub-problems only
            - xpansion_merged: Creates a merged Xpansion master/subproblem
        """

        simulator = 0
        xpansion_merged = 1
        xpansion_master = 2
        xpansion_subproblem = 3

    name: str
    solver: lp.Solver
    context: OptimizationContext
    type: Type

    def __init__(
        self,
        name: str,
        solver: lp.Solver,
        opt_context: OptimizationContext,
        opt_type: Type = Type.simulator,
    ) -> None:
        self.name = name
        self.solver = solver
        self.context = opt_context
        self.type = opt_type

        self._register_connection_fields_definitions()
        self._create_variables()
        self._create_constraints()
        self._create_objectives()


    def _register_connection_fields_definitions(self) -> None:
        for cnx in self.context.network.connections:
            for field_name in list(cnx.master_port.keys()):
                master_port = cnx.master_port[field_name]
                port_definition = master_port.component.model.port_fields_definitions.get(
                    PortFieldId(port_name=master_port.port_id, field_name=field_name.name)
                )
                expression_node = port_definition.definition  # type: ignore
                instantiated_expression = add_component_context(
                    master_port.component.id, expression_node
                )
                self.context.register_connection_fields_expressions(
                    component_id=cnx.port1.component.id,
                    port_name=cnx.port1.port_id,
                    field_name=field_name.name,
                    expression=instantiated_expression,
                )
                self.context.register_connection_fields_expressions(
                    component_id=cnx.port2.component.id,
                    port_name=cnx.port2.port_id,
                    field_name=field_name.name,
                    expression=instantiated_expression,
                )


    def _create_variables(self) -> None:
        for component in self.context.network.all_components:
            component_context = self.context.get_component_context(component)
            model = component.model

            for model_var in model.variables.values():
                if (
                    self.type == OptimizationProblem.Type.xpansion_master
                    and model_var.context == ProblemContext.operational
                ):
                    # Xpansion Master Problem only takes investment variables and parameters
                    continue

                var_indexing = IndexingStructure(
                    model_var.structure.time, model_var.structure.scenario
                )
                instantiated_lb_expr = None
                instantiated_ub_expr = None
                if model_var.lower_bound:
                    instantiated_lb_expr = _instantiate_model_expression(
                        model_var.lower_bound, component.id, self.context
                    )
                if model_var.upper_bound:
                    instantiated_ub_expr = _instantiate_model_expression(
                        model_var.upper_bound, component.id, self.context
                    )
                for block_timestep in self.context.get_time_indices(var_indexing):
                    for scenario in self.context.get_scenario_indices(var_indexing):
                        lower_bound = -self.solver.infinity()
                        upper_bound = self.solver.infinity()
                        if instantiated_lb_expr:
                            lower_bound = component_context.get_values(
                                instantiated_lb_expr
                            ).get_value(block_timestep, scenario)
                        if instantiated_ub_expr:
                            upper_bound = component_context.get_values(
                                instantiated_ub_expr
                            ).get_value(block_timestep, scenario)

                        # TODO: Add BoolVar or IntVar if the variable is specified to be integer or bool
                        solver_var = self.solver.NumVar(lower_bound, upper_bound, model_var.name)
                        component_context.add_variable(block_timestep, scenario, solver_var)


    def _create_constraints(self) -> None:
        for component in self.context.network.all_components:
            for constraint in component.model.get_all_constraints():
                instantiated_expr = _instantiate_model_expression(
                    constraint.expression, component.id, self.context
                )
                instantiated_lb = _instantiate_model_expression(
                    constraint.lower_bound, component.id, self.context
                )
                instantiated_ub = _instantiate_model_expression(
                    constraint.upper_bound, component.id, self.context
                )
                instantiated_constraint = Constraint(
                    name=constraint.name,
                    expression=instantiated_expr,
                    lower_bound=instantiated_lb,
                    upper_bound=instantiated_ub,
                )
                _create_constraint(
                    self.solver,
                    self.context.get_component_context(component),
                    instantiated_constraint,
                )


    def _create_objectives(self) -> None:
        for component in self.context.network.all_components:
            component_context = self.context.get_component_context(component)
            model = component.model

            if (
                self.type != OptimizationProblem.Type.xpansion_master
                and model.objective_operational_contribution is not None
            ):
                # Xpansion SubProblems only take the operational contribution
                _create_objective(
                    self.solver,
                    self.context,
                    component,
                    component_context,
                    model.objective_operational_contribution,
                )

            if (
                self.type != OptimizationProblem.Type.xpansion_subproblem
                and model.objective_investment_contribution is not None
            ):
                # Xpansion Master Problem only takes the investment contribution
                _create_objective(
                    self.solver,
                    self.context,
                    component,
                    component_context,
                    model.objective_investment_contribution,
                )

    def export_as_mps(self) -> str:
        return self.solver.ExportModelAsMpsFormat(fixed_format=True, obfuscated=False)

    def export_as_lp(self) -> str:
        return self.solver.ExportModelAsLpFormat(obfuscated=False)


def build_problem(
    network: Network,
    database: DataBase,
    block: TimeBlock,
    scenarios: int,
    *,
    problem_name: str = "optimization_problem",
    border_management: BlockBorderManagement = BlockBorderManagement.CYCLE,
    solver_id: str = "GLOP",
    problem_type: OptimizationProblem.Type = OptimizationProblem.Type.simulator,
) -> OptimizationProblem:
    """
    Entry point to build the optimization problem for a time period.
    """
    solver: lp.Solver = lp.Solver.CreateSolver(solver_id)

    database.requirements_consistency(network)

    opt_context = OptimizationContext(
        network, database, block, scenarios, border_management
    )

    return OptimizationProblem(problem_name, solver, opt_context, problem_type)


def build_xpansion_problem(
    network: Network,
    database: DataBase,
    block: TimeBlock,
    scenarios: int,
    *,
    border_management: BlockBorderManagement = BlockBorderManagement.CYCLE,
    solver_id: str = "GLOP",
) -> List[OptimizationProblem]:
    """
    Entry point to build the xpansion problem for a time period

    Returns a list of problems where the first one is the master problem and
    subsequent problems are sub-problems
    """
    problems = []

    # Xpansion Master Problem
    problems.append(
        build_problem(
            network,
            database,
            block,
            scenarios,
            problem_name="master",
            border_management=border_management,
            solver_id=solver_id,
            problem_type=OptimizationProblem.Type.xpansion_master,
        )
    )

    # Xpansion Sub-problems
    problems.append(
        build_problem(
            network,
            database,
            block,
            scenarios,
            problem_name="subproblem",
            border_management=border_management,
            solver_id=solver_id,
            problem_type=OptimizationProblem.Type.xpansion_subproblem,
        )
    )

    return problems


def export_xpansion_problem(problems: List["OptimizationProblem"]) -> bool:
    """
    Write MPS files for Master and Sub problems as well as the structure.txt file

    It parses the MPS strings generated by the exports to stock
    the candidates of Master present in the objective function
    keeping record of their column indexes

    Then it parses the sub problems strings and identify the previous
    candidates and their respective column indexes

    Finally it writes the structure.txt file
    """

    if len(problems) < 2:
        # TODO For now, only one master and one subproblem
        return False

    # A mapping similar to the Xpansion mapping for keeping track of variable indexes
    # in Master and Sub-problem files
    problem_to_candidates: Dict[str, Dict[str, int]] = {}
    candidates = set()
    prog = re.compile(r"COLUMNS\n(( .*\n)*)(BOUNDS|RHS|ENDATA)")

    # === write master.mps file ===
    problem_to_candidates["master"] = {}
    master_mps_str = problems[0].export_as_mps()
    match = prog.search(master_mps_str)

    if match is None:
        return False

    seen_variables = set()
    for column in match.group(1).splitlines():
        is_variable_in_objective_func = "COST" in column
        variable = column.lstrip().split()[0]

        if variable in seen_variables:
            # Since MPS files have fixed line length, some variables
            # are duplicated. To avoid over-counting, we check to see
            # if variable was already seen
            continue

        seen_variables.add(variable)
        if is_variable_in_objective_func:
            problem_to_candidates["master"][variable] = len(seen_variables) - 1
            candidates.add(variable)

    if not write_to_file("master.mps", master_mps_str, "outputs/lp"):
        return False

    # === write subproblem.mps file ===
    for problem in problems[1:]:
        problem_to_candidates[problem.name] = {}
        problem_mps_str = problem.export_as_mps()
        match = prog.search(problem_mps_str)

        if match is None:
            return False

        seen_variables.clear()
        for column in match.group(1).splitlines():
            variable = column.lstrip().split()[0]

            if variable in seen_variables:
                continue

            seen_variables.add(variable)
            if variable in candidates:
                # If candidate was identified in master
                problem_to_candidates[problem.name][variable] = len(seen_variables) - 1

        if not write_to_file(
            f"{problem.name}.mps", problem_mps_str, "outputs/lp"
        ):
            return False

    # === write structure.txt file ===
    structure_str = ""
    for problem_name, candidate_to_index in problem_to_candidates.items():
        for candidate, index in candidate_to_index.items():
            structure_str += f"{problem_name:>50}{candidate:>50}{index:>10}\n"

    return write_to_file(f"structure.txt", structure_str, "outputs/lp")
