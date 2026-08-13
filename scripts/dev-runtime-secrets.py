#!/usr/bin/env python3
"""Create the complete development runtime secret bundle on local storage."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "control" / "src"))

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from vonk_control.passwords import hash_password, verify_password

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_MAX_SECRET_BYTES = 64 * 1024
_PRIVATE_MODE = 0o600
_UNSAFE_GENERATION_FILESYSTEMS = frozenset({"9p", "cifs", "drvfs", "smb3"})
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")

DEPLOYMENT_SECRET_NAMES = frozenset(
    {
        "admin-password-verifier",
        "agent-ca-certificate",
        "agent-ca-key",
        "agent-proxy-auth",
        "controller-ca",
        "controller-server-certificate",
        "controller-server-key",
        "database-url",
        "git-signing-key",
        "host-runtime-grant-private-key",
        "litellm-master-key",
        "litellm-upstream-key",
        "management-cidrs",
        "postgres-password",
        "tailscale-oauth-client-id",
        "tailscale-oauth-client-secret",
        "token-signing-key",
    }
)
LOCAL_SOURCE_SECRET_NAMES = DEPLOYMENT_SECRET_NAMES | frozenset(
    {
        "admin-password",
        "controller-ca-key",
        "git-signing-key.pub",
        "host-runtime-grant-public-key",
    }
)
# Compatibility for the project publisher, which validates the complete local
# source before publishing its fixed deployment subset.
RUNTIME_SECRET_NAMES = LOCAL_SOURCE_SECRET_NAMES
_BROWSER_ACCESS_SECRET_NAMES = frozenset(
    {
        "admin-password",
        "admin-password-verifier",
        "tailscale-oauth-client-id",
        "tailscale-oauth-client-secret",
    }
)
_PRE_BROWSER_ACCESS_SECRET_NAMES = RUNTIME_SECRET_NAMES - _BROWSER_ACCESS_SECRET_NAMES
_HOST_RUNTIME_PRIVATE = "host-runtime-grant-private-key"
_HOST_RUNTIME_PUBLIC = "host-runtime-grant-public-key"
_LEGACY_RUNTIME_SECRET_NAMES = RUNTIME_SECRET_NAMES - {
    _HOST_RUNTIME_PRIVATE,
    _HOST_RUNTIME_PUBLIC,
}
_ROTATION_PREFIX = ".admin-password-rotation-"
_ROTATION_NAME = re.compile(r"\.admin-password-rotation-[0-9a-f]{32}\Z")
_ROTATION_PASSWORD = "admin-password"
_ROTATION_VERIFIER = "admin-password-verifier"
_ROTATION_JOURNAL = "journal"
_ROTATION_MANIFEST_TEMPORARY = ".journal.tmp"
_LEGACY_ROTATION_JOURNAL = ".admin-password-rotation"
_LEGACY_ROTATION_PASSWORD = ".admin-password.rotate"
_LEGACY_ROTATION_VERIFIER = ".admin-password-verifier.rotate"
_LEGACY_ROTATION_ENTRIES = frozenset(
    {
        _LEGACY_ROTATION_JOURNAL,
        _LEGACY_ROTATION_PASSWORD,
        _LEGACY_ROTATION_VERIFIER,
    }
)
_ROTATION_DIGEST_KEYS = frozenset(
    {
        "old_password_sha256",
        "old_verifier_sha256",
        "new_password_sha256",
        "new_verifier_sha256",
    }
)


class RuntimeSecretError(RuntimeError):
    """The requested development secret operation is unsafe or invalid."""


class _OwnedEntry:
    __slots__ = ("descriptor", "device", "inode", "name")

    def __init__(
        self, name: str, device: int, inode: int, descriptor: int = -1
    ) -> None:
        self.name = name
        self.device = device
        self.inode = inode
        self.descriptor = descriptor


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _validate_hostname(value: str) -> str:
    hostname = value.rstrip(".").lower()
    if (
        not hostname
        or len(hostname) > 253
        or hostname != value.lower()
        or any(_HOST_LABEL.fullmatch(label) is None for label in hostname.split("."))
    ):
        raise RuntimeSecretError("development hostname is invalid")
    return hostname


def _validate_cidrs(value: str) -> tuple[str, ...]:
    raw_values = [part.strip() for part in value.split(",")]
    if not raw_values or any(not part for part in raw_values):
        raise RuntimeSecretError("development management CIDRs are invalid")
    try:
        networks = tuple(ipaddress.ip_network(part, strict=True) for part in raw_values)
    except ValueError as error:
        raise RuntimeSecretError("development management CIDRs are invalid") from error
    normalized = tuple(str(network) for network in networks)
    if len(set(normalized)) != len(normalized):
        raise RuntimeSecretError("development management CIDRs are duplicated")
    return normalized


def _filesystem_type(path: Path) -> str:
    """Return Linux's filesystem type for the nearest existing path."""
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    physical = os.path.realpath(candidate)
    best_mount = ""
    best_type = "unknown"
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as mountinfo:
            for line in mountinfo:
                left, separator, right = line.partition(" - ")
                if not separator:
                    continue
                fields = left.split()
                trailing = right.split()
                if len(fields) < 5 or not trailing:
                    continue
                mount = fields[4].replace("\\040", " ").replace("\\134", "\\")
                if (
                    physical == mount or physical.startswith(mount.rstrip("/") + "/")
                ) and len(mount) > len(best_mount):
                    best_mount = mount
                    best_type = trailing[0]
    except OSError as error:
        raise RuntimeSecretError(
            "development filesystem cannot be identified"
        ) from error
    return best_type


