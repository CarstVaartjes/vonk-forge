"""Workload-only signed release-lock consumption boundary."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Protocol

from vonk_agent_protocol import AgentProtocolError, PackageReleaseLock

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_LOCK_BYTES = 1024 * 1024


class WorkloadTrustError(RuntimeError):
    """Workload trust metadata or target authorization failed closed."""


@dataclass(frozen=True)
class TrustedWorkloadTarget:
    name: str
    length: int
    sha256: str
    data: bytes


class WorkloadTargetSource(Protocol):
    """Separate workload-TUF cache/root implementation supplied by transport."""

    def refresh(self) -> None: ...

    def trusted_target(self, name: str) -> TrustedWorkloadTarget: ...


class WorkloadTrust:
    """Parse only digest-addressed locks authorized by workload trust roles."""

    def __init__(self, source: WorkloadTargetSource) -> None:
        if not callable(getattr(source, "refresh", None)) or not callable(
            getattr(source, "trusted_target", None)
        ):
            raise WorkloadTrustError("workload trust source is invalid")
        self._source = source

    def refresh(self) -> None:
        try:
            self._source.refresh()
        except Exception as error:
            raise WorkloadTrustError("workload trust refresh failed") from error

    def trusted_lock(self, digest: str) -> PackageReleaseLock:
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise WorkloadTrustError("workload release digest is invalid")
        name = f"releases/{digest}.json"
        try:
            target = self._source.trusted_target(name)
        except Exception as error:
            raise WorkloadTrustError("workload target authorization failed") from error
        if (
            type(target) is not TrustedWorkloadTarget
            or target.name != name
            or isinstance(target.length, bool)
            or not isinstance(target.length, int)
            or not 0 < target.length <= _MAX_LOCK_BYTES
            or not isinstance(target.data, bytes)
            or len(target.data) != target.length
            or not isinstance(target.sha256, str)
            or target.sha256 != digest
            or hashlib.sha256(target.data).hexdigest() != digest
        ):
            raise WorkloadTrustError("workload target identity is inconsistent")
        try:
            lock = PackageReleaseLock.parse(target.data)
        except (AgentProtocolError, TypeError, ValueError) as error:
            raise WorkloadTrustError("workload target lock is invalid") from error
        if lock.canonical_bytes != target.data or lock.digest != digest:
            raise WorkloadTrustError("workload target lock digest is inconsistent")
        return lock
