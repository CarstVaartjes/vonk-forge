"""Shared strict JSON boundary helpers for Pydantic wire models."""

from __future__ import annotations

import types
from collections.abc import Mapping
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, model_validator


def _literal_values(annotation: Any) -> tuple[Any, ...] | None:
    """Return exact Literal values, or ``None`` for a mixed union.

    A non-Literal branch remains authoritative for a union such as
    ``Literal[1] | int``.  Only a Literal-only annotation, optionally with
    ``None``, has enough information for this boundary check.
    """

    origin = get_origin(annotation)
    if origin is Literal:
        return get_args(annotation)
    if origin is Annotated:
        return _literal_values(get_args(annotation)[0])
    if origin in (Union, types.UnionType):
        values: list[Any] = []
        for item in get_args(annotation):
            if item is type(None):
                values.append(None)
                continue
            nested = _literal_values(item)
            if nested is None:
                return None
            values.extend(nested)
        return tuple(values)
    return None


def _exact_literal(value: object, literals: tuple[Any, ...]) -> bool:
    return any(type(value) is type(item) and value == item for item in literals)


class StrictJSONModel(BaseModel):
    """Reject Python equality based coercion for numeric and boolean Literals.

    Pydantic's strict mode still accepts ``True`` and ``1.0`` for
    ``Literal[1]`` because they compare equal to ``1`` in Python.  JSON has
    distinct scalar types, so wire models must require an exact type/value
    match.  String enum and literal handling remains Pydantic's responsibility.
    """

    @model_validator(mode="before")
    @classmethod
    def reject_numeric_literal_coercion(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        for name, field in cls.model_fields.items():
            literals = _literal_values(field.annotation)
            if literals is None:
                continue
            if not any(type(item) in (bool, int, float) for item in literals):
                continue
            keys = {name}
            has_alias = False
            if field.alias:
                keys.add(field.alias)
                has_alias = field.alias != name
            validation_alias = field.validation_alias
            if isinstance(validation_alias, str):
                keys.add(validation_alias)
                has_alias = has_alias or validation_alias != name
            else:
                choices = getattr(validation_alias, "choices", ())
                string_choices = {
                    choice for choice in choices if isinstance(choice, str)
                }
                keys.update(string_choices)
                has_alias = has_alias or bool(choices)
            if has_alias and not cls.model_config.get("validate_by_name", False):
                keys.discard(name)
            for key in keys:
                if key in value and not _exact_literal(value[key], literals):
                    raise ValueError(f"{name} must use its exact JSON scalar type")
        return value
