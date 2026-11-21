"""Shared PKI test material for the Controller CA tests."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

NODE_ID = "spk_0123456789abcdef0123456789abcdef"
NOW = datetime(2026, 8, 4, 12, tzinfo=UTC)


def _der(tag: int, payload: bytes) -> bytes:
    """Encode one definite-length DER element."""
    if len(payload) < 0x80:
        length = bytes([len(payload)])
    else:
        encoded = len(payload).to_bytes((len(payload).bit_length() + 7) // 8, "big")
        length = bytes([0x80 | len(encoded)]) + encoded
    return bytes([tag]) + length + payload


class _Material(TypedDict):
    root: x509.Certificate
    root_path: Path
    intermediate: x509.Certificate
    intermediate_key: ed25519.Ed25519PrivateKey
    intermediate_path: Path
    credential_path: Path
    public_jwk_path: Path
    public_jwk: dict[str, str]
    kid: str


def _b64(value: int) -> str:
    return base64.urlsafe_b64encode(value.to_bytes(32, "big")).rstrip(b"=").decode()


def _write_material(tmp_path: Path) -> _Material:
    tmp_path.mkdir(parents=True, exist_ok=True)
    root_key = ed25519.Ed25519PrivateKey.generate()
    root_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Vonk Forge Offline Root")]
    )
    root = (
        x509.CertificateBuilder()
        .subject_name(root_name)
        .issuer_name(root_name)
        .public_key(root_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            critical=True,
        )
        .sign(root_key, algorithm=None)
    )
    intermediate_key = ed25519.Ed25519PrivateKey.generate()
    intermediate_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Vonk Forge Agent Intermediate")]
    )
    intermediate = (
        x509.CertificateBuilder()
        .subject_name(intermediate_name)
        .issuer_name(root.subject)
        .public_key(intermediate_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            critical=True,
        )
        .sign(root_key, algorithm=None)
    )
    provisioner_key = ec.generate_private_key(ec.SECP256R1())
    root_path = tmp_path / "root.pem"
    intermediate_path = tmp_path / "intermediate.pem"
    credential_path = tmp_path / "provisioner.pem"
    public_jwk_path = tmp_path / "provisioner-public.jwk"
    root_path.write_bytes(root.public_bytes(serialization.Encoding.PEM))
    intermediate_path.write_bytes(intermediate.public_bytes(serialization.Encoding.PEM))
    numbers = provisioner_key.public_key().public_numbers()
    public_jwk = {
        "kty": "EC",
        "crv": "P-256",
        "use": "sig",
        "alg": "ES256",
        "x": _b64(numbers.x),
        "y": _b64(numbers.y),
    }
    thumbprint_input = json.dumps(
        {name: public_jwk[name] for name in ("crv", "kty", "x", "y")},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    kid = (
        base64.urlsafe_b64encode(hashlib.sha256(thumbprint_input).digest())
        .rstrip(b"=")
        .decode()
    )
    public_jwk["kid"] = kid
    private_jwk = public_jwk | {
        "d": _b64(provisioner_key.private_numbers().private_value)
    }
    credential_path.write_text(json.dumps(private_jwk))
    public_jwk_path.write_text(json.dumps(public_jwk))
    credential_path.chmod(0o600)
    return {
        "root": root,
        "root_path": root_path,
        "intermediate": intermediate,
        "intermediate_key": intermediate_key,
        "intermediate_path": intermediate_path,
        "credential_path": credential_path,
        "public_jwk_path": public_jwk_path,
        "public_jwk": public_jwk,
        "kid": kid,
    }


def _csr(node_id: str = NODE_ID) -> bytes:
    key = ed25519.Ed25519PrivateKey.generate()
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id)]))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(
                        f"spiffe://vonk-forge.local/node/{node_id}"
                    )
                ]
            ),
            critical=False,
        )
        .sign(key, algorithm=None)
        .public_bytes(serialization.Encoding.PEM)
    )


def _leaf(
    csr_pem: bytes,
    material: _Material,
    *,
    now: datetime = NOW,
    serial: int = 1234,
    lifetime_seconds: int = 2592000,
) -> x509.Certificate:
    request = x509.load_pem_x509_csr(csr_pem)
    return (
        x509.CertificateBuilder()
        .subject_name(request.subject)
        .issuer_name(material["intermediate"].subject)
        .public_key(request.public_key())
        .serial_number(serial)
        .not_valid_before(now)
        .not_valid_after(now + timedelta(seconds=lifetime_seconds))
        .add_extension(
            x509.KeyUsage(True, False, False, False, False, False, False, False, False),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
        )
        .add_extension(
            request.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value,
            critical=False,
        )
        .sign(material["intermediate_key"], algorithm=None)
    )
