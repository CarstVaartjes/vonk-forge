"""Scheduler."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast

from sqlalchemy import func, select, true
from vonk_agent_protocol import (
    ArtifactLifecycleCode,
    Disposition,
    InvalidRequestReason,
    UnknownOutcomeError,
)
from vonk_agent_protocol.http_failure import TransientReason

from .. import model_cache_states
from ..agent_operation_facts import aware as _aware
from ..artifact_lifecycle import ArtifactIdentity, has_pending_removal
from ..bounded_json import require_integer
from ..categorized_errors import InvalidValue
from ..lifecycle import Effect, Lifecycle, Outcome, Reported, State, Tick
from ..lifecycle.evidence import BookkeepingReason, retire_as_unknown
from ..model_cache_contract import (
    CacheOperationKind,
    ModelCacheDownloadPayload,
    ModelCacheDownloadResult,
    ModelCacheTransfer,
)
from ..models import ModelCacheOperation, ModelCacheSet
from .artifacts import ArtifactSetManifest, _unique_artifacts
from .catalog_helpers import _iso
from .constants import (
    _CREDENTIAL_FAILURE_PUBLIC_CODES,
    _MAX_PARALLEL_DOWNLOADS,
    _RETRY_BASE_SECONDS,
    _TRANSFER_CLAIM_SECONDS,
    SCHEMA_VERSION,
)
from .errors import ModelCacheError, _ArtifactWriterBusy
from .persistence import (
    _cache_failure,
    _manifest_of,
    _store_operation_payload,
    _updated,
)
from .storage import RunningTransfers, TransferNeed
from .views import _BackgroundTransfer

if TYPE_CHECKING:
    from .service import ModelCacheService


@dataclass(frozen=True, slots=True)
class _ClaimFact:
    """What one due candidate's claim needs from the disk, read before the claim.

    ``payload`` is the document the facts were read for: the claim only uses
    them while the locked row still holds exactly it. ``moves_bytes`` is set
    when the claim would transfer bytes, ``need`` when that need was read (not
    when no transfer slot was free), ``replanned`` when an object has gone
    missing.
    """

    payload: ModelCacheDownloadPayload | None
    replanned: ModelCacheTransfer | None = None
    moves_bytes: bool = False
    need: TransferNeed | None = None


@dataclass(slots=True)
class _ClaimFacts:
    """The disk facts of one claim pass, in claim order, read with no lock held."""

    candidates: dict[str, _ClaimFact] = field(default_factory=dict)
    running: RunningTransfers = field(default_factory=dict)
    running_read: bool = False


def _claim_held(row: Lifecycle, now: datetime) -> bool:
    """Whether another actor holds the row: a cancel being settled or a live lease.

    A live lease is ours or another process's (a running workload of an older
    or newer Controller): never retired, even when this process cannot read its
    document.
    """
    return row.state is State.OBSERVING or (
        row.state is State.RUNNING
        and row.lease_deadline is not None
        and row.lease_deadline > _aware(now)
    )


def _claim_backing_off(row: Lifecycle, now: datetime, respect_backoff: bool) -> bool:
    return (
        respect_backoff
        and row.next_action_at is not None
        and row.next_action_at > _aware(now)
    )


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
            # Provider waits outlive a restart: rebuild their cooldowns once.
            cache._adopt_provider_cooldowns(
                list(
                    session.scalars(
                        select(ModelCacheOperation)
                        .where(ModelCacheOperation.kind.in_(["download", "repair"]))
                        .where(ModelCacheOperation.state.in_(model_cache_states.LIVE))
                        .where(
                            ModelCacheOperation.payload["failure"]["code"].as_string()
                            == TransientReason.RATE_LIMITED.value
                        )
                    )
                ),
                cache._clock(),
            )
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
        cache.reconcile_model_revisions()
        return len(rows) + cache.advance_removals(limit=limit)

    def redrive(self, operation_id: str) -> int:
        cache = cast("ModelCacheService", self)
        with cache._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is None:
                return 0
            removal = operation.kind == CacheOperationKind.REMOVE
        if removal:
            return int(cache._advance_model_removal(operation_id, now=cache._clock()))
        return cache.tick(requested_id=operation_id)

    def tick(self, *, limit: int | None = None, requested_id: str | None = None) -> int:
        """Claim and submit due operations without blocking the worker loop."""
        cache = cast("ModelCacheService", self)

        if cache._closed.is_set():
            return 0

        if requested_id is None:
            cache.reconcile_model_revisions()
        requested = cache._max_parallel_downloads if limit is None else limit
        if not 1 <= requested <= _MAX_PARALLEL_DOWNLOADS:
            raise InvalidValue("cache worker batch limit is invalid")
        cache._storage_observer.tick()
        cache._streams.tick()
        cache._reconcile_pending_cancellations(requested_id)
        cache._resume_after_credential_change() if requested_id is None else None
        # Removal steps use the same Controller model-cache worker boundary,
        # but never occupy a transfer slot while waiting: each artifact lock
        # and SQL ownership check is nonblocking and a contended step is
        # durably deferred before this bounded local filesystem action returns.
        cache.reconcile_requested_removals() if requested_id is None else None
        removal_steps = (
            cache.advance_removals(limit=min(requested, cache._max_parallel_downloads))
            if requested_id is None
            else 0
        )
        # The disk is read before the lock: download threads take it for every
        # checkpoint and publication, so no claim holds it across file I/O.
        facts = cache._claim_facts(
            limit=requested,
            respect_backoff=True,
            requested_id=requested_id,
        )
        with cache._lock:
            completed = cache._advance_background_operations()
            # A download whose objects are all stored takes no transfer slot:
            # it is claimed even while the pool is saturated and completes on
            # this tick, so a hung or slow transfer can never hold it back.
            claimed = cache._claim_operations(
                limit=requested,
                transfer_slots=cache._available_transfer_slots(),
                respect_backoff=True,
                requested_id=requested_id,
                facts=facts,
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
                    operation_id, force=kind == CacheOperationKind.REPAIR or refresh
                )
            cache._share_transfer_slots()
            return completed + len(claimed) + removal_steps

    def _available_transfer_slots(self) -> int:
        cache = cast("ModelCacheService", self)
        return max(
            0,
            cache._max_parallel_downloads
            - sum(record.pending() for record in cache._background_operations.values()),
        )

    def _schedule_background_download(self, operation_id: str, *, force: bool) -> None:
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
        if not force and planned_total == 0:
            # The claim just confirmed every object's receipt and size (bytes
            # are verified at ingress, never re-hashed): there is nothing to
            # transfer or re-record, so the download ends here instead of
            # queueing behind the pool. Publication still checks the gates.
            cache._finish_background_success(
                operation_id, set_digest, manifest, planned_total
            )
            return
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

    def _share_transfer_slots(self) -> None:
        """Give each free slot to the transfer with the fewest files in flight.

        Every operation waiting at zero is served before any takes another
        slot, so a large model never refills its own freed slots ahead of a
        model that has none; a newly claimed operation is served first, so a
        claim never holds a lease without a transfer. Each pass submits one
        file, so the loop ends once the pool is full or no operation has a
        file left to start.
        """
        cache = cast("ModelCacheService", self)
        while cache._available_transfer_slots() > 0:
            # Ties go to the operation that has started the fewest files, so
            # a large download back at zero in flight never takes a slot ahead
            # of one that has not started; the id only orders equals.
            waiting = [
                (record.pending(), record.next_index, operation_id)
                for operation_id, record in cache._background_operations.items()
                if record.failure is None
                and record.next_index < len(record.specs)
                and not cache._transfer_stop(operation_id).is_set()
            ]
            if not waiting:
                return
            *_served, operation_id = min(waiting)
            record = cache._background_operations[operation_id]
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
                        cache._remember_verified_models(session, row, manifest)
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
        self,
        *,
        limit: int,
        respect_backoff: bool,
        requested_id: str | None = None,
        transfer_slots: int | None = None,
        facts: _ClaimFacts | None = None,
    ) -> list[tuple[str, str]]:
        """Claim up to ``limit`` due operations.

        With ``transfer_slots``, at most that many claims may need a transfer;
        a download whose objects are all stored needs none and is always due.
        Each claim is one short database transaction over ``facts`` (read with
        no lock held); a row changed since its facts were read waits a tick.
        """
        cache = cast("ModelCacheService", self)
        now = cache._clock()
        claimed: list[tuple[str, str]] = []
        transfers = 0
        if facts is None:
            facts = cache._claim_facts(
                limit=limit,
                respect_backoff=respect_backoff,
                requested_id=requested_id,
            )
        # Each claim reserves its remaining bytes in this pass, so the pass
        # never admits more downloads than the disk holds between them.
        running = facts.running
        for operation_id, fact in facts.candidates.items():
            if len(claimed) >= limit:
                break
            with cache._session(write=True) as session:
                operation = session.scalar(
                    select(ModelCacheOperation)
                    .where(
                        ModelCacheOperation.id == operation_id,
                        ModelCacheOperation.kind.in_(["download", "repair"]),
                        ModelCacheOperation.state.in_(model_cache_states.LIVE),
                    )
                    .with_for_update(skip_locked=True)
                    # An earlier read of this session may have cached this row
                    # before another worker committed a claim; inspect the
                    # locked database value.
                    .execution_options(populate_existing=True)
                )
                if operation is None:
                    continue
                row = cache._lifecycle.lifecycle(operation, now)
                deadline = cache._lifecycle.recovery_deadline(row)
                if (
                    deadline is not None
                    and now >= deadline
                    and not row.cancel_requested
                ):
                    cache._lifecycle.settle(operation, Tick(), now)
                    continue
                if _claim_held(row, now):
                    continue
                # An unreadable envelope is rebuilt from its set row, else the
                # operation ends as failed (kept for inspection) and the claim
                # loop carries on: one damaged row never stops the others.
                payload = cache._transfer_or_retire(operation, now=now)
                if payload is None or payload.cancellation is not None:
                    continue
                if payload != fact.payload:
                    continue  # changed since the facts were read: the next tick re-plans
                if (
                    payload.failure is not None
                    and payload.failure.code in _CREDENTIAL_FAILURE_PUBLIC_CODES
                ):
                    newer = session.scalar(
                        select(ModelCacheOperation.id)
                        .where(
                            ModelCacheOperation.artifact_set_sha256
                            == operation.artifact_set_sha256,
                            ModelCacheOperation.created_at > operation.created_at,
                        )
                        .limit(1)
                    )
                    if newer is not None:
                        cache._lifecycle.settle(
                            operation,
                            Reported(
                                Outcome.CANCELLED,
                                effect=Effect.UNKNOWN,
                                reason=InvalidRequestReason.SUPERSEDED.value,
                            ),
                            now,
                        )
                        continue
                if row.state is State.RUNNING:
                    # The attempt can no longer report: the core decides (rule 1:
                    # nothing here is irreversible, so it is retried with backoff)
                    # instead of the next claimant stealing the claim.
                    row = cache._lifecycle.lapse(operation, now)
                if _claim_backing_off(row, now, respect_backoff):
                    continue
                try:
                    manifest = _manifest_of(payload)
                except (TypeError, ValueError, ModelCacheError):
                    cache._retire_unreadable(
                        operation,
                        retire_as_unknown(
                            "model-cache-operation",
                            operation.id,
                            BookkeepingReason.PERSISTED_STATE_DAMAGED,
                            "the operation's manifest does not read",
                        ),
                        now=now,
                    )
                    continue
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
                    # The removal is the live owner of this wait: its window
                    # restarts instead of running out behind it.
                    operation.progress_advanced_at = _aware(now)
                    cache._lifecycle.settle(
                        operation,
                        Reported.failed(
                            Disposition.WAIT,
                            retry_after=now + timedelta(seconds=_RETRY_BASE_SECONDS),
                            reason="Waiting for the prior model removal fence to settle",
                        ),
                        now,
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
                need = fact.need
                if fact.replanned is not None:
                    # The plan found every object stored, but one has gone
                    # since: its bytes were re-planned, and it takes a transfer
                    # slot like any other download.
                    payload = _updated(payload, transfer=fact.replanned)
                    _store_operation_payload(operation, operation.kind, payload)
                if (
                    fact.moves_bytes
                    and transfer_slots is not None
                    and transfers >= transfer_slots
                ):
                    continue  # due again once a transfer slot is free
                if fact.moves_bytes and need is None:
                    continue  # past the facts' read bound: next tick
                if need is not None and cache._capacity_holds(
                    operation, need, now, running
                ):
                    # A lapsed attempt waits now: its bytes are not in flight.
                    running.pop(operation.id, None)
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
                if need is not None:
                    transfers += 1
                    running[operation.id] = need
        return claimed

    def _claim_facts(
        self,
        *,
        limit: int,
        respect_backoff: bool,
        requested_id: str | None = None,
    ) -> _ClaimFacts:
        """Read the disk facts the next ``limit`` due candidates' claims need.

        No lock and no transaction is held while the files are read: a row that
        cannot be claimed now (live lease, backoff, a cancel being settled) is
        passed over without a read, the reads cover ``limit`` byte-movers and
        ``limit`` downloads that move none (so movers never crowd out an
        all-stored one), and what the running transfers still write is read
        once, for the first candidate that would move bytes. The transfer
        slots are not read here: they change while the disk is read, so the
        claim, under the lock, is the only authority on them.
        """
        cache = cast("ModelCacheService", self)
        now = cache._clock()
        snapshots: list[tuple[str, str, ModelCacheDownloadPayload | None]] = []
        queued_moves = queued_stored = 0
        with cache._session() as session:
            for operation in session.scalars(
                select(ModelCacheOperation)
                .where(
                    ModelCacheOperation.id == requested_id
                    if requested_id is not None
                    else true()
                )
                .where(ModelCacheOperation.kind.in_(["download", "repair"]))
                .where(ModelCacheOperation.state.in_(model_cache_states.LIVE))
                .order_by(ModelCacheOperation.updated_at, ModelCacheOperation.id)
            ):
                row = cache._lifecycle.lifecycle(operation, now)
                running = row.state is State.RUNNING
                # A running row's backoff is decided after the claim lapses it.
                if _claim_held(row, now) or (
                    not running and _claim_backing_off(row, now, respect_backoff)
                ):
                    continue
                payload = cache._transfer_or_none(operation)
                # The payload alone says a transfer moves bytes (no disk read).
                moves = (
                    payload is not None
                    and payload.cancellation is None
                    and (
                        operation.kind == CacheOperationKind.REPAIR
                        or payload.force_refresh
                        or payload.transfer.total_bytes > 0
                    )
                )
                if not running:
                    # A running row here has a lapsed lease: the claim lapses it
                    # (bounded by the transfer pool), so it never uses the limit.
                    if moves:
                        if queued_moves >= limit:
                            continue
                        queued_moves += 1
                    else:
                        if queued_stored >= limit:
                            continue
                        queued_stored += 1
                snapshots.append((operation.id, operation.kind, payload))
        facts = _ClaimFacts()
        for operation_id, kind, payload in snapshots:
            fact = _ClaimFact(payload)
            try:
                manifest = None if payload is None else _manifest_of(payload)
            except (TypeError, ValueError, ModelCacheError):
                manifest = None  # the claim retires it
            if (
                payload is not None
                and manifest is not None
                and payload.cancellation is None
            ):
                replaces = kind == CacheOperationKind.REPAIR or payload.force_refresh
                transfer = payload.transfer
                replanned = None
                unique = _unique_artifacts(manifest.artifacts)
                if not replaces and transfer.total_bytes == 0:
                    cached = frozenset(
                        digest
                        for digest, spec in unique.items()
                        if cache._object_is_stored(spec)
                    )
                    if len(cached) < len(unique):
                        replanned = cache._transfer_state_for_manifest(
                            manifest, force=False, cached=cached
                        )
                        transfer = replanned
                need = None
                moves = replanned is not None or replaces or transfer.total_bytes > 0
                if moves:
                    if not facts.running_read:
                        with cache._session() as session:
                            rows = cache._running_payloads(session)
                        facts.running = cache._running_needs(rows)
                        facts.running_read = True
                    need = cache._transfer_need(manifest, transfer, replaces=replaces)
                fact = _ClaimFact(payload, replanned, moves, need)
            facts.candidates[operation_id] = fact
        return facts
