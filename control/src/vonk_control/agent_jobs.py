"""Transactional, node-scoped agent operation queue with lease fencing."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    AgentClaim,
    AgentDirective,
    AgentOperation,
    AgentProgress,
    AgentResult,
    canonical_message,
)

from .agent_upgrade_status import operator_agent_upgrade_reason
from .auth import AgentSource
from .logging import redact_text
from .models import (
    AgentCertificate,
    AgentNode,
    AgentNodeProfile,
    AgentOperationAttempt,
    ArtifactJob,
    Job,
    Observation,
    RecipeBuild,
    Reconciliation,
    ReconciliationOperation,
)
from .models import AgentOperation as StoredOperation
from .recipe_builds import BUILD_ARTIFACT_FORMAT

AgentFence = str | AgentClaim | AgentProgress | AgentResult
ResultConsumer = Callable[
    [Session, StoredOperation, AgentOperationAttempt, AgentResult], None
]
ContactConsumer = Callable[[Session, AgentSource], None]


_SAFE_AUTOMATIC_RECLAIM = frozenset(
    {
        AgentOperation.NODE_PROBE.value,
        AgentOperation.WORKLOAD_HEALTH.value,
        AgentOperation.WORKLOAD_VERIFY.value,
    }
)
_RECIPE_CAPABILITIES = frozenset(
    {
        AgentOperation.RECIPE_BUILD.value,
        AgentOperation.RECIPE_IMAGE_IMPORT.value,
        AgentOperation.RECIPE_INSTALL.value,
        AgentOperation.RECIPE_START.value,
        AgentOperation.RECIPE_JOB_RUN.value,
        AgentOperation.RECIPE_STOP.value,
        AgentOperation.RECIPE_UNINSTALL.value,
    }
)
_MUTATING_OPERATIONS = frozenset(
    {
        AgentOperation.AGENT_UPGRADE.value,
        AgentOperation.RELEASE_INSTALL.value,
        AgentOperation.WORKLOAD_PREPARE.value,
        AgentOperation.WORKLOAD_START.value,
        AgentOperation.WORKLOAD_STOP.value,
        AgentOperation.RECIPE_BUILD.value,
        AgentOperation.RECIPE_IMAGE_IMPORT.value,
        AgentOperation.RECIPE_INSTALL.value,
        AgentOperation.RECIPE_START.value,
        AgentOperation.RECIPE_JOB_RUN.value,
        AgentOperation.RECIPE_STOP.value,
        AgentOperation.RECIPE_UNINSTALL.value,
    }
)
_TERMINAL_PARENT_STATES = frozenset(
    {"succeeded", "failed", "waiting-for-operator", "expired", "cancelled"}
)
_RETRY_DISPOSITION = "retry"
_DATABASE_REPOLL_SECONDS = 0.25
_REQUIRED_CAPABILITIES = frozenset(
    {
        AgentOperation.NODE_PROBE.value,
        AgentOperation.RELEASE_INSTALL.value,
        AgentOperation.WORKLOAD_HEALTH.value,
        AgentOperation.WORKLOAD_PREPARE.value,
        AgentOperation.WORKLOAD_START.value,
        AgentOperation.WORKLOAD_STOP.value,
        AgentOperation.WORKLOAD_VERIFY.value,
    }
)
_RUNTIME_CAPABILITIES = frozenset({"agent.runtime.rust.v1", "runtime.vonk.v1"})
_NEXT_CAPABILITIES = (
    _REQUIRED_CAPABILITIES | _RUNTIME_CAPABILITIES | _RECIPE_CAPABILITIES
)
_OPTIONAL_CAPABILITIES = frozenset(
    {
        AgentOperation.AGENT_UPGRADE.value,
        "recipe.model-uninstall.v1",
        "recipe.start.two-phase.v1",
        "recipe.run.inspect.exact.v1",
        "recipe.run.inspect.receipt.v1",
    }
)
_KNOWN_CAPABILITIES = _NEXT_CAPABILITIES | _OPTIONAL_CAPABILITIES
_CONTROL_OPERATIONS = (
    _NEXT_CAPABILITIES - _RUNTIME_CAPABILITIES
) | _OPTIONAL_CAPABILITIES


class StaleAgentAttempt(RuntimeError):
    """An agent attempted to update an operation it no longer owns."""


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _document(value: Mapping[str, object]) -> dict[str, object]:
    """Return the protocol's validated, deterministic JSON representation."""
    return json.loads(canonical_message(value))


def _signer_message(value: Mapping[str, object]) -> bytes:
    """Return the signer's canonical newline-delimited wire representation."""
    return canonical_message(value) + b"\n"


