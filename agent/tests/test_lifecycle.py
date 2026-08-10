from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from vonk_agent.client import AgentTransportError, CredentialStore, IssuedCredential
from vonk_agent.config import AgentConfig
from vonk_agent.deadlines import MonotonicDeadline
from vonk_agent.main import (
    Agent,
    ensure_initial_enrollment,
    remove_consumed_enrollment_token,
)
from vonk_agent.operations import OperationContext, OperationRegistry
from vonk_agent.probe import ProbeDeadlineExceeded
from vonk_agent.readiness import ReadinessError, ReadinessReporter
from vonk_agent.releases import (
    ReleaseDisposition,
    ReleaseEvidence,
    ReleaseInspection,
)
from vonk_agent.state import AgentStateStore
from vonk_agent_protocol import (
    AgentClaim,
    AgentDirective,
    AgentOperation,
    AgentProgress,
    canonical_message,
)

NODE_ID = "spk_0123456789abcdef0123456789abcdef"


def probe_claim(*, deadline: datetime | None = None) -> AgentClaim:
    payload: dict[str, object] = {}
    return AgentClaim(
        schema_version=1,
        job_id=str(uuid.uuid4()),
        operation_id=str(uuid.uuid4()),
        attempt=1,
        fence=str(uuid.uuid4()),
        node_id=NODE_ID,
        operation=AgentOperation.NODE_PROBE,
        base_commit="a" * 40,
        payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
        payload=payload,
        deadline=deadline or datetime.now(UTC) + timedelta(minutes=1),
    )


def release_claim(*, deadline: datetime) -> AgentClaim:
    payload: dict[str, object] = {
        "schema_version": 1,
        "target_name": "node-runtime-2026-08",
        "oci_manifest_digest": "sha256:" + "1" * 64,
        "target_digest": "2" * 64,
        "provenance_digest": "3" * 64,
        "adapter_id": "node-runtime-v1",
    }
    return AgentClaim(
        schema_version=1,
        job_id=str(uuid.uuid4()),
        operation_id=str(uuid.uuid4()),
        attempt=1,
        fence=str(uuid.uuid4()),
        node_id=NODE_ID,
        operation=AgentOperation.RELEASE_INSTALL,
        base_commit="a" * 40,
        payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
        payload=payload,
        deadline=deadline,
    )


class FakeControl:
    def __init__(self) -> None:
        self.claims: list[AgentClaim] = []
        self.results: list[dict[str, object]] = []
        self.claim_calls = 0
        self.result_failures = 0

    def queue(self, claim: AgentClaim) -> None:
        self.claims.append(claim)

    def claim(self) -> AgentClaim | None:
        self.claim_calls += 1
        return self.claims.pop(0) if self.claims else None

    def result(self, result) -> None:
        self.results.append(json.loads(canonical_message(result)))
        if self.result_failures:
            self.result_failures -= 1
            raise AgentTransportError("control plane disconnected")


class Probe:
    def collect(self, deadline: datetime) -> dict[str, object]:
        return {"status": "healthy"}


class RenewableDeadlineProbe:
    def __init__(self) -> None:
        self.deadlines: list[MonotonicDeadline | datetime] = []

    def collect(
        self,
        deadline: datetime | MonotonicDeadline,
    ) -> dict[str, object]:
        self.deadlines.append(deadline)
        if type(deadline) is not MonotonicDeadline:
            raise ProbeDeadlineExceeded("probe received stale claim deadline")
        deadline.check()
        return {"status": "healthy"}


class OrderingProbe:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def collect(self, deadline: datetime) -> dict[str, object]:
        self._events.append("execute")
        return {"status": "healthy"}


class BlockingProbe:
    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self.entered = entered
        self.release = release
        self.calls = 0
        self.thread_ids: list[int] = []

    def collect(self, deadline: datetime) -> dict[str, object]:
        self.calls += 1
        self.thread_ids.append(threading.get_ident())
        self.entered.set()
        assert self.release.wait(2), "test did not release blocked operation"
        return {"status": "healthy"}


