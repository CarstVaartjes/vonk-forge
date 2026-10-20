"""Lost terminal reports heal through the real queue, PostgreSQL and parent."""

import subprocess
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_agent_protocol import (
    AgentClaim,
    AgentResult,
    AgentResultState,
    ArtifactDistributionResult,
    DistributionObject,
    LifecycleState,
    OperationProgress,
    ProgressPhase,
    WaitReason,
)
from vonk_agent_protocol import (
    AgentOperation as OperationKind,
)
from vonk_agent_protocol.agent_words import ProfileChildPhase, ProfileEffectState
from vonk_control.agent_jobs import AgentJobService
from vonk_control.distribution import DistributionService, MemoryObjectSource
from vonk_control.distribution_executor import (
    DurableDistributionPhaseExecutor,
    RuntimeImagePull,
)
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Job,
    RecipeRun,
)
from vonk_control.recipe_routes import RecipeRouteService
from vonk_control.run_switch_contract import (
    RunSwitchApplyRequest,
)
from vonk_control.run_switch_operations import PhaseExecution, RunSwitchOperationService

from .runtime_identity_support import claim_agent
from .test_lifecycle_preflight import _finish
from .test_recipe_operations import (
    NOW,
    ConcurrentPublisher,
    bind_route_publications,
    complete_started_recipe,
    installed_recipe,
    setup_services,
)
from .test_run_switch_operations import (
    MODEL_ARTIFACT,
    MODEL_ARTIFACT_SET,
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
    _request,
    _service,
)


@dataclass
class AgentPeer:
    """A process restart loses memory, retaining only its exact terminal receipt."""

    jobs: AgentJobService
    receipt: Path

    def finish_without_reporting(self, claim: AgentClaim) -> AgentResult:
        result = AgentResult(
            fence=claim.fence,
            state=AgentResultState.SUCCEEDED,
            result=ArtifactDistributionResult(downloaded_bytes=1024),
        )
        self.receipt.write_text(result.model_dump_json())
        return result

    def replay(self) -> None:
        result = AgentResult.model_validate_json(self.receipt.read_bytes())
        self.jobs.record_late_result(result)


class TransferExecutor(RecordingArtifactExecutor):
    """Keep the real transfer boundary; unrelated artifact phases are synchronous."""

    routes: RecipeRouteService

    def __init__(self, sessions, jobs, clock):
        super().__init__()
        self.source = source = MemoryObjectSource({MODEL_ARTIFACT: b"x" * 1024})
        self.distribution = DistributionService(source, sessions=sessions, clock=clock)
        self.durable = DurableDistributionPhaseExecutor(
            sessions, jobs, self.distribution, clock=clock
        )

    def execute(self, plan, phase, **kwargs):
        if phase.kind != ProfileChildPhase.TRANSFER:
            return super().execute(plan, phase, **kwargs)
        image, archive, _size, _build = self.durable._runtime_identity(
            plan, kwargs["progress"]
        )
        model = DistributionObject(
            name="model.bin", sha256=MODEL_ARTIFACT, bytes=1024, kind="model"
        )
        self.source.register_artifact_set(MODEL_ARTIFACT_SET, (model,))
        self.source.register_runtime_image(image, archive)
        node = phase.node_ids[0]
        assignment = self.durable._assignment(
            plan,
            node,
            (model,),
            RuntimeImagePull(
                image_digest=image, config_digest="sha256:" + "9" * 64, address=archive
            ),
            model_set_digest=MODEL_ARTIFACT_SET,
        )
        child_id = self.durable._ensure_child(
            plan,
            phase,
            actor=kwargs["actor"],
            request_key=kwargs["request_key"],
            cached=(),
            assignments={node: assignment},
            target_order=(node,),
            workload_intent_ordinal=kwargs["progress"].workload_intent_ordinal,
            target_bytes=1024,
        )
        return PhaseExecution(operation_id=child_id)

    def get(self, operation_id):
        return self.durable.get(operation_id)


