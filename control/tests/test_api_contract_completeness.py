"""Mutations prove new routes cannot silently escape the discovered graph."""

import json
from pathlib import Path

import pytest
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel
from vonk_control.contract_graph import (
    ContractGraphError,
    discover_contracts,
    raw_json_body,
    require_wire_exports,
    schema_application,
)
from vonk_control.operation_api import admin_openapi_schema


class AddedRequest(BaseModel):
    value: str


class AddedResponse(BaseModel):
    accepted: bool


def added(body: AddedRequest) -> AddedResponse:
    return AddedResponse(accepted=bool(body.value))


@pytest.mark.parametrize("browser_auth", [False, True])
def test_all_mounted_routes_and_typed_agent_roots_are_registered(browser_auth):
    app = schema_application(browser_auth=browser_auth)
    operations, models = discover_contracts(app)
    wire = (
        Path(__file__).resolve().parents[2]
        / "rust/crates/vonk-agent-protocol/schema/wire.json"
    )
    require_wire_exports(models, json.loads(wire.read_text()))
    assert (
        bool([op for op in operations if op["path"] == "/api/auth/login"])
        is browser_auth
    )
    agent = [op for op in operations if "/recipe-jobs/" in op["path"]]
    assert {op["method"] for op in agent} == {"GET", "PUT"}
    assert all(
        not path.startswith("/agent/") for path in admin_openapi_schema(app)["paths"]
    )
    assert "/metrics" not in admin_openapi_schema(app)["paths"]


def test_hidden_new_route_fails_even_when_absent_from_openapi():
    app = FastAPI()
    app.add_api_route("/new", added, methods=["POST"], include_in_schema=False)
    assert "/new" not in app.openapi()["paths"]
    with pytest.raises(ContractGraphError, match="hidden route: /new"):
        discover_contracts(app)


def test_mounted_child_routes_are_discovered_and_missing_exports_fail():
    app, child = FastAPI(), FastAPI()
    child.add_api_route("/new", added, methods=["POST"])
    app.mount("/agent", child)
    operations, models = discover_contracts(app)
    assert operations[0]["path"] == "/agent/new"
    assert models == {AddedRequest, AddedResponse}
    with pytest.raises(ContractGraphError, match="AddedRequest"):
        require_wire_exports(
            models, {"x-vonk-models": {"AddedResponse": "#/defs/AddedResponse"}}
        )


def test_plain_response_cannot_claim_a_contract_by_existing_in_openapi():
    app = FastAPI()

    def untyped():
        return {}

    app.add_api_route("/new", untyped, methods=["GET"])
    with pytest.raises(ContractGraphError, match="Untyped response"):
        discover_contracts(app)


async def raw_reader(request: Request) -> AddedResponse:
    await request.body()
    return AddedResponse(accepted=True)


def test_raw_reader_requires_declared_body_and_canonical_json_binding():
    app = FastAPI()
    app.add_api_route("/raw", raw_reader, methods=["POST"])
    with pytest.raises(ContractGraphError, match="request body"):
        discover_contracts(app)
    app = FastAPI()
    app.add_api_route(
        "/raw",
        raw_reader,
        methods=["POST"],
        openapi_extra={
            "requestBody": {
                "content": {
                    "application/json": {"schema": AddedRequest.model_json_schema()}
                }
            },
        },
    )
    with pytest.raises(ContractGraphError, match="no canonical binding"):
        discover_contracts(app)


def test_bounded_raw_model_is_discovered_without_an_exporter_name_list():
    app = FastAPI()

    async def bounded(request: Request) -> AddedResponse:
        return AddedResponse(accepted=True)

    app.add_api_route(
        "/agent/raw",
        raw_json_body(AddedRequest)(bounded),
        methods=["POST"],
        openapi_extra={
            "requestBody": {
                "content": {
                    "application/json": {"schema": AddedRequest.model_json_schema()}
                }
            },
        },
    )
    _, models = discover_contracts(app)
    assert models == {AddedRequest, AddedResponse}