class BlockingReleaseInstaller:
    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self.entered = entered
        self.release = release
        self.deadlines = []
        self.calls = 0

    def install(self, request, deadline):
        self.calls += 1
        self.deadlines.append(deadline)
        self.entered.set()
        assert self.release.wait(2)
        deadline.check()
        return ReleaseEvidence(
            "installed",
            request.target_digest,
            request.oci_manifest_digest,
            request.adapter_id,
        )

    def inspect(self, request, deadline):
        return ReleaseInspection(ReleaseDisposition.SAFE_TO_RESUME)


class HeartbeatControl(FakeControl):
    def __init__(self, state: AgentStateStore) -> None:
        super().__init__()
        self._state = state
        self._condition = threading.Condition()
        self.heartbeat_requests: list[AgentProgress] = []
        self.heartbeat_responses: list[AgentDirective] = []
        self.heartbeat_thread_ids: list[int] = []

    def heartbeat(self, progress: AgentProgress) -> AgentDirective:
        record = self._state.recover_active() or self._state.recover_pending()
        assert record is not None
        assert record.claim.fence == progress.fence
        response = AgentDirective(
            schema_version=progress.schema_version,
            job_id=progress.job_id,
            operation_id=progress.operation_id,
            attempt=progress.attempt,
            fence=progress.fence,
            node_id=progress.node_id,
            deadline=progress.deadline + timedelta(seconds=30),
            cancel_requested=False,
        )
        with self._condition:
            self.heartbeat_requests.append(progress)
            self.heartbeat_responses.append(response)
            self.heartbeat_thread_ids.append(threading.get_ident())
            self._condition.notify_all()
        return response

    def wait_for_heartbeats(self, count: int) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: len(self.heartbeat_requests) >= count,
                timeout=2,
            )


def _run_and_capture(agent: Agent, errors: list[Exception]) -> None:
    try:
        agent.run_once()
    except Exception as error:  # noqa: BLE001 - asserted by caller
        errors.append(error)


def test_readiness_reporter_requires_complete_environment_and_publishes_exact_marker(
    tmp_path: Path,
) -> None:
    assert ReadinessReporter._from_environment_for_test({}, tmp_path).report() is False
    with pytest.raises(ReadinessError):
        ReadinessReporter._from_environment_for_test(
            {"VONK_AGENT_SUPERVISOR_GENERATION": "2"}, tmp_path
        )
    environment = {
        "CREDENTIALS_DIRECTORY": str(tmp_path),
        "VONK_AGENT_SUPERVISOR_GENERATION": "2",
        "VONK_AGENT_SUPERVISOR_SLOT": "B",
        "VONK_AGENT_SUPERVISOR_SHA256": "a" * 64,
    }
    (tmp_path / "activation-challenge").write_text("b" * 64 + "\n")
    reporter = ReadinessReporter._from_environment_for_test(environment, tmp_path)

    assert reporter.report() is True
    marker = tmp_path / "readiness.json"
    assert marker.read_bytes() == (
        b'{"challenge":"'
        + b"b" * 64
        + b'","generation":2,"pid":'
        + str(os.getpid()).encode("ascii")
        + b',"schema_version":2,"sha256":"'
        + b"a" * 64
        + b'","slot":"B"}\n'
    )
    assert marker.stat().st_mode & 0o777 == 0o600
    before = marker.stat().st_mtime_ns
    assert reporter.report() is False
    assert marker.stat().st_mtime_ns == before


def test_readiness_callback_runs_only_after_authenticated_runtime_exchange(
    tmp_path: Path,
) -> None:
    context = OperationContext(
        node_id=NODE_ID,
        state=AgentStateStore(tmp_path / "state"),
        probe=Probe(),
    )
    calls: list[str] = []
    empty = FakeControl()
    Agent(
        empty,
        OperationRegistry(),
        context,
        on_authenticated_exchange=lambda: calls.append("ready"),
    ).run_once()
    assert calls == ["ready"]

    failing = _SequencedControl([AgentTransportError("offline")])
    with pytest.raises(AgentTransportError):
        Agent(
            failing,
            OperationRegistry(),
            context,
            on_authenticated_exchange=lambda: calls.append("unsafe"),
        ).run_once()
    assert calls == ["ready"]


