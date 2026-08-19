"""Secure creation and one-time verification of Spark enrollment grants."""
from __future__ import annotations
import json
import base64
import hashlib
import hmac
import secrets
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from .auth import require_capability
from .enrollment import MAX_ENROLLMENT_GRANT_TTL_SECONDS


@dataclass(frozen=True)
class GrantRequest:
    node_id: str
    actor: object
    controller_endpoint: str
    enrollment_endpoint: str
    ca_fingerprint: str
    ttl_seconds: int = MAX_ENROLLMENT_GRANT_TTL_SECONDS
    metadata: Mapping[str, object] | None = None


@dataclass(frozen=True)
class GrantResponse:
    intent_id: str
    token: str
    expires_at: datetime
    node_id: str
    controller_endpoint: str
    enrollment_endpoint: str
    ca_fingerprint: str


@dataclass(frozen=True)
class VerificationResult:
    intent_id: str
    node_id: str
    metadata: Mapping[str, object]


class EnrollmentGrantError(ValueError):
    """A grant request or verification failed closed."""


class EnrollmentGrantService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] | None = None,
        token_factory: Callable[[], bytes] | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock or (lambda: datetime.now(UTC))
        self._token_factory = token_factory or (lambda: secrets.token_bytes(32))

    def create(self, request: GrantRequest) -> GrantResponse:
        require_capability(request.actor, "fleet:enroll")
        if not request.node_id or not request.controller_endpoint or not request.enrollment_endpoint:
            raise EnrollmentGrantError("grant identity and endpoints are required")
        if not 0 < request.ttl_seconds <= MAX_ENROLLMENT_GRANT_TTL_SECONDS:
            raise EnrollmentGrantError(
                "grant TTL must be between one and "
                f"{MAX_ENROLLMENT_GRANT_TTL_SECONDS} seconds"
            )
        token_bytes = self._token_factory()
        if len(token_bytes) < 32:
            raise EnrollmentGrantError("token generator returned insufficient entropy")
        token = base64.urlsafe_b64encode(token_bytes).rstrip(b"=").decode("ascii")
        now = self._aware(self._clock())
        expires_at = now + timedelta(seconds=request.ttl_seconds)
        intent_id = str(uuid.uuid4())
        metadata = dict(request.metadata or {})
        with self._sessions.begin() as session:
            session.execute(
                text("""
                    INSERT INTO enrollment_intents
                    (intent_id, node_id, state, created_by, created_at, expires_at,
                     token_verifier, consumed_at, controller_endpoint, enrollment_endpoint,
                     ca_fingerprint, metadata)
                    VALUES (:intent_id, :node_id, 'created', :created_by, :created_at,
                            :expires_at, :token_verifier, NULL, :controller_endpoint,
                            :enrollment_endpoint, :ca_fingerprint, :metadata)
                """),
                {
                    "intent_id": intent_id, "node_id": request.node_id,
                    "created_by": getattr(request.actor, "subject", str(request.actor)),
                    "created_at": now, "expires_at": expires_at,
                    "token_verifier": hashlib.sha256(token_bytes).hexdigest(),
                    "controller_endpoint": request.controller_endpoint,
                    "enrollment_endpoint": request.enrollment_endpoint,
                    "ca_fingerprint": request.ca_fingerprint, "metadata": json.dumps(metadata, sort_keys=True),
                },
            )
        return GrantResponse(intent_id, token, expires_at, request.node_id,
                             request.controller_endpoint, request.enrollment_endpoint,
                             request.ca_fingerprint)

    def verify(self, token: str, *, node_id: str) -> VerificationResult:
        if not isinstance(token, str) or not token or not node_id:
            raise EnrollmentGrantError("grant verification failed")
        digest = hashlib.sha256(self._decode(token)).hexdigest() if self._decode_safe(token) else "!"
        now = self._aware(self._clock())
        with self._sessions.begin() as session:
            row = session.execute(
                text("SELECT intent_id, node_id, expires_at, consumed_at, token_verifier, metadata "
                     "FROM enrollment_intents WHERE token_verifier = :digest"),
                {"digest": digest},
            ).mappings().first()
            if row is None or row["node_id"] != node_id or row["consumed_at"] is not None:
                raise EnrollmentGrantError("grant verification failed")
            expires_at = row["expires_at"]
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at)
            if self._aware(expires_at) <= now:
                raise EnrollmentGrantError("grant verification failed")
            if not hmac.compare_digest(str(row["token_verifier"]), digest):
                raise EnrollmentGrantError("grant verification failed")
            consumed = session.execute(
                text("UPDATE enrollment_intents SET consumed_at = :now, state = 'waiting_for_registration' "
                     "WHERE intent_id = :intent_id AND consumed_at IS NULL AND expires_at > :now"),
                {"now": now, "intent_id": row["intent_id"]},
            )
            if consumed.rowcount != 1:
                raise EnrollmentGrantError("grant verification failed")
            return VerificationResult(str(row["intent_id"]), node_id, json.loads(row["metadata"] or "{}"))

    @staticmethod
    def _decode(token: str) -> bytes:
        return base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))

    @classmethod
    def _decode_safe(cls, token: str) -> bool:
        try:
            value = cls._decode(token)
            return bool(value) and len(value) >= 32
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# Explicit alias for callers that use the shorter domain name.
EnrollmentService = EnrollmentGrantService
 
