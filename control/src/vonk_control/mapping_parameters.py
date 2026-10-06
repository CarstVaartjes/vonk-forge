"""The effective settings a cluster mapping runs with."""

from __future__ import annotations

from typing import Annotated

from pydantic import JsonValue
from vonk_forge_contracts.recipe import Scalar

from .stored_json import ExternalPassthrough

EngineArgumentValue = Annotated[
    JsonValue,
    ExternalPassthrough(
        "A structured engine argument value is the upstream recipe's own JSON "
        "(RuntimeArgumentValue in vonk_forge_contracts); the engine, not the "
        "Controller, defines its structure, and the mapping must keep it exact."
    ),
]

# Effective settings the mapping runs with: each knob's value, plus (for a
# recipe that declares options) the chosen option per option name.
MappingParameters = dict[str, Scalar | EngineArgumentValue]

__all__ = ["EngineArgumentValue", "MappingParameters"]
