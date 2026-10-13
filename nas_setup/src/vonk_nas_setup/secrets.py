"""Credential generation and identity-preserving repair of stored projections."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import quote

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from vonk_agent_protocol.installer_setup import (
    NasGeneratedSecrets,
    NasInstallTemplate,
    NasPostgresUrlRequest,
    NasStepCaControllerRequest,
    single_line,
)

from .filesystem import ObservationUnavailable, SetupError, read_member, retry


def generated(payload: NasInstallTemplate) -> NasGeneratedSecrets:
    return payload.generated_secrets or NasGeneratedSecrets()


def pki_paths(request: NasStepCaControllerRequest) -> tuple[str, ...]:
    return tuple(request.files.model_dump().values())


def secret_paths(payload: NasInstallTemplate) -> tuple[str, ...]:
    requests = generated(payload)
    paths = {item.file for item in payload.secrets}
    paths.update(item.file for item in requests.random_text)
    paths.update(item.file for item in requests.ed25519_pkcs8_pem)
    paths.update(item.file for item in requests.postgres_urls)
    if payload.step_ca_controller:
        paths.update(pki_paths(payload.step_ca_controller))
    return tuple(sorted(paths))


def secret_content(value: str) -> bytes:
    return (value.rstrip("\r\n") + "\n").encode() if value else b""


def random_text(count: int) -> str:
    return retry(lambda: os.urandom(count)).hex()


def postgres_url(request: NasPostgresUrlRequest, password: bytes) -> bytes:
    try:
        value = password.decode().rstrip("\r\n")
    except UnicodeDecodeError:
        raise SetupError(
            f"invalid password material for {request.password_file}"
        ) from None
    if not value or not single_line(value):
        raise SetupError(f"invalid password material for {request.password_file}")
    host = request.host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return secret_content(
        f"{request.scheme}://{quote(request.username, safe='')}:{quote(value, safe='')}"
        f"@{host}:{request.port}/{quote(request.database, safe='/')}"
    )


def validate_ed25519(content: bytes, name: str) -> None:
    try:
        key = serialization.load_pem_private_key(content, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise TypeError("wrong algorithm")
        canonical = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        if canonical.rstrip(b"\r\n") != content.rstrip(b"\r\n"):
            raise ValueError("not canonical")
    except (ValueError, TypeError):
        raise SetupError(
            f"{name} is not a canonical Ed25519 PKCS#8 PEM private key"
        ) from None


def generate_missing(payload: NasInstallTemplate, members: dict[str, bytes]) -> None:
    requests = generated(payload)
    for request in requests.random_text:
        if request.file not in members:
            members[request.file] = secret_content(
                (request.prefix or "") + random_text(request.bytes)
            )
    for request in requests.ed25519_pkcs8_pem:
        if request.file not in members:
            key = retry(Ed25519PrivateKey.generate)
            members[request.file] = key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
    for request in requests.postgres_urls:
        if request.password_file not in members:
            raise ObservationUnavailable()
        # URLs are generated projections of the actual password, so repair them.
        members[request.file] = postgres_url(request, members[request.password_file])


def load_members(root: Path, payload: NasInstallTemplate) -> dict[str, bytes]:
    members = {}
    for name in secret_paths(payload):
        value = read_member(root, name)
        if value is not None:
            members[name] = value
    return members


def repair_stored_members(
    payload: NasInstallTemplate,
    members: dict[str, bytes],
    retained: dict[str, bytes],
) -> None:
    """Repair malformed projections only; a well-formed foreign identity blocks."""
    damaged: set[str] = set()
    for request in generated(payload).ed25519_pkcs8_pem:
        if request.file in members:
            try:
                validate_ed25519(members[request.file], request.file)
            except SetupError:
                damaged.add(request.file)
    if payload.step_ca_controller:
        paths = payload.step_ca_controller.files
        for name in (
            paths.root_certificate,
            paths.intermediate_certificate,
            paths.controller_server_certificate,
        ):
            if name in members:
                try:
                    x509.load_pem_x509_certificate(members[name])
                except ValueError:
                    damaged.add(name)
        if paths.controller_server_private_key in members:
            from .pki import load_ed25519_private_key

            try:
                load_ed25519_private_key(members[paths.controller_server_private_key])
            except (ValueError, TypeError):
                damaged.add(paths.controller_server_private_key)
        if paths.provisioner_public_jwk in members:
            try:
                jwk = json.loads(members[paths.provisioner_public_jwk])
                if not isinstance(jwk, dict) or not all(
                    isinstance(jwk.get(name), str)
                    for name in ("kty", "crv", "x", "y", "kid")
                ):
                    damaged.add(paths.provisioner_public_jwk)
            except (ValueError, UnicodeDecodeError):
                damaged.add(paths.provisioner_public_jwk)
    for name in damaged:
        if name not in retained:
            raise ObservationUnavailable()
        members[name] = retained[name]
    if payload.step_ca_controller:
        from .pki import normalize_intermediate_key

        request = payload.step_ca_controller
        paths = payload.step_ca_controller.files
        try:
            normalized = normalize_intermediate_key(request, members)
            encrypted = normalized.get(
                paths.intermediate_private_key, members[paths.intermediate_private_key]
            )
            password = members[paths.password].rstrip(b"\r\n")
            serialization.load_pem_private_key(encrypted, password)
            members.update(normalized)
        except (KeyError, ValueError, TypeError):
            try:
                normalized = normalize_intermediate_key(request, retained)
                encrypted = normalized.get(
                    paths.intermediate_private_key,
                    retained[paths.intermediate_private_key],
                )
                password = retained[paths.password].rstrip(b"\r\n")
                key = serialization.load_pem_private_key(encrypted, password)
                certificate = x509.load_pem_x509_certificate(
                    members[paths.intermediate_certificate]
                )
                if key.public_key().public_bytes(
                    serialization.Encoding.DER,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                ) != certificate.public_key().public_bytes(
                    serialization.Encoding.DER,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                ):
                    raise ValueError("different signer")
            except (KeyError, ValueError, TypeError):
                raise ObservationUnavailable() from None
            members[paths.intermediate_private_key] = encrypted
            members[paths.password] = retained[paths.password]


def apply_secret_group(payload: NasInstallTemplate, root: Path) -> tuple[str, ...]:
    """Only explicitly named members receive group access; refusal is visible."""
    group = payload.group_readable_secrets
    if group is None:
        return ()
    pending = []
    for name in group.files:
        if read_member(root, name) is None:
            raise SetupError(f"group-readable secret is unavailable: {name}")
        path = root / name
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            entry = os.fstat(descriptor)
            if entry.st_nlink != 1:
                raise SetupError(f"group-readable secret is multiply linked: {name}")
            try:
                if entry.st_gid != group.gid:
                    os.fchown(descriptor, -1, group.gid)
            except PermissionError:
                os.fchmod(descriptor, 0o600)
                pending.append(name)
                continue
            os.fchmod(descriptor, 0o640)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return tuple(pending)
