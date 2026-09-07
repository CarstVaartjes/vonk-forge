"""Shared strict JSON boundary helpers for Pydantic wire models."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pydantic import BaseModel, GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema


def _check_literal_type(value: object, expected: tuple[Any, ...]) -> object:
    if any(type(item) in (bool, int, float) for item in expected) and not any(
        type(value) is type(item) and value == item for item in expected
    ):
        raise ValueError("literal must use its exact JSON scalar type")
    return value


def _wrap_numeric_literals(schema: CoreSchema) -> CoreSchema:
    if not isinstance(schema, dict):
        return schema
    if schema.get("type") == "literal":
        expected = tuple(schema.get("expected", ()))
        if any(type(item) in (bool, int, float) for item in expected):
            return core_schema.no_info_before_validator_function(
                lambda value: _check_literal_type(value, expected), schema
            )
        return schema
    for key, nested in tuple(schema.items()):
        if isinstance(nested, dict):
            schema[key] = _wrap_numeric_literals(nested)
        elif isinstance(nested, list):
            schema[key] = [
                _wrap_numeric_literals(item) if isinstance(item, dict) else item
                for item in nested
            ]
    return schema


class StrictJSONModel(BaseModel):
    """Reject Python equality based coercion for numeric and boolean Literals.

    Pydantic's strict mode still accepts ``True`` and ``1.0`` for
    ``Literal[1]`` because they compare equal to ``1`` in Python.  JSON has
    distinct scalar types, so wire models must require an exact type/value
    match.  String enum and literal handling remains Pydantic's responsibility.
    """

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return _wrap_numeric_literals(deepcopy(handler(source_type)))
