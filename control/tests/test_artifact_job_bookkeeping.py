"""Artifact-job bookkeeping reconciles; the submit and ingress refusals still refuse."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select
from vonk_agent_protocol import AgentResultState
from vonk_control import artifact_job_states
from vonk_control.models import ArtifactJob, ArtifactJobFile, RecipeRun

from .test_artifact_jobs import (
    artifact_create_request,
    create_artifact_job,
    running_artifact_service,
    submitted_artifact_job,
)


@pytest.mark.usefixtures("damaged_json_rows")
def test_damaged_preparation_ends_and_fresh_submission_is_admitted(tmp_path) -> None:
    sessions, _operations, _queue, service, run_id, _node_id = running_artifact_service(
        tmp_path
    )
    created = create_artifact_job(
        service,
        **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000320"),
    )
    with sessions.begin() as session:
        run = session.scalar(select(RecipeRun).where(RecipeRun.id == run_id))
        assert run is not None
        run.plan = {"not": "a run plan"}

    ended = service.submit(
        created.id, actor="operator", request_id="00000000-0000-4000-8000-000000000321"
    )
    assert ended.state in artifact_job_states.ENDED
    assert ended.operation_id is None
    fresh = submitted_artifact_job(service, run_id, request_suffix=322)
    assert fresh.operation_id is not None


def test_replayed_finalize_reconnects_to_the_submitted_job(tmp_path) -> None:
    _sessions, _operations, _queue, service, run_id, _node_id = (
        running_artifact_service(tmp_path)
    )
    submitted = submitted_artifact_job(service, run_id, request_suffix=330)

    assert service.finalize(submitted.id).operation_id == submitted.operation_id


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


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize(
    "rebuildable,lost_output", [(True, False), (False, False), (True, True)]
)
def test_damaged_contract_rebuilds_or_ends_without_holding_fresh_request(
    tmp_path, rebuildable, lost_output
):
    import hashlib

    from vonk_agent_protocol import RecipeJobFile, recipe_job_manifest_sha256
    from vonk_agent_protocol.recipe_jobs import (
        RecipeJobEvidence,
        RecipeJobOutputManifest,
        RecipeJobRunResult,
    )
    from vonk_control.models import AgentOperation

    sessions, _operations, _queue, service, run_id, node_id = running_artifact_service(
        tmp_path
    )
    submitted = submitted_artifact_job(service, run_id, request_suffix=350)
    with sessions.begin() as session:
        row = session.get(ArtifactJob, submitted.id)
        assert row is not None
        row.compiled_contract = {"damaged": True}
    content = b"png"
    digest = hashlib.sha256(content).hexdigest()
    service.put_output(
        submitted.id,
        node_id=node_id,
        name="output.png",
        media_type="image/png",
        expected_sha256=digest,
        content=content,
    )
    outputs = (
        RecipeJobFile(
            name="output.png", media_type="image/png", size_bytes=3, sha256=digest
        ),
    )
    if not rebuildable:
        with sessions.begin() as session:
            row = session.get(ArtifactJob, submitted.id)
            assert row is not None
            row.contract_sha256 = "f" * 64
    if lost_output:
        with sessions.begin() as session:
            session.execute(
                delete(ArtifactJobFile).where(
                    ArtifactJobFile.artifact_job_id == submitted.id,
                    ArtifactJobFile.direction == "output",
                )
            )
    result = RecipeJobRunResult(
        job_id=submitted.id,
        run_id=run_id,
        exit_code=0,
        output_manifest=RecipeJobOutputManifest(
            schema_version=1,
            files=outputs,
            total_bytes=3,
            manifest_sha256=recipe_job_manifest_sha256(outputs),
        ),
        evidence=RecipeJobEvidence(elapsed_milliseconds=1, peak_memory_bytes=None),
    )
    with sessions.begin() as session:
        operation = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == submitted.operation_id
            )
        )
        assert operation is not None
        service.consume_agent_result(
            session,
            operation,
            None,
            SimpleNamespace(
                state=AgentResultState.SUCCEEDED, result=result.to_mapping()
            ),
        )
    if rebuildable and not lost_output:
        assert (
            service.result_blob(submitted.id, "output.png", digest)[0].read_bytes()
            == content
        )
    else:
        ended = service.get(submitted.id)
        assert ended.state in artifact_job_states.ENDED
        assert ended.output_manifest_sha256 is None
        assert ended.result_evidence is not None
        assert ended.result_evidence.elapsed_milliseconds == 1
    fresh = submitted_artifact_job(service, run_id, request_suffix=360)
    assert fresh.operation_id is not None


@pytest.mark.usefixtures("damaged_json_rows")
def test_missing_manifest_ends_finalize_without_holding_fresh_request(tmp_path):
    sessions, _operations, _queue, service, run_id, _node_id = running_artifact_service(
        tmp_path
    )
    created = create_artifact_job(
        service,
        **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000370"),
    )
    with sessions.begin() as session:
        row = session.get(ArtifactJob, created.id)
        assert row is not None
        row.input_manifest = {"damaged": True}
    ended = service.finalize(created.id)
    assert ended.state in artifact_job_states.ENDED
    assert ended.operation_id is None
    fresh = submitted_artifact_job(service, run_id, request_suffix=380)
    assert fresh.operation_id is not None


@pytest.mark.usefixtures("damaged_json_rows")
def test_replayed_create_reuses_content_after_compiled_metadata_damage(tmp_path):
    sessions, _operations, _queue, service, run_id, _node_id = running_artifact_service(
        tmp_path
    )
    request = artifact_create_request(run_id, "00000000-0000-4000-8000-000000000400")
    created = create_artifact_job(service, **request)
    with sessions.begin() as session:
        row = session.get(ArtifactJob, created.id)
        assert row is not None
        row.compiled_contract = {"damaged": True}
    replayed = create_artifact_job(service, **request)
    assert replayed.id == created.id
    assert replayed.compiled_contract == created.compiled_contract
    fresh = create_artifact_job(
        service,
        **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000401"),
    )
    assert fresh.id != created.id
