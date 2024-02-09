"""
The network module defines the data model for an instance of network,
including nodes, links, and components (model instantations).
"""
import itertools
from dataclasses import dataclass
from typing import Dict, Iterable, List

from andromede.model import PortField, PortType
from andromede.model.model import Model, PortFieldId
from andromede.utils import require_not_none


@dataclass(frozen=True)
class Component:
    """
    A component is an instance of a model, with specified parameter values.
    """

    model: Model
    id: str


def create_component(model: Model, id: str) -> Component:
    return Component(model=model, id=id)


@dataclass(frozen=True)
class Node(Component):
    """
    A node in the network.
    """

    pass


@dataclass(frozen=True)
class Arc:
    """
    An arc between 2 nodes of the network.
    TODO: we could imagine that it would be a component.
    """

    id: str
    node1_id: str
    node2_id: str


@dataclass(frozen=True)
class PortRef:
    component: Component
    port_id: str

    def get_id(self) -> str:
        return f"{self.port_id}_{self.component.id}"


@dataclass()
class PortsConnection:
    port1: PortRef
    port2: PortRef
    master_port: Dict[PortField, PortRef]

    def __init__(self, port1: PortRef, port2: PortRef):
        self.port1 = port1
        self.port2 = port2
        self.master_port = {}
        self.__validate_ports()

    def get_id(self) -> str:
        return f"{self.port1.get_id()}__{self.port2.get_id()}"

    def __validate_ports(self) -> None:
        model1 = self.port1.component.model
        model2 = self.port2.component.model
        port_1 = model1.ports.get(self.port1.port_id)
        port_2 = model2.ports.get(self.port2.port_id)

        if port_1 is None or port_2 is None:
            raise ValueError(f"Missing port: {port_1} or {port_2} ")
        if port_1.port_type != port_2.port_type:
            raise ValueError(
                f"Incompatible portTypes {port_1.port_type} != {port_2.port_type}"
            )

        for field_name in [f.name for f in port_1.port_type.fields]:
            def1: bool = (
                PortFieldId(port_name=port_1.port_name, field_name=field_name)
                in model1.port_fields_definitions
            )
            def2: bool = (
                PortFieldId(port_name=port_2.port_name, field_name=field_name)
                in model2.port_fields_definitions
            )
            if not def1 and not def2:
                raise ValueError(
                    f"No definition for port field {field_name} on {port_1.port_name}."
                )
            if def1 and def2:
                raise ValueError(
                    f"Port field {field_name} on {port_1.port_name} has 2 definitions."
                )

            self.master_port[PortField(name=field_name)] = (
                self.port1 if def1 else self.port2
            )

    def get_port_type(self) -> PortType:
        port_1 = self.port1.component.model.ports.get(self.port1.port_id)

        if port_1 is None:
            raise ValueError(f"Missing port: {port_1}")
        return port_1.port_type


@dataclass
class Network:
    """
    Network model: simply nodes, links, and components.
    """

    def __init__(self, id: str):
        self.id: str = id
        self._nodes: Dict[str, Node] = {}
        self._arcs: Dict[str, Arc] = {}
        self._components: Dict[str, Component] = {}
        self._connections: List[PortsConnection] = []

    def _check_node_exists(self, node_id: str) -> None:
        if node_id not in self._nodes:
            raise ValueError(f"Node {node_id} does not exist in the network.")

    def add_component(self, component: Component) -> None:
        require_not_none(component)
        self._components[component.id] = component

    def get_component(self, component_id: str) -> Component:
        return self._components[component_id]

    @property
    def components(self) -> Iterable[Component]:
        return self._components.values()

    def add_node(self, node: Node) -> None:
        self._nodes[node.id] = node

    def get_node(self, node_id: str) -> Node:
        return self._nodes[node_id]

    @property
    def nodes(self) -> Iterable[Node]:
        return self._nodes.values()

    @property
    def all_components(self) -> Iterable[Component]:
        """
        An iterable over both nodes and components.
        """
        return itertools.chain(self.nodes, self.components)

    def add_arc(self, arc: Arc) -> None:
        self._check_node_exists(arc.node1_id)
        self._check_node_exists(arc.node2_id)
        self._arcs[arc.id] = arc

    def get_arc(self, arc_id: str) -> Arc:
        return self._arcs[arc_id]

    @property
    def arcs(self) -> Iterable[Arc]:
        return self._arcs.values()

    def connect(self, port1: PortRef, port2: PortRef) -> None:
        ports_connection = PortsConnection(port1, port2)
        self._connections.append(ports_connection)

    @property
    def connections(self) -> Iterable[PortsConnection]:
        return self._connections
