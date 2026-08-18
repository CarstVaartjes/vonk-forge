"""Immutable agent enrollment endpoints and controller trust metadata."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes


_ONE_PEM_CERTIFICATE = re.compile(
    rb"\A[ \t\r\n]*-----BEGIN CERTIFICATE-----\r?\n"
    rb"(?:[A-Za-z0-9+/=]+\r?\n)*[A-Za-z0-9+/=]+\r?\n"
    rb"-----END CERTIFICATE-----[ \t\r\n]*\Z"
)


def _fixed_https_origin(value: str, *, name: str) -> str:
    if value != value.strip():
        raise ValueError(f"{name} must be a fixed HTTPS origin")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{name} must be a fixed HTTPS origin") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or port is not None and not 1 <= port <= 65535
    ):
        raise ValueError(f"{name} must be a fixed HTTPS origin")
    return value.rstrip("/")


@dataclass(frozen=True)
class EnrollmentBootstrapConfig:
    controller_endpoint: str
    enrollment_endpoint: str
    ca_fingerprint: str

    @classmethod
    def from_paths(
        cls,
        *,
        controller_endpoint: str,
        enrollment_endpoint: str,
        controller_ca_path: Path,
    ) -> EnrollmentBootstrapConfig:
        path = Path(controller_ca_path)
        if path.is_symlink() or not path.is_file():
            raise ValueError("controller CA must be a regular non-symlink file")
        pem = path.read_bytes()
        if _ONE_PEM_CERTIFICATE.fullmatch(pem) is None:
            raise ValueError("controller CA must contain exactly one PEM certificate")
        try:
            certificate = x509.load_pem_x509_certificate(pem)
        except ValueError as error:
            raise ValueError(
                "controller CA must contain exactly one PEM certificate"
            ) from error
        return cls(
            controller_endpoint=_fixed_https_origin(
                controller_endpoint,
                name="controller endpoint",
            ),
            enrollment_endpoint=_fixed_https_origin(
                enrollment_endpoint,
                name="enrollment endpoint",
            ),
            ca_fingerprint=certificate.fingerprint(hashes.SHA256()).hex(),
        )
