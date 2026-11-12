"""Checkpoints."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast

from vonk_agent_protocol import (
    Disposition,
    ModelCacheCode,
    ModelFileState,
)

from ..agent_operation_facts import aware as _aware
from ..lifecycle import Outcome, Reported, State
from ..model_cache_contract import ModelCacheTransferArtifact
from ..model_cache_progress import cache_phase, progress_document
from ..models import ModelCacheOperation, ModelCacheSet
from ..operation_progress import STALE_AFTER_SECONDS, STALL_AFTER_SECONDS
from ..profile_read_observation import observe_storage
from .artifacts import ArtifactSetManifest, ArtifactSpec
from .catalog_helpers import _iso, _iso_now
from .constants import _RETRY_BASE_SECONDS, _TRANSFER_CLAIM_SECONDS
from .errors import _ArtifactWriterBusy
from .persistence import (
    _cache_failure,
    _manifest_of,
    _operation_cancellation,
    _operation_progress,
    _store_operation_payload,
)

if TYPE_CHECKING:
    from .service import ModelCacheService


#: How often a running download writes its durable progress. One write per
#: operation carries every stream's latest bytes, so the cost does not grow with
#: the stream count. The cadence is a third of the shortest window that reads
#: that write: progress older than ``STALE_AFTER_SECONDS`` (45 s) is shown as
#: waiting without a rate, ``STALL_AFTER_SECONDS`` flags a stopped transfer, and
#: the write renews the ``_TRANSFER_CLAIM_SECONDS`` lease. Resume never reads it:
#: a restarted transfer resumes from the bytes retained on disk.
PROGRESS_CHECKPOINT_SECONDS = (
    min(STALE_AFTER_SECONDS, STALL_AFTER_SECONDS, _TRANSFER_CLAIM_SECONDS) / 3
)


@dataclass(frozen=True, slots=True)
class _StreamSample:
    """One stream's (or part's) latest progress, as its sampler last saw it."""

    spec: ArtifactSpec
    actual_bytes: int
    completed_artifacts: int
    state: str
    noted_at: datetime


@dataclass(slots=True)
class _ProgressWindow:
    """One operation's live streams between durable writes, in process memory."""

    noted_at: datetime
    written_at: datetime | None = None
    # Object digest -> the latest sample of a stream still transferring.
    samples: dict[str, _StreamSample] = field(default_factory=dict)


def _within(now: datetime, then: datetime | None) -> bool:
    return then is not None and 0 <= (now - then).total_seconds() < (
        PROGRESS_CHECKPOINT_SECONDS
    )


