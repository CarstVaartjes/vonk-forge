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
    ABANDON_CONFIRMATION,
    COMPATIBILITY_TIMEOUT_REASON,
    CONFIRMATION,
    FOLLOWING_NODE_ID,
    GRANTLESS_RETRY_CERTIFICATE_SERIAL,
    GRANTLESS_RETRY_FENCE,
    JOB_ID,
    NODE_ID,
    OPERATION_ID,
    ORIGINAL_DISPATCH_CERTIFICATE_SERIAL,
    RECOVERY_ID,
    RETRY_ATTEMPT,
    SOURCE_ATTEMPT,
    SOURCE_ATTEMPT_CERTIFICATE_SERIAL,
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
RETRY_FENCE = GRANTLESS_RETRY_FENCE
SOURCE_CERTIFICATE = ORIGINAL_DISPATCH_CERTIFICATE_SERIAL
ROTATED_DISPATCH_CERTIFICATE = "205226603666808797593500589744536484673"
GRANTLESS_RETRY_CERTIFICATE = GRANTLESS_RETRY_CERTIFICATE_SERIAL
SOURCE_ATTEMPT_CERTIFICATE = SOURCE_ATTEMPT_CERTIFICATE_SERIAL
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
            "node_ids": [NODE_ID, FOLLOWING_NODE_ID],
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
                serial=SOURCE_ATTEMPT_CERTIFICATE,
                node_id=NODE_ID,
                not_before=NOW - timedelta(days=1),
                not_after=NOW + timedelta(days=1),
                fingerprint="source-attempt-fingerprint",
                state="revoked",
                generation=1,
                revoked_at=NOW - timedelta(minutes=1),
            )
        )
        if GRANTLESS_RETRY_CERTIFICATE != SOURCE_CERTIFICATE:
            session.add(
                AgentCertificate(
                    serial=GRANTLESS_RETRY_CERTIFICATE,
                    node_id=NODE_ID,
                    not_before=NOW - timedelta(days=1),
                    not_after=NOW + timedelta(days=1),
                    fingerprint="grantless-retry-fingerprint",
                    state="revoked",
                    generation=2,
                    revoked_at=NOW - timedelta(seconds=1),
                )
            )
        session.add(
            AgentCertificate(
                serial=SOURCE_CERTIFICATE,
                node_id=NODE_ID,
                not_before=NOW - timedelta(days=1),
                not_after=NOW + timedelta(days=1),
                fingerprint="dispatch-fingerprint",
                generation=3,
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
                targets=[NODE_ID, FOLLOWING_NODE_ID],
                payload_digest=JOB_PLAN_DIGEST,
                payload={
                    "node_order": [NODE_ID, FOLLOWING_NODE_ID],
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
                agent_certificate_serial=SOURCE_ATTEMPT_CERTIFICATE,
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


def seed_grantless_operator_blocked_retry(sessions) -> None:
    with sessions.begin() as session:
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        operation = session.get(AgentOperation, OPERATION_ID)
        job = session.get(Job, JOB_ID)
        source = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == OPERATION_ID,
                AgentOperationAttempt.attempt == SOURCE_ATTEMPT,
            )
        )
        assert recovery is not None and operation is not None and job is not None
        assert source is not None
        source.state = "expired"
        recovery.state = "operator-blocked"
        recovery.blocked_at = NOW
        operation.state = "waiting-for-operator"
        operation.current_attempt = RETRY_ATTEMPT
        operation.retry_disposition = None
        operation.retry_disposition_attempt = None
        job.state = "waiting-for-operator"
        job.status_reason = COMPATIBILITY_TIMEOUT_REASON
        session.add(
            AgentOperationAttempt(
                operation_id=OPERATION_ID,
                attempt=RETRY_ATTEMPT,
                fence=RETRY_FENCE,
                lease_deadline=NOW - timedelta(seconds=1),
                agent_certificate_serial=GRANTLESS_RETRY_CERTIFICATE,
                state="failed",
                result={
                    "error_code": "agent_upgrade_failed",
                    "reason": "agent upgrade authority is unavailable",
                    "status": "failed",
                },
            )
        )


def test_expired_recovery_abandonment_releases_only_queued_mutation(tmp_path):
    sessions, service, notifications = seeded_services(tmp_path)
    original = service.preview()
    service.apply(
        plan_digest=original.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    seed_grantless_operator_blocked_retry(sessions)
    queued_job_id = "ac238c29-d66e-49bc-a480-b7f11fdc8d6c"
    queued_operation_id = "bc238c29-d66e-49bc-a480-b7f11fdc8d6c"
    with sessions.begin() as session:
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        retry = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == OPERATION_ID,
                AgentOperationAttempt.attempt == RETRY_ATTEMPT,
            )
        )
        assert recovery is not None and retry is not None
        recovery.retry_fence = "40000000-0000-4000-8000-000000000004"
        recovery.retry_certificate_serial = SOURCE_CERTIFICATE
        recovery.signed_grant = {"schema_version": 1, "kind": "scheduled-reboot"}
        recovery.grant_request_id = "50000000-0000-4000-8000-000000000005"
        recovery.grant_expires_at = NOW - timedelta(seconds=1)
        recovery.identity_deadline = NOW - timedelta(seconds=1)
        recovery.issued_at = NOW - timedelta(minutes=2)
        retry.state = "waiting-for-operator"
        session.add(
            Job(
                id=queued_job_id,
                request_id=str(uuid.uuid4()),
                kind="agent-upgrade",
                state="queued",
                actor="admin",
                authority_revision="c" * 64,
                targets=[NODE_ID],
                payload_digest="d" * 64,
                payload={"node_order": [NODE_ID]},
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            AgentOperation(
                id=queued_operation_id,
                parent_job_id=queued_job_id,
                node_id=NODE_ID,
                kind="agent.upgrade.v1",
                payload_digest="e" * 64,
                payload={"schema_version": 1},
                authority_revision="c" * 64,
                state="queued",
                current_attempt=0,
                created_at=NOW,
                updated_at=NOW,
            )
        )

    preview = service.preview_abandon()
    assert preview.state == "preview"
    assert preview.document["queued_mutations"] == [
        {
            "job_id": queued_job_id,
            "operation_id": queued_operation_id,
            "kind": "agent.upgrade.v1",
            "authority_revision": "c" * 64,
            "payload_digest": "e" * 64,
        }
    ]
    abandoned = service.abandon(
        plan_digest=preview.plan_digest,
        confirmation=ABANDON_CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    assert abandoned.state == "abandoned"
    assert notifications == [True, True]
    with sessions() as session:
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        operation = session.get(AgentOperation, OPERATION_ID)
        job = session.get(Job, JOB_ID)
        queued_operation = session.get(AgentOperation, queued_operation_id)
        assert recovery is not None and recovery.state == "abandoned"
        assert recovery.completed_at.replace(tzinfo=UTC) == NOW
        assert recovery.signed_grant == {
            "schema_version": 1,
            "kind": "scheduled-reboot",
        }
        assert operation is not None and operation.state == "cancelled"
        assert job is not None and job.state == "cancelled"
        assert queued_operation is not None and queued_operation.state == "queued"


def test_grantless_terminal_abandonment_preserves_no_grant_and_exact_rearm_certs(
    tmp_path,
):
    sessions, service, notifications = seeded_services(tmp_path)
    original = service.preview()
    service.apply(
        plan_digest=original.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    seed_grantless_operator_blocked_retry(sessions)
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_ID)
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        retry = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == OPERATION_ID,
                AgentOperationAttempt.attempt == RETRY_ATTEMPT,
            )
        )
        assert node is not None and recovery is not None and retry is not None
        session.add(
            AgentCertificate(
                serial=ROTATED_DISPATCH_CERTIFICATE,
                node_id=NODE_ID,
                not_before=NOW - timedelta(minutes=1),
                not_after=NOW + timedelta(days=1),
                fingerprint="rotated-dispatch-fingerprint",
                generation=4,
            )
        )
        node.contact_certificate_serial = ROTATED_DISPATCH_CERTIFICATE
        recovery.rearm_attempt_certificate_serial = GRANTLESS_RETRY_CERTIFICATE
        recovery.rearm_dispatch_certificate_serial = ROTATED_DISPATCH_CERTIFICATE
        retry.state = "waiting-for-operator"

    preview = service.preview_abandon()
    assert preview.document["grant_disposition"] == "never-issued"
    assert preview.document["identity_deadline"] is None
    assert (
        preview.document["contact_certificate_serial"] == ROTATED_DISPATCH_CERTIFICATE
    )
    abandoned = service.abandon(
        plan_digest=preview.plan_digest,
        confirmation=ABANDON_CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )

    assert abandoned.state == "abandoned"
    assert notifications == [True, True]
    with sessions() as session:
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        assert recovery is not None and recovery.state == "abandoned"
        assert recovery.signed_grant is None
        assert recovery.retry_fence is None
        assert recovery.retry_certificate_serial is None
        assert recovery.issued_at is None
        assert recovery.identity_deadline is None


