from __future__ import annotations

import uuid
from typing import Any, cast
from unittest.mock import Mock

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.installation_reconciliation_api import (
    INSTALLATION_RECONCILIATION_OPERATION_IDS,
    install_installation_reconciliation_routes,
)
from vonk_control.operation_api import durable_operation_services
from vonk_control.run_switch_contract import RunSwitchCleanupApplyRequest
from vonk_control.run_switch_operations import RunSwitchOperationConflict

from .test_recipe_operations import NOW, installed_recipe, setup_services
from .test_run_switch_operations import RecordingArtifactExecutor, _service

INSTALLATION_ID = "00000000-0000-4000-8000-000000000001"
OPERATION_ID = "00000000-0000-4000-8000-000000000002"
REQUEST_KEY = "00000000-0000-4000-8000-000000000003"
PLAN_DIGEST = "a" * 64


def _actor(role: str = "administrator") -> Actor:
    return Actor("test-actor", role)


def _client(
    operations: Mock, *, role: str = "administrator"
) -> tuple[TestClient, MemoryAuditStore]:
    app = FastAPI()

    @app.middleware("http")
    async def request_identity(request, call_next):
        request.state.request_id = "00000000-0000-4000-8000-000000000004"
        return await call_next(request)

    audits = MemoryAuditStore()
    install_installation_reconciliation_routes(
        app,
        actor_dependency=Depends(lambda: _actor(role)),
        operations=operations,
        audits=audits,
    )
    return TestClient(app), audits


