"""Connected request/store/worker regressions for damaged artifact observations."""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx2
import pytest
from sqlalchemy import select, update
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import LifecycleState, canonical_message
from vonk_agent_protocol.agent_words import ProfileChildPhase
from vonk_control.distribution_executor.receipts import _ChildView
from vonk_control.models import (
    AgentPresence,
    Job,
    NodeInventorySnapshot,
    RecipeRun,
)
from vonk_control.preparation_contract import RuntimeImageIdentity
from vonk_control.run_switch_contract import (
    RunSwitchApplyRequest,
    RunSwitchCleanupResult,
    RunSwitchPhase,
    RunSwitchRuntimeImageResult,
    RunSwitchTargetTransferEvidenceResult,
)
from vonk_control.run_switch_observation_contract import RunSwitchObservedImageIdentity
from vonk_control.run_switch_operations import (
    PhaseExecution,
    _validate_artifact_execution,
)
from vonk_control.run_switch_operations.image_receipts import (
    _require_profile_runtime_image,
)
from vonk_control.runtime_image_preparation import RuntimeImageReceipt

from .non_blocking import assert_ended_without_blocking
from .test_recipe_operations import installed_recipe, setup_services
from .test_run_switch_operations import (
    NOW,
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
    _fixture_child_result,
    _request,
    _runtime_receipt,
    _service,
)