def test_grantless_terminal_abandonment_rejects_inexact_dispatch_certificate(
    tmp_path,
):
    sessions, service, _ = seeded_services(tmp_path)
    original = service.preview()
    service.apply(
        plan_digest=original.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    seed_grantless_operator_blocked_retry(sessions)
    with sessions.begin() as session:
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        assert recovery is not None
        recovery.rearm_attempt_certificate_serial = GRANTLESS_RETRY_CERTIFICATE
        recovery.rearm_dispatch_certificate_serial = ROTATED_DISPATCH_CERTIFICATE

    with pytest.raises(CompatibilityRecoveryConflict, match="cannot be abandoned"):
        service.preview_abandon()


def test_grantless_abandoned_recovery_reauthorizes_exact_attempt_without_grant(
    tmp_path,
):
    sessions, service, notifications = seeded_services(tmp_path)
    original = service.preview()
    service.apply(
        plan_digest=original.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    seed_grantless_operator_blocked_retry(sessions)
    paused_job_id = "ac238c29-d66e-49bc-a480-b7f11fdc8d6c"
    paused_operation_id = "31e67068-461a-4c92-b407-df10a11d6632"
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_ID)
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        retry = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == OPERATION_ID,
                AgentOperationAttempt.attempt == RETRY_ATTEMPT,
            )
        )
        assert node is not None and recovery is not None and retry is not None
        session.add(
            AgentCertificate(
                serial=ROTATED_DISPATCH_CERTIFICATE,
                node_id=NODE_ID,
                not_before=NOW - timedelta(minutes=1),
                not_after=NOW + timedelta(days=1),
                fingerprint="rotated-dispatch-fingerprint",
                generation=4,
            )
        )
        node.contact_certificate_serial = ROTATED_DISPATCH_CERTIFICATE
        recovery.rearm_attempt_certificate_serial = GRANTLESS_RETRY_CERTIFICATE
        recovery.rearm_dispatch_certificate_serial = ROTATED_DISPATCH_CERTIFICATE
        retry.state = "waiting-for-operator"
        session.add(
            Job(
                id=paused_job_id,
                request_id=str(uuid.uuid4()),
                kind="agent-upgrade",
                state="waiting-for-operator",
                actor="admin",
                authority_revision="c" * 64,
                targets=[NODE_ID],
                payload_digest="d" * 64,
                payload={"node_order": [NODE_ID]},
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            AgentOperation(
                id=paused_operation_id,
                parent_job_id=paused_job_id,
                node_id=NODE_ID,
                kind="agent.upgrade.v1",
                payload_digest="e" * 64,
                payload={"schema_version": 1},
                authority_revision="c" * 64,
                state="waiting-for-operator",
                current_attempt=2,
                created_at=NOW,
                updated_at=NOW,
            )
        )

    abandonment = service.preview_abandon()
    service.abandon(
        plan_digest=abandonment.plan_digest,
        confirmation=ABANDON_CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )

    preview = service.preview()
    assert preview.state == "preview"
    assert preview.plan_digest not in {original.plan_digest, abandonment.plan_digest}
    assert (
        preview.document["dispatch_certificate_serial"] == ROTATED_DISPATCH_CERTIFICATE
    )
    with sessions.begin() as session:
        paused = session.get(AgentOperation, paused_operation_id)
        assert paused is not None
        paused.payload_digest = "f" * 64
    with pytest.raises(CompatibilityRecoveryConflict, match="preview is stale"):
        service.apply(
            plan_digest=preview.plan_digest,
            confirmation=CONFIRMATION,
            actor="admin",
            request_id=str(uuid.uuid4()),
        )
    refreshed = service.preview()
    assert refreshed.plan_digest != preview.plan_digest
    applied = service.apply(
        plan_digest=refreshed.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )

    assert applied.state == "armed"
    assert notifications == [True, True, True]
    with sessions() as session:
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        operation = session.get(AgentOperation, OPERATION_ID)
        retry = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == OPERATION_ID,
                AgentOperationAttempt.attempt == RETRY_ATTEMPT,
            )
        )
        job = session.get(Job, JOB_ID)
        paused = session.get(AgentOperation, paused_operation_id)
        assert recovery is not None and recovery.state == "armed"
        assert recovery.abandoned_at is not None
        assert recovery.abandoned_at.replace(tzinfo=UTC) == NOW
        assert recovery.completed_at is None and recovery.blocked_at is None
        assert recovery.signed_grant is None
        assert recovery.retry_fence is None
        assert recovery.retry_certificate_serial is None
        assert recovery.grant_request_id is None
        assert recovery.grant_expires_at is None
        assert recovery.identity_deadline is None
        assert recovery.issued_at is None
        assert operation is not None and operation.state == "running"
        assert retry is not None and retry.state == "running"
        assert retry.attempt == RETRY_ATTEMPT and retry.fence == RETRY_FENCE
        assert job is not None and job.state == "running"
        assert paused is not None and paused.state == "waiting-for-operator"


