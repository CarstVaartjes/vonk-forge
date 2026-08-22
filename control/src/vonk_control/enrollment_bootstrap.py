"""Immutable agent enrollment endpoints and controller trust metadata."""

from __future__ import annotations

import ipaddress
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

_ONE_PEM_CERTIFICATE = re.compile(
    rb"\A[ \t\r\n]*-----BEGIN CERTIFICATE-----\r?\n"
    rb"(?:[A-Za-z0-9+/=]+\r?\n)*[A-Za-z0-9+/=]+\r?\n"
    rb"-----END CERTIFICATE-----[ \t\r\n]*\Z"
)
_MAX_CONTROLLER_CA_BYTES = 64 * 1024
_HOSTNAME = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
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
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise ValueError(f"{name} must be a fixed HTTPS origin")
    return value.rstrip("/")


@dataclass(frozen=True)
class EnrollmentBootstrapConfig:
    controller_endpoint: str
    enrollment_endpoint: str
    ca_fingerprint: str
    ca_pem: str
    controller_address: str | None = None
    service_hostnames: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        controller_endpoint = _fixed_https_origin(
            self.controller_endpoint, name="controller endpoint"
        )
        enrollment_endpoint = _fixed_https_origin(
            self.enrollment_endpoint, name="enrollment endpoint"
        )
        certificate, canonical_pem = _public_ca(self.ca_pem.encode("ascii"))
        fingerprint = certificate.fingerprint(hashes.SHA256()).hex()
        if self.ca_fingerprint != fingerprint:
            raise ValueError("controller CA fingerprint does not match certificate")
        address = self.controller_address
        hostnames = tuple(self.service_hostnames)
        if address is None and hostnames or address is not None and not hostnames:
            raise ValueError(
                "controller address and service hostnames must be configured together"
            )
        if address is not None:
            try:
                parsed_address = ipaddress.ip_address(address)
            except ValueError as error:
                raise ValueError("controller address must be an IP address") from error
            if parsed_address.is_unspecified or parsed_address.is_multicast:
                raise ValueError("controller address must be a usable IP address")
            address = str(parsed_address)
            if (
                len(hostnames) > 16
                or len(set(hostnames)) != len(hostnames)
                or any(
                    hostname != hostname.lower()
                    or _HOSTNAME.fullmatch(hostname) is None
                    for hostname in hostnames
                )
            ):
                raise ValueError("service hostnames are invalid")
            endpoint_hostnames = {
                urlsplit(controller_endpoint).hostname,
                urlsplit(enrollment_endpoint).hostname,
            }
            if not endpoint_hostnames.issubset(hostnames):
                raise ValueError("service hostnames must include both agent endpoints")
        object.__setattr__(self, "controller_endpoint", controller_endpoint)
        object.__setattr__(self, "enrollment_endpoint", enrollment_endpoint)
        object.__setattr__(self, "ca_pem", canonical_pem.decode("ascii"))
        object.__setattr__(self, "controller_address", address)
        object.__setattr__(self, "service_hostnames", hostnames)

    @classmethod
    def from_paths(
        cls,
        *,
        controller_endpoint: str,
        enrollment_endpoint: str,
        controller_ca_path: Path,
        controller_address: str | None = None,
        service_hostnames: tuple[str, ...] = (),
    ) -> EnrollmentBootstrapConfig:
        pem = _read_public_ca(Path(controller_ca_path))
        certificate, canonical_pem = _public_ca(pem)
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
            ca_pem=canonical_pem.decode("ascii"),
            controller_address=controller_address,
            service_hostnames=service_hostnames,
        )


def _read_public_ca(path: Path) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError("controller CA must be a regular non-symlink file") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not 0 < metadata.st_size <= _MAX_CONTROLLER_CA_BYTES
        ):
            raise ValueError("controller CA must be a bounded regular file")
        chunks: list[bytes] = []
        remaining = _MAX_CONTROLLER_CA_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(16 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        pem = b"".join(chunks)
        if len(pem) > _MAX_CONTROLLER_CA_BYTES:
            raise ValueError("controller CA must be a bounded regular file")
        return pem
    finally:
        os.close(descriptor)


def _public_ca(pem: bytes) -> tuple[x509.Certificate, bytes]:
    if (
        len(pem) > _MAX_CONTROLLER_CA_BYTES
        or _ONE_PEM_CERTIFICATE.fullmatch(pem) is None
    ):
        raise ValueError("controller CA must contain exactly one PEM certificate")
    try:
        certificate = x509.load_pem_x509_certificate(pem)
        constraints = certificate.extensions.get_extension_for_class(
            x509.BasicConstraints
        ).value
    except (ValueError, x509.ExtensionNotFound) as error:
        raise ValueError(
            "controller CA must contain exactly one public CA certificate"
        ) from error
    if not constraints.ca:
        raise ValueError("controller CA must contain a CA certificate")
    return certificate, certificate.public_bytes(serialization.Encoding.PEM)
