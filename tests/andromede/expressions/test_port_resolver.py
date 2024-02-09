from typing import Dict, List

from andromede.expression import ExpressionNode, var
from andromede.expression.equality import expressions_equal
from andromede.expression.expression import port_field
from andromede.expression.port_resolver import PortFieldKey, resolve_port
from andromede.model.model import PortFieldId


def test_port_field_resolution():
    ports_expressions: Dict[PortFieldKey, List[ExpressionNode]] = {}

    key = PortFieldKey("com_id", PortFieldId(field_name="field", port_name="port"))
    expression = var("flow")

    ports_expressions[key] = [expression]

    expression_2 = port_field("port", "field") + 2

    assert expressions_equal(
        resolve_port(expression_2, "com_id", ports_expressions), var("flow") + 2
    )


def test_port_field_resolution_sum():
    ports_expressions: Dict[PortFieldKey, List[ExpressionNode]] = {}

    key = PortFieldKey("com_id", PortFieldId(field_name="field", port_name="port"))

    ports_expressions[key] = [var("flow1"), var("flow2")]

    expression_2 = port_field("port", "field").sum_connections()
    # TODO remove 0 from sum()
    assert expressions_equal(
        resolve_port(expression_2, "com_id", ports_expressions),
        var("flow1") + var("flow2"),
    )
