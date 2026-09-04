"""Controller-authorized delivery of immutable model and OCI objects.

The service is intentionally backed by a small source protocol.  The NAS cache
worker can provide that protocol without this module knowing its persistence or
eviction details; recipe image storage can use the filesystem adapter below.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from threading import RLock
from typing import BinaryIO, Protocol

from vonk_agent_protocol import DistributionAssignment, DistributionObject


class DistributionError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class VerifiedObject:
    """A source handle that has been checked against its content address."""

    stream: BinaryIO
    size: int
    sha256: str


class VerifiedObjectSource(Protocol):
    def open_verified(self, digest: str, expected_bytes: int) -> VerifiedObject:
        """Open a complete immutable object or raise DistributionError."""


class FilesystemVerifiedObjectSource:
    """Adapter for flat content-addressed Controller/NAS object storage."""

    def __init__(self, root: Path, *, maximum_bytes: int = 16 * 1024**4) -> None:
        self.root = root
        self.maximum_bytes = maximum_bytes

    def open_verified(self, digest: str, expected_bytes: int) -> VerifiedObject:
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise DistributionError("distribution.object_invalid", "object digest is invalid")
        if not 1 <= expected_bytes <= self.maximum_bytes:
            raise DistributionError("distribution.object_invalid", "object length is invalid")
        try:
            root = self.root
            root_stat = root.lstat()
            if (
                not root.is_absolute()
                or not stat.S_ISDIR(root_stat.st_mode)
                or stat.S_ISLNK(root_stat.st_mode)
                or root_stat.st_uid not in {0, os.geteuid()}
                or root_stat.st_mode & 0o022
            ):
                raise OSError("unsafe object root")
            descriptor = os.open(os.fspath(root), os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
            try:
                fd = os.open(digest, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=descriptor)
            finally:
                os.close(descriptor)
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_mode & 0o022
                or before.st_size != expected_bytes
            ):
                os.close(fd)
                raise DistributionError("distribution.object_unavailable", "verified object length changed")
            hasher = hashlib.sha256()
            remaining = expected_bytes
            while remaining:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    os.close(fd)
                    raise DistributionError("distribution.object_unavailable", "verified object is partial")
                hasher.update(chunk)
                remaining -= len(chunk)
            after = os.fstat(fd)
            if after.st_size != before.st_size or hasher.hexdigest() != digest:
                os.close(fd)
                raise DistributionError("distribution.object_unavailable", "verified object digest mismatch")
            os.lseek(fd, 0, os.SEEK_SET)
            return VerifiedObject(os.fdopen(fd, "rb", closefd=True), expected_bytes, digest)
        except DistributionError:
            raise
        except OSError as error:
            raise DistributionError("distribution.object_unavailable", "verified object is unavailable") from error


class MemoryVerifiedObjectSource:
    """Small deterministic fixture source used by Controller integration tests."""

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects = dict(objects or {})

    def put(self, payload: bytes) -> str:
        digest = hashlib.sha256(payload).hexdigest()
        self.objects[digest] = bytes(payload)
        return digest

    def open_verified(self, digest: str, expected_bytes: int) -> VerifiedObject:
        payload = self.objects.get(digest)
        if payload is None or len(payload) != expected_bytes or hashlib.sha256(payload).hexdigest() != digest:
            raise DistributionError("distribution.object_unavailable", "verified object digest mismatch")
        return VerifiedObject(BytesIO(payload), len(payload), digest)


class DistributionService:
    """Resolves exact assignments and serves only their declared objects."""

    def __init__(self, source: VerifiedObjectSource, *, clock=lambda: datetime.now(UTC)) -> None:
        self.source = source
        self.clock = clock
        self._assignments: dict[str, DistributionAssignment] = {}
        self._lock = RLock()

    def register(self, assignment: DistributionAssignment) -> None:
        assignment = DistributionAssignment.parse(assignment.to_mapping())
        with self._lock:
            existing = self._assignments.get(assignment.plan_digest)
            if existing is not None and existing != assignment:
                raise DistributionError("distribution.assignment_conflict", "plan digest is already bound")
            self._assignments[assignment.plan_digest] = assignment

    def authorize(self, *, node_id: str, plan_digest: str) -> DistributionAssignment:
        with self._lock:
            assignment = self._assignments.get(plan_digest)
        if assignment is None:
            raise DistributionError("distribution.unassigned", "assignment is not available")
        if assignment.node_id != node_id:
            raise DistributionError("distribution.wrong_node", "assignment is bound to another node")
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now) or assignment.expires_at <= now:
            raise DistributionError("distribution.expired", "assignment has expired")
        return assignment

    def manifest(self, *, node_id: str, plan_digest: str) -> dict[str, object]:
        return self.authorize(node_id=node_id, plan_digest=plan_digest).to_mapping()

    def open_object(self, *, node_id: str, plan_digest: str, digest: str) -> tuple[DistributionAssignment, DistributionObject, VerifiedObject]:
        assignment = self.authorize(node_id=node_id, plan_digest=plan_digest)
        object_spec = next((item for item in assignment.objects if item.sha256 == digest), None)
        if object_spec is None:
            raise DistributionError("distribution.unassigned", "object is not assigned to this node")
        try:
            opened = self.source.open_verified(digest, object_spec.bytes)
        except DistributionError:
            raise
        except Exception as error:
            raise DistributionError("distribution.object_unavailable", "verified object is unavailable") from error
        if opened.size != object_spec.bytes or opened.sha256 != digest:
            opened.stream.close()
            raise DistributionError("distribution.object_unavailable", "source returned an invalid object")
        return assignment, object_spec, opened


__all__ = [
    "DistributionError",
    "DistributionService",
    "FilesystemVerifiedObjectSource",
    "MemoryVerifiedObjectSource",
    "VerifiedObject",
    "VerifiedObjectSource",
]
