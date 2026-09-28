"""Controller JSON models and their field-presence serialization policy."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Mapping, Sequence
from typing import Any, overload

from fastapi import Response
from fastapi.concurrency import run_in_threadpool
from fastapi.datastructures import DefaultPlaceholder
from fastapi.routing import APIRoute
from pydantic import BaseModel, RootModel, ValidationError
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


@overload
def serialize_json_value(value: RootModel, *, by_alias: bool = True) -> object: ...


@overload
def serialize_json_value(
    value: BaseModel, *, by_alias: bool = True
) -> dict[str, object]: ...


@overload
def serialize_json_value(value: object, *, by_alias: bool = True) -> object: ...


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


_CONTROLLER_RESPONSE = "_controller_response"


def _presence_policy_endpoint(
    endpoint: Callable[..., Any], route: Callable[[], APIRoute]
) -> Callable[..., Any]:
    """Wrap an endpoint so its validated result is encoded with the presence policy.

    The wrapper carries the endpoint's own resolved signature, plus an injected
    ``Response`` when the endpoint does not declare one, so FastAPI analyses it
    exactly like the endpoint however the route is later included or mounted.
    """

    signature = inspect.signature(endpoint, eval_str=True)
    response_param_name = next(
        (
            name
            for name, parameter in signature.parameters.items()
            if isinstance(parameter.annotation, type)
            and issubclass(parameter.annotation, Response)
        ),
        None,
    )
    parameters = list(signature.parameters.values())
    if response_param_name is None:
        injected = inspect.Parameter(
            _CONTROLLER_RESPONSE, inspect.Parameter.KEYWORD_ONLY, annotation=Response
        )
        position = next(
            (
                index
                for index, parameter in enumerate(parameters)
                if parameter.kind is inspect.Parameter.VAR_KEYWORD
            ),
            len(parameters),
        )
        parameters.insert(position, injected)

    async def policy_endpoint(**values: Any) -> Any:
        injected_response = values.get(response_param_name or _CONTROLLER_RESPONSE)
        if response_param_name is None:
            values.pop(_CONTROLLER_RESPONSE, None)
        if inspect.iscoroutinefunction(endpoint):
            result = await endpoint(**values)
        else:
            result = await run_in_threadpool(endpoint, **values)
        current = route()
        response_field = current.response_field
        if isinstance(result, Response) or response_field is None:
            return result
        value, errors = response_field.validate(result, {}, loc=("response",))
        if errors:
            return result
        status_code = current.status_code or 200
        if isinstance(injected_response, Response) and injected_response.status_code:
            status_code = injected_response.status_code
        response_class = current.response_class
        if isinstance(response_class, DefaultPlaceholder):
            response_class = response_class.value
        response = response_class(
            content=serialize_json_value(
                value, by_alias=current.response_model_by_alias
            ),
            status_code=status_code,
        )
        if isinstance(injected_response, Response):
            response.headers.raw.extend(injected_response.headers.raw)
        return response

    # Copy the identity FastAPI derives names, operation ids and descriptions
    # from, but no ``__wrapped__``: FastAPI must see an async endpoint.
    functools.update_wrapper(policy_endpoint, endpoint, updated=("__dict__",))
    del policy_endpoint.__wrapped__
    policy_endpoint.__signature__ = signature.replace(parameters=parameters)  # type: ignore[attr-defined]
    policy_endpoint.__vonk_endpoint__ = endpoint  # type: ignore[attr-defined]
    return policy_endpoint


class ControllerAPIRoute(APIRoute):
    """Apply the presence policy after response validation for every JSON route."""

    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        if not hasattr(endpoint, "__vonk_endpoint__"):
            endpoint = _presence_policy_endpoint(endpoint, lambda: self)
        super().__init__(path, endpoint, **kwargs)


def route_endpoint(endpoint: Callable[..., Any]) -> Callable[..., Any]:
    """Return the handwritten endpoint behind a Controller route."""

    return getattr(endpoint, "__vonk_endpoint__", endpoint)


__all__ = [
    "ControllerAPIRoute",
    "StrictJSONModel",
    "apply_optional_none_policy",
    "route_endpoint",
    "serialize_json_value",
]


def stored_document_detail(error: Exception) -> str | None:
    """Describe a stored document that will not validate, without its input.

    A pydantic ``ValidationError`` stringifies the offending value, so it cannot
    be handed to a client as-is. The failing field path and error type are
    enough to find the row and say nothing about its contents.
    """

    if not isinstance(error, ValidationError):
        return None
    issues = error.errors()
    issue = issues[0] if issues else {}
    location = ".".join(str(part) for part in issue.get("loc", ()))[:140] or "<root>"
    return f"stored document is invalid at {location} ({issue.get('type', 'invalid')})"
