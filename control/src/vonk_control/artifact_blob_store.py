"""Atomic filesystem CAS for large artifact-job inputs and outputs."""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
import time
import uuid
from collections.abc import AsyncIterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class ArtifactBlobStoreError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StoredArtifactBlob:
    sha256: str
    size_bytes: int
    storage_key: str
    path: Path


@dataclass(slots=True)
class _BlobReservation:
    token: str
    path: Path
    descriptor: int
    size_bytes: int


class ArtifactBlobStore:
    def __init__(self, root: Path, *, max_stored_bytes: int = 16 * 1024**3) -> None:
        if not root.is_absolute() or max_stored_bytes < 1:
            raise ValueError("artifact blob store configuration is invalid")
        self._root = root
        self._max_stored_bytes = max_stored_bytes

    @property
    def max_stored_bytes(self) -> int:
        return self._max_stored_bytes

    def usage(self) -> dict[str, int]:
        self._prepare_root()
        with self._quota_lock():
            reservations = self._reservation_entries()
            reserved = sum(reservations.values())
            used = self._stored_bytes() + self._unreserved_temporary_bytes(
                set(reservations)
            )
            return {
                "max_stored_bytes": self._max_stored_bytes,
                "used_bytes": used,
                "reserved_bytes": reserved,
                "in_flight_uploads": len(reservations),
                "remaining_bytes": max(0, self._max_stored_bytes - used - reserved),
            }

    @contextmanager
    def reference_attachment(self) -> Iterator[None]:
        """Fence one blob verification plus its durable database attachment."""
        with self._reference_lock(exclusive=False):
            yield

    @contextmanager
    def reference_reconciliation(self) -> Iterator[None]:
        """Exclude new database attachments while references are reconciled."""
        with self._reference_lock(exclusive=True):
            yield

    async def put_stream(
        self,
        expected_sha256: str,
        chunks: AsyncIterable[bytes],
        *,
        expected_bytes: int,
        maximum_bytes: int,
    ) -> StoredArtifactBlob:
        self._digest(expected_sha256)
        if not 0 <= expected_bytes <= maximum_bytes:
            raise ArtifactBlobStoreError("artifact upload size is outside its bound")
        self._prepare_root()
        reservation, existing = self._reserve(expected_sha256, expected_bytes)
        if existing is not None:
            digest = hashlib.sha256()
            observed = 0
            async for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise ArtifactBlobStoreError("artifact upload chunk is invalid")
                observed += len(chunk)
                if observed > expected_bytes:
                    raise ArtifactBlobStoreError(
                        "artifact upload exceeds its declared size"
                    )
                digest.update(chunk)
            if observed != expected_bytes:
                raise ArtifactBlobStoreError("artifact upload size does not match")
            if digest.hexdigest() != expected_sha256:
                raise ArtifactBlobStoreError("artifact upload SHA-256 does not match")
            return existing
        assert reservation is not None
        temporary = Path()
        digest = hashlib.sha256()
        observed = 0
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{expected_sha256}.{reservation.token}.",
                suffix=".part",
                dir=self._root / ".tmp",
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as stream:
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise ArtifactBlobStoreError("artifact upload chunk is invalid")
                    observed += len(chunk)
                    if observed > maximum_bytes or observed > expected_bytes:
                        raise ArtifactBlobStoreError(
                            "artifact upload exceeds its declared size"
                        )
                    digest.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if observed != expected_bytes:
                raise ArtifactBlobStoreError("artifact upload size does not match")
            if digest.hexdigest() != expected_sha256:
                raise ArtifactBlobStoreError("artifact upload SHA-256 does not match")
            stored = self._commit(temporary, expected_sha256, observed)
            temporary = Path()
            return stored
        finally:
            if temporary != Path() and temporary.exists():
                temporary.unlink()
            self._release_reservation(reservation)

    def put_bytes(
        self,
        expected_sha256: str,
        content: bytes,
        *,
        maximum_bytes: int,
    ) -> StoredArtifactBlob:
        self._digest(expected_sha256)
        if len(content) > maximum_bytes:
            raise ArtifactBlobStoreError("artifact bytes exceed the limit")
        if hashlib.sha256(content).hexdigest() != expected_sha256:
            raise ArtifactBlobStoreError("artifact upload SHA-256 does not match")
        self._prepare_root()
        reservation, existing = self._reserve(expected_sha256, len(content))
        if existing is not None:
            return existing
        assert reservation is not None
        temporary = Path()
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{expected_sha256}.{reservation.token}.",
                suffix=".part",
                dir=self._root / ".tmp",
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            stored = self._commit(temporary, expected_sha256, len(content))
            temporary = Path()
            return stored
        finally:
            if temporary != Path() and temporary.exists():
                temporary.unlink()
            self._release_reservation(reservation)

    def resolve(self, storage_key: str, sha256: str, size_bytes: int) -> Path:
        self._digest(sha256)
        expected_key = f"{sha256[:2]}/{sha256}"
        if storage_key != expected_key:
            raise ArtifactBlobStoreError("artifact storage key is invalid")
        path = self._root / sha256[:2] / sha256
        if path.is_symlink() or not path.is_file() or path.stat().st_size != size_bytes:
            raise ArtifactBlobStoreError("stored artifact is unavailable")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024**2), b""):
                digest.update(chunk)
        if digest.hexdigest() != sha256:
            raise ArtifactBlobStoreError("stored artifact digest is inconsistent")
        return path

    @staticmethod
    def iter_file(path: Path, *, chunk_bytes: int = 1024**2) -> Iterator[bytes]:
        with path.open("rb") as stream:
            while chunk := stream.read(chunk_bytes):
                yield chunk

    def delete(self, storage_key: str, sha256: str) -> None:
        expected_key = f"{sha256[:2]}/{sha256}"
        self._digest(sha256)
        if storage_key != expected_key:
            raise ArtifactBlobStoreError("artifact storage key is invalid")
        path = self._root / storage_key
        if path.is_symlink():
            raise ArtifactBlobStoreError("artifact storage path is unsafe")
        if path.exists():
            path.unlink()

    def reconcile(
        self,
        referenced_sha256: set[str],
        *,
        batch_limit: int = 1000,
        orphan_grace_seconds: int = 300,
        _reference_fenced: bool = False,
    ) -> dict[str, object]:
        """Remove abandoned temporary/orphan bytes and report referenced gaps."""
        if not _reference_fenced:
            with self.reference_reconciliation():
                return self.reconcile(
                    referenced_sha256,
                    batch_limit=batch_limit,
                    orphan_grace_seconds=orphan_grace_seconds,
                    _reference_fenced=True,
                )
        if not 1 <= batch_limit <= 10_000:
            raise ValueError("artifact reconciliation batch limit is invalid")
        if not 0 <= orphan_grace_seconds <= 3600:
            raise ValueError("artifact reconciliation orphan grace is invalid")
        for digest in referenced_sha256:
            self._digest(digest)
        self._prepare_root()
        removed_temporary = 0
        removed_reservations = 0
        removed_orphans = 0
        remaining_work = False
        remaining_budget = batch_limit
        missing = set(referenced_sha256)
        with self._quota_lock():
            active_reservations: set[str] = set()
            reservations_root = self._root / ".reservations"
            for path in reservations_root.iterdir():
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or not path.name.endswith(".reserve")
                ):
                    continue
                descriptor = os.open(
                    path,
                    os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        active_reservations.add(path.name.removesuffix(".reserve"))
                        continue
                    if remaining_budget == 0:
                        remaining_work = True
                        continue
                    path.unlink()
                    removed_reservations += 1
                    remaining_budget -= 1
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
            temporary_root = self._root / ".tmp"
            for path in temporary_root.iterdir():
                if path.is_file() and not path.is_symlink():
                    token = self._temporary_token(path)
                    if token in active_reservations:
                        continue
                    if remaining_budget == 0:
                        remaining_work = True
                        continue
                    path.unlink()
                    removed_temporary += 1
                    remaining_budget -= 1
            for directory in self._root.iterdir():
                if (
                    len(directory.name) != 2
                    or any(
                        character not in "0123456789abcdef"
                        for character in directory.name
                    )
                    or directory.is_symlink()
                    or not directory.is_dir()
                ):
                    continue
                for path in directory.iterdir():
                    if path.is_symlink() or not path.is_file():
                        continue
                    digest = path.name
                    if digest in referenced_sha256:
                        missing.discard(digest)
                    else:
                        if time.time() - path.stat().st_mtime < orphan_grace_seconds:
                            remaining_work = True
                            continue
                        if remaining_budget == 0:
                            remaining_work = True
                            continue
                        path.unlink()
                        removed_orphans += 1
                        remaining_budget -= 1
        return {
            "removed_temporary_files": removed_temporary,
            "removed_reservation_files": removed_reservations,
            "removed_orphan_blobs": removed_orphans,
            "missing_referenced_blobs": sorted(missing),
            "remaining_work": remaining_work,
            **self.usage(),
        }

    @contextmanager
    def _reference_lock(self, *, exclusive: bool) -> Iterator[None]:
        self._prepare_root()
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self._root / ".references.lock", flags, 0o600)
        try:
            fcntl.flock(
                descriptor,
                fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
            )
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _commit(
        self, temporary: Path, sha256: str, size_bytes: int
    ) -> StoredArtifactBlob:
        directory = self._root / sha256[:2]
        directory.mkdir(mode=0o700, exist_ok=True)
        destination = directory / sha256
        storage_key = f"{sha256[:2]}/{sha256}"
        with self._quota_lock():
            if destination.exists():
                resolved = self.resolve(storage_key, sha256, size_bytes)
                return StoredArtifactBlob(sha256, size_bytes, storage_key, resolved)
            reservations = self._reservation_entries()
            accounted = (
                self._stored_bytes()
                + sum(reservations.values())
                + self._unreserved_temporary_bytes(set(reservations))
            )
            if accounted > self._max_stored_bytes:
                raise ArtifactBlobStoreError("artifact storage quota is exhausted")
            os.replace(temporary, destination)
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return StoredArtifactBlob(sha256, size_bytes, storage_key, destination)

    def _prepare_root(self) -> None:
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._root.is_symlink() or not self._root.is_dir():
            raise ArtifactBlobStoreError("artifact storage root is unsafe")
        for name in (".tmp", ".reservations"):
            path = self._root / name
            path.mkdir(mode=0o700, exist_ok=True)
            if path.is_symlink() or not path.is_dir():
                raise ArtifactBlobStoreError("artifact storage metadata path is unsafe")

    def _stored_bytes(self) -> int:
        total = 0
        for directory in self._root.iterdir():
            if (
                directory.name in {".tmp", ".reservations"}
                or directory.is_symlink()
                or not directory.is_dir()
            ):
                continue
            for path in directory.iterdir():
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
        return total

    def _reserve(
        self, sha256: str, size_bytes: int
    ) -> tuple[_BlobReservation | None, StoredArtifactBlob | None]:
        storage_key = f"{sha256[:2]}/{sha256}"
        destination = self._root / storage_key
        with self._quota_lock():
            if destination.exists():
                return None, StoredArtifactBlob(
                    sha256,
                    size_bytes,
                    storage_key,
                    self.resolve(storage_key, sha256, size_bytes),
                )
            reservations = self._reservation_entries()
            accounted = (
                self._stored_bytes()
                + sum(reservations.values())
                + self._unreserved_temporary_bytes(set(reservations))
            )
            if accounted + size_bytes > self._max_stored_bytes:
                raise ArtifactBlobStoreError("artifact storage quota is exhausted")
            token = uuid.uuid4().hex
            path = self._root / ".reservations" / f"{token}.reserve"
            descriptor = os.open(
                path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                os.write(descriptor, str(size_bytes).encode("ascii"))
                os.fsync(descriptor)
            except BaseException:
                os.close(descriptor)
                path.unlink(missing_ok=True)
                raise
            return _BlobReservation(token, path, descriptor, size_bytes), None

    def _release_reservation(self, reservation: _BlobReservation) -> None:
        try:
            with self._quota_lock():
                reservation.path.unlink(missing_ok=True)
        finally:
            fcntl.flock(reservation.descriptor, fcntl.LOCK_UN)
            os.close(reservation.descriptor)

    def _reservation_entries(self) -> dict[str, int]:
        values: dict[str, int] = {}
        for path in (self._root / ".reservations").iterdir():
            if (
                path.is_symlink()
                or not path.is_file()
                or not path.name.endswith(".reserve")
            ):
                continue
            try:
                value = int(path.read_text(encoding="ascii"))
            except (OSError, UnicodeError, ValueError) as error:
                raise ArtifactBlobStoreError(
                    "artifact storage reservation is invalid"
                ) from error
            if not 0 <= value <= self._max_stored_bytes:
                raise ArtifactBlobStoreError("artifact storage reservation is invalid")
            values[path.name.removesuffix(".reserve")] = value
        return values

    def _unreserved_temporary_bytes(self, reservations: set[str]) -> int:
        return sum(
            path.stat().st_size
            for path in (self._root / ".tmp").iterdir()
            if path.is_file()
            and not path.is_symlink()
            and self._temporary_token(path) not in reservations
        )

    @staticmethod
    def _temporary_token(path: Path) -> str | None:
        parts = path.name.split(".")
        if len(parts) >= 5 and len(parts[2]) == 32:
            return parts[2]
        return None

    def _quota_lock(self):
        lock_path = self._root / ".quota.lock"
        stream = lock_path.open("a+b")

        class _Lock:
            def __enter__(self_nonlocal):
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                return stream

            def __exit__(self_nonlocal, _kind, _value, _traceback):
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                stream.close()

        return _Lock()

    @staticmethod
    def _digest(value: str) -> None:
        if (
            len(value) != 64
            or value != value.lower()
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ArtifactBlobStoreError("artifact digest is invalid")


__all__ = [
    "ArtifactBlobStore",
    "ArtifactBlobStoreError",
    "StoredArtifactBlob",
]
