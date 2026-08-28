import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control.agent_jobs import AgentJobService
from vonk_control.agent_upgrades import (
    _AGENT_UPGRADE_RECOVERY_FENCE,
    AgentUpgradeConflict,
    AgentUpgradeService,
)
from vonk_control.jobs import AttemptFence, JobService, StaleAttempt
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Base,
    Job,
    JobAttempt,
)

NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32
REVISION = "c" * 64
PACKAGE = {
    "architecture": "linux-arm64",
    "package_bytes": 5_000_000,
    "package_sha256": "d" * 64,
    "package_signature": "e" * 128,
    "package_url": (
        "https://install.vonkforge.ai/dev/releases/example/"
        "spark/current/linux-arm64/vonk-forge-agent.deb"
    ),
    "package_version": "0.1.0~dev.330+g0123456789ab",
    "schema_version": 1,
    "target_binary_digest": "a" * 64,
    "target_build_digest": "sha256:" + "b" * 64,
}
OLD_IDENTITY = {
    "architecture": "linux-arm64",
    "binary_digest": "f" * 64,
    "build_digest": "sha256:" + "f" * 64,
    "semantic_version": "0.1.0",
    "self_test_passed": True,
}
NEW_IDENTITY = {
    **OLD_IDENTITY,
    "binary_digest": PACKAGE["target_binary_digest"],
    "build_digest": PACKAGE["target_build_digest"],
}


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 27, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def test_rollout_queues_only_one_spark_until_new_identity_is_proven(tmp_path) -> None:
    now = datetime(2026, 8, 27, tzinfo=UTC)
    engine = create_engine(f"sqlite:///{tmp_path / 'upgrades.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        for node_id, serial in ((NODE_A, "serial-a"), (NODE_B, "serial-b")):
            session.add(
                AgentNode(
                    node_id=node_id,
                    state="active",
                    capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
                    architecture="linux-arm64",
                    semantic_version="0.1.0",
                    build_digest=OLD_IDENTITY["build_digest"],
                    binary_digest=OLD_IDENTITY["binary_digest"],
                    self_test_passed=True,
                    last_seen_at=now,
                )
            )
            session.add(
                AgentCertificate(
                    serial=serial,
                    node_id=node_id,
                    not_before=now - timedelta(minutes=1),
                    not_after=now + timedelta(hours=1),
                    fingerprint=f"fingerprint-{serial}",
                )
            )
    operations = AgentJobService(sessions, clock=lambda: now)
    upgrades = AgentUpgradeService(
        sessions,
        operations,
        clock=lambda: now,
        current_revision=lambda: REVISION,
    )
    operations.set_result_consumer(upgrades.consume_agent_result)
    plan = upgrades.preview(None, PACKAGE)
    assert plan.node_ids == (NODE_A, NODE_B)
    job = upgrades.apply(
        None,
        PACKAGE,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )

    assert _operation_nodes(sessions, job.id) == [NODE_A]
    _upgrade_node(operations, NODE_A, "serial-a")
    assert set(_operation_nodes(sessions, job.id)) == {NODE_A, NODE_B}
    with sessions() as session:
        stored = session.get(Job, job.id)
        assert stored is not None and stored.state == "queued"

    _upgrade_node(operations, NODE_B, "serial-b")
    with sessions() as session:
        stored = session.get(Job, job.id)
        assert stored is not None and stored.state == "succeeded"


def test_active_legacy_helper_bridge_blocks_retry_until_full_budget(
    tmp_path,
) -> None:
    clock = Clock()
    sessions, operations, _upgrades, job = _rollout(
        tmp_path, "bounded-retry", clock=clock
    )

    first = _claim_upgrade(operations, NODE_A, "serial-a", OLD_IDENTITY)
    operations.fail(first, "agent upgrade request is invalid")
    with sessions() as session:
        operation = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
        )
        assert operation is not None
        assert operation.state == "waiting-for-operator"
        assert operation.retry_disposition == "retry"
        assert operation.retry_disposition_attempt == 1
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == 1,
            )
        )
        stored = session.get(Job, job.id)
        assert attempt is not None
        deadline = attempt.lease_deadline
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        assert deadline == clock() + timedelta(seconds=240)
        assert stored is not None and stored.state == "waiting-for-operator"
        assert stored.status_reason is not None
        assert "after 1 install attempt" in stored.status_reason
        assert "target agent 0.1.0~dev.330+g0123456789ab" in stored.status_reason
        assert "controller-managed retry" in stored.status_reason

    # The durable not-before gate survives repeated polls without consuming the
    # sole retry or changing the waiting parent state.
    assert (
        operations.claim(
            NODE_A,
            "serial-a",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=OLD_IDENTITY,
        )
        is None
    )
    clock.advance(seconds=239)
    assert (
        operations.claim(
            NODE_A,
            "serial-a",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=OLD_IDENTITY,
        )
        is None
    )
    with sessions() as session:
        operation = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
        )
        stored = session.get(Job, job.id)
        assert operation is not None and operation.current_attempt == 1
        assert stored is not None and stored.state == "waiting-for-operator"

    clock.advance(seconds=1)
    second = _claim_upgrade(operations, NODE_A, "serial-a", OLD_IDENTITY)
    assert second.attempt == 2
    operations.fail(second, "agent upgrade request is invalid")

    assert (
        operations.claim(
            NODE_A,
            "serial-a",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=OLD_IDENTITY,
        )
        is None
    )
    with sessions() as session:
        operation = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
        )
        stored = session.get(Job, job.id)
        assert operation is not None and operation.current_attempt == 2
        assert operation.state == "waiting-for-operator"
        assert operation.retry_disposition is None
        assert operation.retry_disposition_attempt is None
        assert stored is not None and stored.state == "waiting-for-operator"
        assert stored.status_reason is not None
        assert "after 2 install attempts" in stored.status_reason
        assert "generic failure" in stored.status_reason
        assert "does not establish an authorization or download failure" in (
            stored.status_reason
        )
        assert "Keep the rollout paused" in stored.status_reason
        assert "does not dispatch immediately" in stored.status_reason
        attempts = list(
            session.scalars(
                select(AgentOperationAttempt)
                .where(AgentOperationAttempt.operation_id == operation.id)
                .order_by(AgentOperationAttempt.attempt)
            )
        )
        assert [attempt.state for attempt in attempts] == ["failed", "failed"]
        second_deadline = attempts[1].lease_deadline
        if second_deadline.tzinfo is None:
            second_deadline = second_deadline.replace(tzinfo=UTC)
        assert second_deadline == clock() + timedelta(seconds=240)
        assert [attempt.result for attempt in attempts] == [
            {"reason": "agent upgrade request is invalid"},
            {"reason": "agent upgrade request is invalid"},
        ]
    assert _operation_nodes(sessions, job.id) == [NODE_A]


