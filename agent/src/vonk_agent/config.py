"""Strict, descriptor-verified configuration for the outbound agent."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

DEFAULT_CONFIG_PATH = Path("/etc/vonk-forge-agent/config.json")
DEFAULT_STATE_ROOT = Path("/var/lib/vonk-forge-agent")
DEFAULT_CA_PATH = Path("/etc/vonk-forge-agent/controller-ca.pem")
MAX_CONFIG_BYTES = MAX_IDENTITY_BYTES = 64 * 1024
_NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_DNS = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*\Z"
)
_FIELDS = {
    "control_origin",
    "enrollment_origin",
    "node_id",
    "certificate_path",
    "private_key_path",
    "ca_path",
    "ca_fingerprint",
    "poll_min_seconds",
    "poll_max_seconds",
    "state_root",
    "installed_policy_path",
    "runtime_policy_path",
    "enrollment_token_path",
}


class AgentConfigError(ValueError):
    """Configuration is invalid or unsafe to consume."""


@dataclass(frozen=True)
class AgentConfig:
    control_origin: str
    enrollment_origin: str
    node_id: str
    certificate_path: Path
    private_key_path: Path
    ca_path: Path
    poll_min_seconds: int
    poll_max_seconds: int
    state_root: Path
    installed_policy_path: Path
    runtime_policy_path: Path
    enrollment_token_path: Path
    ca_fingerprint: str = ""

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_PATH) -> AgentConfig:
        try:
            document = json.loads(
                _read(
                    Path(path), "configuration", MAX_CONFIG_BYTES, private=False
                ).decode("utf-8"),
                object_pairs_hook=_unique,
            )
        except UnicodeDecodeError as error:
            raise AgentConfigError("configuration must be UTF-8") from error
        except json.JSONDecodeError as error:
            raise AgentConfigError("configuration must be valid JSON") from error
        if not isinstance(document, dict) or set(document) != _FIELDS:
            raise AgentConfigError("configuration fields are invalid")
        paths = {
            name: _path(document[name], name)
            for name in (
                "certificate_path",
                "private_key_path",
                "ca_path",
                "installed_policy_path",
                "runtime_policy_path",
                "enrollment_token_path",
                "state_root",
            )
        }
        for name in ("ca_path", "installed_policy_path", "runtime_policy_path"):
            _read(paths[name], name, MAX_IDENTITY_BYTES, private=False)
        certificate_present = _read_optional(
            paths["certificate_path"],
            "certificate_path",
            MAX_IDENTITY_BYTES,
            private=False,
        )
        try:
            key_present = _read_optional(
                paths["private_key_path"],
                "private key",
                MAX_IDENTITY_BYTES,
                private=True,
            )
        except AgentConfigError as error:
            if "large" in str(error):
                raise
            raise AgentConfigError("private key is unsafe") from error
        if certificate_present != key_present:
            raise AgentConfigError("certificate and private key must be paired")
        _read_optional(
            paths["enrollment_token_path"],
            "enrollment token",
            MAX_IDENTITY_BYTES,
            private=True,
        )
        _check_state_path(paths["state_root"])
        node_id = document["node_id"]
        if not isinstance(node_id, str) or not _NODE_ID.fullmatch(node_id):
            raise AgentConfigError("node ID is not canonical")
        ca_fingerprint = document["ca_fingerprint"]
        if not isinstance(ca_fingerprint, str) or not _FINGERPRINT.fullmatch(
            ca_fingerprint
        ):
            raise AgentConfigError("CA fingerprint is not canonical")
        minimum, maximum = _poll(
            document["poll_min_seconds"], document["poll_max_seconds"]
        )
        return cls(
            _origin(document["control_origin"]),
            _origin(document["enrollment_origin"]),
            node_id,
            paths["certificate_path"],
            paths["private_key_path"],
            paths["ca_path"],
            minimum,
            maximum,
            paths["state_root"],
            paths["installed_policy_path"],
            paths["runtime_policy_path"],
            paths["enrollment_token_path"],
            ca_fingerprint,
        )


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AgentConfigError("configuration contains duplicate fields")
        result[key] = value
    return result


def _path(value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise AgentConfigError(f"{name} path must be absolute")
    pure = PurePosixPath(value)
    if (
        not pure.is_absolute()
        or str(pure) != value
        or any(component in {"", ".", ".."} for component in pure.parts[1:])
    ):
        raise AgentConfigError(f"{name} path must be canonical")
    return Path(value)


def _trusted(metadata: os.stat_result, *, private: bool, directory: bool) -> None:
    if metadata.st_uid not in {os.geteuid(), 0}:
        raise AgentConfigError("path ownership is untrusted")
    mode = stat.S_IMODE(metadata.st_mode)
    if directory and mode & stat.S_ISVTX:
        return
    if mode & (0o077 if private else 0o022):
        raise AgentConfigError("path permissions are unsafe")


def _parent(path: Path) -> tuple[int, str]:
    if not path.is_absolute() or len(path.parts) < 2:
        raise AgentConfigError("path must be absolute")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in path.parts[1:-1]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
            _trusted(os.fstat(descriptor), private=False, directory=True)
        return descriptor, path.name
    except (OSError, AgentConfigError) as error:
        os.close(descriptor)
        if isinstance(error, AgentConfigError):
            raise
        raise AgentConfigError("path must not traverse symlinks") from error


def _read(path: Path, name: str, limit: int, *, private: bool) -> bytes:
    parent, leaf = _parent(path)
    try:
        descriptor = os.open(
            leaf,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent,
        )
    except OSError as error:
        os.close(parent)
        raise AgentConfigError(f"{name} cannot be read") from error
    try:
        metadata = os.fstat(descriptor)
        _trusted(metadata, private=private, directory=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise AgentConfigError(f"{name} must be a regular file")
        if metadata.st_size > limit:
            raise AgentConfigError(f"{name} is too large")
        data = os.read(descriptor, limit + 1)
        if len(data) > limit:
            raise AgentConfigError(f"{name} is too large")
        return data
    except OSError as error:
        raise AgentConfigError(f"{name} cannot be read") from error
    finally:
        os.close(descriptor)
        os.close(parent)


def _read_optional(path: Path, name: str, limit: int, *, private: bool) -> bool:
    parent, leaf = _parent(path)
    try:
        try:
            descriptor = os.open(
                leaf,
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent,
            )
        except FileNotFoundError:
            return False
        except OSError as error:
            raise AgentConfigError(f"{name} cannot be read") from error
        else:
            os.close(descriptor)
    finally:
        os.close(parent)
    _read(path, name, limit, private=private)
    return True


def _check_state_path(path: Path) -> None:
    parent, leaf = _parent(path)
    try:
        try:
            descriptor = os.open(
                leaf,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent,
            )
        except FileNotFoundError:
            return
        try:
            _trusted(os.fstat(descriptor), private=True, directory=True)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise AgentConfigError("state root is unsafe") from error
    finally:
        os.close(parent)


def _origin(value: Any) -> str:
    if (
        not isinstance(value, str)
        or any(ord(character) <= 32 for character in value)
        or any(character in value for character in "\\?#")
    ):
        raise AgentConfigError("control origin is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise AgentConfigError("control origin is invalid") from error
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.path
        or port == 0
    ):
        raise AgentConfigError("control origin is invalid")
    host = parsed.hostname
    if not host:
        raise AgentConfigError("control origin is invalid")
    try:
        parsed_ip = ipaddress.ip_address(host)
        rendered = (
            f"[{parsed_ip.compressed}]" if parsed_ip.version == 6 else str(parsed_ip)
        )
    except ValueError:
        numeric_alias = re.fullmatch(
            r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*",
            host,
        )
        if numeric_alias:
            raise AgentConfigError("control origin uses an ambiguous numeric host")
        if not _DNS.fullmatch(host):
            raise AgentConfigError("control origin is invalid")
        rendered = host
    canonical = "https://" + rendered + ("" if port is None else f":{port}")
    if value != canonical:
        raise AgentConfigError("control origin is not canonical")
    return canonical


def _poll(minimum: Any, maximum: Any) -> tuple[int, int]:
    if (
        not isinstance(minimum, int)
        or isinstance(minimum, bool)
        or not isinstance(maximum, int)
        or isinstance(maximum, bool)
        or not 1 <= minimum <= maximum <= 300
    ):
        raise AgentConfigError("poll bounds are invalid")
    return minimum, maximum