def test_grantless_abandoned_recovery_rejects_dispatch_identity_drift(tmp_path):
    sessions, service, _ = seeded_services(tmp_path)
    original = service.preview()
    service.apply(
        plan_digest=original.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    seed_grantless_operator_blocked_retry(sessions)
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_ID)
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        retry = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == OPERATION_ID,
                AgentOperationAttempt.attempt == RETRY_ATTEMPT,
            )
        )
        assert node is not None and recovery is not None and retry is not None
        session.add(
            AgentCertificate(
                serial=ROTATED_DISPATCH_CERTIFICATE,
                node_id=NODE_ID,
                not_before=NOW - timedelta(minutes=1),
                not_after=NOW + timedelta(days=1),
                fingerprint="rotated-dispatch-fingerprint",
                generation=4,
            )
        )
        node.contact_certificate_serial = ROTATED_DISPATCH_CERTIFICATE
        recovery.rearm_attempt_certificate_serial = GRANTLESS_RETRY_CERTIFICATE
        recovery.rearm_dispatch_certificate_serial = ROTATED_DISPATCH_CERTIFICATE
        retry.state = "waiting-for-operator"

    abandonment = service.preview_abandon()
    service.abandon(
        plan_digest=abandonment.plan_digest,
        confirmation=ABANDON_CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_ID)
        assert node is not None
        node.binary_digest = "0" * 64

    with pytest.raises(CompatibilityRecoveryConflict, match="no longer recoverable"):
        service.preview()