@pytest.mark.parametrize(
    "fault",
    [
        "transfer-bytes",
        "nas-eviction",
        "unplanned-cleanup",
        "cleanup-response-loss",
        "verify-image",
        "prepare-image",
        "verify-archive",
        "profile-size",
        "profile-architecture",
        "profile-interface",
    ],
)
@pytest.mark.parametrize("repair", [False, True])
def test_artifact_observation_repairs_or_ends_and_admits_fresh(tmp_path, fault, repair):
    """Catches unlimited receipt retry and post-effect reports treated as authority.

    The real apply/store/worker path cannot publish a workload from inconsistent
    receipts. A restart preserves its deadline, expiry issues no destructive
    replay, and the next request is admitted on the same Spark.
    """
    sessions, lifecycle, _, mapping, build, nodes = setup_services(tmp_path)
    installed_recipe(lifecycle, mapping, build, nodes, request_id=str(uuid4()))
    now = [NOW]

    class FaultyObservation(RecordingArtifactExecutor):
        damaged = True

        def __init__(self):
            super().__init__()
            self.cleanup_requests: list[str] = []

        def execute(self, plan, phase, **kwargs):
            if phase.kind == ProfileChildPhase.CLEANUP:
                self.cleanup_requests.append(kwargs["request_key"])
            if self.damaged and phase.kind == (
                ProfileChildPhase.TRANSFER
                if fault == "transfer-bytes"
                else ProfileChildPhase.VERIFY
                if fault.startswith(("verify-", "profile-", "prepare-"))
                else ProfileChildPhase.CLEANUP
            ):
                self.calls.append(phase.kind)
                if fault == "cleanup-response-loss":
                    raise httpx2.ReadError("cleanup response unavailable")
                if fault == "transfer-bytes":
                    receipt = RunSwitchTargetTransferEvidenceResult(
                        phase=ProfileChildPhase.TRANSFER.value,
                        subphase="target-copy",
                        node_id=nodes[0],
                        copied_bytes=0,
                    ).model_copy(update={"copied_bytes": -1})
                elif fault == "prepare-image":
                    # The phase adapter returns a validly shaped but stale image
                    # projection. No ingress verification or denied grant occurs.
                    prepared = RuntimeImageReceipt.model_validate_json(
                        canonical_message(
                            _runtime_receipt(plan, image="sha256:" + "f" * 64)
                        )
                    )
                    prepare_phase = RunSwitchPhase.model_construct(
                        index=phase.index,
                        kind=ProfileChildPhase.PREPARE.value,
                        subphase="runtime-image",
                    )
                    _validate_artifact_execution(
                        plan,
                        prepare_phase,
                        RunSwitchRuntimeImageResult(
                            phase=ProfileChildPhase.PREPARE.value,
                            subphase="runtime-image",
                            runtime_image=prepared,
                            image_digest=prepared.image_digest,
                            oci_layout_sha256=prepared.oci_archive_sha256,
                            image_bytes=prepared.image_bytes,
                        ),
                    )
                    return super().execute(plan, phase, **kwargs)
                elif fault.startswith("profile-"):
                    expected = RuntimeImageIdentity(
                        image_digest=plan.image_digest,
                        oci_layout_sha256=plan.build.oci_layout_sha256,
                        image_bytes=plan.build.image_bytes,
                        architecture="linux-arm64",
                        runtime_interface="vonk.runtime.v1",
                    )
                    observed = RunSwitchObservedImageIdentity(
                        image_digest=expected.image_digest,
                        oci_layout_sha256=expected.oci_layout_sha256,
                        image_bytes=expected.image_bytes + 1
                        if fault == "profile-size"
                        else expected.image_bytes,
                        architecture="unreadable"
                        if fault == "profile-architecture"
                        else expected.architecture,
                        runtime_interface="unreadable"
                        if fault == "profile-interface"
                        else expected.runtime_interface,
                    )
                    _require_profile_runtime_image(expected, observed)
                    return super().execute(plan, phase, **kwargs)
                elif fault.startswith("verify-"):
                    execution = super().execute(plan, phase, **kwargs)
                    field = (
                        "verified_image_digest"
                        if fault == "verify-image"
                        else "verified_oci_layout_sha256"
                    )
                    changed = (
                        "sha256:" + "f" * 64 if fault == "verify-image" else "f" * 64
                    )
                    assert execution.result is not None
                    receipt = execution.result.model_copy(update={field: changed})
                else:
                    receipt = RunSwitchCleanupResult(
                        phase=ProfileChildPhase.CLEANUP.value,
                        scope="spark-local",
                        reclaimed_bytes=0,
                        protected_referenced_bytes=0,
                        reclaimed_digests=["f" * 64]
                        if fault == "unplanned-cleanup"
                        else [],
                        protected_digests=[],
                        nas_evicted=fault == "nas-eviction",
                    )
                return PhaseExecution(result=receipt)
            return super().execute(plan, phase, **kwargs)

    executor = FaultyObservation()
    inspector = CompleteArtifactInspector(
        reclaimable_bytes=30, missing_spark_bytes=1024
    )

    def restart():
        service = _service(sessions, now[0], lifecycle, executor, artifacts=inspector)
        service._clock = lambda: now[0]
        return service

    service = restart()
    request = _request(sessions, nodes[0], retention="reclaim-unreferenced")

    def fresh():
        return service.apply(
            RunSwitchApplyRequest.model_validate_json(
                canonical_message(
                    {**request.model_dump(mode="json"), "request_key": str(uuid4())}
                )
            ),
            actor="admin",
        )

    operation = fresh()
    for _ in range(10):
        service.tick()
        held = service.get(operation.operation_id)
        assert held.result is not None
        if held.result.retry_reason is not None:
            break
    assert held.state in {LifecycleState.RUNNING, LifecycleState.OBSERVING}
    assert held.result is not None
    due = held.next_attempt_at
    assert due is not None and due > now[0]
    with sessions() as session:
        assert not list(session.scalars(select(RecipeRun)))
        assert not list(
            session.scalars(
                select(Job).where(Job.kind == WireAgentOperation.RECIPE_START.value)
            )
        )
    calls = len(executor.calls)
    assert not service.tick()
    assert len(executor.calls) == calls
    service = restart()
    if fault == "cleanup-response-loss":
        # A post-dispatch exception reconnects with the accepted cleanup key,
        # including across a restart; it cannot restart the plan or generation.
        now[0] = due
        assert service.tick()
        held = service.get(operation.operation_id)
        assert held.result is not None and held.next_attempt_at is not None
        due = held.next_attempt_at
        assert len(executor.cleanup_requests) >= 2
        assert len(set(executor.cleanup_requests)) == 1
        calls = len(executor.calls)
    if repair:
        executor.damaged = False
        now[0] = due
        assert service.tick()
        # The first tick can refresh the plan. Continue its normal worker path
        # until the repaired phase publishes its verified receipt.
        wanted = (
            ProfileChildPhase.TRANSFER
            if fault == "transfer-bytes"
            else ProfileChildPhase.VERIFY
            if fault.startswith(("verify-", "profile-", "prepare-"))
            else ProfileChildPhase.CLEANUP
        )
        for _ in range(10):
            resumed = service.get(operation.operation_id)
            assert resumed.result is not None
            if wanted in resumed.result.completed_phases:
                break
            service.tick()
        assert resumed.result is not None
        assert wanted in resumed.result.completed_phases
    else:
        assert held.result.final_verify_started_at is None
        deadline = held.result.recovery_deadline_at
        assert deadline is not None
        # Dependency expiry survives restart without spending verification time.
        restored = service.get(operation.operation_id)
        assert restored.result is not None
        assert restored.result.recovery_deadline_at == deadline
        now[0] = deadline

        def end(_operation):
            assert service.tick()
            return service.get(operation.operation_id)

        def cause(receipt):
            assert receipt.result.failure_code is not None
            assert receipt.result.failed_phase is not None
            assert receipt.result.retryable

        ended, admitted = assert_ended_without_blocking(
            SimpleNamespace(sessions=sessions),
            operation,
            end=end,
            fresh=lambda _world: fresh(),
            assert_reason=cause,
        )
        assert len(executor.calls) == calls
        with sessions() as session:
            assert not list(session.scalars(select(RecipeRun)))
            persisted = session.get(Job, operation.operation_id)
            assert persisted is not None and persisted.state == ended.state
            from vonk_control.lifecycle.types import Effect
            from vonk_control.run_switch_operations.provider import _ADAPTER

            assert ended.result is not None
            assert ended.result.observation_deadline_at == now[0]
            assert (
                _ADAPTER.lifecycle(persisted, ended.result, now[0]).effect
                is Effect.UNKNOWN
            )
        assert admitted.operation_id != operation.operation_id
        assert admitted.node_ids == list(nodes)