def test_controller_recovery_fence_survives_restart_and_bounds_one_retry(
    tmp_path,
) -> None:
    clock = Clock()
    sessions, operations, _upgrades, job = _rollout(
        tmp_path, "durable-retry-fence", clock=clock
    )
    first = _claim_upgrade(operations, NODE_A, "serial-a", OLD_IDENTITY)
    operations.fail(first, "agent upgrade request is invalid")

    # This is a controller safety contract, independent of how the package
    # helper durably recovers apt/dpkg state. The not-before value and sole
    # automatic retry are persisted on the operation attempt.
    assert _AGENT_UPGRADE_RECOVERY_FENCE == timedelta(seconds=240)
    with sessions() as session:
        operation = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
        )
        assert operation is not None
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == 1,
            )
        )
        assert attempt is not None
        deadline = attempt.lease_deadline
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        assert deadline == clock() + _AGENT_UPGRADE_RECOVERY_FENCE
        assert operation.retry_disposition == "retry"
        assert operation.retry_disposition_attempt == 1

    restarted_operations = AgentJobService(sessions, clock=clock)
    restarted_upgrades = AgentUpgradeService(
        sessions,
        restarted_operations,
        clock=clock,
        current_revision=lambda: REVISION,
    )
    restarted_operations.set_result_consumer(restarted_upgrades.consume_agent_result)

    assert (
        restarted_operations.claim(
            NODE_A,
            "serial-a",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=OLD_IDENTITY,
        )
        is None
    )
    clock.advance(seconds=239)
    assert (
        restarted_operations.claim(
            NODE_A,
            "serial-a",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=OLD_IDENTITY,
        )
        is None
    )
    clock.advance(seconds=1)
    second = _claim_upgrade(restarted_operations, NODE_A, "serial-a", OLD_IDENTITY)
    assert second.attempt == 2

    restarted_operations.fail(second, "agent upgrade helper is unavailable")
    clock.advance(seconds=240)
    assert (
        restarted_operations.claim(
            NODE_A,
            "serial-a",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=OLD_IDENTITY,
        )
        is None
    )
    with sessions() as session:
        operation = session.get(AgentOperation, second.operation_id)
        assert operation is not None
        assert operation.current_attempt == 2
        assert operation.retry_disposition is None
        assert operation.retry_disposition_attempt is None


def test_same_binary_packaging_only_release_can_use_bridge_retry(tmp_path) -> None:
    clock = Clock()
    sessions, operations, _upgrades, job = _rollout(
        tmp_path, "same-binary-retry", clock=clock
    )
    packaging_only_identity = {
        **OLD_IDENTITY,
        "binary_digest": PACKAGE["target_binary_digest"],
    }
    first = _claim_upgrade(operations, NODE_A, "serial-a", packaging_only_identity)
    operations.fail(first, "agent upgrade request is invalid")

    with sessions() as session:
        operation = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
        )
        assert operation is not None
        assert operation.retry_disposition == "retry"
        assert operation.retry_disposition_attempt == 1