def test_grantless_operator_blocked_rearm_replays_exact_attempt_four(tmp_path, caplog):
    sessions, service, notifications = seeded_services(tmp_path)
    original = service.preview()
    original_created_at = NOW
    service.apply(
        plan_digest=original.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    seed_grantless_operator_blocked_retry(sessions)
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_ID)
        historical = session.get(AgentCertificate, GRANTLESS_RETRY_CERTIFICATE)
        assert node is not None and historical is not None
        historical.state = "revoked"
        historical.revoked_at = NOW - timedelta(milliseconds=1)
        node.contact_certificate_serial = ROTATED_DISPATCH_CERTIFICATE
        session.add(
            AgentCertificate(
                serial=ROTATED_DISPATCH_CERTIFICATE,
                node_id=NODE_ID,
                not_before=NOW - timedelta(minutes=1),
                not_after=NOW + timedelta(days=1),
                fingerprint="rotated-dispatch-fingerprint",
                generation=4,
            )
        )

    preview = service.preview()
    assert preview.state == "preview"
    assert preview.plan_digest != original.plan_digest
    assert (
        preview.document["dispatch_certificate_serial"] == ROTATED_DISPATCH_CERTIFICATE
    )
    applied = service.apply(
        plan_digest=preview.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    assert applied.state == "armed"
    assert notifications == [True, True]

    with sessions() as session:
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        operation = session.get(AgentOperation, OPERATION_ID)
        job = session.get(Job, JOB_ID)
        attempts = session.scalars(
            select(AgentOperationAttempt)
            .where(AgentOperationAttempt.operation_id == OPERATION_ID)
            .order_by(AgentOperationAttempt.attempt)
        ).all()
        retry = attempts[-1]
        assert recovery is not None and recovery.state == "armed"
        assert recovery.created_at.replace(tzinfo=UTC) == original_created_at
        assert recovery.retry_fence is None
        assert recovery.retry_certificate_serial is None
        assert recovery.signed_grant is None
        assert recovery.grant_request_id is None
        assert recovery.grant_expires_at is None
        assert recovery.identity_deadline is None
        assert recovery.issued_at is None
        assert recovery.rearm_attempt_certificate_serial == GRANTLESS_RETRY_CERTIFICATE
        assert (
            recovery.rearm_dispatch_certificate_serial == ROTATED_DISPATCH_CERTIFICATE
        )
        assert [attempt.attempt for attempt in attempts] == [
            SOURCE_ATTEMPT,
            RETRY_ATTEMPT,
        ]
        assert retry.fence == RETRY_FENCE
        assert retry.agent_certificate_serial == GRANTLESS_RETRY_CERTIFICATE
        assert retry.state == "running"
        assert retry.result == {
            "error_code": "agent_upgrade_failed",
            "reason": "agent upgrade authority is unavailable",
            "status": "failed",
        }
        assert retry.lease_deadline.replace(tzinfo=UTC) == NOW + timedelta(minutes=10)
        assert operation is not None and operation.state == "running"
        assert operation.current_attempt == RETRY_ATTEMPT
        assert job is not None and job.state == "running"

    operations = AgentJobService(sessions, clock=lambda: NOW)
    replay = operations.claim(
        NODE_ID,
        ROTATED_DISPATCH_CERTIFICATE,
        30,
        protocol_version=3,
        capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
        runtime_identity={
            "architecture": "linux-arm64",
            "semantic_version": SOURCE_SEMANTIC_VERSION,
            "build_digest": SOURCE_BUILD_DIGEST,
            "binary_digest": SOURCE_BINARY_DIGEST,
            "self_test_passed": True,
        },
    )
    assert replay is not None
    assert replay.attempt == RETRY_ATTEMPT
    assert replay.fence == RETRY_FENCE
    with sessions() as session:
        assert (
            session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == OPERATION_ID,
                    AgentOperationAttempt.attempt == RETRY_ATTEMPT + 1,
                )
            )
            is None
        )

    authority = HostRuntimeAuthorityService(
        sessions,
        HostHelperGrantIssuer(
            ed25519.Ed25519PrivateKey.from_private_bytes(b"m" * 32),
            clock=lambda: NOW,
            request_id_factory=lambda: "30000000-0000-4000-8000-000000000003",
        ),
        clock=lambda: NOW,
    )
    with pytest.raises(HostHelperAuthorityError, match="stale"):
        authority.issue_agent_upgrade_grant(
            node_id=NODE_ID,
            job_id=JOB_ID,
            operation_id=OPERATION_ID,
            attempt=RETRY_ATTEMPT,
            fence=RETRY_FENCE,
            package_sha256=TARGET_PACKAGE_SHA256,
            package_signature=PACKAGE_SIGNATURE,
            certificate_serial=ROTATED_DISPATCH_CERTIFICATE,
            expires_in_seconds=30,
        )
    rejection = next(
        record
        for record in caplog.records
        if record.getMessage() == "compatibility host-helper grant rejected"
    )
    assert rejection.compatibility_recovery_id == RECOVERY_ID
    assert rejection.operation_id == OPERATION_ID
    assert rejection.attempt == RETRY_ATTEMPT
    assert rejection.rejection_category == "ttl_mismatch"
    with pytest.raises(HostHelperAuthorityError, match="stale"):
        authority.issue_agent_upgrade_grant(
            node_id=NODE_ID,
            job_id=JOB_ID,
            operation_id=OPERATION_ID,
            attempt=RETRY_ATTEMPT,
            fence=RETRY_FENCE,
            package_sha256=TARGET_PACKAGE_SHA256,
            package_signature=PACKAGE_SIGNATURE,
            certificate_serial=GRANTLESS_RETRY_CERTIFICATE,
            expires_in_seconds=10,
        )
    grant = authority.issue_agent_upgrade_grant(
        node_id=NODE_ID,
        job_id=JOB_ID,
        operation_id=OPERATION_ID,
        attempt=RETRY_ATTEMPT,
        fence=RETRY_FENCE,
        package_sha256=TARGET_PACKAGE_SHA256,
        package_signature=PACKAGE_SIGNATURE,
        certificate_serial=ROTATED_DISPATCH_CERTIFICATE,
        expires_in_seconds=10,
    )
    assert grant.claims.expires_at - grant.claims.issued_at == 10
    with sessions() as session:
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        assert recovery is not None and recovery.state == "issued"
        assert recovery.retry_fence == RETRY_FENCE
        assert recovery.retry_certificate_serial == ROTATED_DISPATCH_CERTIFICATE
        assert recovery.signed_grant == grant.to_mapping()

    assert (
        operations.claim(
            NODE_ID,
            ROTATED_DISPATCH_CERTIFICATE,
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
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        operation = session.get(AgentOperation, OPERATION_ID)
        retry = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == OPERATION_ID,
                AgentOperationAttempt.attempt == RETRY_ATTEMPT,
            )
        )
        assert recovery is not None and recovery.state == "completed"
        assert operation is not None and operation.state == "succeeded"
        assert retry is not None
        assert retry.agent_certificate_serial == GRANTLESS_RETRY_CERTIFICATE


