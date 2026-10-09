"""Checkpoints."""

from __future__ import annotations

import os
import stat
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast

from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    ModelCacheCode,
    ModelFileState,
    ProgressPhase,
    adopt_progress_phase,
)

from ..agent_operation_facts import aware as _aware
from ..lifecycle import Outcome, Reported, State
from ..model_cache_contract import ModelCacheTransferArtifact
from ..model_cache_progress import cache_phase, progress_document
from ..models import ModelCacheOperation, ModelCacheSet
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
        cache = cast("ModelCacheService", self)
        now = cache._clock()
        with cache._lock:
            if not force_progress:
                last = cache._progress_checkpoint_at.get(operation_id)
                if last is not None and 0 <= (now - last).total_seconds() < 1:
                    return
            # Bound this process-local fast path to the current sampling window.
            cache._progress_checkpoint_at = {
                key: value
                for key, value in cache._progress_checkpoint_at.items()
                if 0 <= (now - value).total_seconds() < 1
            }
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
                if operation is not None and not force_progress:
                    prior = _operation_progress(operation).measurement
                    if (
                        adopt_progress_phase(prior.phase) is ProgressPhase.DOWNLOADING
                        and prior.observed_at is not None
                        and 0
                        <= (
                            now - datetime.fromisoformat(prior.observed_at)
                        ).total_seconds()
                        < 1
                    ):
                        cache._progress_checkpoint_at[operation_id] = now
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
                    # A part of a split file reports on the whole file's ledger
                    # entry, offset by the parts already appended.
                    ledger_digest = spec.ledger_sha256 or spec.sha256
                    actual_bytes += spec.ledger_base
                    entry = payload.transfer.artifacts.get(ledger_digest)
                    baseline = 0 if entry is None else entry.baseline_bytes
                    previous_received = 0 if entry is None else entry.received_bytes
                    artifacts = {
                        **payload.transfer.artifacts,
                        ledger_digest: ModelCacheTransferArtifact(
                            baseline_bytes=baseline,
                            received_bytes=max(
                                previous_received, max(0, actual_bytes - baseline)
                            ),
                            started_at=entry.started_at
                            if entry is not None
                            else _iso_now(cache._clock()),
                        ),
                    }
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
                            phase="downloading"
                            if state == ModelFileState.PARTIAL
                            else "verifying",
                            completed_artifacts=max(old_completed, completed_artifacts),
                            downloaded_bytes=max(old_downloaded, received),
                            expected_bytes=total,
                            current_artifact_key=spec.key,
                            transfer=transfer,
                        )
                    )
                    operation.current_artifact_key = spec.key
                    operation.updated_at = now
                row = session.get(ModelCacheSet, set_digest)
                if row is not None:
                    row.state = (
                        "downloading"
                        if state == ModelFileState.PARTIAL
                        else "verifying"
                    )
                    row.verified_bytes = cache._verified_bytes(session, set_digest)
                    row.updated_at = now
            cache._progress_checkpoint_at[operation_id] = now

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
            # Local storage observations are cache misses. The normal exact
            # preparation path reopens and verifies after storage recovers.
            return None
        try:
            metadata = os.fstat(fd)
        finally:
            os.close(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != expected_bytes:
            return None
        return expected_bytes

    def _verified_bytes(self, session: Session, set_digest: str) -> int:
        cache = cast("ModelCacheService", self)
        row = session.get(ModelCacheSet, set_digest)
        if row is None:
            return 0
        manifest = cache._stored_manifest(row)
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
                    row.verified_bytes = cache._verified_bytes(session, set_digest)
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
                        consume_retry=True,
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
                Reported(
                    Outcome.UNKNOWN,
                    fence=cache._claim_owner,
                    retry_after=now + timedelta(seconds=_RETRY_BASE_SECONDS),
                    reason=error.detail,
                ),
                now,
                interrupted=False,
                consume_retry=False,
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
