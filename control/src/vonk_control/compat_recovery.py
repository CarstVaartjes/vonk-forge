"""One-shot Controller bridge to Spark3542's already staged a122 recovery.

The installed dev335 agent already forwards the signed host-helper grant from an
``agent.upgrade.v1`` attempt without interpreting the operation. This module
arms exactly one retry of the existing failed a122 operation. The authority
then substitutes only a fixed 60-second scheduled reboot for that retry. On the
next boot, the enabled helper socket pulls in the already-staged a122 recovery.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import canonical_message

from .agent_jobs import MUTATING_AGENT_OPERATIONS
from .models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    AgentUpgradeCompatibilityRecovery,
    Job,
)

RECOVERY_ID = "spark3542-a122-scheduled-reboot-v1"
NODE_ID = "spk_2818d189042b4c77aefa7796f4befd23"
FOLLOWING_NODE_ID = "spk_9a86fdbab116442ab6707bf4181a3c1c"
SOURCE_JOB_TARGETS = (NODE_ID, FOLLOWING_NODE_ID)
JOB_ID = "6b945136-1be6-47e4-8ba0-5c5f815304ad"
OPERATION_ID = "d54e0b56-e465-41bd-9627-c81f37352dfd"
SOURCE_ATTEMPT = 3
RETRY_ATTEMPT = SOURCE_ATTEMPT + 1
SOURCE_ATTEMPT_CERTIFICATE_SERIAL = "45537549826457139242802212060416390279"
# Attempt 4 was originally claimed under this now-historical certificate.  The
# row is immutable audit evidence and must not be rewritten when an
# administrator re-arms the exact grantless failure.
GRANTLESS_RETRY_CERTIFICATE_SERIAL = "4088040328001011815331063771942676957"
DISPATCH_CERTIFICATE_SERIAL = "40880403280010118153316063771942676957"
SOURCE_SEMANTIC_VERSION = "0.1.0"
SOURCE_BINARY_DIGEST = (
    "dcad0a7bac861ad929e287112dedf09f6b845037979f17ca3cd7b8c5fcb0045e"
)
SOURCE_BUILD_DIGEST = (
    "sha256:4a4f433972ab05606bd5708f373f284502b8ea5906156b88867acd11431e84ec"
)
TARGET_PACKAGE_VERSION = "0.1.0~dev.381+ga122909feaa3"
TARGET_PACKAGE_SHA256 = (
    "3d62d157c01ceb500f9c36900d0ff409be75e4ec169619729e51ac6d42288038"
)
TARGET_BINARY_DIGEST = (
    "f103bd5adb535eb14e71c9553221b228b900dc2715e0c5b989d335791b7ae415"
)
TARGET_BUILD_DIGEST = (
    "sha256:f12f9a3953b34638b69ce687f64cd81aa936ce4bc855816fe3ef2cc279362420"
)
CONFIRMATION = "reboot-spark3542-to-resume-staged-a122-recovery"
_ONLINE_WINDOW = timedelta(seconds=150)
_TERMINAL_ATTEMPT_STATES = frozenset({"expired", "failed", "waiting-for-operator"})
_REARM_LEASE = timedelta(seconds=60)
_GRANTLESS_RETRY_FAILURE = {
    "error_code": "agent_upgrade_failed",
    "reason": "agent upgrade authority is unavailable",
}


class CompatibilityRecoveryConflict(RuntimeError):
    """The pinned recovery bridge is absent, stale, or already consumed."""


@dataclass(frozen=True, slots=True)
class CompatibilityRecoveryPlan:
    plan_digest: str
    document: dict[str, object]
    state: str


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class Spark3542CompatibilityRecoveryService:
    """Inspect and arm the only supported legacy recovery transaction."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        notify_available: Callable[[], None],
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._notify_available = notify_available

    def preview(self) -> CompatibilityRecoveryPlan:
        now = self._clock()
        with self._sessions() as session:
            existing = session.get(AgentUpgradeCompatibilityRecovery, RECOVERY_ID)
            if existing is not None:
                if self._grantless_rearm_candidate(existing):
                    binding = self._rearm_snapshot(session, now, lock=False)
                    return CompatibilityRecoveryPlan(
                        plan_digest=hashlib.sha256(
                            canonical_message(binding)
                        ).hexdigest(),
                        document=self._stored_document(existing),
                        state="preview",
                    )
                if self._grantless_rearm_applied_candidate(session, existing):
                    binding = self._rearm_snapshot(session, now, lock=False)
                    return CompatibilityRecoveryPlan(
                        plan_digest=hashlib.sha256(
                            canonical_message(binding)
                        ).hexdigest(),
                        document=self._stored_document(existing),
                        state="armed",
                    )
                return CompatibilityRecoveryPlan(
                    plan_digest=existing.plan_digest,
                    document=self._stored_document(existing),
                    state=existing.state,
                )
            document = self._snapshot(session, now, lock=False)
        digest = hashlib.sha256(canonical_message(document)).hexdigest()
        return CompatibilityRecoveryPlan(digest, document, "preview")

    def apply(
        self,
        *,
        plan_digest: str,
        confirmation: str,
        actor: str,
        request_id: str,
    ) -> CompatibilityRecoveryPlan:
        if confirmation != CONFIRMATION:
            raise CompatibilityRecoveryConflict(
                f"confirmation must be exactly {CONFIRMATION}"
            )
        now = self._clock()
        notify = False
        with self._sessions.begin() as session:
            existing = session.scalar(
                select(AgentUpgradeCompatibilityRecovery)
                .where(AgentUpgradeCompatibilityRecovery.id == RECOVERY_ID)
                .with_for_update(of=AgentUpgradeCompatibilityRecovery)
            )
            if existing is not None:
                if self._grantless_rearm_candidate(existing):
                    binding = self._rearm_snapshot(session, now, lock=True)
                    rearm_digest = hashlib.sha256(
                        canonical_message(binding)
                    ).hexdigest()
                    if plan_digest != rearm_digest:
                        raise CompatibilityRecoveryConflict(
                            "Spark3542 compatibility recovery re-arm preview is stale"
                        )
                    operation = session.get(AgentOperation, OPERATION_ID)
                    retry = session.scalar(
                        select(AgentOperationAttempt).where(
                            AgentOperationAttempt.operation_id == OPERATION_ID,
                            AgentOperationAttempt.attempt == RETRY_ATTEMPT,
                        )
                    )
                    job = session.get(Job, JOB_ID)
                    assert (
                        operation is not None and retry is not None and job is not None
                    )
                    # Reopen the exact failed attempt. Its fence, certificate,
                    # and failure result remain durable; this transition neither
                    # constructs nor persists a host-helper grant.
                    existing.state = "armed"
                    existing.blocked_at = None
                    retry.state = "running"
                    retry.lease_deadline = now + _REARM_LEASE
                    operation.state = "running"
                    operation.updated_at = now
                    job.state = "running"
                    job.status_reason = None
                    job.updated_at = now
                    notify = True
                    document = self._stored_document(existing)
                    result = CompatibilityRecoveryPlan(rearm_digest, document, "armed")
                elif self._grantless_rearm_applied_candidate(session, existing):
                    binding = self._rearm_snapshot(session, now, lock=True)
                    rearm_digest = hashlib.sha256(
                        canonical_message(binding)
                    ).hexdigest()
                    if plan_digest != rearm_digest:
                        raise CompatibilityRecoveryConflict(
                            "Spark3542 compatibility recovery re-arm preview is stale"
                        )
                    return CompatibilityRecoveryPlan(
                        rearm_digest,
                        self._stored_document(existing),
                        "armed",
                    )
                else:
                    if existing.plan_digest != plan_digest:
                        raise CompatibilityRecoveryConflict(
                            "Spark3542 compatibility recovery was already armed from another plan"
                        )
                    return CompatibilityRecoveryPlan(
                        plan_digest=existing.plan_digest,
                        document=self._stored_document(existing),
                        state=existing.state,
                    )
            else:
                result = None
            if result is not None:
                pass
            else:
                document = self._snapshot(session, now, lock=True)
                expected_digest = hashlib.sha256(
                    canonical_message(document)
                ).hexdigest()
                if plan_digest != expected_digest:
                    raise CompatibilityRecoveryConflict(
                        "Spark3542 compatibility recovery preview is stale"
                    )
                job = session.get(Job, JOB_ID)
                operation = session.get(AgentOperation, OPERATION_ID)
                source = session.scalar(
                    select(AgentOperationAttempt).where(
                        AgentOperationAttempt.operation_id == OPERATION_ID,
                        AgentOperationAttempt.attempt == SOURCE_ATTEMPT,
                    )
                )
                assert job is not None and operation is not None and source is not None
                session.add(
                    AgentUpgradeCompatibilityRecovery(
                        id=RECOVERY_ID,
                        node_id=NODE_ID,
                        job_id=JOB_ID,
                        operation_id=OPERATION_ID,
                        source_attempt=SOURCE_ATTEMPT,
                        source_fence=source.fence,
                        source_certificate_serial=source.agent_certificate_serial,
                        expected_retry_attempt=SOURCE_ATTEMPT + 1,
                        source_semantic_version=SOURCE_SEMANTIC_VERSION,
                        source_build_digest=SOURCE_BUILD_DIGEST,
                        source_binary_digest=SOURCE_BINARY_DIGEST,
                        upgrade_payload_sha256=str(document["upgrade_payload_sha256"]),
                        package_sha256=TARGET_PACKAGE_SHA256,
                        target_package_version=TARGET_PACKAGE_VERSION,
                        target_build_digest=TARGET_BUILD_DIGEST,
                        target_binary_digest=TARGET_BINARY_DIGEST,
                        authority_revision=operation.authority_revision,
                        plan_digest=expected_digest,
                        state="armed",
                        actor=actor,
                        request_id=request_id,
                        created_at=now,
                    )
                )
                # This is the exact retry transition AgentJobService already
                # understands. We intentionally do not replace the operation
                # payload, fetch the current release candidate, or install a
                # package. The compatibility authority may issue only the fixed,
                # delayed reboot required to activate the staged recovery at boot.
                # Narrow the historical two-Spark rollout to Spark3542 before it is
                # resumed. Otherwise AgentUpgradeService would automatically
                # materialize the old a122 package for Spark2297 after Spark3542
                # proves the target identity.
                package = operation.payload
                job.targets = [NODE_ID]
                job.payload = {
                    "node_order": [NODE_ID],
                    "package": dict(package),
                    "strategy": "one-at-a-time",
                }
                job.payload_digest = self._job_plan_digest(
                    authority_revision=job.authority_revision,
                    node_ids=[NODE_ID],
                    package=package,
                )
                job.state = "queued"
                job.status_reason = None
                job.updated_at = now
                operation.retry_disposition = "retry"
                operation.retry_disposition_attempt = SOURCE_ATTEMPT
                operation.updated_at = now
                notify = True
                result = CompatibilityRecoveryPlan(expected_digest, document, "armed")
        if notify:
            self._notify_available()
        return result

    @staticmethod
    def _grantless_rearm_candidate(
        recovery: AgentUpgradeCompatibilityRecovery,
    ) -> bool:
        return bool(
            recovery.state == "operator-blocked"
            and recovery.blocked_at is not None
            and recovery.completed_at is None
            and recovery.retry_fence is None
            and recovery.retry_certificate_serial is None
            and recovery.signed_grant is None
            and recovery.grant_request_id is None
            and recovery.grant_expires_at is None
            and recovery.identity_deadline is None
            and recovery.issued_at is None
        )

    @staticmethod
    def _grantless_rearm_applied_candidate(
        session: Session,
        recovery: AgentUpgradeCompatibilityRecovery,
    ) -> bool:
        if not (
            recovery.state == "armed"
            and recovery.blocked_at is None
            and recovery.completed_at is None
            and recovery.retry_fence is None
            and recovery.retry_certificate_serial is None
            and recovery.signed_grant is None
            and recovery.grant_request_id is None
            and recovery.grant_expires_at is None
            and recovery.identity_deadline is None
            and recovery.issued_at is None
        ):
            return False
        retry = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == OPERATION_ID,
                AgentOperationAttempt.attempt == RETRY_ATTEMPT,
            )
        )
        return bool(
            retry is not None
            and retry.state == "running"
            and retry.result == _GRANTLESS_RETRY_FAILURE
        )

    def _rearm_snapshot(
        self, session: Session, now: datetime, *, lock: bool
    ) -> dict[str, object]:
        queries = {
            "node": select(AgentNode).where(AgentNode.node_id == NODE_ID),
            "job": select(Job).where(Job.id == JOB_ID),
            "operation": select(AgentOperation).where(
                AgentOperation.id == OPERATION_ID
            ),
            "source": select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == OPERATION_ID,
                AgentOperationAttempt.attempt == SOURCE_ATTEMPT,
            ),
            "retry": select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == OPERATION_ID,
                AgentOperationAttempt.attempt == RETRY_ATTEMPT,
            ),
            "recovery": select(AgentUpgradeCompatibilityRecovery).where(
                AgentUpgradeCompatibilityRecovery.id == RECOVERY_ID
            ),
            "dispatch_certificate": select(AgentCertificate).where(
                AgentCertificate.serial == DISPATCH_CERTIFICATE_SERIAL
            ),
        }
        if lock:
            queries = {name: query.with_for_update() for name, query in queries.items()}
        rows = {name: session.scalar(query) for name, query in queries.items()}
        node = rows["node"]
        job = rows["job"]
        operation = rows["operation"]
        source = rows["source"]
        retry = rows["retry"]
        recovery = rows["recovery"]
        dispatch_certificate = rows["dispatch_certificate"]
        if not (
            isinstance(node, AgentNode)
            and isinstance(job, Job)
            and isinstance(operation, AgentOperation)
            and isinstance(source, AgentOperationAttempt)
            and isinstance(retry, AgentOperationAttempt)
            and isinstance(recovery, AgentUpgradeCompatibilityRecovery)
            and isinstance(dispatch_certificate, AgentCertificate)
        ):
            raise CompatibilityRecoveryConflict(
                "the exact Spark3542 grantless retry is unavailable"
            )
        package = operation.payload
        payload_digest = hashlib.sha256(canonical_message(dict(package))).hexdigest()
        stored_document = self._stored_document(recovery)
        blocked_rearm = bool(
            self._grantless_rearm_candidate(recovery)
            and operation.state == "waiting-for-operator"
            and job.state == "waiting-for-operator"
            and retry.state in {"failed", "waiting-for-operator"}
            and _aware(retry.lease_deadline) <= _aware(now)
        )
        applied_rearm = bool(
            recovery.state == "armed"
            and recovery.blocked_at is None
            and recovery.completed_at is None
            and recovery.retry_fence is None
            and recovery.retry_certificate_serial is None
            and recovery.signed_grant is None
            and recovery.grant_request_id is None
            and recovery.grant_expires_at is None
            and recovery.identity_deadline is None
            and recovery.issued_at is None
            and operation.state == "running"
            and job.state == "running"
            and retry.state == "running"
            and _aware(retry.lease_deadline) > _aware(now)
        )
        if not (
            (blocked_rearm or applied_rearm)
            and recovery.id == RECOVERY_ID
            and recovery.node_id == NODE_ID
            and recovery.job_id == JOB_ID
            and recovery.operation_id == OPERATION_ID
            and recovery.source_attempt == SOURCE_ATTEMPT
            and recovery.expected_retry_attempt == RETRY_ATTEMPT
            and recovery.source_fence == source.fence
            and recovery.source_certificate_serial
            == source.agent_certificate_serial
            == SOURCE_ATTEMPT_CERTIFICATE_SERIAL
            and recovery.source_semantic_version == SOURCE_SEMANTIC_VERSION
            and recovery.source_binary_digest == SOURCE_BINARY_DIGEST
            and recovery.source_build_digest == SOURCE_BUILD_DIGEST
            and recovery.package_sha256 == TARGET_PACKAGE_SHA256
            and recovery.target_package_version == TARGET_PACKAGE_VERSION
            and recovery.target_binary_digest == TARGET_BINARY_DIGEST
            and recovery.target_build_digest == TARGET_BUILD_DIGEST
            and recovery.plan_digest
            == hashlib.sha256(canonical_message(stored_document)).hexdigest()
            and source.attempt == SOURCE_ATTEMPT
            and source.state in _TERMINAL_ATTEMPT_STATES
            and retry.attempt == RETRY_ATTEMPT
            and retry.agent_certificate_serial
            == GRANTLESS_RETRY_CERTIFICATE_SERIAL
            and retry.result == _GRANTLESS_RETRY_FAILURE
            and operation.id == OPERATION_ID
            and operation.parent_job_id == JOB_ID
            and operation.node_id == NODE_ID
            and operation.kind == "agent.upgrade.v1"
            and operation.current_attempt == RETRY_ATTEMPT
            and operation.retry_disposition is None
            and operation.retry_disposition_attempt is None
            and operation.authority_revision == recovery.authority_revision
            and operation.payload_digest == payload_digest
            and recovery.upgrade_payload_sha256 == payload_digest
            and self._exact_target(package)
            and job.id == JOB_ID
            and job.kind == "agent-upgrade"
            and job.targets == [NODE_ID]
            and job.authority_revision == recovery.authority_revision
            and set(job.payload) == {"node_order", "package", "strategy"}
            and job.payload.get("node_order") == [NODE_ID]
            and job.payload.get("package") == package
            and job.payload.get("strategy") == "one-at-a-time"
            and job.payload_digest
            == self._job_plan_digest(
                authority_revision=job.authority_revision,
                node_ids=[NODE_ID],
                package=package,
            )
            and node.state == "active"
            and node.revoked_at is None
            and node.architecture == "linux-arm64"
            and node.semantic_version == SOURCE_SEMANTIC_VERSION
            and node.binary_digest == SOURCE_BINARY_DIGEST
            and node.build_digest == SOURCE_BUILD_DIGEST
            and node.self_test_passed is True
            and node.protocol_version == 3
            and node.contact_certificate_serial == DISPATCH_CERTIFICATE_SERIAL
            and {"agent.runtime.rust.v1", "agent.upgrade.v1"}.issubset(
                set(node.capabilities or ())
            )
            and node.last_seen_at is not None
            and _aware(node.last_seen_at) <= _aware(now)
            and _aware(now) - _aware(node.last_seen_at) <= _ONLINE_WINDOW
            and dispatch_certificate.node_id == NODE_ID
            and dispatch_certificate.state == "active"
            and dispatch_certificate.revoked_at is None
            and dispatch_certificate.ca_revoked_at is None
            and _aware(dispatch_certificate.not_before) <= _aware(now)
            and _aware(dispatch_certificate.not_after) > _aware(now)
        ):
            raise CompatibilityRecoveryConflict(
                "the exact Spark3542 grantless retry is no longer recoverable"
            )
        conflicting_mutation = session.scalar(
            select(AgentOperation.id)
            .where(
                AgentOperation.node_id == NODE_ID,
                AgentOperation.id != OPERATION_ID,
                AgentOperation.state.in_({"queued", "running"}),
                AgentOperation.kind.in_(MUTATING_AGENT_OPERATIONS),
            )
            .limit(1)
        )
        if conflicting_mutation is not None:
            raise CompatibilityRecoveryConflict(
                "Spark3542 has another queued or running mutation"
            )
        return {
            "compatibility_recovery_id": RECOVERY_ID,
            "recovery_plan_digest": recovery.plan_digest,
            "replay_attempt": RETRY_ATTEMPT,
            "replay_fence": retry.fence,
            "historical_retry_certificate_serial": retry.agent_certificate_serial,
            "dispatch_certificate_serial": DISPATCH_CERTIFICATE_SERIAL,
            "failure": dict(_GRANTLESS_RETRY_FAILURE),
        }

    def _snapshot(
        self, session: Session, now: datetime, *, lock: bool
    ) -> dict[str, object]:
        node_query = select(AgentNode).where(AgentNode.node_id == NODE_ID)
        job_query = select(Job).where(Job.id == JOB_ID)
        operation_query = select(AgentOperation).where(
            AgentOperation.id == OPERATION_ID
        )
        source_query = select(AgentOperationAttempt).where(
            AgentOperationAttempt.operation_id == OPERATION_ID,
            AgentOperationAttempt.attempt == SOURCE_ATTEMPT,
        )
        if lock:
            node_query = node_query.with_for_update(of=AgentNode)
            job_query = job_query.with_for_update(of=Job)
            operation_query = operation_query.with_for_update(of=AgentOperation)
            source_query = source_query.with_for_update(of=AgentOperationAttempt)
        node = session.scalar(node_query)
        job = session.scalar(job_query)
        operation = session.scalar(operation_query)
        source = session.scalar(source_query)
        if node is None or job is None or operation is None or source is None:
            raise CompatibilityRecoveryConflict(
                "the exact Spark3542 a122 recovery transaction is unavailable"
            )
        package = operation.payload
        upgrade_payload_sha256 = hashlib.sha256(
            canonical_message(dict(package))
        ).hexdigest()
        source_certificate_query = select(AgentCertificate).where(
            AgentCertificate.serial == source.agent_certificate_serial
        )
        dispatch_certificate_query = select(AgentCertificate).where(
            AgentCertificate.serial == DISPATCH_CERTIFICATE_SERIAL
        )
        if lock:
            source_certificate_query = source_certificate_query.with_for_update(
                of=AgentCertificate
            )
            dispatch_certificate_query = dispatch_certificate_query.with_for_update(
                of=AgentCertificate
            )
        source_certificate = session.scalar(source_certificate_query)
        dispatch_certificate = session.scalar(dispatch_certificate_query)
        if operation.payload_digest != upgrade_payload_sha256:
            raise CompatibilityRecoveryConflict(
                "the existing Spark3542 operation payload digest is invalid"
            )
        if (
            node.state != "active"
            or node.revoked_at is not None
            or node.architecture != "linux-arm64"
            or node.semantic_version != SOURCE_SEMANTIC_VERSION
            or node.binary_digest != SOURCE_BINARY_DIGEST
            or node.build_digest != SOURCE_BUILD_DIGEST
            or node.self_test_passed is not True
            or node.protocol_version != 3
            or source.agent_certificate_serial != SOURCE_ATTEMPT_CERTIFICATE_SERIAL
            or node.contact_certificate_serial != DISPATCH_CERTIFICATE_SERIAL
            or "agent.upgrade.v1" not in set(node.capabilities or ())
            or "agent.runtime.rust.v1" not in set(node.capabilities or ())
            or node.last_seen_at is None
            or _aware(node.last_seen_at) > _aware(now)
            or _aware(now) - _aware(node.last_seen_at) > _ONLINE_WINDOW
            or source_certificate is None
            or source_certificate.node_id != NODE_ID
            or source_certificate.state not in {"active", "revoked"}
            or _aware(source_certificate.not_before) > _aware(source.lease_deadline)
            or _aware(source_certificate.not_after) <= _aware(source.lease_deadline)
            or (
                source_certificate.revoked_at is not None
                and _aware(source_certificate.revoked_at)
                <= _aware(source.lease_deadline)
            )
            or (
                source_certificate.ca_revoked_at is not None
                and _aware(source_certificate.ca_revoked_at)
                <= _aware(source.lease_deadline)
            )
            or dispatch_certificate is None
            or dispatch_certificate.node_id != NODE_ID
            or dispatch_certificate.state != "active"
            or dispatch_certificate.revoked_at is not None
            or dispatch_certificate.ca_revoked_at is not None
            or _aware(dispatch_certificate.not_before) > _aware(now)
            or _aware(dispatch_certificate.not_after) <= _aware(now)
        ):
            raise CompatibilityRecoveryConflict(
                "Spark3542 does not report the exact live dev335 identity"
            )
        if (
            job.kind != "agent-upgrade"
            or job.state != "waiting-for-operator"
            or job.targets != list(SOURCE_JOB_TARGETS)
            or set(job.payload) != {"node_order", "package", "strategy"}
            or job.payload.get("node_order") != list(SOURCE_JOB_TARGETS)
            or job.payload.get("strategy") != "one-at-a-time"
            or job.payload.get("package") != package
            or job.authority_revision != operation.authority_revision
            or job.payload_digest
            != self._job_plan_digest(
                authority_revision=job.authority_revision,
                node_ids=list(SOURCE_JOB_TARGETS),
                package=package,
            )
            or operation.parent_job_id != JOB_ID
            or operation.node_id != NODE_ID
            or operation.kind != "agent.upgrade.v1"
            or operation.state != "waiting-for-operator"
            or operation.current_attempt != SOURCE_ATTEMPT
            or operation.retry_disposition is not None
            or operation.retry_disposition_attempt is not None
            or source.state not in _TERMINAL_ATTEMPT_STATES
            or _aware(source.lease_deadline) > _aware(now)
        ):
            raise CompatibilityRecoveryConflict(
                "the exact Spark3542 waiting upgrade attempt is no longer recoverable"
            )
        conflicting_mutation = session.scalar(
            select(AgentOperation.id)
            .where(
                AgentOperation.node_id == NODE_ID,
                AgentOperation.id != OPERATION_ID,
                AgentOperation.state.in_({"queued", "running"}),
                AgentOperation.kind.in_(MUTATING_AGENT_OPERATIONS),
            )
            .limit(1)
        )
        if conflicting_mutation is not None:
            raise CompatibilityRecoveryConflict(
                "Spark3542 has another queued or running mutation"
            )
        if not self._exact_target(package):
            raise CompatibilityRecoveryConflict(
                "the existing Spark3542 operation is not the exact a122 package"
            )
        return {
            "action": "schedule-reboot",
            "delay_seconds": 60,
            "compatibility_recovery_id": RECOVERY_ID,
            "node_id": NODE_ID,
            "source_job_targets": list(SOURCE_JOB_TARGETS),
            "dispatch_job_targets": [NODE_ID],
            "source_identity": {
                "semantic_version": SOURCE_SEMANTIC_VERSION,
                "binary_digest": SOURCE_BINARY_DIGEST,
                "build_digest": SOURCE_BUILD_DIGEST,
            },
            "job_id": JOB_ID,
            "operation_id": OPERATION_ID,
            "source_attempt": SOURCE_ATTEMPT,
            "source_fence": source.fence,
            "source_certificate_serial": source.agent_certificate_serial,
            "dispatch_certificate_serial": DISPATCH_CERTIFICATE_SERIAL,
            "expected_retry_attempt": SOURCE_ATTEMPT + 1,
            "authority_revision": operation.authority_revision,
            "upgrade_payload_sha256": upgrade_payload_sha256,
            "target": {
                "package_version": TARGET_PACKAGE_VERSION,
                "package_sha256": TARGET_PACKAGE_SHA256,
                "target_binary_digest": TARGET_BINARY_DIGEST,
                "target_build_digest": TARGET_BUILD_DIGEST,
            },
        }

    @staticmethod
    def _exact_target(package: Mapping[str, object]) -> bool:
        return bool(
            set(package)
            == {
                "architecture",
                "package_bytes",
                "package_sha256",
                "package_signature",
                "package_url",
                "package_version",
                "schema_version",
                "target_binary_digest",
                "target_build_digest",
            }
            and package.get("architecture") == "linux-arm64"
            and package.get("schema_version") == 1
            and package.get("package_sha256") == TARGET_PACKAGE_SHA256
            and package.get("package_version") == TARGET_PACKAGE_VERSION
            and package.get("target_binary_digest") == TARGET_BINARY_DIGEST
            and package.get("target_build_digest") == TARGET_BUILD_DIGEST
            and isinstance(package.get("package_bytes"), int)
            and not isinstance(package.get("package_bytes"), bool)
            and 1 <= int(package["package_bytes"]) <= 1024**3
            and isinstance(package.get("package_signature"), str)
            and len(str(package["package_signature"])) == 128
            and all(
                character in "0123456789abcdef"
                for character in str(package["package_signature"])
            )
            and isinstance(package.get("package_url"), str)
            and str(package["package_url"]).startswith("https://install.vonkforge.ai/")
            and str(package["package_url"]).endswith("/vonk-forge-agent.deb")
            and not any(
                marker in str(package["package_url"]) for marker in ("?", "#", "@")
            )
        )

    @staticmethod
    def _job_plan_digest(
        *,
        authority_revision: str,
        node_ids: list[str],
        package: Mapping[str, object],
    ) -> str:
        return hashlib.sha256(
            canonical_message(
                {
                    "authority_revision": authority_revision,
                    "node_ids": node_ids,
                    "package": dict(package),
                    "strategy": "one-at-a-time",
                }
            )
        ).hexdigest()

    @staticmethod
    def _stored_document(
        recovery: AgentUpgradeCompatibilityRecovery,
    ) -> dict[str, object]:
        return {
            "action": "schedule-reboot",
            "delay_seconds": 60,
            "compatibility_recovery_id": recovery.id,
            "node_id": recovery.node_id,
            "source_job_targets": list(SOURCE_JOB_TARGETS),
            "dispatch_job_targets": [NODE_ID],
            "source_identity": {
                "semantic_version": recovery.source_semantic_version,
                "binary_digest": recovery.source_binary_digest,
                "build_digest": recovery.source_build_digest,
            },
            "job_id": recovery.job_id,
            "operation_id": recovery.operation_id,
            "source_attempt": recovery.source_attempt,
            "source_fence": recovery.source_fence,
            "source_certificate_serial": recovery.source_certificate_serial,
            "dispatch_certificate_serial": DISPATCH_CERTIFICATE_SERIAL,
            "expected_retry_attempt": recovery.expected_retry_attempt,
            "authority_revision": recovery.authority_revision,
            "upgrade_payload_sha256": recovery.upgrade_payload_sha256,
            "target": {
                "package_version": recovery.target_package_version,
                "package_sha256": recovery.package_sha256,
                "target_binary_digest": recovery.target_binary_digest,
                "target_build_digest": recovery.target_build_digest,
            },
        }
