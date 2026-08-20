"""Certificate issuance boundary for outbound GPU node agents."""

from __future__ import annotations

import os
import re
import stat
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID

_PROVIDER_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{43}\Z")


@dataclass(frozen=True)
class IssuedCertificate:
    """Public certificate material returned to a node after issuance."""

    node_id: str
    certificate_pem: bytes
    chain_pem: bytes
    serial: str
    fingerprint: str
    not_before: datetime
    not_after: datetime
    generation: int = 1


class CertificateAuthority(ABC):
    """Stable CA provider boundary; Smallstep can implement this interface."""

    @abstractmethod
    def issue_node(self, node_id: str, csr_pem: bytes, now: datetime) -> IssuedCertificate:
        """Issue a client certificate that represents exactly one node."""

    @abstractmethod
    def renew_node(
        self,
        node_id: str,
        csr_pem: bytes,
        now: datetime,
        *,
        request_id: str,
    ) -> IssuedCertificate:
        """Rotate a node certificate after its authenticated renewal request."""

    @abstractmethod
    def revocation_bundle(self, now: datetime) -> bytes:
        """Return the intermediate's current signed CRL."""

    @abstractmethod
    def revoke_node(self, serial: str, now: datetime) -> None:
        """Revoke one decimal certificate serial; repeated calls are safe in effect."""


def _read_regular_secret_file(path_value: Path | str) -> bytes:
    path = os.fspath(path_value)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError("intermediate material must be a regular non-symlink file") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("intermediate material must be a regular non-symlink file")
        chunks: list[bytes] = []
        while block := os.read(descriptor, 65536):
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _load_node_csr(node_id: str, csr_pem: bytes) -> x509.CertificateSigningRequest:
    try:
        request = x509.load_pem_x509_csr(csr_pem)
    except (TypeError, ValueError) as error:
        raise ValueError("node CSR must be valid PEM") from error
    if not request.is_signature_valid:
        raise ValueError("node CSR signature is invalid")
    expected_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id)])
    if request.subject != expected_subject:
        raise ValueError("node CSR subject does not match node identity")
    if len(request.extensions) != 1:
        raise ValueError("node CSR must contain only the node URI SAN extension")
    try:
        sans = request.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound as error:
        raise ValueError("node CSR must contain the node URI SAN") from error
    expected_sans = x509.SubjectAlternativeName(
        [x509.UniformResourceIdentifier(f"spiffe://vonk-forge.local/node/{node_id}")]
    )
    if sans != expected_sans:
        raise ValueError("node CSR URI SAN does not match node identity")
    public_key = request.public_key()
    if not isinstance(public_key, ed25519.Ed25519PublicKey):
        raise ValueError("node CSR public key must be Ed25519")  # noqa: TRY004
    return request


def _validate_provider_request_id(request_id: str) -> None:
    if _PROVIDER_REQUEST_ID.fullmatch(request_id) is None:
        raise ValueError("provider request ID must be a 43-character base64url value")


def _utc_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC).replace(microsecond=0)
