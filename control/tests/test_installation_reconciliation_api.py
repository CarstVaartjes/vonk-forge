from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta
from typing import Any, cast
from unittest.mock import Mock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from vonk_agent_protocol import RecipeReconcilePayload, canonical_message
from vonk_control.api import create_app
from vonk_control.auth import Actor, TokenCodec
from vonk_control.install_admission import installation_plan_digest_from_stored_document
from vonk_control.installation_reconciliation_api import (
    INSTALLATION_RECONCILIATION_OPERATION_IDS,
    install_installation_reconciliation_routes,
)
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Job,
    RecipeInstallation,
)
from vonk_control.operation_api import durable_operation_services
from vonk_control.run_switch_operations import RunSwitchOperationConflict

from .test_recipe_operations import NOW, installed_recipe, setup_services
from .test_run_switch_operations import RecordingArtifactExecutor, _service

INSTALLATION_ID = "00000000-0000-4000-8000-000000000001"
OPERATION_ID = "00000000-0000-4000-8000-000000000002"
REQUEST_KEY = "00000000-0000-4000-8000-000000000003"
PLAN_DIGEST = "a" * 64


def _actor(role: str = "administrator") -> Actor:
    return Actor("test-actor", role)


def _client(operations: Mock, *, role: str = "administrator") -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def request_identity(request, call_next):
        request.state.request_id = "00000000-0000-4000-8000-000000000004"
        return await call_next(request)

    install_installation_reconciliation_routes(
        app,
        actor_dependency=Depends(lambda: _actor(role)),
        operations=operations,
    )
    return TestClient(app)


def _prepare_exact_legacy_install_for_reconciliation(
    sessions,
    installation_id: str,
    node_ids: tuple[str, ...],
) -> None:
    """Represent a successful install whose old launch spec needs reconciliation."""

    with sessions.begin() as session:
        installation = session.get(RecipeInstallation, installation_id)
        assert installation is not None
        stored_plan = json.loads(json.dumps(installation.plan))
        install_job = session.scalar(
            select(Job).where(
                Job.kind == "recipe.install",
                Job.payload["owner_id"].as_string() == installation_id,
            )
        )
        assert install_job is not None and install_job.result is not None
        operations = tuple(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == install_job.id)
                .order_by(AgentOperation.node_id)
            )
        )
        assert {operation.node_id for operation in operations} == set(node_ids)
        for index, operation in enumerate(operations):
            payload = json.loads(json.dumps(operation.payload))
            compiled = payload["compiled_execution_plan"]
            placement = compiled["runtime"]["placement"]
            assert "memory_floor_bytes" in placement
            placement.pop("memory_floor_bytes")
            payload["compiled_execution_plan"] = compiled
            operation.payload = payload
            operation.payload_digest = hashlib.sha256(
                canonical_message(payload)
            ).hexdigest()
            operation.current_attempt = 1
            session.add(
                AgentOperationAttempt(
                    operation_id=operation.id,
                    attempt=1,
                    fence=str(uuid.uuid4()),
                    lease_deadline=NOW + timedelta(minutes=1),
                    agent_certificate_serial=f"serial-{index}",
                    state="succeeded",
                    result=install_job.result["node_evidence"][operation.node_id],
                )
            )
            stored_plan["compiled_execution_plans"][operation.node_id] = compiled
            node = session.get(AgentNode, operation.node_id)
            assert node is not None
        plan_digest = installation_plan_digest_from_stored_document(stored_plan)
        stored_plan["plan_digest"] = plan_digest
        installation.plan_digest = plan_digest
        job_payload = json.loads(json.dumps(install_job.payload))
        job_payload["plan_digest"] = plan_digest
        install_job.payload = job_payload
        install_job.payload_digest = hashlib.sha256(
            canonical_message(job_payload)
        ).hexdigest()
        for operation in operations:
            payload = json.loads(json.dumps(operation.payload))
            payload["plan_digest"] = plan_digest
            operation.payload = payload
            operation.payload_digest = hashlib.sha256(
                canonical_message(payload)
            ).hexdigest()
        installation.plan = stored_plan


