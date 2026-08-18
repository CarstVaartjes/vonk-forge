"""Register an agent after a one-time enrollment grant was consumed."""
from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from .domain_states import EnrollmentState, transition_enrollment
from .enrollment_service import EnrollmentGrantError, VerificationResult

_NODE_ID = re.compile(r"\Aspk_[0-9a-f]{32}\Z")
_CERTIFICATE_PREFIX = "spiffe://vonk-forge.local/node/"
_REQUIRED_EVIDENCE = frozenset(
    {
        "node_id",
        "csr_public_key_fingerprint",
        "host_key_fingerprint",
        "hardware_fingerprint",
        "agent_digest",
        "boot_id",
    }
)


class AgentRegistrationError(ValueError):
    """Registration was not accepted."""


@dataclass(frozen=True)
class RegistrationRequest:
    """Authenticated certificate and the immutable evidence from an agent."""

    context: VerificationResult
    certificate_identity: str
    csr_pem: str
    host_key_fingerprint: str
    hardware_fingerprint: str
    agent_digest: str
    boot_id: str
    csr_public_key_fingerprint: str


@dataclass(frozen=True)
class PendingRegistration:
    enrollment_id: str
    intent_id: str
    node_id: str
    certificate_identity: str
    state: str


class AgentRegistrationService:
    """Persist exactly one pending registration for a consumed grant."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock or (lambda: datetime.now(UTC))

    def register(
        self,
        request: RegistrationRequest | VerificationResult,
        *,
        certificate_identity: str | None = None,
        csr_pem: str = "",
        evidence: Mapping[str, object] | None = None,
    ) -> PendingRegistration:
        """Register evidence using a grant context already consumed by ``verify``.

        The compact positional form accepts a :class:`RegistrationRequest`; the
        keyword form is useful at the transport boundary and requires the same
        six evidence fields.
        """
        if isinstance(request, RegistrationRequest):
            context = request.context
            certificate_identity = request.certificate_identity
            csr_pem = request.csr_pem
            values = {
                "node_id": context.node_id,
                "csr_public_key_fingerprint": request.csr_public_key_fingerprint,
                "host_key_fingerprint": request.host_key_fingerprint,
                "hardware_fingerprint": request.hardware_fingerprint,
                "agent_digest": request.agent_digest,
                "boot_id": request.boot_id,
            }
        elif isinstance(request, VerificationResult):
            context = request
            values = dict(evidence or {})
        else:
            raise AgentRegistrationError("registration context is invalid")
        node_id = context.node_id
        self._validate_identity(node_id, certificate_identity)
        if not isinstance(csr_pem, str) or not csr_pem:
            raise AgentRegistrationError("CSR is required")
        if set(values) != _REQUIRED_EVIDENCE or any(
            not isinstance(value, str) or not value for value in values.values()
        ):
            if isinstance(request, VerificationResult) and not values:
                raise AgentRegistrationError("enrollment evidence is missing")
            raise AgentRegistrationError("registration evidence is invalid")
        if values["node_id"] != node_id:
            raise AgentRegistrationError("registration node identity does not match grant")
        metadata = dict(context.metadata)
        now = self._aware(self._clock())
        enrollment_id = str(uuid.uuid4())
        try:
            with self._sessions.begin() as session:
                intent = session.execute(
                    text(
                        "SELECT intent_id, node_id, state, consumed_at, expires_at "
                        "FROM enrollment_intents WHERE intent_id = :intent_id"
                    ),
                    {"intent_id": context.intent_id},
                ).mappings().first()
                if intent is None or intent["node_id"] != node_id:
                    raise AgentRegistrationError("enrollment evidence is missing")
                if intent["consumed_at"] is None:
                    raise AgentRegistrationError("enrollment grant was not consumed")
                expires_at = intent["expires_at"]
                if isinstance(expires_at, str):
                    expires_at = datetime.fromisoformat(expires_at)
                if self._aware(expires_at) <= now:
                    raise AgentRegistrationError("enrollment grant has expired")
                if intent["state"] != EnrollmentState.WAITING_FOR_REGISTRATION.value:
                    raise AgentRegistrationError("registration was already submitted")
                bound = session.execute(
                    text(
                        "SELECT node_id FROM certificate_records "
                        "WHERE certificate_identity = :identity AND state = 'active'"
                    ),
                    {"identity": certificate_identity},
                ).first()
                if bound is not None and bound[0] != node_id:
                    raise AgentRegistrationError("certificate identity is bound to another node")
                existing = session.execute(
                    text("SELECT evidence_id FROM enrollment_evidence WHERE intent_id = :intent_id"),
                    {"intent_id": context.intent_id},
                ).first()
                if existing is not None:
                    raise AgentRegistrationError("registration was already submitted")
                transition_enrollment(
                    EnrollmentState(intent["state"]), EnrollmentState.PENDING_REVIEW
                )
                session.execute(
                    text(
                        "INSERT INTO enrollment_evidence "
                        "(evidence_id, intent_id, node_id, csr_pem, host_identity, "
                        "hardware_identity, agent_version, boot_id, evidence) "
                        "VALUES (:evidence_id, :intent_id, :node_id, :csr_pem, "
                        ":host_identity, :hardware_identity, :agent_version, :boot_id, :evidence)"
                    ),
                    {
                        "evidence_id": enrollment_id,
                        "intent_id": context.intent_id,
                        "node_id": node_id,
                        "csr_pem": csr_pem,
                        "host_identity": values["host_key_fingerprint"],
                        "hardware_identity": values["hardware_fingerprint"],
                        "agent_version": values["agent_digest"],
                        "boot_id": values["boot_id"],
                        "evidence": json.dumps(
                            {
                                **values,
                                "certificate_identity": certificate_identity,
                                "metadata": metadata,
                            }
                        ),
                    },
                )
                session.execute(
                    text(
                        "UPDATE enrollment_intents SET state = :state "
                        "WHERE intent_id = :intent_id AND state = :previous"
                    ),
                    {
                        "state": EnrollmentState.PENDING_REVIEW.value,
                        "previous": EnrollmentState.WAITING_FOR_REGISTRATION.value,
                        "intent_id": context.intent_id,
                    },
                )
        except AgentRegistrationError:
            raise
        except Exception as error:
            raise AgentRegistrationError("registration could not be persisted") from error
        return PendingRegistration(
            enrollment_id=enrollment_id,
            intent_id=context.intent_id,
            node_id=node_id,
            certificate_identity=certificate_identity or "",
            state=EnrollmentState.PENDING_REVIEW.value,
        )

    @staticmethod
    def _validate_identity(node_id: object, certificate_identity: object) -> None:
        if not isinstance(node_id, str) or _NODE_ID.fullmatch(node_id) is None:
            raise AgentRegistrationError("node identity is invalid")
        expected = _CERTIFICATE_PREFIX + node_id
        if certificate_identity != expected:
            raise AgentRegistrationError("certificate identity is invalid")

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


RegistrationService = AgentRegistrationService
