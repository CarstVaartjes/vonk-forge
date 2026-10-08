"""Durable token-authorized enrollment for immutable GPU node identities."""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import datetime, timedelta

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from vonk_agent_protocol import ErrorCategory, LifecycleState, UnknownError, WaitReason
from vonk_agent_protocol.enrollment import ExpiredRenewRequest
from vonk_agent_protocol.state_machines import (
    CertificateRecordState,
    EnrollmentRecordState,
    NodeIdentityState,
)

from ..enrollment_contract import (
    EnrollmentObservationOutcome,
    EnrollmentRevocationStatus,
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
    AgentEnrollment,
    AgentIssuedCertificateRevocation,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Job,
)
from ..pki import IssuedCertificate
from ..step_ca import StepCAIssuancePending, StepCAUnavailable
from .persistence import _issuance_binding
from .rotation import RotationService
from .types import (
    EnrollmentDenied,
    EnrollmentIssuanceUncertain,
    ExpiredRenewalGraceExhausted,
    RemoteRevocationUncertain,
    RenewalConflictRevocationUncertain,
    RenewalInProgress,
    RenewalIssuanceUncertain,
)


def _require_expired_source(node: AgentNode | None) -> None:
    """The same source authority is checked before observation and adoption."""
    if (
        node is None
        or node.state != NodeIdentityState.ACTIVE
        or node.revoked_at is not None
    ):
        raise EnrollmentDenied("expired renewal identity is removed or revoked")


