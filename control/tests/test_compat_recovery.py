from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import AgentResult, canonical_message
from vonk_control.agent_jobs import AgentJobService
from vonk_control.agent_upgrades import AgentUpgradeService
from vonk_control.compat_recovery import (
    CONFIRMATION,
    JOB_ID,
    NODE_ID,
    OPERATION_ID,
    RECOVERY_ID,
    SOURCE_ATTEMPT,
    SOURCE_BINARY_DIGEST,
    SOURCE_BUILD_DIGEST,
    SOURCE_SEMANTIC_VERSION,
    TARGET_BINARY_DIGEST,
    TARGET_BUILD_DIGEST,
    TARGET_PACKAGE_SHA256,
    TARGET_PACKAGE_VERSION,
    CompatibilityRecoveryConflict,
    Spark3542CompatibilityRecoveryService,
)
from vonk_control.host_helper_authority import (
    HostHelperAuthorityError,
    HostHelperGrantIssuer,
    HostRuntimeAuthorityService,
)
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    AgentUpgradeCompatibilityRecovery,
    Base,
    Job,
)

NOW = datetime(2026, 8, 29, 15, 0, tzinfo=UTC)
SOURCE_FENCE = "10000000-0000-4000-8000-000000000001"
RETRY_FENCE = "20000000-0000-4000-8000-000000000002"
SOURCE_CERTIFICATE = "dev335-certificate"
PACKAGE_SIGNATURE = "a" * 128
AUTHORITY_REVISION = "b" * 64
PACKAGE = {
    "architecture": "linux-arm64",
    "package_bytes": 5_000_000,
    "package_sha256": TARGET_PACKAGE_SHA256,
    "package_signature": PACKAGE_SIGNATURE,
    "package_url": (
        "https://install.vonkforge.ai/dev/releases/a122/"
        "spark/current/linux-arm64/vonk-forge-agent.deb"
    ),
    "package_version": TARGET_PACKAGE_VERSION,
    "schema_version": 1,
    "target_binary_digest": TARGET_BINARY_DIGEST,
    "target_build_digest": TARGET_BUILD_DIGEST,
}
UPGRADE_PAYLOAD_DIGEST = hashlib.sha256(canonical_message(PACKAGE)).hexdigest()
JOB_PLAN_DIGEST = hashlib.sha256(
    canonical_message(
        {
            "authority_revision": AUTHORITY_REVISION,
            "node_ids": [NODE_ID],
            "package": PACKAGE,
            "strategy": "one-at-a-time",
        }
    )
).hexdigest()


def seeded_services(tmp_path, *, engine=None):
    engine = engine or create_engine(f"sqlite:///{tmp_path / 'compat.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=NODE_ID,
                state="active",
                protocol_version=3,
                architecture="linux-arm64",
                semantic_version=SOURCE_SEMANTIC_VERSION,
                build_digest=SOURCE_BUILD_DIGEST,
                binary_digest=SOURCE_BINARY_DIGEST,
                self_test_passed=True,
                capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
                last_seen_at=NOW,
                contact_certificate_serial=SOURCE_CERTIFICATE,
            )
        )
        session.flush()
        session.add(
            AgentCertificate(
                serial=SOURCE_CERTIFICATE,
                node_id=NODE_ID,
                not_before=NOW - timedelta(days=1),
                not_after=NOW + timedelta(days=1),
                fingerprint="source-fingerprint",
            )
        )
        session.flush()
        session.add(
            Job(
                id=JOB_ID,
                request_id=str(uuid.uuid4()),
                kind="agent-upgrade",
                state="waiting-for-operator",
                actor="admin",
                authority_revision=AUTHORITY_REVISION,
                targets=[NODE_ID],
                payload_digest=JOB_PLAN_DIGEST,
                payload={
                    "node_order": [NODE_ID],
                    "package": PACKAGE,
                    "strategy": "one-at-a-time",
                },
                current_attempt=1,
                created_at=NOW - timedelta(hours=1),
                updated_at=NOW - timedelta(minutes=10),
            )
        )
        session.flush()
        session.add(
            AgentOperation(
                id=OPERATION_ID,
                parent_job_id=JOB_ID,
                node_id=NODE_ID,
                kind="agent.upgrade.v1",
                payload_digest=UPGRADE_PAYLOAD_DIGEST,
                payload=PACKAGE,
                authority_revision=AUTHORITY_REVISION,
                state="waiting-for-operator",
                current_attempt=SOURCE_ATTEMPT,
                created_at=NOW - timedelta(hours=1),
                updated_at=NOW - timedelta(minutes=10),
            )
        )
        session.flush()
        session.add(
            AgentOperationAttempt(
                operation_id=OPERATION_ID,
                attempt=SOURCE_ATTEMPT,
                fence=SOURCE_FENCE,
                lease_deadline=NOW - timedelta(minutes=5),
                agent_certificate_serial=SOURCE_CERTIFICATE,
                state="failed",
                result={"reason": "agent upgrade request is invalid"},
            )
        )
    notifications: list[bool] = []
    service = Spark3542CompatibilityRecoveryService(
        sessions,
        clock=lambda: NOW,
        notify_available=lambda: notifications.append(True),
    )
    return sessions, service, notifications