def test_nonempty_authenticated_claim_reports_readiness_before_operation_execution(
    tmp_path: Path,
) -> None:
    control = FakeControl()
    control.queue(probe_claim())
    events: list[str] = []
    context = OperationContext(
        node_id=NODE_ID,
        state=AgentStateStore(tmp_path / "state"),
        probe=OrderingProbe(events),
    )

    Agent(
        control,
        OperationRegistry(),
        context,
        on_authenticated_exchange=lambda: events.append("ready"),
    ).run_once()

    assert events == ["ready", "execute"]

def test_agent_claims_executes_and_reports_with_same_fence(tmp_path: Path) -> None:
    fake_control = FakeControl()
    claim = probe_claim()
    fake_control.queue(claim)
    state = AgentStateStore(tmp_path / "state")
    context = OperationContext(node_id=NODE_ID, state=state, probe=Probe())
    agent = Agent(fake_control, OperationRegistry(), context)

    agent.run_once()

    assert fake_control.results[0]["fence"] == claim.fence
    assert fake_control.results[0]["state"] == "succeeded"


@pytest.mark.parametrize("recovering", [False, True])
def test_active_operation_sends_periodic_heartbeats_without_moving_execution_thread(
    tmp_path: Path,
    recovering: bool,
) -> None:
    state = AgentStateStore(tmp_path / "state")
    active = probe_claim()
    control = HeartbeatControl(state)
    if recovering:
        state.begin(active)
    else:
        control.queue(active)
    entered = threading.Event()
    release = threading.Event()
    probe = BlockingProbe(entered, release)
    context = OperationContext(node_id=NODE_ID, state=state, probe=probe)
    agent = Agent(
        control,
        OperationRegistry(),
        context,
        heartbeat_interval_seconds=0.01,
    )
    errors: list[Exception] = []
    runner = threading.Thread(
        target=_run_and_capture,
        args=(agent, errors),
        name="test-agent-runner",
    )

    runner.start()
    assert entered.wait(2)
    assert control.wait_for_heartbeats(2)
    persisted = state.recover_active()
    assert persisted is not None and persisted.progress is not None
    assert persisted.progress.deadline == control.heartbeat_requests[-1].deadline
    release.set()
    runner.join(2)

    assert not runner.is_alive()
    assert errors == []
    assert probe.calls == 1
    assert control.claim_calls == (0 if recovering else 1)
    assert len(control.results) == 1
    assert probe.thread_ids == [runner.ident]
    assert all(
        thread_id != runner.ident for thread_id in control.heartbeat_thread_ids
    )
    assert not any(
        thread.name == f"vonk-agent-heartbeat-{active.operation_id[:8]}"
        for thread in threading.enumerate()
    )
    assert control.heartbeat_requests[1].deadline == (
        control.heartbeat_responses[0].deadline
    )
    assert state.recover_active() is None
    assert state.recover_pending() is None