def test_operator_resume_requeues_agent_operation_without_resetting_plan_or_audit(
    tmp_path,
) -> None:
    clock = Clock()
    sessions, operations, upgrades, job = _rollout(
        tmp_path, "operator-resume", clock=clock
    )
    first = _claim_upgrade(operations, NODE_A, "serial-a", OLD_IDENTITY)
    operations.fail(first, "agent upgrade request is invalid")
    clock.advance(seconds=240)
    second = _claim_upgrade(operations, NODE_A, "serial-a", OLD_IDENTITY)
    operations.fail(second, "agent upgrade helper is unavailable")
    # Resume happens after the failed attempt's original fence has expired.
    # It must still establish a fresh full safety interval from this decision.
    clock.advance(seconds=300)

    with sessions() as session:
        before = session.get(Job, job.id)
        operation = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
        )
        assert before is not None and operation is not None
        immutable = (
            before.authority_revision,
            before.payload_digest,
            dict(before.payload),
            list(before.targets),
            before.current_attempt,
            operation.id,
            operation.payload_digest,
            dict(operation.payload),
            operation.current_attempt,
        )
        attempt_audit = list(
            session.execute(
                select(
                    AgentOperationAttempt.attempt,
                    AgentOperationAttempt.state,
                    AgentOperationAttempt.result,
                )
                .where(AgentOperationAttempt.operation_id == operation.id)
                .order_by(AgentOperationAttempt.attempt)
            )
        )

    upgrades.resume(job.id)

    with sessions() as session:
        resumed = session.get(Job, job.id)
        operation = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
        )
        assert resumed is not None and operation is not None
        assert resumed.state == "queued"
        assert resumed.status_reason is None
        assert (
            resumed.authority_revision,
            resumed.payload_digest,
            dict(resumed.payload),
            list(resumed.targets),
            resumed.current_attempt,
            operation.id,
            operation.payload_digest,
            dict(operation.payload),
            operation.current_attempt,
        ) == immutable
        assert operation.state == "waiting-for-operator"
        assert operation.retry_disposition == "retry"
        assert operation.retry_disposition_attempt == 2
        resumed_attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == operation.current_attempt,
            )
        )
        assert resumed_attempt is not None
        resumed_deadline = resumed_attempt.lease_deadline
        if resumed_deadline.tzinfo is None:
            resumed_deadline = resumed_deadline.replace(tzinfo=UTC)
        assert resumed_deadline == clock() + timedelta(seconds=240)
        assert (
            list(
                session.execute(
                    select(
                        AgentOperationAttempt.attempt,
                        AgentOperationAttempt.state,
                        AgentOperationAttempt.result,
                    )
                    .where(AgentOperationAttempt.operation_id == operation.id)
                    .order_by(AgentOperationAttempt.attempt)
                )
            )
            == attempt_audit
        )

    # The generic worker queue must never consume an agent-owned rollout.
    assert JobService(sessions, clock=clock).claim("worker", 30) is None
    assert (
        operations.claim(
            NODE_A,
            "serial-a",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=OLD_IDENTITY,
        )
        is None
    )
    clock.advance(seconds=239)
    assert (
        operations.claim(
            NODE_A,
            "serial-a",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=OLD_IDENTITY,
        )
        is None
    )
    clock.advance(seconds=1)
    third = _claim_upgrade(operations, NODE_A, "serial-a", OLD_IDENTITY)
    assert third.operation_id == second.operation_id
    assert third.attempt == 3


@pytest.mark.parametrize(
    "stored_result",
    [
        None,
        {"error_code": "legacy_helper_error"},
        {"reason": "unlisted legacy helper result"},
        {"status": "upgraded"},
    ],
    ids=["no-reason", "error-code-only", "unlisted-reason", "success-mismatch"],
)
def test_operator_resume_always_sets_fresh_install_safety_fence(
    tmp_path, stored_result
) -> None:
    clock = Clock()
    sessions, operations, upgrades, job = _rollout(
        tmp_path, "operator-resume-any-result", clock=clock
    )
    first = _claim_upgrade(operations, NODE_A, "serial-a", OLD_IDENTITY)
    with sessions.begin() as session:
        parent = session.get(Job, job.id)
        operation = session.get(AgentOperation, first.operation_id)
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == first.operation_id,
                AgentOperationAttempt.attempt == first.attempt,
            )
        )
        assert parent is not None and operation is not None and attempt is not None
        parent.state = "waiting-for-operator"
        parent.status_reason = "runtime identity was not proven"
        operation.state = "waiting-for-operator"
        attempt.state = "waiting-for-operator"
        attempt.result = stored_result
        attempt.lease_deadline = clock()

    upgrades.resume(job.id)

    with sessions() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == first.operation_id,
                AgentOperationAttempt.attempt == first.attempt,
            )
        )
        assert attempt is not None
        deadline = attempt.lease_deadline
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        assert deadline == clock() + timedelta(seconds=240)

    assert (
        operations.claim(
            NODE_A,
            "serial-a",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=OLD_IDENTITY,
        )
        is None
    )
    clock.advance(seconds=239)
    assert (
        operations.claim(
            NODE_A,
            "serial-a",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=OLD_IDENTITY,
        )
        is None
    )
    clock.advance(seconds=1)
    retry = _claim_upgrade(operations, NODE_A, "serial-a", OLD_IDENTITY)
    assert retry.attempt == 2


def test_resume_recovers_legacy_worker_failure_by_exact_identity_without_reinstall(
    tmp_path,
) -> None:
    clock = Clock()
    sessions, operations, upgrades, job = _rollout(
        tmp_path, "legacy-worker-exact", clock=clock
    )
    child = _claim_upgrade(operations, NODE_A, "serial-a", OLD_IDENTITY)
    _record_legacy_worker_dispatch_failure(sessions, job.id, clock())

    upgrades.resume(job.id)
    assert (
        operations.claim(
            NODE_A,
            "serial-a",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=NEW_IDENTITY,
        )
        is None
    )

    with sessions() as session:
        parent = session.get(Job, job.id)
        operation = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == job.id,
                AgentOperation.node_id == NODE_A,
            )
        )
        attempts = list(
            session.scalars(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == operation.id
                )
            )
        )
        worker_attempt = session.scalar(
            select(JobAttempt).where(JobAttempt.job_id == job.id)
        )
        assert parent is not None and parent.state == "queued"
        assert operation is not None and operation.state == "succeeded"
        assert operation.current_attempt == 1
        assert len(attempts) == 1 and attempts[0].fence == child.fence
        assert worker_attempt is not None and worker_attempt.state == "failed"
        assert set(_operation_nodes(sessions, job.id)) == {NODE_A, NODE_B}


