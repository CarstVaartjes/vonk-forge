"""Controller JSON models and their field-presence serialization policy."""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any, overload

from fastapi import Response
from pydantic import BaseModel, ConfigDict, RootModel, ValidationError

from vonk_agent_protocol.outcome import UnknownOutcomeError
from vonk_agent_protocol.wire_model import StrictJSONModel as ProtocolStrictJSONModel

from .http_errors import MAX_RETRY_AFTER_SECONDS
from .logging import redact_text
from .warn_once import warn_once


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


class StrictModel(StrictJSONModel):
    """The one Controller contract base: exact structure, exact scalar types.

    API JSON is a typed boundary.  Pydantic's default lax mode would turn values
    such as ``1`` into ``"1"`` and accept integer flags as booleans, which makes
    malformed requests indistinguishable from canonical ones.
    """

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


@overload
def serialize_json_value(value: RootModel[Any], *, by_alias: bool = True) -> object: ...


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


_DEFAULT_RETRY_AFTER_SECONDS = 5
_MAX_RETRY_AFTER_SECONDS = MAX_RETRY_AFTER_SECONDS


def _retry_later_response(error: UnknownOutcomeError) -> Response:
    """The typed retryable answer: 503 with ``Retry-After`` and a bounded detail.

    The request is not refused and nothing is ended; the caller (a client, an
    agent, a worker) asks again.  The delay is the error's own hint when it
    carries one, bounded.
    """

    hint = getattr(error, "retry_after_seconds", None)
    delay = (
        min(hint, _MAX_RETRY_AFTER_SECONDS)
        if type(hint) is int and hint >= 1
        else _DEFAULT_RETRY_AFTER_SECONDS
    )
    detail = redact_text(str(error) or type(error).__name__)[:230]
    _LOGGER.info(
        "route answered retry-later: %s (retry after %ss)", type(error).__name__, delay
    )
    response = Response(
        content=json.dumps(
            {"detail": f"Temporarily unavailable, retry: {detail}"},
            separators=(",", ":"),
        ).encode(),
        status_code=503,
        headers={"Retry-After": str(delay)},
        media_type="application/json",
    )
    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        response.headers["x-vonk-error-code"] = code
    return response


def route_endpoint(endpoint: Callable[..., Any]) -> Callable[..., Any]:
    """Return the handwritten endpoint behind a Controller route."""

    return getattr(endpoint, "__vonk_endpoint__", endpoint)


__all__ = [
    "StrictJSONModel",
    "StrictModel",
    "apply_optional_none_policy",
    "read_stored_document",
    "read_stored_model",
    "route_endpoint",
    "serialize_json_value",
    "warn_unreadable_once",
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


_LOGGER = logging.getLogger(__name__)


def warn_unreadable_once(kind: str, record_id: object) -> None:
    """Log a stored record that cannot be read, once per process per record."""

    warn_once(
        f"unreadable:{kind}:{record_id}",
        "skipping unreadable stored %s %s",
        kind,
        record_id,
    )


def _drop_field(document: object, location: Sequence[object]) -> None:
    """Remove the key a pydantic error location names, if it is in the document.

    Union member names appear in a location but not in the JSON, so a segment
    that does not exist is skipped rather than followed.
    """

    node: Any = document
    for part in location[:-1]:
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and isinstance(part, int) and part < len(node):
            node = node[part]
    if isinstance(node, dict):
        node.pop(location[-1], None)


def read_stored_document[T](validate: Callable[[object], T], document: object) -> T:
    """Validate a persisted document, ignoring fields a newer contract retired.

    Wire and request models stay strict; a document written by an older build
    may carry fields that were since removed, and that must never make history
    unreadable. Any other defect still raises the original error. A current
    document is validated as it is; only a repair copies it, so the caller's
    value is never changed.
    """

    try:
        return validate(document)
    except ValidationError as error:
        issues = error.errors()
        if not issues or any(i["type"] != "extra_forbidden" for i in issues):
            raise
    document = copy.deepcopy(document)
    for _ in range(64):
        try:
            return validate(document)
        except ValidationError as error:
            issues = error.errors()
            if not issues or any(i["type"] != "extra_forbidden" for i in issues):
                raise
            for issue in issues:
                _drop_field(document, issue["loc"])
    return validate(document)


def read_stored_model[T: BaseModel](
    model: type[T], document: object, *, from_json: bool = False, **options: Any
) -> T:
    """Read a persisted document into ``model``, tolerating retired fields.

    Same result as ``model.model_validate`` (or ``model_validate_json`` with
    ``from_json``) for a current document, and a fast path that adds no cost
    to it. Only ``extra_forbidden`` defects are repaired, by dropping the
    retired field; every other defect raises the original error.
    """

    def validate(value: object) -> T:
        if from_json:
            payload = value if isinstance(value, (str, bytes, bytearray)) else None
            if payload is None:
                payload = json.dumps(value, allow_nan=False)
            return model.model_validate_json(payload, **options)
        return model.model_validate(value, **options)

    try:
        return validate(document)
    except ValidationError as error:
        issues = error.errors()
        if not issues or any(i["type"] != "extra_forbidden" for i in issues):
            raise
    parsed = (
        json.loads(document)
        if isinstance(document, (str, bytes, bytearray))
        else document
    )
    return read_stored_document(validate, parsed)