def test_authenticated_heartbeat_extends_deadline_used_by_running_handler(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state")
    initial_deadline = datetime.now(UTC) + timedelta(milliseconds=150)
    active = release_claim(deadline=initial_deadline)
    control = HeartbeatControl(state)
    control.queue(active)
    entered = threading.Event()
    release = threading.Event()
    installer = BlockingReleaseInstaller(entered, release)
    context = OperationContext(
        node_id=NODE_ID,
        state=state,
        probe=Probe(),
        releases=installer,
    )
    agent = Agent(
        control,
        OperationRegistry(),
        context,
        heartbeat_interval_seconds=0.01,
    )
    errors: list[Exception] = []
    runner = threading.Thread(target=_run_and_capture, args=(agent, errors))

    runner.start()
    assert entered.wait(2)
    assert control.wait_for_heartbeats(1)
    remaining = (initial_deadline - datetime.now(UTC)).total_seconds()
    if remaining > 0:
        time.sleep(remaining + 0.02)
    release.set()
    runner.join(2)

    assert not runner.is_alive()
    assert errors == []
    assert installer.calls == 1
    assert installer.deadlines[0].wall_deadline > initial_deadline
    assert control.results[0]["state"] == "succeeded"


def test_recovery_uses_persisted_extended_deadline_and_heartbeat_reports_readiness(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state")
    initial_deadline = datetime.now(UTC) + timedelta(milliseconds=100)
    active = release_claim(deadline=initial_deadline)
    state.begin(active)
    extended = AgentProgress(
        schema_version=active.schema_version,
        job_id=active.job_id,
        operation_id=active.operation_id,
        attempt=active.attempt,
        fence=active.fence,
        node_id=active.node_id,
        deadline=initial_deadline + timedelta(seconds=30),
        progress={"phase": "recovering"},
    )
    state.heartbeat(extended)
    entered = threading.Event()
    release = threading.Event()
    installer = BlockingReleaseInstaller(entered, release)
    control = HeartbeatControl(state)
    ready: list[str] = []
    ready_event = threading.Event()

    def report_ready() -> None:
        ready.append("ready")
        ready_event.set()

    context = OperationContext(
        node_id=NODE_ID,
        state=state,
        probe=Probe(),
        releases=installer,
    )
    agent = Agent(
        control,
        OperationRegistry(),
        context,
        heartbeat_interval_seconds=0.01,
        on_authenticated_exchange=report_ready,
    )
    errors: list[Exception] = []
    runner = threading.Thread(target=_run_and_capture, args=(agent, errors))

    runner.start()
    assert entered.wait(2)
    assert control.wait_for_heartbeats(1)
    assert ready_event.wait(2)
    assert ready == ["ready"]
    release.set()
    runner.join(2)

    assert not runner.is_alive()
    assert errors == []
    assert installer.deadlines[0].wall_deadline >= extended.deadline
    assert len(control.results) == 1


class MutationCompletingRegistry(OperationRegistry):
    def __init__(self, completed: threading.Event) -> None:
        self._completed = completed

    def execute(self, claim, context, **kwargs):
        execution = super().execute(claim, context, **kwargs)
        self._completed.set()
        return execution


class InflightHeartbeatControl(FakeControl):
    def __init__(
        self,
        state: AgentStateStore,
        entered: threading.Event,
        mutation_completed: threading.Event,
        *,
        fail: bool,
    ) -> None:
        super().__init__()
        self._state = state
        self._entered = entered
        self._mutation_completed = mutation_completed
        self._fail = fail

    def heartbeat(self, progress: AgentProgress) -> AgentProgress:
        assert self._state.recover_active() is not None
        self._entered.set()
        assert self._mutation_completed.wait(2)
        if self._fail:
            raise AgentTransportError("heartbeat response lost")
        return AgentProgress(
            schema_version=progress.schema_version,
            job_id=progress.job_id,
            operation_id=progress.operation_id,
            attempt=progress.attempt,
            fence=progress.fence,
            node_id=progress.node_id,
            deadline=progress.deadline + timedelta(seconds=30),
            progress=progress.progress,
        )


class StuckHeartbeatControl(FakeControl):
    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self._entered = entered
        self._release = release

    def heartbeat(self, progress: AgentProgress) -> AgentProgress:
        self._entered.set()
        assert self._release.wait(2)
        return AgentProgress(
            schema_version=progress.schema_version,
            job_id=progress.job_id,
            operation_id=progress.operation_id,
            attempt=progress.attempt,
            fence=progress.fence,
            node_id=progress.node_id,
            deadline=progress.deadline + timedelta(seconds=30),
            progress=progress.progress,
        )


def test_join_timeout_is_fatal_and_cannot_enter_retry_or_submit_result(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state")
    heartbeat_entered = threading.Event()
    heartbeat_release = threading.Event()
    probe_release = threading.Event()
    probe = BlockingProbe(threading.Event(), probe_release)
    control = StuckHeartbeatControl(heartbeat_entered, heartbeat_release)
    control.queue(probe_claim())
    context = OperationContext(node_id=NODE_ID, state=state, probe=probe)
    agent = Agent(
        control,
        OperationRegistry(),
        context,
        heartbeat_interval_seconds=0.01,
        heartbeat_join_seconds=0.01,
    )
    releaser = threading.Thread(
        target=lambda: (heartbeat_entered.wait(2), probe_release.set())
    )
    releaser.start()

    with pytest.raises(RuntimeError, match="heartbeat worker did not stop") as raised:
        agent.run_once()

    assert not isinstance(raised.value, AgentTransportError)
    pending = state.recover_pending()
    assert pending is not None and pending.result is not None
    assert control.results == []
    with pytest.raises(RuntimeError, match="heartbeat worker did not stop"):
        agent.run_once()
    assert control.claim_calls == 1
    assert control.results == []
    heartbeat_release.set()
    releaser.join(2)


def test_inflight_heartbeat_response_race_does_not_repeat_completed_mutation(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state")
    heartbeat_entered = threading.Event()
    mutation_completed = threading.Event()
    release = threading.Event()
    probe = BlockingProbe(threading.Event(), release)
    control = InflightHeartbeatControl(
        state,
        heartbeat_entered,
        mutation_completed,
        fail=False,
    )
    control.queue(probe_claim())
    context = OperationContext(node_id=NODE_ID, state=state, probe=probe)
    agent = Agent(
        control,
        MutationCompletingRegistry(mutation_completed),
        context,
        heartbeat_interval_seconds=0.01,
    )
    errors: list[Exception] = []
    runner = threading.Thread(target=_run_and_capture, args=(agent, errors))

    runner.start()
    assert heartbeat_entered.wait(2)
    release.set()
    runner.join(2)

    assert not runner.is_alive()
    assert errors == []
    assert probe.calls == 1
    assert len(control.results) == 1
    assert state.recover_pending() is None


def test_heartbeat_transport_failure_after_durable_mutation_leaves_exact_result(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state")
    heartbeat_entered = threading.Event()
    mutation_completed = threading.Event()
    release = threading.Event()
    probe = BlockingProbe(threading.Event(), release)
    disconnected = InflightHeartbeatControl(
        state,
        heartbeat_entered,
        mutation_completed,
        fail=True,
    )
    original = probe_claim()
    disconnected.queue(original)
    context = OperationContext(node_id=NODE_ID, state=state, probe=probe)
    agent = Agent(
        disconnected,
        MutationCompletingRegistry(mutation_completed),
        context,
        heartbeat_interval_seconds=0.01,
    )
    releaser = threading.Thread(
        target=lambda: (heartbeat_entered.wait(2), release.set())
    )
    releaser.start()

    with pytest.raises(AgentTransportError, match="heartbeat response lost"):
        agent.run_once()
    releaser.join(2)

    pending = state.recover_pending()
    assert pending is not None and pending.result is not None
    assert pending.result.fence == original.fence
    assert disconnected.results == []
    restarted = FakeControl()
    Agent(restarted, OperationRegistry(), context).run_once()

    assert probe.calls == 1
    assert len(restarted.results) == 1
    assert restarted.results[0]["fence"] == original.fence
    assert state.recover_pending() is None


@pytest.mark.parametrize("interval", [0, -1, 10.001, 30, 31])
def test_heartbeat_interval_must_precede_control_lease(
    tmp_path: Path,
    interval: float,
) -> None:
    context = OperationContext(
        node_id=NODE_ID,
        state=AgentStateStore(tmp_path / "state"),
        probe=Probe(),
    )

    with pytest.raises(ValueError, match="heartbeat interval"):
        Agent(
            FakeControl(),
            OperationRegistry(),
            context,
            heartbeat_interval_seconds=interval,
        )


def test_pending_result_is_replayed_before_any_new_claim(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state")
    context = OperationContext(node_id=NODE_ID, state=state, probe=Probe())
    disconnected = FakeControl()
    disconnected.result_failures = 1
    original = probe_claim()
    disconnected.queue(original)

    with pytest.raises(AgentTransportError):
        Agent(disconnected, OperationRegistry(), context).run_once()

    pending = state.recover_pending()
    assert pending is not None and pending.result is not None
    restarted = FakeControl()
    restarted.queue(probe_claim())
    Agent(restarted, OperationRegistry(), context).run_once()

    assert restarted.claim_calls == 0
    assert restarted.results[0]["fence"] == original.fence
    assert state.recover_pending() is None


def test_active_attempt_is_recovered_and_executed_before_any_new_claim(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state")
    active = probe_claim()
    state.begin(active)
    context = OperationContext(node_id=NODE_ID, state=state, probe=Probe())
    restarted = FakeControl()
    restarted.queue(probe_claim())

    Agent(restarted, OperationRegistry(), context).run_once()

    assert restarted.claim_calls == 0
    assert restarted.results[0]["fence"] == active.fence
    assert state.recover_active() is None
    assert state.recover_pending() is None


def test_recovered_probe_uses_latest_persisted_lease_once_without_duplicate(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state")
    active = probe_claim(deadline=datetime.now(UTC) - timedelta(seconds=1))
    state.begin(active)
    extended_deadline = datetime.now(UTC) + timedelta(seconds=30)
    state.heartbeat(
        AgentProgress(
            schema_version=active.schema_version,
            job_id=active.job_id,
            operation_id=active.operation_id,
            attempt=active.attempt,
            fence=active.fence,
            node_id=active.node_id,
            deadline=extended_deadline,
            progress={"phase": "recovering"},
        )
    )
    probe = RenewableDeadlineProbe()
    control = FakeControl()
    context = OperationContext(node_id=NODE_ID, state=state, probe=probe)
    agent = Agent(control, OperationRegistry(), context)

    agent.run_once()

    assert control.claim_calls == 0
    assert len(control.results) == 1
    assert control.results[0]["state"] == "succeeded"
    assert len(probe.deadlines) == 1
    received = probe.deadlines[0]
    assert isinstance(received, MonotonicDeadline)
    assert received.wall_deadline == extended_deadline
    assert state.recover_active() is None
    assert state.recover_pending() is None

    agent.run_once()

    assert control.claim_calls == 1
    assert len(control.results) == 1
    assert len(probe.deadlines) == 1


def test_expired_active_attempt_persists_and_replays_exact_failure_before_new_claim(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state")
    expired = probe_claim(deadline=datetime.now(UTC) - timedelta(seconds=1))
    state.begin(expired)
    context = OperationContext(node_id=NODE_ID, state=state, probe=Probe())
    disconnected = FakeControl()
    disconnected.result_failures = 1

    with pytest.raises(AgentTransportError):
        Agent(disconnected, OperationRegistry(), context).run_once()

    pending = state.recover_pending()
    assert pending is not None and pending.result is not None
    assert pending.result.fence == expired.fence
    assert pending.result.deadline == expired.deadline
    assert pending.result.state == "failed"
    assert pending.result.result == {
        "status": "failed",
        "error_code": "claim_deadline_expired",
    }
    assert disconnected.claim_calls == 0

    fresh = probe_claim()
    restarted = FakeControl()
    restarted.queue(fresh)
    agent = Agent(restarted, OperationRegistry(), context)

    agent.run_once()

    assert restarted.claim_calls == 0
    assert restarted.results == [json.loads(pending.canonical_result)]
    assert state.recover_pending() is None

    agent.run_once()

    assert restarted.claim_calls == 1
    assert [result["fence"] for result in restarted.results] == [
        expired.fence,
        fresh.fence,
    ]


class _SequencedControl(FakeControl):
    def __init__(self, outcomes: list[object]) -> None:
        super().__init__()
        self._outcomes = outcomes

    def claim(self) -> AgentClaim | None:
        self.claim_calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]


class _Stop:
    def __init__(self, waits_before_stop: int) -> None:
        self.waits_before_stop = waits_before_stop
        self.delays: list[float] = []

    def is_set(self) -> bool:
        return len(self.delays) >= self.waits_before_stop

    def wait(self, delay: float) -> bool:
        self.delays.append(delay)
        return self.is_set()


def test_run_forever_applies_bounded_jitter_and_resets_after_success(
    tmp_path: Path,
) -> None:
    control = _SequencedControl(
        [
            AgentTransportError("first outage"),
            None,
            AgentTransportError("second outage"),
        ]
    )
    stop = _Stop(waits_before_stop=2)
    context = OperationContext(
        node_id=NODE_ID,
        state=AgentStateStore(tmp_path / "state"),
        probe=Probe(),
    )
    agent = Agent(
        control,
        OperationRegistry(),
        context,
        backoff_min_seconds=1,
        backoff_max_seconds=3,
        jitter=lambda upper: upper,
    )

    agent.run_forever(stop)

    assert stop.delays == [1, 1]


def _credential_material(tmp_path: Path):
    now = datetime.now(UTC)
    ca_key = ed25519.Ed25519PrivateKey.generate()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "rotation-ca")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, algorithm=None)
    )
    key = ed25519.Ed25519PrivateKey.generate()
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, NODE_ID)]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=2))
        .not_valid_after(now + timedelta(minutes=30))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(
                        f"spiffe://vonk-forge.local/node/{NODE_ID}"
                    )
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
        )
        .sign(ca_key, algorithm=None)
    )

    def write(name: str, value: bytes, mode: int) -> Path:
        path = tmp_path / name
        path.write_bytes(value)
        path.chmod(mode)
        return path

    return (
        write("ca.pem", ca.public_bytes(serialization.Encoding.PEM), 0o644),
        write(
            "client.pem", certificate.public_bytes(serialization.Encoding.PEM), 0o644
        ),
        write(
            "client.key",
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
            0o600,
        ),
        ca,
        ca_key,
    )


