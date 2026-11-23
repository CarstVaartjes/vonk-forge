"""Shared node certificate authority base, errors and helpers for the local CA."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed448, ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, ExtensionOID, NameOID
from vonk_agent_protocol.disposition import Disposition
from vonk_agent_protocol.lifecycle_vocabulary import WaitReason
from vonk_agent_protocol.outcome import UnknownOutcomeError
from vonk_agent_protocol.reason_codes.admission import CertificateCode

from .ca_issuance_contract import (
    CertificateIssuanceBinding,
    CertificateRefusalReason,
)
from .pki import (
    CertificateAuthority,
    _load_node_csr,
    _utc_timestamp,
)

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}\Z")
_KID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_DEFAULT_CERTIFICATE_LIFETIME_SECONDS = 30 * 24 * 60 * 60
#: The longest any node certificate this Controller issues stays valid.
CERTIFICATE_LIFETIME = timedelta(seconds=_DEFAULT_CERTIFICATE_LIFETIME_SECONDS)
_MAX_CRL_WINDOW = timedelta(hours=1)


class StepCAError(RuntimeError):
    """A bounded provider operation failed without exposing authorization."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: CertificateRefusalReason = CertificateCode.ISSUANCE_UNAVAILABLE,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class StepCAUnavailable(UnknownOutcomeError, StepCAError):
    """Transport or provider health is unknown, never authentication denial."""

    def __init__(self, message: str) -> None:
        super().__init__(message, reason=WaitReason.OBSERVATION_UNAVAILABLE)


class StepCAIssuancePending(StepCAUnavailable):
    """The exact CA journal request is still owned by an issuer epoch."""

    #: A live owner or dependency holds the work: a wait, never a spent retry.
    disposition = Disposition.WAIT


class NodeCertificateAuthority(CertificateAuthority):
    """Shared exact binding and node certificate policy for CA providers."""

    _intermediate: x509.Certificate
    _certificate_lifetime_seconds: int
    _certificate_lifetime: timedelta
    _provisioner_name: str
    _provisioner_kid: str

    def prepare_request(
        self,
        node_id: str,
        csr_pem: bytes,
        now: datetime,
        *,
        purpose: str,
        source_serial: str | None,
        generation: int,
    ) -> CertificateIssuanceBinding:
        timestamp = _utc_timestamp(now)
        csr = _load_node_csr(node_id, csr_pem)
        issuer = self._intermediate.fingerprint(hashes.SHA256()).hex()
        policy = json.dumps(
            {
                "certificate_lifetime_seconds": self._certificate_lifetime_seconds,
                "issuer_fingerprint": issuer,
                "profile": "vonk-node-client-v1",
                "provisioner_kid": self._provisioner_kid,
                "provisioner_name": self._provisioner_name,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        return CertificateIssuanceBinding.model_validate(
            {
                "request_id": secrets.token_urlsafe(32),
                "node_id": node_id,
                "csr_sha256": hashlib.sha256(
                    csr.public_bytes(serialization.Encoding.DER)
                ).hexdigest(),
                "serial": str(x509.random_serial_number()),
                "not_before": _rfc3339(timestamp),
                "not_after": _rfc3339(timestamp + self._certificate_lifetime),
                "issuer_fingerprint": issuer,
                "provisioner_name": self._provisioner_name,
                "provisioner_kid": self._provisioner_kid,
                "policy_sha256": hashlib.sha256(policy).hexdigest(),
                "purpose": purpose,
                "source_serial": source_serial,
                "generation": generation,
            }
        )

    def _validate_leaf(
        self,
        node_id: str,
        request: x509.CertificateSigningRequest,
        leaf: x509.Certificate,
    ) -> None:
        if leaf.subject != x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, node_id)]
        ):
            raise StepCAError("step-ca returned a mismatched certificate subject")
        if leaf.issuer != self._intermediate.subject:
            raise StepCAError("step-ca returned a mismatched certificate issuer")
        try:
            _signature_verifying_key(self._intermediate).verify(
                leaf.signature, leaf.tbs_certificate_bytes
            )
        except Exception as error:
            raise StepCAError(
                "step-ca returned a certificate with an invalid signature"
            ) from error
        request_key = request.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        if not isinstance(leaf.public_key(), ed25519.Ed25519PublicKey):
            raise StepCAError("step-ca returned a certificate with the wrong key type")
        leaf_key = leaf.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        if leaf_key != request_key:
            raise StepCAError("step-ca returned a certificate for another public key")
        required_extensions = {
            ExtensionOID.KEY_USAGE,
            ExtensionOID.EXTENDED_KEY_USAGE,
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME,
        }
        allowed_extensions = required_extensions | {
            ExtensionOID.BASIC_CONSTRAINTS,
            ExtensionOID.SUBJECT_KEY_IDENTIFIER,
            ExtensionOID.AUTHORITY_KEY_IDENTIFIER,
        }
        extension_oids = {value.oid for value in leaf.extensions}
        if (
            not required_extensions <= extension_oids
            or not extension_oids <= allowed_extensions
        ):
            raise StepCAError(
                "step-ca returned an unexpected certificate extension profile"
            )
        criticality = {value.oid: value.critical for value in leaf.extensions}
        if (
            criticality[ExtensionOID.KEY_USAGE] is not True
            or criticality[ExtensionOID.EXTENDED_KEY_USAGE] is not False
            or criticality[ExtensionOID.SUBJECT_ALTERNATIVE_NAME] is not False
        ):
            raise StepCAError(
                "step-ca returned invalid certificate extension criticality"
            )
        if ExtensionOID.BASIC_CONSTRAINTS in extension_oids:
            basic_constraints = leaf.extensions.get_extension_for_class(
                x509.BasicConstraints
            )
            if (
                basic_constraints.critical is not True
                or basic_constraints.value != x509.BasicConstraints(False, None)
            ):
                raise StepCAError("step-ca returned a CA certificate")
        expected_usage = x509.KeyUsage(
            True, False, False, False, False, False, False, False, False
        )
        if (
            leaf.extensions.get_extension_for_class(x509.KeyUsage).value
            != expected_usage
        ):
            raise StepCAError("step-ca returned invalid key usage")
        expected_eku = x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH])
        if (
            leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
            != expected_eku
        ):
            raise StepCAError("step-ca returned invalid extended key usage")
        expected_san = x509.SubjectAlternativeName(
            [
                x509.UniformResourceIdentifier(
                    f"spiffe://vonk-forge.local/node/{node_id}"
                )
            ]
        )
        if (
            leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            != expected_san
        ):
            raise StepCAError("step-ca returned a mismatched node URI SAN")
        if ExtensionOID.AUTHORITY_KEY_IDENTIFIER in extension_oids:
            try:
                intermediate_skid = (
                    self._intermediate.extensions.get_extension_for_class(
                        x509.SubjectKeyIdentifier
                    ).value.digest
                )
            except x509.ExtensionNotFound as error:
                raise StepCAError(
                    "step-ca returned an unverifiable authority key identifier"
                ) from error
            authority_id = leaf.extensions.get_extension_for_class(
                x509.AuthorityKeyIdentifier
            ).value
            if authority_id.key_identifier != intermediate_skid:
                raise StepCAError(
                    "step-ca returned a mismatched authority key identifier"
                )
        if ExtensionOID.SUBJECT_KEY_IDENTIFIER in extension_oids:
            expected_skid = x509.SubjectKeyIdentifier.from_public_key(leaf.public_key())
            if (
                leaf.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
                != expected_skid
            ):
                raise StepCAError(
                    "step-ca returned a mismatched subject key identifier"
                )


