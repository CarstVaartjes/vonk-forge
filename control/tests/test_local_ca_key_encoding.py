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


@pytest.mark.parametrize("encoding", ["canonical", "old", "mismatch", "password"])
def test_key_encoding_signs_or_reports_typed_configuration_failure(
    tmp_path: Path, encoding: str
) -> None:
    """Catches accepting mismatches, decoding v2 in the Controller, or startup failure."""
    from datetime import timedelta

    from fastapi import HTTPException
    from vonk_control.capabilities import (
        CapabilityConfigurationError,
        RecoveringService,
    )
    from vonk_control.capability_contract import (
        CapabilityAvailability,
        CapabilityReason,
        ControllerCapability,
    )

    material = _write_material(tmp_path)
    key = material["intermediate_key"]
    if encoding == "mismatch":
        key = ed25519.Ed25519PrivateKey.generate()
    canonical = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"test-ca-password"),
    )
    pem = _old_installer_key(key) if encoding == "old" else canonical
    key_path, password_path = tmp_path / "intermediate-key", tmp_path / "password"
    key_path.write_bytes(pem)
    password_path.write_bytes(
        b"wrong-password" if encoding == "password" else b"test-ca-password\n"
    )
    options = {
        "sessions": sessionmaker(),
        "root_certificate_path": material["root_path"],
        "intermediate_certificate_path": material["intermediate_path"],
        "intermediate_key_path": key_path,
        "password_path": password_path,
        "provisioner_name": "vonk-forge-agent",
        "provisioner_kid": material["kid"],
    }
    # Real loading/signing; PostgreSQL journal behavior lives in test_local_ca.
    with patch.object(LocalCertificateAuthority, "_import_revocations"):
        if encoding in {"old", "password"}:
            with pytest.raises(
                CapabilityConfigurationError, match="rerun the NAS installer"
            ):
                LocalCertificateAuthority(**options)
            clock = NOW
            service = RecoveringService(
                ControllerCapability.CERTIFICATE_AUTHORITY,
                LocalCertificateAuthority,
                lambda: LocalCertificateAuthority(**options),
                lambda: clock,
            )
            with pytest.raises(HTTPException) as failure:
                service.require_service()
            assert failure.value.status_code == 503
            assert service.status.availability == CapabilityAvailability.UNAVAILABLE
            assert service.status.reason == CapabilityReason.CA_KEY_ENCODING_UNSUPPORTED
            assert key_path.read_bytes() == pem
            # Simulate the installer fixing the secret. The normal capability
            # retry must recover without restarting or replacing Controller state.
            key_path.write_bytes(canonical)
            password_path.write_bytes(b"test-ca-password\n")
            clock += timedelta(seconds=61)
            ca = service.require_service()
            assert service.status.availability == CapabilityAvailability.AVAILABLE
        elif encoding == "mismatch":
            with pytest.raises(ValueError, match="does not match certificate"):
                LocalCertificateAuthority(**options)
            assert key_path.read_bytes() == pem
            return
        else:
            ca = LocalCertificateAuthority(**options)
        csr = _csr()
        request = _binding(ca, csr)
        leaf = ca._certificate(request, x509.load_pem_x509_csr(csr))
        leaf.verify_directly_issued_by(material["intermediate"])
        assert str(leaf.serial_number) == request.serial
        assert leaf.not_valid_before_utc == NOW
    assert key_path.read_bytes() == canonical