def test_resume_quiesces_stale_old_identity_without_duplicate_mutation(
    tmp_path,
) -> None:
    clock = Clock()
    sessions, operations, upgrades, job = _rollout(
        tmp_path, "legacy-worker-stale", clock=clock
    )
    child = _claim_upgrade(operations, NODE_A, "serial-a", OLD_IDENTITY)
    _record_legacy_worker_dispatch_failure(sessions, job.id, clock())

    upgrades.resume(job.id)
    clock.advance(seconds=31)
    assert (
        operations.claim(
            NODE_A,
            "serial-a",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=OLD_IDENTITY,
        )
        is None
    )

    with sessions() as session:
        parent = session.get(Job, job.id)
        operation = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
        )
        attempts = list(
            session.scalars(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == operation.id
                )
            )
        )
        assert parent is not None and parent.state == "waiting-for-operator"
        assert operation is not None and operation.state == "waiting-for-operator"
        assert operation.current_attempt == 1
        assert len(attempts) == 1
        assert attempts[0].fence == child.fence
        assert attempts[0].state == "expired"


def test_resume_rejects_legacy_running_worker_dispatch_before_lease_deadline(
    tmp_path,
) -> None:
    clock = Clock()
    sessions, operations, upgrades, job = _rollout(
        tmp_path, "legacy-worker-not-stale", clock=clock
    )
    child = _claim_upgrade(operations, NODE_A, "serial-a", OLD_IDENTITY)
    _record_legacy_worker_running(sessions, job.id, clock())

    with pytest.raises(ValueError, match="dispatch is still active"):
        upgrades.resume(job.id)

    with sessions() as session:
        parent = session.get(Job, job.id)
        operation = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
        )
        worker_attempt = session.scalar(
            select(JobAttempt).where(JobAttempt.job_id == job.id)
        )
        assert parent is not None and parent.state == "running"
        assert operation is not None and operation.state == "running"
        assert operation.current_attempt == 1
        assert worker_attempt is not None and worker_attempt.state == "running"
        assert child.attempt == 1


def test_resume_recovers_expired_legacy_running_worker_without_duplicate(
    tmp_path,
) -> None:
    clock = Clock()
    sessions, operations, upgrades, job = _rollout(
        tmp_path, "legacy-worker-expired", clock=clock
    )
    child = _claim_upgrade(operations, NODE_A, "serial-a", OLD_IDENTITY)
    _record_legacy_worker_running(sessions, job.id, clock())
    clock.advance(seconds=31)

    upgrades.resume(job.id)

    with sessions() as session:
        parent = session.get(Job, job.id)
        operation = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
        )
        worker_attempt = session.scalar(
            select(JobAttempt).where(JobAttempt.job_id == job.id)
        )
        child_attempts = list(
            session.scalars(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == operation.id
                )
            )
        )
        assert parent is not None and parent.state == "queued"
        assert worker_attempt is not None and worker_attempt.state == "expired"
        assert operation is not None and operation.state == "running"
        assert operation.current_attempt == 1
        assert len(child_attempts) == 1 and child_attempts[0].fence == child.fence

    # An old identity cannot reclaim the uncertain mutation. The existing child
    # is quiesced for another explicit operator decision without attempt 2.
    assert (
        operations.claim(
            NODE_A,
            "serial-a",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=OLD_IDENTITY,
        )
        is None
    )
    with sessions() as session:
        parent = session.get(Job, job.id)
        operation = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
        )
        assert parent is not None and parent.state == "waiting-for-operator"
        assert operation is not None and operation.current_attempt == 1
        assert operation.state == "waiting-for-operator"


def test_waiting_upgrade_resume_rejects_live_legacy_worker_fence(tmp_path) -> None:
    clock = Clock()
    sessions, operations, upgrades, job = _rollout(
        tmp_path, "waiting-live-worker", clock=clock
    )
    child = _claim_upgrade(operations, NODE_A, "serial-a", OLD_IDENTITY)
    worker_fence = _record_legacy_worker_running(sessions, job.id, clock())
    operations.fail(child, "agent upgrade helper is unavailable")

    with pytest.raises(ValueError, match="dispatch is still active"):
        upgrades.resume(job.id)

    with sessions() as session:
        parent = session.get(Job, job.id)
        worker_attempt = session.scalar(
            select(JobAttempt).where(JobAttempt.job_id == job.id)
        )
        operation = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
        )
        assert parent is not None and parent.state == "waiting-for-operator"
        assert worker_attempt is not None and worker_attempt.state == "running"
        assert operation is not None and operation.current_attempt == 1
        assert worker_fence.attempt == 1


def test_waiting_upgrade_resume_expires_worker_fence_without_shortening_helper_fence(
    tmp_path,
) -> None:
    clock = Clock()
    sessions, operations, upgrades, job = _rollout(
        tmp_path, "waiting-expired-worker", clock=clock
    )
    child = _claim_upgrade(operations, NODE_A, "serial-a", OLD_IDENTITY)
    worker_fence = _record_legacy_worker_running(sessions, job.id, clock())
    operations.fail(child, "agent upgrade helper is unavailable")
    clock.advance(seconds=31)

    upgrades.resume(job.id)

    with pytest.raises(StaleAttempt, match="stale"):
        JobService(sessions, clock=clock).fail(
            worker_fence, "unsupported job kind: agent-upgrade"
        )
    assert (
        operations.claim(
            NODE_A,
            "serial-a",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=OLD_IDENTITY,
        )
        is None
    )
    clock.advance(seconds=239)
    assert (
        operations.claim(
            NODE_A,
            "serial-a",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=OLD_IDENTITY,
        )
        is None
    )
    clock.advance(seconds=1)
    retry = _claim_upgrade(operations, NODE_A, "serial-a", OLD_IDENTITY)
    assert retry.attempt == 2
    with sessions() as session:
        worker_attempt = session.scalar(
            select(JobAttempt).where(JobAttempt.job_id == job.id)
        )
        assert worker_attempt is not None and worker_attempt.state == "expired"


