"""Discover HTTP contract ownership from mounted application routes."""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, get_args

from fastapi import FastAPI
from fastapi.dependencies.utils import get_flat_dependant
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette.routing import Mount, Route


class ContractGraphError(ValueError):
    pass


def raw_json_body(model: type[BaseModel]):
    """Bind a bounded raw reader to its existing canonical request model."""

    def decorate(endpoint):
        endpoint.__vonk_raw_json_model__ = model
        return endpoint

    return decorate


def schema_application(*, browser_auth: bool = True) -> FastAPI:
    from sqlalchemy.orm import sessionmaker

    from .api import create_app
    from .audit import MemoryAuditStore
    from .auth import TokenCodec
    from .browser_auth import BrowserAuthService
    from .jobs import JobService

    key = b"schema-test-signing-key-32-bytes!"
    return create_app(
        jobs=JobService(
            sessionmaker(), clock=lambda: datetime(2026, 9, 7, tzinfo=UTC)
        ),
        tokens=TokenCodec(key),
        audits=MemoryAuditStore(),
        browser_auth=BrowserAuthService(
            sessionmaker(),
            token_signing_key=key,
            clock=lambda: datetime(2026, 9, 7, tzinfo=UTC),
        )
        if browser_auth
        else None,
    )


def _models(annotation: Any) -> set[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return {annotation}
    return set().union(*(_models(arg) for arg in get_args(annotation)))


def _routes(app: FastAPI, prefix: str = "") -> Iterator[tuple[str, APIRoute, dict]]:
    schema = app.openapi()
    framework_paths = {
        app.openapi_url,
        app.docs_url,
        app.redoc_url,
        app.swagger_ui_oauth2_redirect_url,
    }
    for route in app.routes:
        if isinstance(route, Mount):
            if not isinstance(route.app, FastAPI):
                raise ContractGraphError(
                    f"Unregistered mounted transport: {prefix}{route.path}"
                )
            yield from _routes(route.app, prefix + route.path)
        elif isinstance(route, APIRoute):
            path = prefix + route.path_format
            if not route.include_in_schema:
                raise ContractGraphError(f"Unregistered hidden route: {path}")
            for method in route.methods:
                operation = (
                    schema.get("paths", {})
                    .get(route.path_format, {})
                    .get(method.lower())
                )
                if not operation:
                    raise ContractGraphError(
                        f"Missing OpenAPI operation: {method} {path}"
                    )
            yield path, route, schema
        elif isinstance(route, Route):
            if not (
                route.path in framework_paths
                and getattr(route.endpoint, "__module__", None)
                == "fastapi.applications"
                and getattr(route.endpoint, "__qualname__", "").startswith(
                    "FastAPI.setup.<locals>."
                )
            ):
                raise ContractGraphError(
                    f"Unregistered transport: {prefix}{route.path}"
                )
        else:
            raise ContractGraphError(f"Unregistered transport: {prefix}{route!r}")


def _reads_raw_body(endpoint: Any) -> bool:
    # Detect direct raw readers independently of their declarations. Delegated
    # bounded readers attach their canonical model with raw_json_body instead.
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(endpoint)))
    except (OSError, TypeError):
        return False
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"body", "json", "stream"}
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in inspect.signature(endpoint).parameters
        for node in ast.walk(tree)
    )