def test_reconciliation_routes_are_typed_and_discoverable() -> None:
    app = FastAPI()
    install_installation_reconciliation_routes(
        app,
        actor_dependency=Depends(_actor),
        operations=None,
    )

    schema = app.openapi()
    paths = schema["paths"]
    preview = paths["/api/recipe/installations/{installation_id}/reconcile/preview"][
        "post"
    ]
    assert "requestBody" not in preview
    assert preview["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("RunSwitchPlan")
    apply = paths["/api/recipe/installations/{installation_id}/reconcile"]["post"]
    assert apply["requestBody"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("InstallationReconcileRequest")
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


def test_preview_delegates_reconcile_mode_to_existing_run_switch_owner() -> None:
    operations = Mock()
    operations.preview_cleanup.side_effect = RunSwitchOperationConflict(
        "run-switch.reconciliation-unavailable"
    )
    client = _client(operations)

    response = client.post(
        f"/api/recipe/installations/{INSTALLATION_ID}/reconcile/preview"
    )

    assert response.status_code == 409
    request = operations.preview_cleanup.call_args.args[0]
    assert request.installation_id == INSTALLATION_ID
    assert request.cleanup_mode == "reconcile"
    assert operations.preview_cleanup.call_args.kwargs == {"actor": "test-actor"}


@pytest.mark.parametrize("role", ("viewer", "operator"))
def test_reconciliation_preview_refuses_non_administrators_before_planning(role):
    # Break caught: the read-only preview bypasses its administrator role
    # contract, even though apply enforces that contract.
    operations = Mock()
    response = _client(operations, role=role).post(
        f"/api/recipe/installations/{INSTALLATION_ID}/reconcile/preview"
    )
    assert response.status_code == 403
    operations.preview_cleanup.assert_not_called()


def test_apply_is_administrator_only_and_preserves_request_identity() -> None:
    operations = Mock()
    operations.apply_cleanup.side_effect = RunSwitchOperationConflict(
        "run-switch.reconciliation-stale-plan"
    )
    client = _client(operations, role="operator")
    path = f"/api/recipe/installations/{INSTALLATION_ID}/reconcile"
    body = {"request_key": REQUEST_KEY}

    forbidden = client.post(path, json=body)
    assert forbidden.status_code == 403
    operations.apply_cleanup.assert_not_called()

    admin_client = _client(operations)
    rejected = admin_client.post(path, json=body)

    assert rejected.status_code == 409
    request = operations.apply_cleanup.call_args.args[0]
    assert request.installation_id == INSTALLATION_ID
    assert request.cleanup_mode == "reconcile"
    assert request.request_key == REQUEST_KEY


def test_run_switch_status_is_a_typed_exact_id_lookup() -> None:
    operations = Mock()
    operations.get.side_effect = KeyError(OPERATION_ID)
    client = _client(operations)

    response = client.get(f"/api/run-switch/operations/{OPERATION_ID}")

    assert response.status_code == 404
    operations.get.assert_called_once_with(OPERATION_ID)


@pytest.mark.usefixtures("damaged_json_rows")
def test_request_lookup_api_uses_real_run_switch_provider_and_lifecycle_child(
    tmp_path,
    monkeypatch,
) -> None:
    sessions, lifecycle, queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    _prepare_exact_legacy_install_for_reconciliation(
        sessions, installation.owner_id, nodes
    )
    service = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
    request_key = str(uuid.uuid4())
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

    preview_response = client.post(
        f"/api/recipe/installations/{installation.owner_id}/reconcile/preview",
        headers=headers,
    )
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["allowed"] is True, [
        (item["code"], item["detail"]) for item in preview["blockers"]
    ]
    plan_digest = preview["plan_digest"]

    apply_path = f"/api/recipe/installations/{installation.owner_id}/reconcile"
    apply_body = {"request_key": request_key, "plan_digest": plan_digest}
    # A stale preview is refused before an operation or child effect exists.
    stale = client.post(
        apply_path, json={**apply_body, "plan_digest": "0" * 64}, headers=headers
    )
    assert stale.status_code == 409 and "stale_plan" in stale.text
    accepted_response = client.post(apply_path, json=apply_body, headers=headers)
    assert accepted_response.status_code == 202, accepted_response.text
    accepted = accepted_response.json()
    # A lost apply response is recovered through the same durable request key.
    replay = client.post(apply_path, json=apply_body, headers=headers)
    assert replay.status_code == 202, replay.text
    assert replay.json()["operation_id"] == accepted["operation_id"]
    assert replay.json()["request_key"] == request_key
    changed = client.post(
        apply_path, json={**apply_body, "plan_digest": "0" * 64}, headers=headers
    )
    assert (
        changed.status_code == 409 and "request_key_reused_differently" in changed.text
    )

    operation_id = accepted["operation_id"]
    assert service.tick() is True
    started = service.get(operation_id)
    child_id = started.result.child_operation_id if started.result is not None else None
    assert child_id is not None

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
    assert parent["id"] == operation_id
    assert parent["owner"] == {
        "kind": "job",
        "id": operation_id,
        "request_id": request_key,
    }

    with sessions() as session:
        child_job = session.get(Job, child_id)
        child_operations = list(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == child_id)
            )
        )
    assert child_job is not None and child_job.kind == "recipe.reconcile"
    assert child_operations, "cleanup must dispatch lifecycle child operations"
    assert {item.node_id for item in child_operations} == set(nodes)
    for child in child_operations:
        payload = RecipeReconcilePayload.model_validate(child.payload)
        assert payload.installation_id == installation.owner_id
        assert (
            payload.plan_digest
            == preview["reconciliation_authority"]["original_plan_digest"]
        )

    status = client.get(f"/api/run-switch/operations/{operation_id}", headers=headers)
    assert status.status_code == 200, status.text
    operation = status.json()
    assert operation["operation_id"] == operation_id
    assert operation["request_key"] == request_key
    assert operation["plan_digest"] == plan_digest
    assert operation["cleanup_mode"] == "reconcile"
    assert operation["installation_id"] == installation.owner_id
    assert operation["result"]["child_operation_id"] == child_id

    monkeypatch.setattr(
        service,
        "get",
        Mock(side_effect=RuntimeError("private stored-operation detail")),
    )
    unavailable = client.get(
        f"/api/run-switch/operations/{operation_id}", headers=headers
    )
    assert unavailable.status_code == 500
    assert "private stored-operation detail" not in unavailable.text
    assert (
        unavailable.json()["context"]["request_id"]
        == unavailable.headers["x-request-id"]
    )
