from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from vonk_agent.deadlines import DeadlineBindingError, MonotonicDeadline
from vonk_agent.operations import (
    InspectionDisposition,
    OperationContext,
    OperationRegistry,
    UnsupportedOperation,
)
from vonk_agent.releases import (
    ReleaseDisposition,
    ReleaseEvidence,
    ReleaseInspection,
)
from vonk_agent.state import AgentStateConflict, AgentStateStore
from vonk_agent.update import AgentUpdateError
from vonk_agent.workloads import (
    WorkloadAction,
    WorkloadDisposition,
    WorkloadEvidence,
    WorkloadInspection,
)
from vonk_agent_protocol import (
    AgentClaim,
    AgentOperation,
    AgentProtocolError,
    canonical_message,
)

NODE_ID = "spk_0123456789abcdef0123456789abcdef"
OPERATION_ID = "22222222-2222-4222-8222-222222222222"
FENCE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def update_payload() -> dict[str, object]:
    expires_at = int(time.time()) + 300
    return {
        "artifact": {
            "architecture": "linux-arm64",
            "oci_manifest_digest": "sha256:" + "f" * 64,
            "payload_name": "vonk-agent",
            "payload_sha256": "1" * 64,
            "payload_size": 4096,
        },
        "receipt": {
            "architecture": "linux-arm64",
            "attempt": 1,
            "build_digest": "sha256:" + "2" * 64,
            "claim_deadline": expires_at,
            "expires_at": expires_at,
            "fence": FENCE,
            "node_id": NODE_ID,
            "oci_manifest_digest": "sha256:" + "f" * 64,
            "operation_id": OPERATION_ID,
            "payload_name": "vonk-agent",
            "platform_target_name": (
                "platform/releases/1.2.3/" + "3" * 64 + ".json"
            ),
            "platform_target_sha256": "3" * 64,
            "platform_version": "1.2.3",
            "previous_sha256": "4" * 64,
            "previous_generation": 1,
            "previous_slot": "A",
            "sha256": "1" * 64,
            "size": 4096,
            "target_slot": "B",
            "tuf_targets_version": 7,
        },
        "release": {
            "build_digest": "sha256:" + "2" * 64,
            "platform_version": "1.2.3",
            "protocol_maximum": 1,
            "protocol_minimum": 1,
        },
        "signature": {
            "algorithm": "ed25519",
            "key_id": "5" * 64,
            "value": "6" * 128,
        },
    }


def rollback_payload() -> dict[str, object]:
    expires_at = int(time.time()) + 300
    return {
        "receipt": {
            "action": "operator-rollback",
            "attempt": 1,
            "claim_deadline": expires_at,
            "current_generation": 2,
            "current_sha256": "4" * 64,
            "current_slot": "B",
            "expires_at": expires_at,
            "fence": FENCE,
            "node_id": NODE_ID,
            "operation_id": OPERATION_ID,
        },
        "signature": {
            "algorithm": "ed25519",
            "key_id": "5" * 64,
            "value": "6" * 128,
        },
    }


def claim(
    *,
    operation: AgentOperation = AgentOperation.NODE_PROBE,
    payload=None,
    node_id: str = NODE_ID,
    deadline: datetime | None = None,
    job_id: str = "11111111-1111-4111-8111-111111111111",
    operation_id: str = OPERATION_ID,
    fence: str = FENCE,
) -> AgentClaim:
    body = {} if payload is None else payload
    receipt = body.get("receipt") if isinstance(body, dict) else None
    if deadline is None and isinstance(receipt, dict):
        deadline = datetime.fromtimestamp(receipt["claim_deadline"], tz=UTC)
    return AgentClaim(
        schema_version=1,
        job_id=job_id,
        operation_id=operation_id,
        attempt=1,
        fence=fence,
        node_id=node_id,
        operation=operation,
        base_commit="a" * 40,
        payload_digest=hashlib.sha256(canonical_message(body)).hexdigest(),
        payload=body,
        deadline=deadline or datetime.now(UTC) + timedelta(minutes=1),
    )


def _reject_deadline_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject(
        _cls: type[MonotonicDeadline],
        _value: datetime | MonotonicDeadline,
    ) -> MonotonicDeadline:
        raise DeadlineBindingError("deadline has elapsed")

    monkeypatch.setattr(MonotonicDeadline, "bind", classmethod(reject))


class NeverProbe:
    def collect(self, deadline):
        raise AssertionError("closed registry reached the probe")


