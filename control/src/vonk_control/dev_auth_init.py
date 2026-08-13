"""One-shot development administrator bootstrap and rotation."""

from __future__ import annotations

import os
import secrets
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from .browser_auth import BrowserAuthenticationError, BrowserAuthService
from .db import build_engine, session_factory

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
_ROOT_MODE = 0o550
_FILE_MODE = 0o400
_MAX_DATABASE_URL_BYTES = 4 * 1024
_MAX_VERIFIER_BYTES = 512
_FAILURE = "development authentication initialization failed"


class DevAuthInitError(RuntimeError):
    """Development browser authentication cannot be initialized safely."""


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _open_secret_root(path: Path) -> int:
    if (
        os.name != "posix"
        or path.anchor != "/"
        or len(path.parts) < 2
        or "\\" in str(path)
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        raise DevAuthInitError("development authentication secret root is invalid")
    descriptor = -1
    try:
        descriptor = os.open("/", _DIRECTORY_FLAGS)
        for component in path.parts[1:]:
            listed = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            opened = os.fstat(child)
            if not _same_inode(listed, opened) or not stat.S_ISDIR(opened.st_mode):
                os.close(child)
                raise DevAuthInitError(
                    "development authentication secret root is unsafe"
                )
            os.close(descriptor)
            descriptor = child
        metadata = os.fstat(descriptor)
        if (
            metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or stat.S_IMODE(metadata.st_mode) != _ROOT_MODE
        ):
            raise DevAuthInitError("development authentication secret root is unsafe")
        return descriptor
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise DevAuthInitError(
            "development authentication secret root is unsafe"
        ) from error
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise


def _read_secret(root: int, name: str, *, maximum: int) -> bytes:
    try:
        listed = os.stat(name, dir_fd=root, follow_symlinks=False)
        descriptor = os.open(name, _READ_FLAGS, dir_fd=root)
    except OSError as error:
        raise DevAuthInitError("development authentication secret is unsafe") from error
    try:
        before = os.fstat(descriptor)
        if (
            not _same_inode(listed, before)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_gid != os.getegid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != _FILE_MODE
            or not 0 < before.st_size <= maximum
        ):
            raise DevAuthInitError("development authentication secret is unsafe")
        content = bytearray()
        while len(content) <= maximum:
            chunk = os.read(descriptor, min(4096, maximum + 1 - len(content)))
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
            raise DevAuthInitError(
                "development authentication secret changed while reading"
            )
        return bytes(content)
    except OSError as error:
        raise DevAuthInitError(
            "development authentication secret cannot be read"
        ) from error
    finally:
        os.close(descriptor)


def _line(content: bytes, *, encoding: str) -> str:
    try:
        value = content.decode(encoding)
    except UnicodeDecodeError as error:
        raise DevAuthInitError(
            "development authentication secret is invalid"
        ) from error
    stripped = value.removesuffix("\n")
    if (
        value != stripped + "\n"
        or not stripped
        or "\n" in stripped
        or "\x00" in stripped
    ):
        raise DevAuthInitError("development authentication secret is invalid")
    return stripped


def _inputs(root_path: Path) -> tuple[str, str]:
    root = _open_secret_root(root_path)
    try:
        database_url = _read_secret(
            root, "database-url", maximum=_MAX_DATABASE_URL_BYTES
        )
        verifier = _read_secret(
            root,
            "admin-password-verifier",
            maximum=_MAX_VERIFIER_BYTES,
        )
    finally:
        os.close(root)
    return _line(database_url, encoding="utf-8"), _line(verifier, encoding="ascii")


def _run() -> str:
    mode = os.environ.get("VONK_DEV_AUTH_MODE")
    if mode not in {"bootstrap", "reconcile", "rotate"}:
        raise DevAuthInitError("development authentication mode is invalid")
    root = Path(os.environ.get("VONK_DEV_AUTH_SECRET_ROOT", "/auth-secrets"))
    database_url, verifier = _inputs(root)
    engine = build_engine(database_url)
    try:
        service = BrowserAuthService(
            session_factory(engine),
            token_signing_key=secrets.token_bytes(32),
            clock=lambda: datetime.now(UTC),
        )
        if mode == "bootstrap":
            return service.bootstrap_admin(verifier).status
        if mode == "rotate":
            return service.rotate_admin(verifier).status
        try:
            return service.rotate_admin(verifier).status
        except BrowserAuthenticationError:
            return service.bootstrap_admin(verifier).status
    finally:
        engine.dispose()


def main() -> int:
    try:
        status = _run()
    except (
        BrowserAuthenticationError,
        DevAuthInitError,
        OSError,
        SQLAlchemyError,
        ValueError,
    ):
        print(_FAILURE, file=sys.stderr)
        return 1
    print(status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
