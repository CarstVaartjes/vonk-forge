"""HTTP contract parity for the current operator API and CLI.

The server below is deliberately small, but it is a real FastAPI HTTP
boundary: request and response documents are validated against the checked-in
generated OpenAPI schema, and the same routes accept bearer or cookie+CSRF
authentication. This keeps CLI coverage connected to the wire contract
without rebuilding the retired service ledger in a test double.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, RefResolver
from pydantic import BaseModel, ConfigDict, Field

from cluster_profiles import cli

ROOT = Path(__file__).resolve().parents[2]
schema_path = Path(
    os.environ.get("VONK_OPERATOR_OPENAPI", str(ROOT / "schemas/control-openapi.json"))
)
OPENAPI = json.loads(schema_path.read_text())
if "/api/model/library" not in OPENAPI.get("paths", {}):
    pytest.skip("requires the current singular operator OpenAPI", allow_module_level=True)

TOKEN = "operator-test-token"
CSRF = "operator-test-csrf"
REQUEST_KEY = "00000000-0000-4000-8000-000000000099"
OPERATION_ID = "00000000-0000-4000-8000-000000000100"
PROFILE_ID = "00000000-0000-4000-8000-000000000101"
NOW = "2026-09-10T10:00:00+00:00"
SPARK = "spk_" + "1" * 32


class ModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: int = Field(default=2)
    request_key: str


class ProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str | None = None
    expected_revision: int | None = None
    assignments: list[dict[str, Any]] | None = None


def _profile() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "id": PROFILE_ID,
        "number": 1,
        "revision": 1,
        "name": "Default",
        "description": "",
        "installation_policy": "keep-cached",
        "labels": {},
        "favorite": False,
        "assignments": [],
        "profile_digest": "a" * 64,
        "created_by": "operator",
        "created_at": NOW,
        "updated_at": NOW,
    }


def _model_operation(state: str = "succeeded") -> dict[str, Any]:
    phase = "completed" if state == "succeeded" else "copying"
    return {
        "schema_version": 2,
        "action": "download",
        "selector": "qwen-code",
        "request_key": REQUEST_KEY,
        "state": state,
        "phase": phase,
        "progress": {
            "phase": phase,
            "completed_bytes": 128,
            "total_bytes": 128,
            "total_bytes_known": True,
        },
        "transferred_bytes": 128,
        "total_bytes": 128,
        "operation_id": OPERATION_ID,
    }


def _library(kind: str) -> dict[str, Any]:
    value = {
        "schema_version": 2,
        "generated_at": NOW,
        kind: [],
        "facets": {"usage": [], "family": [], "version": [], "quantization": []},
        "next_cursor": None,
        "filters": {},
        "freshness_policy": {},
    }
    return value


def _fleet() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "event_cursor": 0,
        "generated_at": NOW,
        "authority_revision": "a" * 64,
        "nodes": [
            {
                "id": SPARK,
                "display_name": "Atlas",
                "hostname": "atlas",
                "lifecycle": "active",
                "labels": {},
                "connection": {
                    "agent_state": "active",
                    "certificate_state": "valid",
                    "online_state": "online",
                    "offline_reason": None,
                    "last_seen_at": NOW,
                    "last_seen_age_seconds": 0,
                },
                "inventory": None,
                "telemetry": None,
                "installed": [],
                "loaded": [],
                "reservations": {
                    "disk_bytes": 0,
                    "unified_memory_bytes": 0,
                    "host_memory_bytes": 0,
                    "gpu_memory_bytes": 0,
                    "port_count": 0,
                },
                "warnings": [],
            }
        ],
    }


def _route_template(path: str) -> str:
    for pattern, template in (
        (r"^/api/model/operations/[^/]+$", "/api/model/operations/{operation_id}"),
        (r"^/api/model/[^/]+/(download|remove)$", "/api/model/{selector}/\\1"),
        (r"^/api/profile/[0-9]+/(load|preview|progress)$", "/api/profile/{number}/\\1"),
        (r"^/api/profile/[0-9]+$", "/api/profile/{number}"),
    ):
        match = re.match(pattern, path)
        if match:
            return template.replace("\\1", match.group(1)) if match.lastindex else template
    return path


def _validate_contract(path: str, method: str, status: int, payload: object) -> None:
    operation = OPENAPI["paths"][_route_template(path)][method.lower()]
    response = operation["responses"][str(status)]
    schema = response.get("content", {}).get("application/json", {}).get("schema")
    if schema is not None:
        Draft202012Validator(
            schema, resolver=RefResolver.from_schema(OPENAPI)
        ).validate(payload)


def _validate_request(path: str, method: str, payload: object) -> None:
    if payload is None:
        return
    operation = OPENAPI["paths"][_route_template(path)][method.lower()]
    body = operation.get("requestBody", {})
    schema = body.get("content", {}).get("application/json", {}).get("schema")
    if schema is not None:
        Draft202012Validator(
            schema, resolver=RefResolver.from_schema(OPENAPI)
        ).validate(payload)


def _auth(request: Request, *, mutation: bool = False) -> None:
    bearer = request.headers.get("authorization") == f"Bearer {TOKEN}"
    cookie = request.cookies.get("vonk_session") == TOKEN
    if not (bearer or cookie):
        raise HTTPException(status_code=401, detail="authentication required")
    if mutation and cookie and request.headers.get("x-csrf-token") != CSRF:
        raise HTTPException(status_code=403, detail="csrf required")


def _profile_application() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "id": OPERATION_ID,
        "profile_id": PROFILE_ID,
        "profile_digest": "a" * 64,
        "plan_digest": "b" * 64,
        "state": "succeeded",
        "current_step": 1,
        "total_steps": 1,
        "current_operation_id": OPERATION_ID,
        "status_reason": None,
        "progress": {},
        "result": {"changed": True, "completed_steps": 1},
        "created_at": NOW,
        "updated_at": NOW,
    }


def _app() -> FastAPI:
    app = FastAPI()
    profile = _profile()

    @app.get("/api/model/library")
    def model_library(request: Request) -> dict[str, Any]:
        _auth(request)
        return _library("models")

    @app.get("/api/recipe/library")
    def recipe_library(request: Request) -> dict[str, Any]:
        _auth(request)
        return _library("recipes")

    @app.get("/api/fleet")
    def fleet(request: Request) -> dict[str, Any]:
        _auth(request)
        return _fleet()

    @app.post("/api/model/{selector}/download", status_code=202)
    def model_download(
        selector: str, body: ModelRequest, request: Request
    ) -> dict[str, Any]:
        _auth(request, mutation=True)
        return {
            **_model_operation(),
            "selector": selector,
            "request_key": body.request_key,
        }

    @app.get("/api/model/operations/{operation_id}")
    def model_operation(operation_id: str, request: Request) -> dict[str, Any]:
        _auth(request)
        return {**_model_operation(), "operation_id": operation_id}

    @app.get("/api/profile/{number}")
    def get_profile(number: int, request: Request) -> dict[str, Any]:
        _auth(request)
        if number != 1:
            raise HTTPException(status_code=404, detail="profile not found")
        return profile

    @app.put("/api/profile/{number}")
    def save_profile(
        number: int, body: ProfileInput, request: Request
    ) -> dict[str, Any]:
        _auth(request, mutation=True)
        if number != 1:
            raise HTTPException(status_code=404, detail="profile not found")
        if body.name is not None:
            profile["name"] = body.name
        if body.assignments is not None:
            profile["assignments"] = [
                {
                    "selector": item["recipe_selector"].replace("/", "-"),
                    "display_name": item["recipe_selector"],
                    "recipe_selector": item["recipe_selector"],
                    "spark_ids": item["spark_ids"],
                    "assigned_sparks": len(item["spark_ids"]),
                }
                for item in body.assignments
            ]
        return profile

    @app.post("/api/profile/{number}/load", status_code=202)
    def load_profile(number: int, request: Request) -> dict[str, Any]:
        _auth(request, mutation=True)
        if number != 1:
            raise HTTPException(status_code=404, detail="profile not found")
        return _profile_application()

    @app.get("/api/profile/{number}/progress")
    def profile_progress(number: int, request: Request) -> dict[str, Any]:
        _auth(request)
        if number != 1:
            raise HTTPException(status_code=404, detail="profile not found")
        return _profile_application()

    return app


class HTTPTransport:
    def __init__(self, client: TestClient, *, browser: bool = False) -> None:
        self.client = client
        self.browser = browser

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        *,
        extra_headers: dict[str, str] | None = None,
        query: dict[str, object] | None = None,
    ) -> dict[str, object]:
        _validate_request(path, method, payload)
        headers = dict(extra_headers or {})
        cookies = None
        if self.browser:
            headers["x-csrf-token"] = CSRF
            cookies = {"vonk_session": TOKEN}
        else:
            headers["authorization"] = f"Bearer {TOKEN}"
        response = self.client.request(
            method, path, headers=headers, cookies=cookies, json=payload, params=query
        )
        if response.status_code >= 400:
            raise AssertionError(response.text)
        value = response.json() if response.content else {}
        _validate_contract(path, method, response.status_code, value)
        return value


def _cli(transport: HTTPTransport, *argv: str) -> dict[str, object]:
    output = StringIO()
    with redirect_stdout(output):
        code = cli.main(
            argv,
            control_client=transport,
            request_id_factory=lambda: REQUEST_KEY,
        )
    assert code == 0, output.getvalue()
    return json.loads(output.getvalue())


def test_bearer_cli_and_cookie_csrf_operator_outputs_match() -> None:
    api = TestClient(_app())
    cli_transport = HTTPTransport(api)
    browser_transport = HTTPTransport(api, browser=True)

    assert _cli(cli_transport, "model", "library", "--json") == browser_transport.request(
        "GET", "/api/model/library"
    )
    assert _cli(cli_transport, "recipe", "library", "--json") == browser_transport.request(
        "GET", "/api/recipe/library"
    )
    assert _cli(cli_transport, "--profile", "1", "profile", "--json") == browser_transport.request(
        "GET", "/api/profile/1"
    )

    request = {"schema_version": 2, "request_key": REQUEST_KEY}
    assert _cli(cli_transport, "model", "download", "qwen-code", "--json") == browser_transport.request(
        "POST", "/api/model/qwen-code/download", request
    )

    assignment = {
        "recipe_selector": "vonk-forge/qwen-code",
        "spark_ids": [SPARK],
        "desired_state": "running",
    }
    profile_body = {
        "name": "Default",
        "assignments": [assignment],
        "expected_revision": 1,
    }
    assert _cli(
        cli_transport,
        "--profile",
        "1",
        "profile",
        "add",
        "vonk-forge/qwen-code",
        "--spark",
        SPARK,
        "--json",
    ) == browser_transport.request("PUT", "/api/profile/1", profile_body)

    load_request = {"request_key": REQUEST_KEY}
    assert _cli(cli_transport, "--profile", "1", "profile", "load", "--json") == browser_transport.request(
        "POST", "/api/profile/1/load", load_request
    )
    assert _cli(cli_transport, "--profile", "1", "profile", "progress", "--json") == browser_transport.request(
        "GET", "/api/profile/1/progress"
    )
