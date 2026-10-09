"""Enrollment authority checks, independent of issuance bookkeeping.

These predicates guard bearer authority, enrolled-key possession, and incoming
CA certificate bytes. Missing journal evidence is handled by the enrollment
recovery owner, never by these checks.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy.orm import Session
from vonk_agent_protocol.enrollment import ExpiredRenewRequest
from vonk_agent_protocol.state_machines import (
    CertificateRecordState,
    EnrollmentPurpose,
    NodeIdentityState,
)

from ..ca_issuance_contract import CertificateIssuanceBinding
from ..enrollment.types import (
    EnrollmentDenied,
    EnrollmentIssuanceUncertain,
    ExpiredRenewalGraceExhausted,
)
from ..enrollment_validation import _stored_utc, _utc
from ..models import AgentCertificate, AgentEnrollmentGrant, AgentNode
from ..pki import IssuedCertificate
from ..step_ca import StepCAError, StepCAUnavailable


def require_grant(grant: AgentEnrollmentGrant | None) -> AgentEnrollmentGrant:
    if grant is None or grant.revoked_at is not None:
        raise EnrollmentDenied("enrollment grant is revoked or missing")
    return grant


def require_node(node: AgentNode | None) -> AgentNode:
    if node is None:
        raise EnrollmentDenied("certificate serial does not identify node")
    if node.state != NodeIdentityState.ACTIVE or node.revoked_at is not None:
        raise EnrollmentDenied("node identity is retired or revoked")
    return node


def require_certificate(certificate: AgentCertificate | None) -> AgentCertificate:
    if certificate is None:
        raise EnrollmentDenied("certificate serial does not identify node")
    return certificate


def require_fresh_identity(purpose: EnrollmentPurpose, node: AgentNode | None) -> None:
    if purpose == EnrollmentPurpose.NEW_NODE and node is not None:
        raise EnrollmentDenied("node identity already exists")


def require_fresh_enrollment(
    session: Session, purpose: EnrollmentPurpose, node_id: str
) -> None:
    require_fresh_identity(purpose, session.get(AgentNode, node_id))


def require_admitted_request(failure: str | None) -> None:
    """The bearer-grant admission decision; never called for a journal miss."""
    if failure is not None:
        raise EnrollmentDenied(failure)


def require_optional_node(node: AgentNode | None) -> None:
    if node is not None:
        require_node(node)


def source_valid(
    certificate: AgentCertificate, now: datetime, *, expired: bool
) -> bool:
    expiry = _stored_utc(certificate.not_after)
    return expiry <= now <= expiry + timedelta(days=30) if expired else now < expiry


def require_rotation_source(
    node: AgentNode | None,
    source: AgentCertificate | None,
    node_id: str,
    now: datetime,
    *,
    expired: bool,
) -> None:
    require_node(node)
    certificate = require_certificate(source)
    if certificate.node_id != node_id or certificate.revoked_at is not None:
        raise EnrollmentDenied("rotation source authority is invalid")
    if certificate.state != CertificateRecordState.ACTIVE:
        raise EnrollmentDenied("certificate is not active")
    if _stored_utc(certificate.not_before) > now or not source_valid(
        certificate, now, expired=expired
    ):
        raise EnrollmentDenied("certificate is not currently valid")


def require_activation_identity(
    node: AgentNode | None, certificate: AgentCertificate | None, generation: int
) -> None:
    require_node(node)
    certificate = require_certificate(certificate)
    if certificate.generation != generation:
        raise EnrollmentDenied("certificate generation does not match")


def require_staged_certificate(certificate: AgentCertificate, now: datetime) -> None:
    if (
        certificate.state != CertificateRecordState.STAGED
        or certificate.revoked_at is not None
        or _stored_utc(certificate.not_before) > now
        or _stored_utc(certificate.not_after) <= now
    ):
        raise EnrollmentDenied("certificate is not staged for activation")


def require_expired_source(node: AgentNode | None) -> None:
    if (
        node is None
        or node.state != NodeIdentityState.ACTIVE
        or node.revoked_at is not None
    ):
        raise EnrollmentDenied("expired renewal identity is removed or revoked")


def require_expired_proof(
    certificate: AgentCertificate, proof: ExpiredRenewRequest, now: datetime
) -> None:
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
        public_key = (
            x509.load_pem_x509_certificate(
                certificate.certificate_pem.encode("ascii")
            ).public_key()
            if certificate.certificate_pem is not None
            else None
        )
    except (ValueError, UnicodeError) as error:
        raise EnrollmentIssuanceUncertain(
            "expired certificate projection is unavailable"
        ) from error
    if public_key is None:
        raise EnrollmentIssuanceUncertain(
            "expired certificate projection is unavailable"
        )
    if not isinstance(public_key, ed25519.Ed25519PublicKey):
        raise EnrollmentDenied("expired renewal key is not Ed25519")
    try:
        public_key.verify(bytes.fromhex(proof.signature), proof.proof_bytes())
    except (InvalidSignature, ValueError, UnicodeEncodeError):
        raise EnrollmentDenied("expired renewal proof is invalid") from None


def require_provider_verification(error: BaseException) -> None:
    if isinstance(error, StepCAError) and not isinstance(error, StepCAUnavailable):
        raise EnrollmentDenied("certificate authority verification failed") from error


def certificate_text(issued: IssuedCertificate) -> tuple[str, str]:
    try:
        return issued.certificate_pem.decode("ascii"), issued.chain_pem.decode("ascii")
    except UnicodeDecodeError as error:
        raise EnrollmentDenied(
            "certificate authority returned non-PEM certificate material"
        ) from error


def validate_issued_binding(
    issued: IssuedCertificate, binding: CertificateIssuanceBinding
) -> None:
    if (
        issued.node_id != binding.node_id
        or issued.serial != binding.serial
        or _utc(issued.not_before) != datetime.fromisoformat(binding.not_before)
        or _utc(issued.not_after) != datetime.fromisoformat(binding.not_after)
        or issued.generation != binding.generation
    ):
        raise EnrollmentDenied(
            "certificate authority result differs from accepted issuance binding"
        )


def validate_renewal_result(
    issued: IssuedCertificate, node_id: str, source_serial: str
) -> None:
    if issued.node_id != node_id:
        raise EnrollmentDenied(
            "certificate authority returned a mismatched node identity"
        )
    if issued.serial == source_serial:
        raise EnrollmentDenied("certificate authority reused renewal serial")
    certificate_text(issued)
