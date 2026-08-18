"""Secure, command-driven Spark enrollment bootstrap."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID

_NODE = re.compile(r"spk_[0-9a-f]{32}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_FLAGS = {"--token", "--node-id", "--controller-endpoint", "--enrollment-endpoint", "--ca-fingerprint", "--config", "--state-root", "--ca-path", "--installer"}


class BootstrapError(RuntimeError):
    """Bootstrap input, trust, transport, or persistence failed closed."""


@dataclass(frozen=True)
class BootstrapArguments:
    token: str
    node_id: str
    controller_endpoint: str
    enrollment_endpoint: str
    ca_fingerprint: str
    config_path: Path
    state_root: Path
    ca_path: Path
    installer_path: Path | None = None


@dataclass(frozen=True)
class BootstrapResponse:
    status: str
    expires_at: str | None = None


@dataclass(frozen=True)
class BootstrapResult:
    response: BootstrapResponse
    csr: bytes


class Submitter(Protocol):
    def __call__(self, token: str, node_id: str, csr: bytes, ca_fingerprint: str) -> BootstrapResponse: ...


def _path(value: str, label: str) -> Path:
    pure = PurePosixPath(value)
    if not pure.is_absolute() or str(pure) != value or any(part in {"", ".", ".."} for part in pure.parts[1:]):
        raise BootstrapError(f"{label} path is unsafe")
    return Path(value)


def _origin(value: str, label: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname or parsed.path not in {"", "/"}:
        raise BootstrapError(f"{label} endpoint is invalid")
    return value.rstrip("/")


def parse_bootstrap_args(argv: list[str]) -> BootstrapArguments:
    seen: set[str] = set()
    for item in argv:
        if item in _FLAGS:
            if item in seen:
                raise BootstrapError(f"duplicate argument: {item}")
            seen.add(item)
    parser = argparse.ArgumentParser(add_help=False)
    for flag in _FLAGS:
        parser.add_argument(flag, required=flag not in {"--installer"})
    try:
        ns = parser.parse_args(argv)
    except SystemExit as error:
        raise BootstrapError("bootstrap arguments are invalid") from error
    if _TOKEN.fullmatch(ns.token) is None:
        raise BootstrapError("token is invalid")
    if _NODE.fullmatch(ns.node_id) is None:
        raise BootstrapError("node ID is invalid")
    if _DIGEST.fullmatch(ns.ca_fingerprint) is None:
        raise BootstrapError("CA fingerprint is invalid")
    return BootstrapArguments(
        ns.token, ns.node_id,
        _origin(ns.controller_endpoint, "controller"),
        _origin(ns.enrollment_endpoint, "enrollment"),
        ns.ca_fingerprint,
        _path(ns.config, "configuration"),
        _path(ns.state_root, "state root"),
        _path(ns.ca_path, "CA"),
        None if ns.installer is None else _path(ns.installer, "installer"),
    )


def _reject_symlink_ancestors(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(metadata.st_mode):
            raise BootstrapError("bootstrap path has symlinked ancestor")


def _secure_directory(path: Path) -> None:
    _reject_symlink_ancestors(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _reject_symlink_ancestors(path)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise BootstrapError("bootstrap directory is unsafe")


def _read_ca(path: Path, fingerprint: str) -> None:
    _reject_symlink_ancestors(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise BootstrapError("CA path is unsafe")
            certificate = x509.load_pem_x509_certificate(handle.read())
    except BootstrapError:
        raise
    except (OSError, ValueError) as error:
        raise BootstrapError("CA certificate cannot be read") from error
    if certificate.fingerprint(hashes.SHA256()).hex() != fingerprint:
        raise BootstrapError("CA fingerprint does not match certificate")


def _atomic(path: Path, content: bytes, mode: int) -> None:
    _secure_directory(path.parent)
    temporary = path.with_name(f".{path.name}.new-{os.urandom(8).hex()}")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except (OSError, ValueError) as error:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise BootstrapError("bootstrap write failed") from error

def bootstrap(arguments: BootstrapArguments, *, token_path: Path, submit: Submitter, verify_installer: Callable[[Path], None]) -> BootstrapResult:
    if arguments.installer_path is not None:
        try:
            verify_installer(arguments.installer_path)
        except Exception as error:
            raise BootstrapError("signed installer validation failed") from error
    _read_ca(arguments.ca_path, arguments.ca_fingerprint)
    token_path = _path(str(token_path), "token")
    _reject_symlink_ancestors(token_path)
    directory_fd = token_fd = -1
    try:
        directory_fd = os.open(token_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        token_fd = os.open(token_path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        metadata = os.fstat(token_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise BootstrapError("token path is unsafe")
        token = os.read(token_fd, 65537).decode("ascii").strip()
        if token != arguments.token or _TOKEN.fullmatch(token) is None:
            raise BootstrapError("token does not match bootstrap command")
        csr = _make_material(arguments.node_id, arguments.state_root / "credentials")
        try:
            response = submit(token, arguments.node_id, csr, arguments.ca_fingerprint)
        except TypeError:
            response = submit(token, arguments.node_id, csr)
        except Exception as error:
            raise BootstrapError("registration submission failed") from error
        if not isinstance(response, BootstrapResponse) or response.status not in {"pending", "approved"}:
            raise BootstrapError("registration response failed")
        if response.expires_at is not None:
            try:
                if datetime.fromisoformat(response.expires_at).astimezone(UTC) <= datetime.now(UTC):
                    raise BootstrapError("registration response expired")
            except ValueError as error:
                raise BootstrapError("registration response expiry is invalid") from error
        config = {"control_origin": arguments.controller_endpoint, "enrollment_origin": arguments.enrollment_endpoint, "node_id": arguments.node_id, "certificate_path": "/etc/vonk-forge-agent/certificate.pem", "private_key_path": str(arguments.state_root / "credentials" / "pending-key.pem"), "ca_path": str(arguments.ca_path), "ca_fingerprint": arguments.ca_fingerprint, "poll_min_seconds": 5, "poll_max_seconds": 60, "state_root": str(arguments.state_root), "installed_policy_path": "/etc/vonk-forge-agent/runtime-policy.json", "runtime_policy_path": "/etc/vonk-forge-agent/runtime-policy.json", "enrollment_token_path": str(token_path)}
        _atomic(arguments.config_path, json.dumps(config, sort_keys=True, separators=(",", ":")).encode(), 0o600)
        current = os.stat(token_path.name, dir_fd=directory_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise BootstrapError("token changed before consumption")
        os.unlink(token_path.name, dir_fd=directory_fd)
        return BootstrapResult(response, csr)
    except BootstrapError:
        raise
    except (OSError, UnicodeError) as error:
        raise BootstrapError("token cannot be read") from error
    finally:
        if token_fd >= 0:
            os.close(token_fd)
        if directory_fd >= 0:
            os.close(directory_fd)

def main(argv: list[str] | None = None) -> int:
    parse_bootstrap_args(argv or [])
    return 0