class AgentJobService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        result_consumer: ResultConsumer | None = None,
        contact_consumer: ContactConsumer | None = None,
        revision_eligible: Callable[[str], bool] | None = None,
        current_revision: Callable[[], str] | None = None,
    ) -> None:
        if result_consumer is not None and not callable(result_consumer):
            raise TypeError("agent result consumer must be callable")
        if contact_consumer is not None and not callable(contact_consumer):
            raise TypeError("agent contact consumer must be callable")
        if (revision_eligible is None) != (current_revision is None):
            raise ValueError("reconciliation authority is incomplete")
        self._sessions = sessions
        self._clock = clock
        self._result_consumer = result_consumer
        self._contact_consumer = contact_consumer
        self._revision_eligible = revision_eligible
        self._current_revision = current_revision
        self._configuration_lock = threading.Lock()
        self._started = False
        # SQLite ignores row locks. This only prevents same-service test races;
        # PostgreSQL correctness is provided by the database locks below.
        self._claim_lock = threading.RLock()
        self._available = threading.Condition()

    def enqueue(
        self,
        parent_job_id: str,
        node_id: str,
        operation: str,
        authority_revision: str,
        payload: Mapping[str, object],
    ) -> StoredOperation:
        with self._sessions.begin() as session:
            stored = self.enqueue_in_session(
                session,
                parent_job_id,
                node_id,
                operation,
                authority_revision,
                payload,
                operation_id=str(uuid.uuid4()),
            )
        self.notify_available()
        return stored

    def enqueue_in_session(
        self,
        session: Session,
        parent_job_id: str,
        node_id: str,
        operation: str,
        authority_revision: str,
        payload: Mapping[str, object],
        *,
        operation_id: str,
    ) -> StoredOperation:
        """Attach a caller-identified operation to the caller's transaction."""
        self._mark_started()
        now = self._clock()
        try:
            protocol_operation = AgentOperation(operation)
        except ValueError as error:
            raise ValueError(
                "agent operation is not supported by the control plane"
            ) from error
        if protocol_operation.value not in _CONTROL_OPERATIONS:
            raise ValueError("agent operation is not supported by the control plane")
        node = session.scalar(
            select(AgentNode)
            .where(AgentNode.node_id == node_id)
            .with_for_update(of=AgentNode)
        )
        if node is None:
            raise KeyError(node_id)
        if node.state != "active" or node.revoked_at is not None:
            raise ValueError("agent operation node must be active")
        if node.capabilities and operation not in set(node.capabilities):
            raise ValueError(
                f"agent does not advertise operation capability {operation}"
            )
        parent = session.scalar(
            select(Job).where(Job.id == parent_job_id).with_for_update(of=Job)
        )
        if parent is None:
            raise KeyError(parent_job_id)
        if parent.state in _TERMINAL_PARENT_STATES:
            raise ValueError(
                "cannot enqueue an agent operation beneath a terminal parent"
            )
        if parent.authority_revision != authority_revision:
            raise ValueError("agent operation authority revision must match its parent")
        if node_id not in parent.targets:
            raise ValueError("agent operation node must be a parent target")
        reserved_fence = str(uuid.uuid4())
        final_payload: Mapping[str, object] = payload
        validated = AgentClaim(
            schema_version=1,
            job_id=parent_job_id,
            operation_id=operation_id,
            attempt=1,
            fence=reserved_fence,
            node_id=node_id,
            operation=protocol_operation,
            authority_revision=authority_revision,
            payload_digest=hashlib.sha256(canonical_message(final_payload)).hexdigest(),
            payload=final_payload,
            deadline=now,
        )
        stored = StoredOperation(
            id=validated.operation_id,
            parent_job_id=parent_job_id,
            node_id=node_id,
            kind=protocol_operation.value,
            payload_digest=validated.payload_digest,
            payload=_document(validated.payload),
            authority_revision=authority_revision,
            state="queued",
            current_attempt=0,
            created_at=now,
            updated_at=now,
        )
        session.add(stored)
        session.flush()
        return stored

    def notify_available(self) -> None:
        """Wake long polls after a caller-managed enqueue transaction commits."""
        with self._available:
            self._available.notify_all()

    def set_result_consumer(self, consumer: ResultConsumer) -> None:
        """Bind projection consumption once, before the queue serves any work."""
        if not callable(consumer):
            raise TypeError("agent result consumer must be callable")
        with self._configuration_lock:
            if self._result_consumer is not None:
                raise RuntimeError("agent result consumer is already configured")
            if self._started:
                raise RuntimeError("agent job service has already started")
            self._result_consumer = consumer

    def set_contact_consumer(self, consumer: ContactConsumer) -> None:
        """Bind atomic authenticated contact persistence before serving work."""

        if not callable(consumer):
            raise TypeError("agent contact consumer must be callable")
        with self._configuration_lock:
            if self._contact_consumer is not None:
                raise RuntimeError("agent contact consumer is already configured")
            if self._started:
                raise RuntimeError("agent job service has already started")
            self._contact_consumer = consumer

    def _mark_started(self) -> None:
        with self._configuration_lock:
            self._started = True

    def claim(
        self,
        node_id: str,
        certificate_serial: str,
        lease_seconds: int,
        wait_seconds: float = 0,
        protocol_version: int | None = 3,
        capabilities: Sequence[str] | None = tuple(_NEXT_CAPABILITIES),
        *,
        runtime_identity: Mapping[str, object] | None,
        hostname: str | None = None,
        source: AgentSource | None = None,
    ) -> AgentClaim | None:
        self._mark_started()
        if (
            not node_id.strip()
            or not certificate_serial.strip()
            or lease_seconds <= 0
            or isinstance(wait_seconds, bool)
            or not 0 <= wait_seconds <= 60
            or (
                hostname is not None
                and (
                    not isinstance(hostname, str)
                    or len(hostname) > 255
                    or re.fullmatch(
                        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                        r"(?:\.[A-Za-z0-9]"
                        r"(?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*",
                        hostname,
                    )
                    is None
                )
            )
            or (
                protocol_version is not None
                and (
                    isinstance(protocol_version, bool)
                    or not isinstance(protocol_version, int)
                    or not 1 <= protocol_version <= 2_147_483_647
                )
            )
        ):
            raise ValueError("node, certificate, and positive lease are required")
        advertised = self._capabilities(capabilities)
        running = self._runtime_identity(runtime_identity)
        deadline = time.monotonic() + wait_seconds
        with self._available:
            while True:
                claim = self._claim_once(
                    node_id,
                    certificate_serial,
                    lease_seconds,
                    protocol_version,
                    advertised,
                    running,
                    hostname,
                    source,
                )
                if claim is not None:
                    return claim
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._available.wait(min(remaining, _DATABASE_REPOLL_SECONDS))

    def _claim_once(
        self,
        node_id: str,
        certificate_serial: str,
        lease_seconds: int,
        protocol_version: int | None,
        capabilities: tuple[str, ...] | None,
        runtime_identity: dict[str, object],
        hostname: str | None,
        source: AgentSource | None,
    ) -> AgentClaim | None:
        with self._claim_lock, self._sessions.begin() as session:
            reconciliation_hint = session.scalar(
                select(StoredOperation.id)
                .join(Job, Job.id == StoredOperation.parent_job_id)
                .where(
                    StoredOperation.node_id == node_id,
                    Job.reconciliation_id.is_not(None),
                    StoredOperation.state.in_(
                        {"queued", "running", "waiting-for-operator"}
                    ),
                )
                .order_by(StoredOperation.created_at, StoredOperation.id)
                .limit(1)
            )
            if reconciliation_hint is not None:
                self._lock_reconciliation_targets(session, reconciliation_hint)
            identity = self._lock_identity(session, node_id, certificate_serial)
            now = self._clock()
            if identity is None or not self._identity_is_active(*identity, now):
                return None
            node, certificate = identity
            self._validate_agent_contract(
                protocol_version, capabilities, runtime_identity
            )
            self._consume_contact(session, source, node, certificate)
            self._record_contact(
                session,
                node,
                certificate,
                now,
                protocol_version,
                capabilities,
                runtime_identity,
                hostname,
            )
            self._reconcile_agent_upgrade(
                session,
                node_id,
                certificate.serial,
                now,
                capabilities,
                runtime_identity,
            )
            expired_attempt = (
                select(AgentOperationAttempt.id)
                .where(
                    AgentOperationAttempt.operation_id == StoredOperation.id,
                    AgentOperationAttempt.attempt == StoredOperation.current_attempt,
                    AgentOperationAttempt.state == "running",
                    AgentOperationAttempt.lease_deadline <= now,
                )
                .exists()
            )
            retry_ready_attempt = (
                select(AgentOperationAttempt.id)
                .where(
                    AgentOperationAttempt.operation_id == StoredOperation.id,
                    AgentOperationAttempt.attempt == StoredOperation.current_attempt,
                    AgentOperationAttempt.state.in_(
                        {"expired", "failed", "waiting-for-operator"}
                    ),
                    AgentOperationAttempt.lease_deadline <= now,
                )
                .exists()
            )
            while True:
                statement = (
                    select(StoredOperation)
                    .where(
                        StoredOperation.node_id == node_id,
                        or_(
                            and_(
                                StoredOperation.state == "queued",
                                StoredOperation.current_attempt == 0,
                            ),
                            and_(
                                StoredOperation.state == "running",
                                expired_attempt,
                            ),
                            and_(
                                StoredOperation.state == "waiting-for-operator",
                                StoredOperation.retry_disposition == _RETRY_DISPOSITION,
                                StoredOperation.retry_disposition_attempt
                                == StoredOperation.current_attempt,
                                retry_ready_attempt,
                            ),
                        ),
                    )
                    .order_by(StoredOperation.created_at, StoredOperation.id)
                    .with_for_update(of=StoredOperation, skip_locked=True)
                    .execution_options(populate_existing=True)
                    .limit(1)
                )
                operation = session.scalars(statement).first()
                if operation is None:
                    return None
                if self._claim_has_authority(session, operation, now):
                    break
                return None
            if capabilities is not None and operation.kind not in capabilities:
                return None
            if operation.kind in _RECIPE_CAPABILITIES and (
                protocol_version != 3 or capabilities is None
            ):
                return None
            if (
                operation.kind == AgentOperation.RECIPE_BUILD.value
                and not self._recipe_build_runtime_matches(
                    session, operation, runtime_identity
                )
            ):
                self._reject_recipe_build_claim(
                    session, operation, certificate_serial, now
                )
                return None
            if operation.kind in _MUTATING_OPERATIONS:
                active_mutation = session.scalar(
                    select(StoredOperation.id)
                    .where(
                        StoredOperation.node_id == node_id,
                        StoredOperation.id != operation.id,
                        StoredOperation.kind.in_(_MUTATING_OPERATIONS),
                        StoredOperation.state == "running",
                    )
                    .limit(1)
                )
                if active_mutation is not None:
                    return None
            if operation.current_attempt:
                previous = session.scalar(
                    select(AgentOperationAttempt)
                    .where(
                        AgentOperationAttempt.operation_id == operation.id,
                        AgentOperationAttempt.attempt == operation.current_attempt,
                    )
                    .with_for_update(of=AgentOperationAttempt)
                )
                if previous is not None and previous.state in {
                    "running",
                    "waiting-for-operator",
                }:
                    previous.state = "expired"
            if (
                operation.state == "running"
                and operation.kind not in _SAFE_AUTOMATIC_RECLAIM
            ):
                operation.state = "waiting-for-operator"
                operation.retry_disposition = None
                operation.retry_disposition_attempt = None
                operation.updated_at = now
                if not self._project_unsafe_expiry(session, operation, now):
                    self._project_artifact_job_expiry(session, operation, now)
                    self._aggregate_parent(session, operation.parent_job_id)
                return None
            operation.current_attempt += 1
            operation.state = "running"
            operation.updated_at = now
            fence = str(uuid.uuid4())
            deadline = now + timedelta(seconds=lease_seconds)
            attempt = AgentOperationAttempt(
                operation_id=operation.id,
                attempt=operation.current_attempt,
                fence=fence,
                lease_deadline=deadline,
                agent_certificate_serial=certificate_serial,
                state="running",
            )
            session.add(attempt)
            return AgentClaim(
                schema_version=1,
                job_id=operation.parent_job_id,
                operation_id=operation.id,
                attempt=attempt.attempt,
                fence=attempt.fence,
                node_id=operation.node_id,
                operation=AgentOperation(operation.kind),
                authority_revision=operation.authority_revision,
                payload_digest=operation.payload_digest,
                payload=operation.payload,
                deadline=deadline,
            )

    def _reconcile_agent_upgrade(
        self,
        session: Session,
        node_id: str,
        certificate_serial: str,
        now: datetime,
        capabilities: tuple[str, ...] | None,
        runtime_identity: Mapping[str, object],
    ) -> None:
        if (
            capabilities is None
            or AgentOperation.AGENT_UPGRADE.value not in capabilities
        ):
            return
        operation = session.scalar(
            select(StoredOperation)
            .where(
                StoredOperation.node_id == node_id,
                StoredOperation.kind == AgentOperation.AGENT_UPGRADE.value,
                StoredOperation.state.in_(
                    {"queued", "running", "waiting-for-operator"}
                ),
            )
            .order_by(StoredOperation.created_at, StoredOperation.id)
            .with_for_update(of=StoredOperation)
            .limit(1)
        )
        if operation is None or (
            runtime_identity.get("build_digest")
            != operation.payload.get("target_build_digest")
            or runtime_identity.get("binary_digest")
            != operation.payload.get("target_binary_digest")
            or runtime_identity.get("architecture")
            != operation.payload.get("architecture")
            or runtime_identity.get("self_test_passed") is not True
        ):
            return
        evidence = {
            "architecture": runtime_identity["architecture"],
            "binary_digest": runtime_identity["binary_digest"],
            "build_digest": runtime_identity["build_digest"],
            "package_sha256": operation.payload["package_sha256"],
            "package_version": operation.payload["package_version"],
            "self_test_passed": True,
            "status": "upgraded",
        }
        attempt = session.scalar(
            select(AgentOperationAttempt)
            .where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == operation.current_attempt,
            )
            .with_for_update(of=AgentOperationAttempt)
        )
        if operation.state == "queued" and operation.current_attempt == 0:
            operation.current_attempt = 1
            attempt = AgentOperationAttempt(
                operation_id=operation.id,
                attempt=1,
                fence=str(uuid.uuid4()),
                lease_deadline=now,
                agent_certificate_serial=certificate_serial,
                state="succeeded",
                result=_document(evidence),
            )
            session.add(attempt)
        elif attempt is None or attempt.state not in {
            "running",
            "waiting-for-operator",
            "expired",
            "failed",
        }:
            return
        message = AgentResult(
            schema_version=1,
            job_id=operation.parent_job_id,
            operation_id=operation.id,
            attempt=attempt.attempt,
            fence=attempt.fence,
            node_id=operation.node_id,
            deadline=max(_aware(attempt.lease_deadline), _aware(now)),
            state="succeeded",
            result=evidence,
        )
        # Preserve explicit helper failures as truthful attempt audit. Exact
        # contact reconciles the operation projection, not the historical fact
        # that the signed helper attempt returned failure.
        if attempt.state != "failed":
            attempt.state = "succeeded"
            attempt.result = _document(evidence)
        operation.state = "succeeded"
        operation.retry_disposition = None
        operation.retry_disposition_attempt = None
        operation.updated_at = now
        if self._result_consumer is not None:
            self._result_consumer(session, operation, attempt, message)
        self._aggregate_parent(session, operation.parent_job_id)

    @staticmethod
    def _recipe_build_runtime_matches(
        session: Session,
        operation: StoredOperation,
        runtime_identity: Mapping[str, object],
    ) -> bool:
        build_id = operation.payload.get("build_id")
        build = (
            session.get(RecipeBuild, build_id) if isinstance(build_id, str) else None
        )
        report = build.policy_report if build is not None else None
        return bool(
            build is not None
            and build.builder_node_id == operation.node_id
            and isinstance(report, dict)
            and runtime_identity is not None
            and report.get("builder_binary_digest")
            == runtime_identity.get("binary_digest")
            and report.get("artifact_format") == BUILD_ARTIFACT_FORMAT
        )

    def _reject_recipe_build_claim(
        self,
        session: Session,
        operation: StoredOperation,
        certificate_serial: str,
        now: datetime,
    ) -> None:
        operation.current_attempt += 1
        operation.state = "failed"
        operation.retry_disposition = None
        operation.retry_disposition_attempt = None
        operation.updated_at = now
        reason = {"reason": "builder runtime identity changed before claim"}
        fence = str(uuid.uuid4())
        attempt = AgentOperationAttempt(
            operation_id=operation.id,
            attempt=operation.current_attempt,
            fence=fence,
            lease_deadline=now,
            agent_certificate_serial=certificate_serial,
            state="failed",
            result=reason,
        )
        session.add(attempt)
        session.flush()
        if self._result_consumer is not None:
            self._result_consumer(
                session,
                operation,
                attempt,
                AgentResult(
                    schema_version=1,
                    job_id=operation.parent_job_id,
                    operation_id=operation.id,
                    attempt=attempt.attempt,
                    fence=fence,
                    node_id=operation.node_id,
                    deadline=_aware(now),
                    state="failed",
                    result=reason,
                ),
            )
        self._aggregate_parent(session, operation.parent_job_id)

    def _claim_has_authority(
        self,
        session: Session,
        operation: StoredOperation,
        now: datetime,
    ) -> bool:
        job = session.scalar(
            select(Job).where(Job.id == operation.parent_job_id).with_for_update(of=Job)
        )
        if job is None:
            raise ValueError("agent operation lacks its parent job")
        if job.reconciliation_id is None:
            if (
                job.state == "waiting-for-operator"
                and operation.state == "waiting-for-operator"
                and operation.retry_disposition == _RETRY_DISPOSITION
                and operation.retry_disposition_attempt == operation.current_attempt
            ):
                job.state = "queued"
                job.status_reason = None
                job.updated_at = now
                return True
            return job.state not in _TERMINAL_PARENT_STATES
        reconciliation = session.scalar(
            select(Reconciliation)
            .where(Reconciliation.id == job.reconciliation_id)
            .with_for_update(of=Reconciliation)
        )
        projection = session.scalar(
            select(ReconciliationOperation)
            .where(
                ReconciliationOperation.reconciliation_id == job.reconciliation_id,
                ReconciliationOperation.agent_operation_id == operation.id,
            )
            .with_for_update(of=ReconciliationOperation)
        )
        if reconciliation is None or projection is None:
            raise ValueError("agent operation lacks reconciliation authority")
        expected_phase = (
            "compensating" if projection.role == "compensation" else "dispatching"
        )
        if (
            job.state == "running"
            and reconciliation.status == "running"
            and reconciliation.current_phase == expected_phase
            and projection.state in {"queued", "running"}
            and self._continuous_authority_reason(
                session, reconciliation, job, operation
            )
            is None
        ):
            return True
        self._quiesce_reconciliation_operations(
            session,
            reconciliation.id,
            now,
        )
        reason = "reconciliation execution authority is no longer eligible"
        reconciliation.current_phase = "waiting-for-operator"
        reconciliation.status = "failed"
        reconciliation.terminal_reason = reason
        job.state = "waiting-for-operator"
        job.status_reason = reason
        job.updated_at = now
        return False

    def _continuous_authority_reason(
        self,
        session: Session,
        reconciliation: Reconciliation,
        job: Job,
        operation: StoredOperation,
    ) -> str | None:
        if self._revision_eligible is None or self._current_revision is None:
            return None
        try:
            if (
                not self._revision_eligible(reconciliation.authority_revision)
                or self._current_revision() != reconciliation.authority_revision
            ):
                return "reconciliation authority revision is no longer eligible"
        except (OSError, RuntimeError, TypeError, ValueError):
            return "reconciliation authority revision eligibility is unavailable"
        document = reconciliation.resolved_plan
        if not isinstance(document, Mapping):
            return "reconciliation plan authority is unavailable"
        protocol = document.get("agent_protocol_range")
        targets = document.get("targets")
        if (
            not isinstance(protocol, list)
            or len(protocol) != 2
            or not all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in protocol
            )
            or not isinstance(targets, list)
            or targets != job.targets
            or operation.authority_revision != reconciliation.authority_revision
        ):
            return "reconciliation plan authority is invalid"
        nodes = list(
            session.scalars(
                select(AgentNode)
                .where(AgentNode.node_id.in_(targets))
                .order_by(AgentNode.node_id)
            )
        )
        if [node.node_id for node in nodes] != sorted(targets):
            return "reconciliation target set is unavailable"
        if any(
            node.state != "active"
            or node.revoked_at is not None
            or not isinstance(node.protocol_version, int)
            or isinstance(node.protocol_version, bool)
            or not protocol[0] <= node.protocol_version <= protocol[1]
            or not isinstance(node.capabilities, list)
            # Stored capability lists can come from a newer Spark.  The
            # operation claim below already intersects them with the
            # Controller's known operations; only the stable required set is
            # a reconciliation prerequisite.
            or not _REQUIRED_CAPABILITIES <= set(node.capabilities)
            for node in nodes
        ):
            return "reconciliation target agent is incompatible"
        return None

    @staticmethod
    def _quiesce_reconciliation_operations(
        session: Session,
        reconciliation_id: str,
        now: datetime,
    ) -> None:
        projections = list(
            session.scalars(
                select(ReconciliationOperation)
                .where(ReconciliationOperation.reconciliation_id == reconciliation_id)
                .order_by(
                    ReconciliationOperation.graph_operation_id,
                    ReconciliationOperation.role,
                )
                .with_for_update(of=ReconciliationOperation)
            )
        )
        for projection in projections:
            if projection.state == "planned":
                projection.state = "failed"
                continue
            if projection.agent_operation_id is None:
                continue
            candidate = session.scalar(
                select(StoredOperation)
                .where(StoredOperation.id == projection.agent_operation_id)
                .with_for_update(of=StoredOperation)
            )
            if candidate is None:
                raise ValueError("reconciliation operation projection is incomplete")
            if candidate.state == "queued":
                candidate.state = "failed"
                projection.state = "failed"
                candidate.updated_at = now
                continue
            if candidate.state != "running":
                continue
            attempt = session.scalar(
                select(AgentOperationAttempt)
                .where(
                    AgentOperationAttempt.operation_id == candidate.id,
                    AgentOperationAttempt.attempt == candidate.current_attempt,
                )
                .with_for_update(of=AgentOperationAttempt)
            )
            if attempt is None or attempt.state != "running":
                raise ValueError("running reconciliation operation lacks its attempt")
            terminal = (
                "waiting-for-operator"
                if candidate.kind in _MUTATING_OPERATIONS
                else "failed"
            )
            candidate.state = terminal
            attempt.state = terminal
            projection.state = terminal
            candidate.updated_at = now

    def heartbeat(
        self,
        fence: AgentFence,
        progress: Mapping[str, object],
        lease_seconds: int,
        *,
        source: AgentSource | None = None,
    ) -> AgentDirective:
        self._mark_started()
        if lease_seconds <= 0:
            raise ValueError("lease must be positive")
        with self._sessions.begin() as session:
            operation, attempt = self._active(session, fence, source=source)
            now = self._clock()
            deadline = max(
                _aware(attempt.lease_deadline),
                _aware(now) + timedelta(seconds=lease_seconds),
            )
            message = AgentProgress(
                schema_version=1,
                job_id=operation.parent_job_id,
                operation_id=operation.id,
                attempt=attempt.attempt,
                fence=attempt.fence,
                node_id=operation.node_id,
                deadline=deadline,
                progress=progress,
            )
            attempt.progress = _document(message.progress)
            attempt.lease_deadline = deadline
            operation.updated_at = now
            parent = session.get(Job, operation.parent_job_id)
            cancel_requested = bool(
                parent is not None
                and isinstance(parent.result, Mapping)
                and parent.result.get("cancel_requested") is True
            )
            return AgentDirective(
                schema_version=message.schema_version,
                job_id=message.job_id,
                operation_id=message.operation_id,
                attempt=message.attempt,
                fence=message.fence,
                node_id=message.node_id,
                deadline=message.deadline,
                cancel_requested=cancel_requested,
            )

    def succeed(self, fence: AgentFence, result: Mapping[str, object]) -> None:
        self._finish(fence, "succeeded", result=result, reason=None)

    def fail(self, fence: AgentFence, reason: str) -> None:
        self._finish(fence, "failed", result=None, reason=reason)

    def wait_for_operator(self, fence: AgentFence, reason: str) -> None:
        self._finish(fence, "waiting-for-operator", result=None, reason=reason)

    def record_result(
        self, message: AgentResult, *, source: AgentSource | None = None
    ) -> None:
        """Persist one exact agent result and consume it in the same transaction."""
        self._finish(
            message,
            message.state,
            result=message.result,
            reason=None,
            source=source,
        )

    def _finish(
        self,
        fence: AgentFence,
        state: str,
        *,
        result: Mapping[str, object] | None,
        reason: str | None,
        source: AgentSource | None = None,
    ) -> None:
        self._mark_started()
        with self._sessions.begin() as session:
            operation, attempt = self._active(session, fence, source=source)
            now = self._clock()
            if isinstance(fence, AgentResult):
                if fence.state != state or (
                    result is not None and _document(fence.result) != _document(result)
                ):
                    raise ValueError("agent result does not match requested completion")
                message = fence
            else:
                canonical_result = (
                    result if result is not None else {"reason": self._reason(reason)}
                )
                message = AgentResult(
                    schema_version=1,
                    job_id=operation.parent_job_id,
                    operation_id=operation.id,
                    attempt=attempt.attempt,
                    fence=attempt.fence,
                    node_id=operation.node_id,
                    deadline=_aware(attempt.lease_deadline),
                    state=state,
                    result=canonical_result,
                )
            attempt.result = _document(message.result)
            if (
                result is not None
                and state == "succeeded"
                and operation.kind == AgentOperation.NODE_PROBE.value
            ):
                health = self._probe_health(message.result)
                if (
                    operation.payload == {"require_active_nvidia_compute_processes": 0}
                    and health["active_nvidia_compute_processes"] != 0
                ):
                    raise ValueError("node probe compute gate is unsatisfied")
                session.add(
                    Observation(
                        node_id=operation.node_id,
                        kind="health",
                        payload=health,
                        observed_at=now,
                    )
                )
            attempt.state = state
            operation.state = state
            operation.updated_at = now
            if self._result_consumer is not None:
                self._result_consumer(session, operation, attempt, message)
            self._aggregate_parent(session, operation.parent_job_id)
        # A result consumer can atomically make the next durable recipe phase
        # queueable; wake long-polling agents only after that transaction commits.
        self.notify_available()

    def _active(
        self,
        session: Session,
        fence: AgentFence,
        *,
        source: AgentSource | None = None,
    ) -> tuple[StoredOperation, AgentOperationAttempt]:
        token = self._fence_token(fence)
        identity_hint = session.execute(
            select(
                StoredOperation.id,
                StoredOperation.node_id,
                AgentOperationAttempt.agent_certificate_serial,
                StoredOperation.parent_job_id,
            )
            .join(
                AgentOperationAttempt,
                AgentOperationAttempt.operation_id == StoredOperation.id,
            )
            .where(AgentOperationAttempt.fence == token)
        ).one_or_none()
        if identity_hint is None:
            raise StaleAgentAttempt(
                "agent operation lease, certificate, or fence is stale"
            )
        operation_id, node_id, certificate_serial, parent_job_id = identity_hint
        self._lock_reconciliation_targets(session, operation_id)
        identity = self._lock_identity(session, node_id, certificate_serial)
        now = self._clock()
        if identity is None or not self._identity_is_active(*identity, now):
            raise StaleAgentAttempt(
                "agent operation lease, certificate, or fence is stale"
            )
        node, certificate = identity
        self._consume_contact(session, source, node, certificate)
        self._require_active_reconciliation_authority(
            session,
            operation_id,
        )
        parent = session.scalar(
            select(Job).where(Job.id == parent_job_id).with_for_update(of=Job)
        )
        if parent is None or parent.state not in {"queued", "running"}:
            raise StaleAgentAttempt(
                "agent operation lease, certificate, or fence is stale"
            )
        operation = session.scalar(
            select(StoredOperation)
            .where(StoredOperation.id == operation_id)
            .with_for_update(of=StoredOperation)
        )
        if operation is None:
            raise StaleAgentAttempt(
                "agent operation lease, certificate, or fence is stale"
            )
        attempt = session.scalar(
            select(AgentOperationAttempt)
            .where(
                AgentOperationAttempt.fence == token,
                AgentOperationAttempt.operation_id == operation.id,
            )
            .with_for_update(of=AgentOperationAttempt)
        )
        if (
            attempt is None
            or operation.state != "running"
            or (not isinstance(fence, str) and operation.parent_job_id != fence.job_id)
            or (not isinstance(fence, str) and operation.id != fence.operation_id)
            or (not isinstance(fence, str) and operation.node_id != fence.node_id)
            or (
                not isinstance(fence, str)
                and operation.current_attempt != fence.attempt
            )
            or attempt.operation_id != operation.id
            or operation.current_attempt != attempt.attempt
            or attempt.state != "running"
            or _aware(attempt.lease_deadline) <= _aware(now)
        ):
            raise StaleAgentAttempt(
                "agent operation lease, certificate, or fence is stale"
            )
        self._record_contact(
            session,
            node,
            certificate,
            now,
            None,
            None,
            None,
            None,
        )
        return operation, attempt

    @staticmethod
    def _require_active_reconciliation_authority(
        session: Session,
        operation_id: str,
    ) -> None:
        authority = session.execute(
            select(Job.id, Job.reconciliation_id)
            .join(StoredOperation, StoredOperation.parent_job_id == Job.id)
            .where(StoredOperation.id == operation_id)
        ).one_or_none()
        if authority is None:
            raise StaleAgentAttempt(
                "agent operation lease, certificate, or fence is stale"
            )
        job_id, reconciliation_id = authority
        if reconciliation_id is None:
            return
        reconciliation = session.scalar(
            select(Reconciliation)
            .where(Reconciliation.id == reconciliation_id)
            .with_for_update(of=Reconciliation)
        )
        job = session.scalar(
            select(Job)
            .where(
                Job.id == job_id,
                Job.reconciliation_id == reconciliation_id,
            )
            .with_for_update(of=Job)
        )
        projection = session.scalar(
            select(ReconciliationOperation)
            .where(
                ReconciliationOperation.reconciliation_id == reconciliation_id,
                ReconciliationOperation.agent_operation_id == operation_id,
            )
            .with_for_update(of=ReconciliationOperation)
        )
        expected_phase = (
            None
            if projection is None
            else "compensating"
            if projection.role == "compensation"
            else "dispatching"
        )
        if (
            reconciliation is None
            or job is None
            or projection is None
            or job.state != "running"
            or reconciliation.status != "running"
            or reconciliation.current_phase != expected_phase
            or projection.state not in {"queued", "running"}
        ):
            raise StaleAgentAttempt(
                "agent operation lease, certificate, or fence is stale"
            )

    @staticmethod
    def _capabilities(
        capabilities: Sequence[str] | None,
    ) -> tuple[str, ...] | None:
        """Normalize the negotiated capability intersection.

        Agents may be newer than the Controller and advertise capabilities this
        Controller does not know yet.  Those capabilities are intentionally
        ignored for this session; operation dispatch already checks the
        normalized set, so the effective contract is the intersection of both
        sides.  Required capabilities are still enforced by
        ``_validate_agent_contract`` below.
        """
        if capabilities is None:
            return None
        if isinstance(capabilities, (str, bytes)):
            raise TypeError("agent capabilities are invalid")
        values = tuple(capabilities)
        if not values or any(not isinstance(value, str) or not value for value in values):
            raise ValueError("agent capabilities are invalid")
        return tuple(sorted(set(values) & _KNOWN_CAPABILITIES))

    @staticmethod
    def _validate_agent_contract(
        protocol_version: int | None,
        capabilities: tuple[str, ...] | None,
        runtime_identity: Mapping[str, object] | None,
    ) -> None:
        if (
            protocol_version is None
            or protocol_version != 3
            or capabilities is None
            or "agent.runtime.rust.v1" not in capabilities
        ):
            raise ValueError("Rust agent capability negotiation is incomplete")
        receipt_key = runtime_identity.get("observation_receipt_public_key")
        receipt_capable = "recipe.run.inspect.receipt.v1" in capabilities
        if receipt_capable and not (
            isinstance(receipt_key, str) and len(receipt_key) == 64
        ):
            raise ValueError("agent observation receipt identity is incomplete")

    @staticmethod
    def _record_contact(
        session: Session,
        node: AgentNode,
        certificate: AgentCertificate,
        now: datetime,
        protocol_version: int | None,
        capabilities: tuple[str, ...] | None,
        runtime_identity: dict[str, object] | None,
        hostname: str | None,
    ) -> None:
        current = None if node.last_seen_at is None else _aware(node.last_seen_at)
        observed = _aware(now)
        if current is None or observed > current:
            node.last_seen_at = observed
        if protocol_version is not None:
            node.protocol_version = protocol_version
        if capabilities is not None:
            node.capabilities = list(capabilities)
        if hostname is not None:
            profile = session.scalar(
                select(AgentNodeProfile)
                .where(AgentNodeProfile.node_id == node.node_id)
                .with_for_update(of=AgentNodeProfile)
            )
            if profile is not None and profile.hostname != hostname:
                profile.hostname = hostname
        if runtime_identity is not None:
            receipt_key = runtime_identity.get("observation_receipt_public_key")
            if (
                receipt_key is not None
                and node.observation_receipt_public_key is not None
                and node.observation_receipt_public_key != receipt_key
            ):
                raise ValueError("agent observation receipt key changed")
            if (
                isinstance(receipt_key, str)
                and node.observation_receipt_public_key is None
            ):
                # Nodes enrolled before signed run observations existed acquire
                # their immutable receipt identity on the first authenticated
                # contact from a capable upgraded agent.  Subsequent contacts
                # remain change-protected by the check above.
                node.observation_receipt_public_key = receipt_key
            node.architecture = str(runtime_identity["architecture"])
            node.semantic_version = str(runtime_identity["semantic_version"])
            node.build_digest = str(runtime_identity["build_digest"])
            node.binary_digest = str(runtime_identity["binary_digest"])
            node.self_test_passed = bool(runtime_identity["self_test_passed"])
            node.contact_certificate_serial = certificate.serial
            node.contact_observation_digest = hashlib.sha256(
                canonical_message(
                    {
                        "certificate_fingerprint": certificate.fingerprint,
                        "certificate_serial": certificate.serial,
                        "node_id": node.node_id,
                        "observed_at": _aware(node.last_seen_at).isoformat(),
                        "hostname": hostname,
                        "runtime_identity": runtime_identity,
                    }
                )
            ).hexdigest()

    @staticmethod
    def _runtime_identity(
        value: Mapping[str, object] | None,
    ) -> dict[str, object]:
        if value is None:
            raise ValueError("agent runtime identity is required")
        if not isinstance(value, Mapping):
            raise TypeError("agent runtime identity is invalid")
        document = dict(value)
        if {
            "active_slot",
            "agent_sha256",
            "platform_version",
            "supervisor_generation",
            "supervisor_ready_generation",
            "activation_deadline",
        } & document.keys():
            raise ValueError("retired runtime identity fields are not supported")
        required = {
            "architecture",
            "binary_digest",
            "build_digest",
            "semantic_version",
            "self_test_passed",
        }
        # Keep the identity envelope forward-compatible.  Only the stable
        # fields are persisted/validated; newer agents may attach additional
        # evidence without making an otherwise compatible claim unusable.
        if (
            not required <= document.keys()
            or document["architecture"] not in {"linux-amd64", "linux-arm64"}
            or not isinstance(document["architecture"], str)
            or not isinstance(document["binary_digest"], str)
            or re.fullmatch(r"[0-9a-f]{64}", document["binary_digest"]) is None
            or not isinstance(document["build_digest"], str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", document["build_digest"]) is None
            or not isinstance(document["semantic_version"], str)
            or re.fullmatch(
                r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)",
                document["semantic_version"],
            )
            is None
            or document["self_test_passed"] is not True
            or (
                document.get("observation_receipt_public_key") is not None
                and (
                    not isinstance(document["observation_receipt_public_key"], str)
                    or re.fullmatch(
                        r"[0-9a-f]{64}",
                        document["observation_receipt_public_key"],
                    )
                    is None
                )
            )
        ):
            raise ValueError("agent runtime identity is invalid")
        return {
            key: document[key]
            for key in (
                *sorted(required),
                "observation_receipt_public_key",
            )
            if key in document
        }

    def _consume_contact(
        self,
        session: Session,
        source: AgentSource | None,
        node: AgentNode,
        certificate: AgentCertificate,
    ) -> None:
        if source is None:
            return
        identity = source.identity
        if (
            identity.node_id != node.node_id
            or identity.certificate_serial != certificate.serial
            or identity.certificate_fingerprint != certificate.fingerprint
            or identity.verified is not True
        ):
            raise ValueError("agent contact source does not match its locked identity")
        if self._contact_consumer is None:
            raise RuntimeError("agent contact consumer is not configured")
        self._contact_consumer(session, source)

    @staticmethod
    def _lock_reconciliation_targets(session: Session, operation_id: str) -> None:
        authority = session.execute(
            select(Job.reconciliation_id, Job.targets)
            .join(StoredOperation, StoredOperation.parent_job_id == Job.id)
            .where(StoredOperation.id == operation_id)
        ).one_or_none()
        if authority is None or authority.reconciliation_id is None:
            return
        targets = authority.targets
        if (
            not isinstance(targets, list)
            or not targets
            or len(targets) != len(set(targets))
            or not all(isinstance(node_id, str) for node_id in targets)
        ):
            raise ValueError("reconciliation parent targets are invalid")
        locked = list(
            session.scalars(
                select(AgentNode)
                .where(AgentNode.node_id.in_(targets))
                .order_by(AgentNode.node_id)
                .with_for_update(of=AgentNode)
            )
        )
        if [node.node_id for node in locked] != sorted(targets):
            raise StaleAgentAttempt(
                "agent operation lease, certificate, or fence is stale"
            )

    def _project_unsafe_expiry(
        self,
        session: Session,
        operation: StoredOperation,
        now: datetime,
    ) -> bool:
        hint = session.scalar(
            select(ReconciliationOperation).where(
                ReconciliationOperation.agent_operation_id == operation.id
            )
        )
        if hint is None:
            return False
        reconciliation = session.scalar(
            select(Reconciliation)
            .where(Reconciliation.id == hint.reconciliation_id)
            .with_for_update(of=Reconciliation)
        )
        job = session.scalar(
            select(Job)
            .where(
                Job.id == operation.parent_job_id,
                Job.reconciliation_id == hint.reconciliation_id,
            )
            .with_for_update(of=Job)
        )
        projection = session.scalar(
            select(ReconciliationOperation)
            .where(ReconciliationOperation.id == hint.id)
            .with_for_update(of=ReconciliationOperation)
        )
        if reconciliation is None or job is None or projection is None:
            raise ValueError("unsafe agent expiry lacks reconciliation authority")
        reason = "mutating agent operation lease expired with uncertain outcome"
        projection.state = "waiting-for-operator"
        self._quiesce_reconciliation_operations(
            session,
            reconciliation.id,
            now,
        )
        reconciliation.current_phase = "waiting-for-operator"
        reconciliation.status = "failed"
        reconciliation.terminal_reason = reason
        job.state = "waiting-for-operator"
        job.status_reason = reason
        job.updated_at = now
        return True

    @staticmethod
    def _project_artifact_job_expiry(
        session: Session,
        operation: StoredOperation,
        now: datetime,
    ) -> bool:
        if operation.kind != AgentOperation.RECIPE_JOB_RUN.value:
            return False
        artifact_job = session.scalar(
            select(ArtifactJob)
            .where(ArtifactJob.operation_id == operation.parent_job_id)
            .with_for_update(of=ArtifactJob)
        )
        if artifact_job is None:
            return False
        reason = (
            "artifact job agent lease expired; the uncertain attempt was fenced "
            "and late results will be rejected"
        )
        operation.state = "failed"
        operation.updated_at = now
        if artifact_job.state not in {"succeeded", "failed", "cancelled"}:
            artifact_job.state = "failed"
            artifact_job.status_reason = reason
            artifact_job.result_evidence = {
                "failure_kind": "agent-lease-expired",
                "recoverable": True,
                "late_results_accepted": False,
            }
            artifact_job.completed_at = now
            artifact_job.updated_at = now
        return True

    @staticmethod
    def _probe_health(result: Mapping[str, object]) -> dict[str, object]:
        if set(result) != {"status", "evidence"} or result.get("status") != "ok":
            raise ValueError("successful node probe result is invalid")
        evidence = result.get("evidence")
        if not isinstance(evidence, Mapping) or set(evidence) != {
            "vonk_forge",
            "nvidia",
        }:
            raise ValueError("successful node probe evidence is invalid")
        health = evidence.get("vonk_forge")
        nvidia = evidence.get("nvidia")
        if (
            not isinstance(health, Mapping)
            or health.get("schema_version") != 1
            or not isinstance(nvidia, Mapping)
        ):
            raise ValueError("successful node probe evidence is invalid")
        memory = health.get("memory")
        storage = health.get("storage")
        accelerator = health.get("accelerator")
        memory_available = (
            memory.get("available_bytes") if isinstance(memory, Mapping) else None
        )
        disk_available = (
            storage.get("available_bytes") if isinstance(storage, Mapping) else None
        )
        memory_total = (
            memory.get("total_bytes") if isinstance(memory, Mapping) else None
        )
        disk_total = (
            storage.get("total_bytes") if isinstance(storage, Mapping) else None
        )
        accelerator_available = (
            accelerator.get("available") if isinstance(accelerator, Mapping) else False
        )
        raw_compute_processes = (
            accelerator.get("active_nvidia_compute_processes")
            if isinstance(accelerator, Mapping)
            else None
        )
        if (
            not isinstance(memory_available, int)
            or isinstance(memory_available, bool)
            or not 0 <= memory_available <= 2**63 - 1
            or not isinstance(disk_available, int)
            or isinstance(disk_available, bool)
            or not 0 <= disk_available <= 2**63 - 1
            or (
                memory_total is not None
                and (
                    not isinstance(memory_total, int)
                    or isinstance(memory_total, bool)
                    or not memory_available <= memory_total <= 2**63 - 1
                )
            )
            or (
                disk_total is not None
                and (
                    not isinstance(disk_total, int)
                    or isinstance(disk_total, bool)
                    or not disk_available <= disk_total <= 2**63 - 1
                )
            )
            or not isinstance(accelerator_available, bool)
        ):
            raise ValueError("successful node probe capacity is invalid")
        tools = nvidia.get("tools", {})
        if not isinstance(tools, Mapping):
            raise TypeError("successful node probe tool evidence is invalid")
        warning = any(
            not isinstance(item, Mapping) or item.get("status") != "ok"
            for item in tools.values()
        )
        status = (
            "critical"
            if accelerator_available is False
            else "warning"
            if warning
            else "healthy"
        )
        observation: dict[str, object] = {
            "status": status,
            "memory_available_bytes": memory_available,
            "disk_available_bytes": disk_available,
        }
        compute_processes = (
            raw_compute_processes
            if accelerator_available is True
            and isinstance(raw_compute_processes, int)
            and not isinstance(raw_compute_processes, bool)
            and 0 <= raw_compute_processes <= 65535
            else None
        )
        if compute_processes is None and observation["status"] == "healthy":
            observation["status"] = "warning"
        observation["active_nvidia_compute_processes"] = compute_processes
        observation["compute_occupancy"] = (
            "unknown"
            if compute_processes is None
            else "clean"
            if compute_processes == 0
            else "active"
        )
        if memory_total is not None:
            observation["memory_total_bytes"] = memory_total
        if disk_total is not None:
            observation["disk_total_bytes"] = disk_total
        if len(canonical_message(observation)) > 1024:
            raise ValueError("node probe health observation is too large")
        return observation

    @staticmethod
    def _fence_token(fence: AgentFence) -> str:
        if isinstance(fence, str):
            return fence
        if isinstance(fence, (AgentClaim, AgentProgress, AgentResult)):
            return fence.fence
        raise StaleAgentAttempt("agent operation lease, certificate, or fence is stale")

    @staticmethod
    def _reason(reason: str | None) -> str:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("failure reason is required")
        return redact_text(reason)[:1024]

    @staticmethod
    def _lock_identity(
        session: Session,
        node_id: str,
        certificate_serial: str,
    ) -> tuple[AgentNode, AgentCertificate] | None:
        node = session.scalar(
            select(AgentNode)
            .where(AgentNode.node_id == node_id)
            .with_for_update(of=AgentNode)
        )
        if node is None:
            return None
        certificate = session.scalar(
            select(AgentCertificate)
            .where(
                AgentCertificate.serial == certificate_serial,
                AgentCertificate.node_id == node_id,
            )
            .with_for_update(of=AgentCertificate)
        )
        return None if certificate is None else (node, certificate)

    @staticmethod
    def _identity_is_active(
        node: AgentNode,
        certificate: AgentCertificate,
        now: datetime,
    ) -> bool:
        return (
            node.state == "active"
            and node.revoked_at is None
            and certificate.state == "active"
            and certificate.revoked_at is None
            and _aware(certificate.not_before) <= _aware(now)
            and _aware(certificate.not_after) > _aware(now)
        )

    def _aggregate_parent(self, session: Session, parent_job_id: str) -> None:
        job = session.scalar(
            select(Job).where(Job.id == parent_job_id).with_for_update(of=Job)
        )
        if job is None:
            raise KeyError(parent_job_id)
        if job.reconciliation_id is not None:
            return
        operations = list(
            session.scalars(
                select(StoredOperation)
                .where(StoredOperation.parent_job_id == parent_job_id)
                .order_by(StoredOperation.created_at, StoredOperation.id)
            )
        )
        if (
            job.kind == "agent-upgrade"
            and job.state == "waiting-for-operator"
            and set(job.targets) - {operation.node_id for operation in operations}
        ):
            # Sequential agent upgrades intentionally materialize one target at
            # a time. If the next target drifted ineligible, preserve the
            # service's specific operator-facing reason instead of declaring the
            # job successful merely because every materialized operation passed.
            return
        terminal = {
            "cancelled",
            "compensated",
            "failed",
            "succeeded",
            "waiting-for-operator",
        }
        if not operations or any(
            operation.state not in terminal for operation in operations
        ):
            return
        states = {operation.state for operation in operations}
        if "failed" in states:
            state = "failed"
        elif "waiting-for-operator" in states:
            state = "waiting-for-operator"
        elif "cancelled" in states:
            state = "cancelled"
        else:
            state = "succeeded"
        job.state = state
        job.updated_at = self._clock()
        if state == "succeeded":
            job.status_reason = None
            return
        for operation in operations:
            if operation.state != state:
                continue
            attempt = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == operation.id,
                    AgentOperationAttempt.attempt == operation.current_attempt,
                )
            )
            if attempt is not None and attempt.result is not None:
                reason = attempt.result.get("reason")
                if not isinstance(reason, str):
                    reason = attempt.result.get("error_code")
                if isinstance(reason, str):
                    if job.kind == "agent-upgrade":
                        package = job.payload.get("package")
                        node = session.get(AgentNode, operation.node_id)
                        if isinstance(package, Mapping):
                            reason = operator_agent_upgrade_reason(
                                node_id=operation.node_id,
                                attempt_count=operation.current_attempt,
                                package=package,
                                observed_semantic_version=(
                                    None if node is None else node.semantic_version
                                ),
                                observed_binary_digest=(
                                    None if node is None else node.binary_digest
                                ),
                                observed_build_digest=(
                                    None if node is None else node.build_digest
                                ),
                                raw_reason=reason,
                                retry_queued=(
                                    operation.retry_disposition == _RETRY_DISPOSITION
                                    and operation.retry_disposition_attempt
                                    == operation.current_attempt
                                ),
                            )
                    job.status_reason = redact_text(reason)[:1024]
                    return
        job.status_reason = None
