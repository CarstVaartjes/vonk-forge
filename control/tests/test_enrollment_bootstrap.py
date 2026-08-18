from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from vonk_control.enrollment_bootstrap import EnrollmentBootstrapConfig


def _controller_ca() -> tuple[x509.Certificate, bytes]:
    key = ed25519.Ed25519PrivateKey.generate()
    subject = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "controller-ca")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime(2026, 8, 19, tzinfo=UTC) - timedelta(days=1))
        .not_valid_after(datetime(2026, 8, 19, tzinfo=UTC) + timedelta(days=365))
        .sign(key, algorithm=None)
    )
    return certificate, certificate.public_bytes(serialization.Encoding.PEM)


def test_from_paths_returns_the_public_controller_ca_sha256_fingerprint(
    tmp_path: Path,
) -> None:
    certificate, pem = _controller_ca()
    controller_ca = tmp_path / "controller-ca.pem"
    controller_ca.write_bytes(pem)

    bootstrap = EnrollmentBootstrapConfig.from_paths(
        controller_endpoint="https://agents.example.test:8443",
        enrollment_endpoint="https://enroll.example.test:8443",
        controller_ca_path=controller_ca,
    )

    assert bootstrap == EnrollmentBootstrapConfig(
        controller_endpoint="https://agents.example.test:8443",
        enrollment_endpoint="https://enroll.example.test:8443",
        ca_fingerprint=certificate.fingerprint(hashes.SHA256()).hex(),
    )


def test_from_paths_rejects_non_https_controller_origin(tmp_path: Path) -> None:
    _, pem = _controller_ca()
    controller_ca = tmp_path / "controller-ca.pem"
    controller_ca.write_bytes(pem)

    with pytest.raises(ValueError, match="fixed HTTPS origin"):
        EnrollmentBootstrapConfig.from_paths(
            controller_endpoint="http://agents.example.test:8443",
            enrollment_endpoint="https://enroll.example.test:8443",
            controller_ca_path=controller_ca,
        )


@pytest.mark.parametrize("kind", ("missing", "symlink", "invalid"))
def test_from_paths_requires_one_regular_public_pem_certificate(
    tmp_path: Path,
    kind: str,
) -> None:
    controller_ca = tmp_path / "controller-ca.pem"
    if kind == "symlink":
        _, pem = _controller_ca()
        target = tmp_path / "controller-ca-target.pem"
        target.write_bytes(pem)
        controller_ca.symlink_to(target)
    elif kind == "invalid":
        controller_ca.write_text("not a certificate\n", encoding="utf-8")

    with pytest.raises(ValueError):
        EnrollmentBootstrapConfig.from_paths(
            controller_endpoint="https://agents.example.test:8443",
            enrollment_endpoint="https://enroll.example.test:8443",
            controller_ca_path=controller_ca,
        )


def test_from_paths_rejects_ca_material_with_a_private_key(tmp_path: Path) -> None:
    _, certificate_pem = _controller_ca()
    private_key = ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    controller_ca = tmp_path / "controller-ca.pem"
    controller_ca.write_bytes(certificate_pem + private_key)

    with pytest.raises(ValueError):
        EnrollmentBootstrapConfig.from_paths(
            controller_endpoint="https://agents.example.test:8443",
            enrollment_endpoint="https://enroll.example.test:8443",
            controller_ca_path=controller_ca,
        )
