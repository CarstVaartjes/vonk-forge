"""Split transfer."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

from vonk_agent_protocol import (
    ModelCacheCode,
    ModelFileState,
    OperatorActionName,
    ProgressPhase,
)

from ..bounded_retry import bounded_attempts
from ..categorized_faults import OperationInterrupted
from ..model_cache_contract import ModelCacheRepairPayload
from ..models import ModelCacheOperation
from ..worker_memory_contract import WorkerMemoryComponent
from . import constants
from .artifacts import ArtifactSpec
from .constants import _CHUNK_BYTES, _TRANSFER_CLAIM_SECONDS
from .errors import ModelCacheStorageRefused, ModelCacheStorageUnknown
from .persistence import _operation_cancellation

if TYPE_CHECKING:
    from .service import ModelCacheService


class SplitTransferMixin:
    """Split transfer behavior of the cache service."""

    def _download_split(
        self,
        spec: ArtifactSpec,
        set_digest: str,
        *,
        operation_id: str,
        force: bool,
        interrupt_after_bytes: int | None,
    ) -> None:
        """Ingest a file the source hosts only as ordered parts.

        Each part is fetched with the ordinary resumable transfer, then appended
        to one retained temp file while both the part's digest (verified at
        ingress) and the whole file's digest are computed from those same bytes,
        and the part is deleted. The temp file is therefore the only checkpoint:
        its length says which parts are in, a restart cuts it back to a part
        boundary and re-reads that prefix once to recover the digest state, and a
        part that fails its digest is discarded and fetched again. The finished
        file is checked against the declared whole digest, then renamed into the
        object store, so peak extra disk is one part beyond the final file.
        """
        cache = cast("ModelCacheService", self)

        parts = spec.parts
        if parts is None:
            raise ModelCacheStorageUnknown(
                ModelCacheCode.SOURCE_UNAVAILABLE,
                "split artifact metadata is unavailable",
                recovery=OperatorActionName.RESUME,
            )
        owner = cache._partial_owner(operation_id, set_digest)
        assembled = cache._partial_path(owner, spec.sha256)
        assembled.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        if assembled.is_symlink() or (assembled.exists() and not assembled.is_file()):
            assembled.unlink()
        boundaries = [0]
        for part in parts:
            boundaries.append(boundaries[-1] + part.expected_bytes)
        size = assembled.stat().st_size if assembled.exists() else 0
        if size > spec.expected_bytes:
            assembled.unlink()
            size = 0
        # Resume from the last whole part already appended: a crash can leave a
        # torn tail, which is cut off (its part is still on disk or refetched).
        done = max(index for index, edge in enumerate(boundaries) if edge <= size)
        if size != boundaries[done]:
            with assembled.open("r+b") as torn:
                torn.truncate(boundaries[done])
                os.fsync(torn.fileno())
        whole = hashlib.sha256()
        if done and not cache._rehash_prefix(assembled, boundaries[done], whole):
            assembled.unlink(missing_ok=True)
            done = 0
            whole = hashlib.sha256()
        later = {part.sha256 for part in parts[done:]}
        for index in range(done):
            if parts[index].sha256 not in later:
                cache._partial_path(owner, parts[index].sha256).unlink(missing_ok=True)
        for index in range(done, len(parts)):
            part_spec = spec.part_spec(index)
            cache._download_artifact(
                part_spec,
                set_digest,
                operation_id=operation_id,
                completed_artifacts=0,
                force=force,
                interrupt_after_bytes=interrupt_after_bytes,
                fetch_only=True,
            )
            cache._append_part(
                part_spec,
                cache._partial_path(owner, part_spec.sha256),
                assembled,
                whole,
                boundaries[index],
                operation_id=operation_id,
                set_digest=set_digest,
            )
        cache._require_transfer_running(operation_id)
        if (
            assembled.stat().st_size != spec.expected_bytes
            or whole.hexdigest() != spec.sha256
        ):
            assembled.unlink(missing_ok=True)
            cache._checkpoint_artifact(
                spec,
                operation_id=operation_id,
                set_digest=set_digest,
                actual_bytes=0,
                state=ModelFileState.CORRUPT,
            )
            raise ModelCacheStorageRefused(
                ModelCacheCode.DIGEST_MISMATCH,
                "assembled artifact failed SHA-256 verification; the bytes were discarded and the download restarts",
                recovery="resume",
            )
        if cache._operation_cancellation_pending(operation_id):
            raise OperationInterrupted("model download cancelled during assembly")
        with cache._lock:
            if not cache._publication_allowed(operation_id, set_digest, spec.sha256):
                raise OperationInterrupted("model download was removed during assembly")
            cache._place_object(spec, assembled)
            cache._mark_artifact_verified(spec, set_digest)

    @staticmethod
    def _rehash_prefix(path: Path, length: int, digest: hashlib._Hash) -> bool:
        remaining = length
        with path.open("rb") as retained:
            while remaining:
                chunk = retained.read(min(_CHUNK_BYTES, remaining))
                if not chunk:
                    return False
                digest.update(chunk)
                remaining -= len(chunk)
        return True

    def _append_part(
        self,
        part_spec: ArtifactSpec,
        part: Path,
        assembled: Path,
        whole: hashlib._Hash,
        boundary: int,
        *,
        operation_id: str,
        set_digest: str,
    ) -> None:
        """Append one fetched part, verifying its size and digest in the same pass."""
        cache = cast("ModelCacheService", self)

        if part.is_symlink() or not part.is_file():
            raise ModelCacheStorageUnknown(
                ModelCacheCode.SOURCE_TRUNCATED,
                "a downloaded part is missing; it is fetched again",
                recovery="resume",
            )
        if part.stat().st_size != part_spec.expected_bytes:
            part.unlink(missing_ok=True)
            raise ModelCacheStorageUnknown(
                ModelCacheCode.SOURCE_SIZE_MISMATCH,
                "a downloaded part does not have its pinned size; it is fetched again",
                recovery="resume",
            )
        cache._checkpoint_artifact(
            part_spec,
            operation_id=operation_id,
            set_digest=set_digest,
            actual_bytes=part_spec.expected_bytes,
            state=ProgressPhase.VERIFYING,
        )
        stop = cache._transfer_stop(operation_id)
        digest = hashlib.sha256()
        with part.open("rb") as source, assembled.open("ab") as output:
            remaining = part_spec.expected_bytes
            while remaining:
                chunk = source.read(min(_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                if stop.is_set() or cache._closed.is_set():
                    raise OperationInterrupted(
                        "model download stopped; partial files preserved"
                    )
                digest.update(chunk)
                whole.update(chunk)
                output.write(chunk)
            oversized = bool(source.read(1))
            output.flush()
            os.fsync(output.fileno())
        if remaining or oversized or digest.hexdigest() != part_spec.sha256:
            # Cut the bad bytes back off; the next attempt recovers the whole
            # file's digest state from the retained prefix and refetches the part.
            with assembled.open("r+b") as retained:
                retained.truncate(boundary)
                os.fsync(retained.fileno())
            part.unlink(missing_ok=True)
            raise ModelCacheStorageRefused(
                ModelCacheCode.DIGEST_MISMATCH,
                "downloaded part failed SHA-256 verification; the bytes were discarded and the part restarts",
                recovery="resume",
            )
        # The part's bytes are durably in the assembled file: free the space now.
        part.unlink(missing_ok=True)

    def memory_footprint(self) -> dict[WorkerMemoryComponent, int]:
        """Entry counts of the service's long-lived collections, for the worker report."""
        cache = cast("ModelCacheService", self)

        return {
            WorkerMemoryComponent.MODEL_CACHE_BACKGROUND_OPERATIONS: len(
                cache._background_operations
            ),
            WorkerMemoryComponent.MODEL_CACHE_CANCEL_EVENTS: len(cache._cancel_events),
            WorkerMemoryComponent.MODEL_CACHE_PROGRESS_CHECKPOINTS: len(
                cache._progress_checkpoint_at
            ),
            WorkerMemoryComponent.MODEL_CACHE_REVERIFIED: len(cache._reverified_at),
        }

    def _transfer_stop(self, operation_id: str) -> threading.Event:
        cache = cast("ModelCacheService", self)
        with cache._lock:
            return cache._cancel_events.setdefault(operation_id, threading.Event())

    def _operation_cancellation_pending(self, operation_id: str) -> bool:
        cache = cast("ModelCacheService", self)
        with cache._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            return bool(
                operation is not None
                and operation.state != "cancelled"
                and _operation_cancellation(operation) is not None
            )

    @contextmanager
    def _sample_transfer(
        self,
        spec: ArtifactSpec,
        set_digest: str,
        operation_id: str,
        completed_artifacts: int,
        initial_bytes: int,
    ):
        """Sample transfer counters independently of socket reads and disk writes."""
        cache = cast("ModelCacheService", self)
        stopped = threading.Event()
        latest = [initial_bytes]
        errors: list[Exception] = []
        cancel = cache._transfer_stop(operation_id)

        def sample() -> None:
            # A byte of progress renews the idle budget. The immutable size
            # bounds total progress; a stalled source returns to durable retry.
            deadline = time.monotonic() + _TRANSFER_CLAIM_SECONDS
            observed = initial_bytes
            while not stopped.wait(timeout=1):
                if latest[0] > observed:
                    observed = latest[0]
                    deadline = time.monotonic() + _TRANSFER_CLAIM_SECONDS
                if time.monotonic() >= deadline:
                    errors.append(
                        ModelCacheStorageUnknown(
                            ModelCacheCode.SOURCE_UNAVAILABLE,
                            "artifact transfer progress deadline expired",
                            recovery="resume",
                        )
                    )
                    cancel.set()
                    return
                if cache._closed.is_set():
                    cancel.set()
                    return
                try:
                    cache._checkpoint_artifact(
                        spec,
                        operation_id=operation_id,
                        set_digest=set_digest,
                        actual_bytes=latest[0],
                        state=ModelFileState.PARTIAL,
                        completed_artifacts=completed_artifacts,
                    )
                except Exception as error:  # noqa: BLE001 - propagate sampler failures to owner
                    errors.append(error)
                    cancel.set()
                    return

        def observe_once(count: int) -> None:
            latest[0] = count
            if errors:
                raise errors[0]
            if cancel.is_set() or cache._closed.is_set():
                raise OperationInterrupted(
                    "model download stopped; partial files preserved"
                )

        def observe(count: int) -> None:
            ended: OperationInterrupted | None = None
            for _attempt in bounded_attempts():
                try:
                    observe_once(count)
                    return
                except OperationInterrupted as error:
                    ended = error
                    break  # cancellation/shutdown is a known end, never repeated
            if ended is not None:
                raise ended
            raise ModelCacheStorageUnknown(
                ModelCacheCode.SOURCE_UNAVAILABLE,
                "artifact progress observation is unavailable",
                recovery=OperatorActionName.RESUME,
            )

        thread = threading.Thread(
            target=sample, name="vonk-model-progress", daemon=True
        )
        thread.start()
        try:
            observe(initial_bytes)
            yield observe
        finally:
            stopped.set()
            thread.join(timeout=_TRANSFER_CLAIM_SECONDS)
            if thread.is_alive():
                errors.append(
                    ModelCacheStorageUnknown(
                        ModelCacheCode.SOURCE_UNAVAILABLE,
                        "artifact progress sampler shutdown deadline expired",
                        recovery="resume",
                    )
                )
            if errors:
                raise errors[0]

    def _partial_owner(self, operation_id: str, set_digest: str) -> str:
        """The partials directory an operation's retained bytes live under."""
        cache = cast("ModelCacheService", self)

        with cache._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is None or operation.kind != "repair":
                return set_digest
            payload = cache._payload_or_none(operation)
            if not isinstance(payload, ModelCacheRepairPayload):
                return set_digest
            return "repair-" + payload.repair_checkpoint.transfer_id

    def _download_artifact(
        self,
        spec: ArtifactSpec,
        set_digest: str,
        *,
        operation_id: str,
        completed_artifacts: int,
        force: bool,
        interrupt_after_bytes: int | None,
        fetch_only: bool = False,
    ) -> None:
        """Fetch one file into its partial and publish it as a cache object.

        ``fetch_only`` (one part of a split file) stops once the retained file
        holds every byte: a part is verified where it is appended to the file it
        belongs to, and is never a cache object.
        """
        cache = cast("ModelCacheService", self)
        partial_owner = cache._partial_owner(operation_id, set_digest)
        part = cache._partial_path(partial_owner, spec.sha256)
        part.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        if part.is_symlink():
            part.unlink(missing_ok=True)
        offset = part.stat().st_size if part.exists() else 0
        if offset > spec.expected_bytes:
            part.unlink(missing_ok=True)
            offset = 0
        if offset:
            # A previous disk error or abrupt exit can leave readable bytes
            # beyond the last successful sync. Make them durable before using
            # their length for resume or publishing an already-complete file.
            with part.open("r+b") as retained:
                os.fsync(retained.fileno())
        received = offset
        if fetch_only and offset == spec.expected_bytes:
            return
        if offset == spec.expected_bytes and cache._verify_file(part, spec):
            with cache._lock:
                if not cache._publication_allowed(
                    operation_id, set_digest, spec.sha256
                ):
                    raise OperationInterrupted(
                        "model download was removed during verification"
                    )
                cache._publish_object(spec, part)
                cache._mark_artifact_verified(spec, set_digest)
            return
        if offset == spec.expected_bytes:
            part.unlink(missing_ok=True)
            received = 0
            offset = 0
        if (
            spec.expected_bytes >= constants._PARALLEL_RANGE_MIN_BYTES
            and urlsplit(spec.source).scheme in {"http", "https"}
            and interrupt_after_bytes is None
            and cache._download_parallel_ranges(
                spec,
                part,
                set_digest,
                operation_id,
                completed_artifacts,
            )
        ):
            if fetch_only:
                cache._require_transfer_running(operation_id)
                return
            cache._complete_download(
                spec, set_digest, part, operation_id, completed_artifacts
            )
            return
        with cache._stream_gate(operation_id)():
            cache._download_sequential(
                spec,
                set_digest,
                part,
                offset,
                received,
                operation_id=operation_id,
                completed_artifacts=completed_artifacts,
                interrupt_after_bytes=interrupt_after_bytes,
            )
        if fetch_only:
            cache._require_transfer_running(operation_id)
            return
        cache._complete_download(
            spec, set_digest, part, operation_id, completed_artifacts
        )