@pytest.fixture
def transfer(tmp_path, postgres_engine):
    sessions, lifecycle, _queue, mapping, build, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    installed_recipe(lifecycle, mapping, build, nodes, request_id=str(uuid4()))
    now = [NOW]
    jobs = AgentJobService(sessions, clock=lambda: now[0])
    executor = TransferExecutor(sessions, jobs, lambda: now[0])
    _publications, executor.routes = bind_route_publications(
        sessions, lifecycle, ConcurrentPublisher()
    )
    lifecycle._clock = lambda: now[0]
    service = _service(
        sessions,
        NOW,
        lifecycle,
        executor,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    service._clock = lambda: now[0]
    request = _request(sessions, nodes[0])
    parent = service.apply(
        RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid4())),
        actor="admin",
    )
    for _ in range(12):
        service.tick()
        current = service.get(parent.operation_id)
        if current.result is not None and current.result.child_operation_id is not None:
            break
    else:
        pytest.fail("transfer child was never issued")
    claim = claim_agent(jobs, nodes[0], "serial-0", 60, preflight_fingerprint="a" * 64)
    assert claim is not None and claim.operation == OperationKind.ARTIFACT_DISTRIBUTION
    jobs.heartbeat(
        claim,
        OperationProgress(phase=ProgressPhase.TRANSFER, completed_bytes=512),
        60,
    )
    return sessions, lifecycle, jobs, executor, service, parent.operation_id, claim, now


def _lapse(world):
    sessions, _lifecycle, jobs, _executor, service, parent_id, claim, now = world
    with sessions() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
        assert attempt is not None
        now[0] = attempt.lease_deadline + timedelta(seconds=1)
    jobs.reconcile_orders()
    service.tick()
    current = service.get(parent_id)
    assert current.result is not None
    return current


def _observe_transfer(world):
    _sessions, _lifecycle, _jobs, _executor, service, parent_id, _claim, now = world
    for _ in range(12):
        current = service.get(parent_id)
        if ProfileChildPhase.TRANSFER in current.completed_phases:
            return current
        if current.next_attempt_at is not None:
            now[0] = max(now[0], current.next_attempt_at)
        service.tick()
    pytest.fail("transfer did not settle: " + service.get(parent_id).model_dump_json())


def _complete_parent(world):
    sessions, lifecycle, _jobs, executor, service, parent_id, _claim, now = world
    for _ in range(24):
        current = service.get(parent_id)
        if current.state == LifecycleState.SUCCEEDED:
            return
        assert current.state in {LifecycleState.RUNNING, LifecycleState.OBSERVING}, (
            current.status_reason
        )
        assert current.result is not None
        if current.next_attempt_at is not None:
            now[0] = max(now[0], current.next_attempt_at)
        checkpoint = current.result.preflight
        if checkpoint is not None and checkpoint.pending_job_id:
            with sessions() as session:
                pending = session.get(Job, checkpoint.pending_job_id)
                needs_result = pending is not None and pending.state in {
                    LifecycleState.QUEUED,
                    LifecycleState.RUNNING,
                }
            if needs_result:
                _finish(sessions, checkpoint, now[0])
        child_id = current.result.child_operation_id
        if child_id is not None:
            with sessions() as session:
                child = session.get(Job, child_id)
                is_start = (
                    child is not None and child.kind == OperationKind.RECIPE_START
                )
            if is_start:
                complete_started_recipe(sessions, lifecycle, child_id)
        with sessions() as session:
            run_ids = tuple(session.scalars(select(RecipeRun.id)))
        for run_id in run_ids:
            executor.routes._clock = lambda: now[0]
            executor.routes.publish_run(run_id)
        service.tick()
    pytest.fail(
        "run-switch did not finish: " + service.get(parent_id).model_dump_json()
    )


def test_restart_replays_exact_terminal_receipt_and_advances_parent(transfer, tmp_path):
    """Catches late success retained as history while the parent retries forever."""
    sessions, lifecycle, jobs, executor, _service, parent_id, claim, now = transfer
    peer = AgentPeer(jobs, tmp_path / "receipt.json")
    peer.finish_without_reporting(claim)
    _lapse(transfer)
    # Both processes restart. Neither retains a Python object from the old peer.
    restarted_jobs = AgentJobService(sessions, clock=lambda: now[0])
    AgentPeer(restarted_jobs, peer.receipt).replay()
    AgentPeer(restarted_jobs, peer.receipt).replay()  # Durable replay is idempotent.
    restarted = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: now[0],
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
        artifact_phase_executor=executor,
        memory_floor_bytes=50,
    )
    world = (
        sessions,
        lifecycle,
        restarted_jobs,
        executor,
        restarted,
        parent_id,
        claim,
        now,
    )
    current = _observe_transfer(world)
    assert current.result is not None and current.result.child_operation_id is None
    _complete_parent(world)
    with sessions() as session:
        order = session.scalar(
            select(AgentOperation)
            .join(
                AgentOperationAttempt,
                AgentOperationAttempt.operation_id == AgentOperation.id,
            )
            .where(AgentOperationAttempt.fence == claim.fence)
        )
        assert order is not None and order.state == LifecycleState.SUCCEEDED
        assert order.current_attempt == 1