class RecordingProbe:
    def __init__(self, evidence=None, error: Exception | None = None) -> None:
        self.evidence = evidence or {"platform": {"status": "ok"}}
        self.error = error
        self.deadlines: list[datetime | MonotonicDeadline] = []

    def collect(self, deadline):
        self.deadlines.append(deadline)
        if self.error is not None:
            raise self.error
        return self.evidence


class RecordingReleaseInstaller:
    def __init__(self, inspection=None) -> None:
        self.requests = []
        self.inspection = inspection or ReleaseInspection(ReleaseDisposition.READY)

    def install(self, request, deadline):
        self.requests.append((request, deadline))
        return ReleaseEvidence(
            "installed", request.target_digest, request.oci_manifest_digest,
            request.adapter_id,
        )

    def inspect(self, request, deadline):
        self.requests.append(("inspect", request))
        return self.inspection


class RecordingWorkloads:
    def __init__(self, inspection=None) -> None:
        self.requests = []
        self.inspection = inspection or WorkloadInspection(WorkloadDisposition.READY)

    def execute(self, request, deadline, job_id, operation_id, attempt, fence):
        self.requests.append((request, deadline, job_id, operation_id, attempt, fence))
        return WorkloadEvidence(
            "healthy" if request.action is WorkloadAction.HEALTH else "completed",
            request.action,
            request.workload_id,
            request.release_digest,
            "8" * 64,
        )

    def inspect(self, request, deadline, job_id, operation_id, attempt, fence):
        self.requests.append(("inspect", request))
        return self.inspection


class RecordingUpdates:
    def __init__(self, error: Exception | None = None) -> None:
        self.commands = []
        self.rollbacks = 0
        self.deadlines: list[MonotonicDeadline] = []
        self.bindings: list[tuple[str, str]] = []
        self.error = error

    def execute(self, command, deadline, operation_id, fence):
        self.commands.append(command)
        self.deadlines.append(deadline)
        self.bindings.append((operation_id, fence))
        if self.error is not None:
            raise self.error
        return {
            "status": "pending-activation",
            "target_slot": "B",
            "previous_slot": "A",
        }

    def rollback(self, command, deadline, operation_id, fence):
        self.rollbacks += 1
        self.commands.append(command)
        self.deadlines.append(deadline)
        self.bindings.append((operation_id, fence))
        if self.error is not None:
            raise self.error
        return {
            "status": "pending-activation",
            "target_slot": "A",
            "previous_slot": "B",
        }


def context(tmp_path) -> OperationContext:
    return OperationContext(
        node_id=NODE_ID,
        state=AgentStateStore(tmp_path / "state"),
        probe=NeverProbe(),
    )


def test_unknown_duck_typed_or_known_unimplemented_operation_never_dispatches(tmp_path) -> None:
    registry = OperationRegistry()
    operation_context = context(tmp_path)

    with pytest.raises(UnsupportedOperation):
        registry.execute(
            SimpleNamespace(operation="system.exec", payload={}),  # type: ignore[arg-type]
            operation_context,
        )
    with pytest.raises(UnsupportedOperation):
        registry.execute(
            claim(operation=AgentOperation.RELEASE_INSTALL),
            operation_context,
        )


def test_release_and_workload_operations_dispatch_only_to_typed_interfaces(tmp_path) -> None:
    release = RecordingReleaseInstaller()
    workloads = RecordingWorkloads()
    release_payload = {
        "schema_version": 1,
        "target_name": "node-runtime-2026-08",
        "oci_manifest_digest": "sha256:" + "1" * 64,
        "target_digest": "2" * 64,
        "provenance_digest": "3" * 64,
        "adapter_id": "node-runtime-v1",
    }
    release_context = OperationContext(
        NODE_ID, AgentStateStore(tmp_path / "release-state"), NeverProbe(),
        release, workloads,
    )

    installed = OperationRegistry().execute(
        claim(operation=AgentOperation.RELEASE_INSTALL, payload=release_payload),
        release_context,
    )

    assert installed["status"] == "ok"
    assert installed["evidence"]["release_digest"] == "2" * 64
    assert len(release.requests) == 1

    operation_payloads = {
        AgentOperation.WORKLOAD_PREPARE: {"profile_digest": "5" * 64},
        AgentOperation.WORKLOAD_START: {"preparation_digest": "6" * 64},
        AgentOperation.WORKLOAD_STOP: {},
        AgentOperation.WORKLOAD_HEALTH: {},
        AgentOperation.WORKLOAD_VERIFY: {"expected_digest": "7" * 64},
    }
    for index, (operation, extra) in enumerate(operation_payloads.items()):
        payload = {
            "schema_version": 1,
            "workload_id": "deepseek-v4-flash-a",
            "release_digest": "4" * 64,
            "adapter_id": "node-runtime-v1",
        } | extra
        operation_context = OperationContext(
            NODE_ID,
            AgentStateStore(tmp_path / f"workload-state-{index}"),
            NeverProbe(),
            release,
            workloads,
        )
        executed = OperationRegistry().execute(
            claim(operation=operation, payload=payload), operation_context
        )
        assert executed["status"] == "ok"

    assert [item[0].action for item in workloads.requests] == list(WorkloadAction)