class EnrollmentService(RotationService):
    def _repair_certificate_material(self, node_id: str, serial: str) -> bool:
        """Re-observe an exact reference before using a damaged local key copy."""
        with self._sessions() as session:
            certificate = session.get(AgentCertificate, serial)
            if certificate is None or certificate.node_id != node_id:
                return False
            try:
                if certificate.certificate_pem is not None:
                    x509.load_pem_x509_certificate(
                        certificate.certificate_pem.encode("ascii")
                    )
                    return True
            except (ValueError, UnicodeError):
                pass
            binding = _issuance_binding(certificate.provider_request)
            material = certificate.csr_pem
            if binding is None or material is None:
                enrollment = session.scalar(
                    select(AgentEnrollment).where(
                        AgentEnrollment.certificate_serial == serial
                    )
                )
                if enrollment is not None:
                    binding = _issuance_binding(enrollment.provider_request)
                    material = enrollment.csr_pem
        if binding is None or material is None:
            return False
        try:
            issued = self._authority.observe_node(
                material.encode("ascii"), _utc(self._clock()), request=binding
            )
            if issued is None:
                return False
            from .persistence import _validate_issued_binding

            _validate_issued_binding(issued, binding)
            with self._transaction() as session:
                current = session.get(AgentCertificate, serial, with_for_update=True)
                if (
                    current is None
                    or current.revoked_at is not None
                    or current.node_id != node_id
                ):
                    return False
                current.certificate_pem = issued.certificate_pem.decode("ascii")
                current.chain_pem = issued.chain_pem.decode("ascii")
            return True
        except (
            EnrollmentIssuanceUncertain,
            StepCAUnavailable,
            RuntimeError,
            OSError,
            ValueError,
            SQLAlchemyError,
        ):
            return False

    def revocation_status(self, node_id: str) -> EnrollmentRevocationStatus:
        with self._sessions() as session:
            node = session.get(AgentNode, node_id)
            pending = session.scalar(
                select(AgentCertificate.serial)
                .where(
                    AgentCertificate.node_id == node_id,
                    AgentCertificate.revoked_at.is_not(None),
                    AgentCertificate.ca_revoked_at.is_(None),
                )
                .limit(1)
            )
            orphan = session.scalar(
                select(AgentIssuedCertificateRevocation.serial)
                .where(
                    AgentIssuedCertificateRevocation.node_id == node_id,
                    AgentIssuedCertificateRevocation.ca_revoked_at.is_(None),
                )
                .limit(1)
            )
        return EnrollmentRevocationStatus(
            local_denial_complete=node is None or node.revoked_at is not None,
            ca_confirmation_complete=pending is None and orphan is None,
        )

    def reconcile_revocations(self) -> bool:
        """Worker-owned, restart-safe confirmation without any admission gate.

        Each provider observation has its transport deadline. Failed serials
        back off independently; other nodes and newer requests keep progressing.
        An ended exact binding is only observed, never issued or adopted.
        """
        now = _utc(self._clock())
        from .ending import retain_ended_effect

        with self._transaction() as session:
            abandoned = list(
                session.scalars(
                    select(AgentEnrollment)
                    .where(
                        AgentEnrollment.state == EnrollmentRecordState.ISSUING,
                        AgentEnrollment.created_at <= now - timedelta(minutes=5),
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            for accepted in abandoned:
                retain_ended_effect(
                    session,
                    _issuance_binding(accepted.provider_request),
                    accepted.csr_pem,
                    now,
                )
                accepted.state = EnrollmentRecordState.ENDED
            abandoned_rotations = list(
                session.scalars(
                    select(AgentCertificateRotation)
                    .where(
                        AgentCertificateRotation.created_at
                        <= now - timedelta(minutes=5),
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            for accepted_rotation in abandoned_rotations:
                retain_ended_effect(
                    session,
                    _issuance_binding(accepted_rotation.provider_request),
                    accepted_rotation.csr_pem,
                    now,
                )
                session.delete(accepted_rotation)
        with self._sessions() as session:
            certificates = list(
                session.scalars(
                    select(AgentCertificate).where(
                        AgentCertificate.revoked_at.is_not(None),
                        AgentCertificate.ca_revoked_at.is_(None),
                        (
                            AgentCertificate.ca_next_attempt_at.is_(None)
                            | (AgentCertificate.ca_next_attempt_at <= now)
                        ),
                    )
                )
            )
            evidence = list(
                session.scalars(
                    select(AgentIssuedCertificateRevocation).where(
                        AgentIssuedCertificateRevocation.ca_revoked_at.is_(None),
                        (
                            AgentIssuedCertificateRevocation.next_attempt_at.is_(None)
                            | (AgentIssuedCertificateRevocation.next_attempt_at <= now)
                        ),
                    )
                )
            )
        changed = False
        for row in evidence:
            binding = _issuance_binding(row.provider_request)
            if binding is not None and datetime.fromisoformat(binding.not_after) <= now:
                continue  # Expired bytes can no longer authenticate ingress.
            # Persist the next check before HTTP: process death cannot hot-loop.
            with self._transaction() as session:
                current = session.get(AgentIssuedCertificateRevocation, row.serial)
                if current is None or current.ca_revoked_at is not None:
                    continue
                current.next_attempt_at = now + timedelta(seconds=5)
            try:
                if binding is not None:
                    if row.csr_pem is None:
                        continue
                    issued = self._authority.observe_node(
                        row.csr_pem.encode("ascii"), now, request=binding
                    )
                    if issued is None:
                        continue
                self._authority.revoke_node(row.serial, now)
                self._confirm_remote_revocation(row.node_id, row.serial, now)
                changed = True
            except (
                StepCAUnavailable,
                EnrollmentIssuanceUncertain,
                RuntimeError,
                ValueError,
                SQLAlchemyError,
            ):
                continue
        for row in certificates:
            if _stored_utc(row.not_after) <= now:
                continue
            with self._transaction() as session:
                current_certificate = session.get(
                    AgentCertificate, row.serial, with_for_update=True
                )
                if (
                    current_certificate is None
                    or current_certificate.ca_revoked_at is not None
                ):
                    continue
                current_certificate.ca_next_attempt_at = now + timedelta(seconds=5)
            try:
                self._authority.revoke_node(row.serial, now)
                self._confirm_remote_revocation(row.node_id, row.serial, now)
                changed = True
            except (
                StepCAUnavailable,
                EnrollmentIssuanceUncertain,
                RuntimeError,
                ValueError,
                SQLAlchemyError,
            ):
                continue
        return changed

    def activate(
        self, node_id: str, serial: str, generation: int
    ) -> None | UnknownError:
        for delay in (0.0, 0.05, 0.1, 0.2):
            if delay:
                time.sleep(delay)
            try:
                return self._activate_once(node_id, serial, generation)
            except (SQLAlchemyError, EnrollmentIssuanceUncertain):
                pass
        return EnrollmentObservationOutcome(
            category=ErrorCategory.UNKNOWN,
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
            state=LifecycleState.FAILED,
        )

    def _activate_once(self, node_id: str, serial: str, generation: int) -> None:
        _validate_node_id(node_id)
        if (
            not serial.strip()
            or not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
        ):
            raise ValueError("certificate activation identity is invalid")
        now = _utc(self._clock())
        with self._transaction() as session:
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
            if node.state != NodeIdentityState.ACTIVE or node.revoked_at is not None:
                raise EnrollmentDenied("node identity is retired or revoked")
            if certificate.generation != generation:
                raise EnrollmentDenied("certificate generation does not match")
            if (
                certificate.state == CertificateRecordState.ACTIVE
                and certificate.revoked_at is None
            ):
                return
            if (
                certificate.state != CertificateRecordState.STAGED
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
            # Rotation changes the credential, not a live operation's authority.
            # Transfer only an unexpired current attempt from an active source;
            # keep its fence and deadline, and never revive revoked/finished work.
            live_sources = [
                previous.serial
                for previous in older
                if previous.state == CertificateRecordState.ACTIVE
                and previous.revoked_at is None
                and previous.ca_revoked_at is None
                and _stored_utc(previous.not_before) <= now
                and _stored_utc(previous.not_after) > now
            ]
            if live_sources:
                current_operations = (
                    select(AgentOperation.id)
                    .join(Job, Job.id == AgentOperation.parent_job_id)
                    .where(
                        AgentOperation.node_id == node_id,
                        AgentOperation.state == LifecycleState.RUNNING,
                        AgentOperation.current_attempt == AgentOperationAttempt.attempt,
                        AgentOperation.authority_revision == Job.authority_revision,
                        Job.state.in_((LifecycleState.QUEUED, LifecycleState.RUNNING)),
                    )
                )
                session.execute(
                    update(AgentOperationAttempt)
                    .where(
                        AgentOperationAttempt.agent_certificate_serial.in_(
                            live_sources
                        ),
                        AgentOperationAttempt.state == LifecycleState.RUNNING,
                        AgentOperationAttempt.lease_deadline > now,
                        AgentOperationAttempt.operation_id.in_(current_operations),
                    )
                    .values(agent_certificate_serial=serial)
                    .execution_options(synchronize_session=False)
                )
            certificate.state = CertificateRecordState.ACTIVE
            for previous in older:
                previous.state = CertificateRecordState.REVOKED
                previous.revoked_at = previous.revoked_at or now

    def _revoke_node_once(self, node_id: str, actor: str) -> None:
        """Retire locally before best-effort provider revocation.

        Retrying is safe: local state remains denied and provider revocation is
        monotonic. An uncertain provider response is never allowed to restore
        ingress access.
        """
        _validate_node_id(node_id)
        _validate_actor(actor)
        now = _utc(self._clock())
        with self._transaction() as session:
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
                return
            if node is not None:
                node.state = NodeIdentityState.RETIRED
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
                certificate.state = CertificateRecordState.REVOKED
                certificate.revoked_at = certificate.revoked_at or now
        uncertain = False
        for serial in serials:
            try:
                self._authority.revoke_node(serial, now)
            except (StepCAUnavailable, RuntimeError, OSError, ValueError):
                uncertain = True
            else:
                self._confirm_remote_revocation(node_id, serial, now)
        if uncertain:
            raise RemoteRevocationUncertain(
                "local revocation complete; remote CA revocation is uncertain"
            )

    def renew_expired(
        self, proof: ExpiredRenewRequest
    ) -> IssuedCertificate | UnknownError:
        for delay in (0.0, 0.05, 0.1, 0.2):
            if delay:
                time.sleep(delay)
            try:
                return self._renew_expired_once(proof)
            except (SQLAlchemyError, EnrollmentIssuanceUncertain):
                pass
        return EnrollmentObservationOutcome(
            category=ErrorCategory.UNKNOWN,
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
            state=LifecycleState.FAILED,
        )

    def _renew_expired_once(
        self, proof: ExpiredRenewRequest
    ) -> IssuedCertificate | UnknownError:
        """Authenticate the stored active key; never relax work/activation mTLS.

        Fresh proofs bind a durable CSR, so replay uses the existing journal
        and cannot choose another key or issue another generation.
        """
        with self._sessions() as session:
            node = session.get(AgentNode, proof.node_id)
            _require_expired_source(node)
        for delay in (0.0, 0.05, 0.1, 0.2):
            if delay:
                time.sleep(delay)
            if self._repair_certificate_material(proof.node_id, proof.serial):
                break
        else:
            return EnrollmentObservationOutcome(
                category=ErrorCategory.UNKNOWN,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
                state=LifecycleState.FAILED,
            )
        now = _utc(self._clock())
        with self._transaction() as session:
            node = session.get(AgentNode, proof.node_id)
            certificate = session.get(AgentCertificate, proof.serial)
            _require_expired_source(node)
            if certificate is None or certificate.certificate_pem is None:
                raise EnrollmentIssuanceUncertain(
                    "expired certificate projection is unavailable"
                )
            if (
                certificate.node_id != proof.node_id
                or certificate.state != CertificateRecordState.ACTIVE
                or certificate.revoked_at is not None
            ):
                raise EnrollmentDenied("expired certificate authority is revoked")
            expiry = _stored_utc(certificate.not_after)
            if now < expiry or now > expiry + timedelta(days=30):
                raise ExpiredRenewalGraceExhausted(
                    "expired renewal grace exhausted; re-enrollment required"
                )
            if abs(int(now.timestamp()) - proof.signed_at) > 300:
                raise EnrollmentDenied("expired renewal proof is stale")
            try:
                public_key = x509.load_pem_x509_certificate(
                    certificate.certificate_pem.encode("ascii")
                ).public_key()
            except (ValueError, UnicodeError) as error:
                raise EnrollmentIssuanceUncertain(
                    "expired certificate projection is unavailable"
                ) from error
            if not isinstance(public_key, ed25519.Ed25519PublicKey):
                raise EnrollmentDenied("expired renewal key is not Ed25519")
            try:
                public_key.verify(bytes.fromhex(proof.signature), proof.proof_bytes())
            except (InvalidSignature, ValueError, UnicodeEncodeError):
                raise EnrollmentDenied("expired renewal proof is invalid") from None
        # Admission rechecks removal/revocation and grace under the existing
        # node/certificate locks. No transaction spans provider I/O.
        return self.recover_rotation(
            proof.node_id, proof.serial, proof.csr.encode("ascii"), expired=True
        )

    def submit(
        self, token: str, csr: bytes, evidence: Mapping[str, str]
    ) -> IssuedCertificate | UnknownError:
        """Bounded bookkeeping retries; issuance uncertainty preserves its typed handoff."""
        end_owner = False
        for delay in (0.0, 0.05, 0.1, 0.2):
            if delay:
                time.sleep(delay)
            try:
                return self._submit_once(token, csr, evidence)
            except (RenewalInProgress, StepCAIssuancePending):
                # A follower ends observation, not the CA owner's accepted work.
                pass
            except (
                SQLAlchemyError,
                EnrollmentIssuanceUncertain,
                RenewalIssuanceUncertain,
                RenewalConflictRevocationUncertain,
                RemoteRevocationUncertain,
            ):
                end_owner = True
        ended = False
        for delay in (0.0, 0.05, 0.1, 0.2):
            if delay:
                time.sleep(delay)
            try:
                if end_owner:
                    self._end_submission(token)
                    ended = True
                break
            except (SQLAlchemyError, EnrollmentIssuanceUncertain):
                pass
        return EnrollmentObservationOutcome(
            category=ErrorCategory.UNKNOWN,
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
            state=LifecycleState.FAILED if ended else LifecycleState.OBSERVING,
        )

    def renew(
        self, node_id: str, serial: str, csr: bytes, *, expired: bool = False
    ) -> IssuedCertificate | UnknownError:
        """Bounded bookkeeping retries; issuance uncertainty preserves its typed handoff."""
        end_owner = False
        for delay in (0.0, 0.05, 0.1, 0.2):
            if delay:
                time.sleep(delay)
            try:
                return self._renew_once(node_id, serial, csr, expired=expired)
            except (RenewalInProgress, StepCAIssuancePending):
                # A follower ends observation, not the CA owner's accepted work.
                pass
            except (
                SQLAlchemyError,
                EnrollmentIssuanceUncertain,
                RenewalIssuanceUncertain,
                RenewalConflictRevocationUncertain,
                RemoteRevocationUncertain,
            ):
                end_owner = True
        ended = False
        for delay in (0.0, 0.05, 0.1, 0.2):
            if delay:
                time.sleep(delay)
            try:
                if end_owner:
                    self._end_rotation(node_id, csr=csr, serial=serial)
                    ended = True
                break
            except (SQLAlchemyError, EnrollmentIssuanceUncertain):
                pass
        return EnrollmentObservationOutcome(
            category=ErrorCategory.UNKNOWN,
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
            state=LifecycleState.FAILED if ended else LifecycleState.OBSERVING,
        )

    def recover_rotation(
        self, node_id: str, serial: str, csr: bytes, *, expired: bool = False
    ) -> IssuedCertificate | UnknownError:
        """Bounded bookkeeping retries; issuance uncertainty preserves its typed handoff."""
        end_owner = False
        for delay in (0.0, 0.05, 0.1, 0.2):
            if delay:
                time.sleep(delay)
            try:
                return self._recover_rotation_once(
                    node_id, serial, csr, expired=expired
                )
            except (RenewalInProgress, StepCAIssuancePending):
                # A follower ends observation, not the CA owner's accepted work.
                pass
            except (
                SQLAlchemyError,
                EnrollmentIssuanceUncertain,
                RenewalIssuanceUncertain,
                RenewalConflictRevocationUncertain,
                RemoteRevocationUncertain,
            ):
                end_owner = True
        ended = False
        for delay in (0.0, 0.05, 0.1, 0.2):
            if delay:
                time.sleep(delay)
            try:
                if end_owner:
                    self._end_rotation(node_id, csr=csr, serial=serial)
                    ended = True
                break
            except (SQLAlchemyError, EnrollmentIssuanceUncertain):
                pass
        return EnrollmentObservationOutcome(
            category=ErrorCategory.UNKNOWN,
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
            state=LifecycleState.FAILED if ended else LifecycleState.OBSERVING,
        )

    def revoke_node(self, node_id: str, actor: str) -> None | UnknownError:
        """Observe exact durable intent within a bounded exponential backoff."""
        for delay in (0.0, 0.05, 0.1, 0.2):
            if delay:
                time.sleep(delay)
            try:
                return self._revoke_node_once(node_id, actor)
            except (
                SQLAlchemyError,
                EnrollmentIssuanceUncertain,
                RenewalIssuanceUncertain,
                RenewalConflictRevocationUncertain,
                RemoteRevocationUncertain,
                RenewalInProgress,
            ):
                pass
        return UnknownError(
            category=ErrorCategory.UNKNOWN, reason=WaitReason.OBSERVATION_UNAVAILABLE
        )