def test_lost_transfer_is_visible_and_replanned_under_fresh_fence(transfer):
    """Catches silent unknown members and retries that cannot finish their parent."""
    sessions, _lifecycle, jobs, _executor, service, parent_id, claim, now = transfer
    current = _lapse(transfer)
    assert current.result is not None
    assert current.progress.members[0].state == ProfileEffectState.UNKNOWN
    assert current.next_attempt_at is not None
    assert now[0] <= current.next_attempt_at <= now[0] + timedelta(seconds=5)
    assert current.blockers and current.blockers[0].operation is not None
    assert current.blockers[0].operation.id == current.result.child_operation_id
    item = service.activity_provider().get_operation(parent_id)
    assert item is not None and item.wait_reason == WaitReason.OBSERVATION_UNAVAILABLE
    assert item.next_attempt_at is not None
    with sessions() as session:
        order = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == current.result.child_operation_id
            )
        )
        assert order is not None and order.next_action_at is not None
        now[0] = order.next_action_at
    fresh = claim_agent(
        jobs, current.node_ids[0], "serial-0", 60, preflight_fingerprint="a" * 64
    )
    assert fresh is not None and fresh.fence != claim.fence
    assert fresh.payload == claim.payload
    jobs.record_result(
        AgentResult(
            fence=fresh.fence,
            state=AgentResultState.SUCCEEDED,
            result=ArtifactDistributionResult(downloaded_bytes=1024),
        )
    )
    _observe_transfer(transfer)
    _complete_parent(transfer)


@pytest.mark.parametrize(
    "boundary", ["new_attempt", "new_intent", "cancelled_parent", "wrong_bytes"]
)
def test_old_receipt_cannot_revive_newer_attempt_or_intent(
    transfer, tmp_path, boundary
):
    """Catches adopting history after a fresh exact attempt has taken ownership."""
    sessions, _lifecycle, jobs, executor, _service, _parent_id, claim, now = transfer
    peer = AgentPeer(jobs, tmp_path / "receipt.json")
    peer.finish_without_reporting(claim)
    current = _lapse(transfer)
    assert current.result is not None and current.result.child_operation_id is not None
    child_id = current.result.child_operation_id
    if boundary == "new_attempt":
        with sessions() as session:
            order = session.scalar(
                select(AgentOperation).where(AgentOperation.parent_job_id == child_id)
            )
            assert order is not None and order.next_action_at is not None
            now[0] = order.next_action_at
        fresh = claim_agent(
            jobs, current.node_ids[0], "serial-0", 60, preflight_fingerprint="a" * 64
        )
        assert fresh is not None
    elif boundary == "new_intent":
        with sessions.begin() as session:
            node = session.get(AgentNode, current.node_ids[0])
            assert node is not None
            node.workload_intent_ordinal += 1
    elif boundary == "cancelled_parent":
        with sessions.begin() as session:
            child = session.get(Job, child_id)
            assert child is not None
            child.state = LifecycleState.CANCELLED
    else:
        peer.receipt.write_text(
            AgentResult(
                fence=claim.fence,
                state=AgentResultState.SUCCEEDED,
                result=ArtifactDistributionResult(downloaded_bytes=512),
            ).model_dump_json()
        )
    peer.replay()
    child = executor.get(current.result.child_operation_id)
    assert child is not None and child.state != LifecycleState.SUCCEEDED