def test_grantless_rearm_apply_is_idempotent_after_lost_response(tmp_path):
    sessions, service, notifications = seeded_services(tmp_path)
    original = service.preview()
    service.apply(
        plan_digest=original.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    seed_grantless_operator_blocked_retry(sessions)

    preview = service.preview()
    first = service.apply(
        plan_digest=preview.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    repeated_preview = service.preview()
    repeated = service.apply(
        plan_digest=preview.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )

    assert first.state == repeated_preview.state == repeated.state == "armed"
    assert first.plan_digest == repeated_preview.plan_digest == repeated.plan_digest
    assert notifications == [True, True]


def test_grantless_rearm_rejects_certificate_renewal_between_preview_and_apply(
    tmp_path,
):
    sessions, service, notifications = seeded_services(tmp_path)
    original = service.preview()
    service.apply(
        plan_digest=original.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    seed_grantless_operator_blocked_retry(sessions)
    stale = service.preview()

    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_ID)
        assert node is not None
        node.contact_certificate_serial = ROTATED_DISPATCH_CERTIFICATE
        session.add(
            AgentCertificate(
                serial=ROTATED_DISPATCH_CERTIFICATE,
                node_id=NODE_ID,
                not_before=NOW - timedelta(minutes=1),
                not_after=NOW + timedelta(days=1),
                fingerprint="renewal-race-fingerprint",
                generation=4,
            )
        )

    with pytest.raises(CompatibilityRecoveryConflict, match="preview is stale"):
        service.apply(
            plan_digest=stale.plan_digest,
            confirmation=CONFIRMATION,
            actor="admin",
            request_id=str(uuid.uuid4()),
        )
    with sessions() as session:
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        assert recovery is not None and recovery.state == "operator-blocked"
        assert recovery.rearm_attempt_certificate_serial is None
        assert recovery.rearm_dispatch_certificate_serial is None

    fresh = service.preview()
    assert fresh.plan_digest != stale.plan_digest
    assert fresh.document["dispatch_certificate_serial"] == ROTATED_DISPATCH_CERTIFICATE
    applied = service.apply(
        plan_digest=fresh.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    assert applied.state == "armed"
    assert notifications == [True, True]
    with sessions() as session:
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        assert recovery is not None
        assert recovery.rearm_attempt_certificate_serial == GRANTLESS_RETRY_CERTIFICATE
        assert (
            recovery.rearm_dispatch_certificate_serial == ROTATED_DISPATCH_CERTIFICATE
        )
    with sessions() as session:
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        attempts = session.scalars(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == OPERATION_ID
            )
        ).all()
        assert recovery is not None and recovery.signed_grant is None
        assert len(attempts) == 2


def test_grantless_rearm_can_repeat_after_another_dispatch_timeout(tmp_path):
    sessions, service, notifications = seeded_services(tmp_path)
    original = service.preview()
    service.apply(
        plan_digest=original.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    seed_grantless_operator_blocked_retry(sessions)
    preview = service.preview()
    service.apply(
        plan_digest=preview.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )

    clock = [NOW + timedelta(minutes=6)]
    operations = AgentJobService(sessions, clock=lambda: clock[0])
    # A maximum five-minute agent backoff plus an in-flight long poll cannot
    # consume the durable re-arm before dev335 has a chance to reclaim it.
    assert operations.reconcile_compatibility_recoveries() is False
    with sessions() as session:
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        retry = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == OPERATION_ID,
                AgentOperationAttempt.attempt == RETRY_ATTEMPT,
            )
        )
        assert recovery is not None and recovery.state == "armed"
        assert retry is not None and retry.state == "running"
        assert retry.lease_deadline.replace(tzinfo=UTC) == NOW + timedelta(minutes=10)

    clock[0] = NOW + timedelta(minutes=11)
    assert operations.reconcile_compatibility_recoveries() is True
    with sessions.begin() as session:
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        node = session.get(AgentNode, NODE_ID)
        retry = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == OPERATION_ID,
                AgentOperationAttempt.attempt == RETRY_ATTEMPT,
            )
        )
        assert recovery is not None and recovery.state == "operator-blocked"
        assert node is not None
        node.last_seen_at = clock[0]
        assert retry is not None and retry.state == "waiting-for-operator"
        assert retry.fence == RETRY_FENCE
        assert retry.result == {
            "error_code": "agent_upgrade_failed",
            "reason": "agent upgrade authority is unavailable",
            "status": "failed",
        }
        assert recovery.retry_fence is None
        assert recovery.retry_certificate_serial is None
        assert recovery.signed_grant is None
        assert recovery.grant_request_id is None
        assert recovery.grant_expires_at is None
        assert recovery.identity_deadline is None
        assert recovery.issued_at is None

        job = session.get(Job, JOB_ID)
        assert job is not None
        assert job.status_reason == COMPATIBILITY_TIMEOUT_REASON

    later_service = Spark3542CompatibilityRecoveryService(
        sessions,
        clock=lambda: clock[0],
        notify_available=lambda: notifications.append(True),
    )
    repeated_preview = later_service.preview()
    assert repeated_preview.state == "preview"
    repeated = later_service.apply(
        plan_digest=repeated_preview.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    assert repeated.state == "armed"
    assert notifications == [True, True, True]
    with sessions() as session:
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        retry = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == OPERATION_ID,
                AgentOperationAttempt.attempt == RETRY_ATTEMPT,
            )
        )
        assert retry is not None and retry.state == "running"
        assert retry.fence == RETRY_FENCE
        assert retry.lease_deadline.replace(tzinfo=UTC) == clock[0] + timedelta(
            minutes=10
        )
        assert recovery is not None
        assert recovery.retry_fence is None
        assert recovery.retry_certificate_serial is None
        assert recovery.signed_grant is None
        assert recovery.grant_request_id is None
        assert recovery.grant_expires_at is None
        assert recovery.identity_deadline is None
        assert recovery.issued_at is None


