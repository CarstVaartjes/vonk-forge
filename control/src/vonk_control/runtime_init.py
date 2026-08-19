"""Root-only initialization of private control-container runtime material."""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

_MAX_PRIVATE_KEY_BYTES = 16 * 1024
_DESTINATION_NAME = "admin-grant-private-key.pem"


class RuntimeSecretError(RuntimeError):
    """A private runtime secret cannot be projected safely."""


def stage_private_key(
    source: Path,
    destination: Path,
    *,
    owner_uid: int = 0,
    owner_gid: int = 0,
    mode: int = 0o444,
) -> Path:
    """Copy a file-backed private key into a Docker-managed volume atomically."""
    source = Path(source)
    destination = Path(destination)
    try:
        descriptor = os.open(
            source,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError as error:
        raise RuntimeSecretError("private key source is unsafe") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 0 < before.st_size <= _MAX_PRIVATE_KEY_BYTES
        ):
            raise RuntimeSecretError("private key source is unsafe")
        content = bytearray()
        while len(content) <= _MAX_PRIVATE_KEY_BYTES:
            chunk = os.read(
                descriptor,
                min(4096, _MAX_PRIVATE_KEY_BYTES + 1 - len(content)),
            )
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        if len(content) != before.st_size or _identity(before) != _identity(after):
            raise RuntimeSecretError("private key changed while read")
    except OSError as error:
        raise RuntimeSecretError("private key cannot be read") from error
    finally:
        os.close(descriptor)

    parent = destination.parent
    parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    if os.geteuid() == 0:
        os.chown(parent, 0, 10001)
    os.chmod(parent, 0o750)
    temporary = parent / f".{destination.name}.{secrets.token_hex(12)}.new"
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            mode,
        )
        try:
            os.fchown(descriptor, owner_uid, owner_gid)
            os.fchmod(descriptor, mode)
            offset = 0
            while offset < len(content):
                offset += os.write(descriptor, content[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, destination)
        return destination
    except OSError as error:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise RuntimeSecretError("private key staging failed") from error


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _snapshot_source(path: Path, *, source_uid: int) -> bytes:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError as error:
        raise RuntimeSecretError("admin grant private key source is unsafe") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != source_uid
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o444
            or not 0 < before.st_size <= _MAX_PRIVATE_KEY_BYTES
        ):
            raise RuntimeSecretError("admin grant private key source is unsafe")
        content = bytearray()
        while len(content) <= _MAX_PRIVATE_KEY_BYTES:
            chunk = os.read(
                descriptor,
                min(4096, _MAX_PRIVATE_KEY_BYTES + 1 - len(content)),
            )
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        if len(content) != before.st_size or _identity(before) != _identity(after):
            raise RuntimeSecretError("admin grant private key changed while read")
        return bytes(content)
    except OSError as error:
        raise RuntimeSecretError("admin grant private key cannot be read") from error
    finally:
        os.close(descriptor)


def _validate_existing(parent: int, *, api_uid: int, api_gid: int) -> None:
    try:
        descriptor = os.open(
            _DESTINATION_NAME,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent,
        )
    except FileNotFoundError:
        return
    except OSError as error:
        raise RuntimeSecretError("admin grant runtime key is unsafe") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != api_uid
            or metadata.st_gid != api_gid
            or stat.S_IMODE(metadata.st_mode) != 0o400
            or not 0 < metadata.st_size <= _MAX_PRIVATE_KEY_BYTES
        ):
            raise RuntimeSecretError("admin grant runtime key is unsafe")
    finally:
        os.close(descriptor)


def install_admin_grant_key(
    source: Path,
    runtime_root: Path,
    *,
    source_uid: int = 0,
    api_uid: int = 10001,
    api_gid: int = 10001,
) -> Path:
    """Atomically project one root-owned 0444 key as API-only 0400 state."""
    source = Path(source)
    runtime_root = Path(runtime_root)
    if (
        not source.is_absolute()
        or not runtime_root.is_absolute()
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (source_uid, api_uid, api_gid)
        )
    ):
        raise RuntimeSecretError("admin grant runtime projection is invalid")
    content = _snapshot_source(source, source_uid=source_uid)
    try:
        parent = os.open(
            runtime_root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError as error:
        raise RuntimeSecretError("admin grant runtime directory is unsafe") from error
    temporary = f".{_DESTINATION_NAME}.{secrets.token_hex(12)}.new"
    descriptor = -1
    try:
        directory = os.fstat(parent)
        if not stat.S_ISDIR(directory.st_mode) or directory.st_uid not in {
            0,
            os.geteuid(),
        }:
            raise RuntimeSecretError("admin grant runtime directory is unsafe")
        os.fchown(parent, 0 if os.geteuid() == 0 else os.geteuid(), api_gid)
        os.fchmod(parent, 0o710)
        _validate_existing(parent, api_uid=api_uid, api_gid=api_gid)
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | os.O_CLOEXEC,
            0o600,
            dir_fd=parent,
        )
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise RuntimeSecretError("admin grant runtime key write was incomplete")
            offset += written
        os.fchown(descriptor, api_uid, api_gid)
        os.fchmod(descriptor, 0o400)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary,
            _DESTINATION_NAME,
            src_dir_fd=parent,
            dst_dir_fd=parent,
        )
        os.fsync(parent)
    except RuntimeSecretError:
        raise
    except OSError as error:
        raise RuntimeSecretError("admin grant runtime key cannot be installed") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)
    return runtime_root / _DESTINATION_NAME


def main() -> None:
    install_admin_grant_key(
        Path("/run/secrets/admin-grant-private-key"),
        Path("/runtime"),
    )


if __name__ == "__main__":
    main()
