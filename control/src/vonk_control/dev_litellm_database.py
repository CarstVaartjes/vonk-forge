"""One-shot development LiteLLM PostgreSQL authority initializer."""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
_ROOT_MODE = 0o550
_FILE_MODE = 0o400
_MAX_DATABASE_URL_BYTES = 4 * 1024
_PASSWORD = re.compile(r"[0-9a-f]{64}\Z")
_ROLE = "litellm"
_DATABASE = "litellm"
_FAILURE = "development LiteLLM database initialization failed"


class DevLiteLLMDatabaseError(RuntimeError):
    """The development LiteLLM database cannot be initialized safely."""


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
        raise DevLiteLLMDatabaseError(
            "development LiteLLM database secret root is invalid"
        )
    descriptor = -1
    try:
        descriptor = os.open("/", _DIRECTORY_FLAGS)
        for component in path.parts[1:]:
            listed = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            opened = os.fstat(child)
            if not _same_inode(listed, opened) or not stat.S_ISDIR(opened.st_mode):
                os.close(child)
                raise DevLiteLLMDatabaseError(
                    "development LiteLLM database secret root is unsafe"
                )
            os.close(descriptor)
            descriptor = child
        metadata = os.fstat(descriptor)
        if (
            metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or stat.S_IMODE(metadata.st_mode) != _ROOT_MODE
        ):
            raise DevLiteLLMDatabaseError(
                "development LiteLLM database secret root is unsafe"
            )
        return descriptor
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise DevLiteLLMDatabaseError(
            "development LiteLLM database secret root is unsafe"
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
        raise DevLiteLLMDatabaseError(
            "development LiteLLM database secret is unsafe"
        ) from error
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
            raise DevLiteLLMDatabaseError(
                "development LiteLLM database secret is unsafe"
            )
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
            raise DevLiteLLMDatabaseError(
                "development LiteLLM database secret changed while reading"
            )
        return bytes(content)
    except OSError as error:
        raise DevLiteLLMDatabaseError(
            "development LiteLLM database secret cannot be read"
        ) from error
    finally:
        os.close(descriptor)


def _line(content: bytes) -> str:
    try:
        value = content.decode("ascii")
    except UnicodeDecodeError as error:
        raise DevLiteLLMDatabaseError(
            "development LiteLLM database secret is invalid"
        ) from error
    stripped = value.removesuffix("\n")
    if value != stripped + "\n" or not stripped or "\n" in stripped:
        raise DevLiteLLMDatabaseError(
            "development LiteLLM database secret is invalid"
        )
    return stripped


def _inputs(root_path: Path) -> tuple[dict[str, str | int], str]:
    root = _open_secret_root(root_path)
    try:
        database_url = _line(
            _read_secret(root, "database-url", maximum=_MAX_DATABASE_URL_BYTES)
        )
        password = _line(
            _read_secret(root, "litellm-database-password", maximum=65)
        )
    finally:
        os.close(root)
    if _PASSWORD.fullmatch(password) is None:
        raise DevLiteLLMDatabaseError(
            "development LiteLLM database password is invalid"
        )
    try:
        parsed = make_url(database_url)
    except SQLAlchemyError as error:
        raise DevLiteLLMDatabaseError(
            "development LiteLLM database URL is invalid"
        ) from error
    if (
        parsed.get_backend_name() != "postgresql"
        or parsed.host != "postgres"
        or parsed.port not in {None, 5432}
        or not parsed.database
        or not parsed.username
        or not parsed.password
        or parsed.query
    ):
        raise DevLiteLLMDatabaseError(
            "development LiteLLM database URL is invalid"
        )
    return (
        {
            "host": "postgres",
            "port": 5432,
            "dbname": parsed.database,
            "user": parsed.username,
            "password": parsed.password,
        },
        password,
    )


def _reconcile(connection: Any, password: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s",
            (_DATABASE,),
        )
        owner = cursor.fetchone()
        if owner is not None and owner[0] != _ROLE:
            raise DevLiteLLMDatabaseError(
                "development LiteLLM database owner is invalid"
            )

        cursor.execute("SELECT rolname FROM pg_roles WHERE rolname = %s", (_ROLE,))
        role = cursor.fetchone()
        if role is None:
            cursor.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(_ROLE), sql.Literal(password)
                )
            )
        else:
            cursor.execute(
                sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(_ROLE), sql.Literal(password)
                )
            )

        if owner is None:
            cursor.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(_DATABASE), sql.Identifier(_ROLE)
                )
            )

        cursor.execute(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s",
            (_DATABASE,),
        )
        final_owner = cursor.fetchone()
        if final_owner != (_ROLE,):
            raise DevLiteLLMDatabaseError(
                "development LiteLLM database owner is invalid"
            )


def _run() -> None:
    root = Path(
        os.environ.get("VONK_DEV_LITELLM_DATABASE_SECRET_ROOT", "/run/secrets")
    )
    settings, password = _inputs(root)
    with psycopg.connect(**settings, autocommit=True) as connection:
        _reconcile(connection, password)


def main() -> int:
    try:
        _run()
    except (DevLiteLLMDatabaseError, OSError, psycopg.Error, SQLAlchemyError, ValueError):
        print(_FAILURE, file=sys.stderr)
        return 1
    print("development LiteLLM database initialized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