class CheckpointsMixin:
    """Checkpoints behavior of the cache service."""

    def _checkpoint_artifact(
        self,
        spec: ArtifactSpec,
        *,
        operation_id: str,
        set_digest: str,
        actual_bytes: int,
        state: str,
        completed_artifacts: int = 0,
        force_progress: bool = True,
    ) -> None:
        """Record a stream's bytes; write the operation's progress when due.

        A sampled heartbeat (``force_progress=False``) is held in memory and
        written with the operation's next write, at most once per
        ``PROGRESS_CHECKPOINT_SECONDS``. A completion, failure or stop writes at
        once. The set's ``verified_bytes`` is not recomputed here: the entry
        read, reconcile and finalization own that whole-set projection.
        """
        cache = cast("ModelCacheService", self)
        now = cache._clock()
        with cache._lock:
            # Bound this process-local state to operations still sampling.
            windows = {
                key: value
                for key, value in cache._progress_checkpoints.items()
                if key == operation_id or _within(now, value.noted_at)
            }
            cache._progress_checkpoints = windows
            window = windows.setdefault(operation_id, _ProgressWindow(noted_at=now))
            window.noted_at = now
            sample = _StreamSample(spec, actual_bytes, completed_artifacts, state, now)
            window.samples[spec.sha256] = sample
            if not force_progress and _within(now, window.written_at):
                return
            samples = list(window.samples.values())
            # The operation is downloading while any of its streams still is:
            # one file's verification does not hide the others' transfer.
            transferring = [
                value for value in samples if value.state == ModelFileState.PARTIAL
            ]
            current = (
                sample
                if sample.state == ModelFileState.PARTIAL or not transferring
                else transferring[-1]
            )
            phase = "downloading" if transferring else "verifying"
            # Kept for the next write: streams still transferring. A stream that
            # ended (verified or corrupt), or that wrote and then stayed silent
            # for a whole window, has nothing left to report.
            previous = window.written_at
            window.samples = {
                key: value
                for key, value in window.samples.items()
                if value.state == ModelFileState.PARTIAL
                and (
                    previous is None
                    or value.noted_at >= previous
                    or _within(now, value.noted_at)
                )
            }
            window.written_at = now
            with cache._session(write=True) as session:
                operation = session.get(
                    ModelCacheOperation, operation_id, with_for_update=True
                )
                if operation is not None and (
                    operation.state == "cancelled"
                    or _operation_cancellation(operation) is not None
                ):
                    cache._transfer_stop(operation_id).set()
                    return
                if operation is not None and operation.state in {"succeeded", "failed"}:
                    return
                # Refresh bytes belong to the operation's transfer ledger. The
                # last verified receipt and its object remain published until
                # the replacement has been verified and atomically committed.
                payload = (
                    None if operation is None else cache._transfer_or_none(operation)
                )
                if operation is not None and payload is not None:
                    # (an unreadable document is not checkpointed: the bytes
                    # are content-addressed and the claim loop reconciles it)
                    manifest = _manifest_of(payload)
                    artifacts = dict(payload.transfer.artifacts)
                    for value in samples:
                        # A part of a split file reports on the whole file's
                        # ledger entry, offset by the parts already appended.
                        sampled = value.spec
                        ledger_digest = sampled.ledger_sha256 or sampled.sha256
                        landed = value.actual_bytes + sampled.ledger_base
                        entry = artifacts.get(ledger_digest)
                        baseline = 0 if entry is None else entry.baseline_bytes
                        previous_received = 0 if entry is None else entry.received_bytes
                        artifacts[ledger_digest] = ModelCacheTransferArtifact(
                            baseline_bytes=baseline,
                            received_bytes=max(
                                previous_received, max(0, landed - baseline)
                            ),
                            started_at=entry.started_at
                            if entry is not None
                            else _iso_now(cache._clock()),
                        )
                    transfer = payload.transfer.model_copy(
                        update={"artifacts": artifacts}
                    )
                    total = transfer.total_bytes
                    received = sum(value.received_bytes for value in artifacts.values())
                    payload = payload.model_copy(update={"transfer": transfer})
                    _store_operation_payload(operation, operation.kind, payload)
                    old_progress = _operation_progress(operation)
                    old_downloaded = old_progress.downloaded_bytes
                    old_completed = old_progress.completed_artifacts
                    # A checkpoint is a heartbeat: it renews this process's lease;
                    # under another fence, or after the core put the operation
                    # back to wait, it changes nothing (rule 7).
                    cache._lifecycle.renew(
                        operation,
                        cache._claim_owner,
                        _TRANSFER_CLAIM_SECONDS,
                        now,
                        take=False,
                    )
                    operation.progress = progress_document(
                        cache._progress(
                            manifest,
                            previous=old_progress,
                            phase=phase,
                            completed_artifacts=max(
                                old_completed,
                                *(value.completed_artifacts for value in samples),
                            ),
                            downloaded_bytes=max(old_downloaded, received),
                            expected_bytes=total,
                            current_artifact_key=current.spec.key,
                            transfer=transfer,
                        )
                    )
                    operation.current_artifact_key = current.spec.key
                    operation.updated_at = now
                    if received > old_downloaded:
                        operation.progress_advanced_at = now
                row = session.get(ModelCacheSet, set_digest)
                if row is not None:
                    row.state = phase
                    row.updated_at = now

    def _mark_artifact_verified(self, spec: ArtifactSpec, set_digest: str) -> None:
        cache = cast("ModelCacheService", self)
        now = cache._clock()
        # Availability is a managed-storage fact: the receipt beside the bytes
        # owns it. This runs once per object inside the publication lock, so it
        # stays bounded -- the set-level projection is recomputed once when the
        # operation finalizes or the entry is read, never per object, which
        # would make one set quadratic in its own membership.
        cache._write_object_receipt(spec, now)
        with cache._session(write=True) as session:
            row = session.get(ModelCacheSet, set_digest)
            if row is not None:
                row.updated_at = now

    @observe_storage
    def _stored_object(self, digest: str, expected_bytes: int) -> int | None:
        """Return the stored bytes when storage holds this exact object.

        One receipt read and one descriptor check answer both "is it
        available" and "how many bytes are there", so a whole-set read costs
        one pass over the objects instead of several.
        """
        cache = cast("ModelCacheService", self)

        if cache._read_object_receipt(digest, expected_bytes) is None:
            return None
        try:
            fd = os.open(
                cache._object_path(digest),
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
            )
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError:
            # Local availability is unconfirmed; preview remains readable and
            # the request-led worker observes or prepares this object again.
            return None
        try:
            metadata = os.fstat(fd)
        except OSError:
            metadata = None
        finally:
            try:
                os.close(fd)
            except OSError:
                metadata = None
        if (
            metadata is None
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != expected_bytes
        ):
            return None
        return expected_bytes

    def _verified_bytes(self, set_digest: str) -> int:
        """The bytes of the set held verified in storage.

        Read before a locking transaction opens, never inside one: the manifest
        is read and a session of its own closed before any file is read, so the
        transaction only stores the figure.
        """
        cache = cast("ModelCacheService", self)
        with cache._session() as session:
            row = session.get(ModelCacheSet, set_digest)
            manifest = None if row is None else cache._stored_manifest(row)
        return cache._stored_bytes_of(manifest)

    def _stored_bytes_of(self, manifest: ArtifactSetManifest | None) -> int:
        cache = cast("ModelCacheService", self)
        if manifest is None:
            return 0  # unknown: counted again once the manifest is re-derived
        total = 0
        seen: set[str] = set()
        for spec in manifest.artifacts:
            if spec.sha256 in seen:
                continue
            seen.add(spec.sha256)
            stored = cache._stored_object(spec.sha256, spec.expected_bytes)
            if stored is not None:
                total += stored
        return total

    def _finish_partial(
        self,
        operation_id: str,
        set_digest: str,
        manifest: ArtifactSetManifest,
        detail: str,
    ) -> None:
        cache = cast("ModelCacheService", self)
        now = cache._clock()
        cancellation_pending = False
        verified_bytes = cache._verified_bytes(set_digest)
        with cache._session(write=True) as session:
            operation = session.get(
                ModelCacheOperation, operation_id, with_for_update=True
            )
            if operation is not None and operation.state == "cancelled":
                return
            cancellation_pending = (
                operation is not None and _operation_cancellation(operation) is not None
            )
            if not cancellation_pending:
                row = session.get(ModelCacheSet, set_digest)
                if row is not None:
                    row.state = "incomplete"
                    row.verified_bytes = verified_bytes
                    row.updated_at = now
                    row.last_error = detail[:512]
                if operation is not None:
                    operation.last_error = detail[:512]
                    # Interrupted work is uncertain, not failed: the core retries
                    # the exact transfer (nothing here is irreversible).
                    cache._lifecycle.complete(
                        operation,
                        Reported(
                            Outcome.UNKNOWN,
                            fence=cache._claim_owner,
                            reason=detail[:512],
                        ),
                        now,
                        interrupted=True,
                    )
                    current = cache._transfer_or_none(operation)
                    if current is not None:
                        payload = current.model_copy(
                            update={
                                "failure": _cache_failure(
                                    ModelCacheCode.INTERRUPTED,
                                    f"{detail[:480]}; preserved bytes remain available to resume",
                                    retryable=True,
                                    recovery="resume",
                                )
                            }
                        )
                        _store_operation_payload(operation, operation.kind, payload)
                        _total, received = cache._transfer_totals(payload)
                        previous_progress = _operation_progress(operation)
                        operation.progress = progress_document(
                            cache._progress(
                                manifest,
                                previous=previous_progress,
                                phase="downloading",
                                completed_artifacts=previous_progress.completed_artifacts,
                                downloaded_bytes=received,
                                expected_bytes=_total,
                                current_artifact_key=operation.current_artifact_key,
                                transfer=payload.transfer,
                            )
                        )
                        operation.progress = progress_document(
                            cache_phase(
                                _operation_progress(operation),
                                "downloading",
                                now,
                                waiting=True,
                            )
                        )
                    operation.updated_at = now
        if cancellation_pending:
            cache._try_settle_cancellation(operation_id)

    def _defer_artifact_writer(
        self,
        operation_id: str,
        error: _ArtifactWriterBusy,
        artifact_key: str | None,
    ) -> None:
        cache = cast("ModelCacheService", self)
        now = cache._clock()
        with cache._session(write=True) as session:
            operation = session.get(
                ModelCacheOperation, operation_id, with_for_update={"nowait": True}
            )
            if operation is None or operation.state != "running":
                return
            payload = cache._payload_or_none(operation)
            if payload is None:
                return  # unreadable: the lease lapses and the claim loop reconciles
            if payload.cancellation is not None:
                cache._transfer_stop(operation_id).set()
                return
            row = cache._lifecycle.lifecycle(operation, now)
            if (
                row.state is not State.RUNNING
                or row.fence not in (None, cache._claim_owner)
                or (
                    row.lease_deadline is not None and row.lease_deadline <= _aware(now)
                )
            ):
                return  # the lease is another process's (or has lapsed): not ours
            # A dependency wait, not a consumed attempt: the owner of the busy
            # lock releases it and the same attempt resumes.
            cache._lifecycle.complete(
                operation,
                Reported.failed(
                    Disposition.WAIT,
                    fence=cache._claim_owner,
                    retry_after=now + timedelta(seconds=_RETRY_BASE_SECONDS),
                    reason=error.detail,
                ),
                now,
                interrupted=False,
            )
            next_retry = operation.next_action_at
            if next_retry is None:
                return
            delay = max(1, round((_aware(next_retry) - now).total_seconds()))
            cache._store_failure(
                operation,
                _cache_failure(
                    error.code,
                    error.detail,
                    retryable=True,
                    recovery="resume",
                    retry_time=_iso(next_retry),
                    retry_after_seconds=delay,
                    artifact_key=artifact_key,
                ),
            )
            operation.last_error = error.detail
            operation.progress = progress_document(
                cache_phase(_operation_progress(operation), "queued", now, waiting=True)
            )