def test_unknown_mounted_transport_fails_closed():
    app = FastAPI()

    async def opaque(scope, receive, send):
        pass

    app.mount("/opaque", opaque)
    with pytest.raises(ContractGraphError, match="mounted transport: /opaque"):
        discover_contracts(app)


def test_raw_json_declaration_must_match_its_actual_model():
    app = FastAPI()

    async def bounded(request: Request) -> AddedResponse:
        return AddedResponse(accepted=True)

    app.add_api_route(
        "/agent/raw",
        raw_json_body(AddedRequest)(bounded),
        methods=["POST"],
        openapi_extra={
            "requestBody": {
                "content": {"application/json": {"schema": {"type": "string"}}}
            },
        },
    )
    with pytest.raises(ContractGraphError, match="differs from canonical"):
        discover_contracts(app)


def test_exported_name_cannot_impersonate_another_model_owner():
    with pytest.raises(ContractGraphError, match="ownership differs"):
        require_wire_exports(
            {AddedRequest},
            {
                "x-vonk-models": {"AddedRequest": "#/$defs/AddedRequest"},
                "x-vonk-model-sources": {"AddedRequest": "other_module.AddedRequest"},
            },
        )


def test_request_delegation_cannot_hide_an_unclassified_body():
    app = FastAPI()

    async def delegated(request: Request) -> AddedResponse:
        return AddedResponse(accepted=True)

    app.add_api_route("/delegate", delegated, methods=["POST"])
    with pytest.raises(ContractGraphError, match="Unclassified raw request body"):
        discover_contracts(app)


def test_exporter_discovers_new_api_models_without_a_curated_name_entry(monkeypatch):
    import runpy

    from vonk_control import contract_graph

    def application(**_kwargs):
        app = FastAPI()
        app.add_api_route("/agent/new", added, methods=["POST"])
        return app

    monkeypatch.setattr(contract_graph, "schema_application", application)
    exporter = Path(__file__).resolve().parents[2] / "scripts/export-agent-wire-schema"
    exported = runpy.run_path(str(exporter))["export_schema"]()
    require_wire_exports({AddedRequest, AddedResponse}, exported)


def test_stream_flag_cannot_hide_an_untyped_json_response():
    app = FastAPI()

    def response():
        return {}

    app.add_api_route(
        "/stream",
        response,
        methods=["GET"],
        openapi_extra={
            "x-vonk-streaming-transport": True,
            "responses": {
                "200": {"content": {"application/json": {"schema": {"type": "object"}}}}
            },
        },
    )
    with pytest.raises(ContractGraphError, match="no canonical or exact-byte binding"):
        discover_contracts(app)


def test_custom_transport_cannot_hide_behind_a_framework_documentation_path():
    app = FastAPI()
    app.add_route("/docs", raw_reader)
    with pytest.raises(ContractGraphError, match="Unregistered transport: /docs"):
        discover_contracts(app)


@pytest.mark.parametrize("invalid", [None, "body", "header"])
def test_head_response_uses_typed_headers_without_a_body(invalid):
    app = FastAPI()
    response = {
        "description": "Upload cursor",
        "headers": {"x-upload-offset": {"schema": {"type": "integer", "minimum": 0}}},
    }
    if invalid == "body":
        response["content"] = {"application/json": {"schema": {"type": "object"}}}
    elif invalid == "header":
        response["headers"]["x-upload-offset"] = {}

    def head():
        return Response(headers={"x-upload-offset": "0"})

    app.add_api_route(
        "/upload",
        head,
        methods=["HEAD"],
        response_class=Response,
        responses={200: response},
    )
    if invalid:
        with pytest.raises(
            ContractGraphError,
            match=(
                "No-content response"
                if invalid == "body"
                else "Untyped response header"
            ),
        ):
            discover_contracts(app)
    else:
        operations, _ = discover_contracts(app)
        assert operations[0]["method"] == "HEAD"