def test_postgres_accepts_awaiting_identity_and_enforces_grant_shape(
    tmp_path, postgres_engine
):
    sessions, service, _ = seeded_services(tmp_path, engine=postgres_engine)
    plan = service.preview()
    service.apply(
        plan_digest=plan.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    with sessions.begin() as session:
        operation = session.get(AgentOperation, OPERATION_ID)
        assert operation is not None
        operation.state = "running"
        operation.current_attempt = 4
        session.add(
            AgentOperationAttempt(
                operation_id=OPERATION_ID,
                attempt=4,
                fence=RETRY_FENCE,
                lease_deadline=NOW + timedelta(seconds=60),
                agent_certificate_serial=SOURCE_CERTIFICATE,
                state="running",
            )
        )

    issuer = HostHelperGrantIssuer(
        ed25519.Ed25519PrivateKey.from_private_bytes(b"m" * 32),
        clock=lambda: NOW,
        request_id_factory=lambda: "30000000-0000-4000-8000-000000000003",
    )
    HostRuntimeAuthorityService(
        sessions, issuer, clock=lambda: NOW
    ).issue_agent_upgrade_grant(
        node_id=NODE_ID,
        job_id=JOB_ID,
        operation_id=OPERATION_ID,
        attempt=4,
        fence=RETRY_FENCE,
        package_sha256=TARGET_PACKAGE_SHA256,
        package_signature=PACKAGE_SIGNATURE,
        certificate_serial=SOURCE_CERTIFICATE,
        expires_in_seconds=30,
    )
    with sessions.begin() as session:
        recovery = session.get(
            AgentUpgradeCompatibilityRecovery,
            "spark3542-a122-exact-package-retry-v1",
        )
        assert recovery is not None
        recovery.state = "awaiting-identity"

    with sessions() as session:
        recovery = session.get(
            AgentUpgradeCompatibilityRecovery,
            "spark3542-a122-exact-package-retry-v1",
        )
        assert recovery is not None and recovery.state == "awaiting-identity"

    with pytest.raises(IntegrityError), sessions.begin() as session:
        recovery = session.get(
            AgentUpgradeCompatibilityRecovery,
            "spark3542-a122-exact-package-retry-v1",
        )
        assert recovery is not None
        recovery.signed_grant = None


def test_preview_and_apply_bind_exact_failed_attempt_and_arm_only_its_retry(tmp_path):
    sessions, service, notifications = seeded_services(tmp_path)

    plan = service.preview()
    assert plan.document["source_fence"] == SOURCE_FENCE
    assert plan.document["expected_retry_attempt"] == 4
    assert plan.document["target"]["package_sha256"] == TARGET_PACKAGE_SHA256
    with pytest.raises(CompatibilityRecoveryConflict, match="confirmation"):
        service.apply(
            plan_digest=plan.plan_digest,
            confirmation="restart",
            actor="admin",
            request_id=str(uuid.uuid4()),
        )

    applied = service.apply(
        plan_digest=plan.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )

    assert applied.state == "armed"
    assert notifications == [True]
    with sessions() as session:
        recovery = session.get(
            AgentUpgradeCompatibilityRecovery,
            applied.document["compatibility_recovery_id"],
        )
        operation = session.get(AgentOperation, OPERATION_ID)
        job = session.get(Job, JOB_ID)
        assert recovery is not None and recovery.state == "armed"
        assert recovery.source_fence == SOURCE_FENCE
        assert recovery.expected_retry_attempt == 4
        assert operation is not None and operation.retry_disposition == "retry"
        assert operation.retry_disposition_attempt == SOURCE_ATTEMPT
        assert job is not None and job.state == "queued"

    replay = service.apply(
        plan_digest=plan.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    assert replay.state == "armed"
    assert notifications == [True]


def test_database_rejects_issued_state_without_one_shot_grant_fields(tmp_path):
    sessions, service, _ = seeded_services(tmp_path)
    plan = service.preview()
    service.apply(
        plan_digest=plan.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )

    with pytest.raises(IntegrityError), sessions.begin() as session:
        recovery = session.get(
            AgentUpgradeCompatibilityRecovery,
            "spark3542-a122-exact-package-retry-v1",
        )
        assert recovery is not None
        recovery.state = "issued"


def test_preview_rejects_identity_drift_and_concurrent_mutation(tmp_path):
    sessions, service, _ = seeded_services(tmp_path)
    plan = service.preview()
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_ID)
        assert node is not None
        node.binary_digest = "f" * 64
    with pytest.raises(CompatibilityRecoveryConflict, match="exact live dev335"):
        service.preview()

    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_ID)
        operation = session.get(AgentOperation, OPERATION_ID)
        assert node is not None
        assert operation is not None
        node.binary_digest = SOURCE_BINARY_DIGEST
        operation.payload = {**operation.payload, "package_signature": "e" * 128}
    with pytest.raises(
        CompatibilityRecoveryConflict, match="payload digest is invalid"
    ):
        service.apply(
            plan_digest=plan.plan_digest,
            confirmation=CONFIRMATION,
            actor="admin",
            request_id=str(uuid.uuid4()),
        )

    with sessions.begin() as session:
        operation = session.get(AgentOperation, OPERATION_ID)
        assert operation is not None
        operation.payload = PACKAGE
        session.add(
            AgentOperation(
                parent_job_id=JOB_ID,
                node_id=NODE_ID,
                kind="release.install",
                payload_digest="e" * 64,
                payload={},
                authority_revision=AUTHORITY_REVISION,
                state="queued",
                current_attempt=0,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    with pytest.raises(CompatibilityRecoveryConflict, match="another queued"):
        service.preview()


def test_grant_is_one_persisted_helper_restart_and_never_an_install(tmp_path):
    sessions, service, _ = seeded_services(tmp_path)
    plan = service.preview()
    service.apply(
        plan_digest=plan.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    with sessions.begin() as session:
        operation = session.get(AgentOperation, OPERATION_ID)
        source = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == OPERATION_ID,
                AgentOperationAttempt.attempt == SOURCE_ATTEMPT,
            )
        )
        assert operation is not None and source is not None
        source.state = "expired"
        operation.state = "running"
        operation.current_attempt = 4
        session.add(
            AgentOperationAttempt(
                operation_id=OPERATION_ID,
                attempt=4,
                fence=RETRY_FENCE,
                lease_deadline=NOW + timedelta(seconds=60),
                agent_certificate_serial=SOURCE_CERTIFICATE,
                state="running",
            )
        )

    issued_ids = iter(
        (
            "30000000-0000-4000-8000-000000000003",
            "40000000-0000-4000-8000-000000000004",
        )
    )
    clock = [NOW]
    issuer = HostHelperGrantIssuer(
        ed25519.Ed25519PrivateKey.from_private_bytes(b"m" * 32),
        clock=lambda: clock[0],
        request_id_factory=lambda: next(issued_ids),
    )
    authority = HostRuntimeAuthorityService(sessions, issuer, clock=lambda: clock[0])
    arguments = {
        "node_id": NODE_ID,
        "job_id": JOB_ID,
        "operation_id": OPERATION_ID,
        "attempt": 4,
        "fence": RETRY_FENCE,
        "package_sha256": TARGET_PACKAGE_SHA256,
        "package_signature": PACKAGE_SIGNATURE,
        "certificate_serial": SOURCE_CERTIFICATE,
        "expires_in_seconds": 30,
    }

    with pytest.raises(HostHelperAuthorityError, match="stale"):
        authority.issue_agent_upgrade_grant(**{**arguments, "expires_in_seconds": 29})
    first = authority.issue_agent_upgrade_grant(**arguments)
    replay = authority.issue_agent_upgrade_grant(**arguments)

    assert first.to_mapping() == replay.to_mapping()
    assert first.claims.operation.to_mapping() == {
        "type": "install-vonk-deb",
        "package_sha256": TARGET_PACKAGE_SHA256,
        "package_signature": PACKAGE_SIGNATURE,
    }
    with sessions() as session:
        recovery = session.get(
            AgentUpgradeCompatibilityRecovery, "spark3542-a122-exact-package-retry-v1"
        )
        assert recovery is not None and recovery.state == "issued"
        assert recovery.retry_fence == RETRY_FENCE
        assert recovery.grant_request_id == first.claims.request_id

    with pytest.raises(HostHelperAuthorityError, match="stale"):
        authority.issue_agent_upgrade_grant(**{**arguments, "fence": SOURCE_FENCE})
    clock[0] = NOW + timedelta(seconds=31)
    with pytest.raises(HostHelperAuthorityError, match="stale"):
        authority.issue_agent_upgrade_grant(**arguments)
    clock[0] = NOW

    operations = AgentJobService(sessions, clock=lambda: NOW)
    upgrades = AgentUpgradeService(
        sessions,
        operations,
        clock=lambda: NOW,
        current_revision=lambda: AUTHORITY_REVISION,
    )
    operations.set_result_consumer(upgrades.consume_agent_result)
    operations.record_result(
        AgentResult(
            schema_version=1,
            job_id=JOB_ID,
            operation_id=OPERATION_ID,
            attempt=4,
            fence=RETRY_FENCE,
            node_id=NODE_ID,
            deadline=NOW + timedelta(seconds=60),
            state="failed",
            result={"reason": "agent upgrade request is invalid"},
        )
    )
    with sessions() as session:
        operation = session.get(AgentOperation, OPERATION_ID)
        recovery = session.get(
            AgentUpgradeCompatibilityRecovery,
            "spark3542-a122-exact-package-retry-v1",
        )
        assert operation is not None and operation.state == "waiting-for-operator"
        assert operation.retry_disposition is None
        assert recovery is not None and recovery.state == "awaiting-identity"

    # A later recipe mutation may queue, but it cannot enter the Spark's
    # mutation lane while the compatibility recovery awaits target identity.
    recipe_job_id = "80000000-0000-4000-8000-000000000008"
    recipe_operation_id = "90000000-0000-4000-8000-000000000009"
    with sessions.begin() as session:
        session.add(
            Job(
                id=recipe_job_id,
                request_id=str(uuid.uuid4()),
                kind="recipe-operation",
                state="queued",
                actor="admin",
                authority_revision=AUTHORITY_REVISION,
                targets=[NODE_ID],
                payload_digest="8" * 64,
                payload={},
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            AgentOperation(
                id=recipe_operation_id,
                parent_job_id=recipe_job_id,
                node_id=NODE_ID,
                kind="recipe.start",
                payload_digest="9" * 64,
                payload={},
                authority_revision=AUTHORITY_REVISION,
                state="queued",
                current_attempt=0,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    assert (
        operations.claim(
            NODE_ID,
            SOURCE_CERTIFICATE,
            30,
            protocol_version=3,
            capabilities=[
                "agent.runtime.rust.v1",
                "agent.upgrade.v1",
                "recipe.start",
            ],
            runtime_identity={
                "architecture": "linux-arm64",
                "semantic_version": SOURCE_SEMANTIC_VERSION,
                "build_digest": SOURCE_BUILD_DIGEST,
                "binary_digest": SOURCE_BINARY_DIGEST,
                "self_test_passed": True,
            },
        )
        is None
    )
    with sessions() as session:
        recipe_operation = session.get(AgentOperation, recipe_operation_id)
        assert recipe_operation is not None and recipe_operation.state == "queued"

    # Only an authenticated contact with the exact a122 identity and required
    # runtime/upgrade capabilities completes the original operation.
    assert (
        operations.claim(
            NODE_ID,
            SOURCE_CERTIFICATE,
            30,
            protocol_version=3,
            capabilities=["agent.runtime.rust.v1"],
            runtime_identity={
                "architecture": "linux-arm64",
                "semantic_version": "0.1.0",
                "build_digest": TARGET_BUILD_DIGEST,
                "binary_digest": TARGET_BINARY_DIGEST,
                "self_test_passed": True,
            },
        )
        is None
    )
    with sessions() as session:
        recovery = session.get(
            AgentUpgradeCompatibilityRecovery,
            "spark3542-a122-exact-package-retry-v1",
        )
        assert recovery is not None and recovery.state == "awaiting-identity"

    assert (
        operations.claim(
            NODE_ID,
            SOURCE_CERTIFICATE,
            30,
            protocol_version=3,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity={
                "architecture": "linux-arm64",
                "semantic_version": "0.1.1",
                "build_digest": TARGET_BUILD_DIGEST,
                "binary_digest": TARGET_BINARY_DIGEST,
                "self_test_passed": True,
            },
        )
        is None
    )
    with sessions() as session:
        recovery = session.get(
            AgentUpgradeCompatibilityRecovery,
            "spark3542-a122-exact-package-retry-v1",
        )
        assert recovery is not None and recovery.state == "awaiting-identity"

    assert (
        operations.claim(
            NODE_ID,
            SOURCE_CERTIFICATE,
            30,
            protocol_version=3,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity={
                "architecture": "linux-arm64",
                "semantic_version": "0.1.0",
                "build_digest": TARGET_BUILD_DIGEST,
                "binary_digest": TARGET_BINARY_DIGEST,
                "self_test_passed": True,
            },
        )
        is None
    )
    with sessions() as session:
        operation = session.get(AgentOperation, OPERATION_ID)
        job = session.get(Job, JOB_ID)
        recovery = session.get(
            AgentUpgradeCompatibilityRecovery,
            "spark3542-a122-exact-package-retry-v1",
        )
        assert operation is not None and operation.state == "succeeded"
        assert job is not None and job.state == "succeeded"
        assert recovery is not None and recovery.state == "completed"
        assert recovery.completed_at is not None
        assert recovery.completed_at.replace(tzinfo=UTC) == NOW


def test_lost_response_or_agent_death_times_out_operator_blocked(tmp_path):
    sessions, service, _ = seeded_services(tmp_path)
    plan = service.preview()
    service.apply(
        plan_digest=plan.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    deadline = NOW + timedelta(seconds=60)
    with sessions.begin() as session:
        operation = session.get(AgentOperation, OPERATION_ID)
        assert operation is not None
        operation.state = "running"
        operation.current_attempt = 4
        session.add(
            AgentOperationAttempt(
                operation_id=OPERATION_ID,
                attempt=4,
                fence=RETRY_FENCE,
                lease_deadline=deadline,
                agent_certificate_serial=SOURCE_CERTIFICATE,
                state="running",
            )
        )

    issuer = HostHelperGrantIssuer(
        ed25519.Ed25519PrivateKey.from_private_bytes(b"m" * 32),
        clock=lambda: NOW,
        request_id_factory=lambda: "30000000-0000-4000-8000-000000000003",
    )
    authority = HostRuntimeAuthorityService(sessions, issuer, clock=lambda: NOW)
    authority.issue_agent_upgrade_grant(
        node_id=NODE_ID,
        job_id=JOB_ID,
        operation_id=OPERATION_ID,
        attempt=4,
        fence=RETRY_FENCE,
        package_sha256=TARGET_PACKAGE_SHA256,
        package_signature=PACKAGE_SIGNATURE,
        certificate_serial=SOURCE_CERTIFICATE,
        expires_in_seconds=30,
    )
    clock = [NOW + timedelta(seconds=601)]
    operations = AgentJobService(sessions, clock=lambda: clock[0])

    # The active systemd recovery owns a 600-second start window. Its durable
    # Controller guard adds margin and keeps the mutation lane closed.
    assert operations.reconcile_compatibility_recoveries() is False
    with sessions() as session:
        recovery = session.get(
            AgentUpgradeCompatibilityRecovery,
            "spark3542-a122-exact-package-retry-v1",
        )
        assert recovery is not None and recovery.state == "issued"
        assert recovery.identity_deadline is not None
        assert recovery.identity_deadline.replace(tzinfo=UTC) == NOW + timedelta(
            minutes=15
        )

    clock[0] = NOW + timedelta(seconds=901)
    assert operations.reconcile_compatibility_recoveries() is True
    assert operations.reconcile_compatibility_recoveries() is False
    with sessions() as session:
        recovery = session.get(
            AgentUpgradeCompatibilityRecovery,
            "spark3542-a122-exact-package-retry-v1",
        )
        operation = session.get(AgentOperation, OPERATION_ID)
        job = session.get(Job, JOB_ID)
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == OPERATION_ID,
                AgentOperationAttempt.attempt == 4,
            )
        )
        assert recovery is not None and recovery.state == "operator-blocked"
        assert recovery.blocked_at is not None
        assert recovery.blocked_at.replace(tzinfo=UTC) == clock[0]
        assert operation is not None and operation.state == "waiting-for-operator"
        assert operation.retry_disposition is None
        assert attempt is not None and attempt.state == "waiting-for-operator"
        assert job is not None and job.state == "waiting-for-operator"
        assert "no replacement grant" in str(job.status_reason)
        issued_grant = dict(recovery.signed_grant or {})
        grant_request_id = recovery.grant_request_id

    blocked_job_id = "81000000-0000-4000-8000-000000000008"
    blocked_operation_id = "91000000-0000-4000-8000-000000000009"
    with sessions.begin() as session:
        session.add(
            Job(
                id=blocked_job_id,
                request_id=str(uuid.uuid4()),
                kind="release-install",
                state="queued",
                actor="admin",
                authority_revision=AUTHORITY_REVISION,
                targets=[NODE_ID],
                payload_digest="7" * 64,
                payload={},
                created_at=clock[0],
                updated_at=clock[0],
            )
        )
        session.add(
            AgentOperation(
                id=blocked_operation_id,
                parent_job_id=blocked_job_id,
                node_id=NODE_ID,
                kind="release.install",
                payload_digest="6" * 64,
                payload={},
                authority_revision=AUTHORITY_REVISION,
                state="queued",
                current_attempt=0,
                created_at=clock[0],
                updated_at=clock[0],
            )
        )
    assert (
        operations.claim(
            NODE_ID,
            SOURCE_CERTIFICATE,
            30,
            protocol_version=3,
            capabilities=["agent.runtime.rust.v1", "release.install"],
            runtime_identity={
                "architecture": "linux-arm64",
                "semantic_version": SOURCE_SEMANTIC_VERSION,
                "build_digest": SOURCE_BUILD_DIGEST,
                "binary_digest": SOURCE_BINARY_DIGEST,
                "self_test_passed": True,
            },
        )
        is None
    )
    with sessions() as session:
        blocked_operation = session.get(AgentOperation, blocked_operation_id)
        assert blocked_operation is not None and blocked_operation.state == "queued"

    assert (
        operations.claim(
            NODE_ID,
            SOURCE_CERTIFICATE,
            30,
            protocol_version=3,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity={
                "architecture": "linux-arm64",
                "semantic_version": "0.1.0",
                "build_digest": TARGET_BUILD_DIGEST,
                "binary_digest": TARGET_BINARY_DIGEST,
                "self_test_passed": True,
            },
        )
        is None
    )
    with sessions() as session:
        recovery = session.get(
            AgentUpgradeCompatibilityRecovery,
            "spark3542-a122-exact-package-retry-v1",
        )
        operation = session.get(AgentOperation, OPERATION_ID)
        job = session.get(Job, JOB_ID)
        assert recovery is not None and recovery.state == "completed"
        assert recovery.blocked_at is not None
        assert recovery.blocked_at.replace(tzinfo=UTC) == clock[0]
        assert recovery.signed_grant == issued_grant
        assert recovery.grant_request_id == grant_request_id
        assert operation is not None and operation.state == "succeeded"
        assert job is not None and job.state == "succeeded"


def test_exact_target_contact_completes_armed_recovery_before_retry_dispatch(
    tmp_path,
):
    sessions, service, _ = seeded_services(tmp_path)
    plan = service.preview()
    service.apply(
        plan_digest=plan.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    operations = AgentJobService(sessions, clock=lambda: NOW)
    upgrades = AgentUpgradeService(
        sessions,
        operations,
        clock=lambda: NOW,
        current_revision=lambda: AUTHORITY_REVISION,
    )
    operations.set_result_consumer(upgrades.consume_agent_result)

    assert (
        operations.claim(
            NODE_ID,
            SOURCE_CERTIFICATE,
            30,
            protocol_version=3,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity={
                "architecture": "linux-arm64",
                "semantic_version": "0.1.0",
                "build_digest": TARGET_BUILD_DIGEST,
                "binary_digest": TARGET_BINARY_DIGEST,
                "self_test_passed": True,
            },
        )
        is None
    )
    with sessions() as session:
        recovery = session.get(
            AgentUpgradeCompatibilityRecovery,
            "spark3542-a122-exact-package-retry-v1",
        )
        operation = session.get(AgentOperation, OPERATION_ID)
        job = session.get(Job, JOB_ID)
        retry = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == OPERATION_ID,
                AgentOperationAttempt.attempt == 4,
            )
        )
        assert recovery is not None and recovery.state == "completed-before-dispatch"
        assert recovery.grant_request_id is None
        assert recovery.signed_grant is None
        assert retry is None
        assert operation is not None and operation.state == "succeeded"
        assert job is not None and job.state == "succeeded"


def test_exact_target_contact_completes_grantless_operator_blocked_recovery(
    tmp_path,
):
    sessions, service, _ = seeded_services(tmp_path)
    plan = service.preview()
    service.apply(
        plan_digest=plan.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    clock = [NOW + timedelta(minutes=5)]
    operations = AgentJobService(sessions, clock=lambda: clock[0])
    upgrades = AgentUpgradeService(
        sessions,
        operations,
        clock=lambda: clock[0],
        current_revision=lambda: AUTHORITY_REVISION,
    )
    operations.set_result_consumer(upgrades.consume_agent_result)

    assert operations.reconcile_compatibility_recoveries() is True
    with sessions() as session:
        recovery = session.get(
            AgentUpgradeCompatibilityRecovery,
            "spark3542-a122-exact-package-retry-v1",
        )
        assert recovery is not None and recovery.state == "operator-blocked"
        assert recovery.blocked_at is not None
        blocked_at = recovery.blocked_at.replace(tzinfo=UTC)
        assert recovery.issued_at is None
        assert recovery.signed_grant is None

    assert (
        operations.claim(
            NODE_ID,
            SOURCE_CERTIFICATE,
            30,
            protocol_version=3,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity={
                "architecture": "linux-arm64",
                "semantic_version": "0.1.0",
                "build_digest": TARGET_BUILD_DIGEST,
                "binary_digest": TARGET_BINARY_DIGEST,
                "self_test_passed": True,
            },
        )
        is None
    )
    with sessions() as session:
        recovery = session.get(
            AgentUpgradeCompatibilityRecovery,
            "spark3542-a122-exact-package-retry-v1",
        )
        operation = session.get(AgentOperation, OPERATION_ID)
        job = session.get(Job, JOB_ID)
        retry = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == OPERATION_ID,
                AgentOperationAttempt.attempt == 4,
            )
        )
        assert recovery is not None
        assert recovery.state == "completed-before-dispatch"
        assert recovery.blocked_at is not None
        assert recovery.blocked_at.replace(tzinfo=UTC) == blocked_at
        assert recovery.issued_at is None
        assert recovery.signed_grant is None
        assert retry is None
        assert operation is not None and operation.state == "succeeded"
        assert job is not None and job.state == "succeeded"


@pytest.mark.parametrize("terminal_state", ("armed", "operator-blocked"))
@pytest.mark.parametrize(
    "drift",
    (
        "wrong-certificate",
        "protocol",
        "missing-runtime-capability",
        "missing-upgrade-capability",
        "payload",
        "operation-payload-digest",
        "recovery-payload-digest",
        "recovery-plan-digest",
        "parent-payload-digest",
        "target-package-version",
        "target-binary",
        "target-build",
        "target-architecture",
        "target-semantic-version",
        "self-test",
    ),
)
def test_identity_only_terminal_reconciliation_rejects_every_inexact_contact(
    tmp_path,
    terminal_state,
    drift,
):
    sessions, service, _ = seeded_services(tmp_path)
    plan = service.preview()
    service.apply(
        plan_digest=plan.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    clock = [NOW]
    operations = AgentJobService(sessions, clock=lambda: clock[0])
    upgrades = AgentUpgradeService(
        sessions,
        operations,
        clock=lambda: clock[0],
        current_revision=lambda: AUTHORITY_REVISION,
    )
    operations.set_result_consumer(upgrades.consume_agent_result)
    if terminal_state == "operator-blocked":
        clock[0] = NOW + timedelta(minutes=5)
        assert operations.reconcile_compatibility_recoveries() is True

    certificate_serial = SOURCE_CERTIFICATE
    protocol_version = 3
    capabilities = ["agent.runtime.rust.v1", "agent.upgrade.v1"]
    identity = {
        "architecture": "linux-arm64",
        "semantic_version": "0.1.0",
        "build_digest": TARGET_BUILD_DIGEST,
        "binary_digest": TARGET_BINARY_DIGEST,
        "self_test_passed": True,
    }
    with sessions.begin() as session:
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        operation = session.get(AgentOperation, OPERATION_ID)
        job = session.get(Job, JOB_ID)
        assert recovery is not None and operation is not None and job is not None
        if drift == "wrong-certificate":
            certificate_serial = "other-active-certificate"
            session.add(
                AgentCertificate(
                    serial=certificate_serial,
                    node_id=NODE_ID,
                    not_before=NOW - timedelta(days=1),
                    not_after=NOW + timedelta(days=1),
                    fingerprint="other-active-fingerprint",
                    generation=2,
                )
            )
        elif drift == "payload":
            operation.payload = {**operation.payload, "package_bytes": 5_000_001}
        elif drift == "operation-payload-digest":
            operation.payload_digest = "0" * 64
        elif drift == "recovery-payload-digest":
            recovery.upgrade_payload_sha256 = "1" * 64
        elif drift == "recovery-plan-digest":
            recovery.plan_digest = "2" * 64
        elif drift == "parent-payload-digest":
            job.payload_digest = "3" * 64
        elif drift == "target-package-version":
            recovery.target_package_version = "0.1.0~dev.380+g000000000000"

    if drift == "protocol":
        protocol_version = 2
    elif drift == "missing-runtime-capability":
        capabilities.remove("agent.runtime.rust.v1")
    elif drift == "missing-upgrade-capability":
        capabilities.remove("agent.upgrade.v1")
    elif drift == "target-binary":
        identity["binary_digest"] = "4" * 64
    elif drift == "target-build":
        identity["build_digest"] = f"sha256:{'5' * 64}"
    elif drift == "target-architecture":
        identity["architecture"] = "linux-amd64"
    elif drift == "target-semantic-version":
        identity["semantic_version"] = "0.1.1"
    elif drift == "self-test":
        identity["self_test_passed"] = False

    with sessions() as session:
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        operation = session.get(AgentOperation, OPERATION_ID)
        job = session.get(Job, JOB_ID)
        assert recovery is not None and operation is not None and job is not None
        before = (
            recovery.state,
            recovery.signed_grant,
            recovery.grant_request_id,
            recovery.grant_expires_at,
            recovery.retry_fence,
            recovery.retry_certificate_serial,
            recovery.issued_at,
            recovery.completed_at,
            operation.state,
            operation.current_attempt,
            operation.retry_disposition,
            operation.retry_disposition_attempt,
            job.state,
            job.status_reason,
        )

    if drift in {"protocol", "missing-runtime-capability", "self-test"}:
        with pytest.raises(ValueError):
            operations.claim(
                NODE_ID,
                certificate_serial,
                30,
                protocol_version=protocol_version,
                capabilities=capabilities,
                runtime_identity=identity,
            )
    else:
        assert (
            operations.claim(
                NODE_ID,
                certificate_serial,
                30,
                protocol_version=protocol_version,
                capabilities=capabilities,
                runtime_identity=identity,
            )
            is None
        )

    with sessions() as session:
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        operation = session.get(AgentOperation, OPERATION_ID)
        job = session.get(Job, JOB_ID)
        retry = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == OPERATION_ID,
                AgentOperationAttempt.attempt == SOURCE_ATTEMPT + 1,
            )
        )
        assert recovery is not None and operation is not None and job is not None
        after = (
            recovery.state,
            recovery.signed_grant,
            recovery.grant_request_id,
            recovery.grant_expires_at,
            recovery.retry_fence,
            recovery.retry_certificate_serial,
            recovery.issued_at,
            recovery.completed_at,
            operation.state,
            operation.current_attempt,
            operation.retry_disposition,
            operation.retry_disposition_attempt,
            job.state,
            job.status_reason,
        )
        assert after == before
        assert retry is None