def test_fresh_controller_contract_registration_starts_without_import_cycle():
    """Catches a receipt-handler import breaking a fresh Controller process."""
    subprocess.run(
        [sys.executable, "-c", "import vonk_control.stored_columns"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_worker_reobserves_retained_success_after_controller_restart(
    transfer, tmp_path, monkeypatch
):
    """Catches a successful receipt stranded in SQL without a new agent report."""
    from vonk_control.agent_jobs import results as ingress

    sessions, lifecycle, jobs, executor, _service, parent_id, claim, now = transfer
    peer = AgentPeer(jobs, tmp_path / "receipt.json")
    receipt = peer.finish_without_reporting(claim)
    current = _lapse(transfer)
    assert current.result is not None and current.result.child_operation_id is not None
    child_id = current.result.child_operation_id
    # The historical producer authenticated and stored the receipt but never
    # projected its success. Exercise that real producer/store boundary.
    with monkeypatch.context() as fault:
        fault.setattr(ingress, "adopt_preparation_receipt", lambda *_args: False)
        jobs.record_late_result(receipt)
    with sessions() as session:
        order = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == child_id)
        )
        assert order is not None and order.next_action_at is not None
        assert now[0] < order.next_action_at
    with sessions() as session:
        node = session.get(AgentNode, current.node_ids[0])
        assert node is not None
        last_contact = node.last_seen_at
    restarted_jobs = AgentJobService(sessions, clock=lambda: now[0])
    # No agent connects or resends. The first worker tick reads durable SQL.
    restarted_jobs.reconcile_orders()
    child = executor.get(child_id)
    assert child is not None and child.state == LifecycleState.SUCCEEDED
    restarted_jobs.reconcile_orders()
    with sessions() as session:
        node = session.get(AgentNode, current.node_ids[0])
        order = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == child_id)
        )
        assert node is not None and node.last_seen_at == last_contact
        assert order is not None and order.current_attempt == 1
    world = (
        sessions,
        lifecycle,
        restarted_jobs,
        executor,
        _service,
        parent_id,
        claim,
        now,
    )
    _observe_transfer(world)
    _complete_parent(world)


def test_an_invalid_retained_receipt_cannot_starve_another_exact_transfer(
    transfer, monkeypatch
):
    """Catches a bounded sweep stuck examining the same unusable history row."""
    from vonk_control.agent_jobs import results as ingress
    from vonk_control.job_documents import DistributionJobPayload
    from vonk_control.run_switch_operations import _stored_job_plan
    from vonk_control.strict_json import read_stored_model

    sessions, _lifecycle, jobs, executor, _service, parent_id, claim, now = transfer
    current = _lapse(transfer)
    assert current.result is not None and current.result.child_operation_id is not None
    old_id = current.result.child_operation_id
    with monkeypatch.context() as fault:
        fault.setattr(ingress, "adopt_preparation_receipt", lambda *_args: False)
        jobs.record_late_result(
            AgentResult(
                fence=claim.fence,
                state=AgentResultState.SUCCEEDED,
                result=ArtifactDistributionResult(downloaded_bytes=512),
            )
        )
    with sessions() as session:
        owner = session.get(Job, parent_id)
        child = session.get(Job, old_id)
        assert owner is not None and child is not None
        plan = _stored_job_plan(owner)
        assert plan is not None
        payload = read_stored_model(
            DistributionJobPayload, child.payload, from_json=True
        )
    phase = next(
        item for item in plan.phases if item.kind == ProfileChildPhase.TRANSFER
    )
    # Another exact preparation can use the same authorized content and node.
    fresh_id = executor.durable._ensure_child(
        plan,
        phase,
        actor="admin",
        request_key=str(uuid4()),
        cached=(),
        assignments=payload.assignments,
        target_order=tuple(payload.target_order),
        workload_intent_ordinal=payload.workload_intent_ordinal,
        target_bytes=1024,
    )
    fresh = claim_agent(
        jobs, current.node_ids[0], "serial-0", 60, preflight_fingerprint="a" * 64
    )
    assert fresh is not None and fresh.fence != claim.fence
    now[0] = fresh.deadline + timedelta(seconds=1)
    with monkeypatch.context() as fault:
        fault.setattr(ingress, "adopt_preparation_receipt", lambda *_args: False)
        jobs.record_late_result(
            AgentResult(
                fence=fresh.fence,
                state=AgentResultState.SUCCEEDED,
                result=ArtifactDistributionResult(downloaded_bytes=1024),
            )
        )
    for _ in range(2):
        jobs.reconcile_orders(limit=1)
    adopted = executor.get(fresh_id)
    assert adopted is not None and adopted.state == LifecycleState.SUCCEEDED
    old = executor.get(old_id)
    assert old is not None and old.state != LifecycleState.SUCCEEDED