@pytest.mark.parametrize("repair", [False, True])
def test_accepted_readiness_observation_has_a_restart_safe_end(tmp_path, repair):
    """An unavailable executor cannot leave an accepted plan retrying forever."""
    sessions, lifecycle, _, mapping, build, nodes = setup_services(tmp_path)
    installed_recipe(lifecycle, mapping, build, nodes, request_id=str(uuid4()))
    now = [NOW]
    executor = RecordingArtifactExecutor()

    def restart(available=False):
        service = _service(
            sessions,
            now[0],
            lifecycle,
            executor if available else None,
            artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
        )
        service._clock = lambda: now[0]
        return service

    service = restart()
    request = _request(sessions, nodes[0])

    def fresh():
        return service.apply(
            RunSwitchApplyRequest.model_validate_json(
                canonical_message(
                    {
                        **request.model_dump(mode="json"),
                        "request_key": str(uuid4()),
                    }
                )
            ),
            actor="admin",
        )

    operation = fresh()
    assert operation.result is not None
    due = operation.next_attempt_at
    deadline = operation.result.observation_deadline_at
    assert deadline is not None and deadline > NOW
    assert due is not None and due < deadline
    service = restart(available=repair)
    now[0] = due
    assert service.tick()
    observed = service.get(operation.operation_id)
    if repair:
        assert not observed.blockers
        assert observed.state in {LifecycleState.QUEUED, LifecycleState.RUNNING}
    else:
        assert observed.result is not None
        assert observed.result.final_verify_started_at is None
        # Before preparation begins, dependency recovery is bounded from
        # request creation; observing readiness must not renew that window.
        deadline = NOW + timedelta(hours=1)
        service = restart()
        restored = service.get(operation.operation_id)
        assert restored.result is not None
        assert restored.result == observed.result
        now[0] = deadline
        assert service.tick()
        observed = service.get(operation.operation_id)

        def cause(receipt):
            assert receipt.result.failure_code is not None
            assert receipt.result.retryable

        _, admitted = assert_ended_without_blocking(
            SimpleNamespace(sessions=sessions),
            operation,
            end=lambda _operation: observed,
            fresh=lambda _world: fresh(),
            assert_reason=cause,
        )
        assert admitted.operation_id != operation.operation_id
    assert not executor.calls
    with sessions() as session:
        assert not list(session.scalars(select(RecipeRun)))
        assert not list(
            session.scalars(
                select(Job).where(Job.kind == WireAgentOperation.RECIPE_START.value)
            )
        )