def test_reconciliation_routes_are_typed_and_discoverable() -> None:
    app = FastAPI()
    install_installation_reconciliation_routes(
        app,
        actor_dependency=Depends(_actor),
        operations=None,
        audits=MemoryAuditStore(),
    )

    schema = app.openapi()
    paths = schema["paths"]
    preview = paths["/api/recipe/installations/{installation_id}/reconcile/preview"][
        "post"
    ]
    assert preview["requestBody"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("RunSwitchCleanupPreviewRequest")
    assert preview["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("RunSwitchPlan")
    apply = paths["/api/recipe/installations/{installation_id}/reconcile"]["post"]
    assert apply["requestBody"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("RunSwitchCleanupApplyRequest")
    assert apply["responses"]["202"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("RunSwitchOperation")
    assert paths["/api/run-switch/operations/{operation_id}"]["get"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"]["$ref"].endswith("RunSwitchOperation")
    for (
        method,
        path,
    ), operation_id in INSTALLATION_RECONCILIATION_OPERATION_IDS.items():
        assert paths[path][method]["operationId"] == operation_id


def test_preview_requires_path_identity_and_explicit_reconcile_mode() -> None:
    operations = Mock()
    client, _audits = _client(operations)
    path = f"/api/recipe/installations/{INSTALLATION_ID}/reconcile/preview"

    wrong_installation = client.post(
        path,
        json={
            "schema_version": 2,
            "installation_id": OPERATION_ID,
            "cleanup_mode": "reconcile",
        },
    )
    wrong_mode = client.post(
        path,
        json={"schema_version": 2, "installation_id": INSTALLATION_ID},
    )

    assert wrong_installation.status_code == 422
    assert wrong_mode.status_code == 422
    operations.preview_cleanup.assert_not_called()


def test_preview_delegates_reconcile_mode_to_existing_run_switch_owner() -> None:
    operations = Mock()
    operations.preview_cleanup.side_effect = RunSwitchOperationConflict(
        "run-switch.reconciliation-unavailable"
    )
    client, _audits = _client(operations)

    response = client.post(
        f"/api/recipe/installations/{INSTALLATION_ID}/reconcile/preview",
        json={
            "schema_version": 2,
            "installation_id": INSTALLATION_ID,
            "cleanup_mode": "reconcile",
        },
    )

    assert response.status_code == 409
    request = operations.preview_cleanup.call_args.args[0]
    assert request.installation_id == INSTALLATION_ID
    assert request.cleanup_mode == "reconcile"
    assert operations.preview_cleanup.call_args.kwargs == {"actor": "test-actor"}


def test_apply_is_administrator_only_and_preserves_reviewed_request_identity() -> None:
    operations = Mock()
    operations.apply_cleanup.side_effect = RunSwitchOperationConflict(
        "run-switch.reconciliation-stale-plan"
    )
    client, audits = _client(operations, role="operator")
    path = f"/api/recipe/installations/{INSTALLATION_ID}/reconcile"
    body = {
        "schema_version": 2,
        "installation_id": INSTALLATION_ID,
        "cleanup_mode": "reconcile",
        "plan_digest": PLAN_DIGEST,
        "request_key": REQUEST_KEY,
    }

    forbidden = client.post(path, json=body)
    assert forbidden.status_code == 403
    operations.apply_cleanup.assert_not_called()

    admin_client, admin_audits = _client(operations)
    rejected = admin_client.post(path, json=body)

    assert rejected.status_code == 409
    request = operations.apply_cleanup.call_args.args[0]
    assert request.installation_id == INSTALLATION_ID
    assert request.cleanup_mode == "reconcile"
    assert request.plan_digest == PLAN_DIGEST
    assert request.request_key == REQUEST_KEY
    assert audits.list() == []
    assert admin_audits.list() == []


def test_run_switch_status_is_a_typed_exact_id_lookup() -> None:
    operations = Mock()
    operations.get.side_effect = KeyError(OPERATION_ID)
    client, _audits = _client(operations)

    response = client.get(f"/api/run-switch/operations/{OPERATION_ID}")

    assert response.status_code == 404
    operations.get.assert_called_once_with(OPERATION_ID)


def test_request_lookup_api_uses_real_run_switch_provider_and_lifecycle_child(
    tmp_path,
    monkeypatch,
) -> None:
    sessions, lifecycle, queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    service = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
    request_key = str(uuid.uuid4())
    preview = service.preview_cleanup(installation.owner_id, actor="test-actor")
    assert preview.allowed is True
    accepted = service.apply_cleanup(
        RunSwitchCleanupApplyRequest(
            installation_id=installation.owner_id,
            request_key=request_key,
        ),
        actor="test-actor",
    )
    assert service.tick() is True
    started = service.get(accepted.operation_id)
    child_id = started.result.child_operation_id if started.result is not None else None
    assert child_id is not None

    tokens = TokenCodec(b"installation-reconciliation-test-key-32b")
    operation_api = durable_operation_services(
        sessions,
        tmp_path / "routes",
        clock=lambda: NOW,
        cursors=tokens.cursor_codec(),
        operation_providers=(service.activity_provider(),),
    )
    app = create_app(
        jobs=cast(Any, queue),
        tokens=tokens,
        audits=MemoryAuditStore(),
        operations=operation_api,
        run_switch_operations=service,
        now=lambda: int(NOW.timestamp()),
    )
    bearer = tokens.issue(
        Actor("test-actor", "administrator"),
        ttl_seconds=3600,
        now=int(NOW.timestamp()),
    )
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {bearer}"}

    lookup = client.get(
        "/api/operations",
        params={"request_id": request_key, "limit": 100},
        headers=headers,
    )

    assert lookup.status_code == 200, lookup.text
    page = lookup.json()
    assert page["total"] == len(page["operations"])
    assert page.get("next_cursor") is None
    parent_rows = [
        item for item in page["operations"] if item["kind"] == "recipe.cleanup.v2"
    ]
    assert len(parent_rows) == 1
    parent = parent_rows[0]
    assert parent["id"] == accepted.operation_id
    assert parent["owner"] == {
        "kind": "job",
        "id": accepted.operation_id,
        "request_id": request_key,
    }

    from sqlalchemy import select
    from vonk_control.models import AgentOperation

    with sessions() as session:
        child_operations = list(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == child_id)
            )
        )
    assert child_operations, "cleanup must dispatch lifecycle child operations"

    status = client.get(
        f"/api/run-switch/operations/{accepted.operation_id}", headers=headers
    )
    assert status.status_code == 200, status.text
    operation = status.json()
    assert operation["operation_id"] == accepted.operation_id
    assert operation["request_key"] == request_key
    assert operation["plan_digest"] == preview.plan_digest
    assert operation["cleanup_mode"] == "uninstall"
    assert operation["installation_id"] == installation.owner_id
    assert operation["result"]["child_operation_id"] == child_id

    monkeypatch.setattr(
        service,
        "get",
        Mock(side_effect=RuntimeError("private stored-operation detail")),
    )
    unavailable = client.get(
        f"/api/run-switch/operations/{accepted.operation_id}", headers=headers
    )
    assert unavailable.status_code == 500
    assert "private stored-operation detail" not in unavailable.text
    assert (
        unavailable.json()["context"]["request_id"]
        == unavailable.headers["x-request-id"]
    )
