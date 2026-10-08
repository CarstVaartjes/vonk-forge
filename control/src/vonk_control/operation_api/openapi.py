"""Operation Api: openapi."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Annotated

from fastapi import FastAPI
from pydantic.json_schema import JsonSchemaValue

from ..stored_json import ExternalPassthrough
from .constants import _ADMIN_OPERATION_IDS, _HTTP_METHODS

type ExternalSchemaDocument = Annotated[
    JsonSchemaValue,
    ExternalPassthrough("OpenAPI and JSON Schema are external specification documents"),
]


def admin_openapi_schema(app: FastAPI) -> ExternalSchemaDocument:
    """Return the deterministic authenticated admin surface without agent APIs."""

    source = deepcopy(app.openapi())
    paths: ExternalSchemaDocument = {}
    browser_auth_paths = {
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/session",
        "/api/auth/cli-token",
    }
    for path, path_item in source.get("paths", {}).items():
        if path in {"/api/healthz", "/api/readyz"}:
            continue
        if not path.startswith("/api/"):
            continue
        selected = deepcopy(path_item)
        for method, operation in selected.items():
            if method not in _HTTP_METHODS:
                continue
            try:
                operation["operationId"] = _ADMIN_OPERATION_IDS[(method, path)]
            except KeyError as error:
                raise RuntimeError(
                    f"admin operation ID is not explicit for {method.upper()} {path}"
                ) from error
            if path == "/api/auth/login":
                operation["security"] = []
            elif path in browser_auth_paths:
                operation["security"] = [{"BrowserSession": []}]
            else:
                operation["security"] = [{"BearerAuth": []}, {"BrowserSession": []}]
        paths[path] = selected
    source["paths"] = paths
    components = source.setdefault("components", {})
    components["securitySchemes"] = {"BearerAuth": {"scheme": "bearer", "type": "http"}}
    components["securitySchemes"]["BrowserSession"] = {
        "in": "cookie",
        "name": "vonk_session",
        "type": "apiKey",
    }

    referenced: set[str] = set()

    def collect(value: object) -> None:
        if isinstance(value, Mapping):
            reference = value.get("$ref")
            if isinstance(reference, str) and reference.startswith(
                "#/components/schemas/"
            ):
                referenced.add(reference.rsplit("/", 1)[-1])
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(paths)
    schemas = components.get("schemas", {})
    pending = list(referenced)
    while pending:
        name = pending.pop()
        before = set(referenced)
        collect(schemas.get(name, {}))
        pending.extend(sorted(referenced - before))
    components["schemas"] = {
        name: schemas[name] for name in sorted(referenced) if name in schemas
    }
    # The lifecycle and error vocabulary is one closed set shared with the agent
    # and the generated clients; no route has to mention a word for it to exist.
    for name, schema in _contract_component_schemas().items():
        if components["schemas"].setdefault(name, schema) != schema:
            raise RuntimeError(
                f"contract component {name} conflicts with a route model"
            )
    stored_schemas, json_columns = _stored_component_schemas()
    for name, schema in stored_schemas.items():
        # A model a route also exposes keeps the route's description of it.
        components["schemas"].setdefault(name, schema)
    components["schemas"] = dict(sorted(components["schemas"].items()))
    source["x-vonk-json-columns"] = json_columns
    return source


def _contract_component_schemas() -> dict[str, ExternalSchemaDocument]:
    from pydantic.json_schema import models_json_schema
    from vonk_agent_protocol import (
        ErrorCatalog,
        LifecycleVocabulary,
        ReasonCodeVocabulary,
    )

    from ..auth_api import CliTokenDownload

    _references, document = models_json_schema(
        [
            (LifecycleVocabulary, "validation"),
            (ReasonCodeVocabulary, "validation"),
            (ErrorCatalog, "validation"),
            (CliTokenDownload, "validation"),
        ],
        ref_template="#/components/schemas/{model}",
    )
    return deepcopy(document["$defs"])


def _stored_component_schemas() -> tuple[
    dict[str, ExternalSchemaDocument], dict[str, list[str]]
]:
    """The contracts of the JSON columns, and where each column's contract is.

    Every contract the JSON-column registry binds is published, so a generated
    client (TypeScript, Python) reads a stored document exactly as the
    Controller does.  They are described as the Controller writes them
    (serialization mode).  The second answer maps ``table.column`` to the
    component names of its contract's models (one per kind for a column that
    stores several document families).
    """

    from pydantic import BaseModel
    from pydantic.json_schema import JsonSchemaMode, models_json_schema

    from ..stored_json import bindings, contract_models

    stored = {binding.key: contract_models(binding) for binding in bindings().values()}
    members: list[tuple[type[BaseModel], JsonSchemaMode]] = [
        (model, "serialization") for models in stored.values() for model in models
    ]
    references, document = models_json_schema(
        members, ref_template="#/components/schemas/{model}"
    )
    columns = {
        key: sorted(
            {
                str(references[(model, "serialization")]["$ref"]).rsplit("/", 1)[-1]
                for model in models
            }
        )
        for key, models in sorted(stored.items())
        if models
    }
    return deepcopy(document["$defs"]), columns