def test_ordinary_upgrade_still_receives_only_install_grant(tmp_path):
    sessions, _service, _ = seeded_services(tmp_path)
    other_operation = "50000000-0000-4000-8000-000000000005"
    other_job = "60000000-0000-4000-8000-000000000006"
    other_fence = "70000000-0000-4000-8000-000000000007"
    with sessions.begin() as session:
        session.add(
            Job(
                id=other_job,
                request_id=str(uuid.uuid4()),
                kind="agent-upgrade",
                state="queued",
                actor="admin",
                authority_revision=AUTHORITY_REVISION,
                targets=[NODE_ID],
                payload_digest="1" * 64,
                payload={},
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            AgentOperation(
                id=other_operation,
                parent_job_id=other_job,
                node_id=NODE_ID,
                kind="agent.upgrade.v1",
                payload_digest="2" * 64,
                payload=PACKAGE,
                authority_revision=AUTHORITY_REVISION,
                state="running",
                current_attempt=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            AgentOperationAttempt(
                operation_id=other_operation,
                attempt=1,
                fence=other_fence,
                lease_deadline=NOW + timedelta(seconds=60),
                agent_certificate_serial=SOURCE_CERTIFICATE,
                state="running",
            )
        )
    authority = HostRuntimeAuthorityService(
        sessions,
        HostHelperGrantIssuer(
            ed25519.Ed25519PrivateKey.from_private_bytes(b"m" * 32),
            clock=lambda: NOW,
        ),
        clock=lambda: NOW,
    )
    grant = authority.issue_agent_upgrade_grant(
        node_id=NODE_ID,
        job_id=other_job,
        operation_id=other_operation,
        attempt=1,
        fence=other_fence,
        package_sha256=TARGET_PACKAGE_SHA256,
        package_signature=PACKAGE_SIGNATURE,
        certificate_serial=SOURCE_CERTIFICATE,
    )
    assert grant.claims.operation.to_mapping()["type"] == "install-vonk-deb"
