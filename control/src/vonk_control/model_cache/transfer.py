"""Transfer."""

from __future__ import annotations

import fcntl
import stat
from typing import TYPE_CHECKING, cast

import httpx2
from sqlalchemy.orm import Session
from vonk_agent_protocol import ModelCacheCode, OperationMemberProgress

from ..categorized_faults import OperationInterrupted
from ..model_cache_contract import (
    ModelCacheCounters,
    ModelCacheDownloadPayload,
    ModelCacheDownloadResult,
    ModelCacheOperationPhase,
    ModelCacheOperationProgress,
    ModelCacheRepairCheckpoint,
    ModelCacheRepairPayload,
    ModelCacheTransfer,
    ModelCacheTransferArtifact,
)
from ..model_cache_progress import PHASES, cache_progress
from ..model_cache_ranges import range_partial_bytes
from ..models import ModelCacheOperation, ModelCacheSet, ModelCacheSetArtifact
from . import constants
from .artifacts import (
    ArtifactPart,
    ArtifactSetManifest,
    ArtifactSpec,
    _unique_artifacts,
)
from .catalog_helpers import _iso_now
from .constants import (
    _PARALLEL_RANGE_WORKERS,
    _USE_MANIFEST_BYTES,
    SCHEMA_VERSION,
)
from .errors import ModelCacheConflictRefused, ModelCacheError, _ArtifactWriterBusy
from .persistence import _manifest_of, _operation_cancellation, _store_operation_payload

if TYPE_CHECKING:
    from .service import ModelCacheService


