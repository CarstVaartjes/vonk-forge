#!/usr/bin/env python3
"""Create the complete development runtime secret bundle on local storage."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import ipaddress
import os
import re
import secrets
import stat
import sys
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_MAX_SECRET_BYTES = 64 * 1024
_PRIVATE_MODE = 0o600
_UNSAFE_GENERATION_FILESYSTEMS = frozenset({"9p", "cifs", "drvfs", "smb3"})
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")

RUNTIME_SECRET_NAMES = frozenset(
    {
        "agent-ca-certificate",
        "agent-ca-key",
        "agent-proxy-auth",
        "controller-ca",
        "controller-server-certificate",
        "controller-server-key",
        "database-url",
        "git-signing-key",
        "git-signing-key.pub",
        "litellm-master-key",
        "litellm-upstream-key",
        "management-cidrs",
        "postgres-password",
    }
)


class RuntimeSecretError(RuntimeError):
    """The requested development secret operation is unsafe or invalid."""


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
) -> dict[str, bytes]:
    agent_key = Ed25519PrivateKey.generate()
    controller_key = Ed25519PrivateKey.generate()
    server_key = Ed25519PrivateKey.generate()
    signing_key = Ed25519PrivateKey.generate()
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
        "agent-ca-certificate": agent_ca.public_bytes(serialization.Encoding.PEM),
        "agent-ca-key": private_pem(agent_key),
        "agent-proxy-auth": token(),
        "controller-ca": controller_ca.public_bytes(serialization.Encoding.PEM),
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
        "litellm-master-key": token(),
        "litellm-upstream-key": token(),
        "management-cidrs": ("\n".join(management_cidrs) + "\n").encode("ascii"),
        "postgres-password": (password + "\n").encode("ascii"),
    }


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
        server_key = serialization.load_pem_private_key(
            bundle["controller-server-key"], password=None
        )
        signing_key = serialization.load_ssh_private_key(
            bundle["git-signing-key"], password=None
        )
        signing_public = serialization.load_ssh_public_key(
            bundle["git-signing-key.pub"]
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
        or not isinstance(server_key, Ed25519PrivateKey)
        or not isinstance(signing_key, Ed25519PrivateKey)
        or not isinstance(signing_public, Ed25519PublicKey)
        or not isinstance(agent_public, Ed25519PublicKey)
        or not isinstance(controller_public, Ed25519PublicKey)
        or not isinstance(server_public, Ed25519PublicKey)
        or agent_public.public_bytes_raw() != agent_key.public_key().public_bytes_raw()
        or server_public.public_bytes_raw()
        != server_key.public_key().public_bytes_raw()
        or signing_key.public_key().public_bytes_raw()
        != signing_public.public_bytes_raw()
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
    if (
        agent_ca.subject != expected_agent_subject
        or agent_ca.issuer != agent_ca.subject
        or controller_ca.subject != expected_controller_subject
        or controller_ca.issuer != controller_ca.subject
        or server.subject != expected_server_subject
        or agent_basic != x509.BasicConstraints(ca=True, path_length=0)
        or controller_basic != x509.BasicConstraints(ca=True, path_length=0)
        or server_basic != x509.BasicConstraints(ca=False, path_length=None)
        or not agent_usage.key_cert_sign
        or not agent_usage.crl_sign
        or not controller_usage.key_cert_sign
        or not controller_usage.crl_sign
        or server_usage.key_cert_sign
        or server_usage.crl_sign
        or not server_usage.digital_signature
        or set(server_extended_usage) != {ExtendedKeyUsageOID.SERVER_AUTH}
        or set(sans.get_values_for_type(x509.DNSName))
        != {enroll_hostname, agent_hostname, registry_hostname}
        or not agent_ca.not_valid_before_utc <= now <= agent_ca.not_valid_after_utc
        or not controller_ca.not_valid_before_utc
        <= now
        <= controller_ca.not_valid_after_utc
        or not server.not_valid_before_utc <= now <= server.not_valid_after_utc
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
    for name in ("agent-proxy-auth", "litellm-master-key", "litellm-upstream-key"):
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


def prepare_runtime_secrets(
    secrets_dir: Path,
    *,
    management_cidrs: str,
    enroll_hostname: str,
    agent_hostname: str,
    registry_hostname: str,
) -> Path:
    enroll = _validate_hostname(enroll_hostname)
    agent = _validate_hostname(agent_hostname)
    registry = _validate_hostname(registry_hostname)
    cidrs = _validate_cidrs(management_cidrs)
    parent = _open_private_parent(secrets_dir)
    existing_descriptor = -1
    try:
        if _filesystem_type(secrets_dir) in _UNSAFE_GENERATION_FILESYSTEMS:
            raise RuntimeSecretError(
                "development secrets must be generated on local storage"
            )
        existing = _open_existing_directory(parent, secrets_dir.name)
        if existing is not None:
            existing_descriptor, existing_metadata = existing
            names = set(os.listdir(existing_descriptor))
            if names:
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
                return secrets_dir
        else:
            existing_metadata = None
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
        if existing_descriptor >= 0:
            os.close(existing_descriptor)
        os.close(parent)


def _expiry(certificate: x509.Certificate) -> str:
    return certificate.not_valid_after_utc.isoformat().replace("+00:00", "Z")


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