def _signature_verifying_key(
    certificate: x509.Certificate,
) -> ed25519.Ed25519PublicKey | ed448.Ed448PublicKey:
    """Return the certificate key that verifies with only signature and data.

    step-ca signs with Ed25519 keys, whose ``verify`` takes exactly the
    signature and the signed bytes. A key that cannot verify that way is
    rejected here instead of reaching an unsupported call.
    """

    key = certificate.public_key()
    if isinstance(key, (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey)):
        return key
    raise ValueError("certificate public key cannot verify a signature")


def _one_certificate(
    pem: bytes, label: str, *, provider_error: bool = False
) -> x509.Certificate:
    try:
        certificate = x509.load_pem_x509_certificate(pem)
    except ValueError as error:
        exception = StepCAUnavailable if provider_error else ValueError
        raise exception(
            f"{label} certificate must be exactly one valid PEM certificate"
        ) from error
    normalized = certificate.public_bytes(serialization.Encoding.PEM)
    if pem.strip() != normalized.strip():
        exception = StepCAUnavailable if provider_error else ValueError
        raise exception(
            f"{label} certificate must be exactly one valid PEM certificate"
        )
    return certificate


def _verify_ca_chain(
    root: x509.Certificate,
    intermediate: x509.Certificate,
) -> None:
    if root.subject != root.issuer:
        raise ValueError("root certificate must be self-issued")
    try:
        _signature_verifying_key(root).verify(
            root.signature, root.tbs_certificate_bytes
        )
    except Exception as error:
        raise ValueError("root certificate self-signature is invalid") from error
    try:
        root_constraints = root.extensions.get_extension_for_class(
            x509.BasicConstraints
        ).value
        root_usage = root.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound as error:
        raise ValueError(
            "root certificate must contain CA constraints and key usage"
        ) from error
    if (
        not root_constraints.ca
        or root_constraints.path_length is not None
        and root_constraints.path_length < 1
    ):
        raise ValueError("root certificate cannot issue the configured intermediate")
    if not root_usage.key_cert_sign or not root_usage.crl_sign:
        raise ValueError("root certificate must permit certificate and CRL signing")
    if intermediate.issuer != root.subject:
        raise ValueError("intermediate certificate is not issued by configured root")
    try:
        _signature_verifying_key(root).verify(
            intermediate.signature, intermediate.tbs_certificate_bytes
        )
    except Exception as error:
        raise ValueError("intermediate certificate signature is invalid") from error
    try:
        constraints = intermediate.extensions.get_extension_for_class(
            x509.BasicConstraints
        ).value
        usage = intermediate.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound as error:
        raise ValueError(
            "intermediate certificate must contain CA constraints and key usage"
        ) from error
    if (
        constraints != x509.BasicConstraints(True, 0)
        or not usage.key_cert_sign
        or not usage.crl_sign
    ):
        raise ValueError(
            "intermediate certificate is not a path-length-zero signing CA"
        )
    now = datetime.now(UTC)
    if root.not_valid_before_utc > now or root.not_valid_after_utc <= now:
        raise ValueError("root certificate is not currently valid")
    if (
        intermediate.not_valid_before_utc > now
        or intermediate.not_valid_after_utc <= now
    ):
        raise ValueError("intermediate certificate is not currently valid")


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
