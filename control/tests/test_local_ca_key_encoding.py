"""Old NAS keys remain usable without modifying signing secrets on upgrade."""

import base64
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, padding, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from sqlalchemy.orm import sessionmaker
from vonk_control.local_ca import LocalCertificateAuthority

from .test_local_ca import _binding
from .test_step_ca import NOW, _csr, _der, _write_material


def _old_installer_key(
    key: ed25519.Ed25519PrivateKey, *, embedded_public: bytes | None = None
) -> bytes:
    """ed25519-dalek's v2 DER encrypted with the old installer's PBES2 params."""
    public = (
        key.public_key().public_bytes_raw()
        if embedded_public is None
        else embedded_public
    )
    plaintext = _der(
        0x30,
        _der(0x02, b"\x01")
        + _der(0x30, _der(0x06, bytes.fromhex("2b6570")))
        + _der(0x04, _der(0x04, key.private_bytes_raw()))
        + _der(0x81, b"\x00" + public),
    )
    salt, iv = bytes(range(16)), bytes(range(16, 32))
    wrapping_key = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=600_000
    ).derive(b"test-ca-password")
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(wrapping_key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    kdf = _der(
        0x30,
        _der(0x06, bytes.fromhex("2a864886f70d01050c"))
        + _der(
            0x30,
            _der(0x04, salt)
            + _der(0x02, bytes.fromhex("0927c0"))
            + _der(0x02, b"\x20")
            + _der(
                0x30, _der(0x06, bytes.fromhex("2a864886f70d0209")) + _der(0x05, b"")
            ),
        ),
    )
    cipher = _der(
        0x30, _der(0x06, bytes.fromhex("60864801650304012a")) + _der(0x04, iv)
    )
    encrypted = _der(
        0x30,
        _der(
            0x30,
            _der(0x06, bytes.fromhex("2a864886f70d01050d")) + _der(0x30, kdf + cipher),
        )
        + _der(0x04, ciphertext),
    )
    encoded = base64.b64encode(encrypted)
    return (
        b"-----BEGIN ENCRYPTED PRIVATE KEY-----\n"
        + b"\n".join(encoded[i : i + 64] for i in range(0, len(encoded), 64))
        + b"\n-----END ENCRYPTED PRIVATE KEY-----\n"
    )


@pytest.mark.parametrize(
    "fault", [None, "certificate-mismatch", "embedded-mismatch", "password"]
)
def test_old_installer_key_loads_and_signs_without_rewriting_or_accepting_mismatch(
    tmp_path: Path, fault: str | None
) -> None:
    """Catches rejecting installed v2 keys or using a key with the wrong identity."""
    material = _write_material(tmp_path)
    key = material["intermediate_key"]
    if fault == "certificate-mismatch":
        key = ed25519.Ed25519PrivateKey.generate()
    pem = _old_installer_key(
        key, embedded_public=bytes(32) if fault == "embedded-mismatch" else None
    )
    key_path, password_path = tmp_path / "intermediate-key", tmp_path / "password"
    key_path.write_bytes(pem)
    password_path.write_bytes(
        b"wrong-password" if fault == "password" else b"test-ca-password\n"
    )
    # Prove this actually exercises the format OpenSSL rejects, not canonical v1.
    with pytest.raises(ValueError):
        serialization.load_pem_private_key(pem, b"test-ca-password")
    options = {
        "sessions": sessionmaker(),
        "root_certificate_path": material["root_path"],
        "intermediate_certificate_path": material["intermediate_path"],
        "intermediate_key_path": key_path,
        "password_path": password_path,
        "provisioner_name": "vonk-forge-agent",
        "provisioner_kid": material["kid"],
    }
    # The boundary here is secret loading and real certificate signing; the
    # journal is exercised separately by test_local_ca on real PostgreSQL.
    with patch.object(LocalCertificateAuthority, "_import_revocations"):
        if fault:
            match = (
                "does not match certificate"
                if fault == "certificate-mismatch"
                else None
            )
            with pytest.raises(ValueError, match=match):
                LocalCertificateAuthority(**options)
        else:
            ca = LocalCertificateAuthority(**options)
            csr = _csr()
            request = _binding(ca, csr)
            leaf = ca._certificate(request, x509.load_pem_x509_csr(csr))
            leaf.verify_directly_issued_by(material["intermediate"])
            assert str(leaf.serial_number) == request.serial
            assert leaf.not_valid_before_utc == NOW
    assert key_path.read_bytes() == pem
