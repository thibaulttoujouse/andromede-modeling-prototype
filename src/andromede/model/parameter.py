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

from dataclasses import dataclass
from enum import Enum

from andromede.expression.indexing_structure import IndexingStructure


class ParameterValueType(Enum):
    FLOAT = "FLOAT"
    INTEGER = "INTEGER"
    # Needs more ?


@dataclass(frozen=True)
class Parameter:
    """
    A parameter of the model: a parameter is mainly defined by a name and expected type.
    When the model is instantiated as a component, a value must be provided for
    parameters, either as constant values or timeseries-based values.
    """

    name: str
    type: ParameterValueType
    structure: IndexingStructure


def int_parameter(
    name: str,
    structure: IndexingStructure = IndexingStructure(True, True),
) -> Parameter:
    return Parameter(name, ParameterValueType.INTEGER, structure)


def float_parameter(
    name: str,
    structure: IndexingStructure = IndexingStructure(True, True),
) -> Parameter:
    return Parameter(name, ParameterValueType.FLOAT, structure)
