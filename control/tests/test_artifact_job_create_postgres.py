"""Artifact create validates the wire declaration before any durable effects."""

from http import HTTPStatus
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from vonk_control.artifact_job_api import ArtifactJobCreate
from vonk_control.models import ArtifactJob
from vonk_control.operation_api import RequestValidationProblem

from .test_artifact_jobs import artifact_create_request, running_artifact_service
from .test_operator_acceptance_postgres import _api, _receipt


@pytest.mark.parametrize(
    "fault", ["media-order", "duplicate-media", "output-bytes", "reserved-input"]
)
def test_create_rejects_invalid_wire_declaration_and_same_key_recovers(
    tmp_path, postgres_engine, fault
):
    """Catches the weaker HTTP DTO admitting a declaration that then raises a 500."""
    sessions, _operations, _queue, owner, run_id, _node = running_artifact_service(
        tmp_path, engine=postgres_engine
    )
    api, headers = _api(sessions, artifact_jobs=owner)
    key = str(uuid4())
    request = artifact_create_request(run_id, key)
    create = ArtifactJobCreate.model_validate(
        {
            name: value
            for name, value in request.items()
            if name not in {"run_id", "actor", "request_id"}
        }
    )
    damaged = create.model_dump(mode="json")
    if fault == "media-order":
        # MOSS declares replay before transcript: slot traversal yields this
        # order, while the wire contract requires a canonical media-type set.
        damaged["output_limits"]["allowed_media_types"] = [
            "video/mp4",
            "application/x-ndjson",
        ]
    elif fault == "duplicate-media":
        damaged["output_limits"]["allowed_media_types"] = ["image/png", "image/png"]
    elif fault == "output-bytes":
        damaged["output_limits"]["max_total_bytes"] = (
            damaged["output_limits"]["max_file_bytes"] - 1
        )
    else:
        damaged["inputs"][0]["name"] = "manifest.json"
    path = f"/api/recipe/runs/{run_id}/artifact-jobs"
    headers = {**headers, "X-Request-ID": key}
    rejected = api.post(path, json=damaged, headers=headers)
    assert rejected.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, rejected.text
    assert "x-vonk-receipt" not in rejected.headers
    problem = RequestValidationProblem.model_validate_json(rejected.content)
    field = "inputs" if fault == "reserved-input" else "output_limits"
    assert any(issue.loc[:2] == ["body", field] for issue in problem.issues)
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(ArtifactJob)) == 0
    # Correcting the request must work without resetting the run or changing
    # request identity; the rejected declaration has no accepted effects.
    accepted = api.post(path, json=create.model_dump(mode="json"), headers=headers)
    assert accepted.status_code == HTTPStatus.CREATED, accepted.text
    identity = _receipt(api, accepted, headers)["id"]
    replay = api.post(path, json=create.model_dump(mode="json"), headers=headers)
    assert _receipt(api, replay, headers)["id"] == identity
