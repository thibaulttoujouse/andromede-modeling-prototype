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

from unittest.mock import Mock

import ortools.linear_solver.pywraplp as lp

from andromede.simulation import OutputValues
from andromede.simulation.optimization import (
    OptimizationContext,
    SolverAndContext,
    TimestepComponentVariableKey,
)


def test_component_and_flow_output_object() -> None:
    mock_variable_component = Mock(spec=lp.Variable)
    mock_variable_flow = Mock(spec=lp.Variable)
    opt_context = Mock(spec=OptimizationContext)

    mock_variable_component.solution_value.side_effect = lambda: 1.0

    mock_variable_flow.solution_value.side_effect = lambda: -1.0

    opt_context.get_all_component_variables.return_value = {
        TimestepComponentVariableKey(
            component_id="component_id_test",
            variable_name="component_var_name",
            block_timestep=0,
            scenario=0,
        ): mock_variable_component
    }

    opt_context.block_length.return_value = 1

    problem = SolverAndContext(mock_variable_flow, opt_context)
    output = OutputValues(problem)

    wrong_output = OutputValues()
    wrong_output.component("component_id_test").var(
        "wrong_component_var_name"
    ).value = 1.0

    assert output != wrong_output, f"Output is equal to wrong output: {output}"

    expected_output = OutputValues()
    expected_output.component("component_id_test").var("component_var_name").value = 1.0

    assert output == expected_output, f"Output differs from expected: {output}"

    print(output)
