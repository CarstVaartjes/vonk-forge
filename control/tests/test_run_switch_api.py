from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor
from vonk_control.operation_api import admin_openapi_schema
from vonk_control.run_switch_api import (
    RUN_SWITCH_OPERATION_IDS,
    RunSwitchConflictResponse,
    install_run_switch_routes,
)
from vonk_control.run_switch_operations import RunSwitchOperationConflict


def _administrator() -> Actor:
    return Actor("test", "administrator")


def test_run_switch_conflict_is_strict_and_declared_on_mutating_routes() -> None:
    response = RunSwitchConflictResponse(
        detail="operation is already active",
        request_id="00000000-0000-4000-8000-000000000001",
    )
    assert response.model_dump(mode="json") == {
        "code": "run-switch.operation_conflict",
        "detail": "operation is already active",
        "request_id": "00000000-0000-4000-8000-000000000001",
    }
    with pytest.raises(ValidationError):
        RunSwitchConflictResponse.model_validate(
            {
                "code": "run-switch.operation_conflict",
                "detail": "conflict",
                "request_id": "00000000-0000-4000-8000-000000000001",
                "unexpected": True,
            }
        )

    app = FastAPI()
    install_run_switch_routes(
        app,
        actor_dependency=Depends(_administrator),
        audits=MemoryAuditStore(),
        service=None,
    )
    schema = admin_openapi_schema(app)
    for (method, path), operation_id in RUN_SWITCH_OPERATION_IDS.items():
        operation = schema["paths"][path][method]
        assert operation["operationId"] == operation_id
        if operation_id in {
            "applyRecipeRunSwitch",
            "retryRecipeRunSwitchOperation",
            "applyRecipeRunSwitchStop",
        }:
            assert operation["responses"]["409"]["content"]["application/json"][
                "schema"
            ] == {"$ref": "#/components/schemas/RunSwitchConflictResponse"}


def test_run_switch_conflict_route_serializes_the_declared_model() -> None:
    class ConflictService:
        def apply(self, *_args, **_kwargs):
            raise RunSwitchOperationConflict("plan is stale")

    app = FastAPI()

    @app.middleware("http")
    async def request_identity(request, call_next):
        request.state.request_id = "00000000-0000-4000-8000-000000000001"
        return await call_next(request)

    install_run_switch_routes(
        app,
        actor_dependency=Depends(_administrator),
        audits=MemoryAuditStore(),
        service=ConflictService(),
    )
    response = TestClient(app).post(
        "/api/v1/recipes/run-switches",
        json={
            "schema_version": 2,
            "model_content_sha256": "a" * 64,
            "recipe_revision_id": "00000000-0000-4000-8000-000000000002",
            "spark_group": {
                "nodes": [
                    {
                        "node_id": "spk_" + "a" * 32,
                        "rank": 0,
                        "role": "entrypoint",
                        "endpoint_owner": True,
                    }
                ]
            },
            "alias": "demo",
            "request_key": "00000000-0000-4000-8000-000000000003",
        },
    )

    assert response.status_code == 409
    assert RunSwitchConflictResponse.model_validate_json(response.content).detail == (
        "plan is stale"
    )
