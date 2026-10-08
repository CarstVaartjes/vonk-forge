"""Certificate requests and enrollment input validation."""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID
from pydantic import ValidationError
from vonk_agent_protocol.enrollment import (
    EnrollmentEvidence,
)


def _load_csr(node_id: str | None, csr: bytes) -> tuple[bytes, bytes, str, str]:
    from .enrollment import _NODE_ID, EnrollmentDenied

    try:
        request = x509.load_pem_x509_csr(csr)
    except (TypeError, ValueError) as error:
        raise EnrollmentDenied("CSR must be valid PEM") from error
    if not request.is_signature_valid:
        raise EnrollmentDenied("CSR signature is invalid")
    common_names = request.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if len(common_names) != 1:
        raise EnrollmentDenied("CSR subject must contain a canonical node ID")
    common_name = common_names[0].value
    if not isinstance(common_name, str) or _NODE_ID.fullmatch(common_name) is None:
        raise EnrollmentDenied("CSR subject must contain a canonical node ID")
    csr_node_id = common_name
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
    try:
        values = EnrollmentEvidence.model_validate(evidence).model_dump()
    except ValidationError:
        return {}, "evidence fields are invalid"
    if values["node_id"] != csr_node_id:
        return values, "evidence node ID does not match CSR"
    if grant_node_id is not None and values["node_id"] != grant_node_id:
        return values, "evidence node ID does not match enrollment grant"
    if values["csr_public_key_fingerprint"] != public_key_fingerprint:
        return values, "evidence CSR public-key fingerprint does not match CSR"
    return values, None


def _decode_token(token: str) -> bytes:
    from .enrollment import _TOKEN, EnrollmentDenied

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
    from .enrollment import _NODE_ID

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