def test_agent_update_and_rollback_dispatch_only_to_typed_updater(tmp_path) -> None:
    updates = RecordingUpdates()
    payload = update_payload()
    update_context = OperationContext(
        node_id=NODE_ID,
        state=AgentStateStore(tmp_path / "update-state"),
        probe=NeverProbe(),
        updates=updates,
    )
    rollback_context = OperationContext(
        node_id=NODE_ID,
        state=AgentStateStore(tmp_path / "rollback-state"),
        probe=NeverProbe(),
        updates=updates,
    )

    update = OperationRegistry().execute(
        claim(operation=AgentOperation.AGENT_UPDATE, payload=payload),
        update_context,
    )
    rollback = OperationRegistry().execute(
        claim(operation=AgentOperation.AGENT_ROLLBACK, payload=rollback_payload()),
        rollback_context,
    )

    assert update["evidence"]["target_slot"] == "B"
    assert rollback["evidence"]["target_slot"] == "A"
    assert len(updates.commands) == 2
    assert updates.bindings == [(OPERATION_ID, FENCE), (OPERATION_ID, FENCE)]
    assert updates.rollbacks == 1
    assert len(updates.deadlines) == 2
    assert all(type(deadline) is MonotonicDeadline for deadline in updates.deadlines)


@pytest.mark.parametrize(
    ("operation", "payload", "error_code"),
    [
        (
            AgentOperation.AGENT_UPDATE,
            update_payload(),
            "agent_update_failed",
        ),
        (AgentOperation.AGENT_ROLLBACK, rollback_payload(), "agent_rollback_failed"),
    ],
)
def test_agent_update_failures_use_operation_specific_bounded_codes(
    tmp_path, operation, payload, error_code
) -> None:
    updates = RecordingUpdates(AgentUpdateError("bounded failure"))
    operation_context = OperationContext(
        node_id=NODE_ID,
        state=AgentStateStore(tmp_path / operation.value),
        probe=NeverProbe(),
        updates=updates,
    )

    execution = OperationRegistry().execute(
        claim(operation=operation, payload=payload), operation_context
    )

    assert execution.result.state == "failed"
    assert execution.result.result == {
        "error_code": error_code,
        "status": "failed",
    }