def _issue_rotation(
    csr_pem: bytes, ca: x509.Certificate, ca_key, *, generation: int = 2
) -> IssuedCredential:
    request = x509.load_pem_x509_csr(csr_pem)
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(request.subject)
        .issuer_name(ca.subject)
        .public_key(request.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            request.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value,
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
        )
        .sign(ca_key, algorithm=None)
    )
    der = certificate.public_bytes(serialization.Encoding.DER)
    return IssuedCredential(
        node_id=NODE_ID,
        certificate_pem=certificate.public_bytes(serialization.Encoding.PEM),
        chain_pem=ca.public_bytes(serialization.Encoding.PEM),
        serial=str(certificate.serial_number),
        fingerprint=hashlib.sha256(der).hexdigest(),
        not_before=certificate.not_valid_before_utc,
        not_after=certificate.not_valid_after_utc,
        generation=generation,
    )


class EnrollmentControl:
    def __init__(self, response) -> None:
        self.response = response
        self.csrs: list[bytes] = []

    def enroll(self, _origin: str, _token: str, csr: bytes, _evidence):
        self.csrs.append(csr)
        return self.response


def test_initial_enrollment_reuses_csr_keeps_token_until_durable_pickup(
    tmp_path: Path,
) -> None:
    ca_path, _certificate_path, _key_path, ca, ca_key = _credential_material(tmp_path)
    state_root = tmp_path / "bootstrap-state"
    token = tmp_path / "enrollment-token"
    token.write_text("t" * 43 + "\n")
    token.chmod(0o600)
    missing_certificate = tmp_path / "initial-missing.pem"
    missing_key = tmp_path / "initial-missing.key"
    config = AgentConfig(
        "https://runtime.example",
        "https://enroll.example",
        NODE_ID,
        missing_certificate,
        missing_key,
        ca_path,
        1,
        60,
        state_root,
        tmp_path / "nvidia.json",
        tmp_path / "runtime.json",
        token,
    )
    store = CredentialStore(state_root, ca_path, missing_certificate, missing_key)
    from vonk_agent.client import EnrollmentPending

    pending_control = EnrollmentControl(
        EnrollmentPending(
            "00000000-0000-0000-0000-000000000001", NODE_ID, "pending-approval"
        )
    )
    evidence = {
        "agent_digest": "a" * 64,
        "boot_id": "boot",
        "csr_public_key_fingerprint": "b" * 64,
        "hardware_fingerprint": "hardware",
        "host_key_fingerprint": "host",
        "node_id": NODE_ID,
    }

    assert ensure_initial_enrollment(config, store, pending_control, evidence) is False
    assert token.exists()
    issued = _issue_rotation(pending_control.csrs[0], ca, ca_key, generation=1)
    issued_control = EnrollmentControl(issued)

    assert ensure_initial_enrollment(config, store, issued_control, evidence) is True
    assert issued_control.csrs == pending_control.csrs
    assert not token.exists()
    assert store.has_active_credentials

    # A crash after active publication but before unlink is recovered safely.
    token.write_text("t" * 43 + "\n")
    token.chmod(0o600)
    assert remove_consumed_enrollment_token(config, store) is True
    assert not token.exists()


