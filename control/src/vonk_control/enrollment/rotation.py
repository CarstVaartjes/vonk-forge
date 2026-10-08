"""Durable token-authorized enrollment for immutable GPU node identities."""

from __future__ import annotations

import secrets
from dataclasses import replace
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from vonk_agent_protocol.state_machines import (
    CertificateIssuancePurpose,
    CertificateRecordState,
    CertificateRotationState,
    NodeIdentityState,
)

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
    AgentIssuedCertificateRevocation,
    AgentNode,
)
from ..pki import IssuedCertificate
from ..step_ca import StepCAError, StepCAIssuancePending, StepCAUnavailable
from .issuance import EnrollmentCore
from .persistence import (
    _certificate_issued,
    _issuance_binding,
    _rotation_claim,
    _rotation_source_valid,
    _validate_issued_binding,
)
from .types import (
    EnrollmentDenied,
    RenewalConflictRevocationUncertain,
    RenewalInProgress,
    RenewalIssuanceUncertain,
    _RotationClaim,
    _RotationRecoveryClaim,
)

_ROTATION_ISSUANCE_TIMEOUT = timedelta(minutes=5)


class RotationService(EnrollmentCore):
    def _renew_once(
        self, node_id: str, serial: str, csr: bytes, *, expired: bool = False
    ) -> IssuedCertificate:
        _validate_node_id(node_id)
        if not serial.strip():
            raise ValueError("certificate serial is required")
        normalized_csr, _, csr_fingerprint, _ = _load_csr(node_id, csr)
        now = _utc(self._clock())
        with self._sessions() as session:
            intent = session.get(AgentCertificateRotation, node_id)
            staged = session.scalar(
                select(AgentCertificate).where(
                    AgentCertificate.node_id == node_id,
                    AgentCertificate.state == CertificateRecordState.STAGED,
                    AgentCertificate.revoked_at.is_(None),
                )
            )
            competing = (
                intent is not None
                and (
                    intent.csr_public_key_fingerprint != csr_fingerprint
                    or intent.source_serial != serial
                    or intent.state
                    not in {
                        CertificateRotationState.ISSUING,
                        CertificateRotationState.MANUAL_RECOVERY,
                    }
                    or not intent.csr_pem.isascii()
                )
            ) or (
                staged is not None
                and staged.csr_public_key_fingerprint != csr_fingerprint
            )
        if competing:
            return self._recover_rotation_once(node_id, serial, csr, expired=expired)
        try:
            claim = self._claim_rotation(
                node_id,
                serial,
                normalized_csr,
                csr_fingerprint,
                now,
                expired=expired,
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
                expired=expired,
            )
        if isinstance(claim, IssuedCertificate):
            return claim
        return self._issue_rotation_claim(claim, now)

    def _recover_rotation_once(
        self, node_id: str, serial: str, csr: bytes, *, expired: bool = False
    ) -> IssuedCertificate:
        """Recover an unactivated staged certificate for the durable pending CSR.

        The caller must still authenticate with the active source certificate.
        A conflicting staged certificate is denied locally first, then revoked
        at the CA before the new CSR is issued.  The revocation-pending intent
        is durable so a lost response or process restart cannot cause a second
        issuance or allow an unrevoked alternative identity to remain admitted.
        """
        _validate_node_id(node_id)
        if not serial.strip():
            raise ValueError("certificate serial is required")
        normalized_csr, _, csr_fingerprint, _ = _load_csr(node_id, csr)
        now = _utc(self._clock())
        with self._sessions() as session:
            # Authenticate the source before observing or issuing an older
            # effect on behalf of this recovery request. Admission rechecks it
            # under the owning locks before adopting any replacement.
            node = session.get(AgentNode, node_id)
            source = session.get(AgentCertificate, serial)
            if (
                node is None
                or node.state != NodeIdentityState.ACTIVE
                or node.revoked_at is not None
                or source is None
                or source.node_id != node_id
                or source.state != CertificateRecordState.ACTIVE
                or source.revoked_at is not None
                or _stored_utc(source.not_before) > now
                or not _rotation_source_valid(source, now, expired=expired)
            ):
                raise EnrollmentDenied("rotation source authority is invalid")
            intent = session.get(AgentCertificateRotation, node_id)
            competing = (
                _rotation_claim(intent, owner=False)
                if intent is not None
                and intent.csr_public_key_fingerprint != csr_fingerprint
                and intent.state
                in {
                    CertificateRotationState.ISSUING,
                    CertificateRotationState.MANUAL_RECOVERY,
                }
                else None
            )
        if competing is not None:
            try:
                self._issue_rotation_claim(competing, now)
            except (
                EnrollmentDenied,
                RenewalIssuanceUncertain,
                RenewalInProgress,
                StepCAIssuancePending,
            ):
                # Source authentication above belongs to the fresh request.
                # An obsolete binding's answer cannot deny the newer intent.
                self._end_rotation(node_id)
        recovery = self._prepare_rotation_recovery(
            node_id,
            serial,
            normalized_csr,
            csr_fingerprint,
            now,
            expired=expired,
        )
        if recovery is None:
            return self._renew_once(node_id, serial, normalized_csr, expired=expired)
        try:
            self._authority.revoke_node(recovery.retiring_serial, now)
        except RuntimeError as error:
            raise RenewalConflictRevocationUncertain(
                "obsolete staged certificate revocation observation is unavailable"
            ) from error
        owns_issuance = self._finish_rotation_recovery(
            node_id,
            recovery.retiring_serial,
            recovery.claim,
            now,
        )
        if not owns_issuance:
            return self._renew_once(node_id, serial, normalized_csr, expired=expired)
        issued = self._issue_rotation_claim(recovery.claim, now)
        if recovery.claim.csr_public_key_fingerprint != csr_fingerprint:
            # Finish the older exact binding before the bounded caller observes
            # it and prepares the requested replacement. Never rebind a journal.
            raise RenewalInProgress(
                "completed competing rotation observation is pending"
            )
        return issued

    def _prepare_rotation_recovery(
        self,
        node_id: str,
        serial: str,
        normalized_csr: bytes,
        csr_fingerprint: str,
        now: datetime,
        *,
        expired: bool = False,
    ) -> _RotationRecoveryClaim | None:
        with self._transaction() as session:
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
                    .order_by(AgentCertificate.generation, AgentCertificate.serial)
                    .with_for_update(of=AgentCertificate)
                )
            )
            source = next(
                (candidate for candidate in certificates if candidate.serial == serial),
                None,
            )
            if source is None:
                raise EnrollmentDenied("certificate serial does not identify node")
            if (
                node.state != NodeIdentityState.ACTIVE
                or node.revoked_at is not None
                or source.revoked_at is not None
                or source.state != CertificateRecordState.ACTIVE
                or _stored_utc(source.not_before) > now
                or not _rotation_source_valid(source, now, expired=expired)
            ):
                raise EnrollmentDenied("node identity is retired or revoked")

            intent = session.scalar(
                select(AgentCertificateRotation)
                .where(AgentCertificateRotation.node_id == node_id)
                .with_for_update(of=AgentCertificateRotation)
            )
            if intent is not None:
                if (
                    intent.source_serial != serial
                    or _issuance_binding(intent.provider_request) is None
                    or not intent.csr_pem.isascii()
                ):
                    from .ending import retain_ended_effect

                    retain_ended_effect(
                        session,
                        _issuance_binding(intent.provider_request),
                        intent.csr_pem,
                        now,
                    )
                    session.delete(intent)
                    return None
                if (
                    intent.source_serial != serial
                    or intent.csr_public_key_fingerprint != csr_fingerprint
                    or intent.csr_pem != normalized_csr.decode("ascii")
                ):
                    if intent.state in {
                        CertificateRotationState.ISSUING,
                        CertificateRotationState.MANUAL_RECOVERY,
                    }:
                        raise RenewalIssuanceUncertain(
                            "certificate rotation observation is unavailable"
                        )
                    if (
                        intent.state != CertificateRotationState.REVOCATION_PENDING
                        or intent.source_serial != serial
                    ):
                        from .ending import retain_ended_effect

                        retain_ended_effect(
                            session,
                            _issuance_binding(intent.provider_request),
                            intent.csr_pem,
                            now,
                        )
                        session.delete(intent)
                        return None
                if intent.state == CertificateRotationState.REVOCATION_PENDING:
                    retiring = next(
                        (
                            candidate
                            for candidate in certificates
                            if candidate.generation
                            in {intent.generation - 1, intent.generation}
                            and candidate.state == CertificateRecordState.REVOKED
                            and candidate.revoked_at is not None
                            and candidate.ca_revoked_at is None
                        ),
                        None,
                    )
                    if retiring is None:
                        # No locally admitted target remains. Absence never
                        # authorizes a CA effect or leaves a stale issuance gate.
                        session.delete(intent)
                        return None
                    return _RotationRecoveryClaim(
                        claim=_rotation_claim(intent, owner=True),
                        retiring_serial=retiring.serial,
                    )
                if intent.state in {
                    CertificateRotationState.ISSUING,
                    CertificateRotationState.MANUAL_RECOVERY,
                }:
                    return None
                intent.state = CertificateRotationState.ISSUING
                return None

            staged = next(
                (
                    candidate
                    for candidate in certificates
                    if candidate.state == CertificateRecordState.STAGED
                    and candidate.revoked_at is None
                ),
                None,
            )
            if staged is None or staged.csr_public_key_fingerprint == csr_fingerprint:
                return None
            staged.state = CertificateRecordState.REVOKED
            staged.revoked_at = staged.revoked_at or now
            intent = AgentCertificateRotation(
                node_id=node_id,
                source_serial=serial,
                generation=staged.generation + 1,
                csr_pem=normalized_csr.decode("ascii"),
                csr_public_key_fingerprint=csr_fingerprint,
                provider_request_id=secrets.token_urlsafe(32),
                state=CertificateRotationState.REVOCATION_PENDING,
                created_at=now,
                updated_at=now,
            )
            binding = self._authority.prepare_request(
                node_id,
                normalized_csr,
                now,
                purpose=CertificateIssuancePurpose.ROTATION,
                source_serial=serial,
                generation=intent.generation,
            )
            intent.provider_request = binding.model_dump(mode="json")
            intent.provider_request_id = binding.request_id
            session.add(intent)
            session.flush()
            return _RotationRecoveryClaim(
                claim=_rotation_claim(intent, owner=True), retiring_serial=staged.serial
            )

    def _finish_rotation_recovery(
        self,
        node_id: str,
        retiring_serial: str,
        claim: _RotationClaim,
        now: datetime,
    ) -> bool:
        with self._transaction() as session:
            certificate = session.scalar(
                select(AgentCertificate)
                .where(
                    AgentCertificate.node_id == node_id,
                    AgentCertificate.serial == retiring_serial,
                )
                .with_for_update(of=AgentCertificate)
            )
            intent = session.scalar(
                select(AgentCertificateRotation)
                .where(
                    AgentCertificateRotation.node_id == node_id,
                    AgentCertificateRotation.provider_request_id
                    == claim.provider_request_id,
                )
                .with_for_update(of=AgentCertificateRotation)
            )
            if (
                certificate is None
                or certificate.state != CertificateRecordState.REVOKED
                or certificate.revoked_at is None
                or intent is None
            ):
                raise RenewalConflictRevocationUncertain(
                    "obsolete staged certificate recovery state changed"
                )
            if (
                intent.state == CertificateRotationState.ISSUING
                and certificate.ca_revoked_at is not None
            ):
                return False
            if intent.state != CertificateRotationState.REVOCATION_PENDING:
                raise RenewalConflictRevocationUncertain(
                    "obsolete staged certificate recovery state changed"
                )
            certificate.ca_revoked_at = certificate.ca_revoked_at or now
            intent.state = CertificateRotationState.ISSUING
            intent.updated_at = now
            return True

    def _issue_rotation_claim(
        self, claim: _RotationClaim, now: datetime
    ) -> IssuedCertificate:
        try:
            if claim.provider_request is None:
                with self._transaction() as session:
                    intent = session.get(
                        AgentCertificateRotation, claim.node_id, with_for_update=True
                    )
                    if (
                        intent is not None
                        and intent.provider_request_id == claim.provider_request_id
                    ):
                        session.delete(intent)
                raise RenewalInProgress(
                    "historical certificate rotation has no exact journal binding"
                )
            issued = self._authority.observe_node(
                claim.csr_pem, now, request=claim.provider_request
            )
            if issued is None:
                issued = self._authority.renew_node(
                    claim.node_id,
                    claim.csr_pem,
                    now,
                    request=claim.provider_request,
                )
            _validate_issued_binding(issued, claim.provider_request)
            self._validate_renewal_result(issued, claim)
            disposition = self._persist_rotation(issued, claim)
        except StepCAIssuancePending:
            raise
        except (EnrollmentDenied, RenewalInProgress):
            raise
        except Exception as error:
            if isinstance(error, StepCAError) and not isinstance(
                error, StepCAUnavailable
            ):
                raise EnrollmentDenied(
                    "certificate authority verification failed"
                ) from error
            self._mark_rotation_uncertain(claim, now)
            raise RenewalIssuanceUncertain(
                "certificate rotation observation is unavailable"
            ) from error
        if disposition == CertificateRotationState.REVOCATION_PENDING:
            self._revoke_denied_rotation(issued.serial, claim, now)
            with self._sessions() as session:
                node = session.get(AgentNode, claim.node_id)
                denied = (
                    node is None
                    or node.revoked_at is not None
                    or node.state != NodeIdentityState.ACTIVE
                )
            if denied:
                raise EnrollmentDenied(
                    "node identity retired during certificate rotation; issued certificate revoked"
                )
            raise RenewalInProgress("superseded rotation effect reconciled")
        return replace(issued, generation=claim.generation)

    def _claim_rotation(
        self,
        node_id: str,
        serial: str,
        normalized_csr: bytes,
        csr_fingerprint: str,
        now: datetime,
        *,
        expired: bool = False,
    ) -> _RotationClaim | IssuedCertificate:
        with self._transaction() as session:
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
                node.state != NodeIdentityState.ACTIVE
                or node.revoked_at is not None
                or certificate.revoked_at is not None
            ):
                raise EnrollmentDenied("node identity is retired or revoked")
            if certificate.state != CertificateRecordState.ACTIVE:
                raise EnrollmentDenied("certificate is not active")
            if _stored_utc(certificate.not_before) > now or not _rotation_source_valid(
                certificate, now, expired=expired
            ):
                raise EnrollmentDenied("certificate is not currently valid")
            staged = next(
                (
                    candidate
                    for candidate in certificates
                    if candidate.state == CertificateRecordState.STAGED
                    and candidate.revoked_at is None
                ),
                None,
            )
            if staged is not None:
                if _stored_utc(staged.not_after) <= now:
                    staged.state = CertificateRecordState.REVOKED
                    staged.revoked_at = staged.revoked_at or now
                    session.flush()
                else:
                    if staged.csr_public_key_fingerprint != csr_fingerprint:
                        raise RenewalInProgress(
                            "a different certificate rotation is already staged"
                        )
                    try:
                        return _certificate_issued(staged)
                    except (RuntimeError, ValueError, UnicodeError):
                        binding = _issuance_binding(staged.provider_request)
                        if binding is not None:
                            return _RotationClaim(
                                node_id=node_id,
                                source_serial=serial,
                                generation=binding.generation,
                                csr_pem=normalized_csr,
                                csr_public_key_fingerprint=csr_fingerprint,
                                provider_request_id=binding.request_id,
                                provider_request=binding,
                                state=CertificateRotationState.ISSUING,
                                owner=False,
                            )
                        # No exact reference remains; retire the damaged projection.
                        staged.state = CertificateRecordState.REVOKED
                        staged.revoked_at = staged.revoked_at or now

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
                    raise RenewalInProgress(
                        "a different certificate rotation is already in progress"
                    )
                if (
                    intent.state == CertificateRotationState.ISSUING
                    and intent.provider_request is None
                    and now - _stored_utc(intent.updated_at)
                    >= _ROTATION_ISSUANCE_TIMEOUT
                ):
                    intent.state = CertificateRotationState.MANUAL_RECOVERY
                    intent.updated_at = now
                if (
                    intent.provider_request is not None
                    and intent.state == CertificateRotationState.MANUAL_RECOVERY
                ):
                    intent.state = CertificateRotationState.ISSUING
                # The exact binding, rather than a damaged phase projection,
                # owns observation and adoption.
                intent.state = CertificateRotationState.ISSUING
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
                state=CertificateRotationState.ISSUING,
                created_at=now,
                updated_at=now,
            )
            binding = self._authority.prepare_request(
                node_id,
                normalized_csr,
                now,
                purpose=CertificateIssuancePurpose.ROTATION,
                source_serial=serial,
                generation=generation,
            )
            intent.provider_request = binding.model_dump(mode="json")
            intent.provider_request_id = binding.request_id
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
        with self._transaction() as session:
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
                return CertificateRotationState.REVOCATION_PENDING
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
            if intent is None:
                committed = next(
                    (
                        candidate
                        for candidate in certificates
                        if candidate.serial == issued.serial
                        and candidate.generation == claim.generation
                        and candidate.csr_public_key_fingerprint
                        == claim.csr_public_key_fingerprint
                        and candidate.state
                        in {
                            CertificateRecordState.STAGED,
                            CertificateRecordState.ACTIVE,
                        }
                        and candidate.revoked_at is None
                    ),
                    None,
                )
                if committed is not None:
                    committed.certificate_pem = issued.certificate_pem.decode("ascii")
                    committed.chain_pem = issued.chain_pem.decode("ascii")
                    committed.provider_request = (
                        claim.provider_request.model_dump(mode="json")
                        if claim.provider_request is not None
                        else None
                    )
                    committed.csr_pem = claim.csr_pem.decode("ascii")
                    return CertificateRecordState.STAGED
            if (
                intent is None
                or intent.state != CertificateRotationState.ISSUING
                or _issuance_binding(intent.provider_request) != claim.provider_request
            ):
                raise RenewalInProgress(
                    "certificate rotation issuance identity changed"
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
                node.state != NodeIdentityState.ACTIVE
                or node.revoked_at is not None
                or source is None
                or source.state != CertificateRecordState.ACTIVE
                or source.revoked_at is not None
            )
            state = (
                CertificateRecordState.REVOKED
                if denied
                else CertificateRecordState.STAGED
            )
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
                    provider_request=claim.provider_request.model_dump(mode="json")
                    if claim.provider_request is not None
                    else None,
                    csr_pem=claim.csr_pem.decode("ascii"),
                )
            )
            if denied:
                intent.state = CertificateRotationState.REVOCATION_PENDING
                intent.updated_at = now
                session.flush()
                return CertificateRotationState.REVOCATION_PENDING
            session.delete(intent)
            session.flush()
            return CertificateRecordState.STAGED

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
                # The exact verified provider result owns this serial. Repair
                # damaged local revocation bookkeeping from that result.
                evidence.node_id = claim.node_id
                evidence.provider_request_id = claim.provider_request_id
                evidence.fingerprint = issued.fingerprint
                evidence.generation = claim.generation
                evidence.ca_revoked_at = None
                evidence.state = CertificateRotationState.REVOCATION_PENDING
                evidence.updated_at = now
            return
        session.add(
            AgentIssuedCertificateRevocation(
                serial=issued.serial,
                node_id=claim.node_id,
                provider_request_id=claim.provider_request_id,
                fingerprint=issued.fingerprint,
                generation=claim.generation,
                state=CertificateRotationState.REVOCATION_PENDING,
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
            with self._transaction() as session:
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
                if (
                    intent is not None
                    and intent.state == CertificateRotationState.ISSUING
                ):
                    if intent.provider_request is None:
                        intent.state = CertificateRotationState.MANUAL_RECOVERY
                    intent.updated_at = now
        except SQLAlchemyError:
            # The committed issuing row remains authoritative when the
            # follow-up annotation cannot be stored. Its exact binding still
            # permits provider observation without inventing another effect.
            pass
