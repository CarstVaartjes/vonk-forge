"""Canonical OpenAPI request and response validation."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from functools import lru_cache
from importlib.resources import files
from typing import Never

import httpx2
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError, best_match

from .errors import ControlClientError, ControlMalformedResponse, _ControlValidator


def _schema_unavailable(message: str) -> Never:
    """A damaged derived schema cache must not poison the next observation."""
    _control_openapi.cache_clear()
    _control_validator.cache_clear()
    _operation.cache_clear()
    raise ControlMalformedResponse(message)


@lru_cache(maxsize=1)
def _control_openapi() -> dict[str, object]:
    try:
        raw = (
            files("cluster_profiles.schemas")
            .joinpath("control-openapi.json")
            .read_text()
        )
        schema = json.loads(raw)
        Draft202012Validator.check_schema(schema)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, SchemaError):
        _schema_unavailable("bundled control API schema is unreadable")
    if not isinstance(schema, dict):
        _schema_unavailable("bundled control API schema must be an object")
    return schema


def source_schema_validator(schema: dict[str, object]) -> Draft202012Validator:
    """Validate an explicit source graph with the canonical JSON token rules.

    Acceptance may supply a verified historical release's schema here. This
    factory never selects or falls back to the current bundled contract.
    """
    return _ControlValidator(schema, format_checker=FormatChecker())


@lru_cache(maxsize=1)
def _control_validator() -> Draft202012Validator:
    return source_schema_validator(_control_openapi())


def _path_pattern(template: str) -> re.Pattern[str]:
    escaped = re.escape(template)
    return re.compile(r"^" + re.sub(r"\\\{[^}]+\\\}", r"[^/]+", escaped) + r"$")


@lru_cache(maxsize=256)
def _operation(path: str, method: str) -> dict[str, object]:
    schema = _control_openapi()
    paths = schema.get("paths")
    if not isinstance(paths, dict):
        _schema_unavailable("bundled control API schema has no paths")
    candidates = sorted(
        paths.items(), key=lambda entry: (entry[0].count("{"), -len(entry[0]))
    )
    for template, item in candidates:
        if not isinstance(template, str):
            continue
        if _path_pattern(template).match(path):
            if not isinstance(item, dict):
                _schema_unavailable("control API route schema is unreadable")
            operation = item.get(method.lower())
            if operation is not None and not isinstance(operation, dict):
                _schema_unavailable("control API operation schema is unreadable")
            if isinstance(operation, dict):
                return operation
    raise ControlClientError("control API route is not in the bundled schema")


def _validate_schema(value: object, schema: object, *, message: str) -> None:
    if not isinstance(schema, dict):
        _schema_unavailable("canonical control schema is unreadable")
    # Keep the generated document as the reference root while validating an
    # operation-local schema, so component $refs resolve exactly as emitted.
    operation_schema = {
        "components": _control_openapi().get("components", {}),
        "allOf": [schema],
    }
    # The validator's instance type is the recursive JSON alias. A decoded
    # payload is JSON by construction, so round-trip it through JSON rather
    # than asserting a type the validator cannot check.
    try:
        instance = json.loads(json.dumps(value))
    except (TypeError, ValueError):
        raise ControlClientError(message) from None
    error = best_match(
        _control_validator().evolve(schema=operation_schema).iter_errors(instance)
    )
    if error is not None:
        raise ControlClientError(f"{message}: {_schema_violation(error)}") from None


_VALUE_FREE_VIOLATIONS = frozenset(
    {"required", "additionalProperties", "unevaluatedProperties", "dependentRequired"}
)


def _schema_violation(error: ValidationError) -> str:
    """Name the failing field and the rule it broke, never the submitted value.

    The document can hold a credential or an operator's text, so the reason is
    built from the schema's own rule rather than from jsonschema's message,
    which repeats the rejected value.
    """

    path = "$"
    for part in error.absolute_path:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    if error.validator in _VALUE_FREE_VIOLATIONS:
        reason = str(error.message)
    else:
        reason = f"violates {error.validator} {error.validator_value!r}"
    return f"{path}: {reason}"[:240]


def validate_control_document(name: str, document: object) -> dict[str, object]:
    """Validate a CLI-authored document with the generated canonical schema."""
    if not isinstance(document, dict):
        raise ControlClientError(f"{name} must be a JSON object")
    _validate_schema(
        document,
        {"$ref": f"#/components/schemas/{name}"},
        message=f"document does not match the canonical {name} contract",
    )
    return document


def _request_contract(
    path: str, method: str, payload: Mapping[str, object] | None
) -> None:
    operation = _operation(path, method)
    request_body = operation.get("requestBody")
    if request_body is not None and not isinstance(request_body, dict):
        _schema_unavailable("control API request body schema is unreadable")
    if not isinstance(request_body, dict):
        if payload is not None:
            raise ControlClientError("control API request has no OpenAPI request body")
        return
    if payload is None:
        if request_body.get("required") is True:
            raise ControlClientError(
                "control API request is missing its OpenAPI request body"
            )
        return
    content = request_body.get("content")
    if not isinstance(content, dict):
        _schema_unavailable("control API request body media types are unreadable")
    media = content.get("application/json")
    if media is not None and not isinstance(media, dict):
        _schema_unavailable("control API request media schema is unreadable")
    if not isinstance(media, dict):
        raise ControlClientError(
            "control API request body does not accept application/json"
        )
    _validate_schema(
        payload,
        media.get("schema"),
        message="control API request does not match the OpenAPI schema",
    )


def _request_media_contract(path: str, method: str, media_type: str) -> None:
    operation = _operation(path, method)
    request_body = operation.get("requestBody")
    if request_body is not None and not isinstance(request_body, dict):
        _schema_unavailable("control API request body schema is unreadable")
    if not isinstance(request_body, dict):
        raise ControlClientError("control API request has no OpenAPI request body")
    content = request_body.get("content")
    if not isinstance(content, dict):
        _schema_unavailable("control API request media schema is unreadable")
    if media_type not in content:
        raise ControlClientError("control API request content type is not documented")


def _response_definition(path: str, method: str, status: int) -> dict[str, object]:
    operation = _operation(path, method)
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        _schema_unavailable("control API response schema is unreadable")
    response = responses.get(str(status), responses.get("default"))
    if not isinstance(response, dict):
        raise ControlMalformedResponse("control API returned an undocumented status")
    return response


def _response_contract(path: str, method: str, status: int, decoded: object) -> None:
    response = _response_definition(path, method, status)
    content = response.get("content")
    if not isinstance(content, dict):
        return
    media = content.get("application/json")
    if isinstance(media, dict):
        try:
            _validate_schema(
                decoded,
                media.get("schema"),
                message="control API response does not match the OpenAPI schema",
            )
        except ControlClientError as error:
            raise ControlMalformedResponse(str(error)) from None


def _response_media_contract(
    path: str, method: str, status: int, media_type: str, *, has_content: bool
) -> None:
    if not has_content:
        return
    response = _response_definition(path, method, status)
    content = response.get("content")
    if not isinstance(content, dict) or media_type not in content:
        raise ControlMalformedResponse(
            "control API response content type is not documented"
        )


def _validate_generated_request(request: httpx2.Request) -> None:
    parsed_url = urllib.parse.urlsplit(str(request.url))
    route_path = parsed_url.path
    request_media_type = request.headers.get("content-type", "").split(";", 1)[0]
    if request.content:
        if request_media_type.strip().lower() == "application/json":
            try:
                payload = json.loads(request.content)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ControlClientError(
                    "control API request contains invalid JSON"
                ) from None
            if not isinstance(payload, Mapping):
                raise ControlClientError("control API request must be a JSON object")
            _request_contract(route_path, request.method, payload)
        else:
            _request_media_contract(
                route_path, request.method, request_media_type.strip().lower()
            )
    else:
        _request_contract(route_path, request.method, None)


def _validate_generated_response(
    request: httpx2.Request, response: httpx2.Response
) -> None:
    parsed_url = urllib.parse.urlsplit(str(request.url))
    route_path = parsed_url.path
    response_media_type = response.headers.get("content-type", "").split(";", 1)[0]
    _response_media_contract(
        route_path,
        request.method,
        response.status_code,
        response_media_type.strip().lower(),
        has_content=bool(response.content),
    )
    if response.status_code == 204 or not response.content:
        _response_contract(route_path, request.method, response.status_code, {})
    elif response_media_type.strip().lower() == "application/json":
        try:
            decoded = json.loads(response.content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ControlMalformedResponse(
                "control API returned invalid JSON"
            ) from None
        _response_contract(route_path, request.method, response.status_code, decoded)
    else:
        _response_definition(route_path, request.method, response.status_code)