def _open_private_parent(path: Path) -> int:
    if (
        os.name != "posix"
        or not path.is_absolute()
        or "\\" in str(path)
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        raise RuntimeSecretError(
            "development secrets path must be an absolute local path"
        )
    components = path.parent.parts[1:]
    descriptor = -1
    try:
        descriptor = os.open("/", _DIRECTORY_FLAGS)
        for component in components:
            listed = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            opened = os.fstat(child)
            if not _same_inode(listed, opened) or not stat.S_ISDIR(opened.st_mode):
                os.close(child)
                raise RuntimeSecretError(
                    "development secrets parent changed while opening"
                )
            os.close(descriptor)
            descriptor = child
        opened = os.fstat(descriptor)
        if opened.st_uid != os.geteuid() or stat.S_IMODE(opened.st_mode) != 0o700:
            raise RuntimeSecretError(
                "development secrets parent ownership or mode is unsafe"
            )
        return descriptor
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise RuntimeSecretError(
            "development secrets parent cannot be opened safely"
        ) from error
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise


def _ensure_default_development_directory(repository_root: Path) -> None:
    repository = -1
    development = -1
    try:
        repository = os.open(repository_root, _DIRECTORY_FLAGS)
        try:
            listed = os.stat(".dev", dir_fd=repository, follow_symlinks=False)
        except FileNotFoundError:
            os.mkdir(".dev", 0o700, dir_fd=repository)
            listed = os.stat(".dev", dir_fd=repository, follow_symlinks=False)
        development = os.open(".dev", _DIRECTORY_FLAGS, dir_fd=repository)
        opened = os.fstat(development)
        if (
            not _same_inode(listed, opened)
            or not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o700
        ):
            raise RuntimeSecretError(
                "development directory ownership or mode is unsafe"
            )
    except OSError as error:
        raise RuntimeSecretError(
            "development directory cannot be opened safely"
        ) from error
    finally:
        if development >= 0:
            os.close(development)
        if repository >= 0:
            os.close(repository)


def _read_file(directory: int, name: str) -> bytes:
    try:
        listed = os.stat(name, dir_fd=directory, follow_symlinks=False)
        descriptor = os.open(name, _READ_FLAGS, dir_fd=directory)
    except OSError as error:
        raise RuntimeSecretError(f"development secret {name} is unsafe") from error
    try:
        before = os.fstat(descriptor)
        if (
            not _same_inode(listed, before)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != _PRIVATE_MODE
            or not 0 < before.st_size <= _MAX_SECRET_BYTES
        ):
            raise RuntimeSecretError(f"development secret {name} is unsafe")
        content = bytearray()
        while len(content) <= _MAX_SECRET_BYTES:
            chunk = os.read(descriptor, min(4096, _MAX_SECRET_BYTES + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_nlink,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        updated = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_nlink,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if len(content) != before.st_size or identity != updated:
            raise RuntimeSecretError(f"development secret {name} changed while reading")
        return bytes(content)
    finally:
        os.close(descriptor)


def _write_file(directory: int, name: str, content: bytes) -> None:
    descriptor = -1
    try:
        descriptor = os.open(name, _WRITE_FLAGS, _PRIVATE_MODE, dir_fd=directory)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fchmod(descriptor, _PRIVATE_MODE)
        os.fsync(descriptor)
    except OSError as error:
        raise RuntimeSecretError(
            f"development secret {name} cannot be created"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _owned_entry(
    name: str, metadata: os.stat_result, descriptor: int = -1
) -> _OwnedEntry:
    return _OwnedEntry(name, metadata.st_dev, metadata.st_ino, descriptor)


def _same_owned_entry(entry: _OwnedEntry, metadata: os.stat_result) -> bool:
    return (entry.device, entry.inode) == (metadata.st_dev, metadata.st_ino)


def _unlink_owned_entry(directory: int, entry: _OwnedEntry) -> bool:
    if entry.descriptor < 0:
        return False
    try:
        current = os.stat(entry.name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    opened = os.fstat(entry.descriptor)
    if (
        not _same_inode(current, opened)
        or not stat.S_ISREG(current.st_mode)
        or not stat.S_ISREG(opened.st_mode)
        or current.st_uid != os.geteuid()
        or opened.st_uid != os.geteuid()
        or current.st_nlink != 1
        or opened.st_nlink != 1
    ):
        return False
    os.unlink(entry.name, dir_fd=directory)
    return True


def _close_owned_entry(entry: _OwnedEntry) -> None:
    if entry.descriptor >= 0:
        os.close(entry.descriptor)
        entry.descriptor = -1


def _rmdir_owned_entry(directory: int, entry: _OwnedEntry) -> bool:
    if entry.descriptor < 0:
        return False
    try:
        current = os.stat(entry.name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    opened = os.fstat(entry.descriptor)
    if (
        not _same_inode(current, opened)
        or not stat.S_ISDIR(current.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or current.st_uid != os.geteuid()
        or opened.st_uid != os.geteuid()
        or current.st_nlink != 2
        or opened.st_nlink != 2
        or stat.S_IMODE(current.st_mode) != 0o700
        or stat.S_IMODE(opened.st_mode) != 0o700
    ):
        return False
    os.rmdir(entry.name, dir_fd=directory)
    return True


def _create_owned_file(directory: int, name: str, content: bytes) -> _OwnedEntry:
    descriptor = -1
    owned: _OwnedEntry | None = None
    succeeded = False
    try:
        descriptor = os.open(name, _WRITE_FLAGS, _PRIVATE_MODE, dir_fd=directory)
        opened = os.fstat(descriptor)
        owned = _owned_entry(name, opened, descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
        ):
            raise RuntimeSecretError(f"development secret {name} cannot be created")
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fchmod(descriptor, _PRIVATE_MODE)
        os.fsync(descriptor)
        final = os.fstat(descriptor)
        if (
            not _same_owned_entry(owned, final)
            or final.st_size != len(content)
            or stat.S_IMODE(final.st_mode) != _PRIVATE_MODE
        ):
            raise RuntimeSecretError(f"development secret {name} cannot be created")
        succeeded = True
        return owned
    except Exception as error:
        if owned is not None:
            _unlink_owned_entry(directory, owned)
        if isinstance(error, RuntimeSecretError):
            raise
        raise RuntimeSecretError(
            f"development secret {name} cannot be created"
        ) from error
    finally:
        if descriptor >= 0 and not succeeded:
            if owned is not None:
                _close_owned_entry(owned)
            else:
                os.close(descriptor)


def _read_rotation_file(directory: int, name: str) -> tuple[bytes, _OwnedEntry]:
    descriptor = -1
    succeeded = False
    try:
        listed = os.stat(name, dir_fd=directory, follow_symlinks=False)
        descriptor = os.open(name, _READ_FLAGS, dir_fd=directory)
        before = os.fstat(descriptor)
        if (
            not _same_inode(listed, before)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != _PRIVATE_MODE
            or not 0 <= before.st_size <= _MAX_SECRET_BYTES
        ):
            raise RuntimeSecretError(
                "development administrator rotation state is invalid"
            )
        content = bytearray()
        while len(content) <= _MAX_SECRET_BYTES:
            chunk = os.read(descriptor, min(4096, _MAX_SECRET_BYTES + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_nlink,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        updated = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_nlink,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if len(content) != before.st_size or identity != updated:
            raise RuntimeSecretError(
                "development administrator rotation state is invalid"
            )
        succeeded = True
        return bytes(content), _owned_entry(name, before, descriptor)
    except RuntimeSecretError:
        raise
    except OSError as error:
        raise RuntimeSecretError(
            "development administrator rotation state is invalid"
        ) from error
    finally:
        if descriptor >= 0 and not succeeded:
            os.close(descriptor)


def _create_owned_directory(directory: int, name: str) -> tuple[int, _OwnedEntry]:
    descriptor = -1
    owned: _OwnedEntry | None = None
    try:
        os.mkdir(name, 0o700, dir_fd=directory)
        listed = os.stat(name, dir_fd=directory, follow_symlinks=False)
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory)
        opened = os.fstat(descriptor)
        owned = _owned_entry(name, opened, descriptor)
        if (
            not _same_inode(listed, opened)
            or not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 2
            or stat.S_IMODE(opened.st_mode) != 0o700
        ):
            raise RuntimeSecretError(
                "development administrator rotation cannot be created"
            )
        return descriptor, owned
    except Exception as error:
        if owned is not None:
            _rmdir_owned_entry(directory, owned)
            _close_owned_entry(owned)
            descriptor = -1
        elif descriptor >= 0:
            os.close(descriptor)
            descriptor = -1
        if isinstance(error, RuntimeSecretError):
            raise
        raise RuntimeSecretError(
            "development administrator rotation cannot be created"
        ) from error


def _acquire_mutation_lock(directory: int) -> int:
    descriptor = -1
    try:
        listed = os.stat("management-cidrs", dir_fd=directory, follow_symlinks=False)
        descriptor = os.open("management-cidrs", _READ_FLAGS, dir_fd=directory)
        before = os.fstat(descriptor)
        if (
            not _same_inode(listed, before)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != _PRIVATE_MODE
            or not 0 < before.st_size <= _MAX_SECRET_BYTES
        ):
            raise RuntimeSecretError("development mutation lock is unsafe")
        identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_nlink,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        named = os.stat("management-cidrs", dir_fd=directory, follow_symlinks=False)
        after = os.fstat(descriptor)
        updated = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_nlink,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if not _same_inode(named, after) or identity != updated:
            raise RuntimeSecretError("development mutation lock changed")
        return descriptor
    except RuntimeSecretError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise RuntimeSecretError(
            "development mutation lock cannot be acquired safely"
        ) from error


def _read_external_secret(path: Path) -> bytes:
    parent = _open_private_parent(path)
    try:
        return _read_file(parent, path.name)
    finally:
        os.close(parent)


def _unlink_if_present(directory: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory)
    except FileNotFoundError:
        pass


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _ca_certificate(common_name: str, key: Ed25519PrivateKey) -> x509.Certificate:
    now = dt.datetime.now(dt.UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .sign(key, algorithm=None)
    )


def _secret_bundle(
    *,
    management_cidrs: tuple[str, ...],
    enroll_hostname: str,
    agent_hostname: str,
    registry_hostname: str,
    tailscale_oauth_client_id: bytes,
    tailscale_oauth_client_secret: bytes,
) -> dict[str, bytes]:
    agent_key = Ed25519PrivateKey.generate()
    controller_key = Ed25519PrivateKey.generate()
    server_key = Ed25519PrivateKey.generate()
    signing_key = Ed25519PrivateKey.generate()
    host_runtime_key = Ed25519PrivateKey.generate()
    agent_ca = _ca_certificate("Vonk Forge Development Agent CA", agent_key)
    controller_ca = _ca_certificate(
        "Vonk Forge Development Controller CA", controller_key
    )
    now = dt.datetime.now(dt.UTC)
    server_subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Vonk Forge Development Controller")]
    )
    server_certificate = (
        x509.CertificateBuilder()
        .subject_name(server_subject)
        .issuer_name(controller_ca.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=825))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName(enroll_hostname),
                    x509.DNSName(agent_hostname),
                    x509.DNSName(registry_hostname),
                ]
            ),
            critical=False,
        )
        .sign(controller_key, algorithm=None)
    )
    password = secrets.token_hex(32)
    admin_password, admin_password_verifier = _admin_credential_pair()
    private_pem = lambda key: key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    signing_private = (
        signing_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
        + b"\n"
    )
    signing_public = (
        signing_key.public_key().public_bytes(
            serialization.Encoding.OpenSSH,
            serialization.PublicFormat.OpenSSH,
        )
        + b" vonk-forge-dev\n"
    )
    token = lambda: (secrets.token_urlsafe(32) + "\n").encode("ascii")
    return {
        "admin-password": admin_password,
        "admin-password-verifier": admin_password_verifier,
        "agent-ca-certificate": agent_ca.public_bytes(serialization.Encoding.PEM),
        "agent-ca-key": private_pem(agent_key),
        "agent-proxy-auth": token(),
        "controller-ca": controller_ca.public_bytes(serialization.Encoding.PEM),
        "controller-ca-key": private_pem(controller_key),
        "controller-server-certificate": server_certificate.public_bytes(
            serialization.Encoding.PEM
        ),
        "controller-server-key": private_pem(server_key),
        "database-url": (
            f"postgresql+psycopg://control:{password}@postgres:5432/control\n".encode(
                "ascii"
            )
        ),
        "git-signing-key": signing_private,
        "git-signing-key.pub": signing_public,
        "host-runtime-grant-private-key": private_pem(host_runtime_key),
        "host-runtime-grant-public-key": (
            host_runtime_key.public_key().public_bytes_raw().hex() + "\n"
        ).encode("ascii"),
        "litellm-master-key": token(),
        "litellm-upstream-key": token(),
        "management-cidrs": ("\n".join(management_cidrs) + "\n").encode("ascii"),
        "postgres-password": (password + "\n").encode("ascii"),
        "tailscale-oauth-client-id": tailscale_oauth_client_id,
        "tailscale-oauth-client-secret": tailscale_oauth_client_secret,
        "token-signing-key": token(),
    }


def _admin_credential_pair() -> tuple[bytes, bytes]:
    password = secrets.token_urlsafe(32)
    verifier = hash_password(password)
    return (password + "\n").encode("ascii"), (verifier + "\n").encode("ascii")


def _validate_admin_credential(password: bytes, verifier: bytes) -> None:
    try:
        admin_password = password.decode("ascii")
        admin_verifier = verifier.decode("ascii")
        password_value = admin_password.removesuffix("\n")
        verifier_value = admin_verifier.removesuffix("\n")
        verifier_parts = verifier_value.split("$")
        salt = base64.b64decode(verifier_parts[4] + "=" * (-len(verifier_parts[4]) % 4))
        password_hash = base64.b64decode(
            verifier_parts[5] + "=" * (-len(verifier_parts[5]) % 4)
        )
    except (IndexError, UnicodeDecodeError, ValueError) as error:
        raise RuntimeSecretError(
            "development administrator credential is invalid"
        ) from error
    verification = verify_password(verifier_value, password_value)
    if (
        admin_password != password_value + "\n"
        or re.fullmatch(r"[A-Za-z0-9_-]{43}", password_value) is None
        or admin_verifier != verifier_value + "\n"
        or verifier_parts[:4]
        != [
            "",
            "argon2id",
            "v=19",
            "m=65536,t=3,p=1",
        ]
        or len(verifier_parts) != 6
        or len(salt) != 16
        or len(password_hash) != 32
        or not verification.valid
        or verification.needs_rehash
    ):
        raise RuntimeSecretError("development administrator credential is invalid")


def _validate_bundle(
    bundle: dict[str, bytes],
    *,
    management_cidrs: tuple[str, ...],
    enroll_hostname: str,
    agent_hostname: str,
    registry_hostname: str,
) -> None:
    if set(bundle) != RUNTIME_SECRET_NAMES:
        raise RuntimeSecretError("development secret bundle is incomplete")
    try:
        agent_ca = x509.load_pem_x509_certificate(bundle["agent-ca-certificate"])
        controller_ca = x509.load_pem_x509_certificate(bundle["controller-ca"])
        server = x509.load_pem_x509_certificate(bundle["controller-server-certificate"])
        agent_key = serialization.load_pem_private_key(
            bundle["agent-ca-key"], password=None
        )
        controller_key = serialization.load_pem_private_key(
            bundle["controller-ca-key"], password=None
        )
        server_key = serialization.load_pem_private_key(
            bundle["controller-server-key"], password=None
        )
        signing_key = serialization.load_ssh_private_key(
            bundle["git-signing-key"], password=None
        )
        signing_public = serialization.load_ssh_public_key(
            bundle["git-signing-key.pub"]
        )
        host_runtime_key = serialization.load_pem_private_key(
            bundle["host-runtime-grant-private-key"], password=None
        )
        host_runtime_public = bytes.fromhex(
            bundle["host-runtime-grant-public-key"].decode("ascii").strip()
        )
    except (TypeError, ValueError) as error:
        raise RuntimeSecretError(
            "development secret bundle contains invalid key material"
        ) from error
    agent_public = agent_ca.public_key()
    controller_public = controller_ca.public_key()
    server_public = server.public_key()
    if (
        not isinstance(agent_key, Ed25519PrivateKey)
        or not isinstance(controller_key, Ed25519PrivateKey)
        or not isinstance(server_key, Ed25519PrivateKey)
        or not isinstance(signing_key, Ed25519PrivateKey)
        or not isinstance(signing_public, Ed25519PublicKey)
        or not isinstance(host_runtime_key, Ed25519PrivateKey)
        or len(host_runtime_public) != 32
        or not isinstance(agent_public, Ed25519PublicKey)
        or not isinstance(controller_public, Ed25519PublicKey)
        or not isinstance(server_public, Ed25519PublicKey)
        or agent_public.public_bytes_raw() != agent_key.public_key().public_bytes_raw()
        or controller_public.public_bytes_raw()
        != controller_key.public_key().public_bytes_raw()
        or server_public.public_bytes_raw()
        != server_key.public_key().public_bytes_raw()
        or signing_key.public_key().public_bytes_raw()
        != signing_public.public_bytes_raw()
        or host_runtime_key.public_key().public_bytes_raw() != host_runtime_public
        or agent_ca.subject == controller_ca.subject
        or agent_ca.serial_number == controller_ca.serial_number
        or server.issuer != controller_ca.subject
    ):
        raise RuntimeSecretError(
            "development secret bundle key relationships are invalid"
        )

    expected_agent_subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Vonk Forge Development Agent CA")]
    )
    expected_controller_subject = x509.Name(
        [
            x509.NameAttribute(
                NameOID.COMMON_NAME, "Vonk Forge Development Controller CA"
            )
        ]
    )
    expected_server_subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Vonk Forge Development Controller")]
    )
    try:
        agent_public.verify(agent_ca.signature, agent_ca.tbs_certificate_bytes)
        controller_public.verify(
            controller_ca.signature, controller_ca.tbs_certificate_bytes
        )
        controller_public.verify(server.signature, server.tbs_certificate_bytes)
        agent_basic = agent_ca.extensions.get_extension_for_class(
            x509.BasicConstraints
        ).value
        controller_basic = controller_ca.extensions.get_extension_for_class(
            x509.BasicConstraints
        ).value
        server_basic = server.extensions.get_extension_for_class(
            x509.BasicConstraints
        ).value
        agent_usage = agent_ca.extensions.get_extension_for_class(x509.KeyUsage).value
        controller_usage = controller_ca.extensions.get_extension_for_class(
            x509.KeyUsage
        ).value
        server_usage = server.extensions.get_extension_for_class(x509.KeyUsage).value
        server_extended_usage = server.extensions.get_extension_for_class(
            x509.ExtendedKeyUsage
        ).value
        sans = server.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
    except (InvalidSignature, ValueError, x509.ExtensionNotFound) as error:
        raise RuntimeSecretError(
            "development secret certificate constraints are invalid"
        ) from error
    now = dt.datetime.now(dt.UTC)
    agent_before, agent_after = _certificate_window(agent_ca)
    controller_before, controller_after = _certificate_window(controller_ca)
    server_before, server_after = _certificate_window(server)

    def usage_values(usage: x509.KeyUsage) -> tuple[bool | None, ...]:
        return (
            usage.digital_signature,
            usage.content_commitment,
            usage.key_encipherment,
            usage.data_encipherment,
            usage.key_agreement,
            usage.key_cert_sign,
            usage.crl_sign,
            usage.encipher_only if usage.key_agreement else None,
            usage.decipher_only if usage.key_agreement else None,
        )

    expected_ca_usage = (True, False, False, False, False, True, True, None, None)
    expected_server_usage = (
        True,
        False,
        False,
        False,
        False,
        False,
        False,
        None,
        None,
    )
    if (
        agent_ca.subject != expected_agent_subject
        or agent_ca.issuer != agent_ca.subject
        or controller_ca.subject != expected_controller_subject
        or controller_ca.issuer != controller_ca.subject
        or server.subject != expected_server_subject
        or agent_basic != x509.BasicConstraints(ca=True, path_length=0)
        or controller_basic != x509.BasicConstraints(ca=True, path_length=0)
        or server_basic != x509.BasicConstraints(ca=False, path_length=None)
        or usage_values(agent_usage) != expected_ca_usage
        or usage_values(controller_usage) != expected_ca_usage
        or usage_values(server_usage) != expected_server_usage
        or set(server_extended_usage) != {ExtendedKeyUsageOID.SERVER_AUTH}
        or set(sans.get_values_for_type(x509.DNSName))
        != {enroll_hostname, agent_hostname, registry_hostname}
        or not agent_before <= now <= agent_after
        or not controller_before <= now <= controller_after
        or not server_before <= now <= server_after
    ):
        raise RuntimeSecretError(
            "development secret certificate constraints are invalid"
        )
    password = bundle["postgres-password"].decode("ascii", errors="strict")
    if not re.fullmatch(r"[0-9a-f]{64}\n", password):
        raise RuntimeSecretError("development database credential is invalid")
    expected_url = (
        f"postgresql+psycopg://control:{password.strip()}@postgres:5432/control\n"
    )
    if bundle["database-url"] != expected_url.encode("ascii"):
        raise RuntimeSecretError(
            "development database URL does not match its credential"
        )
    if bundle["management-cidrs"] != ("\n".join(management_cidrs) + "\n").encode(
        "ascii"
    ):
        raise RuntimeSecretError("development management CIDRs do not match")
    _validate_admin_credential(
        bundle["admin-password"], bundle["admin-password-verifier"]
    )
    for name in (
        "agent-proxy-auth",
        "litellm-master-key",
        "litellm-upstream-key",
        "token-signing-key",
    ):
        try:
            value = bundle[name].decode("ascii").strip()
            decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        except (UnicodeDecodeError, ValueError) as error:
            raise RuntimeSecretError(f"development token {name} is invalid") from error
        if bundle[name] != (value + "\n").encode("ascii") or len(decoded) < 32:
            raise RuntimeSecretError(f"development token {name} is invalid")


def _open_existing_directory(
    parent: int, name: str
) -> tuple[int, os.stat_result] | None:
    try:
        listed = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    except OSError as error:
        raise RuntimeSecretError(
            "development secrets directory cannot be opened safely"
        ) from error
    opened = os.fstat(descriptor)
    if (
        not _same_inode(listed, opened)
        or not stat.S_ISDIR(opened.st_mode)
        or opened.st_uid != os.geteuid()
        or stat.S_IMODE(opened.st_mode) != 0o700
    ):
        os.close(descriptor)
        raise RuntimeSecretError(
            "development secrets directory ownership or mode is unsafe"
        )
    return descriptor, opened


def _remove_staging(parent: int, name: str, descriptor: int) -> None:
    try:
        for child in os.listdir(descriptor):
            os.unlink(child, dir_fd=descriptor)
    finally:
        os.close(descriptor)
        try:
            os.rmdir(name, dir_fd=parent)
        except FileNotFoundError:
            pass


def _host_runtime_authority_pair(
    retained_private: bytes | None = None,
) -> tuple[bytes, bytes]:
    if retained_private is None:
        key = Ed25519PrivateKey.generate()
        private = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    else:
        try:
            key = serialization.load_pem_private_key(retained_private, password=None)
        except (TypeError, ValueError) as error:
            raise RuntimeSecretError(
                "development host runtime authority is invalid"
            ) from error
        if not isinstance(key, Ed25519PrivateKey):
            raise RuntimeSecretError("development host runtime authority is invalid")
        private = retained_private
    public = (key.public_key().public_bytes_raw().hex() + "\n").encode("ascii")
    return private, public


def _upgrade_host_runtime_authority(
    directory: int,
    bundle: dict[str, bytes],
    *,
    management_cidrs: tuple[str, ...],
    enroll_hostname: str,
    agent_hostname: str,
    registry_hostname: str,
) -> None:
    retained = bundle.get(_HOST_RUNTIME_PRIVATE)
    private, public = _host_runtime_authority_pair(retained)
    candidate = dict(bundle)
    candidate[_HOST_RUNTIME_PRIVATE] = private
    candidate[_HOST_RUNTIME_PUBLIC] = public
    _validate_bundle(
        candidate,
        management_cidrs=management_cidrs,
        enroll_hostname=enroll_hostname,
        agent_hostname=agent_hostname,
        registry_hostname=registry_hostname,
    )
    if retained is None:
        _write_file(directory, _HOST_RUNTIME_PRIVATE, private)
        os.fsync(directory)
    _write_file(directory, _HOST_RUNTIME_PUBLIC, public)
    os.fsync(directory)
    installed = {name: _read_file(directory, name) for name in RUNTIME_SECRET_NAMES}
    _validate_bundle(
        installed,
        management_cidrs=management_cidrs,
        enroll_hostname=enroll_hostname,
        agent_hostname=agent_hostname,
        registry_hostname=registry_hostname,
    )


def _upgrade_browser_access(
    directory: int,
    prior_bundle: dict[str, bytes],
    *,
    tailscale_oauth_client_id: bytes,
    tailscale_oauth_client_secret: bytes,
    management_cidrs: tuple[str, ...],
    enroll_hostname: str,
    agent_hostname: str,
    registry_hostname: str,
) -> None:
    password, verifier = _admin_credential_pair()
    additions = {
        "admin-password": password,
        "admin-password-verifier": verifier,
        "tailscale-oauth-client-id": tailscale_oauth_client_id,
        "tailscale-oauth-client-secret": tailscale_oauth_client_secret,
    }
    candidate = dict(prior_bundle)
    candidate.update(additions)
    _validate_bundle(
        candidate,
        management_cidrs=management_cidrs,
        enroll_hostname=enroll_hostname,
        agent_hostname=agent_hostname,
        registry_hostname=registry_hostname,
    )
    created: list[_OwnedEntry] = []
    try:
        for name, content in sorted(additions.items()):
            created.append(_create_owned_file(directory, name, content))
        os.fsync(directory)
        installed = {name: _read_file(directory, name) for name in RUNTIME_SECRET_NAMES}
        _validate_bundle(
            installed,
            management_cidrs=management_cidrs,
            enroll_hostname=enroll_hostname,
            agent_hostname=agent_hostname,
            registry_hostname=registry_hostname,
        )
    except BaseException:
        for entry in created:
            _unlink_owned_entry(directory, entry)
        os.fsync(directory)
        raise
    finally:
        for entry in created:
            _close_owned_entry(entry)


def _rotation_journal(
    old_password: bytes,
    old_verifier: bytes,
    new_password: bytes,
    new_verifier: bytes,
) -> bytes:
    return (
        json.dumps(
            {
                "old_password_sha256": _sha256(old_password),
                "old_verifier_sha256": _sha256(old_verifier),
                "new_password_sha256": _sha256(new_password),
                "new_verifier_sha256": _sha256(new_verifier),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")


def _parse_rotation_journal(content: bytes) -> dict[str, str]:
    try:
        document = json.loads(content.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeSecretError(
            "development administrator rotation journal is invalid"
        ) from error
    if (
        not isinstance(document, dict)
        or set(document) != _ROTATION_DIGEST_KEYS
        or any(
            not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in document.values()
        )
    ):
        raise RuntimeSecretError(
            "development administrator rotation journal is invalid"
        )
    return document


def _legacy_rotation_value(directory: int, name: str) -> bytes | None:
    try:
        os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return _read_file(directory, name)


def _finish_legacy_admin_rotation(directory: int, journal: dict[str, str]) -> None:
    password = _read_file(directory, "admin-password")
    verifier = _read_file(directory, "admin-password-verifier")
    password_temporary = _legacy_rotation_value(directory, _LEGACY_ROTATION_PASSWORD)
    verifier_temporary = _legacy_rotation_value(directory, _LEGACY_ROTATION_VERIFIER)
    password_digest = _sha256(password)
    verifier_digest = _sha256(verifier)
    temporary_password_digest = (
        None if password_temporary is None else _sha256(password_temporary)
    )
    temporary_verifier_digest = (
        None if verifier_temporary is None else _sha256(verifier_temporary)
    )
    old_pair = (
        password_digest == journal["old_password_sha256"]
        and verifier_digest == journal["old_verifier_sha256"]
    )
    new_pair = (
        password_digest == journal["new_password_sha256"]
        and verifier_digest == journal["new_verifier_sha256"]
    )
    if old_pair:
        valid_password_temporary = temporary_password_digest in {
            None,
            journal["new_password_sha256"],
        }
        valid_verifier_temporary = temporary_verifier_digest in {
            None,
            journal["new_verifier_sha256"],
        }
        if not valid_password_temporary or not valid_verifier_temporary:
            raise RuntimeSecretError(
                "development administrator rotation state is invalid"
            )
        if password_temporary is not None and verifier_temporary is not None:
            os.replace(
                _LEGACY_ROTATION_PASSWORD,
                "admin-password",
                src_dir_fd=directory,
                dst_dir_fd=directory,
            )
            os.fsync(directory)
            os.replace(
                _LEGACY_ROTATION_VERIFIER,
                "admin-password-verifier",
                src_dir_fd=directory,
                dst_dir_fd=directory,
            )
        else:
            _unlink_if_present(directory, _LEGACY_ROTATION_PASSWORD)
            _unlink_if_present(directory, _LEGACY_ROTATION_VERIFIER)
    elif (
        password_digest == journal["new_password_sha256"]
        and verifier_digest == journal["old_verifier_sha256"]
        and password_temporary is None
        and temporary_verifier_digest == journal["new_verifier_sha256"]
    ):
        os.fsync(directory)
        os.replace(
            _LEGACY_ROTATION_VERIFIER,
            "admin-password-verifier",
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
    elif new_pair and password_temporary is None and verifier_temporary is None:
        pass
    else:
        raise RuntimeSecretError("development administrator rotation state is invalid")
    os.fsync(directory)
    _unlink_if_present(directory, _LEGACY_ROTATION_JOURNAL)
    os.fsync(directory)


def _replace_owned_rotation_file(
    source: int,
    entry: _OwnedEntry,
    destination: int,
    destination_name: str,
) -> None:
    if entry.descriptor < 0:
        raise RuntimeSecretError("development administrator rotation state is invalid")
    try:
        current = os.stat(entry.name, dir_fd=source, follow_symlinks=False)
        opened = os.fstat(entry.descriptor)
    except OSError as error:
        raise RuntimeSecretError(
            "development administrator rotation state is invalid"
        ) from error
    if (
        not _same_inode(current, opened)
        or not stat.S_ISREG(current.st_mode)
        or not stat.S_ISREG(opened.st_mode)
        or current.st_uid != os.geteuid()
        or opened.st_uid != os.geteuid()
        or current.st_nlink != 1
        or opened.st_nlink != 1
    ):
        raise RuntimeSecretError("development administrator rotation state is invalid")
    os.replace(
        entry.name,
        destination_name,
        src_dir_fd=source,
        dst_dir_fd=destination,
    )


def _remove_rotation_transaction(
    directory: int,
    transaction: int,
    transaction_entry: _OwnedEntry,
    entries: dict[str, tuple[bytes, _OwnedEntry]],
) -> None:
    for _content, entry in entries.values():
        _unlink_owned_entry(transaction, entry)
    if os.listdir(transaction):
        raise RuntimeSecretError("development administrator rotation state is invalid")
    os.fsync(transaction)
    if not _rmdir_owned_entry(directory, transaction_entry):
        raise RuntimeSecretError("development administrator rotation state is invalid")
    os.fsync(directory)


def _open_rotation_transaction(
    directory: int, name: str
) -> tuple[int, _OwnedEntry, dict[str, tuple[bytes, _OwnedEntry]]]:
    opened = _open_existing_directory(directory, name)
    if opened is None:
        raise RuntimeSecretError("development administrator rotation state is invalid")
    transaction, metadata = opened
    entries: dict[str, tuple[bytes, _OwnedEntry]] = {}
    try:
        if metadata.st_nlink != 2:
            raise RuntimeSecretError(
                "development administrator rotation state is invalid"
            )
        names = set(os.listdir(transaction))
        if _ROTATION_JOURNAL not in names:
            if not names <= {_ROTATION_MANIFEST_TEMPORARY}:
                raise RuntimeSecretError(
                    "development administrator rotation state is invalid"
                )
        elif not names <= {
            _ROTATION_JOURNAL,
            _ROTATION_PASSWORD,
            _ROTATION_VERIFIER,
        }:
            raise RuntimeSecretError(
                "development administrator rotation state is invalid"
            )
        for child in sorted(names):
            entries[child] = _read_rotation_file(transaction, child)
        return transaction, _owned_entry(name, metadata, transaction), entries
    except BaseException:
        for _content, entry in entries.values():
            _close_owned_entry(entry)
        os.close(transaction)
        raise


def _resume_admin_rotation(directory: int, name: str) -> None:
    transaction = -1
    transaction_entry: _OwnedEntry | None = None
    entries: dict[str, tuple[bytes, _OwnedEntry]] = {}
    try:
        transaction, transaction_entry, entries = _open_rotation_transaction(
            directory, name
        )
        password = _read_file(directory, "admin-password")
        verifier = _read_file(directory, "admin-password-verifier")
        journal_content = entries.get(_ROTATION_JOURNAL, (None, None))[0]
        if journal_content is None:
            _validate_admin_credential(password, verifier)
            _remove_rotation_transaction(
                directory, transaction, transaction_entry, entries
            )
            return
        journal = _parse_rotation_journal(journal_content)

        password_digest = _sha256(password)
        verifier_digest = _sha256(verifier)
        temporary_password = entries.get(_ROTATION_PASSWORD)
        temporary_verifier = entries.get(_ROTATION_VERIFIER)
        temporary_password_digest = (
            None if temporary_password is None else _sha256(temporary_password[0])
        )
        temporary_verifier_digest = (
            None if temporary_verifier is None else _sha256(temporary_verifier[0])
        )
        old_pair = (
            password_digest == journal["old_password_sha256"]
            and verifier_digest == journal["old_verifier_sha256"]
        )
        forward_mixed_pair = (
            password_digest == journal["new_password_sha256"]
            and verifier_digest == journal["old_verifier_sha256"]
        )
        new_pair = (
            password_digest == journal["new_password_sha256"]
            and verifier_digest == journal["new_verifier_sha256"]
        )

        if old_pair:
            password_recognized = temporary_password_digest in {
                None,
                _sha256(b""),
                journal["new_password_sha256"],
            }
            verifier_recognized = temporary_verifier_digest in {
                None,
                _sha256(b""),
                journal["new_verifier_sha256"],
            }
            if not password_recognized or not verifier_recognized:
                raise RuntimeSecretError(
                    "development administrator rotation state is invalid"
                )
            complete_payloads = (
                temporary_password_digest == journal["new_password_sha256"]
                and temporary_verifier_digest == journal["new_verifier_sha256"]
            )
            if not complete_payloads:
                _remove_rotation_transaction(
                    directory, transaction, transaction_entry, entries
                )
                return
            _replace_owned_rotation_file(
                transaction,
                temporary_password[1],
                directory,
                "admin-password",
            )
            os.fsync(directory)
            _replace_owned_rotation_file(
                transaction,
                temporary_verifier[1],
                directory,
                "admin-password-verifier",
            )
            os.fsync(directory)
        elif forward_mixed_pair:
            if (
                temporary_password is not None
                or temporary_verifier_digest != journal["new_verifier_sha256"]
            ):
                raise RuntimeSecretError(
                    "development administrator rotation state is invalid"
                )
            os.fsync(directory)
            _replace_owned_rotation_file(
                transaction,
                temporary_verifier[1],
                directory,
                "admin-password-verifier",
            )
            os.fsync(directory)
        elif new_pair:
            if temporary_password is not None or temporary_verifier is not None:
                raise RuntimeSecretError(
                    "development administrator rotation state is invalid"
                )
        else:
            raise RuntimeSecretError(
                "development administrator rotation state is invalid"
            )

        installed_password = _read_file(directory, "admin-password")
        installed_verifier = _read_file(directory, "admin-password-verifier")
        if (
            _sha256(installed_password) != journal["new_password_sha256"]
            or _sha256(installed_verifier) != journal["new_verifier_sha256"]
        ):
            raise RuntimeSecretError(
                "development administrator rotation state is invalid"
            )
        _validate_admin_credential(installed_password, installed_verifier)
        _remove_rotation_transaction(directory, transaction, transaction_entry, entries)
    finally:
        for _content, entry in entries.values():
            _close_owned_entry(entry)
        if transaction_entry is not None:
            _close_owned_entry(transaction_entry)
            transaction = -1
        elif transaction >= 0:
            os.close(transaction)


def _recover_admin_rotation(directory: int) -> None:
    names = set(os.listdir(directory))
    extras = names - RUNTIME_SECRET_NAMES
    if not extras:
        return
    if extras <= _LEGACY_ROTATION_ENTRIES:
        if _LEGACY_ROTATION_JOURNAL not in extras:
            raise RuntimeSecretError(
                "development administrator rotation state is invalid"
            )
        journal = _parse_rotation_journal(
            _read_file(directory, _LEGACY_ROTATION_JOURNAL)
        )
        _finish_legacy_admin_rotation(directory, journal)
        return
    if len(extras) != 1:
        raise RuntimeSecretError("development administrator rotation state is invalid")
    name = next(iter(extras))
    if _ROTATION_NAME.fullmatch(name) is None:
        raise RuntimeSecretError(
            "development secrets directory contains unknown entries"
        )
    try:
        _resume_admin_rotation(directory, name)
    except RuntimeSecretError:
        raise
    except OSError as error:
        raise RuntimeSecretError(
            "development administrator rotation recovery was interrupted"
        ) from error


def _rotate_admin_password(
    directory: int,
    bundle: dict[str, bytes],
    *,
    management_cidrs: tuple[str, ...],
    enroll_hostname: str,
    agent_hostname: str,
    registry_hostname: str,
) -> None:
    new_password, new_verifier = _admin_credential_pair()
    candidate = dict(bundle)
    candidate["admin-password"] = new_password
    candidate["admin-password-verifier"] = new_verifier
    _validate_bundle(
        candidate,
        management_cidrs=management_cidrs,
        enroll_hostname=enroll_hostname,
        agent_hostname=agent_hostname,
        registry_hostname=registry_hostname,
    )
    transaction = -1
    transaction_entry: _OwnedEntry | None = None
    entries: dict[str, tuple[bytes, _OwnedEntry]] = {}
    journal_durable = False
    transaction_name = _ROTATION_PREFIX + secrets.token_hex(16)
    try:
        transaction, transaction_entry = _create_owned_directory(
            directory, transaction_name
        )
        os.fsync(directory)
        journal_content = _rotation_journal(
            bundle["admin-password"],
            bundle["admin-password-verifier"],
            new_password,
            new_verifier,
        )
        journal_entry = _create_owned_file(
            transaction,
            _ROTATION_MANIFEST_TEMPORARY,
            journal_content,
        )
        entries[_ROTATION_MANIFEST_TEMPORARY] = (journal_content, journal_entry)
        os.replace(
            _ROTATION_MANIFEST_TEMPORARY,
            _ROTATION_JOURNAL,
            src_dir_fd=transaction,
            dst_dir_fd=transaction,
        )
        entries.pop(_ROTATION_MANIFEST_TEMPORARY)
        journal_entry.name = _ROTATION_JOURNAL
        entries[_ROTATION_JOURNAL] = (journal_content, journal_entry)
        os.fsync(transaction)
        journal_durable = True
        password_entry = _create_owned_file(
            transaction, _ROTATION_PASSWORD, new_password
        )
        entries[_ROTATION_PASSWORD] = (new_password, password_entry)
        os.fsync(transaction)
        verifier_entry = _create_owned_file(
            transaction, _ROTATION_VERIFIER, new_verifier
        )
        entries[_ROTATION_VERIFIER] = (new_verifier, verifier_entry)
        os.fsync(transaction)
        _resume_admin_rotation(directory, transaction_name)
        installed = {name: _read_file(directory, name) for name in RUNTIME_SECRET_NAMES}
        _validate_bundle(
            installed,
            management_cidrs=management_cidrs,
            enroll_hostname=enroll_hostname,
            agent_hostname=agent_hostname,
            registry_hostname=registry_hostname,
        )
    except Exception as error:
        if not journal_durable and transaction >= 0 and transaction_entry is not None:
            try:
                _remove_rotation_transaction(
                    directory,
                    transaction,
                    transaction_entry,
                    entries,
                )
            except (OSError, RuntimeSecretError):
                pass
        if isinstance(error, RuntimeSecretError) and not journal_durable:
            raise
        raise RuntimeSecretError(
            "development administrator rotation was interrupted; rerun to recover"
        ) from error
    finally:
        for _content, entry in entries.values():
            _close_owned_entry(entry)
        if transaction_entry is not None:
            _close_owned_entry(transaction_entry)
            transaction = -1
        elif transaction >= 0:
            os.close(transaction)


def prepare_runtime_secrets(
    secrets_dir: Path,
    *,
    management_cidrs: str,
    enroll_hostname: str,
    agent_hostname: str,
    registry_hostname: str,
    tailscale_oauth_client_id_file: Path,
    tailscale_oauth_client_secret_file: Path,
    upgrade_host_runtime_authority: bool = False,
    upgrade_browser_access: bool = False,
    rotate_admin_password: bool = False,
) -> Path:
    if (
        sum(
            (
                upgrade_host_runtime_authority,
                upgrade_browser_access,
                rotate_admin_password,
            )
        )
        > 1
    ):
        raise RuntimeSecretError("development secret operation is ambiguous")
    enroll = _validate_hostname(enroll_hostname)
    agent = _validate_hostname(agent_hostname)
    registry = _validate_hostname(registry_hostname)
    cidrs = _validate_cidrs(management_cidrs)
    tailscale_oauth_client_id = _read_external_secret(tailscale_oauth_client_id_file)
    tailscale_oauth_client_secret = _read_external_secret(
        tailscale_oauth_client_secret_file
    )
    parent = _open_private_parent(secrets_dir)
    existing_descriptor = -1
    mutation_lock = -1
    try:
        if _filesystem_type(secrets_dir) in _UNSAFE_GENERATION_FILESYSTEMS:
            raise RuntimeSecretError(
                "development secrets must be generated on local storage"
            )
        existing = _open_existing_directory(parent, secrets_dir.name)
        if existing is not None:
            existing_descriptor, existing_metadata = existing
            if os.listdir(existing_descriptor):
                mutation_lock = _acquire_mutation_lock(existing_descriptor)
            _recover_admin_rotation(existing_descriptor)
            names = set(os.listdir(existing_descriptor))
            if names:
                if upgrade_browser_access:
                    if names != _PRE_BROWSER_ACCESS_SECRET_NAMES:
                        for name in sorted(names):
                            if name not in RUNTIME_SECRET_NAMES:
                                raise RuntimeSecretError(
                                    "development secrets directory contains unknown entries"
                                )
                            _read_file(existing_descriptor, name)
                        raise RuntimeSecretError(
                            "browser access upgrade requires the exact prior generation"
                        )
                    prior_bundle = {
                        name: _read_file(existing_descriptor, name) for name in names
                    }
                    _upgrade_browser_access(
                        existing_descriptor,
                        prior_bundle,
                        tailscale_oauth_client_id=tailscale_oauth_client_id,
                        tailscale_oauth_client_secret=tailscale_oauth_client_secret,
                        management_cidrs=cidrs,
                        enroll_hostname=enroll,
                        agent_hostname=agent,
                        registry_hostname=registry,
                    )
                    return secrets_dir
                recoverable_upgrade = frozenset(names) in {
                    frozenset(_LEGACY_RUNTIME_SECRET_NAMES),
                    frozenset(_LEGACY_RUNTIME_SECRET_NAMES | {_HOST_RUNTIME_PRIVATE}),
                }
                if upgrade_host_runtime_authority and recoverable_upgrade:
                    bundle = {
                        name: _read_file(existing_descriptor, name) for name in names
                    }
                    _upgrade_host_runtime_authority(
                        existing_descriptor,
                        bundle,
                        management_cidrs=cidrs,
                        enroll_hostname=enroll,
                        agent_hostname=agent,
                        registry_hostname=registry,
                    )
                    return secrets_dir
                if names != RUNTIME_SECRET_NAMES:
                    # Validate every present entry before reporting an incomplete bundle.
                    for name in sorted(names):
                        if name not in RUNTIME_SECRET_NAMES:
                            raise RuntimeSecretError(
                                "development secrets directory contains unknown entries"
                            )
                        _read_file(existing_descriptor, name)
                    raise RuntimeSecretError("development secret bundle is incomplete")
                bundle = {name: _read_file(existing_descriptor, name) for name in names}
                _validate_bundle(
                    bundle,
                    management_cidrs=cidrs,
                    enroll_hostname=enroll,
                    agent_hostname=agent,
                    registry_hostname=registry,
                )
                if rotate_admin_password:
                    _rotate_admin_password(
                        existing_descriptor,
                        bundle,
                        management_cidrs=cidrs,
                        enroll_hostname=enroll,
                        agent_hostname=agent,
                        registry_hostname=registry,
                    )
                return secrets_dir
        else:
            existing_metadata = None
        if upgrade_browser_access or rotate_admin_password:
            raise RuntimeSecretError(
                "development secret operation requires an existing generation"
            )
        staging_name = f".{secrets_dir.name}.staging-{secrets.token_hex(16)}"
        staging_descriptor = -1
        try:
            os.mkdir(staging_name, 0o700, dir_fd=parent)
            staging_descriptor = os.open(staging_name, _DIRECTORY_FLAGS, dir_fd=parent)
            bundle = _secret_bundle(
                management_cidrs=cidrs,
                enroll_hostname=enroll,
                agent_hostname=agent,
                registry_hostname=registry,
                tailscale_oauth_client_id=tailscale_oauth_client_id,
                tailscale_oauth_client_secret=tailscale_oauth_client_secret,
            )
            _validate_bundle(
                bundle,
                management_cidrs=cidrs,
                enroll_hostname=enroll,
                agent_hostname=agent,
                registry_hostname=registry,
            )
            for name, content in sorted(bundle.items()):
                _write_file(staging_descriptor, name, content)
            for name in sorted(bundle):
                if _read_file(staging_descriptor, name) != bundle[name]:
                    raise RuntimeSecretError("development secret verification failed")
            os.fsync(staging_descriptor)
            if existing_metadata is not None:
                current = os.stat(
                    secrets_dir.name, dir_fd=parent, follow_symlinks=False
                )
                if not _same_inode(existing_metadata, current) or os.listdir(
                    existing_descriptor
                ):
                    raise RuntimeSecretError(
                        "development secrets directory changed during generation"
                    )
            os.replace(
                staging_name, secrets_dir.name, src_dir_fd=parent, dst_dir_fd=parent
            )
            os.close(staging_descriptor)
            staging_descriptor = -1
        except OSError as error:
            raise RuntimeSecretError(
                "development secret bundle cannot be installed safely"
            ) from error
        finally:
            if staging_descriptor >= 0:
                _remove_staging(parent, staging_name, staging_descriptor)
        return secrets_dir
    finally:
        if mutation_lock >= 0:
            os.close(mutation_lock)
        if existing_descriptor >= 0:
            os.close(existing_descriptor)
        os.close(parent)


def _certificate_window(
    certificate: x509.Certificate,
) -> tuple[dt.datetime, dt.datetime]:
    before = getattr(certificate, "not_valid_before_utc", None)
    after = getattr(certificate, "not_valid_after_utc", None)
    if before is None or after is None:
        before = certificate.not_valid_before.replace(tzinfo=dt.UTC)
        after = certificate.not_valid_after.replace(tzinfo=dt.UTC)
    return before.astimezone(dt.UTC), after.astimezone(dt.UTC)


def _expiry(certificate: x509.Certificate) -> str:
    return _certificate_window(certificate)[1].isoformat().replace("+00:00", "Z")


def _public_summary(secrets_dir: Path) -> tuple[str, ...]:
    try:
        agent_ca = x509.load_pem_x509_certificate(
            (secrets_dir / "agent-ca-certificate").read_bytes()
        )
        controller_ca = x509.load_pem_x509_certificate(
            (secrets_dir / "controller-ca").read_bytes()
        )
        server = x509.load_pem_x509_certificate(
            (secrets_dir / "controller-server-certificate").read_bytes()
        )
    except (OSError, ValueError) as error:
        raise RuntimeSecretError(
            "development public certificate summary cannot be produced"
        ) from error
    return (
        f"agent-ca-not-after={_expiry(agent_ca)}",
        f"agent-ca-sha256={agent_ca.fingerprint(hashes.SHA256()).hex()}",
        f"controller-ca-not-after={_expiry(controller_ca)}",
        f"controller-ca-sha256={controller_ca.fingerprint(hashes.SHA256()).hex()}",
        f"controller-server-not-after={_expiry(server)}",
    )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--secrets-dir", type=Path, default=Path(".dev/vonk-forge-secrets")
    )
    parser.add_argument("--management-cidrs", required=True)
    parser.add_argument("--enroll-hostname", required=True)
    parser.add_argument("--agent-hostname", required=True)
    parser.add_argument("--registry-hostname", required=True)
    parser.add_argument("--tailscale-oauth-client-id-file", type=Path, required=True)
    parser.add_argument(
        "--tailscale-oauth-client-secret-file", type=Path, required=True
    )
    operations = parser.add_mutually_exclusive_group()
    operations.add_argument(
        "--upgrade-host-runtime-authority",
        action="store_true",
        help=(
            "add only the host runtime signing key pair to a validated "
            "legacy development secret generation"
        ),
    )
    operations.add_argument(
        "--upgrade-browser-access",
        action="store_true",
        help="add browser access secrets to the exact prior generation",
    )
    operations.add_argument(
        "--rotate-admin-password",
        action="store_true",
        help="rotate only the administrator password and verifier",
    )
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    os.umask(0o077)
    try:
        secrets_dir = arguments.secrets_dir
        if not secrets_dir.is_absolute():
            if secrets_dir == Path(".dev/vonk-forge-secrets"):
                _ensure_default_development_directory(Path.cwd())
            secrets_dir = Path.cwd() / secrets_dir
        destination = prepare_runtime_secrets(
            secrets_dir,
            management_cidrs=arguments.management_cidrs,
            enroll_hostname=arguments.enroll_hostname,
            agent_hostname=arguments.agent_hostname,
            registry_hostname=arguments.registry_hostname,
            tailscale_oauth_client_id_file=(arguments.tailscale_oauth_client_id_file),
            tailscale_oauth_client_secret_file=(
                arguments.tailscale_oauth_client_secret_file
            ),
            upgrade_host_runtime_authority=(arguments.upgrade_host_runtime_authority),
            upgrade_browser_access=arguments.upgrade_browser_access,
            rotate_admin_password=arguments.rotate_admin_password,
        )
        print(destination)
        for line in _public_summary(destination):
            print(line)
        return 0
    except RuntimeSecretError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
