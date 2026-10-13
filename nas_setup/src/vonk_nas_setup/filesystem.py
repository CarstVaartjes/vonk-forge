"""Private, no-follow reads and durable atomic replacements."""

from __future__ import annotations

import errno
import fcntl
import os
import shutil
import stat
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from vonk_agent_protocol.installer_setup import validate_secret_name
from vonk_agent_protocol.lifecycle_vocabulary import WaitReason

MAX_MEMBER_BYTES = 256 * 1024
MAX_TEMPLATE_BYTES = 16 * 1024 * 1024


class SetupError(ValueError):
    """An invalid contract or unsafe destination, with no secret in its message."""


class ObservationUnavailable(BlockingIOError):
    def __init__(self) -> None:
        super().__init__(errno.EAGAIN, WaitReason.OBSERVATION_UNAVAILABLE.value)


def retry[T](action: Callable[[], T]) -> T:
    """Bound temporary OS/provider faults; denied access never retries."""
    for attempt in range(3):
        try:
            return action()
        except PermissionError:
            raise
        except OSError:
            if attempt == 2:
                raise
            time.sleep(0.05 * 2**attempt)
    raise AssertionError("unreachable")


def metadata(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def real_directory(path: Path) -> None:
    entry = path.lstat()
    if not stat.S_ISDIR(entry.st_mode):
        raise SetupError(f"destination is not a real directory: {path}")


def selected_path(path: Path) -> Path:
    if ".." in path.parts:
        raise SetupError("parent traversal is not allowed")
    entry = metadata(path)
    if entry and stat.S_ISLNK(entry.st_mode):
        raise SetupError(f"destination is a symbolic link: {path}")
    # A selected real directory below a symlinked ancestor is normal on macOS.
    return path.resolve()


def output_root(path: Path) -> Path:
    selected = selected_path(path)
    if metadata(selected) is None:
        selected.mkdir(parents=True, mode=0o700)
        selected.chmod(0o700)
    real_directory(selected)
    return selected


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def staging_directory(parent: Path) -> Path:
    return Path(tempfile.mkdtemp(prefix=".vonk-forge.setup-", dir=parent))


def preserve(path: Path) -> None:
    retained = staging_directory(path.parent)
    os.replace(path, retained / "preserved")
    sync_directory(path.parent)


def ensure_directory(path: Path) -> None:
    entry = metadata(path)
    if entry and not stat.S_ISDIR(entry.st_mode):
        preserve(path)
        entry = None
    if entry is None:
        path.mkdir(mode=0o700)
    path.chmod(0o700)


def nested_parent(root: Path, relative: str) -> Path:
    validate_secret_name(relative)
    real_directory(root)
    parent = root
    for part in Path(relative).parts[:-1]:
        parent /= part
        ensure_directory(parent)
    return parent


def read_member(
    root: Path, relative: str, *, limit: int = MAX_MEMBER_BYTES
) -> bytes | None:
    """An unavailable projection is absent; OS refusal remains an error.

    Directory descriptors protect intermediate components as well as the final
    member against symlink replacement while a private read is in progress.
    """
    parts = Path(relative).parts
    if not parts or any(part in ("..", "/") for part in parts):
        raise SetupError("unsafe member path")
    directory = -1
    file_descriptor = -1
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in parts[:-1]:
            next_directory = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = next_directory
        file_descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        entry = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(entry.st_mode)
            or entry.st_nlink != 1
            or entry.st_size > limit
        ):
            return None
        with os.fdopen(file_descriptor, "rb") as stream:
            file_descriptor = -1
            value = stream.read(limit + 1)
        return value if len(value) <= limit else None
    except OSError as error:
        if error.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
            return None
        raise
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        if directory >= 0:
            os.close(directory)


def atomic_replace(path: Path, content: bytes, mode: int = 0o600) -> None:
    def replace() -> None:
        original = metadata(path)
        if original and (not stat.S_ISREG(original.st_mode) or original.st_nlink != 1):
            preserve(path)
            original = None
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.tmp-", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), mode)
                if original:
                    try:
                        os.fchown(stream.fileno(), original.st_uid, original.st_gid)
                    except PermissionError:
                        pass  # Non-root keeps its own ownership; never broaden access.
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    retry(replace)


def write_member(root: Path, relative: str, content: bytes) -> None:
    parent = nested_parent(root, relative)
    atomic_replace(parent / Path(relative).name, content)


@contextmanager
def acquire_owner(root: Path) -> Iterator[None]:
    path = root / ".vonk-setup.lock"
    entry = metadata(path)
    if entry and (not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1):
        preserve(path)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        entry = os.fstat(descriptor)
        if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
            raise SetupError("unsafe publication owner file")
        retry(lambda: fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB))
        yield
    finally:
        os.close(descriptor)


def discard(path: Path) -> None:
    """Only disposable private staging is recursively removed."""
    try:
        shutil.rmtree(path)
    except OSError:
        pass
