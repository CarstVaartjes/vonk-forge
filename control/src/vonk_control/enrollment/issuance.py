"""Durable token-authorized enrollment for immutable GPU node identities."""

from __future__ import annotations

import base64
import logging
import re
import secrets
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    CertificateCode,
    EnrollmentGrantState,
)
from vonk_agent_protocol.enrollment import MAX_CSR_BYTES
from vonk_agent_protocol.state_machines import (
    CertificateIssuancePurpose,
    CertificateRecordState,
    CertificateRotationState,
    EnrollmentPurpose,
    EnrollmentRecordState,
    NodeIdentityState,
)

from ..enrollment_contract import ENROLLMENT_ID_PATTERN, EnrollmentGrantStatus
from ..enrollment_validation import (
    _decode_token as _decode_token,  # noqa: PLC0414 -- shared helper export
)
from ..enrollment_validation import (
    _digest as _digest,  # noqa: PLC0414 -- shared helper export
)
from ..enrollment_validation import (
    _load_csr as _load_csr,  # noqa: PLC0414 -- shared helper export
)
from ..enrollment_validation import (
    _stored_utc as _stored_utc,  # noqa: PLC0414 -- shared helper export
)
from ..enrollment_validation import (
    _utc as _utc,  # noqa: PLC0414 -- shared helper export
)
from ..enrollment_validation import (
    _validate_actor as _validate_actor,  # noqa: PLC0414 -- shared helper export
)
from ..enrollment_validation import (
    _validate_evidence as _validate_evidence,  # noqa: PLC0414 -- shared helper export
)
from ..enrollment_validation import (
    _validate_node_id as _validate_node_id,  # noqa: PLC0414 -- shared helper export
)
from ..models import (
    AgentCertificate,
    AgentCertificateRotation,
    AgentEnrollment,
    AgentEnrollmentGrant,
    AgentIssuedCertificateRevocation,
    AgentNode,
)
from ..pki import CertificateAuthority, IssuedCertificate
from ..step_ca import StepCAError, StepCAIssuancePending, StepCAUnavailable

_LOGGER = logging.getLogger("vonk_control.enrollment")

from .persistence import (
    _issuance_binding,
    _issued,
    _lock_node_issuance,
    _locked_enrollment,
    _persist_issued_enrollment,
    _replay_matches,
    _require_issuance_binding,
    _validate_issued_binding,
)
from .types import (
    MAX_ENROLLMENT_GRANT_TTL_SECONDS,
    CertificateResponseCapacityRefused,
    EnrollmentDenied,
    EnrollmentGrant,
    EnrollmentIssuanceUncertain,
    _IssuanceClaim,
)