def test_resume_rejects_all_at_once_subset_topology(tmp_path) -> None:
    sessions, _operations, upgrades, job = _rollout(
        tmp_path, "topology-all-subset", strategy="all-at-once"
    )
    with sessions.begin() as session:
        parent = session.get(Job, job.id)
        operation_b = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == job.id,
                AgentOperation.node_id == NODE_B,
            )
        )
        assert parent is not None and operation_b is not None
        parent.state = "waiting-for-operator"
        parent.status_reason = "operator review"
        session.delete(operation_b)

    with pytest.raises(ValueError, match="topology"):
        upgrades.resume(job.id)
    with sessions() as session:
        assert session.get(Job, job.id).state == "waiting-for-operator"


def test_resume_rejects_one_at_a_time_non_prefix_topology(tmp_path) -> None:
    sessions, _operations, upgrades, job = _rollout(tmp_path, "topology-non-prefix")
    with sessions.begin() as session:
        parent = session.get(Job, job.id)
        operation = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
        )
        assert parent is not None and operation is not None
        parent.state = "waiting-for-operator"
        parent.status_reason = "operator review"
        operation.node_id = NODE_B

    with pytest.raises(ValueError, match="topology"):
        upgrades.resume(job.id)
    with sessions() as session:
        assert session.get(Job, job.id).state == "waiting-for-operator"


def test_resume_rejects_unsucceeded_earlier_sequential_child(tmp_path) -> None:
    sessions, operations, upgrades, job = _rollout(tmp_path, "topology-earlier-active")
    _upgrade_node(operations, NODE_A, "serial-a")
    with sessions.begin() as session:
        parent = session.get(Job, job.id)
        operation_a = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == job.id,
                AgentOperation.node_id == NODE_A,
            )
        )
        assert parent is not None and operation_a is not None
        parent.state = "waiting-for-operator"
        parent.status_reason = "operator review"
        operation_a.state = "waiting-for-operator"

    with pytest.raises(ValueError, match="topology"):
        upgrades.resume(job.id)
    with sessions() as session:
        assert session.get(Job, job.id).state == "waiting-for-operator"


def test_resume_restores_success_after_late_legacy_failure_of_completed_rollout(
    tmp_path,
) -> None:
    sessions, operations, upgrades, job = _rollout(
        tmp_path, "late-worker-completed", strategy="all-at-once"
    )
    _upgrade_node(operations, NODE_A, "serial-a")
    _upgrade_node(operations, NODE_B, "serial-b")
    _record_legacy_worker_dispatch_failure(
        sessions, job.id, datetime(2026, 8, 27, tzinfo=UTC)
    )

    upgrades.resume(job.id)

    with sessions() as session:
        parent = session.get(Job, job.id)
        child_states = list(
            session.scalars(
                select(AgentOperation.state)
                .where(AgentOperation.parent_job_id == job.id)
                .order_by(AgentOperation.node_id)
            )
        )
        worker_attempt = session.scalar(
            select(JobAttempt).where(JobAttempt.job_id == job.id)
        )
        assert parent is not None and parent.state == "succeeded"
        assert parent.status_reason is None
        assert child_states == ["succeeded", "succeeded"]
        assert worker_attempt is not None and worker_attempt.state == "failed"


def test_resume_continues_succeeded_sequential_prefix_after_late_worker_failure(
    tmp_path,
) -> None:
    clock = Clock()
    sessions, operations, upgrades, job = _rollout(
        tmp_path, "late-worker-prefix", clock=clock
    )
    _claim_upgrade(operations, NODE_A, "serial-a", OLD_IDENTITY)
    with sessions.begin() as session:
        node_b = session.get(AgentNode, NODE_B)
        assert node_b is not None
        node_b.capabilities = ["agent.runtime.rust.v1"]
    assert (
        operations.claim(
            NODE_A,
            "serial-a",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=NEW_IDENTITY,
        )
        is None
    )
    with sessions.begin() as session:
        node_b = session.get(AgentNode, NODE_B)
        assert node_b is not None
        node_b.capabilities = ["agent.runtime.rust.v1", "agent.upgrade.v1"]
        node_b.last_seen_at = clock()
    _record_legacy_worker_dispatch_failure(sessions, job.id, clock())

    upgrades.resume(job.id)

    with sessions() as session:
        parent = session.get(Job, job.id)
        operations_by_node = {
            operation.node_id: operation
            for operation in session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
            )
        }
        assert parent is not None and parent.state == "queued"
        assert set(operations_by_node) == {NODE_A, NODE_B}
        assert operations_by_node[NODE_A].state == "succeeded"
        assert operations_by_node[NODE_B].state == "queued"
        assert operations_by_node[NODE_B].current_attempt == 0


