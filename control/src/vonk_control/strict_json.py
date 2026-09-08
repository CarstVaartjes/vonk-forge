"""Controller JSON models and their field-presence serialization policy."""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from copy import copy
from typing import Any

from fastapi import Response
from fastapi.concurrency import run_in_threadpool
from fastapi.datastructures import DefaultPlaceholder
from fastapi.routing import APIRoute
from pydantic import BaseModel, RootModel
from vonk_agent_protocol.wire_model import StrictJSONModel as ProtocolStrictJSONModel


def _serialized_field_name(field_name: str, field: Any, *, by_alias: bool) -> str:
    if by_alias:
        return str(field.serialization_alias or field.alias or field_name)
    return field_name


def _apply_nested_value(value: object, document: object, *, by_alias: bool) -> None:
    if isinstance(value, BaseModel):
        apply_optional_none_policy(value, document, by_alias=by_alias)
    elif isinstance(value, Mapping) and isinstance(document, dict):
        for key, nested_value in value.items():
            if key in document:
                _apply_nested_value(nested_value, document[key], by_alias=by_alias)
    elif (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
        and isinstance(document, list)
    ):
        for item, encoded in zip(value, document, strict=False):
            _apply_nested_value(item, encoded, by_alias=by_alias)


def apply_optional_none_policy(
    model: BaseModel, document: object, *, by_alias: bool = True
) -> object:
    """Omit optional ``None`` fields while retaining required nullable fields.

    Recurse through actual nested Pydantic models and typed containers. Mapping
    values without nested models (for example engine arguments) retain nulls.
    """

    if isinstance(model, RootModel):
        _apply_nested_value(model.root, document, by_alias=by_alias)
        return document
    if not isinstance(document, dict):
        return document
    fields = type(model).model_fields
    for field_name, field in fields.items():
        output_name = _serialized_field_name(field_name, field, by_alias=by_alias)
        if output_name not in document:
            continue
        value = getattr(model, field_name, None)
        serialized = document[output_name]
        if serialized is None:
            if not field.is_required():
                document.pop(output_name)
            continue
        _apply_nested_value(value, serialized, by_alias=by_alias)
    return document


class StrictJSONModel(ProtocolStrictJSONModel):
    """Controller model using the shared protocol validation boundary."""


def serialize_json_value(value: object, *, by_alias: bool = True) -> object:
    """Encode models while retaining required nullable fields at JSON egress."""

    if isinstance(value, BaseModel):
        document = value.model_dump(
            mode="json",
            by_alias=by_alias,
            exclude_none=False,
            exclude_unset=False,
            exclude_defaults=False,
        )
        return apply_optional_none_policy(value, document, by_alias=by_alias)
    if isinstance(value, Mapping):
        return {
            key: serialize_json_value(item, by_alias=by_alias)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [serialize_json_value(item, by_alias=by_alias) for item in value]
    if isinstance(value, tuple):
        return [serialize_json_value(item, by_alias=by_alias) for item in value]
    return value


class ControllerAPIRoute(APIRoute):
    """Apply the presence policy after response validation for every JSON route."""

    def get_route_handler(self):
        original_dependant = self.dependant
        original_call = original_dependant.call
        original_response_param_name = original_dependant.response_param_name
        response_param_name = original_response_param_name or "__controller_response"
        response_field = self.secure_cloned_response_field
        by_alias = self.response_model_by_alias
        response_class = self.response_class
        if isinstance(response_class, DefaultPlaceholder):
            response_class = response_class.value

        async def policy_endpoint(**values: Any) -> Any:
            injected_response = values.get(response_param_name)
            if original_response_param_name is None:
                values.pop(response_param_name, None)
            if inspect.iscoroutinefunction(original_call):
                result = await original_call(**values)
            else:
                result = await run_in_threadpool(original_call, **values)
            if isinstance(result, Response) or response_field is None:
                return result
            value, errors = response_field.validate(result, {}, loc=("response",))
            if errors:
                return result
            status_code = self.status_code or 200
            if isinstance(injected_response, Response) and injected_response.status_code:
                status_code = injected_response.status_code
            response = response_class(
                content=serialize_json_value(value, by_alias=by_alias),
                status_code=status_code,
            )
            if isinstance(injected_response, Response):
                response.headers.raw.extend(injected_response.headers.raw)
            return response

        dependant = copy(original_dependant)
        dependant.call = policy_endpoint
        dependant.response_param_name = response_param_name
        self.dependant = dependant
        try:
            return super().get_route_handler()
        finally:
            self.dependant = original_dependant


__all__ = [
    "ControllerAPIRoute",
    "StrictJSONModel",
    "apply_optional_none_policy",
    "serialize_json_value",
]
