"""Safely stage packaged development runtime assets into a dedicated volume."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

_CADDY_UID = 10000
_CADDY_GID = 10000
_LITELLM_UID = 10002
_LITELLM_GID = 10001
_TAILSCALE_UID = 0
_TAILSCALE_GID = 0


class DevelopmentAssetError(RuntimeError):
    """A packaged development asset cannot be staged safely."""


@dataclass(frozen=True)
class _Asset:
    mode: int
    uid: int
    gid: int
    maximum_bytes: int


_ASSETS = {
    "Caddyfile": _Asset(0o444, _CADDY_UID, _CADDY_GID, 128 * 1024),
    "caddy-entrypoint.sh": _Asset(0o555, _CADDY_UID, _CADDY_GID, 128 * 1024),
    "litellm-bootstrap.json": _Asset(0o444, _LITELLM_UID, _LITELLM_GID, 128 * 1024),
    "litellm-entrypoint.sh": _Asset(0o555, _LITELLM_UID, _LITELLM_GID, 128 * 1024),
    "litellm-supervisor.py": _Asset(0o555, _LITELLM_UID, _LITELLM_GID, 128 * 1024),
    "tailscale-configure.sh": _Asset(
        0o555,
        _TAILSCALE_UID,
        _TAILSCALE_GID,
        128 * 1024,
    ),
}


def _resource_path(resource: Traversable) -> Path | None:
    try:
        return Path(os.fspath(resource))
    except TypeError:
        return None


def _read_resource(resource: Traversable, *, name: str, limit: int) -> bytes:
    path = _resource_path(resource)
    if path is None:
        raise DevelopmentAssetError(f"development asset {name} is unsafe")

    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 0 < before.st_size <= limit
        ):
            raise DevelopmentAssetError(f"development asset {name} is unsafe")
        content = bytearray()
        while len(content) <= limit:
            chunk = os.read(descriptor, min(4096, limit + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if len(content) != before.st_size or before_identity != after_identity:
            raise DevelopmentAssetError(f"development asset {name} changed while read")
        return bytes(content)
    except DevelopmentAssetError:
        raise
    except OSError as error:
        raise DevelopmentAssetError(f"development asset {name} is unsafe") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _destination_components(destination: Path) -> tuple[str, ...]:
    if (
        destination.anchor != "/"
        or len(destination.parts) < 2
        or any(part in {"", ".", ".."} for part in destination.parts[1:])
    ):
        raise DevelopmentAssetError(
            "development asset destination must be absolute and normalized"
        )
    return destination.parts[1:]


def _open_destination(destination: Path) -> int:
    components = _destination_components(destination)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    current = -1
    try:
        current = os.open("/", flags)
        for component in components:
            try:
                child = os.open(component, flags, dir_fd=current)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o755, dir_fd=current)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = child
        if os.geteuid() == 0:
            os.fchown(current, 0, 0)
        os.fchmod(current, 0o755)
        return current
    except OSError as error:
        if current >= 0:
            os.close(current)
        raise DevelopmentAssetError(
            "development asset destination is unsafe"
        ) from error


def _preflight_targets(parent: int) -> None:
    try:
        for name in _ASSETS:
            try:
                metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise DevelopmentAssetError(
                    f"development asset target {name} is unsafe"
                )
    except DevelopmentAssetError:
        raise
    except OSError as error:
        raise DevelopmentAssetError("development asset target is unsafe") from error


def _effective_identity(asset: _Asset) -> tuple[int, int]:
    if os.geteuid() == 0:
        return asset.uid, asset.gid
    return os.geteuid(), os.getegid()


def _target_matches(parent: int, name: str, content: bytes, asset: _Asset) -> bool:
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent,
        )
    except FileNotFoundError:
        return False
    except OSError as error:
        raise DevelopmentAssetError(
            f"development asset target {name} is unsafe"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        uid, gid = _effective_identity(asset)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != asset.mode
            or (metadata.st_uid, metadata.st_gid) != (uid, gid)
            or metadata.st_size != len(content)
        ):
            return False
        existing = bytearray()
        while len(existing) <= len(content):
            chunk = os.read(descriptor, min(4096, len(content) + 1 - len(existing)))
            if not chunk:
                break
            existing.extend(chunk)
        after = os.fstat(descriptor)
        return bytes(existing) == content and (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mtime_ns,
        ) == (after.st_dev, after.st_ino, after.st_mtime_ns)
    except OSError as error:
        raise DevelopmentAssetError(
            f"development asset target {name} is unsafe"
        ) from error
    finally:
        os.close(descriptor)


def _stage_asset(
    parent: int,
    name: str,
    content: bytes,
    digest: str,
    asset: _Asset,
) -> None:
    temporary = f".{name}.{secrets.token_hex(12)}.new"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent,
        )
        staged_digest = hashlib.sha256()
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise DevelopmentAssetError("development asset write was incomplete")
            staged_digest.update(content[offset : offset + written])
            offset += written
        if staged_digest.hexdigest() != digest:
            raise DevelopmentAssetError("development asset digest changed while staged")
        uid, gid = _effective_identity(asset)
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, asset.mode)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    except DevelopmentAssetError:
        raise
    except OSError as error:
        raise DevelopmentAssetError(
            f"development asset {name} cannot be staged"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass


def stage_development_assets(source_package: str, destination: Path) -> None:
    """Atomically replace the allowlisted development runtime assets."""
    try:
        package = resources.files(source_package)
    except (ImportError, ModuleNotFoundError, TypeError) as error:
        raise DevelopmentAssetError(
            "development asset package is unavailable"
        ) from error
    staged: dict[str, tuple[bytes, str]] = {}
    for name, asset in _ASSETS.items():
        content = _read_resource(
            package.joinpath(name),
            name=name,
            limit=asset.maximum_bytes,
        )
        staged[name] = (content, hashlib.sha256(content).hexdigest())

    parent = _open_destination(Path(destination))
    try:
        _preflight_targets(parent)
        for name, asset in _ASSETS.items():
            content, digest = staged[name]
            if not _target_matches(parent, name, content, asset):
                _stage_asset(parent, name, content, digest, asset)
    finally:
        os.close(parent)
