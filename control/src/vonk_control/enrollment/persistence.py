"""Durable token-authorized enrollment for immutable GPU node identities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta

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
from .types import EnrollmentDenied, EnrollmentIssuanceUncertain, _RotationClaim


def _persist_issued_enrollment(
    session: Session,
    enrollment: AgentEnrollment,
    issued: IssuedCertificate,
    *,
    purpose: EnrollmentPurpose,
    now: datetime,
) -> None:
    try:
        certificate_pem = issued.certificate_pem.decode("ascii")
        chain_pem = issued.chain_pem.decode("ascii")
    except UnicodeDecodeError as error:
        raise EnrollmentDenied(
            "certificate authority returned non-PEM certificate material"
        ) from error
    grant = session.get(AgentEnrollmentGrant, enrollment.grant_id)
    if grant is None or grant.revoked_at is not None:
        raise EnrollmentDenied("enrollment grant is revoked or missing")
    node = session.scalar(
        select(AgentNode)
        .where(AgentNode.node_id == enrollment.node_id)
        .with_for_update(of=AgentNode)
    )
    if purpose not in {EnrollmentPurpose.NEW_NODE, EnrollmentPurpose.RE_ENROLL}:
        raise EnrollmentDenied("enrollment purpose is invalid")
    if purpose == EnrollmentPurpose.NEW_NODE and node is not None:
        raise EnrollmentDenied("node identity already exists")
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
            raise EnrollmentDenied("enrollment purpose is invalid")
        if node.state != NodeIdentityState.ACTIVE or node.revoked_at is not None:
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
    if generation != issued.generation:
        raise EnrollmentDenied(
            "enrollment generation changed from accepted issuance binding"
        )
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
        provider_request=_issuance_binding(rotation.provider_request),
        state=CertificateRotationState(rotation.state),
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


def _validate_issued_binding(
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


def _require_issuance_binding(
    stored: JsonValue | None, expected: CertificateIssuanceBinding
) -> None:
    """The same exact accepted binding fences admission and result adoption."""
    if _issuance_binding(stored) != expected:
        raise EnrollmentIssuanceUncertain("enrollment issuance binding changed")


def _rotation_source_valid(
    certificate: AgentCertificate, now: datetime, *, expired: bool
) -> bool:
    expiry = _stored_utc(certificate.not_after)
    return expiry <= now <= expiry + timedelta(days=30) if expired else now < expiry