class EnrollmentCore:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        authority: CertificateAuthority,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._sessions = sessions
        self._authority = authority
        self._clock = clock
        # SQLite's local fixtures lack row locks. Guard only their short SQL
        # transactions; PostgreSQL uses its durable row/advisory claims and
        # never holds a process-wide lock across SQL waits or provider HTTP.
        self._sqlite_transaction_lock = threading.RLock()

    @contextmanager
    def _transaction(self) -> Iterator[Session]:
        with self._sessions() as session:
            guard = (
                self._sqlite_transaction_lock
                if session.get_bind().dialect.name == "sqlite"
                else nullcontext()
            )
            with guard, session.begin():
                yield session

    def create(
        self, node_id: str | None, actor: str, ttl_seconds: int
    ) -> EnrollmentGrant:
        return self._create(
            node_id,
            actor,
            ttl_seconds,
            purpose=EnrollmentPurpose.NEW_NODE,
            requested_display_name=None,
            request_key=str(uuid.uuid4()),
        )

    def create_named(
        self,
        display_name: str,
        actor: str,
        ttl_seconds: int,
        *,
        request_key: str,
    ) -> EnrollmentGrant:
        """Create a one-time grant whose approved name is bound on enrollment."""
        normalized = " ".join(display_name.split())
        if not 1 <= len(normalized) <= 200:
            raise ValueError(
                "enrollment display name must be between one and 200 characters"
            )
        return self._create(
            None,
            actor,
            ttl_seconds,
            purpose=EnrollmentPurpose.NEW_NODE,
            requested_display_name=normalized,
            request_key=request_key,
        )

    def create_reenrollment(
        self, node_id: str | None, actor: str, ttl_seconds: int, *, request_key: str
    ) -> EnrollmentGrant:
        """Authorize an explicit replacement of a Spark identity.

        An unbound grant deliberately supports controller database recovery:
        the CSR-derived node identity remains cryptographically bound to the
        one-time grant when the former node row no longer exists.
        """
        return self._create(
            node_id,
            actor,
            ttl_seconds,
            purpose=EnrollmentPurpose.RE_ENROLL,
            requested_display_name=None,
            request_key=request_key,
        )

    def _create(
        self,
        node_id: str | None,
        actor: str,
        ttl_seconds: int,
        *,
        purpose: EnrollmentPurpose,
        requested_display_name: str | None,
        request_key: str,
    ) -> EnrollmentGrant:
        if node_id is not None:
            _validate_node_id(node_id)
        _validate_actor(actor)
        if re.fullmatch(ENROLLMENT_ID_PATTERN, request_key) is None:
            raise ValueError("enrollment request key must be a canonical UUID4")
        if not 0 < ttl_seconds <= MAX_ENROLLMENT_GRANT_TTL_SECONDS:
            raise ValueError(
                "enrollment grant TTL must be between one and "
                f"{MAX_ENROLLMENT_GRANT_TTL_SECONDS} seconds"
            )
        now = _utc(self._clock())
        token_bytes = secrets.token_bytes(32)
        token = base64.urlsafe_b64encode(token_bytes).rstrip(b"=").decode("ascii")
        grant = AgentEnrollmentGrant(
            id=request_key,
            node_id=node_id,
            purpose=purpose,
            requested_display_name=requested_display_name,
            token_digest=_digest(token_bytes),
            created_by=actor,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        try:
            with self._transaction() as session:
                session.add(grant)
        except IntegrityError as error:
            with self._sessions() as session:
                existing = session.get(AgentEnrollmentGrant, request_key)
            if existing is None:
                raise
            raise EnrollmentDenied(
                "enrollment grant identity already exists; inspect its status"
            ) from error
        return EnrollmentGrant(
            id=grant.id,
            node_id=node_id,
            expires_at=grant.expires_at,
            purpose=purpose,
            token=token,
        )

    def _grant_status(self, grant: AgentEnrollmentGrant) -> EnrollmentGrantStatus:
        now = _utc(self._clock())
        return EnrollmentGrantStatus.model_validate(
            {
                "id": grant.id,
                "state": (
                    EnrollmentGrantState.REVOKED
                    if grant.revoked_at is not None
                    else EnrollmentGrantState.CONSUMED
                    if grant.consumed_at is not None
                    else EnrollmentGrantState.EXPIRED
                    if _stored_utc(grant.expires_at) <= now
                    else EnrollmentGrantState.PENDING
                ),
                "purpose": EnrollmentPurpose(grant.purpose),
                "node_id": grant.node_id,
                "display_name": grant.requested_display_name,
                "expires_at": _stored_utc(grant.expires_at),
                "consumed_at": _stored_utc(grant.consumed_at)
                if grant.consumed_at is not None
                else None,
                "revoked_at": _stored_utc(grant.revoked_at)
                if grant.revoked_at is not None
                else None,
            }
        )

    def grant_status(self, grant_id: str, *, actor: str) -> EnrollmentGrantStatus:
        with self._sessions() as session:
            grant = session.get(AgentEnrollmentGrant, grant_id)
            if grant is None or grant.created_by != actor:
                raise KeyError(grant_id)
            return self._grant_status(grant)

    def revoke_grant(self, grant_id: str, *, actor: str) -> EnrollmentGrantStatus:
        # Submit takes this same row first. Revocation either wins before
        # consumption, or refuses without undoing an issued certificate.
        with self._transaction() as session:
            grant = session.get(AgentEnrollmentGrant, grant_id, with_for_update=True)
            if grant is None or grant.created_by != actor:
                raise KeyError(grant_id)
            current = self._grant_status(grant)
            if current.state == EnrollmentGrantState.CONSUMED:
                raise EnrollmentDenied(
                    "enrollment grant is consumed; inspect the enrolled Spark"
                )
            if current.state == EnrollmentGrantState.PENDING:
                grant.revoked_at = _utc(self._clock())
                session.flush()
            return self._grant_status(grant)

    def _submit_once(
        self, token: str, csr: bytes, evidence: Mapping[str, str]
    ) -> IssuedCertificate:
        token_bytes = _decode_token(token)
        now = _utc(self._clock())
        self._reconcile_competing_enrollment(token_bytes, csr, evidence, now)
        failure: str | None = None
        outcome: IssuedCertificate | None = None
        claim: _IssuanceClaim | None = None
        wait_for_enrollment_id: str | None = None
        with self._transaction() as session:
            grant = session.scalar(
                select(AgentEnrollmentGrant)
                .where(AgentEnrollmentGrant.token_digest == _digest(token_bytes))
                .with_for_update(of=AgentEnrollmentGrant)
            )
            if grant is None:
                failure = "invalid enrollment grant"
            elif grant.revoked_at is not None:
                failure = "enrollment grant is revoked"
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
                else:
                    wait_for_enrollment_id = enrollment.id
            elif _stored_utc(grant.expires_at) <= now:
                failure = "enrollment grant is expired"
            else:
                try:
                    if not isinstance(csr, bytes) or len(csr) > MAX_CSR_BYTES:
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
                        state=EnrollmentRecordState.ISSUING,
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
                    if (
                        grant.purpose == EnrollmentPurpose.NEW_NODE
                        and existing_node is not None
                    ):
                        failure = "node identity already exists"
                    elif (
                        grant.purpose == EnrollmentPurpose.RE_ENROLL
                        and existing_node is not None
                    ):
                        if (
                            existing_node.state != NodeIdentityState.ACTIVE
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
                            AgentEnrollment.state == EnrollmentRecordState.ISSUING,
                        )
                        .with_for_update(of=AgentEnrollment)
                        .limit(1)
                    )
                    if competing is not None:
                        failure = "node enrollment issuance is in progress"
                    grant.node_id = node_id
                    grant.consumed_at = now
                    if failure is None:
                        generation = (
                            session.scalar(
                                select(AgentCertificate.generation)
                                .where(AgentCertificate.node_id == node_id)
                                .order_by(AgentCertificate.generation.desc())
                                .limit(1)
                            )
                            or 0
                        ) + 1
                        binding = self._authority.prepare_request(
                            node_id,
                            csr_pem,
                            now,
                            purpose=CertificateIssuancePurpose.ENROLLMENT,
                            source_serial=None,
                            generation=generation,
                        )
                        enrollment.provider_request = binding.model_dump(mode="json")
                        session.add(enrollment)
                        claim = _IssuanceClaim(
                            enrollment_id=enrollment.id,
                            node_id=node_id,
                            csr_pem=csr_pem,
                            purpose=EnrollmentPurpose(grant.purpose),
                            provider_request=binding,
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
        return self._issue_enrollment_claim(claim, now)

    def _issue_enrollment_claim(
        self, claim: _IssuanceClaim, now: datetime
    ) -> IssuedCertificate:
        if claim.provider_request is None:
            with self._transaction() as session:
                accepted = _locked_enrollment(session, claim.enrollment_id)
                if _issuance_binding(accepted.provider_request) is None:
                    session.delete(accepted)
            raise EnrollmentIssuanceUncertain(
                "historical certificate issuance has no exact journal binding"
            )
        with self._transaction() as session:
            accepted = _locked_enrollment(session, claim.enrollment_id)
            grant = session.get(AgentEnrollmentGrant, accepted.grant_id)
            if grant is None or grant.revoked_at is not None:
                raise EnrollmentDenied("enrollment grant is revoked or missing")
            _require_issuance_binding(accepted.provider_request, claim.provider_request)
            node = session.get(AgentNode, claim.node_id)
            if node is not None and (
                node.state != NodeIdentityState.ACTIVE or node.revoked_at is not None
            ):
                raise EnrollmentDenied("node identity is retired or revoked")
            if accepted.state == EnrollmentRecordState.CERTIFICATE_ISSUED:
                try:
                    return _issued(accepted)
                except RuntimeError:
                    accepted.state = EnrollmentRecordState.ISSUING
        try:
            try:
                issued = self._authority.observe_node(
                    claim.csr_pem, now, request=claim.provider_request
                )
            except StepCAIssuancePending:
                issued = None
            if issued is None:
                issued = self._authority.issue_node(
                    claim.node_id,
                    claim.csr_pem,
                    now,
                    request=claim.provider_request,
                )
        except EnrollmentDenied:
            raise
        except Exception as error:
            if (
                isinstance(error, StepCAError)
                and error.reason_code == CertificateCode.RESPONSE_UNREPRESENTABLE
            ):
                raise CertificateResponseCapacityRefused(
                    "certificate response exceeds the supported wire budget"
                ) from error
            if isinstance(error, StepCAError) and not isinstance(
                error, StepCAUnavailable
            ):
                raise EnrollmentDenied(
                    "certificate authority verification failed"
                ) from error
            # The client only learns that issuance is uncertain.  Operators
            # still need the provider cause and traceback to reconcile a
            # stuck node, keyed by the node identity that owns the claim.
            # The grant token and CSR never appear in the message or the
            # provider error, and the client-facing detail is unchanged.
            _LOGGER.exception(
                "agent certificate issuance failed for node %s",
                claim.node_id,
                extra={"failure_type": type(error).__name__},
            )
            raise EnrollmentIssuanceUncertain(
                "certificate issuance observation is unavailable"
            ) from error
        _validate_issued_binding(issued, claim.provider_request)
        try:
            with self._transaction() as session:
                enrollment = _locked_enrollment(session, claim.enrollment_id)
                _require_issuance_binding(
                    enrollment.provider_request, claim.provider_request
                )
                if enrollment.state == EnrollmentRecordState.CERTIFICATE_ISSUED:
                    return _issued(enrollment)
                enrollment.state = EnrollmentRecordState.ISSUING
                existing = session.get(AgentCertificate, issued.serial)
                if (
                    existing is not None
                    and existing.node_id == claim.node_id
                    and existing.revoked_at is None
                ):
                    # The verified exact CA response repairs its local projection.
                    enrollment.state = EnrollmentRecordState.CERTIFICATE_ISSUED
                    enrollment.certificate_pem = issued.certificate_pem.decode("ascii")
                    enrollment.chain_pem = issued.chain_pem.decode("ascii")
                    enrollment.certificate_serial = issued.serial
                    enrollment.certificate_fingerprint = issued.fingerprint
                    enrollment.certificate_generation = issued.generation
                    enrollment.certificate_not_before = issued.not_before
                    enrollment.certificate_not_after = issued.not_after
                    return issued
                if (
                    claim.purpose == EnrollmentPurpose.NEW_NODE
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
                    raise EnrollmentIssuanceUncertain(
                        "certificate generation was not persisted"
                    )
                issued = replace(issued, generation=enrollment.certificate_generation)
        except SQLAlchemyError as error:
            # The durable issuing state was committed before the provider
            # call. Retry observes that exact provider journal entry before
            # a same-binding issue can resume through the CA epoch fence.
            raise EnrollmentIssuanceUncertain(
                "certificate persistence observation is unavailable"
            ) from error
        return issued

    def _wait_for_issuance(self, enrollment_id: str) -> IssuedCertificate:
        now = _utc(self._clock())
        with self._transaction() as session:
            enrollment = _locked_enrollment(session, enrollment_id)
            grant = session.get(AgentEnrollmentGrant, enrollment.grant_id)
            if grant is None or grant.revoked_at is not None:
                raise EnrollmentDenied("enrollment grant is revoked or missing")
            if enrollment.state == EnrollmentRecordState.CERTIFICATE_ISSUED:
                try:
                    return _issued(enrollment)
                except RuntimeError:
                    pass
            enrollment.state = EnrollmentRecordState.ISSUING
            claim = _IssuanceClaim(
                enrollment_id=enrollment.id,
                node_id=enrollment.node_id,
                csr_pem=enrollment.csr_pem.encode("ascii"),
                purpose=EnrollmentPurpose(grant.purpose),
                provider_request=_issuance_binding(enrollment.provider_request),
            )
        return self._issue_enrollment_claim(claim, now)

    def _confirm_remote_revocation(
        self,
        node_id: str,
        serial: str,
        now: datetime,
    ) -> None:
        with self._transaction() as session:
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
                    evidence.state = CertificateRecordState.REVOKED
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
                evidence.state = CertificateRecordState.REVOKED
                evidence.updated_at = now
                evidence.ca_revoked_at = evidence.ca_revoked_at or now
            if (
                certificate is not None
                and intent is not None
                and intent.state == CertificateRotationState.REVOCATION_PENDING
                and certificate.generation == intent.generation
            ):
                session.delete(intent)

    def _reconcile_competing_enrollment(
        self, token_bytes: bytes, csr: bytes, evidence: Mapping[str, str], now: datetime
    ) -> None:
        """A valid fresh grant drives exact observation of an older issuance gate."""
        with self._sessions() as session:
            grant = session.scalar(
                select(AgentEnrollmentGrant).where(
                    AgentEnrollmentGrant.token_digest == _digest(token_bytes)
                )
            )
            if (
                grant is None
                or grant.revoked_at is not None
                or grant.consumed_at is not None
                or _stored_utc(grant.expires_at) <= now
            ):
                return
            try:
                if not isinstance(csr, bytes) or len(csr) > MAX_CSR_BYTES:
                    return
                _, _, fingerprint, node_id = _load_csr(grant.node_id, csr)
            except EnrollmentDenied:
                return
            _, failure = _validate_evidence(
                evidence, grant.node_id, node_id, fingerprint
            )
            if failure is not None:
                return
            competing = session.scalar(
                select(AgentEnrollment.id).where(
                    AgentEnrollment.node_id == node_id,
                    AgentEnrollment.state == EnrollmentRecordState.ISSUING,
                )
            )
        if competing is not None:
            try:
                self._wait_for_issuance(competing)
            except (EnrollmentDenied, EnrollmentIssuanceUncertain):
                # Only a locally ended historical gate may be passed. Revocation,
                # identity mismatch and changed binding still refuse ingress.
                with self._sessions() as session:
                    ended = session.get(AgentEnrollment, competing)
                    released = ended is None
                if not released:
                    raise
