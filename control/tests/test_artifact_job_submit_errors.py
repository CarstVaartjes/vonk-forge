"""Submit preserves domain categories across admission and the HTTP boundary."""

from http import HTTPStatus
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from vonk_agent_protocol import (
    ControllerErrorCode,
    InvalidRequestError,
    InvalidRequestReason,
    RunState,
    SecurityRefusalError,
    SecurityRefusalReason,
    UnknownOutcomeError,
    WaitReason,
)
from vonk_control.api import create_app
from vonk_control.auth import Actor, TokenCodec
from vonk_control.categorized_errors import InvalidType, SecurityRefused
from vonk_control.models import AgentNode, AgentOperation, ArtifactJob, RecipeRun
from vonk_control.operation_api import BoundedErrorResponse, RequestValidationProblem
from vonk_control.recipe_operations import RecipeRequestInvalid, RecipeRetryLater

from .test_api import Jobs
from .test_artifact_job_api import JOB_ID, REQUEST_ID, _TransferService
from .test_artifact_jobs import (
    artifact_create_request,
    create_artifact_job,
    running_artifact_service,
)
from .test_operator_acceptance_postgres import _api


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        *[
            (
                InvalidRequestError("invalid submission", reason=reason),
                HTTPStatus.NOT_FOUND
                if reason is InvalidRequestReason.NOT_FOUND
                else HTTPStatus.CONFLICT
                if reason
                in {
                    InvalidRequestReason.CONFLICT,
                    InvalidRequestReason.SUPERSEDED,
                    InvalidRequestReason.NOT_READY,
                    InvalidRequestReason.IMMUTABLE,
                    InvalidRequestReason.DUPLICATE,
                }
                else HTTPStatus.UNPROCESSABLE_ENTITY,
                reason,
            )
            for reason in InvalidRequestReason
        ],
        (
            InvalidRequestError("invalid submission"),
            422,
            InvalidRequestReason.MALFORMED,
        ),
        (
            InvalidType("invalid binding", reason=InvalidRequestReason.MALFORMED),
            422,
            InvalidRequestReason.MALFORMED,
        ),
        (
            RecipeRequestInvalid("superseded", reason=InvalidRequestReason.SUPERSEDED),
            409,
            InvalidRequestReason.SUPERSEDED,
        ),
        (
            SecurityRefusalError(
                "authority refused", reason=SecurityRefusalReason.FORBIDDEN
            ),
            403,
            SecurityRefusalReason.FORBIDDEN,
        ),
        (
            SecurityRefused(
                "authority refused", reason=SecurityRefusalReason.DIGEST_MISMATCH
            ),
            403,
            SecurityRefusalReason.DIGEST_MISMATCH,
        ),
        (
            UnknownOutcomeError(
                "observation unavailable", reason=WaitReason.OBSERVATION_UNAVAILABLE
            ),
            503,
            ControllerErrorCode.UNAVAILABLE,
        ),
        (RecipeRetryLater("admission busy"), 503, ControllerErrorCode.UNAVAILABLE),
    ],
)
def test_submit_maps_every_domain_category_without_builtin_inheritance(
    tmp_path, monkeypatch, error, status, code
):
    """Catches RuntimeError/category-only errors escaping as controller.internal_error."""
    service = _TransferService(tmp_path / "unused")

    def refuse(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(service, "submit", refuse)
    codec = TokenCodec(b"submit-error-category-test-key-32")
    app = create_app(jobs=Jobs(), tokens=codec, now=lambda: 1, artifact_jobs=service)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        f"/api/artifact-jobs/{JOB_ID}/submit",
        headers={
            "Authorization": f"Bearer {codec.issue(Actor('operator', 'operator'), ttl_seconds=100, now=0)}",
            "X-Request-ID": REQUEST_ID,
        },
    )
    assert response.status_code == status, response.text
    model = RequestValidationProblem if status == 422 else BoundedErrorResponse
    assert model.model_validate_json(response.content).detail
    assert response.headers["x-vonk-error-code"] == code
    if status == HTTPStatus.SERVICE_UNAVAILABLE:
        assert int(response.headers["retry-after"]) > 0
    else:
        assert "retry-after" not in response.headers


@pytest.mark.parametrize("superseded", [True, False])
def test_live_run_submit_admission_conflict_is_typed_and_has_no_effects(
    tmp_path, postgres_engine, superseded
):
    """Catches a live run's stale intent or reused request identity returning 500."""
    sessions, _, _, service, run_id, node_id = running_artifact_service(
        tmp_path, engine=postgres_engine
    )
    draft = create_artifact_job(
        service, **artifact_create_request(run_id, str(uuid4()))
    )
    assert draft.input_declarations is not None
    service.put_input(
        draft.id,
        name="input.png",
        media_type=draft.input_declarations[0].media_type,
        expected_sha256=draft.input_declarations[0].sha256,
        content=b"png",
    )
    service.finalize(draft.id)
    if superseded:
        with sessions.begin() as session:
            node = session.get(AgentNode, node_id)
            assert node is not None
            node.workload_intent_ordinal += 1
    api, headers = _api(sessions, artifact_jobs=service)
    # The activation's key names a different owner/kind, so it cannot be reused.
    headers["X-Request-ID"] = (
        str(uuid4()) if superseded else "00000000-0000-4000-8000-000000000102"
    )
    path = f"/api/artifact-jobs/{draft.id}/submit"
    with sessions() as session:
        before = session.scalar(select(func.count()).select_from(AgentOperation))
    response = api.post(path, headers=headers)
    assert response.status_code == HTTPStatus.CONFLICT, response.text
    assert response.headers["x-vonk-error-code"] == (
        InvalidRequestReason.SUPERSEDED if superseded else InvalidRequestReason.CONFLICT
    )
    assert "retry-after" not in response.headers
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        job = session.get(ArtifactJob, draft.id)
        assert run is not None and run.state == RunState.RUNNING
        assert job is not None and job.operation_id is None
        assert (
            session.scalar(select(func.count()).select_from(AgentOperation)) == before
        )
    if not superseded:
        headers["X-Request-ID"] = str(uuid4())
        accepted = api.post(path, headers=headers)
        assert accepted.status_code == HTTPStatus.ACCEPTED, accepted.text
        assert service.get(draft.id).operation_id is not None