@pytest.mark.parametrize(
    "reported_identity",
    [
        {**NEW_IDENTITY, "build_digest": OLD_IDENTITY["build_digest"]},
        {**NEW_IDENTITY, "binary_digest": OLD_IDENTITY["binary_digest"]},
        {**NEW_IDENTITY, "semantic_version": "0.1.1"},
    ],
    ids=["mismatched-build", "mismatched-binary", "mismatched-semantic"],
)
def test_success_result_cannot_advance_without_exact_fresh_agent_identity(
    tmp_path, reported_identity
) -> None:
    sessions, operations, _upgrades, job = _rollout(tmp_path, "identity-mismatch")
    claim = _claim_upgrade(operations, NODE_A, "serial-a", OLD_IDENTITY)

    # Even an otherwise well-formed success result is not identity evidence.
    operations.succeed(claim, _target_evidence())
    assert (
        operations.claim(
            NODE_A,
            "serial-a",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=reported_identity,
        )
        is None
    )

    assert _operation_nodes(sessions, job.id) == [NODE_A]
    with sessions() as session:
        operation = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
        )
        stored = session.get(Job, job.id)
        assert operation is not None and operation.state == "waiting-for-operator"
        assert stored is not None and stored.state == "waiting-for-operator"


def test_exact_identity_after_legacy_retry_continues_to_second_target(tmp_path) -> None:
    clock = Clock()
    sessions, operations, _upgrades, job = _rollout(
        tmp_path, "retry-continuation", clock=clock
    )
    first = _claim_upgrade(operations, NODE_A, "serial-a", OLD_IDENTITY)
    operations.fail(first, "agent upgrade request is invalid")
    clock.advance(seconds=120)
    assert (
        operations.claim(
            NODE_B,
            "serial-b",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=OLD_IDENTITY,
        )
        is None
    )
    clock.advance(seconds=120)
    second = _claim_upgrade(operations, NODE_A, "serial-a", OLD_IDENTITY)
    assert second.attempt == 2
    operations.fail(second, "agent upgrade helper is unavailable")
    assert (
        operations.claim(
            NODE_A,
            "serial-a",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=OLD_IDENTITY,
        )
        is None
    )

    # A second helper failure is not claimable a third time, but the restarted
    # agent's exact authenticated identity can still reconcile it and make the
    # still-polling Spark B runnable.
    assert (
        operations.claim(
            NODE_A,
            "serial-a",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=NEW_IDENTITY,
        )
        is None
    )
    assert set(_operation_nodes(sessions, job.id)) == {NODE_A, NODE_B}
    with sessions() as session:
        operation_a = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == job.id,
                AgentOperation.node_id == NODE_A,
            )
        )
        assert operation_a is not None and operation_a.state == "succeeded"
        failed_retry = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation_a.id,
                AgentOperationAttempt.attempt == 2,
            )
        )
        assert failed_retry is not None and failed_retry.state == "failed"
        assert failed_retry.result == {"reason": "agent upgrade helper is unavailable"}
    next_claim = _claim_upgrade(operations, NODE_B, "serial-b", OLD_IDENTITY)
    assert next_claim.attempt == 1


def test_queued_exact_target_contact_skips_reinstall_and_completes_rollout(
    tmp_path,
) -> None:
    sessions, operations, _upgrades, job = _rollout(tmp_path, "queued-exact-target")
    _upgrade_node(operations, NODE_A, "serial-a")

    # B reached the published target out of band after preview but before its
    # queued operation was claimed. Its authenticated identity is sufficient;
    # the installer operation must never be dispatched.
    assert (
        operations.claim(
            NODE_B,
            "serial-b",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=NEW_IDENTITY,
        )
        is None
    )

    with sessions() as session:
        operation = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == job.id,
                AgentOperation.node_id == NODE_B,
            )
        )
        stored = session.get(Job, job.id)
        assert operation is not None
        assert operation.state == "succeeded"
        assert operation.current_attempt == 1
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == 1,
            )
        )
        assert attempt is not None and attempt.state == "succeeded"
        assert attempt.result is not None
        assert attempt.result["status"] == "upgraded"
        assert stored is not None and stored.state == "succeeded"


@pytest.mark.parametrize(
    ("drift", "reason"),
    [
        ("revoked", "is not active"),
        ("capability", "does not support controller upgrades"),
    ],
)
def test_sequential_rollout_preserves_first_success_when_next_target_drifted(
    tmp_path, drift: str, reason: str
) -> None:
    clock = Clock()
    sessions, operations, _upgrades, job = _rollout(
        tmp_path, f"next-target-{drift}", clock=clock
    )
    first = _claim_upgrade(operations, NODE_A, "serial-a", OLD_IDENTITY)
    with sessions.begin() as session:
        node_b = session.get(AgentNode, NODE_B)
        assert node_b is not None
        if drift == "revoked":
            node_b.revoked_at = clock()
        else:
            node_b.capabilities = ["agent.runtime.rust.v1"]

    assert (
        operations.claim(
            NODE_A,
            "serial-a",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=NEW_IDENTITY,
        )
        is None
    )

    with sessions() as session:
        operations_for_job = list(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
            )
        )
        stored = session.get(Job, job.id)
        assert len(operations_for_job) == 1
        assert operations_for_job[0].node_id == NODE_A
        assert operations_for_job[0].state == "succeeded"
        assert stored is not None and stored.state == "waiting-for-operator"
        assert stored.status_reason == f"Spark {NODE_B} {reason}"
    assert first.attempt == 1


