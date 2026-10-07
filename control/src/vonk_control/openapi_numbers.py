"""Emit canonical numeric schema values through FastAPI's OpenAPI assembler.

FastAPI0.141.1's final OpenAPI model coerces JSON Schema numeric keywords into
float. The pinned assembler below is upstream get_openapi (MIT, FastAPI
contributors), with only its final return adapted. Pydantic remains the schema
owner; FastAPI still owns route construction and document validation.
Upstream source SHA256:41a50551f99619f9333ac64edf7cd397b64f721244b89a82916c69f233239def.
"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from typing import Any

from fastapi import FastAPI, routing
from fastapi._compat import (
    get_definitions,
    get_flat_models_from_fields,
    get_model_name_map,
    get_schema_from_model_field,
)
from fastapi.encoders import jsonable_encoder
from fastapi.openapi.models import OpenAPI
from fastapi.openapi.utils import (
    _get_api_route_for_openapi,
    get_fields_from_routes,
    get_openapi_path,
)
from pydantic.json_schema import models_json_schema
from starlette.routing import BaseRoute

UPSTREAM_ASSEMBLER_SHA256 = (
    "41a50551f99619f9333ac64edf7cd397b64f721244b89a82916c69f233239def"
)
_NUMERIC_KEYWORDS = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "const",
        "enum",
    }
)


def _restore_numeric_keywords(raw: Any, rendered: Any) -> None:
    """Retain all authored numeric rules, at every schema/inline position."""
    if isinstance(raw, dict) and isinstance(rendered, dict):
        for key, value in raw.items():
            if key in _NUMERIC_KEYWORDS and key in rendered:
                rendered[key] = deepcopy(value)
            elif key in rendered:
                _restore_numeric_keywords(value, rendered[key])
    elif isinstance(raw, list) and isinstance(rendered, list):
        for original, item in zip(raw, rendered):
            _restore_numeric_keywords(original, item)


def install_canonical_openapi(app: FastAPI) -> None:
    """Use the same precise document for server docs and packaged consumers."""

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = canonical_openapi(
                title=app.title,
                version=app.version,
                openapi_version=app.openapi_version,
                summary=app.summary,
                description=app.description,
                routes=app.routes,
                webhooks=app.webhooks.routes,
                tags=app.openapi_tags,
                servers=app.servers,
                terms_of_service=app.terms_of_service,
                contact=app.contact,
                license_info=app.license_info,
                separate_input_output_schemas=app.separate_input_output_schemas,
                external_docs=app.openapi_external_docs,
            )
        return app.openapi_schema

    app.openapi = openapi


def canonical_openapi(
    *,
    title: str,
    version: str,
    openapi_version: str = "3.1.0",
    summary: str | None = None,
    description: str | None = None,
    routes: Sequence[BaseRoute | routing.RouteContext],
    webhooks: Sequence[BaseRoute | routing.RouteContext] | None = None,
    tags: list[dict[str, Any]] | None = None,
    servers: list[dict[str, str | Any]] | None = None,
    terms_of_service: str | None = None,
    contact: dict[str, str | Any] | None = None,
    license_info: dict[str, str | Any] | None = None,
    separate_input_output_schemas: bool = True,
    external_docs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    info: dict[str, Any] = {"title": title, "version": version}
    if summary:
        info["summary"] = summary
    if description:
        info["description"] = description
    if terms_of_service:
        info["termsOfService"] = terms_of_service
    if contact:
        info["contact"] = contact
    if license_info:
        info["license"] = license_info
    output: dict[str, Any] = {"openapi": openapi_version, "info": info}
    if servers:
        output["servers"] = servers
    components: dict[str, dict[str, Any]] = {}
    paths: dict[str, dict[str, Any]] = {}
    webhook_paths: dict[str, dict[str, Any]] = {}
    operation_ids: set[str] = set()
    all_fields = get_fields_from_routes(list(routes) + list(webhooks or []))
    flat_models = get_flat_models_from_fields(all_fields, known_models=set())
    model_name_map = get_model_name_map(flat_models)
    field_mapping, definitions = get_definitions(
        fields=all_fields,
        model_name_map=model_name_map,
        separate_input_output_schemas=separate_input_output_schemas,
    )
    # Streaming observations transport bytes of these original canonical
    # models. They remain generated payload contracts even though an individual
    # HTTP record carries a transfer envelope rather than the whole model.
    from .fleet_projection import FleetSnapshot
    from .platform_observation import PlatformObservation

    _, payload_graph = models_json_schema(
        [(FleetSnapshot, "serialization"), (PlatformObservation, "serialization")],
        ref_template="#/components/schemas/{model}",
    )
    for name, definition in payload_graph.get("$defs", {}).items():
        definitions.setdefault(name, definition)
    for route_context in routing.iter_route_contexts(routes):
        api_route = _get_api_route_for_openapi(route_context)
        if api_route is not None:
            result = get_openapi_path(
                route=api_route,
                operation_ids=operation_ids,
                model_name_map=model_name_map,
                field_mapping=field_mapping,
                separate_input_output_schemas=separate_input_output_schemas,
            )
            if result:
                path, security_schemes, path_definitions = result
                if path:
                    # FastAPI defaults a non-JSON response class to a raw
                    # string, then merges an additional response model into it.
                    # Our declared record/frame schema describes each decoded
                    # record, so retain its exact owning model field instead
                    # of the impossible string-and-object intersection.
                    stream_field = api_route.response_fields.get(200)
                    if stream_field is not None:
                        for operation in path.values():
                            if not isinstance(operation, dict) or not (
                                operation.get("x-vonk-response-record-max-bytes")
                                or operation.get("x-vonk-response-frame-max-bytes")
                            ):
                                continue
                            record_schema = get_schema_from_model_field(
                                field=stream_field,
                                model_name_map=model_name_map,
                                field_mapping=field_mapping,
                                separate_input_output_schemas=separate_input_output_schemas,
                            )
                            for media in operation["responses"]["200"][
                                "content"
                            ].values():
                                media["schema"] = deepcopy(record_schema)
                    paths.setdefault(api_route.path_format, {}).update(path)
                if security_schemes:
                    components.setdefault("securitySchemes", {}).update(
                        security_schemes
                    )
                if path_definitions:
                    definitions.update(path_definitions)
    for webhook_context in routing.iter_route_contexts(webhooks or []):
        api_webhook = _get_api_route_for_openapi(webhook_context)
        if api_webhook is not None:
            result = get_openapi_path(
                route=api_webhook,
                operation_ids=operation_ids,
                model_name_map=model_name_map,
                field_mapping=field_mapping,
                separate_input_output_schemas=separate_input_output_schemas,
            )
            if result:
                path, security_schemes, path_definitions = result
                if path:
                    webhook_paths.setdefault(api_webhook.path_format, {}).update(path)
                if security_schemes:
                    components.setdefault("securitySchemes", {}).update(
                        security_schemes
                    )
                if path_definitions:
                    definitions.update(path_definitions)
    if definitions:
        components["schemas"] = {k: definitions[k] for k in sorted(definitions)}
    if components:
        output["components"] = components
    output["paths"] = paths
    if webhook_paths:
        output["webhooks"] = webhook_paths
    if tags:
        output["tags"] = tags
    if external_docs:
        output["externalDocs"] = external_docs
    document = jsonable_encoder(OpenAPI(**output), by_alias=True, exclude_none=True)
    _restore_numeric_keywords(output, document)
    return document