@pytest.mark.parametrize(
    "field",
    ("rearm_attempt_certificate_serial", "rearm_dispatch_certificate_serial"),
)
def test_second_grantless_rearm_rejects_prior_certificate_binding_drift(
    tmp_path, field
):
    sessions, service, _ = seeded_services(tmp_path)
    original = service.preview()
    service.apply(
        plan_digest=original.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    seed_grantless_operator_blocked_retry(sessions)
    preview = service.preview()
    service.apply(
        plan_digest=preview.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    later = NOW + timedelta(minutes=11)
    operations = AgentJobService(sessions, clock=lambda: later)
    assert operations.reconcile_compatibility_recoveries() is True
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_ID)
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        assert node is not None and recovery is not None
        node.last_seen_at = later
        setattr(recovery, field, "inexact-certificate")

    later_service = Spark3542CompatibilityRecoveryService(
        sessions,
        clock=lambda: later,
        notify_available=lambda: None,
    )
    with pytest.raises(CompatibilityRecoveryConflict, match="no longer recoverable"):
        later_service.preview()
    with sessions() as session:
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        assert recovery is not None and recovery.state == "operator-blocked"
        assert recovery.retry_fence is None
        assert recovery.retry_certificate_serial is None
        assert recovery.signed_grant is None
        assert recovery.grant_request_id is None
        assert recovery.grant_expires_at is None
        assert recovery.identity_deadline is None
        assert recovery.issued_at is None


def test_postgres_second_grantless_rearm_preserves_attempt_and_null_grant(
    tmp_path, postgres_engine
):
    sessions, service, _ = seeded_services(tmp_path, engine=postgres_engine)
    original = service.preview()
    service.apply(
        plan_digest=original.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    seed_grantless_operator_blocked_retry(sessions)
    first_preview = service.preview()
    service.apply(
        plan_digest=first_preview.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )

    later = NOW + timedelta(minutes=11)
    operations = AgentJobService(sessions, clock=lambda: later)
    assert operations.reconcile_compatibility_recoveries() is True
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_ID)
        assert node is not None
        node.last_seen_at = later
    later_service = Spark3542CompatibilityRecoveryService(
        sessions,
        clock=lambda: later,
        notify_available=lambda: None,
    )
    second_preview = later_service.preview()
    second = later_service.apply(
        plan_digest=second_preview.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )

    assert second.state == "armed"
    with sessions() as session:
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        attempts = session.scalars(
            select(AgentOperationAttempt)
            .where(AgentOperationAttempt.operation_id == OPERATION_ID)
            .order_by(AgentOperationAttempt.attempt)
        ).all()
        retry = attempts[-1]
        assert [attempt.attempt for attempt in attempts] == [
            SOURCE_ATTEMPT,
            RETRY_ATTEMPT,
        ]
        assert retry.fence == RETRY_FENCE
        assert retry.state == "running"
        assert retry.lease_deadline.replace(tzinfo=UTC) == later + timedelta(minutes=10)
        assert recovery is not None and recovery.state == "armed"
        assert recovery.retry_fence is None
        assert recovery.retry_certificate_serial is None
        assert recovery.signed_grant is None
        assert recovery.grant_request_id is None
        assert recovery.grant_expires_at is None
        assert recovery.identity_deadline is None
        assert recovery.issued_at is None


@pytest.mark.parametrize(
    "drift",
    (
        "failure",
        "failure-status",
        "timeout-reason",
        "protocol",
        "capabilities",
        "self-test",
        "certificate",
        "payload",
    ),
)
def test_grantless_operator_blocked_rearm_rejects_every_drift(tmp_path, drift):
    sessions, service, _ = seeded_services(tmp_path)
    original = service.preview()
    service.apply(
        plan_digest=original.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    seed_grantless_operator_blocked_retry(sessions)
    with sessions.begin() as session:
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        retry = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == OPERATION_ID,
                AgentOperationAttempt.attempt == RETRY_ATTEMPT,
            )
        )
        node = session.get(AgentNode, NODE_ID)
        operation = session.get(AgentOperation, OPERATION_ID)
        job = session.get(Job, JOB_ID)
        assert recovery is not None and retry is not None
        assert node is not None and operation is not None and job is not None
        if drift == "failure":
            retry.result = {
                "error_code": "agent_upgrade_failed",
                "reason": "different failure",
                "status": "failed",
            }
        elif drift == "failure-status":
            retry.result = {
                "error_code": "agent_upgrade_failed",
                "reason": "agent upgrade authority is unavailable",
            }
        elif drift == "timeout-reason":
            job.status_reason = "different timeout"
        elif drift == "protocol":
            node.protocol_version = 2
        elif drift == "capabilities":
            node.capabilities = ["agent.runtime.rust.v1"]
        elif drift == "self-test":
            node.self_test_passed = False
        elif drift == "certificate":
            retry.agent_certificate_serial = SOURCE_ATTEMPT_CERTIFICATE
        elif drift == "payload":
            operation.payload_digest = "0" * 64

    with pytest.raises(CompatibilityRecoveryConflict, match="no longer recoverable"):
        service.preview()
    with sessions() as session:
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        retry = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == OPERATION_ID,
                AgentOperationAttempt.attempt == RETRY_ATTEMPT,
            )
        )
        assert recovery is not None and recovery.state == "operator-blocked"
        assert recovery.signed_grant is None
        assert retry is not None and retry.state == "failed"


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
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        assert operation is not None
        assert recovery is not None
        operation.state = "running"
        operation.current_attempt = 4
        recovery.rearm_attempt_certificate_serial = SOURCE_CERTIFICATE
        recovery.rearm_dispatch_certificate_serial = SOURCE_CERTIFICATE
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
        expires_in_seconds=10,
    )
    with sessions.begin() as session:
        recovery = session.get(
            AgentUpgradeCompatibilityRecovery,
            "spark3542-a122-scheduled-reboot-v1",
        )
        assert recovery is not None
        recovery.state = "awaiting-identity"

    with sessions() as session:
        recovery = session.get(
            AgentUpgradeCompatibilityRecovery,
            "spark3542-a122-scheduled-reboot-v1",
        )
        assert recovery is not None and recovery.state == "awaiting-identity"

    with pytest.raises(IntegrityError), sessions.begin() as session:
        recovery = session.get(
            AgentUpgradeCompatibilityRecovery,
            "spark3542-a122-scheduled-reboot-v1",
        )
        assert recovery is not None
        recovery.signed_grant = None