@pytest.mark.parametrize("copying", [True, False])
def test_transfer_that_keeps_copying_outlives_the_recovery_window(tmp_path, copying):
    """Catches a fixed total window ending a transfer that still copies bytes.

    Copied bytes restart the window, so a slow transfer runs past several
    windows; a transfer whose bytes stop is ended one window after the last.
    """
    from vonk_control.run_switch_progress import RECOVERY_STALL_WINDOW

    sessions, lifecycle, _, mapping, build, nodes = setup_services(tmp_path)
    installed_recipe(lifecycle, mapping, build, nodes, request_id=str(uuid4()))
    now = [NOW]
    copied = [0]
    transfer: list = []

    class SlowTransfer(RecordingArtifactExecutor):
        def execute(self, plan, phase, **kwargs):
            if phase.kind == ProfileChildPhase.TRANSFER:
                transfer[:] = [plan, phase]
            return super().execute(plan, phase, **kwargs)

    def report_bytes():
        """The distribution child reports what it has copied so far."""
        plan, phase = transfer
        for child_id in executor.children:
            executor.children[child_id] = _ChildView(
                state="running",
                result=_fixture_child_result(
                    plan,
                    phase,
                    {"progress": {"completed_bytes": copied[0], "total_bytes": 10**9}},
                ),
            )

    executor = SlowTransfer(child_transfer=True)
    inspector = CompleteArtifactInspector(
        reclaimable_bytes=30, missing_spark_bytes=1024
    )
    service = _service(sessions, now[0], lifecycle, executor, artifacts=inspector)
    service._clock = lambda: now[0]
    request = _request(sessions, nodes[0], retention="reclaim-unreferenced")
    operation = service.apply(
        RunSwitchApplyRequest.model_validate_json(
            canonical_message(
                {**request.model_dump(mode="json"), "request_key": str(uuid4())}
            )
        ),
        actor="admin",
    )
    step = RECOVERY_STALL_WINDOW / 4
    for _ in range(14):  # three and a half windows
        if copying:
            copied[0] += 1
        if transfer:
            report_bytes()
        service.tick()
        now[0] += step
        with sessions.begin() as session:
            # The Sparks stay connected: only the transfer's bytes vary.
            session.execute(
                update(NodeInventorySnapshot).values(
                    observed_at=now[0], received_at=now[0]
                )
            )
            session.execute(update(AgentPresence).values(observed_at=now[0]))
    service.tick()
    held = service.get(operation.operation_id)
    assert held.result is not None
    assert ProfileChildPhase.TRANSFER in executor.calls
    if copying:
        assert held.state in {LifecycleState.RUNNING, LifecycleState.OBSERVING}
        assert held.result.failure_code is None
        assert held.result.recovery_deadline_at is not None
        assert held.result.recovery_deadline_at > now[0]
    else:
        assert held.state not in {LifecycleState.RUNNING, LifecycleState.OBSERVING}
        assert held.result.failed_phase is not None
        assert held.result.retryable
