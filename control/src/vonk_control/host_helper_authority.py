"""Controller-only signer for the narrow GPU node host-maintenance helper."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import AgentProtocolError, canonical_message
from vonk_agent_protocol.host_helper import (
    HOST_HELPER_AUTHORITY,
    MAX_HOST_HELPER_GRANT_SECONDS,
    ContainerRuntimeAction,
    HostHelperGrantClaims,
    HostHelperOperation,
    HostHelperSignature,
    HostOperationKind,
    SignedHostHelperGrant,
    host_helper_grant_signing_bytes,
)

from .compat_recovery import (
    _GRANTLESS_RETRY_FAILURE as COMPAT_GRANTLESS_RETRY_FAILURE,
)
from .compat_recovery import (
    DISPATCH_CERTIFICATE_SERIAL as COMPAT_DISPATCH_CERTIFICATE_SERIAL,
)
from .compat_recovery import (
    GRANTLESS_RETRY_CERTIFICATE_SERIAL as COMPAT_GRANTLESS_RETRY_CERTIFICATE_SERIAL,
)
from .compat_recovery import JOB_ID as COMPAT_JOB_ID
from .compat_recovery import (
    NODE_ID as COMPAT_NODE_ID,
)
from .compat_recovery import (
    OPERATION_ID as COMPAT_OPERATION_ID,
)
from .compat_recovery import RECOVERY_ID as COMPAT_RECOVERY_ID
from .compat_recovery import RETRY_ATTEMPT as COMPAT_RETRY_ATTEMPT
from .compat_recovery import (
    SOURCE_BINARY_DIGEST as COMPAT_SOURCE_BINARY_DIGEST,
)
from .compat_recovery import (
    SOURCE_BUILD_DIGEST as COMPAT_SOURCE_BUILD_DIGEST,
)
from .compat_recovery import (
    SOURCE_SEMANTIC_VERSION as COMPAT_SOURCE_SEMANTIC_VERSION,
)
from .compat_recovery import (
    TARGET_BINARY_DIGEST as COMPAT_TARGET_BINARY_DIGEST,
)
from .compat_recovery import (
    TARGET_BUILD_DIGEST as COMPAT_TARGET_BUILD_DIGEST,
)
from .compat_recovery import (
    TARGET_PACKAGE_SHA256 as COMPAT_TARGET_PACKAGE_SHA256,
)
from .compat_recovery import (
    TARGET_PACKAGE_VERSION as COMPAT_TARGET_PACKAGE_VERSION,
)
from .models import AgentNode, AgentOperationAttempt, AgentUpgradeCompatibilityRecovery
from .models import AgentOperation as StoredAgentOperation
from .workload_helper_authority import _load_private_key

logger = logging.getLogger(__name__)


class HostHelperAuthorityError(RuntimeError):
    """The host-helper grant could not be issued safely."""


class HostHelperGrantIssuer:
    """Sign one short-lived, exact host operation for one GPU node."""

    def __init__(
        self,
        private_key: ed25519.Ed25519PrivateKey,
        *,
        clock: Callable[[], datetime] | None = None,
        request_id_factory: Callable[[], object] | None = None,
    ) -> None:
        if not isinstance(private_key, ed25519.Ed25519PrivateKey):
            raise TypeError("host helper authority key must be Ed25519")
        if clock is not None and not callable(clock):
            raise TypeError("host helper authority clock is invalid")
        if request_id_factory is not None and not callable(request_id_factory):
            raise TypeError("host helper request ID factory is invalid")
        self._private_key = private_key
        self._clock = clock or (lambda: datetime.now(UTC))
        self._request_id_factory = request_id_factory or uuid4
        self.public_key = private_key.public_key()
        self.public_key_bytes = self.public_key.public_bytes_raw()
        self.key_id = hashlib.sha256(self.public_key_bytes).hexdigest()

    @classmethod
    def from_private_key_file(
        cls,
        path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        request_id_factory: Callable[[], object] | None = None,
    ) -> HostHelperGrantIssuer:
        return cls(
            _load_private_key(Path(path)),
            clock=clock,
            request_id_factory=request_id_factory,
        )

    def public_key_document(self) -> dict[str, object]:
        return {
            "algorithm": "ed25519",
            "authority": HOST_HELPER_AUTHORITY,
            "key_id": self.key_id,
            "public_key": self.public_key_bytes.hex(),
            "schema_version": 1,
            "usage": "host-maintenance-grant",
        }

    def issue_grant(
        self,
        *,
        node_id: object,
        operation: object,
        expires_in_seconds: object,
        request_id: object | None = None,
    ) -> SignedHostHelperGrant:
        if type(operation) is not HostHelperOperation:
            raise HostHelperAuthorityError("host helper operation is invalid")
        if (
            not isinstance(expires_in_seconds, int)
            or isinstance(expires_in_seconds, bool)
            or not 1 <= expires_in_seconds <= MAX_HOST_HELPER_GRANT_SECONDS
        ):
            raise HostHelperAuthorityError("host helper grant expiry is invalid")
        now = self._now()
        try:
            claims = HostHelperGrantClaims(
                schema_version=1,
                authority=HOST_HELPER_AUTHORITY,
                request_id=str(
                    self._request_id_factory() if request_id is None else request_id
                ),
                node_id=node_id,
                issued_at=now,
                expires_at=now + expires_in_seconds,
                operation=operation,
            )
        except (AgentProtocolError, TypeError, ValueError) as error:
            raise HostHelperAuthorityError(
                "host helper grant binding is invalid"
            ) from error
        return SignedHostHelperGrant(
            schema_version=1,
            claims=claims,
            signature=HostHelperSignature(
                algorithm="ed25519",
                key_id=self.key_id,
                value=self._private_key.sign(
                    host_helper_grant_signing_bytes(claims)
                ).hex(),
            ),
        )

    def _now(self) -> int:
        try:
            now = self._clock()
        except Exception as error:
            raise HostHelperAuthorityError(
                "host helper authority clock is unavailable"
            ) from error
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise HostHelperAuthorityError(
                "host helper authority clock must be timezone-aware"
            )
        return int(now.astimezone(UTC).timestamp())


class HostRuntimeAuthorityService:
    """Bind a narrow host-runtime grant to one live agent attempt."""

    _ACTION_KINDS: ClassVar[dict[ContainerRuntimeAction, frozenset[str]]] = {
        ContainerRuntimeAction.IMAGE_IMPORT: frozenset({"recipe.image.import.v1"}),
        ContainerRuntimeAction.IMAGE_INSPECT: frozenset({"recipe.install"}),
        ContainerRuntimeAction.RUN_INSPECT: frozenset({"recipe.start"}),
        ContainerRuntimeAction.START: frozenset({"recipe.start", "recipe.job.run.v1"}),
        # A start attempt may stop its own managed run when readiness fails.
        ContainerRuntimeAction.STOP: frozenset(
            {"recipe.start", "recipe.stop", "recipe.job.run.v1"}
        ),
    }
    # dev335 and the staged a122 recovery both request this exact TTL. Keep
    # equality below: this is a compatibility value, not a range or default.
    _COMPATIBILITY_GRANT_SECONDS: ClassVar[int] = 10
    _COMPATIBILITY_IDENTITY_WINDOW: ClassVar[timedelta] = timedelta(minutes=15)

    def __init__(
        self,
        sessions: sessionmaker[Session],
        issuer: HostHelperGrantIssuer,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(sessions):
            raise TypeError("host runtime sessions are invalid")
        if not isinstance(issuer, HostHelperGrantIssuer):
            raise TypeError("host runtime grant issuer is invalid")
        if clock is not None and not callable(clock):
            raise TypeError("host runtime authority clock is invalid")
        self._sessions = sessions
        self._issuer = issuer
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def public_key_document(self) -> dict[str, object]:
        return self._issuer.public_key_document()

    def issue_grant(
        self,
        *,
        node_id: str,
        job_id: str,
        operation_id: str,
        attempt: int,
        fence: str,
        action: ContainerRuntimeAction,
        request_sha256: str,
        certificate_serial: str,
        expires_in_seconds: int = 30,
    ) -> SignedHostHelperGrant:
        if type(action) is not ContainerRuntimeAction:
            raise HostHelperAuthorityError("container runtime action is invalid")
        lease_deadline = self._check_attempt(
            node_id=node_id,
            job_id=job_id,
            operation_id=operation_id,
            attempt=attempt,
            fence=fence,
            action=action,
            certificate_serial=certificate_serial,
        )
        grant = self._issuer.issue_grant(
            node_id=node_id,
            operation=HostHelperOperation(
                HostOperationKind.EXECUTE_CONTAINER_RUNTIME_REQUEST,
                {
                    "action": action.value,
                    "job_id": job_id,
                    "operation_id": operation_id,
                    "attempt": attempt,
                    "fence": fence,
                    "request_sha256": request_sha256,
                },
            ),
            expires_in_seconds=expires_in_seconds,
        )
        if grant.claims.expires_at > int(lease_deadline.timestamp()):
            raise HostHelperAuthorityError(
                "host runtime grant exceeds the active attempt lease"
            )
        return grant

    def issue_agent_upgrade_grant(
        self,
        *,
        node_id: str,
        job_id: str,
        operation_id: str,
        attempt: int,
        fence: str,
        package_sha256: str,
        package_signature: str,
        certificate_serial: str,
        expires_in_seconds: int = 30,
    ) -> SignedHostHelperGrant:
        now = self._clock()
        with self._sessions.begin() as session:
            operation = session.get(StoredAgentOperation, operation_id)
            current = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == operation_id,
                    AgentOperationAttempt.attempt == attempt,
                )
            )
            lease_deadline = (
                None
                if current is None
                else current.lease_deadline
                if current.lease_deadline.tzinfo is not None
                else current.lease_deadline.replace(tzinfo=UTC)
            )
            recovery = session.scalar(
                select(AgentUpgradeCompatibilityRecovery)
                .where(AgentUpgradeCompatibilityRecovery.operation_id == operation_id)
                .with_for_update(of=AgentUpgradeCompatibilityRecovery)
            )
            certificate_matches = bool(
                current is not None
                and (
                    current.agent_certificate_serial == certificate_serial
                    or (
                        recovery is not None
                        and recovery.id == COMPAT_RECOVERY_ID
                        and recovery.node_id == COMPAT_NODE_ID == node_id
                        and recovery.job_id == COMPAT_JOB_ID == job_id
                        and recovery.operation_id
                        == COMPAT_OPERATION_ID
                        == operation_id
                        and recovery.expected_retry_attempt
                        == COMPAT_RETRY_ATTEMPT
                        == attempt
                        and current.fence == fence
                        and current.agent_certificate_serial
                        == COMPAT_GRANTLESS_RETRY_CERTIFICATE_SERIAL
                        and certificate_serial
                        == COMPAT_DISPATCH_CERTIFICATE_SERIAL
                    )
                )
            )
            if (
                operation is None
                or current is None
                or operation.node_id != node_id
                or operation.parent_job_id != job_id
                or operation.kind != "agent.upgrade.v1"
                or operation.payload.get("package_sha256") != package_sha256
                or operation.payload.get("package_signature") != package_signature
                or operation.state != "running"
                or operation.current_attempt != attempt
                or current.state != "running"
                or current.fence != fence
                or not certificate_matches
                or lease_deadline is None
                or lease_deadline <= now
            ):
                raise HostHelperAuthorityError("agent upgrade authority is stale")
            if recovery is not None:
                return self._issue_compatibility_recovery_grant(
                    session=session,
                    recovery=recovery,
                    operation=operation,
                    current=current,
                    lease_deadline=lease_deadline,
                    node_id=node_id,
                    job_id=job_id,
                    operation_id=operation_id,
                    attempt=attempt,
                    fence=fence,
                    package_sha256=package_sha256,
                    package_signature=package_signature,
                    certificate_serial=certificate_serial,
                    expires_in_seconds=expires_in_seconds,
                    now=now,
                )
        grant = self._issuer.issue_grant(
            node_id=node_id,
            operation=HostHelperOperation(
                HostOperationKind.INSTALL_VONK_DEB,
                {
                    "package_sha256": package_sha256,
                    "package_signature": package_signature,
                },
            ),
            expires_in_seconds=expires_in_seconds,
        )
        if grant.claims.expires_at > int(lease_deadline.timestamp()):
            raise HostHelperAuthorityError(
                "agent upgrade grant exceeds the active attempt lease"
            )
        return grant

    def _issue_compatibility_recovery_grant(
        self,
        *,
        session: Session,
        recovery: AgentUpgradeCompatibilityRecovery,
        operation: StoredAgentOperation,
        current: AgentOperationAttempt,
        lease_deadline: datetime,
        node_id: str,
        job_id: str,
        operation_id: str,
        attempt: int,
        fence: str,
        package_sha256: str,
        package_signature: str,
        certificate_serial: str,
        expires_in_seconds: int,
        now: datetime,
    ) -> SignedHostHelperGrant:
        """Issue or replay the sole scheduled-reboot grant; never fall through."""

        node = session.get(AgentNode, node_id)
        source = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation_id,
                AgentOperationAttempt.attempt == recovery.source_attempt,
            )
        )
        package = operation.payload
        exact = bool(
            recovery.node_id == COMPAT_NODE_ID == node_id
            and recovery.job_id == job_id
            and recovery.operation_id == COMPAT_OPERATION_ID == operation_id
            and recovery.authority_revision == operation.authority_revision
            and recovery.expected_retry_attempt == recovery.source_attempt + 1
            and recovery.expected_retry_attempt == attempt
            and current.attempt == attempt
            and current.fence == fence
            and (
                current.agent_certificate_serial == certificate_serial
                or (
                    current.agent_certificate_serial
                    == COMPAT_GRANTLESS_RETRY_CERTIFICATE_SERIAL
                    and current.result == COMPAT_GRANTLESS_RETRY_FAILURE
                )
            )
            and source is not None
            and source.attempt == recovery.source_attempt
            and source.fence == recovery.source_fence
            and source.agent_certificate_serial == recovery.source_certificate_serial
            and node is not None
            and node.contact_certificate_serial
            == COMPAT_DISPATCH_CERTIFICATE_SERIAL
            == certificate_serial
            and node.semantic_version
            == recovery.source_semantic_version
            == COMPAT_SOURCE_SEMANTIC_VERSION
            and node.build_digest
            == recovery.source_build_digest
            == COMPAT_SOURCE_BUILD_DIGEST
            and node.binary_digest
            == recovery.source_binary_digest
            == COMPAT_SOURCE_BINARY_DIGEST
            and node.self_test_passed is True
            and recovery.package_sha256
            == package_sha256
            == package.get("package_sha256")
            == COMPAT_TARGET_PACKAGE_SHA256
            and package_signature == package.get("package_signature")
            and recovery.upgrade_payload_sha256
            == operation.payload_digest
            == hashlib.sha256(canonical_message(dict(package))).hexdigest()
            and recovery.target_package_version
            == package.get("package_version")
            == COMPAT_TARGET_PACKAGE_VERSION
            and recovery.target_binary_digest
            == package.get("target_binary_digest")
            == COMPAT_TARGET_BINARY_DIGEST
            and recovery.target_build_digest
            == package.get("target_build_digest")
            == COMPAT_TARGET_BUILD_DIGEST
        )
        if not exact:
            self._log_compatibility_rejection(
                recovery, attempt=attempt, category="authority_mismatch"
            )
            raise HostHelperAuthorityError(
                "Spark3542 compatibility recovery authority is stale"
            )
        if expires_in_seconds != self._COMPATIBILITY_GRANT_SECONDS:
            self._log_compatibility_rejection(
                recovery, attempt=attempt, category="ttl_mismatch"
            )
            raise HostHelperAuthorityError(
                "Spark3542 compatibility recovery authority is stale"
            )
        if recovery.state == "issued":
            expires_at = recovery.grant_expires_at
            if (
                recovery.retry_fence != fence
                or recovery.retry_certificate_serial != certificate_serial
                or recovery.signed_grant is None
                or expires_at is None
                or (
                    expires_at
                    if expires_at.tzinfo is not None
                    else expires_at.replace(tzinfo=UTC)
                )
                <= now
            ):
                raise HostHelperAuthorityError(
                    "Spark3542 compatibility recovery grant is stale"
                )
            try:
                replay = SignedHostHelperGrant.parse(recovery.signed_grant)
            except (AgentProtocolError, TypeError, ValueError) as error:
                raise HostHelperAuthorityError(
                    "Spark3542 compatibility recovery grant is invalid"
                ) from error
            if (
                replay.claims.request_id != recovery.grant_request_id
                or replay.claims.node_id != COMPAT_NODE_ID
                or replay.claims.operation.to_mapping()
                != {"type": "schedule-reboot", "delay_seconds": 60}
            ):
                raise HostHelperAuthorityError(
                    "Spark3542 compatibility recovery grant is invalid"
                )
            return replay
        if recovery.state != "armed" or recovery.signed_grant is not None:
            self._log_compatibility_rejection(
                recovery, attempt=attempt, category="grant_unavailable"
            )
            raise HostHelperAuthorityError(
                "Spark3542 compatibility recovery grant is unavailable"
            )
        grant = self._issuer.issue_grant(
            node_id=node_id,
            operation=HostHelperOperation(
                HostOperationKind.SCHEDULE_REBOOT,
                {"delay_seconds": 60},
            ),
            expires_in_seconds=expires_in_seconds,
        )
        if grant.claims.expires_at > int(lease_deadline.timestamp()):
            raise HostHelperAuthorityError(
                "Spark3542 compatibility recovery grant exceeds the active attempt lease"
            )
        recovery.state = "issued"
        recovery.retry_fence = fence
        recovery.retry_certificate_serial = certificate_serial
        recovery.signed_grant = grant.to_mapping()
        recovery.grant_request_id = grant.claims.request_id
        recovery.grant_expires_at = datetime.fromtimestamp(
            grant.claims.expires_at, tz=UTC
        )
        recovery.identity_deadline = now + self._COMPATIBILITY_IDENTITY_WINDOW
        recovery.issued_at = now
        return grant

    @staticmethod
    def _log_compatibility_rejection(
        recovery: AgentUpgradeCompatibilityRecovery,
        *,
        attempt: int,
        category: str,
    ) -> None:
        logger.warning(
            "compatibility host-helper grant rejected",
            extra={
                "compatibility_recovery_id": recovery.id,
                "operation_id": recovery.operation_id,
                "attempt": attempt,
                "rejection_category": category,
            },
        )

    def _check_attempt(
        self,
        *,
        node_id: str,
        job_id: str,
        operation_id: str,
        attempt: int,
        fence: str,
        action: ContainerRuntimeAction,
        certificate_serial: str,
    ) -> datetime:
        now = self._clock()
        with self._sessions() as session:
            operation = session.get(StoredAgentOperation, operation_id)
            current = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == operation_id,
                    AgentOperationAttempt.attempt == attempt,
                )
            )
            lease_deadline = (
                None
                if current is None
                else current.lease_deadline
                if current.lease_deadline.tzinfo is not None
                else current.lease_deadline.replace(tzinfo=UTC)
            )
            if (
                operation is None
                or current is None
                or operation.node_id != node_id
                or operation.parent_job_id != job_id
                or operation.kind not in self._ACTION_KINDS[action]
                or operation.state != "running"
                or operation.current_attempt != attempt
                or current.state != "running"
                or current.fence != fence
                or current.agent_certificate_serial != certificate_serial
                or lease_deadline is None
                or lease_deadline <= now
            ):
                raise HostHelperAuthorityError(
                    "container runtime action authority is stale"
                )
            return lease_deadline