@pytest.mark.parametrize(
    ("disposition", "expected_reason"),
    [
        (WorkloadDisposition.COMPENSATE, "compensation-required"),
        (
            WorkloadDisposition.OPERATOR_INTERVENTION,
            "operator-intervention-required",
        ),
    ],
)
def test_interrupted_mutation_persists_operator_disposition_and_replays_exactly(
    tmp_path,
    disposition: WorkloadDisposition,
    expected_reason: str,
) -> None:
    payload = {
        "schema_version": 1,
        "workload_id": "deepseek-v4-flash-a",
        "release_digest": "4" * 64,
        "adapter_id": "node-runtime-v1",
        "preparation_digest": "6" * 64,
    }
    active = claim(operation=AgentOperation.WORKLOAD_START, payload=payload)
    state_root = tmp_path / "state"
    state = AgentStateStore(state_root)
    state.begin(active)
    evidence = WorkloadEvidence(
        "inspected",
        WorkloadAction.START,
        "deepseek-v4-flash-a",
        "4" * 64,
        "8" * 64,
    )
    workloads = RecordingWorkloads(WorkloadInspection(disposition, evidence))
    operation_context = OperationContext(
        NODE_ID, state, NeverProbe(), RecordingReleaseInstaller(), workloads
    )
    registry = OperationRegistry()

    recovered = registry.execute(active, operation_context)

    assert recovered.result.state == "waiting-for-operator"
    assert recovered.result.result == {"reason": expected_reason}
    assert recovered.result.job_id == active.job_id
    assert recovered.result.operation_id == active.operation_id
    assert recovered.result.attempt == active.attempt
    assert recovered.result.fence == active.fence
    assert recovered.result.deadline == active.deadline
    assert recovered.canonical_result == canonical_message(recovered.result)
    assert recovered.replayed is True
    assert len(workloads.requests) == 1
    assert workloads.requests[0][0] == "inspect"
    assert workloads.requests[0][1].action is WorkloadAction.START

    persisted = state.lookup_exact(active)
    assert persisted is not None
    assert persisted.result == recovered.result
    assert persisted.canonical_result == recovered.canonical_result
    restarted = OperationContext(
        NODE_ID,
        AgentStateStore(state_root),
        NeverProbe(),
        RecordingReleaseInstaller(),
        workloads,
    )
    replay = registry.execute(active, restarted)

    assert replay.result == recovered.result
    assert replay.canonical_result == recovered.canonical_result
    assert replay.replayed is True
    assert len(workloads.requests) == 1
    pending = restarted.state.recover_pending()
    assert pending is not None
    assert pending.canonical_result == replay.canonical_result

    next_probe = RecordingProbe()
    next_claim = claim(
        job_id="33333333-3333-4333-8333-333333333333",
        operation_id="44444444-4444-4444-8444-444444444444",
        fence="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    )
    next_context = OperationContext(NODE_ID, restarted.state, next_probe)
    with pytest.raises(AgentStateConflict):
        registry.execute(next_claim, next_context)
    acknowledged = restarted.state.acknowledge(replay.result)

    assert acknowledged.acknowledged_at is not None
    assert restarted.state.recover_pending() is None
    followup = registry.execute(next_claim, next_context)

    assert followup.result.state == "succeeded"
    assert len(next_probe.deadlines) == 1


def test_node_probe_rejects_every_nonempty_payload_before_dispatch(tmp_path) -> None:
    with pytest.raises(AgentProtocolError):
        OperationRegistry().execute(
            claim(payload={"selector": "device_identity"}),
            context(tmp_path),
        )


@pytest.mark.parametrize(
    ("count", "state"), ((0, "succeeded"), (1, "failed"), (None, "failed"))
)
def test_zero_compute_gate_succeeds_only_for_exact_zero_count(
    tmp_path, count: int | None, state: str
) -> None:
    probe = RecordingProbe(
        {
            "vonk_forge": {
                "accelerator": {
                    "active_nvidia_compute_processes": count,
                }
            },
            "nvidia": {"tools": {}},
        }
    )

    execution = OperationRegistry().execute(
        claim(payload={"require_active_nvidia_compute_processes": 0}),
        OperationContext(
            node_id=NODE_ID,
            state=AgentStateStore(tmp_path / "state"),
            probe=probe,
        ),
    )

    assert execution.result.state == state
    if state == "failed":
        assert execution.result.result == {
            "status": "failed",
            "error_code": "probe_failed",
        }


def test_wrong_node_and_expired_claim_fail_before_state_or_probe(tmp_path) -> None:
    probe = RecordingProbe()
    store = AgentStateStore(tmp_path / "state")
    operation_context = OperationContext(node_id=NODE_ID, state=store, probe=probe)

    with pytest.raises(AgentProtocolError, match="node"):
        OperationRegistry().execute(
            claim(node_id="spk_ffffffffffffffffffffffffffffffff"), operation_context
        )
    with pytest.raises(AgentProtocolError, match="deadline"):
        OperationRegistry().execute(
            claim(deadline=datetime.now(UTC) - timedelta(seconds=1)), operation_context
        )

    assert probe.deadlines == []
    assert store.recover_active() is None


def test_success_is_canonical_persisted_and_replayed_without_recollection(tmp_path) -> None:
    probe = RecordingProbe({"z": 2, "a": {"status": "ok"}})
    store = AgentStateStore(tmp_path / "state")
    operation_context = OperationContext(node_id=NODE_ID, state=store, probe=probe)
    request = claim()

    first = OperationRegistry().execute(request, operation_context)
    second = OperationRegistry().execute(request, operation_context)

    assert first.result.state == "succeeded"
    assert first.result.result == {"status": "ok", "evidence": {"a": {"status": "ok"}, "z": 2}}
    assert first.canonical_result == canonical_message(first.result)
    assert first.replayed is False
    assert second.result == first.result
    assert second.canonical_result == first.canonical_result
    assert second.replayed is True
    assert len(probe.deadlines) == 1
    assert isinstance(probe.deadlines[0], MonotonicDeadline)
    assert probe.deadlines[0].wall_deadline == request.deadline


def test_terminal_result_replays_after_deadline_without_local_execution(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = RecordingProbe()
    operation_context = OperationContext(
        node_id=NODE_ID,
        state=AgentStateStore(tmp_path / "state"),
        probe=probe,
    )
    request = claim()
    first = OperationRegistry().execute(request, operation_context)
    _reject_deadline_binding(monkeypatch)

    replay = OperationRegistry().execute(request, operation_context)

    assert replay.result == first.result
    assert replay.replayed is True
    assert len(probe.deadlines) == 1

def test_acknowledged_terminal_result_inspects_and_replays_after_restart_and_expiry(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    probe = RecordingProbe()
    request = claim()
    first_context = OperationContext(
        node_id=NODE_ID,
        state=AgentStateStore(state_root),
        probe=probe,
    )
    first = OperationRegistry().execute(request, first_context)
    first_context.state.acknowledge(first.result)
    _reject_deadline_binding(monkeypatch)
    restarted = OperationContext(
        node_id=NODE_ID,
        state=AgentStateStore(state_root),
        probe=NeverProbe(),
    )

    inspection = OperationRegistry().inspect(request, restarted)
    replay = OperationRegistry().execute(request, restarted)

    assert inspection.disposition is InspectionDisposition.COMPLETED
    assert inspection.result == first.result
    assert inspection.canonical_result == first.canonical_result
    assert replay.result == first.result
    assert replay.canonical_result == first.canonical_result
    assert replay.replayed is True
    assert len(probe.deadlines) == 1

    changed = claim(deadline=datetime.now(UTC) + timedelta(minutes=1))
    with pytest.raises(AgentStateConflict):
        OperationRegistry().inspect(changed, restarted)
    with pytest.raises(AgentStateConflict):
        OperationRegistry().execute(changed, restarted)


def test_inspection_is_read_only_for_interrupted_and_completed_probe(tmp_path) -> None:
    request = claim()
    probe = RecordingProbe()
    store = AgentStateStore(tmp_path / "state")
    store.begin(request)
    operation_context = OperationContext(node_id=NODE_ID, state=store, probe=probe)
    registry = OperationRegistry()

    interrupted = registry.inspect(request, operation_context)
    assert interrupted.disposition is InspectionDisposition.SAFE_TO_RETRY
    assert interrupted.result is None
    assert probe.deadlines == []

    execution = registry.execute(request, operation_context)
    completed = registry.inspect(request, operation_context)
    assert completed.disposition is InspectionDisposition.COMPLETED
    assert completed.result == execution.result
    assert completed.canonical_result == execution.canonical_result
    assert len(probe.deadlines) == 1


def test_inspection_and_execution_reject_a_different_unresolved_claim(tmp_path) -> None:
    store = AgentStateStore(tmp_path / "state")
    store.begin(claim())
    other = claim(
        job_id="33333333-3333-4333-8333-333333333333",
        operation_id="44444444-4444-4444-8444-444444444444",
        fence="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    )
    probe = RecordingProbe()
    operation_context = OperationContext(node_id=NODE_ID, state=store, probe=probe)

    with pytest.raises(AgentStateConflict):
        OperationRegistry().inspect(other, operation_context)
    with pytest.raises(AgentStateConflict):
        OperationRegistry().execute(other, operation_context)
    assert probe.deadlines == []


def test_probe_exception_persists_only_stable_redacted_failure(tmp_path) -> None:
    sentinel = "secret-token-in-/tmp/private.log"
    probe = RecordingProbe(error=RuntimeError(sentinel))
    operation_context = OperationContext(
        node_id=NODE_ID,
        state=AgentStateStore(tmp_path / "state"),
        probe=probe,
    )

    execution = OperationRegistry().execute(claim(), operation_context)

    assert execution.result.state == "failed"
    assert execution.result.result == {
        "status": "failed",
        "error_code": "probe_failed",
    }
    assert sentinel.encode() not in execution.canonical_result
    assert len(probe.deadlines) == 1


def test_unrecognized_exception_error_code_cannot_enter_persisted_result(tmp_path) -> None:
    class MaliciousError(RuntimeError):
        error_code = "secret_selected_error_code"

    operation_context = OperationContext(
        node_id=NODE_ID,
        state=AgentStateStore(tmp_path / "state"),
        probe=RecordingProbe(error=MaliciousError("raw secret /tmp/value")),
    )

    execution = OperationRegistry().execute(claim(), operation_context)

    assert execution.result.result == {"status": "failed", "error_code": "probe_failed"}
    assert b"secret" not in execution.canonical_result


@pytest.mark.parametrize(
    ("operation", "payload", "expected_code"),
    [
        (
            AgentOperation.RELEASE_INSTALL,
            {
                "schema_version": 1,
                "target_name": "node-runtime-2026-08",
                "oci_manifest_digest": "sha256:" + "1" * 64,
                "target_digest": "2" * 64,
                "provenance_digest": "3" * 64,
                "adapter_id": "node-runtime-v1",
            },
            "release_install_failed",
        ),
        (
            AgentOperation.WORKLOAD_HEALTH,
            {
                "schema_version": 1,
                "workload_id": "deepseek-v4-flash-a",
                "release_digest": "4" * 64,
                "adapter_id": "node-runtime-v1",
            },
            "workload_failed",
        ),
    ],
)
def test_release_and_workload_failures_persist_family_code_and_replay_exactly(
    tmp_path, operation, payload, expected_code
) -> None:
    sentinel = "registry-secret-/tmp/output"

    class FailingRelease(RecordingReleaseInstaller):
        def install(self, request, deadline):
            raise RuntimeError(sentinel)

    class FailingWorkloads(RecordingWorkloads):
        def execute(self, request, deadline, job_id, operation_id, attempt, fence):
            raise RuntimeError(sentinel)

    operation_context = OperationContext(
        NODE_ID,
        AgentStateStore(tmp_path / "state"),
        NeverProbe(),
        FailingRelease(),
        FailingWorkloads(),
    )
    request = claim(operation=operation, payload=payload)
    first = OperationRegistry().execute(request, operation_context)
    replay = OperationRegistry().execute(request, operation_context)

    assert first.result.result == {"status": "failed", "error_code": expected_code}
    assert sentinel.encode() not in first.canonical_result
    assert replay.canonical_result == first.canonical_result
    assert replay.replayed is True


def test_expired_active_mutation_can_inspect_complete_or_persist_deadline_failure(tmp_path) -> None:
    payload = {
        "schema_version": 1,
        "workload_id": "deepseek-v4-flash-a",
        "release_digest": "4" * 64,
        "adapter_id": "node-runtime-v1",
        "preparation_digest": "6" * 64,
    }
    active = claim(
        operation=AgentOperation.WORKLOAD_START,
        payload=payload,
        deadline=datetime.now(UTC) + timedelta(milliseconds=30),
    )
    state = AgentStateStore(tmp_path / "completed-state")
    state.begin(active)
    evidence = WorkloadEvidence(
        "inspected", WorkloadAction.START, "deepseek-v4-flash-a",
        "4" * 64, "8" * 64,
    )
    workloads = RecordingWorkloads(
        WorkloadInspection(WorkloadDisposition.COMPLETED, evidence)
    )
    operation_context = OperationContext(
        NODE_ID, state, NeverProbe(), RecordingReleaseInstaller(), workloads
    )
    time.sleep(0.04)
    assert OperationRegistry().inspect(
        active, operation_context
    ).disposition is InspectionDisposition.COMPLETED
    recovered = OperationRegistry().execute(active, operation_context)
    assert recovered.result.state == "succeeded"

    retry_claim = claim(
        operation=AgentOperation.WORKLOAD_START,
        payload=payload,
        deadline=datetime.now(UTC) + timedelta(milliseconds=30),
        job_id="55555555-5555-4555-8555-555555555555",
        operation_id="66666666-6666-4666-8666-666666666666",
        fence="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    )
    retry_state = AgentStateStore(tmp_path / "retry-state")
    retry_state.begin(retry_claim)
    retry_workloads = RecordingWorkloads(
        WorkloadInspection(WorkloadDisposition.SAFE_TO_RETRY)
    )
    retry_context = OperationContext(
        NODE_ID, retry_state, NeverProbe(), RecordingReleaseInstaller(),
        retry_workloads,
    )
    time.sleep(0.04)
    assert OperationRegistry().inspect(
        retry_claim, retry_context
    ).disposition is InspectionDisposition.SAFE_TO_RETRY
    failed = OperationRegistry().execute(retry_claim, retry_context)
    assert failed.result.state == "failed"
    assert failed.result.fence == retry_claim.fence
    assert failed.result.result == {
        "status": "failed",
        "error_code": "claim_deadline_expired",
    }
    assert all(item[0] == "inspect" for item in retry_workloads.requests)