def _structure(
    value: Any, definitions: dict[str, Any], trail: tuple[str, ...] = ()
) -> Any:
    if isinstance(value, list):
        return [_structure(child, definitions, trail) for child in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        name = value["$ref"].rsplit("/", 1)[-1]
        if name in trail:
            return {"$recursive": name}
        return _structure(definitions[name], definitions, (*trail, name))
    return {
        key: _structure(child, definitions, trail)
        for key, child in value.items()
        if key != "$defs"
        and not (key in {"title", "description"} and isinstance(child, str))
    }


def discover_contracts(
    app: FastAPI,
) -> tuple[list[dict[str, Any]], set[type[BaseModel]]]:
    operations = []
    agent_models: set[type[BaseModel]] = set()
    identities = set()
    for path, route, schema in _routes(app):
        models = _models(route.response_model)
        if route.body_field is not None:
            models |= _models(route.body_field.type_)
        raw_model = getattr(route.endpoint, "__vonk_raw_json_model__", None)
        if raw_model is not None:
            models.add(raw_model)
        for field in route.response_fields.values():
            models |= _models(field.type_)
        if path.startswith("/agent/"):
            agent_models |= models
        for method in sorted(route.methods):
            identity = (method, path)
            if identity in identities:
                raise ContractGraphError(
                    f"Duplicate mounted operation: {method} {path}"
                )
            identities.add(identity)
            operation = schema["paths"][route.path_format][method.lower()]
            flat = get_flat_dependant(route.dependant, skip_repeats=True)
            declared_parameters = {
                (value.get("in"), value.get("name")): value
                for value in operation.get("parameters", [])
            }
            for location, fields in (
                ("path", flat.path_params),
                ("query", flat.query_params),
                ("header", flat.header_params),
                ("cookie", flat.cookie_params),
            ):
                for field in fields:
                    declared = declared_parameters.get((location, field.alias))
                    if declared is None or not declared.get("schema"):
                        raise ContractGraphError(
                            f"Missing parameter contract: {method} {path} {location}:{field.alias}"
                        )
            body = operation.get("requestBody")
            if (
                route.dependant.request_param_name
                and method in {"POST", "PUT", "PATCH"}
                and not body
                and operation.get("x-vonk-request-body") != "none"
            ):
                raise ContractGraphError(
                    f"Unclassified raw request body: {method} {path}"
                )
            if (
                route.body_field is not None
                or raw_model is not None
                or _reads_raw_body(route.endpoint)
            ) and not body:
                raise ContractGraphError(f"Undeclared request body: {method} {path}")
            if body:
                content = body.get("content", {})
                if not content or any(
                    not value.get("schema") for value in content.values()
                ):
                    raise ContractGraphError(f"Untyped request body: {method} {path}")
                if "application/json" in content and not (
                    route.body_field is not None or raw_model is not None
                ):
                    raise ContractGraphError(
                        f"JSON request has no canonical binding: {method} {path}"
                    )
            if raw_model is not None and body:
                actual = (
                    body.get("content", {}).get("application/json", {}).get("schema")
                )
                expected = raw_model.model_json_schema()
                if actual is None or _structure(
                    actual, schema.get("components", {}).get("schemas", {})
                ) != _structure(expected, expected.get("$defs", {})):
                    raise ContractGraphError(
                        f"Raw JSON declaration differs from canonical model: {method} {path}"
                    )
            success = {
                code: value
                for code, value in operation.get("responses", {}).items()
                if code.startswith("2")
            }
            if not success:
                raise ContractGraphError(f"Missing success contract: {method} {path}")
            for code, response in success.items():
                if method == "HEAD" or code in {"204", "205"}:
                    if response.get("content"):
                        raise ContractGraphError(
                            f"No-content response declares a body: {method} {path}"
                        )
                    for name, header in response.get("headers", {}).items():
                        if not header.get("schema"):
                            raise ContractGraphError(
                                f"Untyped response header: {method} {path} {name}"
                            )
                    continue
                content = response.get("content", {})
                if not content or any(
                    not value.get("schema") for value in content.values()
                ):
                    raise ContractGraphError(
                        f"Untyped response: {method} {path} {code}"
                    )
                typed_response = route.response_model or route.response_fields.get(
                    int(code)
                )
                exact_bytes = all(
                    value["schema"] == {"type": "string", "format": "binary"}
                    for value in content.values()
                )
                if not typed_response and (
                    operation.get("x-vonk-streaming-transport") is not True
                    or not exact_bytes
                ):
                    raise ContractGraphError(
                        f"Response has no canonical or exact-byte binding: {method} {path}"
                    )
            operations.append(
                {
                    "method": method,
                    "path": path,
                    "models": sorted(
                        f"{model.__module__}.{model.__name__}" for model in models
                    ),
                    "request_media": sorted(body.get("content", {})) if body else [],
                    "parameters": sorted(
                        f"{location}:{name}" for location, name in declared_parameters
                    ),
                    "response_media": {
                        code: sorted(value.get("content", {}))
                        for code, value in success.items()
                    },
                    "stream": operation.get("x-vonk-streaming-transport") is True,
                }
            )
    return sorted(
        operations, key=lambda value: (value["path"], value["method"])
    ), agent_models


def require_wire_exports(
    models: set[type[BaseModel]], exported: dict[str, Any]
) -> None:
    roots = exported.get("x-vonk-models", {})
    missing = sorted(model.__name__ for model in models if model.__name__ not in roots)
    if missing:
        raise ContractGraphError(
            f"Agent route models absent from wire export: {missing}"
        )

    identities = exported.get("x-vonk-model-sources", {})
    mismatched = sorted(
        model.__name__
        for model in models
        if identities.get(model.__name__) != f"{model.__module__}.{model.__name__}"
    )
    if mismatched:
        raise ContractGraphError(
            f"Agent wire model ownership differs from route: {mismatched}"
        )
    for model in models:
        reference = roots[model.__name__]
        if (
            not isinstance(reference, str)
            or not reference.startswith("#/$defs/")
            or reference.removeprefix("#/$defs/") not in exported.get("$defs", {})
        ):
            raise ContractGraphError(
                f"Agent wire model definition is absent: {model.__name__}"
            )
