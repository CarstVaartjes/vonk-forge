import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select
from vonk_agent_protocol.runtime_preflight import RuntimePreflightRequest
from vonk_control.lifecycle_preflight import LifecyclePreflight
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    CatalogDocumentRevision,
    Job,
)
from vonk_control.runtime_preflight import mandatory_capabilities, request_digest

from .test_recipe_operations import NOW, setup_services


def _clear(sessions):
    with sessions.begin() as session:
        operations = list(
            session.scalars(
                select(AgentOperation).where(
                    AgentOperation.kind == "runtime.preflight.v1"
                )
            )
        )
        for operation in operations:
            session.execute(
                delete(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == operation.id
                )
            )
            session.delete(operation)
            session.delete(session.get(Job, operation.parent_job_id))


def _finish(sessions, checkpoint, now, *, failed=None, fingerprint="a" * 64):
    with sessions.begin() as session:
        job = session.get(Job, checkpoint.pending_job_id)
        operation = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
        )
        request = RuntimePreflightRequest.model_validate(operation.payload)
        job.state = operation.state = "succeeded"
        operation.current_attempt = 1
        operation.updated_at = now
        session.add(
            AgentOperationAttempt(
                operation_id=operation.id,
                attempt=1,
                fence=str(uuid.uuid4()),
                lease_deadline=now + timedelta(seconds=60),
                agent_certificate_serial="serial-0",
                state="succeeded",
                result={
                    "schema_version": 1,
                    "fingerprint": fingerprint,
                    "request_sha256": request_digest(request),
                    "observed_at": int(now.timestamp()),
                    "duration_ms": 1,
                    "cached": False,
                    "findings": [
                        {
                            "capability": capability,
                            "status": "failed" if capability == failed else "passed",
                            "code": "probe_failed"
                            if capability == failed
                            else "available",
                        }
                        for capability in mandatory_capabilities(request)
                    ],
                },
            )
        )


def _setup(tmp_path):
    sessions, _lifecycle, queue, _, _, nodes = setup_services(tmp_path)
    _clear(sessions)
    clock = SimpleNamespace(now=NOW)
    with sessions() as session:
        document = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe"
            )
        ).document
    arguments = {
        "document": document,
        "nodes": {nodes[0]: False},
        "phase_index": 0,
        "request_key": str(uuid.uuid4()),
        "actor": "test",
    }
    service = LifecyclePreflight(sessions, queue, lambda: clock.now, 10)
    return sessions, queue, clock, nodes[0], service, arguments


def test_probe_restart_and_duplicate_dispatch_converge_without_advancing_work(tmp_path):
    sessions, queue, clock, node_id, service, arguments = _setup(tmp_path)
    first, error = service.ensure(**arguments, previous=None)
    assert error is None and first.pending_job_id
    duplicate, error = service.ensure(**arguments, previous=None)
    assert duplicate.pending_job_id == first.pending_job_id
    restarted = LifecyclePreflight(sessions, queue, lambda: clock.now, 10)
    pending, error = restarted.ensure(**arguments, previous=first)
    assert pending.pending_job_id == first.pending_job_id
    _finish(sessions, pending, clock.now)
    complete, error = restarted.ensure(**arguments, previous=pending)
    assert error is None and complete.pending_job_id is None
    assert complete.receipts[node_id].request_sha256
    with sessions() as session:
        assert len(list(session.scalars(select(AgentOperation)))) == 1


@pytest.mark.parametrize(
    "failed", ["signed_helper_run", "disk_reserve", "temporary_directory"]
)
def test_successful_probe_execution_with_failed_findings_blocks_expensive_work(
    tmp_path, failed
):
    sessions, _, clock, _, service, arguments = _setup(tmp_path)
    pending, _ = service.ensure(**arguments, previous=None)
    _finish(sessions, pending, clock.now, failed=failed)
    completed, error = service.ensure(**arguments, previous=pending)
    assert completed.pending_job_id is None
    assert failed in error


@pytest.mark.parametrize("change", ["fingerprint", "age", "request"])
def test_only_preflight_checkpoint_refreshes_when_dependent_identity_changes(
    tmp_path, change
):
    sessions, _, clock, node_id, service, arguments = _setup(tmp_path)
    pending, _ = service.ensure(**arguments, previous=None)
    _finish(sessions, pending, clock.now)
    completed, error = service.ensure(**arguments, previous=pending)
    assert error is None
    if change == "fingerprint":
        with sessions.begin() as session:
            node = session.get(AgentNode, node_id)
            node.capabilities = [
                v
                for v in node.capabilities
                if not v.startswith("runtime.preflight.fingerprint.")
            ] + ["runtime.preflight.fingerprint." + "b" * 64]
    elif change == "age":
        clock.now += timedelta(seconds=301)
    else:
        arguments["nodes"] = {node_id: True}
    refreshed, error = service.ensure(**arguments, previous=completed)
    assert error is None and refreshed.pending_job_id != pending.pending_job_id
    assert refreshed.attempts[node_id] == 2
    assert refreshed.receipts == completed.receipts


def test_repeated_host_changes_exhaust_bounded_probe_attempts(tmp_path):
    sessions, _, clock, _, service, arguments = _setup(tmp_path)
    checkpoint = None
    for _ in range(3):
        checkpoint, error = service.ensure(**arguments, previous=checkpoint)
        assert error is None and checkpoint.pending_job_id
        _finish(sessions, checkpoint, clock.now, fingerprint="b" * 64)
    checkpoint, error = service.ensure(**arguments, previous=checkpoint)
    assert error == "runtime_preflight.retry_exhausted"
    assert len(checkpoint.receipts) == 1


def test_high_level_gate_finishes_probe_before_dispatching_expensive_transfer(tmp_path):
    from vonk_control.run_switch_contract import RunSwitchApplyRequest

    from .test_recipe_operations import installed_recipe
    from .test_run_switch_operations import (
        CompleteArtifactInspector,
        RecordingArtifactExecutor,
        _request,
        _service,
    )

    sessions, lifecycle, _queue, mapping, build, nodes = setup_services(tmp_path)
    installed_recipe(lifecycle, mapping, build, nodes, request_id=str(uuid.uuid4()))
    _clear(sessions)
    artifacts = RecordingArtifactExecutor(child_transfer=True)
    service = _service(sessions, NOW, lifecycle, artifacts, artifacts=CompleteArtifactInspector(missing_spark_bytes=1024))
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    operation = service.apply(RunSwitchApplyRequest(**request.model_dump(), plan_digest=plan.plan_digest, request_key=str(uuid.uuid4())), actor="admin")
    service.tick()
    pending = service.get(operation.operation_id)
    assert pending.result.child_operation_id is None
    assert pending.result.preflight.pending_job_id
    assert not artifacts.children
    assert pending.progress.operation.phase == "runtime-preflight"
    _finish(sessions, pending.result.preflight, NOW)
    restarted = _service(sessions, NOW, lifecycle, artifacts, artifacts=CompleteArtifactInspector(missing_spark_bytes=1024))
    restarted.tick()
    active = restarted.get(operation.operation_id)
    assert active.progress.phase_index == 0
    assert active.result.child_operation_id
    assert active.result.preflight.pending_job_id is None
    assert len(artifacts.children) == 1