def test_all_at_once_rollout_dispatches_every_selected_spark(tmp_path) -> None:
    now = datetime(2026, 8, 27, tzinfo=UTC)
    engine = create_engine(f"sqlite:///{tmp_path / 'parallel-upgrades.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        for node_id in (NODE_A, NODE_B):
            session.add(
                AgentNode(
                    node_id=node_id,
                    state="active",
                    capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
                    architecture="linux-arm64",
                    semantic_version="0.1.0",
                    build_digest=OLD_IDENTITY["build_digest"],
                    binary_digest=OLD_IDENTITY["binary_digest"],
                    self_test_passed=True,
                    last_seen_at=now,
                )
            )
    operations = AgentJobService(sessions, clock=lambda: now)
    upgrades = AgentUpgradeService(
        sessions,
        operations,
        clock=lambda: now,
        current_revision=lambda: REVISION,
    )
    plan = upgrades.preview(None, PACKAGE, strategy="all-at-once")

    job = upgrades.apply(
        None,
        PACKAGE,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id=str(uuid.uuid4()),
        strategy="all-at-once",
    )

    assert set(_operation_nodes(sessions, job.id)) == {NODE_A, NODE_B}


def test_all_at_once_bridge_retries_are_delayed_bounded_and_independent(
    tmp_path,
) -> None:
    clock = Clock()
    sessions, operations, _upgrades, job = _rollout(
        tmp_path,
        "all-at-once-recovery",
        clock=clock,
        strategy="all-at-once",
    )
    first_a = _claim_upgrade(operations, NODE_A, "serial-a", OLD_IDENTITY)
    first_b = _claim_upgrade(operations, NODE_B, "serial-b", OLD_IDENTITY)
    operations.fail(first_a, "agent upgrade request is invalid")
    operations.fail(first_b, "agent upgrade request is invalid")

    for node_id, serial in ((NODE_A, "serial-a"), (NODE_B, "serial-b")):
        assert (
            operations.claim(
                node_id,
                serial,
                30,
                capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
                runtime_identity=OLD_IDENTITY,
            )
            is None
        )
    with sessions() as session:
        stored = session.get(Job, job.id)
        attempts = list(
            session.scalars(
                select(AgentOperation.current_attempt).where(
                    AgentOperation.parent_job_id == job.id
                )
            )
        )
        assert stored is not None and stored.state == "waiting-for-operator"
        assert attempts == [1, 1]

    clock.advance(seconds=240)
    second_a = _claim_upgrade(operations, NODE_A, "serial-a", OLD_IDENTITY)
    second_b = _claim_upgrade(operations, NODE_B, "serial-b", OLD_IDENTITY)
    assert second_a.attempt == second_b.attempt == 2

    # Spark A restarts during its retry. Spark B independently reports the
    # helper race and later reconciles without becoming claimable a third time.
    assert (
        operations.claim(
            NODE_A,
            "serial-a",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=NEW_IDENTITY,
        )
        is None
    )
    operations.fail(second_b, "agent upgrade helper is unavailable")
    assert (
        operations.claim(
            NODE_B,
            "serial-b",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=OLD_IDENTITY,
        )
        is None
    )
    assert (
        operations.claim(
            NODE_B,
            "serial-b",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=NEW_IDENTITY,
        )
        is None
    )
    with sessions() as session:
        stored = session.get(Job, job.id)
        assert stored is not None and stored.state == "succeeded"


def test_controller_selection_excludes_offline_sparks_and_individual_preview_explains(
    tmp_path,
) -> None:
    now = datetime(2026, 8, 27, tzinfo=UTC)
    engine = create_engine(f"sqlite:///{tmp_path / 'online-upgrades.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        for node_id, last_seen_at in (
            (NODE_A, now),
            (NODE_B, now - timedelta(minutes=10)),
        ):
            session.add(
                AgentNode(
                    node_id=node_id,
                    state="active",
                    capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
                    architecture="linux-arm64",
                    semantic_version="0.1.0",
                    build_digest=OLD_IDENTITY["build_digest"],
                    binary_digest=OLD_IDENTITY["binary_digest"],
                    self_test_passed=True,
                    last_seen_at=last_seen_at,
                )
            )
    upgrades = AgentUpgradeService(
        sessions,
        AgentJobService(sessions, clock=lambda: now),
        clock=lambda: now,
        current_revision=lambda: REVISION,
    )

    assert upgrades.preview(None, PACKAGE).node_ids == (NODE_A,)
    with pytest.raises(AgentUpgradeConflict, match="is not currently online"):
        upgrades.preview([NODE_B], PACKAGE)


def test_current_candidate_is_derived_from_the_published_arm64_release(
    tmp_path,
) -> None:
    generation = "9" * 64
    package_path = (
        f"artifacts/dev/releases/{generation}/spark/current/"
        "linux-arm64/vonk-forge-agent.deb"
    )
    signature_path = f"{package_path}.host.sig"
    signature_raw = ("e" * 128 + "\n").encode()
    release = {
        "artifacts": {
            "agent-package-linux-arm64": {
                "architecture": "linux-arm64",
                "host_signature": "e" * 128,
                "package_version": PACKAGE["package_version"],
                "path": package_path,
                "sha256": PACKAGE["package_sha256"],
                "size": PACKAGE["package_bytes"],
                "target_binary_digest": PACKAGE["target_binary_digest"],
                "target_build_digest": PACKAGE["target_build_digest"],
            },
            "agent-package-signature-linux-arm64": {
                "path": signature_path,
                "sha256": hashlib.sha256(signature_raw).hexdigest(),
                "size": len(signature_raw),
            },
        },
        "channel": "dev",
        "generation": generation,
    }

    request_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_hosts.append(request.url.host)
        if request.url.path == "/artifacts/dev/current.manifest":
            return httpx.Response(
                200,
                text=(
                    f"generation={generation}\n"
                    f"release_path=artifacts/dev/releases/{generation}/release.json\n"
                ),
            )
        if request.url.path == f"/artifacts/dev/releases/{generation}/release.json":
            return httpx.Response(200, content=json.dumps(release).encode())
        if request.url.path == f"/{signature_path}":
            return httpx.Response(200, content=signature_raw)
        return httpx.Response(404)

    engine = create_engine(f"sqlite:///{tmp_path / 'candidate.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    operations = AgentJobService(
        sessions, clock=lambda: datetime(2026, 8, 27, tzinfo=UTC)
    )
    upgrades = AgentUpgradeService(
        sessions,
        operations,
        clock=lambda: datetime(2026, 8, 27, tzinfo=UTC),
        current_revision=lambda: REVISION,
        release_api_url="http://caddy:8084",
        transport=httpx.MockTransport(handler),
    )

    assert upgrades.current_package() == {
        **PACKAGE,
        "package_url": f"https://install.vonkforge.ai/{package_path}",
    }
    assert request_hosts == ["caddy", "caddy", "caddy"]


def _operation_nodes(sessions, job_id: str) -> list[str]:
    with sessions() as session:
        return list(
            session.scalars(
                select(AgentOperation.node_id)
                .where(AgentOperation.parent_job_id == job_id)
                .order_by(AgentOperation.created_at, AgentOperation.id)
            )
        )


def _record_legacy_worker_dispatch_failure(
    sessions,
    job_id: str,
    now: datetime,
) -> None:
    with sessions.begin() as session:
        parent = session.get(Job, job_id)
        assert parent is not None
        parent.state = "failed"
        parent.status_reason = "unsupported job kind: agent-upgrade"
        parent.current_attempt = 1
        parent.updated_at = now
        session.add(
            JobAttempt(
                job_id=job_id,
                attempt=1,
                fence=str(uuid.uuid4()),
                worker_id="legacy-worker",
                lease_deadline=now + timedelta(seconds=30),
                state="failed",
            )
        )


def _record_legacy_worker_running(
    sessions,
    job_id: str,
    now: datetime,
) -> AttemptFence:
    with sessions.begin() as session:
        parent = session.get(Job, job_id)
        assert parent is not None
        parent.state = "running"
        parent.status_reason = None
        parent.current_attempt = 1
        parent.updated_at = now
        fence = str(uuid.uuid4())
        deadline = now + timedelta(seconds=30)
        session.add(
            JobAttempt(
                job_id=job_id,
                attempt=1,
                fence=fence,
                worker_id="legacy-worker",
                lease_deadline=deadline,
                state="running",
            )
        )
        return AttemptFence(
            job_id=job_id,
            attempt=1,
            fence=fence,
            worker_id="legacy-worker",
            lease_deadline=deadline,
            kind=parent.kind,
            payload=dict(parent.payload),
            authority_revision=parent.authority_revision,
            targets=tuple(parent.targets),
        )


def _rollout(
    tmp_path,
    database_name: str,
    *,
    clock=None,
    strategy: str = "one-at-a-time",
):
    now = datetime(2026, 8, 27, tzinfo=UTC) if clock is None else clock()
    service_clock = (lambda: now) if clock is None else clock
    engine = create_engine(f"sqlite:///{tmp_path / f'{database_name}.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        for node_id, serial in ((NODE_A, "serial-a"), (NODE_B, "serial-b")):
            session.add(
                AgentNode(
                    node_id=node_id,
                    state="active",
                    capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
                    architecture="linux-arm64",
                    semantic_version="0.1.0",
                    build_digest=OLD_IDENTITY["build_digest"],
                    binary_digest=OLD_IDENTITY["binary_digest"],
                    self_test_passed=True,
                    last_seen_at=now,
                )
            )
            session.add(
                AgentCertificate(
                    serial=serial,
                    node_id=node_id,
                    not_before=now - timedelta(minutes=1),
                    not_after=now + timedelta(hours=1),
                    fingerprint=f"fingerprint-{serial}",
                )
            )
    operations = AgentJobService(sessions, clock=service_clock)
    upgrades = AgentUpgradeService(
        sessions,
        operations,
        clock=service_clock,
        current_revision=lambda: REVISION,
    )
    operations.set_result_consumer(upgrades.consume_agent_result)
    plan = upgrades.preview(None, PACKAGE, strategy=strategy)
    job = upgrades.apply(
        None,
        PACKAGE,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id=str(uuid.uuid4()),
        strategy=strategy,
    )
    return sessions, operations, upgrades, job


def _claim_upgrade(
    operations: AgentJobService,
    node_id: str,
    certificate_serial: str,
    runtime_identity: dict[str, object],
):
    claim = operations.claim(
        node_id,
        certificate_serial,
        30,
        capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
        runtime_identity=runtime_identity,
    )
    assert claim is not None
    return claim


def _target_evidence() -> dict[str, object]:
    return {
        "architecture": PACKAGE["architecture"],
        "binary_digest": PACKAGE["target_binary_digest"],
        "build_digest": PACKAGE["target_build_digest"],
        "package_sha256": PACKAGE["package_sha256"],
        "package_version": PACKAGE["package_version"],
        "self_test_passed": True,
        "status": "upgraded",
    }


def _upgrade_node(
    operations: AgentJobService, node_id: str, certificate_serial: str
) -> None:
    claim = operations.claim(
        node_id,
        certificate_serial,
        30,
        capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
        runtime_identity=OLD_IDENTITY,
    )
    assert claim is not None
    assert (
        operations.claim(
            node_id,
            certificate_serial,
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=NEW_IDENTITY,
        )
        is None
    )
