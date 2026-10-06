"""Artifact-job bookkeeping reconciles; the submit and ingress refusals still refuse."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select
from vonk_control.artifact_jobs import ArtifactJobError
from vonk_control.models import ArtifactJob, ArtifactJobFile, RecipeRun

from .test_artifact_jobs import (
    artifact_create_request,
    running_artifact_service,
    submitted_artifact_job,
)


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_run_that_cannot_accept_jobs_still_refuses_the_submit(tmp_path) -> None:
    sessions, _operations, _queue, service, run_id, _node_id = running_artifact_service(
        tmp_path
    )
    created = service.create(
        **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000320")
    )
    with sessions.begin() as session:
        run = session.scalar(select(RecipeRun).where(RecipeRun.id == run_id))
        assert run is not None
        run.plan = {"not": "a run plan"}

    with pytest.raises(ArtifactJobError, match="not ready|not accepting|plan"):
        service.submit(created.id, actor="operator", request_id="submit-320")


def test_a_replayed_finalize_of_a_moved_on_job_still_refuses(tmp_path) -> None:
    _sessions, _operations, _queue, service, run_id, _node_id = (
        running_artifact_service(tmp_path)
    )
    submitted = submitted_artifact_job(service, run_id, request_suffix=330)

    with pytest.raises(ArtifactJobError):
        service.finalize(submitted.id)


def test_a_result_for_an_order_without_an_artifact_job_is_recorded_not_raised(
    tmp_path,
) -> None:
    sessions, _operations, _queue, service, run_id, _node_id = running_artifact_service(
        tmp_path
    )
    submitted = submitted_artifact_job(service, run_id, request_suffix=340)
    with sessions.begin() as session:
        session.execute(
            delete(ArtifactJobFile).where(
                ArtifactJobFile.artifact_job_id == submitted.id
            )
        )
        session.execute(delete(ArtifactJob).where(ArtifactJob.id == submitted.id))

    with sessions.begin() as session:
        service.consume_agent_result(
            session,
            SimpleNamespace(parent_job_id=submitted.operation_id),  # type: ignore[arg-type]
            None,
            SimpleNamespace(state="succeeded", result=None),
        )


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_damaged_run_plan_still_finds_the_endpoint_owner_from_the_mapping(
    tmp_path,
) -> None:
    sessions, _operations, _queue, service, run_id, _node_id = running_artifact_service(
        tmp_path
    )
    with sessions.begin() as session:
        run = session.scalar(select(RecipeRun).where(RecipeRun.id == run_id))
        assert run is not None
        expected = service._job_node_in_session(session, run).node_id
        run.plan = {"not": "a run plan"}
        session.flush()
        rebuilt = service._job_node_in_session(session, run)

    assert rebuilt.node_id == expected
