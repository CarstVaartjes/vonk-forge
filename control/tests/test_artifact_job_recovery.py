"""Artifact requests recover exact projections and fence unknown old effects."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from vonk_agent_protocol import (
    AgentResult,
    AgentResultState,
    RecipeJobFile,
    RecipeJobRunResult,
    RecipeStopResult,
    RunState,
    recipe_job_manifest_document,
    recipe_job_manifest_sha256,
)
from vonk_control import artifact_job_states as ajs
from vonk_control.agent_jobs import AgentJobService
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.models import (
    AgentOperation,
    ArtifactJob,
    ArtifactJobFile,
    Job,
    RecipeRun,
)

from .runtime_identity_support import claim_agent
from .test_artifact_job_lifecycle import _issued_job
from .test_artifact_jobs import (
    artifact_create_request,
    create_artifact_job,
    running_artifact_service,
    submitted_artifact_job,
)
from .test_recipe_operations import _issue_exact_stop_grant


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize("damaged", ["manifest", "digest", "total"])
def test_replay_repairs_the_original_binding_before_comparing_input(tmp_path, damaged):
    """Catches comparing damaged projections with the caller before recovery."""
    sessions, _ops, _queue, service, run_id, _node = running_artifact_service(tmp_path)
    request = artifact_create_request(run_id, "00000000-0000-4000-8000-000000001000")
    original = create_artifact_job(service, **request)
    with sessions.begin() as session:
        row = session.get(ArtifactJob, original.id)
        assert row is not None
        if damaged == "manifest":
            row.input_manifest = {"damaged": True}
        elif damaged == "digest":
            row.input_manifest_sha256 = "f" * 64
        else:
            row.input_total_bytes += 1
    replay = create_artifact_job(service, **request)
    assert replay.id == original.id
    assert replay.input_declarations == original.input_declarations
    assert replay.input_manifest_sha256 == original.input_manifest_sha256
    assert replay.input_total_bytes == original.input_total_bytes
    with sessions() as session:
        row = session.get(ArtifactJob, original.id)
        assert row is not None
        assert row.input_manifest_sha256 == original.input_manifest_sha256
        assert row.input_total_bytes == original.input_total_bytes
    fresh = submitted_artifact_job(service, run_id, request_suffix=1002)
    assert fresh.operation_id is not None


@pytest.mark.usefixtures("damaged_json_rows")
def test_draft_limit_damage_ends_replay_and_admits_fresh_intent(tmp_path):
    """Catches classifying lost local limits as a reused caller key conflict."""
    sessions, _ops, _queue, service, run_id, _node = running_artifact_service(tmp_path)
    request = artifact_create_request(run_id, "00000000-0000-4000-8000-000000001006")
    original = create_artifact_job(service, **request)
    with sessions.begin() as session:
        row = session.get(ArtifactJob, original.id)
        assert row is not None
        row.output_limits = {"damaged": True}
    observed = service.get(original.id)
    assert observed.output_limits is None
    ended = create_artifact_job(service, **request)
    assert ended.id == original.id and ended.state in ajs.ENDED
    assert ended.operation_id is None
    assert (
        service.cancel(
            original.id,
            actor="operator",
            request_id="00000000-0000-4000-8000-000000001007",
            reason="stop",
        ).id
        == original.id
    )
    fresh = submitted_artifact_job(service, run_id, request_suffix=1008)
    assert fresh.operation_id is not None


def test_missing_input_bytes_end_without_dispatch_and_fresh_upload_recovers(tmp_path):
    """Catches READY bookkeeping dispatching a job whose bytes disappeared."""
    sessions, _ops, _queue, service, run_id, _node = running_artifact_service(tmp_path)
    request = artifact_create_request(run_id, "00000000-0000-4000-8000-000000001010")
    original = create_artifact_job(service, **request)
    content = b"png"
    digest = hashlib.sha256(content).hexdigest()
    service.put_input(
        original.id,
        name="input.png",
        media_type="image/png",
        expected_sha256=digest,
        content=content,
    )
    service.finalize(original.id)
    path = service._blob_store.resolve(f"{digest[:2]}/{digest}", digest, len(content))
    assert path is not None
    path.unlink()
    ended = service.submit(
        original.id, actor="operator", request_id="00000000-0000-4000-8000-000000001011"
    )
    assert ended.state in ajs.ENDED and ended.operation_id is None
    with sessions() as session:
        assert not tuple(
            session.scalars(
                select(AgentOperation)
                .join(Job, Job.id == AgentOperation.parent_job_id)
                .where(Job.request_id == "00000000-0000-4000-8000-000000001011")
            )
        )
    fresh = submitted_artifact_job(service, run_id, request_suffix=1012)
    assert fresh.operation_id is not None
    restored = service._blob_store.resolve(
        f"{digest[:2]}/{digest}", digest, len(content)
    )
    assert restored is not None and restored.read_bytes() == content


@pytest.mark.usefixtures("damaged_json_rows")
def test_submitted_limits_are_recovered_from_the_exact_accepted_request(tmp_path):
    """Catches trusting a damaged SQL limits projection over accepted authority."""
    sessions, _ops, _queue, service, run_id, node = running_artifact_service(tmp_path)
    original = submitted_artifact_job(service, run_id, request_suffix=1020)
    with sessions.begin() as session:
        row = session.get(ArtifactJob, original.id)
        assert row is not None
        row.output_limits = {"damaged": True}
    assert service.get(original.id).output_limits == original.output_limits
    content = b"png"
    digest = hashlib.sha256(content).hexdigest()
    service.put_output(
        original.id,
        node_id=node,
        name="output.png",
        media_type="image/png",
        expected_sha256=digest,
        content=content,
    )
    assert original.output_limits is not None
    with sessions() as session:
        row = session.get(ArtifactJob, original.id)
        assert (
            row is not None and row.output_limits == original.output_limits.model_dump()
        )
    # A newer request cancels the still-unissued order and obtains its own fence.
    fresh = submitted_artifact_job(service, run_id, request_suffix=1022)
    assert (
        fresh.operation_id is not None and fresh.operation_id != original.operation_id
    )
    assert service.get(original.id).state in ajs.ENDED


def recover_unknown_job_under_new_intent(tmp_path, *, malformed: bool) -> None:
    """Exercise consumer/store/restart/Stop authority and fresh physical admission."""
    sessions, operations, service, queue, clock, original, claim, run_id = _issued_job(
        tmp_path, 1030
    )
    service._clock = clock
    operations._clock = clock
    with sessions() as session:
        operation = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == original.operation_id
            )
        )
        assert operation is not None
        node_id = operation.node_id
    content = b"verified output"
    digest = hashlib.sha256(content).hexdigest()
    service.put_output(
        original.id,
        node_id=node_id,
        name="output.png",
        media_type="image/png",
        expected_sha256=digest,
        content=content,
    )
    if malformed:
        with sessions.begin() as session:
            operation = session.scalar(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == original.operation_id
                )
            )
            assert operation is not None
            service.consume_agent_result(
                session,
                operation,
                None,
                SimpleNamespace(
                    state=AgentResultState.SUCCEEDED, result={"unreadable": True}
                ),
            )
        assert service.get(original.id).state not in ajs.ENDED
        with sessions() as session:
            operation = session.scalar(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == original.operation_id
                )
            )
            assert operation is not None and operation.current_attempt == 1
    else:
        clock.advance(seconds=31)
        queue.reconcile_orders()
    # Restart and expiry use the original submitted timestamp, never a new clock.
    restarted = AgentJobService(sessions, clock=clock)

    def consume(session, operation, attempt, message):
        service.consume_agent_result(session, operation, attempt, message)
        operations.consume_agent_result(session, operation, attempt, message)

    restarted.set_result_consumer(consume)
    operations._agent_jobs = restarted
    clock.advance(seconds=3601)
    restarted.reconcile_orders()
    ended = service.get(original.id)
    assert ended.state in ajs.ENDED and ended.supported_actions == ()
    assert (
        ended.result_evidence is not None
        and ended.result_evidence.active_scope_may_remain
    )
    restored = service._blob_store.resolve(
        f"{digest[:2]}/{digest}", digest, len(content)
    )
    assert restored is not None and restored.read_bytes() == content
    next_attempt = submitted_artifact_job(service, run_id, request_suffix=1040)
    assert next_attempt.state in ajs.ENDED and next_attempt.operation_id is None
    exact, payload, _grant = _issue_exact_stop_grant(
        sessions,
        node_id=node_id,
        certificate_serial="serial-0",
        grant_now=clock(),
    )
    assert exact.fence != claim.fence and payload.target_runtime_id == original.id
    restarted.record_result(
        AgentResult(
            fence=exact.fence,
            state=AgentResultState.SUCCEEDED,
            result=RecipeStopResult(),
        )
    )
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.state == RunState.STOPPED
        installation_id = run.installation_id
    InventoryRepository(sessions, clock=clock).record(
        InventorySnapshotInput(
            node_id,
            clock(),
            10_000,
            8_000,
            10_000,
            8_000,
            10_000,
            8_000,
            1,
            False,
            ("runtime.vonk.v1", "recipe.image.pull.v1", "recipe.operations.v1"),
            memory_pool="shared",
        )
    )
    plan = operations.preview_run(installation_id, "image-job")
    assert plan.allowed
    fresh_run = operations.activate_job_run(
        plan,
        plan_digest=plan.plan_digest,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000001050",
    )
    fresh = submitted_artifact_job(service, fresh_run.owner_id, request_suffix=1052)
    fresh_claim = claim_agent(restarted, node_id, "serial-0")
    assert fresh.operation_id is not None and fresh_claim is not None
    assert fresh_claim.fence not in {claim.fence, exact.fence}


def test_unreadable_peer_result_observes_then_ends_and_new_intent_reconciles(tmp_path):
    """Catches treating an unreadable reply as a definite failure or replaying it."""
    recover_unknown_job_under_new_intent(tmp_path, malformed=True)


def test_lapsed_job_ends_and_new_intent_drives_exact_stop_without_manual_cancel(
    tmp_path,
):
    """Catches an old uncertain result permanently vetoing every newer submit."""
    recover_unknown_job_under_new_intent(tmp_path, malformed=False)


def test_lost_order_projection_ends_at_original_deadline_and_fresh_draft_is_admitted(
    tmp_path,
):
    """Catches a missing order becoming an endless queue head or free capacity."""
    sessions, _ops, service, agents, clock, original, _claim, run_id = _issued_job(
        tmp_path, 1050
    )
    with sessions.begin() as session:
        operation = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == original.operation_id
            )
        )
        assert operation is not None
        session.delete(operation)
    agents.reconcile_orders()
    observed = service.get(original.id)
    assert observed.state == ajs.OBSERVING
    assert observed.result_evidence is not None
    assert observed.result_evidence.active_scope_may_remain
    clock.advance(seconds=3601)
    agents.reconcile_orders()
    ended = service.get(original.id)
    assert ended.state in ajs.ENDED
    assert ended.result_evidence is not None
    assert ended.result_evidence.active_scope_may_remain
    fresh = create_artifact_job(
        service,
        **artifact_create_request(run_id, "00000000-0000-4000-8000-000000001052"),
    )
    assert fresh.id != original.id and fresh.operation_id is None


def test_terminal_receipt_repairs_result_projection_without_reexecuting(tmp_path):
    """Catches treating lost or contradictory output metadata as a read refusal."""
    sessions, _ops, service, agents, _clock, original, claim, run_id = _issued_job(
        tmp_path, 1060
    )
    with sessions() as session:
        operation = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == original.operation_id
            )
        )
        assert operation is not None
        node_id = operation.node_id
    content = b"{}"
    digest = hashlib.sha256(content).hexdigest()
    outputs = tuple(
        RecipeJobFile(
            name=name, media_type=media_type, size_bytes=len(content), sha256=digest
        )
        for name, media_type in (
            ("metadata.json", "application/json"),
            ("output.png", "image/png"),
        )
    )
    for output in outputs:
        service.put_output(
            original.id,
            node_id=node_id,
            name=output.name,
            media_type=output.media_type,
            expected_sha256=digest,
            content=content,
        )
    agents.record_result(
        AgentResult(
            fence=claim.fence,
            state=AgentResultState.SUCCEEDED,
            result=RecipeJobRunResult.model_validate(
                {
                    "job_id": original.id,
                    "run_id": run_id,
                    "exit_code": 0,
                    "output_manifest": {
                        **recipe_job_manifest_document(outputs),
                        "manifest_sha256": recipe_job_manifest_sha256(outputs),
                    },
                    "evidence": {"elapsed_milliseconds": 10, "peak_memory_bytes": None},
                }
            ),
        )
    )
    with sessions.begin() as session:
        row = session.get(ArtifactJob, original.id)
        assert row is not None
        row.output_manifest_sha256 = None
        files = tuple(
            session.scalars(
                select(ArtifactJobFile).where(
                    ArtifactJobFile.artifact_job_id == original.id,
                    ArtifactJobFile.direction == "output",
                )
            )
        )
        for file in files:
            file.size_bytes += 1
    repaired = service.get(original.id)
    assert repaired.output_manifest_sha256 == recipe_job_manifest_sha256(outputs)
    assert tuple(file.size_bytes for file in repaired.output_files) == (
        len(content),
        len(content),
    )
    path, media_type, name, size = service.result_blob(
        original.id, "output.png", digest
    )
    assert (path.read_bytes(), media_type, name, size) == (
        content,
        "image/png",
        "output.png",
        len(content),
    )
    with sessions() as session:
        operation = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == original.operation_id
            )
        )
        assert operation is not None and operation.current_attempt == 1
    fresh = submitted_artifact_job(service, run_id, request_suffix=1062)
    assert (
        fresh.operation_id is not None and fresh.operation_id != original.operation_id
    )
