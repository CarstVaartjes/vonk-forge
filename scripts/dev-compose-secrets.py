#!/usr/bin/env python3
"""Prepare local development secrets without following mutable pathnames."""

from __future__ import annotations

import argparse
import errno
import importlib.util
import os
import secrets
import stat
import subprocess
import sys
from pathlib import Path

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_READ_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_MAX_SECRET_BYTES = 64 * 1024
_PRIVATE_MODE = 0o600


class SecretPreparationError(RuntimeError):
    """The local development secret store is unsafe."""


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _validate_directory(descriptor: int, *, label: str) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise SecretPreparationError(f"{label} ownership or mode is unsafe")
    return metadata


def _open_child_directory(parent: int, name: str, *, label: str) -> int:
    try:
        try:
            listed = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            os.mkdir(name, 0o700, dir_fd=parent)
            listed = os.stat(name, dir_fd=parent, follow_symlinks=False)
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    except OSError as error:
        raise SecretPreparationError(f"{label} cannot be opened safely") from error
    try:
        opened = os.fstat(descriptor)
        if not _same_inode(listed, opened):
            raise SecretPreparationError(f"{label} changed while opening")
        _validate_directory(descriptor, label=label)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_development_directory(repository_root: Path) -> int:
    repository = -1
    try:
        repository = os.open(repository_root, _DIRECTORY_FLAGS)
        return _open_child_directory(
            repository,
            ".dev",
            label="development directory",
        )
    except OSError as error:
        raise SecretPreparationError(
            "development repository cannot be opened safely"
        ) from error
    finally:
        if repository >= 0:
            os.close(repository)


def _open_external_secret_directory(path: Path) -> int:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        listed = path.lstat()
        descriptor = os.open(path, _DIRECTORY_FLAGS)
    except OSError as error:
        raise SecretPreparationError(
            "explicit development secrets directory cannot be opened safely"
        ) from error
    try:
        opened = os.fstat(descriptor)
        if not _same_inode(listed, opened):
            raise SecretPreparationError(
                "explicit development secrets directory changed while opening"
            )
        _validate_directory(
            descriptor,
            label="explicit development secrets directory",
        )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_existing(
    directory: int,
    name: str,
    *,
    allowed_modes: frozenset[int] = frozenset({_PRIVATE_MODE}),
) -> bytes | None:
    try:
        listed = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise SecretPreparationError(f"development secret {name} is unsafe") from error
    if (
        not stat.S_ISREG(listed.st_mode)
        or listed.st_nlink != 1
        or listed.st_uid != os.geteuid()
        or stat.S_IMODE(listed.st_mode) not in allowed_modes
        or not 0 < listed.st_size <= _MAX_SECRET_BYTES
    ):
        raise SecretPreparationError(f"development secret {name} is unsafe")
    try:
        descriptor = os.open(name, _FILE_READ_FLAGS, dir_fd=directory)
    except OSError as error:
        raise SecretPreparationError(f"development secret {name} is unsafe") from error
    try:
        before = os.fstat(descriptor)
        if not _same_inode(listed, before) or not stat.S_ISREG(before.st_mode):
            raise SecretPreparationError(
                f"development secret {name} changed while opening"
            )
        content = bytearray()
        while len(content) <= _MAX_SECRET_BYTES:
            chunk = os.read(
                descriptor,
                min(4096, _MAX_SECRET_BYTES + 1 - len(content)),
            )
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
        updated_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_nlink,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if len(content) != before.st_size or identity != updated_identity:
            raise SecretPreparationError(
                f"development secret {name} changed while reading"
            )
        return bytes(content)
    finally:
        os.close(descriptor)


def _unlink_if_same(directory: int, name: str, identity: tuple[int, int]) -> None:
    try:
        metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (metadata.st_dev, metadata.st_ino) == identity:
            os.unlink(name, dir_fd=directory)
    except FileNotFoundError:
        pass


