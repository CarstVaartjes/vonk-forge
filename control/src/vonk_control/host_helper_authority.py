"""Controller-only signer for the narrow GPU node host-maintenance helper."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import AgentProtocolError
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

from .models import AgentOperation as StoredAgentOperation
from .models import AgentOperationAttempt
from .package_helper_authority import _load_private_key


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

    _ACTION_KINDS: ClassVar[
        dict[ContainerRuntimeAction, frozenset[str]]
    ] = {
        ContainerRuntimeAction.IMAGE_IMPORT: frozenset({"recipe.image.import.v1"}),
        ContainerRuntimeAction.IMAGE_INSPECT: frozenset({"recipe.install"}),
        ContainerRuntimeAction.RUN_INSPECT: frozenset({"recipe.start"}),
        ContainerRuntimeAction.START: frozenset({"recipe.start"}),
        # A start attempt may stop its own managed run when readiness fails.
        ContainerRuntimeAction.STOP: frozenset({"recipe.start", "recipe.stop"}),
    }

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
