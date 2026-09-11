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
from typing import Literal

from pydantic import ValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    AgentClaim,
    AgentDirective,
    AgentOperation,
    AgentProgress,
    AgentResult,
    canonical_message,
    validate_result_for_operation,
)
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.contracts import canonical_payload

from .agent_upgrade_status import operator_agent_upgrade_reason
from .auth import AgentSource
from .failure_evidence import safe_text, sanitize_diagnostics
from .logging import redact_text
from .models import (
    AgentCertificate,
    AgentNode,
    AgentNodeProfile,
    AgentOperationAttempt,
    ArtifactJob,
    Job,
    RecipeBuild,
)
from .models import AgentOperation as StoredOperation
from .operation_contract import sanitize_failure_evidence, validate_progress_update
from .operation_progress import observe_progress, progress_write_due
from .recipe_builds import BUILD_ARTIFACT_FORMAT
from .recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_build_policy,
)

AgentFence = str | AgentClaim | AgentProgress | AgentResult
ResultConsumer = Callable[
    [Session, StoredOperation, AgentOperationAttempt, AgentResult], None
]
ContactConsumer = Callable[[Session, AgentSource], None]
# The protocol owns this closed set; the Controller aliases it locally so
# ``_finish`` cannot be handed any string.
AgentResultState = Literal[
    "succeeded", "failed", "cancelled", "waiting-for-operator"
]


