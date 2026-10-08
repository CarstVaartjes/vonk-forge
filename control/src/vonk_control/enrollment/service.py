"""Durable token-authorized enrollment for immutable GPU node identities."""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import timedelta

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import select, update
from vonk_agent_protocol import ErrorCategory, LifecycleState, UnknownError, WaitReason
from vonk_agent_protocol.enrollment import ExpiredRenewRequest
from vonk_agent_protocol.state_machines import CertificateRecordState, NodeIdentityState

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
    AgentOperation,
    AgentOperationAttempt,
    Job,
)
from ..pki import IssuedCertificate
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


class EnrollmentService(RotationService):
    def activate(self, node_id: str, serial: str, generation: int) -> None:
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
                raise EnrollmentDenied("node identity does not exist")
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
            except RuntimeError:
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
        """Authenticate the stored active key; never relax work/activation mTLS.

        Fresh proofs bind a durable CSR, so replay uses the existing journal
        and cannot choose another key or issue another generation.
        """
        now = _utc(self._clock())
        with self._transaction() as session:
            node = session.get(AgentNode, proof.node_id)
            certificate = session.get(AgentCertificate, proof.serial)
            if (
                node is None
                or node.state != NodeIdentityState.ACTIVE
                or node.revoked_at is not None
                or certificate is None
                or certificate.node_id != proof.node_id
                or certificate.state != CertificateRecordState.ACTIVE
                or certificate.revoked_at is not None
                or certificate.certificate_pem is None
            ):
                raise EnrollmentDenied("expired renewal identity is removed or revoked")
            expiry = _stored_utc(certificate.not_after)
            if now < expiry or now > expiry + timedelta(days=30):
                raise ExpiredRenewalGraceExhausted(
                    "expired renewal grace exhausted; re-enrollment required"
                )
            if abs(int(now.timestamp()) - proof.signed_at) > 300:
                raise EnrollmentDenied("expired renewal proof is stale")
            public_key = x509.load_pem_x509_certificate(
                certificate.certificate_pem.encode("ascii")
            ).public_key()
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
        """Observe exact durable intent within a bounded exponential backoff."""
        for delay in (0.0, 0.05, 0.1, 0.2):
            if delay:
                time.sleep(delay)
            try:
                return self._submit_once(token, csr, evidence)
            except (
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

    def renew(
        self, node_id: str, serial: str, csr: bytes, *, expired: bool = False
    ) -> IssuedCertificate | UnknownError:
        """Observe exact durable intent within a bounded exponential backoff."""
        for delay in (0.0, 0.05, 0.1, 0.2):
            if delay:
                time.sleep(delay)
            try:
                return self._renew_once(node_id, serial, csr, expired=expired)
            except (
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

    def recover_rotation(
        self, node_id: str, serial: str, csr: bytes, *, expired: bool = False
    ) -> IssuedCertificate | UnknownError:
        """Observe exact durable intent within a bounded exponential backoff."""
        for delay in (0.0, 0.05, 0.1, 0.2):
            if delay:
                time.sleep(delay)
            try:
                return self._recover_rotation_once(
                    node_id, serial, csr, expired=expired
                )
            except (
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

    def revoke_node(self, node_id: str, actor: str) -> None | UnknownError:
        """Observe exact durable intent within a bounded exponential backoff."""
        for delay in (0.0, 0.05, 0.1, 0.2):
            if delay:
                time.sleep(delay)
            try:
                return self._revoke_node_once(node_id, actor)
            except (
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
