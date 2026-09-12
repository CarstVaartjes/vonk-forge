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


def _setup(tmp_path, *, node_count=1):
    sessions, _lifecycle, queue, _, _, nodes = setup_services(tmp_path, nodes=node_count)
    _clear(sessions)
    clock = SimpleNamespace(now=NOW)
    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe"
            )
        )
        assert revision is not None
        document = revision.document
    arguments = {
        "document": document,
        "nodes": {nodes[0]: False},
        "phase_index": 0,
        "request_key": str(uuid.uuid4()),
        "actor": "test",
    }
    service = LifecyclePreflight(sessions, queue, lambda: clock.now, 10)
    return sessions, queue, clock, nodes[0], service, arguments


def test_dispatched_preflight_claim_can_receive_its_signed_helper_grant(tmp_path):
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from vonk_agent_protocol import (
        ContainerRuntimeAction,
        ExecuteContainerRuntimeRequestOperation,
        host_helper_grant_signing_bytes,
    )
    from vonk_control.agent_api import HostRuntimeGrantRequest
    from vonk_control.agent_jobs import _NEXT_CAPABILITIES, AgentJobService
    from vonk_control.host_helper_authority import (
        HostHelperGrantIssuer,
        HostRuntimeAuthorityService,
    )

    from .runtime_identity_support import PACKAGED_RUNTIME_IDENTITY, claim_agent

    sessions, _, clock, node_id, _, arguments = _setup(tmp_path)
    queue = AgentJobService(sessions, clock=lambda: clock.now)
    service = LifecyclePreflight(sessions, queue, lambda: clock.now, 10)
    pending, error = service.ensure(**arguments, previous=None)
    assert error is None
    claim = claim_agent(
        queue,
        node_id,
        "serial-0",
        60,
        capabilities=sorted(_NEXT_CAPABILITIES | {"runtime.preflight.v1"}),
        runtime_identity={**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"},
    )
    assert claim is not None and claim.job_id == pending.pending_job_id
    # Exercise the HTTP request contract against IDs from the real dispatcher.
    request = HostRuntimeGrantRequest.model_validate({
        "node_id": claim.node_id,
        "job_id": claim.job_id,
        "operation_id": claim.operation_id,
        "attempt": claim.attempt,
        "fence": claim.fence,
        "action": "runtime-preflight",
        "request_sha256": "e" * 64,
        "expires_in_seconds": 10,
    })
    issuer = HostHelperGrantIssuer(
        ed25519.Ed25519PrivateKey.from_private_bytes(b"m" * 32),
        clock=lambda: clock.now,
    )
    authority = HostRuntimeAuthorityService(sessions, issuer, clock=lambda: clock.now)
    grant = authority.issue_grant(
        **{**request.model_dump(), "action": ContainerRuntimeAction.RUNTIME_PREFLIGHT},
        certificate_serial="serial-0",
    )
    claims_operation = grant.claims.operation
    assert isinstance(claims_operation, ExecuteContainerRuntimeRequestOperation)
    assert claims_operation.job_id == claim.job_id
    assert claims_operation.operation_id == claim.operation_id
    assert claims_operation.fence == claim.fence
    issuer.public_key.verify(
        bytes.fromhex(grant.signature.value),
        host_helper_grant_signing_bytes(grant.claims),
    )


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
            assert node is not None
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


def test_changed_earlier_rank_is_reprobed_after_pending_peer_completes(tmp_path):
    sessions, _, clock, first_node, service, arguments = _setup(tmp_path, node_count=2)
    with sessions() as session:
        nodes = sorted(session.scalars(select(AgentNode.node_id)))
    assert len(nodes) == 2 and nodes[0] == first_node
    arguments["nodes"] = dict.fromkeys(nodes, False)
    first, error = service.ensure(**arguments, previous=None)
    assert error is None and first.pending_node_id == nodes[0]
    _finish(sessions, first, clock.now)
    second, error = service.ensure(**arguments, previous=first)
    assert error is None and second.pending_node_id == nodes[1]
    with sessions.begin() as session:
        node = session.get(AgentNode, nodes[0])
        assert node is not None
        node.capabilities = [
            value for value in node.capabilities
            if not value.startswith("runtime.preflight.fingerprint.")
        ] + ["runtime.preflight.fingerprint." + "b" * 64]
    _finish(sessions, second, clock.now)
    refreshed, error = service.ensure(**arguments, previous=second)
    assert error is None
    assert refreshed.pending_node_id == nodes[0]
    assert refreshed.attempts[nodes[0]] == 2


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


@pytest.mark.parametrize("state", ["queued", "running"])
def test_pending_probe_deadline_survives_restart_and_progress_updates(tmp_path, state):
    sessions, queue, clock, _, service, arguments = _setup(tmp_path)
    checkpoint, error = service.ensure(**arguments, previous=None)
    assert error is None
    clock.now += timedelta(seconds=179)
    with sessions.begin() as session:
        child = session.get(Job, checkpoint.pending_job_id)
        assert child is not None
        child.state = state
        child.updated_at = clock.now
    restarted = LifecyclePreflight(sessions, queue, lambda: clock.now, 10)
    pending, error = restarted.ensure(**arguments, previous=checkpoint)
    assert error is None and pending.pending_job_id == checkpoint.pending_job_id
    clock.now += timedelta(seconds=1)
    timed_out, error = restarted.ensure(**arguments, previous=pending)
    assert error == "runtime_preflight.deadline_exceeded"
    assert timed_out.attempts == checkpoint.attempts
    assert timed_out.receipts == {}


def test_completed_probe_is_consumed_even_when_controller_resumes_after_deadline(tmp_path):
    sessions, _, clock, node_id, service, arguments = _setup(tmp_path)
    checkpoint, _ = service.ensure(**arguments, previous=None)
    _finish(sessions, checkpoint, clock.now)
    clock.now += timedelta(seconds=180)
    completed, error = service.ensure(**arguments, previous=checkpoint)
    assert error is None and completed.pending_job_id is None
    assert node_id in completed.receipts


@pytest.mark.parametrize("probe_completed", [True, False])
def test_high_level_gate_finishes_probe_before_dispatching_expensive_transfer(tmp_path, probe_completed):
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
    pending_result = pending.result
    assert pending_result is not None
    assert pending_result.child_operation_id is None
    pending_preflight = pending_result.preflight
    assert pending_preflight is not None
    assert pending_preflight.pending_job_id
    assert not artifacts.children
    pending_progress = pending.progress.operation
    assert pending_progress is not None
    assert pending_progress.phase == "runtime-preflight"
    if probe_completed:
        _finish(sessions, pending_preflight, NOW)
    restarted = _service(sessions, NOW + timedelta(seconds=180), lifecycle, artifacts, artifacts=CompleteArtifactInspector(missing_spark_bytes=1024))
    restarted.tick()
    active = restarted.get(operation.operation_id)
    if not probe_completed:
        assert active.state == "failed"
        assert not artifacts.children
        return
    active_result = active.result
    assert active_result is not None
    assert active.progress.phase_index == 0
    assert active_result.child_operation_id
    active_preflight = active_result.preflight
    assert active_preflight is not None
    assert active_preflight.pending_job_id is None
    assert len(artifacts.children) == 1