_RECIPE_CAPABILITIES = frozenset(
    {
        AgentOperation.RECIPE_BUILD.value,
        AgentOperation.RECIPE_IMAGE_IMPORT.value,
        AgentOperation.RECIPE_INSTALL.value,
        AgentOperation.ARTIFACT_DISTRIBUTION.value,
        AgentOperation.RECIPE_START.value,
        AgentOperation.RECIPE_JOB_RUN.value,
        AgentOperation.RECIPE_STOP.value,
        AgentOperation.RECIPE_UNINSTALL.value,
    }
)
_MUTATING_OPERATIONS = frozenset(
    {
        AgentOperation.AGENT_UPGRADE.value,
        AgentOperation.RECIPE_BUILD.value,
        AgentOperation.RECIPE_IMAGE_IMPORT.value,
        AgentOperation.RECIPE_INSTALL.value,
        AgentOperation.ARTIFACT_DISTRIBUTION.value,
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
_RUNTIME_CAPABILITIES = frozenset({"agent.runtime.rust.v1", "runtime.vonk.v1"})
_NEXT_CAPABILITIES = _RUNTIME_CAPABILITIES | _RECIPE_CAPABILITIES
_OPTIONAL_CAPABILITIES = frozenset(
    {
        AgentOperation.AGENT_UPGRADE.value,
        AgentOperation.RUNTIME_PREFLIGHT.value,
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


def _failure_result(
    error_code: str, reason: str, *, uncertain: bool
) -> dict[str, object]:
    """Build a typed failure result through the redaction boundary."""

    evidence: dict[str, object] = {
        "error_code": error_code,
        "summary": reason,
        "reason": reason,
        "uncertain": uncertain,
        "recovery": "inspect-before-resume" if uncertain else "retry-or-inspect",
    }
    if not uncertain:
        evidence["status"] = "failed"
    return sanitize_failure_evidence(evidence)


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
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if result_consumer is not None and not callable(result_consumer):
            raise TypeError("agent result consumer must be callable")
        if contact_consumer is not None and not callable(contact_consumer):
            raise TypeError("agent contact consumer must be callable")
        self._sessions = sessions
        self._clock = clock
        self._monotonic = monotonic
        self._result_consumer = result_consumer
        self._contact_consumer = contact_consumer
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
        targets = session.scalar(select(Job.targets).where(Job.id == parent_job_id))
        if targets is None:
            raise KeyError(parent_job_id)
        scope = self._target_scope(targets)
        if scope is None or node_id not in scope:
            raise ValueError("agent operation node must be a parent target")
        if not self._lock_target_scopes(
            session, {"enqueue": (parent_job_id, scope)}, node_id
        ):
            raise ValueError("agent operation parent target scope changed")
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
        payload_bytes = canonical_payload(protocol_operation, payload)
        final_payload = json.loads(payload_bytes)
        validated = AgentClaim(
            schema_version=1,
            job_id=parent_job_id,
            operation_id=operation_id,
            attempt=1,
            fence=reserved_fence,
            node_id=node_id,
            operation=protocol_operation,
            authority_revision=authority_revision,
            payload_digest=hashlib.sha256(payload_bytes).hexdigest(),
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
        deadline = self._monotonic() + wait_seconds
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
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    return None
                self._available.wait(min(remaining, _DATABASE_REPOLL_SECONDS))

    @staticmethod
    def _claimable_operations(node_id: str, now: datetime):
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
        return (
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
            .execution_options(populate_existing=True)
            .limit(1)
        )

    def _claim_once(
        self,
        node_id: str,
        certificate_serial: str,
        lease_seconds: int,
        protocol_version: int | None,
        capabilities: tuple[str, ...] | None,
        runtime_identity: AgentRuntimeIdentity,
        hostname: str | None,
        source: AgentSource | None,
    ) -> AgentClaim | None:
        with self._claim_lock, self._sessions.begin() as session:
            now = self._clock()
            candidate_id = session.scalar(
                self._claimable_operations(node_id, now).with_only_columns(
                    StoredOperation.id
                )
            )
            upgrade_id = None
            if (
                capabilities is not None
                and AgentOperation.AGENT_UPGRADE.value in capabilities
                and runtime_identity.package_activation is not None
            ):
                receipt = runtime_identity.package_activation
                upgrade_id = session.scalar(
                    select(StoredOperation.id)
                    .where(
                        StoredOperation.node_id == node_id,
                        StoredOperation.kind == AgentOperation.AGENT_UPGRADE.value,
                        StoredOperation.payload["rollback"]["attempt_nonce"].as_string()
                        == receipt.attempt_nonce,
                        StoredOperation.state.in_(
                            {"queued", "running", "waiting-for-operator"}
                        ),
                    )
                    .order_by(StoredOperation.created_at, StoredOperation.id)
                    .limit(1)
                )
            scopes = self._lock_operation_scopes(
                session,
                tuple(
                    dict.fromkeys(
                        value
                        for value in (candidate_id, upgrade_id)
                        if value is not None
                    )
                ),
                node_id,
            )
            if scopes is None:
                return None
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
                operation_id=upgrade_id,
                parent_job_id=None if upgrade_id is None else scopes[upgrade_id][0],
            )
            if candidate_id is None:
                return None
            statement = (
                self._claimable_operations(node_id, now)
                .where(StoredOperation.id == candidate_id)
                .with_for_update(of=StoredOperation, skip_locked=True)
                .execution_options(populate_existing=True)
            )
            operation = session.scalar(statement)
            if operation is None or operation.parent_job_id != scopes[candidate_id][0]:
                return None
            if not self._claim_has_authority(
                session,
                operation,
                now,
                node=node,
                protocol_version=protocol_version,
                capabilities=capabilities,
                locked_targets=scopes[candidate_id][1],
            ):
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
            resumable_progress = None
            if operation.current_attempt:
                previous = session.scalar(
                    select(AgentOperationAttempt)
                    .where(
                        AgentOperationAttempt.operation_id == operation.id,
                        AgentOperationAttempt.attempt == operation.current_attempt,
                    )
                    .with_for_update(of=AgentOperationAttempt)
                )
                if previous is not None and operation.kind == AgentOperation.ARTIFACT_DISTRIBUTION.value:
                    resumable_progress = (
                        None if previous.progress is None
                        else validate_progress_update(None, previous.progress)
                    )
                if previous is not None and previous.state in {
                    "running",
                    "waiting-for-operator",
                }:
                    previous.state = "expired"
            if operation.state == "running":
                operation.state = "waiting-for-operator"
                operation.retry_disposition = None
                operation.retry_disposition_attempt = None
                operation.updated_at = now
                self._project_artifact_job_expiry(session, operation, now)
                self._aggregate_parent(session, operation.parent_job_id)
                return None
            if operation.kind == AgentOperation.AGENT_UPGRADE.value:
                import secrets

                from vonk_agent_protocol.contracts import AgentUpgradePayload
                payload = AgentUpgradePayload.model_validate(operation.payload)
                if runtime_identity.binary_digest != payload.rollback.source.binary_sha256:
                    return None
                # A new claim owns a fresh watchdog authority. The retry query
                # already enforced the full previous rollback safety fence.
                document = payload.model_dump(mode="json")
                document["rollback"].update(attempt_nonce=secrets.token_hex(32), activation_deadline=int(now.timestamp()) + 900)
                payload_bytes = canonical_payload(AgentOperation.AGENT_UPGRADE, document)
                operation.payload = json.loads(payload_bytes)
                operation.payload_digest = hashlib.sha256(payload_bytes).hexdigest()
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
                progress=resumable_progress,
            )
            session.add(attempt)
            return AgentClaim.model_validate(
                {
                    "schema_version": 1,
                    "job_id": operation.parent_job_id,
                    "operation_id": operation.id,
                    "attempt": attempt.attempt,
                    "fence": attempt.fence,
                    "node_id": operation.node_id,
                    "operation": AgentOperation(operation.kind),
                    "authority_revision": operation.authority_revision,
                    "payload_digest": operation.payload_digest,
                    "payload": operation.payload,
                    "deadline": deadline,
                }
            )

    def _reconcile_agent_upgrade(
        self,
        session: Session,
        node_id: str,
        certificate_serial: str,
        now: datetime,
        capabilities: tuple[str, ...] | None,
        runtime_identity: AgentRuntimeIdentity,
        *,
        operation_id: str | None,
        parent_job_id: str | None,
    ) -> None:
        if (
            operation_id is None
            or capabilities is None
            or AgentOperation.AGENT_UPGRADE.value not in capabilities
        ):
            return
        operation = session.scalar(
            select(StoredOperation)
            .where(
                StoredOperation.id == operation_id,
                StoredOperation.parent_job_id == parent_job_id,
                StoredOperation.node_id == node_id,
                StoredOperation.kind == AgentOperation.AGENT_UPGRADE.value,
                StoredOperation.state.in_(
                    {"queued", "running", "waiting-for-operator"}
                ),
            )
            .order_by(StoredOperation.created_at, StoredOperation.id)
            .with_for_update(of=StoredOperation)
            .execution_options(populate_existing=True)
            .limit(1)
        )
        from vonk_agent_protocol.contracts import AgentUpgradePayload

        from .package_activation import matches_receipt
        receipt = runtime_identity.package_activation
        if operation is None or operation.current_attempt == 0 or receipt is None:
            return
        payload = AgentUpgradePayload.model_validate(operation.payload)
        if not matches_receipt(receipt, payload, node_id):
            return
        if receipt.phase in {"rolled_back", "rollback_failed"}:
            if receipt.phase == "rolled_back" and runtime_identity.binary_digest != payload.rollback.source.binary_sha256:
                return
            current = session.scalar(select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == operation.current_attempt))
            # The same terminal receipt is reported until the next install.
            # It must not revoke an operator-authorized retry of that outcome.
            if (
                operation.retry_disposition == "retry"
                and operation.retry_disposition_attempt == operation.current_attempt
                and current is not None
                and isinstance(current.result, dict)
                and current.result.get("package_activation") == receipt.model_dump(mode="json")
            ):
                return
            operation.state = "waiting-for-operator"
            operation.retry_disposition = None
            operation.retry_disposition_attempt = None
            operation.updated_at = now
            if current is not None:
                current.state = "failed"
                current.result = {"reason": "agent package " + receipt.phase, "package_activation": receipt.model_dump(mode="json")}
            parent = session.get(Job, operation.parent_job_id)
            if parent is not None:
                parent.state = "waiting-for-operator"
                parent.status_reason = "Spark package " + receipt.phase + "; rollout stopped"
                parent.updated_at = now
            return
        if receipt.phase != "acknowledged" or (
            runtime_identity.build_digest
            != operation.payload.get("target_build_digest")
            or runtime_identity.binary_digest
            != operation.payload.get("target_binary_digest")
            or runtime_identity.architecture != operation.payload.get("architecture")
            or runtime_identity.self_test_passed is not True
        ):
            return
        evidence = {
            "architecture": runtime_identity.architecture,
            "binary_digest": runtime_identity.binary_digest,
            "build_digest": runtime_identity.build_digest,
            "package_sha256": operation.payload["package_sha256"],
            "package_version": operation.payload["package_version"],
            "self_test_passed": True,
            "status": "upgraded",
            "activation_receipt": receipt.model_dump(mode="json"),
        }
        attempt = session.scalar(
            select(AgentOperationAttempt)
            .where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == operation.current_attempt,
            )
            .with_for_update(of=AgentOperationAttempt)
        )
        if attempt is None or attempt.state not in {
            "running", "waiting-for-operator", "expired", "failed",
        }:
            return
        message = AgentResult.model_validate(
            {
                "schema_version": 1,
                "job_id": operation.parent_job_id,
                "operation_id": operation.id,
                "attempt": attempt.attempt,
                "fence": attempt.fence,
                "node_id": operation.node_id,
                "deadline": max(_aware(attempt.lease_deadline), _aware(now)),
                "state": "succeeded",
                "result": evidence,
            }
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
        runtime_identity: AgentRuntimeIdentity,
    ) -> bool:
        build_id = operation.payload.get("build_id")
        build = (
            session.get(RecipeBuild, build_id) if isinstance(build_id, str) else None
        )
        try:
            report = parse_stored_build_policy(build.policy_report) if build else None
        except RecipeExecutionContractError:
            return False
        return bool(
            build is not None
            and build.builder_node_id == operation.node_id
            and report is not None
            and report.builder_binary_digest == runtime_identity.binary_digest
            and report.artifact_format == BUILD_ARTIFACT_FORMAT
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
                AgentResult.model_validate(
                    {
                        "schema_version": 1,
                        "job_id": operation.parent_job_id,
                        "operation_id": operation.id,
                        "attempt": attempt.attempt,
                        "fence": fence,
                        "node_id": operation.node_id,
                        "deadline": _aware(now),
                        "state": "failed",
                        "result": reason,
                    }
                ),
            )
        self._aggregate_parent(session, operation.parent_job_id)

    def _claim_has_authority(
        self,
        session: Session,
        operation: StoredOperation,
        now: datetime,
        *,
        node: AgentNode,
        protocol_version: int | None,
        capabilities: tuple[str, ...] | None,
        locked_targets: tuple[str, ...],
    ) -> bool:
        job = session.scalar(
            select(Job).where(Job.id == operation.parent_job_id).with_for_update(of=Job)
        )
        if job is None:
            raise ValueError("agent operation lacks its parent job")
        if self._target_scope(job.targets) != locked_targets:
            return False
        current_operation = session.scalar(
            select(StoredOperation)
            .where(StoredOperation.id == operation.id)
            .with_for_update(of=StoredOperation)
        )
        if current_operation is None:
            return False
        if (
            current_operation.node_id != node.node_id
            or current_operation.node_id not in job.targets
            or current_operation.authority_revision != job.authority_revision
            or node.state != "active"
            or node.revoked_at is not None
            or protocol_version is None
            or node.protocol_version != protocol_version
            or capabilities is None
            or current_operation.kind not in capabilities
            or not isinstance(node.capabilities, list)
            or current_operation.kind not in node.capabilities
        ):
            return False
        if (
            job.state == "waiting-for-operator"
            and current_operation.state == "waiting-for-operator"
            and current_operation.retry_disposition == _RETRY_DISPOSITION
            and current_operation.retry_disposition_attempt
            == current_operation.current_attempt
        ):
            job.state = "queued"
            job.status_reason = None
            job.updated_at = now
            return True
        return job.state not in _TERMINAL_PARENT_STATES

    @staticmethod
    def _target_scope(targets: object) -> tuple[str, ...] | None:
        if (
            not isinstance(targets, list)
            or not targets
            or not all(isinstance(node_id, str) for node_id in targets)
            or len(targets) != len(set(targets))
        ):
            return None
        return tuple(sorted(targets))

    @classmethod
    def _lock_operation_scopes(
        cls,
        session: Session,
        operation_ids: tuple[str, ...],
        node_id: str,
    ) -> dict[str, tuple[str, tuple[str, ...]]] | None:
        """Lock hinted target nodes, then parents; callers pin and refresh operations."""
        rows = (
            session.execute(
                select(
                    StoredOperation.id,
                    StoredOperation.parent_job_id,
                    StoredOperation.node_id,
                    Job.targets,
                )
                .join(Job, Job.id == StoredOperation.parent_job_id)
                .where(StoredOperation.id.in_(operation_ids))
            ).all()
            if operation_ids
            else []
        )
        if len(rows) != len(operation_ids):
            return None
        scopes = {}
        for operation_id, parent_id, operation_node, targets in rows:
            scope = cls._target_scope(targets)
            if scope is None or node_id not in scope or operation_node != node_id:
                return None
            scopes[operation_id] = (parent_id, scope)
        if not cls._lock_target_scopes(session, scopes, node_id):
            return None
        return scopes

    @classmethod
    def _lock_target_scopes(
        cls,
        session: Session,
        scopes: dict[str, tuple[str, tuple[str, ...]]],
        node_id: str,
    ) -> bool:
        nodes = sorted(
            {node_id} | {target for _, scope in scopes.values() for target in scope}
        )
        locked = list(
            session.scalars(
                select(AgentNode)
                .where(AgentNode.node_id.in_(nodes))
                .order_by(AgentNode.node_id)
                .with_for_update(of=AgentNode)
                .execution_options(populate_existing=True)
            )
        )
        if [node.node_id for node in locked] != nodes:
            return False
        parent_ids = sorted({parent_id for parent_id, _ in scopes.values()})
        parents = (
            {
                job.id: job
                for job in session.scalars(
                    select(Job)
                    .where(Job.id.in_(parent_ids))
                    .order_by(Job.id)
                    .with_for_update(of=Job)
                    .execution_options(populate_existing=True)
                )
            }
            if parent_ids
            else {}
        )
        return not any(
            parent_id not in parents
            or cls._target_scope(parents[parent_id].targets) != scope
            for parent_id, scope in scopes.values()
        )

    def heartbeat(
        self,
        fence: AgentFence,
        progress: Mapping[str, object] | None,
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
            message = AgentProgress.model_validate(
                {
                    "schema_version": 1,
                    "job_id": operation.parent_job_id,
                    "operation_id": operation.id,
                    "attempt": attempt.attempt,
                    "fence": attempt.fence,
                    "node_id": operation.node_id,
                    "deadline": deadline,
                    "progress": progress,
                }
            )
            write_progress = message.progress is None
            if message.progress is not None:
                try:
                    current_progress = dict(message.progress)
                    if operation.kind == AgentOperation.ARTIFACT_DISTRIBUTION.value and attempt.progress:
                        # A restarted transfer walks already durable objects again.
                        # Replayed offsets are not loss of retained operation bytes.
                        for key in ("completed_bytes", "completed_items"):
                            if key in current_progress and key in attempt.progress:
                                current_progress[key] = max(current_progress[key], attempt.progress[key])
                    validated = validate_progress_update(attempt.progress, current_progress, partial=False)
                    write_progress = progress_write_due(attempt.progress, validated, _aware(now))
                    if write_progress:
                        attempt.progress = observe_progress(attempt.progress, validated, _aware(now))
                except (TypeError, ValueError) as error:
                    raise ValueError(f"operation progress is invalid: {error}") from error
            if write_progress:
                attempt.lease_deadline = deadline
                operation.updated_at = now
            else:
                deadline = _aware(attempt.lease_deadline)
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
                deadline=deadline,
                cancel_requested=cancel_requested,
            )

    def succeed(self, fence: AgentFence, result: Mapping[str, object]) -> None:
        self._finish(fence, "succeeded", result=result, reason=None)

    def fail(self, fence: AgentFence, reason: str) -> None:
        self._finish(
            fence,
            "failed",
            result=_failure_result("operation_failed", reason, uncertain=False),
            reason=None,
        )

    def wait_for_operator(self, fence: AgentFence, reason: str) -> None:
        self._finish(
            fence,
            "waiting-for-operator",
            result=_failure_result(
                "operation_requires_operator", reason, uncertain=True
            ),
            reason=None,
        )

    def uncertain(self, fence: AgentFence, reason: str) -> None:
        """Persist an ambiguous mutation outcome and require inspection first."""
        self._finish(
            fence,
            "waiting-for-operator",
            result=_failure_result(
                "operation_outcome_uncertain", reason, uncertain=True
            ),
            reason=None,
        )

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
        state: AgentResultState,
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
                message = AgentResult.model_validate(
                    {
                        "schema_version": 1,
                        "job_id": operation.parent_job_id,
                        "operation_id": operation.id,
                        "attempt": attempt.attempt,
                        "fence": attempt.fence,
                        "node_id": operation.node_id,
                        "deadline": _aware(attempt.lease_deadline),
                        "state": state,
                        "result": canonical_result,
                    }
                )
            validate_result_for_operation(
                operation.kind,
                message.result,
                state=message.state,
            )
            if state in {"failed", "waiting-for-operator"}:
                try:
                    raw_result = _document(message.result)
                    diagnostics = raw_result.pop("diagnostics", None)
                    if (
                        operation.kind == AgentOperation.RECIPE_JOB_RUN.value
                        and "exit_code" in raw_result
                    ):
                        # Preserve the typed output manifest and process receipt.
                        # Generic log truncation must not rewrite their structure.
                        message_result = raw_result
                        raw_reason = message_result.get("reason")
                        if isinstance(raw_reason, str):
                            message_result["reason"] = safe_text(raw_reason)
                    else:
                        message_result = sanitize_failure_evidence(raw_result)
                    if diagnostics is not None:
                        message_result["diagnostics"] = sanitize_diagnostics(
                            diagnostics
                        ).model_dump(mode="json")
                    message = AgentResult.model_validate(
                        {
                            **message.model_dump(mode="json"),
                            "result": message_result,
                        }
                    )
                except (TypeError, ValueError) as error:
                    raise ValueError(
                        f"operation failure evidence is invalid: {error}"
                    ) from error
            else:
                message_result = _document(message.result)
            if state == "succeeded" and operation.kind == AgentOperation.ARTIFACT_DISTRIBUTION.value:
                # Final authoritative evidence closes a last sample that may
                # have been coalesced immediately before result publication.
                final_progress = {"phase": "completed", "completed_bytes": message_result["downloaded_bytes"]}
                if attempt.progress and attempt.progress.get("total_items") is not None:
                    final_progress["completed_items"] = attempt.progress["total_items"]
                final_progress = validate_progress_update(attempt.progress, final_progress)
                attempt.progress = observe_progress(attempt.progress, final_progress, _aware(now))
            attempt.result = message_result
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
        scopes = self._lock_operation_scopes(session, (operation_id,), node_id)
        if scopes is None or scopes[operation_id][0] != parent_job_id:
            raise StaleAgentAttempt(
                "agent operation lease, certificate, or fence is stale"
            )
        identity = self._lock_identity(session, node_id, certificate_serial)
        now = self._clock()
        if identity is None or not self._identity_is_active(*identity, now):
            raise StaleAgentAttempt(
                "agent operation lease, certificate, or fence is stale"
            )
        node, certificate = identity
        self._consume_contact(session, source, node, certificate)
        parent = session.scalar(
            select(Job).where(Job.id == parent_job_id).with_for_update(of=Job)
        )
        if (
            parent is None
            or parent.state not in {"queued", "running"}
            or node.node_id not in parent.targets
        ):
            raise StaleAgentAttempt(
                "agent operation lease, certificate, or fence is stale"
            )
        operation = session.scalar(
            select(StoredOperation)
            .where(StoredOperation.id == operation_id)
            .with_for_update(of=StoredOperation)
            .execution_options(populate_existing=True)
        )
        if operation is None:
            raise StaleAgentAttempt(
                "agent operation lease, certificate, or fence is stale"
            )
        if (
            self._target_scope(parent.targets) != scopes[operation_id][1]
            or operation.parent_job_id != parent_job_id
            or operation.node_id != node.node_id
            or operation.authority_revision != parent.authority_revision
            or node.state != "active"
            or node.revoked_at is not None
            or not isinstance(node.capabilities, list)
            or operation.kind not in node.capabilities
        ):
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
            .execution_options(populate_existing=True)
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
        if not values or any(
            not isinstance(value, str) or not value for value in values
        ):
            raise ValueError("agent capabilities are invalid")
        return tuple(sorted({
            value for value in values
            if value in _KNOWN_CAPABILITIES
            or re.fullmatch(r"runtime\.preflight\.fingerprint\.[0-9a-f]{64}", value)
        }))

    @staticmethod
    def _validate_agent_contract(
        protocol_version: int | None,
        capabilities: tuple[str, ...] | None,
        runtime_identity: AgentRuntimeIdentity,
    ) -> None:
        if (
            protocol_version is None
            or protocol_version != 3
            or capabilities is None
            or "agent.runtime.rust.v1" not in capabilities
        ):
            raise ValueError("Rust agent capability negotiation is incomplete")
        receipt_key = runtime_identity.observation_receipt_public_key
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
        runtime_identity: AgentRuntimeIdentity | None,
        hostname: str | None,
    ) -> None:
        current = None if node.last_seen_at is None else _aware(node.last_seen_at)
        observed = _aware(now)
        if current is None or observed > current:
            node.last_seen_at = observed
        contact_time = node.last_seen_at
        if contact_time is None:
            raise ValueError("agent contact timestamp is unavailable")
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
            receipt_key = runtime_identity.observation_receipt_public_key
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
                # The first authenticated contact binds the immutable receipt
                # identity; subsequent contacts remain change-protected above.
                node.observation_receipt_public_key = receipt_key
            node.architecture = runtime_identity.architecture
            node.semantic_version = runtime_identity.semantic_version
            node.build_digest = runtime_identity.build_digest
            node.binary_digest = runtime_identity.binary_digest
            node.self_test_passed = runtime_identity.self_test_passed
            node.contact_certificate_serial = certificate.serial
            node.contact_observation_digest = hashlib.sha256(
                canonical_message(
                    {
                        "certificate_fingerprint": certificate.fingerprint,
                        "certificate_serial": certificate.serial,
                        "node_id": node.node_id,
                        "observed_at": _aware(contact_time).isoformat(),
                        "hostname": hostname,
                        "runtime_identity": runtime_identity,
                    }
                )
            ).hexdigest()

    @staticmethod
    def _runtime_identity(
        value: AgentRuntimeIdentity | Mapping[str, object] | None,
    ) -> AgentRuntimeIdentity:
        if value is None:
            raise ValueError("agent runtime identity is required")
        try:
            return AgentRuntimeIdentity.model_validate(value)
        except (TypeError, ValidationError) as error:
            raise ValueError("agent runtime identity is invalid") from error

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