def test_preview_and_apply_bind_exact_failed_attempt_and_arm_only_its_retry(tmp_path):
    sessions, service, notifications = seeded_services(tmp_path)

    plan = service.preview()
    assert plan.document["source_fence"] == SOURCE_FENCE
    assert plan.document["source_certificate_serial"] == SOURCE_ATTEMPT_CERTIFICATE
    assert plan.document["dispatch_certificate_serial"] == SOURCE_CERTIFICATE
    assert plan.document["source_job_targets"] == [NODE_ID, FOLLOWING_NODE_ID]
    assert plan.document["dispatch_job_targets"] == [NODE_ID]
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
        assert job.targets == [NODE_ID]
        assert job.payload["node_order"] == [NODE_ID]
        assert (
            job.payload_digest
            == hashlib.sha256(
                canonical_message(
                    {
                        "authority_revision": AUTHORITY_REVISION,
                        "node_ids": [NODE_ID],
                        "package": PACKAGE,
                        "strategy": "one-at-a-time",
                    }
                )
            ).hexdigest()
        )

    replay = service.apply(
        plan_digest=plan.plan_digest,
        confirmation=CONFIRMATION,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    assert replay.state == "armed"
    assert notifications == [True]


def test_preview_accepts_only_the_pinned_post_attempt_certificate_rotation(tmp_path):
    sessions, service, _ = seeded_services(tmp_path)

    with sessions() as session:
        node = session.get(AgentNode, NODE_ID)
        source = session.get(AgentCertificate, SOURCE_ATTEMPT_CERTIFICATE)
        dispatch = session.get(AgentCertificate, SOURCE_CERTIFICATE)
        assert node is not None and source is not None and dispatch is not None
        assert source.state == "revoked"
        assert node.contact_certificate_serial == dispatch.serial
        assert source.serial != dispatch.serial

    plan = service.preview()
    assert plan.document["source_certificate_serial"] == source.serial
    assert plan.document["dispatch_certificate_serial"] == dispatch.serial

    with sessions.begin() as session:
        source = session.get(AgentCertificate, SOURCE_ATTEMPT_CERTIFICATE)
        assert source is not None
        source.revoked_at = NOW - timedelta(minutes=10)
    with pytest.raises(CompatibilityRecoveryConflict, match="exact live dev335"):
        service.preview()


def test_preview_rejects_inactive_pinned_dispatch_certificate(tmp_path):
    sessions, service, _ = seeded_services(tmp_path)

    with sessions.begin() as session:
        dispatch = session.get(AgentCertificate, SOURCE_CERTIFICATE)
        assert dispatch is not None
        dispatch.state = "revoked"
        dispatch.revoked_at = NOW

    with pytest.raises(CompatibilityRecoveryConflict, match="exact live dev335"):
        service.preview()


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
            "spark3542-a122-scheduled-reboot-v1",
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


def test_grant_is_one_persisted_scheduled_reboot_and_never_an_install(tmp_path):
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
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        source = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == OPERATION_ID,
                AgentOperationAttempt.attempt == SOURCE_ATTEMPT,
            )
        )
        assert operation is not None and source is not None and recovery is not None
        source.state = "expired"
        operation.state = "running"
        operation.current_attempt = 4
        recovery.rearm_attempt_certificate_serial = SOURCE_CERTIFICATE
        recovery.rearm_dispatch_certificate_serial = SOURCE_CERTIFICATE
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
        "expires_in_seconds": 10,
    }

    with pytest.raises(HostHelperAuthorityError, match="stale"):
        authority.issue_agent_upgrade_grant(**{**arguments, "expires_in_seconds": 11})
    first = authority.issue_agent_upgrade_grant(**arguments)
    replay = authority.issue_agent_upgrade_grant(**arguments)

    assert first.to_mapping() == replay.to_mapping()
    assert first.claims.operation.to_mapping() == {
        "type": "schedule-reboot",
        "delay_seconds": 60,
    }
    with sessions() as session:
        recovery = session.get(
            AgentUpgradeCompatibilityRecovery, "spark3542-a122-scheduled-reboot-v1"
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
            "spark3542-a122-scheduled-reboot-v1",
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
            "spark3542-a122-scheduled-reboot-v1",
        )
        assert recovery is not None and recovery.state == "awaiting-identity"

    # Exact target contact must not complete if the persisted root grant was
    # changed after issuance, even though every node and payload identity is
    # otherwise exact.
    with sessions.begin() as session:
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        assert recovery is not None and recovery.signed_grant is not None
        exact_signed_grant = recovery.signed_grant
        changed_grant = dict(exact_signed_grant)
        changed_claims = dict(changed_grant["claims"])
        changed_claims["operation"] = {
            "type": "schedule-reboot",
            "delay_seconds": 61,
        }
        changed_grant["claims"] = changed_claims
        recovery.signed_grant = changed_grant
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
    with sessions.begin() as session:
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        assert recovery is not None and recovery.state == "awaiting-identity"
        recovery.signed_grant = exact_signed_grant

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
            "spark3542-a122-scheduled-reboot-v1",
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
            "spark3542-a122-scheduled-reboot-v1",
        )
        assert operation is not None and operation.state == "succeeded"
        assert job is not None and job.state == "succeeded"
        assert (
            session.scalar(
                select(AgentOperation).where(
                    AgentOperation.node_id == FOLLOWING_NODE_ID
                )
            )
            is None
        )
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
        recovery = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
        assert operation is not None and recovery is not None
        operation.state = "running"
        operation.current_attempt = 4
        recovery.rearm_attempt_certificate_serial = SOURCE_CERTIFICATE
        recovery.rearm_dispatch_certificate_serial = SOURCE_CERTIFICATE
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
        expires_in_seconds=10,
    )
    clock = [NOW + timedelta(seconds=601)]
    operations = AgentJobService(sessions, clock=lambda: clock[0])

    # The active systemd recovery owns a 600-second start window. Its durable
    # Controller guard adds margin and keeps the mutation lane closed.
    assert operations.reconcile_compatibility_recoveries() is False
    with sessions() as session:
        recovery = session.get(
            AgentUpgradeCompatibilityRecovery,
            "spark3542-a122-scheduled-reboot-v1",
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
            "spark3542-a122-scheduled-reboot-v1",
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
            "spark3542-a122-scheduled-reboot-v1",
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
            "spark3542-a122-scheduled-reboot-v1",
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
    clock = [NOW + timedelta(minutes=11)]
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
            "spark3542-a122-scheduled-reboot-v1",
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
            "spark3542-a122-scheduled-reboot-v1",
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
        clock[0] = NOW + timedelta(minutes=11)
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
                    generation=4,
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
