"""Scheduler."""

from __future__ import annotations

from concurrent.futures import Future
from datetime import timedelta
from typing import TYPE_CHECKING, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from vonk_agent_protocol import ArtifactLifecycleCode, UnknownOutcomeError

from .. import model_cache_states
from ..agent_operation_facts import aware as _aware
from ..artifact_lifecycle import ArtifactIdentity, has_pending_removal
from ..bounded_json import require_integer
from ..categorized_errors import InvalidValue
from ..lifecycle import Outcome, Reported, State
from ..model_cache_contract import ModelCacheDownloadPayload, ModelCacheDownloadResult
from ..models import ModelCacheOperation, ModelCacheSet
from .artifacts import ArtifactSetManifest, _unique_artifacts
from .catalog_helpers import _iso
from .constants import (
    _MAX_PARALLEL_DOWNLOADS,
    _RETRY_BASE_SECONDS,
    _TRANSFER_CLAIM_SECONDS,
    SCHEMA_VERSION,
)
from .errors import _ArtifactWriterBusy
from .persistence import _cache_failure, _manifest_of
from .views import _BackgroundTransfer

if TYPE_CHECKING:
    from .service import ModelCacheService


class SchedulerMixin:
    """Scheduler behavior of the cache service."""

    def resume_operations(self, *, limit: int = 16) -> int:
        """Return durable cache work for the Controller worker to resume.

        Startup must not perform network or disk transfers inline.  The
        worker calls :meth:`tick` after it has claimed its process loop, so an
        API restart only discovers outstanding work here.
        """
        cache = cast("ModelCacheService", self)
        if not 1 <= limit <= 100:
            raise InvalidValue("cache operation limit is invalid")
        with cache._session() as session:
            count = require_integer(
                session.scalar(
                    select(func.count())
                    .select_from(ModelCacheOperation)
                    .where(
                        ModelCacheOperation.kind.in_(["download", "repair", "remove"])
                    )
                    .where(ModelCacheOperation.state.in_(model_cache_states.LIVE))
                ),
                "cache operation count",
            )
        return min(count, limit)

    def run_pending(self, *, limit: int = 1) -> int:
        """Process a bounded batch synchronously for maintenance and tests.

        Production worker dispatch uses :meth:`tick`, which only claims and
        submits work to the Controller-wide bounded pool.  This synchronous
        method intentionally remains available to deterministic maintenance
        callers and fixture tests.
        """
        cache = cast("ModelCacheService", self)
        if not 1 <= limit <= 16:
            raise InvalidValue("cache worker batch limit is invalid")
        cache._reconcile_pending_cancellations()
        cache._resume_after_credential_change()
        cache.reconcile_requested_removals()
        rows = cache._claim_operations(limit=limit, respect_backoff=False)
        for operation_id, kind in rows:
            with cache._session() as session:
                operation = session.get(ModelCacheOperation, operation_id)
                payload = (
                    cache._payload_or_none(operation) if operation is not None else None
                )
                refresh = (
                    isinstance(payload, ModelCacheDownloadPayload)
                    and payload.force_refresh
                )
            cache._run_download(operation_id, force=kind == "repair" or refresh)
        return len(rows) + cache.advance_removals(limit=limit)

    def tick(self, *, limit: int | None = None) -> int:
        """Claim and submit due operations without blocking the worker loop."""
        cache = cast("ModelCacheService", self)

        if cache._closed.is_set():
            return 0

        # A caller-supplied Session is intentionally retained for synchronous
        # fixture/maintenance use only. Background tasks must obtain isolated
        # sessions from a sessionmaker; SQLAlchemy Session is not thread safe.
        if isinstance(cache._sessions, Session):
            return cache.run_pending(limit=1)
        requested = cache._max_parallel_downloads if limit is None else limit
        if not 1 <= requested <= _MAX_PARALLEL_DOWNLOADS:
            raise InvalidValue("cache worker batch limit is invalid")
        cache._streams.tick()
        cache._reconcile_pending_cancellations()
        cache._resume_after_credential_change()
        # Removal steps use the same Controller model-cache worker boundary,
        # but never occupy a transfer slot while waiting: each artifact lock
        # and SQL ownership check is nonblocking and a contended step is
        # durably deferred before this bounded local filesystem action returns.
        cache.reconcile_requested_removals()
        removal_steps = cache.advance_removals(
            limit=min(requested, cache._max_parallel_downloads)
        )
        with cache._lock:
            completed = cache._advance_background_operations()
            capacity = cache._available_transfer_slots()
            if not capacity:
                return completed + removal_steps
            claimed = cache._claim_operations(
                limit=min(requested, capacity), respect_backoff=True
            )
            for operation_id, kind in claimed:
                with cache._session() as session:
                    operation = session.get(ModelCacheOperation, operation_id)
                    stored = (
                        None
                        if operation is None
                        else cache._transfer_or_none(operation)
                    )
                    refresh = stored is not None and stored.force_refresh
                cache._schedule_background_download(
                    operation_id,
                    force=kind == "repair" or refresh,
                    capacity=1,
                )
            # Allocate one transfer to every selected operation first, then
            # round-robin remaining slots. A large Model cannot monopolize the
            # Controller pool while another selected Model waits at zero.
            while True:
                capacity = cache._available_transfer_slots()
                if capacity <= 0:
                    break
                progressed = False
                for operation_id in list(cache._background_operations):
                    before = cache._available_transfer_slots()
                    record = cache._background_operations.get(operation_id)
                    pending = 0 if record is None else record.pending()
                    cache._fill_background_slots(operation_id, pending + 1)
                    if cache._available_transfer_slots() < before:
                        progressed = True
                    if cache._available_transfer_slots() <= 0:
                        break
                if not progressed:
                    break
            return completed + len(claimed) + removal_steps

    def _available_transfer_slots(self) -> int:
        cache = cast("ModelCacheService", self)
        return max(
            0,
            cache._max_parallel_downloads
            - sum(record.pending() for record in cache._background_operations.values()),
        )

    def _schedule_background_download(
        self, operation_id: str, *, force: bool, capacity: int
    ) -> None:
        cache = cast("ModelCacheService", self)
        started = cache._start_transfer(operation_id, force=force)
        if started is None:
            return
        manifest, set_digest, force, transfer = started
        planned_total = transfer.total_bytes
        with cache._session(write=True) as session:
            cache._ensure_set(session, manifest)
        transfer_attempt = cache._mark_running(operation_id)
        if transfer_attempt is None:
            return
        specs = list(_unique_artifacts(manifest.artifacts).values())
        cache._background_operations[operation_id] = _BackgroundTransfer(
            set_digest=set_digest,
            manifest=manifest,
            force=force,
            specs=specs,
            planned_total=planned_total,
            transfer_attempt=transfer_attempt,
        )
        cache._set_operation_progress(
            operation_id,
            manifest,
            phase="verifying" if force else "downloading",
            completed_artifacts=0,
            expected_bytes=planned_total,
            downloaded_bytes=cache._operation_transfer_snapshot(operation_id)[1],
            current_artifact_key=None,
            transfer=transfer,
        )
        cache._fill_background_slots(operation_id, capacity)

    def _fill_background_slots(self, operation_id: str, capacity: int) -> None:
        cache = cast("ModelCacheService", self)
        record = cache._background_operations.get(operation_id)
        if cache._transfer_stop(operation_id).is_set():
            return
        if record is None:
            return
        pending = record.pending()
        while pending < capacity and record.next_index < len(record.specs):
            spec = record.specs[record.next_index]
            record.next_index += 1
            future = cache._executor.submit(
                cache._download_one_unique,
                spec,
                record.set_digest,
                operation_id=operation_id,
                force=record.force,
                interrupt_after_bytes=None,
            )
            record.futures.append(future)
            record.future_specs[future] = spec.key
            pending += 1

    def _advance_background_operations(self) -> int:
        cache = cast("ModelCacheService", self)
        cache._renew_background_claims()
        finished = 0
        for operation_id, record in list(cache._background_operations.items()):
            futures: list[Future[None]] = record.futures
            first_error = record.failure
            future: Future[None]
            for future in [future for future in futures if future.done()]:
                futures.remove(future)
                failed_artifact_key = record.future_specs.pop(future, None)
                try:
                    future.result()
                except UnknownOutcomeError as error:
                    # An unknown outcome (a busy writer, unconfirmed storage or
                    # bookkeeping, a stopped transfer) is observed and retried,
                    # never ended: the operation keeps its durable row, settles
                    # below through the core's bounded backoff and the claim
                    # loop resumes the same transfer on a later tick.
                    if first_error is None:
                        first_error = error
                        record.failure = error
                        record.failure_artifact_key = failed_artifact_key
                    sibling: Future[None]
                    for sibling in futures:
                        sibling.cancel()
                except Exception as error:  # noqa: BLE001 - settle failed background transfers durably
                    if record.failure is None:
                        record.failure = error
                        record.failure_artifact_key = failed_artifact_key
                    other: Future[None]
                    for other in futures:
                        other.cancel()
            first_error = record.failure
            if first_error is not None:
                # A cancelled Future may still be running. Keep the durable
                # claim and record until every sibling has settled, so a
                # late checkpoint cannot resurrect a failed operation or
                # overwrite its terminal failure payload.
                if futures:
                    continue
                if isinstance(first_error, InterruptedError):
                    cache._finish_partial(
                        operation_id,
                        record.set_digest,
                        record.manifest,
                        str(first_error) or "download interrupted",
                    )
                else:
                    cache._finish_failed(
                        operation_id,
                        record.set_digest,
                        record.manifest,
                        first_error,
                        failed_artifact_key=record.failure_artifact_key,
                        transfer_attempt=record.transfer_attempt,
                    )
                cache._background_operations.pop(operation_id, None)
                finished += 1
                continue
            if record.next_index >= len(record.specs) and not futures:
                cache._finish_background_success(
                    operation_id,
                    record.set_digest,
                    record.manifest,
                    record.planned_total,
                )
                cache._background_operations.pop(operation_id, None)
                finished += 1
            else:
                capacity = cache._available_transfer_slots()
                cache._fill_background_slots(
                    operation_id, record.pending() + min(1, capacity)
                )
        return finished

    def _renew_background_claims(self) -> None:
        cache = cast("ModelCacheService", self)
        if not cache._background_operations:
            return
        now = cache._clock()
        with cache._session(write=True) as session:
            for operation_id in cache._background_operations:
                operation = session.get(ModelCacheOperation, operation_id)
                if operation is None:
                    continue
                # An unreadable document carries no readable cancel intent: the
                # transfer keeps its lease and the claim loop reconciles the row.
                operation_payload = cache._payload_or_none(operation)
                if operation.state == "cancelled" or (
                    operation_payload is not None
                    and operation_payload.cancellation is not None
                ):
                    cache._transfer_stop(operation_id).set()
                    cache._background_operations[
                        operation_id
                    ].failure = InterruptedError(
                        "model download cancellation was accepted"
                    )
                    continue
                # The heartbeat of every transfer this process runs: one Heartbeat
                # per operation renews its lease (a fence that is not ours is
                # left alone).
                cache._lifecycle.renew(
                    operation,
                    cache._claim_owner,
                    _TRANSFER_CLAIM_SECONDS,
                    now,
                    take=False,
                )

    def _finish_background_success(
        self,
        operation_id: str,
        set_digest: str,
        manifest: ArtifactSetManifest,
        planned_total: int,
    ) -> None:
        cache = cast("ModelCacheService", self)
        cache._set_operation_progress(
            operation_id,
            manifest,
            phase="completed",
            completed_artifacts=len(manifest.artifacts),
            downloaded_bytes=cache._operation_transfer_snapshot(operation_id)[1],
            expected_bytes=planned_total,
            current_artifact_key=None,
            transfer=cache._transfer_state_for_operation(operation_id),
        )
        now = cache._clock()
        publication_allowed = False
        with cache._lock:
            try:
                publication_allowed = cache._publication_allowed(
                    operation_id, set_digest
                )
            except _ArtifactWriterBusy as error:
                cache._defer_artifact_writer(operation_id, error, None)
                return
            if publication_allowed:
                with cache._session(write=True) as session:
                    row = session.get(ModelCacheSet, set_digest)
                    if row is not None:
                        row.state = "cached"
                        row.verified_bytes = manifest.expected_bytes
                        row.verified_at = now
                        row.updated_at = now
                        row.last_accessed_at = now
                        row.last_error = None
        if not publication_allowed:
            cache._try_settle_cancellation(operation_id)
            return
        cache._finish_succeeded(
            operation_id,
            ModelCacheDownloadResult(
                schema_version=SCHEMA_VERSION,
                artifact_set_sha256=set_digest,
                coverage="complete",
            ),
        )

    def _claim_operations(
        self, *, limit: int, respect_backoff: bool
    ) -> list[tuple[str, str]]:
        cache = cast("ModelCacheService", self)
        now = cache._clock()
        claimed: list[tuple[str, str]] = []
        with cache._session(write=True) as session:
            if respect_backoff:
                cooldown_rows = list(
                    session.scalars(
                        select(ModelCacheOperation)
                        .where(ModelCacheOperation.kind.in_(["download", "repair"]))
                        .where(
                            ModelCacheOperation.state.in_(
                                (*model_cache_states.LIVE, "failed")
                            )
                        )
                        .order_by(ModelCacheOperation.updated_at.desc())
                        .limit(256)
                    )
                )
                cache._refresh_huggingface_cooldown(cooldown_rows, now)
            candidate_ids = list(
                session.scalars(
                    select(ModelCacheOperation.id)
                    .where(ModelCacheOperation.kind.in_(["download", "repair"]))
                    .where(ModelCacheOperation.state.in_(model_cache_states.LIVE))
                    .order_by(ModelCacheOperation.updated_at, ModelCacheOperation.id)
                )
            )
            for operation_id in candidate_ids:
                if len(claimed) >= limit:
                    break
                operation = session.scalar(
                    select(ModelCacheOperation)
                    .where(
                        ModelCacheOperation.id == operation_id,
                        ModelCacheOperation.kind.in_(["download", "repair"]),
                        ModelCacheOperation.state.in_(model_cache_states.LIVE),
                    )
                    .with_for_update(skip_locked=True)
                    # The cooldown scan may have cached this row before another
                    # worker committed a claim; inspect the locked database value.
                    .execution_options(populate_existing=True)
                )
                if operation is None:
                    continue
                row = cache._lifecycle.lifecycle(operation, now)
                if row.state is State.OBSERVING:
                    continue  # a cancel is being settled
                if (
                    row.state is State.RUNNING
                    and row.lease_deadline is not None
                    and row.lease_deadline > _aware(now)
                ):
                    # A live lease, ours or another process's (a running workload
                    # of an older or newer Controller): never retired, even when
                    # this process cannot read its document.
                    continue
                # An unreadable envelope is rebuilt from its set row, else the
                # operation ends as failed (kept for inspection) and the claim
                # loop carries on: one damaged row never stops the others.
                payload = cache._transfer_or_retire(operation, now=now)
                if payload is None or payload.cancellation is not None:
                    continue
                if row.state is State.RUNNING:
                    # The attempt can no longer report: the core decides (rule 1:
                    # nothing here is irreversible, so it is retried with backoff)
                    # instead of the next claimant stealing the claim.
                    row = cache._lifecycle.lapse(operation, now)
                if respect_backoff:
                    if row.next_action_at is not None and row.next_action_at > _aware(
                        now
                    ):
                        continue
                    if (
                        cache._hf_cooldown_until is not None
                        and cache._hf_cooldown_until > now
                        and cache._payload_has_huggingface_source(payload)
                    ):
                        continue
                manifest = _manifest_of(payload)
                if has_pending_removal(
                    session,
                    (
                        ArtifactIdentity("model-set", manifest.digest),
                        *(
                            ArtifactIdentity("model-object", item.sha256)
                            for item in manifest.artifacts
                        ),
                    ),
                ):
                    cache._lifecycle.settle(
                        operation,
                        Reported(
                            Outcome.UNKNOWN,
                            retry_after=now + timedelta(seconds=_RETRY_BASE_SECONDS),
                            reason="Waiting for the prior model removal fence to settle",
                        ),
                        now,
                        consume_retry=False,
                    )
                    cache._store_failure(
                        operation,
                        _cache_failure(
                            ArtifactLifecycleCode.DELETION_IN_PROGRESS,
                            "Waiting for the prior model removal fence to settle",
                            retryable=True,
                            recovery="retry",
                            retry_time=_iso(operation.next_action_at),
                        ),
                    )
                    continue
                if cache._capacity_holds(operation, payload, now):
                    continue
                if not cache._lifecycle.claim(
                    operation,
                    cache._claim_owner,
                    _TRANSFER_CLAIM_SECONDS,
                    now,
                    ignore_backoff=not respect_backoff,
                ):
                    continue
                claimed.append((operation.id, operation.kind))
        return claimed
