from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec

from .test_agent_api import Jobs
from .test_artifact_job_installed_cli import _artifact_api
from .test_artifact_jobs import artifact_create_request
from .test_recipe_operations import NOW


def test_create_app_enforces_artifact_mutation_roles_before_owner_effect(
    postgres_engine, tmp_path
) -> None:
    _sessions, _fixture_api, service, run_id, _node_id = _artifact_api(
        tmp_path, postgres_engine
    )
    now = int(NOW.timestamp())
    tokens = TokenCodec(b"artifact-job-auth-boundary-key-0001")
    app = create_app(
        jobs=Jobs(),
        tokens=tokens,
        audits=MemoryAuditStore(),
        artifact_jobs=service,
        now=lambda: now,
    )
    client = TestClient(app)
    viewer = {
        "Authorization": "Bearer "
        + tokens.issue(Actor("read-only", "viewer"), ttl_seconds=600, now=now)
    }
    operator = {
        "Authorization": "Bearer "
        + tokens.issue(Actor("operator", "operator"), ttl_seconds=600, now=now)
    }

    create_request_id = "00000000-0000-4000-8000-000000000201"
    create = artifact_create_request(run_id, create_request_id)
    body = {
        key: value
        for key, value in create.items()
        if key not in {"actor", "request_id", "run_id"}
    }
    create_path = f"/api/recipe/runs/{run_id}/artifact-jobs"
    for authorization, status in (({}, 401), (viewer, 403)):
        refused = client.post(
            create_path,
            json=body,
            headers={**authorization, "X-Request-ID": create_request_id},
        )
        assert refused.status_code == status
        with pytest.raises(KeyError):
            service.get_by_request_id(create_request_id)

    created = client.post(
        create_path,
        json=body,
        headers={**operator, "X-Request-ID": create_request_id},
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    content = b"png"
    uploaded = client.put(
        f"/api/artifact-jobs/{job_id}/inputs/input.png",
        content=content,
        headers={
            **operator,
            "Content-Type": "image/png",
            "Content-Length": str(len(content)),
            "X-Content-SHA256": hashlib.sha256(content).hexdigest(),
        },
    )
    assert uploaded.status_code == 200, uploaded.text
    finalized = client.post(f"/api/artifact-jobs/{job_id}/finalize", headers=operator)
    assert finalized.status_code == 200, finalized.text
    assert service.get(job_id).state == "ready"

    submit_request_id = "00000000-0000-4000-8000-000000000202"
    submit_path = f"/api/artifact-jobs/{job_id}/submit"
    for authorization, status in (({}, 401), (viewer, 403)):
        refused = client.post(
            submit_path,
            headers={**authorization, "X-Request-ID": submit_request_id},
        )
        assert refused.status_code == status
        unchanged = service.get(job_id)
        assert unchanged.state == "ready" and unchanged.operation_id is None

    submitted = client.post(
        submit_path,
        headers={**operator, "X-Request-ID": submit_request_id},
    )
    assert submitted.status_code == 202, submitted.text
    assert submitted.json()["state"] == "queued"

    cancel_request_id = "00000000-0000-4000-8000-000000000203"
    cancel_draft_request_id = "00000000-0000-4000-8000-000000000204"
    cancel_draft = artifact_create_request(run_id, cancel_draft_request_id)
    cancel_body = {
        key: value
        for key, value in cancel_draft.items()
        if key not in {"actor", "request_id", "run_id"}
    }
    cancel_created = client.post(
        create_path,
        json=cancel_body,
        headers={**operator, "X-Request-ID": cancel_draft_request_id},
    )
    assert cancel_created.status_code == 201, cancel_created.text
    cancel_job_id = cancel_created.json()["id"]
    cancel_path = f"/api/artifact-jobs/{cancel_job_id}/cancel"
    for authorization, status in (({}, 401), (viewer, 403)):
        refused = client.post(
            cancel_path,
            json={"reason": "operator approved cancellation"},
            headers={**authorization, "X-Request-ID": cancel_request_id},
        )
        assert refused.status_code == status
        unchanged = service.get(cancel_job_id)
        assert unchanged.state == "draft" and unchanged.result_evidence is None

    cancelled = client.post(
        cancel_path,
        json={"reason": "operator approved cancellation"},
        headers={**operator, "X-Request-ID": cancel_request_id},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled"
    assert cancelled.json()["result_evidence"]["cancel_request_id"] == cancel_request_id
