"""Durable token-authorized enrollment for immutable GPU node identities."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    AgentCertificate,
    AgentCertificateRotation,
    AgentEnrollment,
    AgentEnrollmentGrant,
    AgentIssuedCertificateRevocation,
    AgentNode,
    AgentNodeProfile,
)
from .pki import CertificateAuthority, IssuedCertificate

_NODE_ID = re.compile(r"spk_[0-9a-f]{32}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
MAX_ENROLLMENT_GRANT_TTL_SECONDS = 900
_MAX_CSR_BYTES = 16 * 1024
_EVIDENCE_FIELDS = (
    "node_id",
    "csr_public_key_fingerprint",
    "host_key_fingerprint",
    "hardware_fingerprint",
    "agent_digest",
    "boot_id",
)
_EVIDENCE_LIMITS = {
    "node_id": 36,
    "csr_public_key_fingerprint": 64,
    "host_key_fingerprint": 512,
    "hardware_fingerprint": 512,
    "agent_digest": 128,
    "boot_id": 128,
}
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_ROTATION_ISSUANCE_TIMEOUT = timedelta(minutes=5)


class EnrollmentDenied(RuntimeError):
    """Enrollment input or state does not authorize the requested operation."""


class EnrollmentIssuanceUncertain(EnrollmentDenied):
    """A provider write may have succeeded and must never be retried automatically."""


class RemoteRevocationUncertain(EnrollmentDenied):
    """Local denial committed, but provider confirmation remains pending."""


class RenewalInProgress(RuntimeError):
    """A committed renewal owner may still persist its provider result."""


class RenewalIssuanceUncertain(EnrollmentDenied):
    """Renewal issuance is terminal until an operator reconciles the intent."""


@dataclass(frozen=True)
class EnrollmentGrant:
    id: str
    node_id: str
    expires_at: datetime
    purpose: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class _IssuanceClaim:
    enrollment_id: str
    node_id: str
    csr_pem: bytes
    purpose: str


@dataclass(frozen=True)
class _RotationClaim:
    node_id: str
    source_serial: str
    generation: int
    csr_pem: bytes
    csr_public_key_fingerprint: str
    provider_request_id: str
    state: str
    owner: bool


class EnrollmentService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        authority: CertificateAuthority,
        *,
        clock: Callable[[], datetime],
        issuance_replay_wait_seconds: float = 5.0,
    ) -> None:
        if not 0 <= issuance_replay_wait_seconds <= 30:
            raise ValueError("issuance replay wait must be between zero and 30 seconds")
        self._sessions = sessions
        self._authority = authority
        self._clock = clock
        self._issuance_replay_wait_seconds = issuance_replay_wait_seconds
        # SQLite ignores row locks. PostgreSQL correctness comes from the
        # locked grant row; this preserves the same behavior in local tests.
        self._submit_lock = threading.RLock()
        # PostgreSQL uses a durable advisory lock for cross-service claims.
        # This makes SQLite's same-process behavior match that safety rule.
        self._issuance_lock = threading.RLock()
        # SQLite ignores the row locks used by renewal and activation. Keep
        # same-process rotations serialized so its behavior matches the
        # production database transaction boundary.
        self._rotation_lock = threading.RLock()

    def create(
        self, node_id: str | None, actor: str, ttl_seconds: int
    ) -> EnrollmentGrant:
        return self._create(node_id, actor, ttl_seconds, purpose="new-node")

    def create_reenrollment(
        self, node_id: str | None, actor: str, ttl_seconds: int
    ) -> EnrollmentGrant:
        """Authorize an explicit replacement of a Spark identity.

        An unbound grant deliberately supports controller database recovery:
        the CSR-derived node identity remains cryptographically bound to the
        one-time grant when the former node row no longer exists.
        """
        return self._create(node_id, actor, ttl_seconds, purpose="re-enroll")

    def _create(
        self, node_id: str | None, actor: str, ttl_seconds: int, *, purpose: str
    ) -> EnrollmentGrant:
        if node_id is not None:
            _validate_node_id(node_id)
        _validate_actor(actor)
        if not 0 < ttl_seconds <= MAX_ENROLLMENT_GRANT_TTL_SECONDS:
            raise ValueError(
                "enrollment grant TTL must be between one and "
                f"{MAX_ENROLLMENT_GRANT_TTL_SECONDS} seconds"
            )
        now = _utc(self._clock())
        token_bytes = secrets.token_bytes(32)
        token = base64.urlsafe_b64encode(token_bytes).rstrip(b"=").decode("ascii")
        grant = AgentEnrollmentGrant(
            id=str(uuid.uuid4()),
            node_id=node_id,
            purpose=purpose,
            token_digest=_digest(token_bytes),
            created_by=actor,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        with self._sessions.begin() as session:
            session.add(grant)
        return EnrollmentGrant(
            id=grant.id,
            node_id=node_id,
            expires_at=grant.expires_at,
            purpose=purpose,
            token=token,
        )

    def submit(
        self, token: str, csr: bytes, evidence: Mapping[str, object]
    ) -> IssuedCertificate:
        token_bytes = _decode_token(token)
        now = _utc(self._clock())
        failure: str | None = None
        outcome: IssuedCertificate | None = None
        claim: _IssuanceClaim | None = None
        wait_for_enrollment_id: str | None = None
        with self._submit_lock, self._sessions.begin() as session:
            grant = session.scalar(
                select(AgentEnrollmentGrant)
                .where(AgentEnrollmentGrant.token_digest == _digest(token_bytes))
                .with_for_update(of=AgentEnrollmentGrant)
            )
            if grant is None:
                failure = "invalid enrollment grant"
            elif grant.consumed_at is not None:
                enrollment = session.scalar(
                    select(AgentEnrollment)
                    .where(AgentEnrollment.grant_id == grant.id)
                    .with_for_update(of=AgentEnrollment)
                )
                if enrollment is None:
                    failure = "enrollment grant is consumed"
                elif not _replay_matches(enrollment, csr, evidence):
                    failure = "enrollment replay does not match original request"
                elif enrollment.state == "certificate_issued":
                    outcome = _issued(enrollment)
                elif enrollment.state == "issuing":
                    wait_for_enrollment_id = enrollment.id
                else:
                    failure = "enrollment state is invalid"
            elif _stored_utc(grant.expires_at) <= now:
                grant.consumed_at = now
                failure = "enrollment grant is expired"
            else:
                try:
                    if not isinstance(csr, bytes) or len(csr) > _MAX_CSR_BYTES:
                        raise EnrollmentDenied("CSR is too large")
                    csr_pem, public_key_pem, public_key_fingerprint, csr_node_id = (
                        _load_csr(grant.node_id, csr)
                    )
                except EnrollmentDenied as error:
                    failure = str(error)
                else:
                    values, failure = _validate_evidence(
                        evidence,
                        grant.node_id,
                        csr_node_id,
                        public_key_fingerprint,
                    )
                if failure is None:
                    node_id = values["node_id"]
                    enrollment = AgentEnrollment(
                        id=str(uuid.uuid4()),
                        grant_id=grant.id,
                        node_id=node_id,
                        state="issuing",
                        csr_pem=csr_pem.decode("ascii"),
                        csr_public_key_pem=public_key_pem.decode("ascii"),
                        csr_public_key_fingerprint=public_key_fingerprint,
                        host_key_fingerprint=values["host_key_fingerprint"],
                        hardware_fingerprint=values["hardware_fingerprint"],
                        agent_digest=values["agent_digest"],
                        boot_id=values["boot_id"],
                        created_at=now,
                    )
                    _lock_node_issuance(session, node_id)
                    existing_node = session.scalar(
                        select(AgentNode)
                        .where(AgentNode.node_id == node_id)
                        .with_for_update(of=AgentNode)
                    )
                    if grant.purpose == "new-node" and existing_node is not None:
                        failure = "node identity already exists"
                    elif grant.purpose == "re-enroll" and existing_node is not None:
                        if (
                            existing_node.state != "active"
                            or existing_node.revoked_at is not None
                        ):
                            failure = "node identity is retired or revoked"
                        elif (
                            session.scalar(
                                select(AgentCertificate.serial)
                                .where(AgentCertificate.node_id == node_id)
                                .limit(1)
                            )
                            is None
                        ):
                            failure = "node identity has no certificate history"
                        elif (
                            session.scalar(
                                select(AgentCertificateRotation)
                                .where(AgentCertificateRotation.node_id == node_id)
                                .with_for_update(of=AgentCertificateRotation)
                            )
                            is not None
                        ):
                            failure = "certificate rotation is in progress"
                    competing = session.scalar(
                        select(AgentEnrollment.id)
                        .where(
                            AgentEnrollment.node_id == node_id,
                            AgentEnrollment.state == "issuing",
                        )
                        .with_for_update(of=AgentEnrollment)
                        .limit(1)
                    )
                    if competing is not None:
                        failure = "node enrollment issuance is in progress"
                    grant.node_id = node_id
                    grant.consumed_at = now
                    if failure is None:
                        session.add(enrollment)
                        claim = _IssuanceClaim(
                            enrollment.id,
                            node_id,
                            csr_pem,
                            grant.purpose,
                        )
                else:
                    grant.consumed_at = now
        if failure is not None:
            raise EnrollmentDenied(failure)
        if outcome is not None:
            return outcome
        if wait_for_enrollment_id is not None:
            return self._wait_for_issuance(wait_for_enrollment_id)
        assert claim is not None
        with self._issuance_lock:
            try:
                issued = self._authority.issue_node(claim.node_id, claim.csr_pem, now)
            except Exception as error:
                raise EnrollmentIssuanceUncertain(
                    "certificate issuance is uncertain; manual recovery required"
                ) from error
            if issued.node_id != claim.node_id:
                raise EnrollmentIssuanceUncertain(
                    "certificate issuance is uncertain; manual recovery required"
                )
            try:
                with self._sessions.begin() as session:
                    enrollment = _locked_enrollment(session, claim.enrollment_id)
                    if enrollment.state != "issuing":
                        raise EnrollmentDenied(
                            "certificate issuance state changed; manual recovery required"
                        )
                    if (
                        claim.purpose == "new-node"
                        and session.get(AgentNode, enrollment.node_id) is not None
                    ):
                        raise EnrollmentDenied("node identity already exists")
                    _persist_issued_enrollment(
                        session,
                        enrollment,
                        issued,
                        purpose=claim.purpose,
                        now=now,
                    )
                    if enrollment.certificate_generation is None:
                        raise EnrollmentDenied(
                            "certificate generation was not persisted"
                        )
                    issued = replace(
                        issued, generation=enrollment.certificate_generation
                    )
            except IntegrityError as error:
                # The durable issuing state was committed before the provider
                # call.  Never retry automatically after an uncertain write:
                # the provider may already have created this certificate.
                raise EnrollmentIssuanceUncertain(
                    "certificate persistence failed; manual recovery required"
                ) from error
            return issued

    def _wait_for_issuance(self, enrollment_id: str) -> IssuedCertificate:
        deadline = time.monotonic() + self._issuance_replay_wait_seconds
        while time.monotonic() < deadline:
            with self._sessions() as session:
                enrollment = session.get(AgentEnrollment, enrollment_id)
                if enrollment is None:
                    raise EnrollmentDenied("enrollment state is invalid")
                if enrollment.state == "certificate_issued":
                    return _issued(enrollment)
                if enrollment.state != "issuing":
                    raise EnrollmentDenied("enrollment state is invalid")
            time.sleep(0.01)
        raise EnrollmentIssuanceUncertain(
            "certificate issuance is uncertain; manual recovery required"
        )

    def renew(self, node_id: str, serial: str, csr: bytes) -> IssuedCertificate:
        with self._rotation_lock:
            return self._renew_locked(node_id, serial, csr)

    def _renew_locked(self, node_id: str, serial: str, csr: bytes) -> IssuedCertificate:
        _validate_node_id(node_id)
        if not serial.strip():
            raise ValueError("certificate serial is required")
        normalized_csr, _, csr_fingerprint, _ = _load_csr(node_id, csr)
        now = _utc(self._clock())
        try:
            claim = self._claim_rotation(
                node_id,
                serial,
                normalized_csr,
                csr_fingerprint,
                now,
            )
        except IntegrityError:
            # SQLite does not implement SELECT FOR UPDATE. A node-unique row
            # still arbitrates separate service instances at commit.
            claim = self._claim_rotation(
                node_id,
                serial,
                normalized_csr,
                csr_fingerprint,
                now,
            )
        if isinstance(claim, IssuedCertificate):
            return claim
        if not claim.owner:
            if claim.state == "manual-recovery":
                raise RenewalIssuanceUncertain(
                    "certificate rotation requires manual recovery"
                )
            raise RenewalInProgress("certificate rotation issuance is in progress")
        try:
            issued = self._authority.renew_node(
                node_id,
                normalized_csr,
                now,
                request_id=claim.provider_request_id,
            )
            self._validate_renewal_result(issued, claim)
            disposition = self._persist_rotation(issued, claim)
        except Exception as error:
            self._mark_rotation_uncertain(claim, now)
            raise RenewalIssuanceUncertain(
                "certificate rotation requires manual recovery"
            ) from error
        if disposition == "revocation-pending":
            self._revoke_denied_rotation(issued.serial, claim, now)
            raise EnrollmentDenied(
                "node identity retired during certificate rotation; issued certificate revoked"
            )
        return replace(issued, generation=claim.generation)

    def _claim_rotation(
        self,
        node_id: str,
        serial: str,
        normalized_csr: bytes,
        csr_fingerprint: str,
        now: datetime,
    ) -> _RotationClaim | IssuedCertificate:
        with self._sessions.begin() as session:
            node = session.scalar(
                select(AgentNode)
                .where(AgentNode.node_id == node_id)
                .with_for_update(of=AgentNode)
            )
            if node is None:
                raise EnrollmentDenied("certificate serial does not identify node")
            certificates = list(
                session.scalars(
                    select(AgentCertificate)
                    .where(AgentCertificate.node_id == node_id)
                    .order_by(AgentCertificate.serial)
                    .with_for_update(of=AgentCertificate)
                )
            )
            certificate = next(
                (candidate for candidate in certificates if candidate.serial == serial),
                None,
            )
            if certificate is None:
                raise EnrollmentDenied("certificate serial does not identify node")
            if (
                node.state != "active"
                or node.revoked_at is not None
                or certificate.revoked_at is not None
            ):
                raise EnrollmentDenied("node identity is retired or revoked")
            if certificate.state != "active":
                raise EnrollmentDenied("certificate is not active")
            if (
                _stored_utc(certificate.not_before) > now
                or _stored_utc(certificate.not_after) <= now
            ):
                raise EnrollmentDenied("certificate is not currently valid")
            staged = next(
                (
                    candidate
                    for candidate in certificates
                    if candidate.state == "staged" and candidate.revoked_at is None
                ),
                None,
            )
            if staged is not None:
                if _stored_utc(staged.not_after) <= now:
                    staged.state = "revoked"
                    staged.revoked_at = staged.revoked_at or now
                    session.flush()
                else:
                    if staged.csr_public_key_fingerprint != csr_fingerprint:
                        raise EnrollmentDenied(
                            "a different certificate rotation is already staged"
                        )
                    return _certificate_issued(staged)
            intent = session.scalar(
                select(AgentCertificateRotation)
                .where(AgentCertificateRotation.node_id == node_id)
                .with_for_update(of=AgentCertificateRotation)
            )
            if intent is not None:
                if (
                    intent.source_serial != serial
                    or intent.csr_public_key_fingerprint != csr_fingerprint
                    or intent.csr_pem != normalized_csr.decode("ascii")
                ):
                    raise EnrollmentDenied(
                        "a different certificate rotation is already in progress"
                    )
                if (
                    intent.state == "issuing"
                    and now - _stored_utc(intent.updated_at)
                    >= _ROTATION_ISSUANCE_TIMEOUT
                ):
                    intent.state = "manual-recovery"
                    intent.updated_at = now
                if intent.state not in {"issuing", "manual-recovery"}:
                    raise EnrollmentDenied("certificate rotation state is invalid")
                return _rotation_claim(intent, owner=False)
            generation = (
                max(
                    (candidate.generation for candidate in certificates),
                    default=0,
                )
                + 1
            )
            intent = AgentCertificateRotation(
                node_id=node_id,
                source_serial=serial,
                generation=generation,
                csr_pem=normalized_csr.decode("ascii"),
                csr_public_key_fingerprint=csr_fingerprint,
                provider_request_id=secrets.token_urlsafe(32),
                state="issuing",
                created_at=now,
                updated_at=now,
            )
            session.add(intent)
            return _rotation_claim(intent, owner=True)

    @staticmethod
    def _validate_renewal_result(
        issued: IssuedCertificate,
        claim: _RotationClaim,
    ) -> None:
        if issued.node_id != claim.node_id:
            raise EnrollmentDenied(
                "certificate authority returned a mismatched node identity"
            )
        if issued.serial == claim.source_serial:
            raise EnrollmentDenied("certificate authority reused renewal serial")
        try:
            issued.certificate_pem.decode("ascii")
            issued.chain_pem.decode("ascii")
        except UnicodeDecodeError as error:
            raise EnrollmentDenied(
                "certificate authority returned non-PEM certificate material"
            ) from error

    def _persist_rotation(
        self,
        issued: IssuedCertificate,
        claim: _RotationClaim,
    ) -> str:
        now = _utc(self._clock())
        with self._sessions.begin() as session:
            node = session.scalar(
                select(AgentNode)
                .where(AgentNode.node_id == claim.node_id)
                .with_for_update(of=AgentNode)
            )
            if node is None:
                self._record_orphan_revocation(
                    session,
                    issued,
                    claim,
                    now,
                )
                return "revocation-pending"
            certificates = list(
                session.scalars(
                    select(AgentCertificate)
                    .where(AgentCertificate.node_id == claim.node_id)
                    .order_by(AgentCertificate.serial)
                    .with_for_update(of=AgentCertificate)
                )
            )
            intent = session.scalar(
                select(AgentCertificateRotation)
                .where(
                    AgentCertificateRotation.node_id == claim.node_id,
                    AgentCertificateRotation.provider_request_id
                    == claim.provider_request_id,
                )
                .with_for_update(of=AgentCertificateRotation)
            )
            if intent is None or intent.state != "issuing":
                raise EnrollmentDenied(
                    "certificate rotation issuance state changed; manual recovery required"
                )
            source = next(
                (
                    certificate
                    for certificate in certificates
                    if certificate.serial == claim.source_serial
                ),
                None,
            )
            denied = (
                node.state != "active"
                or node.revoked_at is not None
                or source is None
                or source.state != "active"
                or source.revoked_at is not None
            )
            state = "revoked" if denied else "staged"
            revoked_at = now if denied else None
            session.add(
                AgentCertificate(
                    serial=issued.serial,
                    node_id=claim.node_id,
                    not_before=issued.not_before,
                    not_after=issued.not_after,
                    fingerprint=issued.fingerprint,
                    state=state,
                    generation=claim.generation,
                    certificate_pem=issued.certificate_pem.decode("ascii"),
                    chain_pem=issued.chain_pem.decode("ascii"),
                    csr_public_key_fingerprint=claim.csr_public_key_fingerprint,
                    revoked_at=revoked_at,
                )
            )
            if denied:
                intent.state = "revocation-pending"
                intent.updated_at = now
                session.flush()
                return "revocation-pending"
            session.delete(intent)
            session.flush()
            return "staged"

    @staticmethod
    def _record_orphan_revocation(
        session: Session,
        issued: IssuedCertificate,
        claim: _RotationClaim,
        now: datetime,
    ) -> None:
        evidence = session.scalar(
            select(AgentIssuedCertificateRevocation)
            .where(AgentIssuedCertificateRevocation.serial == issued.serial)
            .with_for_update(of=AgentIssuedCertificateRevocation)
        )
        if evidence is not None:
            if (
                evidence.node_id != claim.node_id
                or evidence.provider_request_id != claim.provider_request_id
                or evidence.fingerprint != issued.fingerprint
                or evidence.generation != claim.generation
            ):
                raise EnrollmentDenied(
                    "issued-certificate revocation evidence conflicts; manual recovery required"
                )
            return
        session.add(
            AgentIssuedCertificateRevocation(
                serial=issued.serial,
                node_id=claim.node_id,
                provider_request_id=claim.provider_request_id,
                fingerprint=issued.fingerprint,
                generation=claim.generation,
                state="revocation-pending",
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()

    def _revoke_denied_rotation(
        self,
        serial: str,
        claim: _RotationClaim,
        now: datetime,
    ) -> None:
        try:
            self._authority.revoke_node(serial, now)
        except RuntimeError as error:
            raise RenewalIssuanceUncertain(
                "issued certificate is denied locally; remote revocation requires reconciliation"
            ) from error
        self._confirm_remote_revocation(claim.node_id, serial, now)

    def _mark_rotation_uncertain(
        self,
        claim: _RotationClaim,
        now: datetime,
    ) -> None:
        try:
            with self._sessions.begin() as session:
                node = session.scalar(
                    select(AgentNode)
                    .where(AgentNode.node_id == claim.node_id)
                    .with_for_update(of=AgentNode)
                )
                if node is None:
                    return
                list(
                    session.scalars(
                        select(AgentCertificate)
                        .where(AgentCertificate.node_id == claim.node_id)
                        .order_by(AgentCertificate.serial)
                        .with_for_update(of=AgentCertificate)
                    )
                )
                intent = session.scalar(
                    select(AgentCertificateRotation)
                    .where(
                        AgentCertificateRotation.node_id == claim.node_id,
                        AgentCertificateRotation.provider_request_id
                        == claim.provider_request_id,
                    )
                    .with_for_update(of=AgentCertificateRotation)
                )
                if intent is not None and intent.state == "issuing":
                    intent.state = "manual-recovery"
                    intent.updated_at = now
        except SQLAlchemyError:
            # The committed issuing row remains authoritative when the
            # follow-up annotation cannot be stored. It still forbids a call.
            pass

    def activate(self, node_id: str, serial: str, generation: int) -> None:
        with self._rotation_lock:
            self._activate_locked(node_id, serial, generation)

    def _activate_locked(self, node_id: str, serial: str, generation: int) -> None:
        _validate_node_id(node_id)
        if (
            not serial.strip()
            or not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
        ):
            raise ValueError("certificate activation identity is invalid")
        now = _utc(self._clock())
        with self._sessions.begin() as session:
            node = session.scalar(
                select(AgentNode)
                .where(AgentNode.node_id == node_id)
                .with_for_update(of=AgentNode)
            )
            certificate = session.scalar(
                select(AgentCertificate)
                .where(
                    AgentCertificate.serial == serial,
                    AgentCertificate.node_id == node_id,
                )
                .with_for_update(of=AgentCertificate)
            )
            if node is None or certificate is None:
                raise EnrollmentDenied("certificate serial does not identify node")
            if node.state != "active" or node.revoked_at is not None:
                raise EnrollmentDenied("node identity is retired or revoked")
            if certificate.generation != generation:
                raise EnrollmentDenied("certificate generation does not match")
            if certificate.state == "active" and certificate.revoked_at is None:
                return
            if (
                certificate.state != "staged"
                or certificate.revoked_at is not None
                or _stored_utc(certificate.not_before) > now
                or _stored_utc(certificate.not_after) <= now
            ):
                raise EnrollmentDenied("certificate is not staged for activation")
            older = list(
                session.scalars(
                    select(AgentCertificate)
                    .where(
                        AgentCertificate.node_id == node_id,
                        AgentCertificate.generation < generation,
                    )
                    .with_for_update(of=AgentCertificate)
                )
            )
            certificate.state = "active"
            for previous in older:
                previous.state = "revoked"
                previous.revoked_at = previous.revoked_at or now

    def revoke_node(self, node_id: str, actor: str) -> None:
        """Retire locally before best-effort provider revocation.

        Retrying is safe: local state remains denied and provider revocation is
        monotonic. An uncertain provider response is never allowed to restore
        ingress access.
        """
        _validate_node_id(node_id)
        _validate_actor(actor)
        now = _utc(self._clock())
        with self._sessions.begin() as session:
            node = session.scalar(
                select(AgentNode)
                .where(AgentNode.node_id == node_id)
                .with_for_update(of=AgentNode)
            )
            certificates: list[AgentCertificate] = []
            if node is not None:
                certificates = list(
                    session.scalars(
                        select(AgentCertificate)
                        .where(AgentCertificate.node_id == node_id)
                        .order_by(AgentCertificate.serial)
                        .with_for_update(of=AgentCertificate)
                    )
                )
                session.scalar(
                    select(AgentCertificateRotation)
                    .where(AgentCertificateRotation.node_id == node_id)
                    .with_for_update(of=AgentCertificateRotation)
                )
            orphan_evidence = list(
                session.scalars(
                    select(AgentIssuedCertificateRevocation)
                    .where(AgentIssuedCertificateRevocation.node_id == node_id)
                    .order_by(AgentIssuedCertificateRevocation.serial)
                    .with_for_update(of=AgentIssuedCertificateRevocation)
                )
            )
            if node is None and not orphan_evidence:
                raise EnrollmentDenied("node identity does not exist")
            if node is not None:
                node.state = "retired"
                node.revoked_at = node.revoked_at or now
            serials = [
                certificate.serial
                for certificate in certificates
                if certificate.ca_revoked_at is None
            ]
            serials.extend(
                evidence.serial
                for evidence in orphan_evidence
                if evidence.ca_revoked_at is None
            )
            serials = list(dict.fromkeys(serials))
            for certificate in certificates:
                certificate.state = "revoked"
                certificate.revoked_at = certificate.revoked_at or now
        uncertain = False
        for serial in serials:
            try:
                self._authority.revoke_node(serial, now)
            except RuntimeError:
                uncertain = True
            else:
                self._confirm_remote_revocation(node_id, serial, now)
        if uncertain:
            raise RemoteRevocationUncertain(
                "local revocation complete; remote CA revocation is uncertain"
            )

    def _confirm_remote_revocation(
        self,
        node_id: str,
        serial: str,
        now: datetime,
    ) -> None:
        with self._sessions.begin() as session:
            node = session.scalar(
                select(AgentNode)
                .where(AgentNode.node_id == node_id)
                .with_for_update(of=AgentNode)
            )
            if node is None:
                evidence = session.scalar(
                    select(AgentIssuedCertificateRevocation)
                    .where(
                        AgentIssuedCertificateRevocation.serial == serial,
                        AgentIssuedCertificateRevocation.node_id == node_id,
                    )
                    .with_for_update(of=AgentIssuedCertificateRevocation)
                )
                if evidence is not None:
                    evidence.state = "revoked"
                    evidence.updated_at = now
                    evidence.ca_revoked_at = evidence.ca_revoked_at or now
                return
            certificate = session.scalar(
                select(AgentCertificate)
                .where(
                    AgentCertificate.serial == serial,
                    AgentCertificate.node_id == node_id,
                )
                .with_for_update(of=AgentCertificate)
            )
            intent = session.scalar(
                select(AgentCertificateRotation)
                .where(AgentCertificateRotation.node_id == node_id)
                .with_for_update(of=AgentCertificateRotation)
            )
            evidence = session.scalar(
                select(AgentIssuedCertificateRevocation)
                .where(
                    AgentIssuedCertificateRevocation.serial == serial,
                    AgentIssuedCertificateRevocation.node_id == node_id,
                )
                .with_for_update(of=AgentIssuedCertificateRevocation)
            )
            if certificate is not None:
                certificate.ca_revoked_at = certificate.ca_revoked_at or now
            if evidence is not None:
                evidence.state = "revoked"
                evidence.updated_at = now
                evidence.ca_revoked_at = evidence.ca_revoked_at or now
            if (
                certificate is not None
                and intent is not None
                and intent.state == "revocation-pending"
                and certificate.generation == intent.generation
            ):
                intent.state = "revoked"
                intent.updated_at = now


def _persist_issued_enrollment(
    session: Session,
    enrollment: AgentEnrollment,
    issued: IssuedCertificate,
    *,
    purpose: str,
    now: datetime,
) -> None:
    try:
        certificate_pem = issued.certificate_pem.decode("ascii")
        chain_pem = issued.chain_pem.decode("ascii")
    except UnicodeDecodeError as error:
        raise EnrollmentDenied(
            "certificate authority returned non-PEM certificate material"
        ) from error
    node = session.scalar(
        select(AgentNode)
        .where(AgentNode.node_id == enrollment.node_id)
        .with_for_update(of=AgentNode)
    )
    if purpose not in {"new-node", "re-enroll"}:
        raise EnrollmentDenied("enrollment purpose is invalid")
    if purpose == "new-node" and node is not None:
        raise EnrollmentDenied("node identity already exists")
    if node is None:
        node = AgentNode(
            node_id=enrollment.node_id,
            state="active",
            capabilities=[],
        )
        session.add(node)
        # There is no ORM relationship between these operational rows. Flush
        # the FK parent explicitly for PostgreSQL.
        session.flush([node])
        session.add(
            AgentNodeProfile(
                node_id=enrollment.node_id,
                display_name=enrollment.node_id,
                hostname="",
                lifecycle="ready",
                labels={},
            )
        )
        generation = 1
    else:
        if purpose != "re-enroll":
            raise EnrollmentDenied("enrollment purpose is invalid")
        if node.state != "active" or node.revoked_at is not None:
            raise EnrollmentDenied("node identity is retired or revoked")
        certificates = list(
            session.scalars(
                select(AgentCertificate)
                .where(AgentCertificate.node_id == enrollment.node_id)
                .order_by(AgentCertificate.generation, AgentCertificate.serial)
                .with_for_update(of=AgentCertificate)
            )
        )
        if (
            session.scalar(
                select(AgentCertificateRotation)
                .where(AgentCertificateRotation.node_id == enrollment.node_id)
                .with_for_update(of=AgentCertificateRotation)
            )
            is not None
        ):
            raise EnrollmentDenied("certificate rotation is in progress")
        if not certificates:
            raise EnrollmentDenied("node identity has no certificate history")
        generation = max(certificate.generation for certificate in certificates) + 1
        for certificate in certificates:
            if certificate.state in {"active", "staged"}:
                certificate.state = "revoked"
                certificate.revoked_at = certificate.revoked_at or now
    session.add(
        AgentCertificate(
            serial=issued.serial,
            node_id=enrollment.node_id,
            not_before=issued.not_before,
            not_after=issued.not_after,
            fingerprint=issued.fingerprint,
            state="active",
            generation=generation,
            certificate_pem=certificate_pem,
            chain_pem=chain_pem,
            csr_public_key_fingerprint=enrollment.csr_public_key_fingerprint,
        )
    )
    enrollment.state = "certificate_issued"
    enrollment.certificate_pem = certificate_pem
    enrollment.chain_pem = chain_pem
    enrollment.certificate_serial = issued.serial
    enrollment.certificate_fingerprint = issued.fingerprint
    enrollment.certificate_generation = generation
    enrollment.certificate_not_before = issued.not_before
    enrollment.certificate_not_after = issued.not_after


def _locked_enrollment(session: Session, enrollment_id: str) -> AgentEnrollment:
    enrollment = session.scalar(
        select(AgentEnrollment)
        .where(AgentEnrollment.id == enrollment_id)
        .with_for_update(of=AgentEnrollment)
    )
    if enrollment is None:
        raise EnrollmentDenied("unknown enrollment")
    return enrollment


def _lock_node_issuance(session: Session, node_id: str) -> None:
    """Serialize claims for an absent node identity across PostgreSQL services."""
    if session.get_bind().dialect.name == "postgresql":
        key = int.from_bytes(
            hashlib.sha256(node_id.encode("ascii")).digest()[:8], "big", signed=True
        )
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def _issued(enrollment: AgentEnrollment) -> IssuedCertificate:
    if any(
        value is None
        for value in (
            enrollment.certificate_pem,
            enrollment.chain_pem,
            enrollment.certificate_serial,
            enrollment.certificate_fingerprint,
            enrollment.certificate_not_before,
            enrollment.certificate_not_after,
            enrollment.certificate_generation,
        )
    ):
        raise RuntimeError("issued enrollment is missing certificate metadata")
    return IssuedCertificate(
        node_id=enrollment.node_id,
        certificate_pem=enrollment.certificate_pem.encode("ascii"),  # type: ignore[union-attr]
        chain_pem=enrollment.chain_pem.encode("ascii"),  # type: ignore[union-attr]
        serial=enrollment.certificate_serial,  # type: ignore[arg-type]
        fingerprint=enrollment.certificate_fingerprint,  # type: ignore[arg-type]
        not_before=_stored_utc(enrollment.certificate_not_before),  # type: ignore[arg-type]
        not_after=_stored_utc(enrollment.certificate_not_after),  # type: ignore[arg-type]
        generation=enrollment.certificate_generation,  # type: ignore[arg-type]
    )


def _replay_matches(
    enrollment: AgentEnrollment,
    csr: bytes,
    evidence: Mapping[str, object],
) -> bool:
    try:
        normalized, _, fingerprint, _ = _load_csr(enrollment.node_id, csr)
    except EnrollmentDenied:
        return False
    values, failure = _validate_evidence(
        evidence,
        enrollment.node_id,
        enrollment.node_id,
        fingerprint,
    )
    return failure is None and (
        normalized.decode("ascii") == enrollment.csr_pem
        and fingerprint == enrollment.csr_public_key_fingerprint
        and values["host_key_fingerprint"] == enrollment.host_key_fingerprint
        and values["hardware_fingerprint"] == enrollment.hardware_fingerprint
        and values["agent_digest"] == enrollment.agent_digest
        and values["boot_id"] == enrollment.boot_id
    )


def _certificate_issued(certificate: AgentCertificate) -> IssuedCertificate:
    if certificate.certificate_pem is None or certificate.chain_pem is None:
        raise RuntimeError("staged certificate is missing public material")
    return IssuedCertificate(
        node_id=certificate.node_id,
        certificate_pem=certificate.certificate_pem.encode("ascii"),
        chain_pem=certificate.chain_pem.encode("ascii"),
        serial=certificate.serial,
        fingerprint=certificate.fingerprint,
        not_before=_stored_utc(certificate.not_before),
        not_after=_stored_utc(certificate.not_after),
        generation=certificate.generation,
    )


def _rotation_claim(
    rotation: AgentCertificateRotation,
    *,
    owner: bool,
) -> _RotationClaim:
    return _RotationClaim(
        node_id=rotation.node_id,
        source_serial=rotation.source_serial,
        generation=rotation.generation,
        csr_pem=rotation.csr_pem.encode("ascii"),
        csr_public_key_fingerprint=rotation.csr_public_key_fingerprint,
        provider_request_id=rotation.provider_request_id,
        state=rotation.state,
        owner=owner,
    )


def _load_csr(node_id: str | None, csr: bytes) -> tuple[bytes, bytes, str, str]:
    try:
        request = x509.load_pem_x509_csr(csr)
    except (TypeError, ValueError) as error:
        raise EnrollmentDenied("CSR must be valid PEM") from error
    if not request.is_signature_valid:
        raise EnrollmentDenied("CSR signature is invalid")
    common_names = request.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
    if len(common_names) != 1 or _NODE_ID.fullmatch(common_names[0].value) is None:
        raise EnrollmentDenied("CSR subject must contain a canonical node ID")
    csr_node_id = common_names[0].value
    if node_id is not None and csr_node_id != node_id:
        raise EnrollmentDenied("CSR subject does not match enrollment node")
    if len(request.extensions) != 1:
        raise EnrollmentDenied("CSR must contain only the node URI SAN extension")
    try:
        sans = request.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
    except x509.ExtensionNotFound as error:
        raise EnrollmentDenied("CSR node URI SAN is required") from error
    expected_sans = x509.SubjectAlternativeName(
        [
            x509.UniformResourceIdentifier(
                f"spiffe://vonk-forge.local/node/{csr_node_id}"
            )
        ]
    )
    if sans != expected_sans:
        raise EnrollmentDenied("CSR node URI SAN does not match enrollment node")
    public_key = request.public_key()
    if not isinstance(public_key, ed25519.Ed25519PublicKey):
        raise EnrollmentDenied("CSR public key must be Ed25519")
    public_key_pem = public_key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    public_key_der = public_key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return (
        request.public_bytes(serialization.Encoding.PEM),
        public_key_pem,
        _digest(public_key_der),
        csr_node_id,
    )


def _validate_evidence(
    evidence: Mapping[str, object],
    grant_node_id: str | None,
    csr_node_id: str,
    public_key_fingerprint: str,
) -> tuple[dict[str, str], str | None]:
    values: dict[str, str] = {}
    if set(evidence) != set(_EVIDENCE_FIELDS):
        return values, "evidence fields are invalid"
    for name in _EVIDENCE_FIELDS:
        value = evidence.get(name)
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > _EVIDENCE_LIMITS[name]
        ):
            return values, f"evidence {name} is required"
        values[name] = value
    if values["node_id"] != csr_node_id:
        return values, "evidence node ID does not match CSR"
    if grant_node_id is not None and values["node_id"] != grant_node_id:
        return values, "evidence node ID does not match enrollment grant"
    if values["csr_public_key_fingerprint"] != public_key_fingerprint:
        return values, "evidence CSR public-key fingerprint does not match CSR"
    if _HEX_64.fullmatch(values["csr_public_key_fingerprint"]) is None:
        return values, "evidence CSR public-key fingerprint is invalid"
    if _HEX_64.fullmatch(values["agent_digest"]) is None:
        return values, "evidence agent digest is invalid"
    return values, None


def _decode_token(token: str) -> bytes:
    if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
        raise EnrollmentDenied("invalid enrollment grant")
    try:
        value = base64.b64decode(
            (token + "=").encode("ascii"), altchars=b"-_", validate=True
        )
    except (ValueError, binascii.Error) as error:
        raise EnrollmentDenied("invalid enrollment grant") from error
    if len(value) != 32:
        raise EnrollmentDenied("invalid enrollment grant")
    return value


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _validate_node_id(node_id: str) -> None:
    if _NODE_ID.fullmatch(node_id) is None:
        raise ValueError(
            "node ID must be a canonical spk_<32 lowercase hex characters> value"
        )


def _validate_actor(actor: str) -> None:
    if not actor.strip():
        raise ValueError("administrator actor is required")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _stored_utc(value: datetime) -> datetime:
    """Normalize database timestamps; SQLite does not round-trip tzinfo."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
