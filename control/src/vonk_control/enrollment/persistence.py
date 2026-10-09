"""Durable token-authorized enrollment for immutable GPU node identities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from pydantic import JsonValue, ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from vonk_agent_protocol.enrollment import MAX_CSR_BYTES
from vonk_agent_protocol.state_machines import (
    CertificateRecordState,
    CertificateRotationState,
    EnrollmentProfileState,
    EnrollmentPurpose,
    EnrollmentRecordState,
    NodeIdentityState,
)

from ..ca_issuance_contract import CertificateIssuanceBinding
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
    AgentNode,
    AgentNodeProfile,
)
from ..pki import IssuedCertificate
from ..security.enrollment import (
    certificate_text,
    require_fresh_identity,
    require_grant,
    require_node,
)
from .types import EnrollmentDenied, EnrollmentIssuanceUncertain, _RotationClaim


def _persist_issued_enrollment(
    session: Session,
    enrollment: AgentEnrollment,
    issued: IssuedCertificate,
    *,
    purpose: EnrollmentPurpose,
    now: datetime,
) -> None:
    certificate_pem, chain_pem = certificate_text(issued)
    grant = session.get(AgentEnrollmentGrant, enrollment.grant_id)
    grant = require_grant(grant)
    node = session.scalar(
        select(AgentNode)
        .where(AgentNode.node_id == enrollment.node_id)
        .with_for_update(of=AgentNode)
    )
    if purpose not in {EnrollmentPurpose.NEW_NODE, EnrollmentPurpose.RE_ENROLL}:
        raise EnrollmentIssuanceUncertain(
            "enrollment purpose projection is unavailable"
        )
    require_fresh_identity(purpose, node)
    certificates: list[AgentCertificate] = []
    if node is None:
        node = AgentNode(
            node_id=enrollment.node_id,
            state=NodeIdentityState.ACTIVE,
        )
        session.add(node)
        # There is no ORM relationship between these operational rows. Flush
        # the FK parent explicitly for PostgreSQL.
        session.flush()
        session.add(
            AgentNodeProfile(
                node_id=enrollment.node_id,
                display_name=grant.requested_display_name or enrollment.node_id,
                hostname="",
                lifecycle=EnrollmentProfileState.READY,
                labels={},
            )
        )
        generation = 1
    else:
        if purpose != EnrollmentPurpose.RE_ENROLL:
            raise EnrollmentIssuanceUncertain(
                "enrollment purpose projection is unavailable"
            )
        require_node(node)
        certificates = list(
            session.scalars(
                select(AgentCertificate)
                .where(AgentCertificate.node_id == enrollment.node_id)
                .order_by(AgentCertificate.generation, AgentCertificate.serial)
                .with_for_update(of=AgentCertificate)
            )
        )
        rotation = session.scalar(
            select(AgentCertificateRotation)
            .where(AgentCertificateRotation.node_id == enrollment.node_id)
            .with_for_update(of=AgentCertificateRotation)
        )
        if rotation is not None:
            from .ending import retain_ended_effect

            retain_ended_effect(
                session,
                _issuance_binding(rotation.provider_request),
                rotation.csr_pem,
                now,
            )
            session.delete(rotation)
    # Generation is part of the accepted exact CA effect, not a recomputed
    # projection policy. Missing history cannot invalidate verified content.
    generation = issued.generation
    for certificate in certificates:
        if certificate.state in {
            CertificateRecordState.ACTIVE,
            CertificateRecordState.STAGED,
        }:
            certificate.state = CertificateRecordState.REVOKED
            certificate.revoked_at = certificate.revoked_at or now
    session.add(
        AgentCertificate(
            serial=issued.serial,
            node_id=enrollment.node_id,
            not_before=issued.not_before,
            not_after=issued.not_after,
            fingerprint=issued.fingerprint,
            state=CertificateRecordState.ACTIVE,
            generation=generation,
            certificate_pem=certificate_pem,
            chain_pem=chain_pem,
            csr_public_key_fingerprint=enrollment.csr_public_key_fingerprint,
            provider_request=enrollment.provider_request,
            csr_pem=enrollment.csr_pem,
        )
    )
    enrollment.state = EnrollmentRecordState.CERTIFICATE_ISSUED
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
        raise EnrollmentIssuanceUncertain("enrollment projection is absent")
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
    # Stored public material is a projection, never proof of a new effect.
    # Parse damage takes the caller's exact journal repair path.
    if enrollment.certificate_pem is None or enrollment.chain_pem is None:
        raise RuntimeError("issued enrollment public material is unavailable")
    leaf = x509.load_pem_x509_certificate(enrollment.certificate_pem.encode("ascii"))
    x509.load_pem_x509_certificate(enrollment.chain_pem.encode("ascii"))
    if (
        leaf.fingerprint(hashes.SHA256()).hex() != enrollment.certificate_fingerprint
        or str(leaf.serial_number) != enrollment.certificate_serial
    ):
        raise RuntimeError("issued certificate projection is inconsistent")
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
    evidence: Mapping[str, str],
) -> bool:
    # Apply the fresh-submit CSR bound before parsing; an oversized or
    # non-bytes replay cannot match the original request.
    if not isinstance(csr, bytes) or len(csr) > MAX_CSR_BYTES:
        return False
    try:
        normalized, _, fingerprint, _ = _load_csr(enrollment.node_id, csr)
    except EnrollmentDenied:
        return False
    _, failure = _validate_evidence(
        evidence,
        enrollment.node_id,
        enrollment.node_id,
        fingerprint,
    )
    binding = _issuance_binding(enrollment.provider_request)
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    content_matches = (
        hashlib.sha256(
            x509.load_pem_x509_csr(normalized).public_bytes(serialization.Encoding.DER)
        ).hexdigest()
        == binding.csr_sha256
        if binding is not None
        else normalized.decode("ascii") == enrollment.csr_pem
    )
    return (
        failure is None
        and content_matches
        and fingerprint == enrollment.csr_public_key_fingerprint
    )


def _certificate_issued(certificate: AgentCertificate) -> IssuedCertificate:
    if certificate.certificate_pem is None or certificate.chain_pem is None:
        raise RuntimeError("staged certificate is missing public material")
    leaf = x509.load_pem_x509_certificate(certificate.certificate_pem.encode("ascii"))
    x509.load_pem_x509_certificate(certificate.chain_pem.encode("ascii"))
    if (
        leaf.fingerprint(hashes.SHA256()).hex() != certificate.fingerprint
        or str(leaf.serial_number) != certificate.serial
    ):
        raise RuntimeError("staged certificate projection is inconsistent")
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
    binding = _issuance_binding(rotation.provider_request)
    try:
        csr_pem = rotation.csr_pem.encode("ascii")
    except UnicodeError:
        csr_pem = b""
        binding = None
    return _RotationClaim(
        node_id=rotation.node_id,
        source_serial=rotation.source_serial,
        generation=rotation.generation,
        csr_pem=csr_pem,
        csr_public_key_fingerprint=rotation.csr_public_key_fingerprint,
        provider_request_id=rotation.provider_request_id,
        provider_request=binding,
        state=(
            CertificateRotationState.REVOCATION_PENDING
            if rotation.state == CertificateRotationState.REVOCATION_PENDING
            else CertificateRotationState.ISSUING
        ),
        owner=owner,
    )


def _issuance_binding(
    value: JsonValue | None,
) -> CertificateIssuanceBinding | None:
    if value is None:
        return None
    try:
        return CertificateIssuanceBinding.model_validate_json(
            json.dumps(value, allow_nan=False)
        )
    except (ValidationError, ValueError, TypeError):
        # Stored bookkeeping is a miss. A request must establish a fresh exact
        # binding; no damaged record may authorize certificate adoption.
        return None


def _require_issuance_binding(
    stored: JsonValue | None, expected: CertificateIssuanceBinding
) -> None:
    """The same exact accepted binding fences admission and result adoption."""
    if _issuance_binding(stored) != expected:
        raise EnrollmentIssuanceUncertain("enrollment issuance binding changed")
