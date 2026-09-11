"""Recorder regressions; these tests are not endpoint coverage evidence."""

import json

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Route
from vonk_control.contract_graph import schema_application

from .api_response_witness import ResponseWitnesses, install_observer


@pytest.fixture
def recorder():
    collector = ResponseWitnesses()
    collector.test_id = "recorder-regression"
    original, handle = install_observer(collector)
    try:
        yield collector
    finally:
        FastAPI.__call__, APIRoute.handle = original, handle


def test_actual_health_response_survives_scope_copy_and_nested_app(recorder):
    child = schema_application(browser_auth=False)
    parent = FastAPI()
    parent.mount("/nested", child)
    response = TestClient(parent).get("/nested/api/healthz")
    assert response.status_code == 200
    recorder.flush()
    assert list(recorder.successes) == [
        ("GET", "/nested/api/healthz", 200, "application/json")
    ]
    assert next(iter(recorder.successes.values()))["response_count"] == 1


def test_actual_final_middleware_bytes_are_validated_without_leaking_values(recorder):
    app = schema_application(browser_auth=False)

    class CorruptResponse:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            async def corrupt(message):
                if message["type"] == "http.response.body" and message.get("body"):
                    message = {**message, "body": b'{"secret": "must-not-appear"}'}
                await send(message)

            await self.app(scope, receive, corrupt)

    app.add_middleware(CorruptResponse)
    assert TestClient(app).get("/api/healthz").status_code == 200
    with pytest.raises(AssertionError, match="schema_") as error:
        recorder.flush()
    assert "must-not-appear" not in str(error.value)
    assert "must-not-appear" not in json.dumps(recorder.report())
    assert not recorder.successes


@pytest.mark.parametrize(
    ("media", "schema", "body"),
    [
        ("text/plain", {"type": "string"}, b"a metric 1\n"),
        ("image/png", {"type": "string", "format": "binary"}, b"\x89PNG\xff"),
        ("text/event-stream", {"type": "integer"}, b"data: 1\n\ndata: 2\n\n"),
    ],
)
def test_transport_decoder_uses_declared_media(recorder, media, schema, body):
    # Rebind the declaration on an actual route only to test the observer decoder.
    app = schema_application(browser_auth=False)
    route = next(r for r in app.routes if getattr(r, "path", "") == "/api/healthz")
    assert isinstance(route, Route)
    app.openapi()["paths"][route.path]["get"]["responses"]["200"] = {
        "content": {media: {"schema": schema}}
    }
    recorder.validate(
        app,
        {"method": "GET", "route": route},
        200,
        [(b"content-type", media.encode())],
        body,
    )
    assert len(recorder.successes) == 1


def test_failed_requests_are_not_success_evidence_and_missing_routes_are_reported(
    recorder,
):
    app = schema_application(browser_auth=False)
    assert TestClient(app).get("/api/jobs").status_code == 401
    recorder.flush()
    report = recorder.report()
    assert report["witnessed_operation_count"] == 0
    assert {"method": "GET", "path": "/api/healthz"} in report["missing_operations"]