class TransferMixin:
    """Transfer behavior of the cache service."""

    def _managed_cached_objects(self, manifest: ArtifactSetManifest) -> frozenset[str]:
        """Read admission metadata for objects verified into managed storage.

        Publication verifies content before atomically placing an object and
        recording its receipt. Admission trusts that receipt across processes
        and restarts, while checking the file is still present and complete.
        Transfers and explicit verification retain their content checks.
        """
        cache = cast("ModelCacheService", self)
        specs = _unique_artifacts(manifest.artifacts)
        # Managed storage owns per-object availability: a verified object is
        # available exactly when its receipt is beside its bytes. SQL is not
        # consulted and holds no availability flag, so a damage or restore that
        # loses the receipt cannot be masked by a stale database row.
        return frozenset(
            sha256
            for sha256, spec in specs.items()
            if cache._object_is_available(sha256, spec.expected_bytes)
        )

    def _stored_object_bytes(self, digest: str) -> int:
        """Return a stored object's byte length, or zero when storage lacks it."""
        cache = cast("ModelCacheService", self)

        path = cache._object_path(digest)
        try:
            metadata = path.lstat()
        except (FileNotFoundError, NotADirectoryError):
            return 0
        except OSError:
            return 0
        return metadata.st_size if stat.S_ISREG(metadata.st_mode) else 0

    def _partial_bytes(self, set_digest: str, spec: ArtifactSpec) -> int:
        """Return only a bounded, reusable partial checkpoint length."""
        cache = cast("ModelCacheService", self)
        if spec.parts is not None:
            return cache._split_partial_bytes(set_digest, spec, spec.parts)
        partial = cache._partial_path(set_digest, spec.sha256)
        try:
            if partial.is_symlink():
                return 0
            if spec.expected_bytes >= constants._PARALLEL_RANGE_MIN_BYTES:
                return range_partial_bytes(
                    partial, spec.expected_bytes, workers=_PARALLEL_RANGE_WORKERS
                )
            if not partial.is_file():
                return 0
            size = partial.stat().st_size
        except OSError:
            return 0
        if size < 0 or size > spec.expected_bytes:
            return 0
        if size == spec.expected_bytes and not cache._verify_file(partial, spec):
            return 0
        return size

    def _split_partial_bytes(
        self, set_digest: str, spec: ArtifactSpec, parts: tuple[ArtifactPart, ...]
    ) -> int:
        """Retained bytes of a split file: appended whole parts plus the next part.

        Cheap by design (no hashing): digests are verified where the bytes are
        appended, so this only sizes what a resume will not refetch.
        """
        cache = cast("ModelCacheService", self)

        assembled = cache._partial_path(set_digest, spec.sha256)
        try:
            if assembled.is_symlink() or not assembled.is_file():
                size = 0
            else:
                size = assembled.stat().st_size
            if size > spec.expected_bytes:
                size = 0
            appended = 0
            done = 0
            for part in parts:
                if appended + part.expected_bytes > size:
                    break
                appended += part.expected_bytes
                done += 1
            retained = 0
            if done < len(parts):
                next_part = parts[done]
                path = cache._partial_path(set_digest, next_part.sha256)
                if not path.is_symlink():
                    if next_part.expected_bytes >= constants._PARALLEL_RANGE_MIN_BYTES:
                        retained = range_partial_bytes(
                            path,
                            next_part.expected_bytes,
                            workers=_PARALLEL_RANGE_WORKERS,
                        )
                    elif path.is_file():
                        retained = min(path.stat().st_size, next_part.expected_bytes)
            return appended + retained
        except OSError:
            return 0

    def _transfer_state_for_manifest(
        self,
        manifest: ArtifactSetManifest,
        *,
        force: bool,
        cached: frozenset[str] | None = None,
    ) -> ModelCacheTransfer:
        """Create the immutable planned transfer and per-object baselines."""
        cache = cast("ModelCacheService", self)
        artifacts: dict[str, ModelCacheTransferArtifact] = {}
        total_bytes = 0
        for digest, spec in _unique_artifacts(manifest.artifacts).items():
            baseline = (
                0
                if force
                else (
                    spec.expected_bytes
                    if (
                        digest in cached
                        if cached is not None
                        else cache._object_is_stored(spec)
                    )
                    else cache._partial_bytes(manifest.digest, spec)
                )
            )
            remaining = max(0, spec.expected_bytes - baseline)
            artifacts[digest] = ModelCacheTransferArtifact(
                baseline_bytes=baseline,
                received_bytes=0,
                started_at=_iso_now(cache._clock()),
            )
            total_bytes += remaining
        return ModelCacheTransfer(
            schema_version=SCHEMA_VERSION,
            total_bytes=total_bytes,
            artifacts=artifacts,
        )

    @staticmethod
    def _transfer_totals(payload: ModelCacheDownloadPayload) -> tuple[int, int]:
        transfer = payload.transfer
        return transfer.total_bytes, sum(
            entry.received_bytes for entry in transfer.artifacts.values()
        )

    def _start_transfer(
        self, operation_id: str, *, force: bool
    ) -> tuple[ArtifactSetManifest, str, bool, ModelCacheTransfer] | None:
        """What a claimed download or repair starts from, or ``None`` to skip it.

        The operation is the claim's own record: if it vanished, nothing is left
        to run, and one whose document cannot be read (and cannot be rebuilt from
        its set row) is ended as failed and kept for inspection.  Either way the
        caller moves on to the next operation (rule 5: unknown, never a blocker).
        A missing ``artifact_set_sha256`` column is re-derived from the manifest.
        """
        cache = cast("ModelCacheService", self)

        with cache._session(write=True) as session:
            operation = session.get(
                ModelCacheOperation, operation_id, with_for_update=True
            )
            if operation is None:
                return None
            payload = cache._transfer_or_retire(operation)
            if payload is None:
                return None
            manifest = _manifest_of(payload)
            if operation.artifact_set_sha256 is None:
                operation.artifact_set_sha256 = manifest.digest
            return (
                manifest,
                operation.artifact_set_sha256,
                payload.force_refresh or force,
                payload.transfer,
            )

    def _operation_transfer_snapshot(self, operation_id: str) -> tuple[int | None, int]:
        """The ledger's planned total and received bytes; unknown when unreadable."""
        cache = cast("ModelCacheService", self)

        with cache._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            payload = None if operation is None else cache._transfer_or_none(operation)
            return (None, 0) if payload is None else cache._transfer_totals(payload)

    def _transfer_state_for_operation(
        self, operation_id: str
    ) -> ModelCacheTransfer | None:
        cache = cast("ModelCacheService", self)
        with cache._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is None:
                return None
            payload = cache._transfer_or_none(operation)
            return None if payload is None else payload.transfer

    def _ensure_set(
        self,
        session: Session,
        manifest: ArtifactSetManifest,
    ) -> ModelCacheSet:
        cache = cast("ModelCacheService", self)
        set_digest = manifest.digest
        row = session.get(ModelCacheSet, set_digest)
        now = cache._clock()
        if row is None:
            row = ModelCacheSet(
                artifact_set_sha256=set_digest,
                schema_version=SCHEMA_VERSION,
                model_content_sha256=manifest.model_content_sha256,
                recipe_revision_sha256=manifest.recipe_revision_sha256,
                manifest=manifest.document(),
                expected_bytes=manifest.expected_bytes,
                verified_bytes=0,
                state="incomplete",
                protected=False,
                protected_reasons=[],
                created_at=now,
                updated_at=now,
                verified_at=None,
                last_accessed_at=now,
                last_error=None,
            )
            session.add(row)
            session.flush()
        else:
            # The set row keeps the first requested provenance document, while
            # the primary key is the reusable file identity.  A later model
            # or recipe revision may have different notes, capabilities,
            # roles, or requested digests without changing any bytes.
            stored = cache._stored_manifest(row)
            if stored is None:
                # The stored provenance document is damaged and nothing else
                # re-derives it: the manifest in hand (same key) replaces it.
                row.manifest = manifest.document()
            elif stored.digest != manifest.digest:
                raise ModelCacheConflictRefused(
                    ModelCacheCode.IDENTITY_CONFLICT,
                    "artifact-set digest resolves to different immutable content",
                )
        for spec in manifest.artifacts:
            # SQL owns membership only. The object's identity, size, and
            # availability are the manifest and the managed-storage receipt.
            membership = session.get(
                ModelCacheSetArtifact,
                {"artifact_set_sha256": set_digest, "artifact_key": spec.key},
            )
            if membership is None:
                session.add(
                    ModelCacheSetArtifact(
                        artifact_set_sha256=set_digest,
                        artifact_key=spec.key,
                        artifact_sha256=spec.sha256,
                        path=spec.path,
                    )
                )
        return row

    def _progress(
        self,
        manifest: ArtifactSetManifest,
        *,
        phase: ModelCacheOperationPhase,
        completed_artifacts: int = 0,
        downloaded_bytes: int = 0,
        expected_bytes: int | None | object = _USE_MANIFEST_BYTES,
        current_artifact_key: str | None = None,
        transfer: ModelCacheTransfer | None = None,
        previous: ModelCacheOperationProgress | None = None,
    ) -> ModelCacheOperationProgress:
        cache = cast("ModelCacheService", self)
        resolved_bytes = (
            manifest.expected_bytes
            if expected_bytes is _USE_MANIFEST_BYTES
            else expected_bytes
        )
        assert resolved_bytes is None or isinstance(resolved_bytes, int)
        counters = ModelCacheCounters(
            phase=phase,
            completed_artifacts=completed_artifacts,
            total_artifacts=len(manifest.artifacts),
            downloaded_bytes=downloaded_bytes,
            expected_bytes=resolved_bytes,
            current_artifact_key=current_artifact_key,
        )
        members = []
        unique = _unique_artifacts(manifest.artifacts)
        # Progress cannot impose a shard-count limit on installable models.
        # Larger sets retain exact aggregate counters without a partial member list.
        if transfer is not None and len(unique) <= 1024:
            for spec in unique.values():
                entry = transfer.artifacts[spec.sha256]
                baseline, received = entry.baseline_bytes, entry.received_bytes
                members.append(
                    OperationMemberProgress(
                        member_id=spec.sha256,
                        object_sha256=spec.sha256,
                        phase=PHASES[phase],
                        completed_bytes=min(spec.expected_bytes, baseline + received),
                        total_bytes=spec.expected_bytes,
                        state="succeeded"
                        if phase == "completed" or baseline == spec.expected_bytes
                        else "running",
                    )
                )
        return cache_progress(
            counters, previous=previous, now=cache._clock(), members=members
        )

    def _run_download(
        self,
        operation_id: str,
        *,
        force: bool,
        interrupt_after_bytes: int | None = None,
    ) -> None:
        cache = cast("ModelCacheService", self)
        started = cache._start_transfer(operation_id, force=force)
        if started is None:
            return
        manifest, set_digest, force, transfer = started
        planned_total = transfer.total_bytes
        transfer_attempt = cache._mark_running(operation_id)
        if transfer_attempt is None:
            return
        completed = 0
        try:
            with cache._lock:
                if not cache._publication_allowed(operation_id, set_digest):
                    raise OperationInterrupted(
                        "model download was removed before publication"
                    )
                with cache._session(write=True) as session:
                    cache._ensure_set(session, manifest)
            unique_specs = list(_unique_artifacts(manifest.artifacts).values())
            cache._set_operation_progress(
                operation_id,
                manifest,
                phase="verifying" if force else "downloading",
                completed_artifacts=0,
                downloaded_bytes=cache._operation_transfer_snapshot(operation_id)[1],
                expected_bytes=planned_total,
                current_artifact_key=None,
                transfer=transfer,
            )
            # Each background operation has its own SQLAlchemy sessions and
            # digest-specific partial paths.  The single Controller-wide pool
            # bounds concurrent transfer work across all selected operations.
            for spec in unique_specs:
                cache._download_one_unique(
                    spec,
                    set_digest,
                    operation_id=operation_id,
                    force=force,
                    interrupt_after_bytes=interrupt_after_bytes,
                )
            # Count logical manifest entries after all unique objects have
            # completed. Shared digests therefore remain one network transfer
            # while every selected file still reaches the complete stage.
            completed = len(manifest.artifacts)
            cache._set_operation_progress(
                operation_id,
                manifest,
                phase="completed",
                completed_artifacts=completed,
                downloaded_bytes=cache._operation_transfer_snapshot(operation_id)[1],
                expected_bytes=planned_total,
                current_artifact_key=None,
                transfer=cache._transfer_state_for_operation(operation_id),
            )
            with cache._session(write=True) as session:
                row = session.get(ModelCacheSet, set_digest)
                if row is not None:
                    row.state = "cached"
                    row.verified_bytes = manifest.expected_bytes
                    row.verified_at = cache._clock()
                    row.updated_at = cache._clock()
                    row.last_accessed_at = row.updated_at
                    row.last_error = None
            cache._finish_succeeded(
                operation_id,
                ModelCacheDownloadResult(
                    schema_version=SCHEMA_VERSION,
                    artifact_set_sha256=set_digest,
                    coverage="complete",
                ),
            )
        except (OperationInterrupted, InterruptedError) as error:
            cache._finish_partial(
                operation_id, set_digest, manifest, str(error) or "download interrupted"
            )
        except (ModelCacheError, OSError, httpx2.HTTPError, ValueError) as error:
            cache._finish_failed(
                operation_id,
                set_digest,
                manifest,
                error,
                failed_artifact_key=getattr(error, "failed_artifact_key", None),
                transfer_attempt=transfer_attempt,
            )

    def _download_one_unique(
        self,
        spec: ArtifactSpec,
        set_digest: str,
        *,
        operation_id: str,
        force: bool,
        interrupt_after_bytes: int | None,
    ) -> None:
        cache = cast("ModelCacheService", self)
        with cache._lock:
            if spec.sha256 in cache._active_digests:
                raise _ArtifactWriterBusy(spec.sha256)
            cache._active_digests.add(spec.sha256)
        try:
            # Neither local nor cross-process contention can park a transfer
            # slot. The durable operation is rescheduled after releasing it.
            with (cache._root / "locks" / spec.sha256).open("a+b") as lock_file:
                try:
                    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise _ArtifactWriterBusy(spec.sha256) from None
                # This small owner hint lets cancellation distinguish this
                # operation's issued effects from another consumer sharing
                # the same artifact lock. The flock remains the authority;
                # the bytes are consulted only while that flock is busy.
                lock_file.seek(0)
                lock_file.truncate()
                lock_file.write(operation_id.encode("ascii"))
                lock_file.flush()
                cache._download_locked(
                    spec,
                    set_digest,
                    operation_id=operation_id,
                    force=force,
                    interrupt_after_bytes=interrupt_after_bytes,
                )
        except ModelCacheError as error:
            if error.failed_artifact_key is None:
                error.failed_artifact_key = spec.key
            raise
        finally:
            with cache._lock:
                cache._active_digests.discard(spec.sha256)

    def _download_locked(
        self,
        spec: ArtifactSpec,
        set_digest: str,
        *,
        operation_id: str,
        force: bool,
        interrupt_after_bytes: int | None,
    ) -> None:
        cache = cast("ModelCacheService", self)
        with cache._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            assert operation is not None
            if operation.state == "cancelled" or _operation_cancellation(operation):
                cache._transfer_stop(operation_id).set()
                raise OperationInterrupted(
                    "model download cancellation was accepted; partial files preserved"
                )
            is_repair = operation.kind == "repair"
            payload = cache._payload_or_none(operation)
            if payload is None:
                # Unreadable bookkeeping interrupts the attempt; the claim loop
                # rebuilds or retires the operation, the transfer ledger on disk
                # (content-addressed) is kept.
                raise OperationInterrupted("cache operation document is unreadable")
            checkpoint = (
                payload.repair_checkpoint
                if force and isinstance(payload, ModelCacheRepairPayload)
                else None
            )
            repaired = checkpoint.completed_objects if checkpoint is not None else []
        if (not force or spec.sha256 in repaired) and cache._object_is_stored(spec):
            cache._mark_artifact_verified(spec, set_digest)
            return
        if spec.parts is not None:
            cache._download_split(
                spec,
                set_digest,
                operation_id=operation_id,
                force=force,
                interrupt_after_bytes=interrupt_after_bytes,
            )
        else:
            cache._download_artifact(
                spec,
                set_digest,
                operation_id=operation_id,
                completed_artifacts=0,
                force=force,
                interrupt_after_bytes=interrupt_after_bytes,
            )
        if is_repair:
            with cache._session(write=True) as session:
                operation = session.get(
                    ModelCacheOperation, operation_id, with_for_update=True
                )
                assert operation is not None
                payload = cache._payload_or_none(operation)
                if not isinstance(payload, ModelCacheRepairPayload):
                    raise OperationInterrupted("cache operation document is unreadable")
                checkpoint = payload.repair_checkpoint
                _store_operation_payload(
                    operation,
                    "repair",
                    payload.model_copy(
                        update={
                            "repair_checkpoint": ModelCacheRepairCheckpoint(
                                transfer_id=checkpoint.transfer_id,
                                completed_objects=list(
                                    dict.fromkeys(
                                        [*checkpoint.completed_objects, spec.sha256]
                                    )
                                ),
                            )
                        }
                    ),
                )
