import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control.agent_jobs import AgentJobService
from vonk_control.agent_upgrades import AgentUpgradeConflict, AgentUpgradeService
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Base,
    Job,
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


def test_legacy_helper_bridge_failure_gets_exactly_one_bounded_retry(
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
        assert deadline == clock() + timedelta(seconds=10)
        assert stored is not None and stored.state == "waiting-for-operator"

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
    clock.advance(seconds=9)
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
        attempts = list(
            session.scalars(
                select(AgentOperationAttempt)
                .where(AgentOperationAttempt.operation_id == operation.id)
                .order_by(AgentOperationAttempt.attempt)
            )
        )
        assert [attempt.state for attempt in attempts] == ["failed", "failed"]
        assert [attempt.result for attempt in attempts] == [
            {"reason": "agent upgrade request is invalid"},
            {"reason": "agent upgrade request is invalid"},
        ]
    assert _operation_nodes(sessions, job.id) == [NODE_A]


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
    clock.advance(seconds=10)
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

    # The retry may race a delayed helper restart and report helper unavailable.
    # It is not claimable a third time, but the restarted agent's exact
    # authenticated identity can still reconcile it and make Spark B runnable.
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

    clock.advance(seconds=10)
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