class RotationControl(FakeControl):
    def __init__(
        self,
        issued: IssuedCredential,
        *,
        fail_renew: int = 0,
        fail_activate: int = 0,
        fail_claim: bool = False,
    ) -> None:
        super().__init__()
        self.issued = issued
        self.fail_renew = fail_renew
        self.fail_activate = fail_activate
        self.fail_claim = fail_claim
        self.renewed_csrs: list[bytes] = []
        self.activations: list[int] = []

    def renew(self, csr: bytes) -> IssuedCredential:
        self.renewed_csrs.append(csr)
        if self.fail_renew:
            self.fail_renew -= 1
            raise AgentTransportError("renew response lost")
        return self.issued

    def activate(self, generation: int, _credentials) -> None:
        self.activations.append(generation)
        if self.fail_activate:
            self.fail_activate -= 1
            raise AgentTransportError("activation response lost")

    def claim(self):
        if self.fail_claim:
            raise AgentTransportError("claim response lost")
        return super().claim()


def test_rotation_retries_same_csr_after_renew_response_loss_and_resumes_activation_after_restart(
    tmp_path: Path,
) -> None:
    ca_path, certificate_path, key_path, ca, ca_key = _credential_material(tmp_path)
    state_root = tmp_path / "state"
    store = CredentialStore(state_root, ca_path, certificate_path, key_path)
    pending = store.prepare_rotation(NODE_ID)
    issued = _issue_rotation(pending.csr_pem, ca, ca_key)
    first = RotationControl(issued, fail_renew=1)
    context = OperationContext(
        node_id=NODE_ID,
        state=AgentStateStore(state_root),
        probe=Probe(),
    )

    with pytest.raises(AgentTransportError):
        Agent(first, OperationRegistry(), context, credentials=store).run_once()
    assert store.pending_rotation() is not None
    first_csr = first.renewed_csrs[0]

    after_renew_restart = CredentialStore(
        state_root, ca_path, certificate_path, key_path
    )
    second = RotationControl(issued, fail_activate=1)
    failed_readiness: list[str] = []
    with pytest.raises(AgentTransportError):
        Agent(
            second,
            OperationRegistry(),
            context,
            credentials=after_renew_restart,
            on_authenticated_exchange=lambda: failed_readiness.append("ready"),
        ).run_once()
    assert second.renewed_csrs == [first_csr]
    assert after_renew_restart.active_generation == 1
    assert after_renew_restart.staged_generation == 2
    assert failed_readiness == []

    after_activation_restart = CredentialStore(
        state_root, ca_path, certificate_path, key_path
    )
    third = RotationControl(issued, fail_claim=True)
    activated_readiness: list[str] = []
    with pytest.raises(AgentTransportError):
        Agent(
            third,
            OperationRegistry(),
            context,
            credentials=after_activation_restart,
            on_authenticated_exchange=lambda: activated_readiness.append("ready"),
        ).run_once()

    assert third.renewed_csrs == []
    assert third.activations == [2]
    assert after_activation_restart.active_generation == 2
    assert after_activation_restart.staged_generation is None
    assert activated_readiness == ["ready"]


def test_installed_console_entry_point_has_bounded_help_without_loading_credentials(
    tmp_path: Path,
) -> None:
    project = Path(__file__).parents[1]
    result = subprocess.run(
        ["uv", "run", "--project", str(project), "vonk-forge-agent", "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Vonk Forge outbound agent" in result.stdout