def _create_file(directory: int, name: str, content: bytes) -> None:
    descriptor = -1
    identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(
            name,
            _FILE_WRITE_FLAGS,
            _PRIVATE_MODE,
            dir_fd=directory,
        )
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError(errno.EIO, "short development secret write")
            view = view[written:]
        os.fchmod(descriptor, _PRIVATE_MODE)
        os.fsync(descriptor)
    except OSError as error:
        if identity is not None:
            _unlink_if_same(directory, name, identity)
        raise SecretPreparationError(
            f"development secret {name} cannot be created"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _remove_temporary_directory(parent: int, name: str, descriptor: int) -> None:
    pinned = os.fstat(descriptor)
    try:
        for child in os.listdir(descriptor):
            os.unlink(child, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    try:
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not _same_inode(pinned, current):
        return
    try:
        os.rmdir(name, dir_fd=parent)
    except FileNotFoundError:
        pass


def _generate_signing_key(directory: int) -> None:
    temporary = f".git-signing-key.{secrets.token_hex(16)}"
    temporary_descriptor = -1
    try:
        os.mkdir(temporary, 0o700, dir_fd=directory)
        listed = os.stat(
            temporary,
            dir_fd=directory,
            follow_symlinks=False,
        )
        temporary_descriptor = os.open(
            temporary,
            _DIRECTORY_FLAGS,
            dir_fd=directory,
        )
        opened = os.fstat(temporary_descriptor)
        if (
            not stat.S_ISDIR(listed.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or not _same_inode(listed, opened)
        ):
            raise SecretPreparationError(
                "development signing-key staging changed while opening"
            )
    except SecretPreparationError:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        raise
    except OSError as error:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        raise SecretPreparationError(
            "development signing-key staging cannot be created"
        ) from error
    linked: list[tuple[str, tuple[int, int]]] = []

    def rollback_links() -> None:
        for name, identity in reversed(linked):
            _unlink_if_same(directory, name, identity)

    try:
        key_path = f"/proc/self/fd/{temporary_descriptor}/key"
        try:
            result = subprocess.run(
                (
                    "ssh-keygen",
                    "-q",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-C",
                    "vonk-forge-dev",
                    "-f",
                    key_path,
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=30,
                pass_fds=(temporary_descriptor,),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SecretPreparationError(
                "development signing key cannot be generated"
            ) from error
        if result.returncode != 0:
            raise SecretPreparationError("development signing key cannot be generated")
        private = _read_existing(temporary_descriptor, "key")
        public = _read_existing(
            temporary_descriptor,
            "key.pub",
            allowed_modes=frozenset({0o600, 0o644}),
        )
        if private is None or public is None:
            raise SecretPreparationError("development signing key is incomplete")
        for source, destination in (
            ("key", "git-signing-key"),
            ("key.pub", "git-signing-key.pub"),
        ):
            os.link(
                source,
                destination,
                src_dir_fd=temporary_descriptor,
                dst_dir_fd=directory,
                follow_symlinks=False,
            )
            metadata = os.stat(
                destination,
                dir_fd=directory,
                follow_symlinks=False,
            )
            linked.append((destination, (metadata.st_dev, metadata.st_ino)))
    except OSError as error:
        rollback_links()
        raise SecretPreparationError(
            "development signing key cannot be installed safely"
        ) from error
    except BaseException:
        rollback_links()
        raise
    finally:
        try:
            _remove_temporary_directory(
                directory,
                temporary,
                temporary_descriptor,
            )
        except BaseException:
            rollback_links()
            raise
    try:
        for name, _identity in linked:
            descriptor = os.open(name, _FILE_READ_FLAGS, dir_fd=directory)
            try:
                os.fchmod(descriptor, _PRIVATE_MODE)
            finally:
                os.close(descriptor)
            if _read_existing(directory, name) is None:
                raise SecretPreparationError(f"development secret {name} is missing")
    except BaseException:
        rollback_links()
        raise


def _prepare_secret_files(directory: int) -> None:
    existing = {
        "postgres-password": _read_existing(directory, "postgres-password"),
        "database-url": _read_existing(directory, "database-url"),
        "git-signing-key": _read_existing(directory, "git-signing-key"),
        "git-signing-key.pub": _read_existing(
            directory,
            "git-signing-key.pub",
            allowed_modes=frozenset({0o600, 0o644}),
        ),
    }
    if (existing["git-signing-key"] is None) != (
        existing["git-signing-key.pub"] is None
    ):
        raise SecretPreparationError("development signing key pair is incomplete")
    password = existing["postgres-password"]
    if password is None:
        password = (secrets.token_hex(24) + "\n").encode("ascii")
        _create_file(directory, "postgres-password", password)
    if existing["database-url"] is None:
        database_url = (
            b"postgresql+psycopg://control:"
            + password.strip()
            + b"@postgres:5432/control\n"
        )
        _create_file(directory, "database-url", database_url)
    if existing["git-signing-key"] is None:
        _generate_signing_key(directory)


def _physical_directory_path(descriptor: int) -> str:
    try:
        target = os.readlink(f"/proc/self/fd/{descriptor}")
    except OSError as error:
        raise SecretPreparationError(
            "development secrets directory has no physical path"
        ) from error
    if not target.startswith("/") or target.endswith(" (deleted)"):
        raise SecretPreparationError(
            "development secrets directory was unlinked during preparation"
        )
    verification = -1
    try:
        verification = os.open(target, _DIRECTORY_FLAGS)
        if not _same_inode(os.fstat(descriptor), os.fstat(verification)):
            raise SecretPreparationError(
                "development secrets directory path changed during preparation"
            )
    except OSError as error:
        raise SecretPreparationError(
            "development secrets directory path is unsafe"
        ) from error
    finally:
        if verification >= 0:
            os.close(verification)
    return target


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--external-secrets-dir", type=Path)
    parser.add_argument("--management-cidrs", default="127.0.0.1/32")
    parser.add_argument("--tailscale-oauth-client-id-file", type=Path, required=True)
    parser.add_argument(
        "--tailscale-oauth-client-secret-file", type=Path, required=True
    )
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    os.umask(0o077)
    development = -1
    try:
        development = _open_development_directory(arguments.repository_root)
        if arguments.external_secrets_dir is None:
            secrets_path = Path(_physical_directory_path(development)) / (
                "vonk-forge-secrets"
            )
        else:
            secrets_path = arguments.external_secrets_dir.resolve(strict=False)
        helper = Path(__file__).with_name("dev-runtime-secrets.py")
        specification = importlib.util.spec_from_file_location(
            "vonk_dev_runtime_secrets", helper
        )
        if specification is None or specification.loader is None:
            raise SecretPreparationError(
                "development runtime secret generator is unavailable"
            )
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        try:
            destination = module.prepare_runtime_secrets(
                secrets_path,
                management_cidrs=arguments.management_cidrs,
                enroll_hostname="enroll.vonk-forge.lan",
                agent_hostname="agents.vonk-forge.lan",
                registry_hostname="registry.vonk-forge.lan",
                tailscale_oauth_client_id_file=(
                    arguments.tailscale_oauth_client_id_file
                ),
                tailscale_oauth_client_secret_file=(
                    arguments.tailscale_oauth_client_secret_file
                ),
            )
        except module.RuntimeSecretError as error:
            raise SecretPreparationError(str(error)) from error
        print(destination)
        return 0
    except (SecretPreparationError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        if development >= 0:
            os.close(development)


if __name__ == "__main__":
    raise SystemExit(main())
