"""Cancellation."""

from __future__ import annotations

import fcntl
from typing import TYPE_CHECKING, cast

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import ModelCacheCode

from .. import model_cache_states
from ..agent_operation_facts import aware as _aware
from ..lifecycle import CancelRequested, State, Tick
from ..lifecycle.evidence import Residue
from ..model_cache_contract import (
    ModelCacheCancellation,
    ModelCacheCancellationRequest,
    ModelCacheOperationPayload,
)
from ..model_cache_progress import cache_phase, progress_document
from ..models import ModelCacheOperation
from .artifacts import ArtifactSetManifest, _unique_artifacts
from .catalog_helpers import _iso
from .errors import ModelCacheConflictInvalid
from .persistence import (
    _operation_cancellation,
    _operation_payload,
    _operation_progress,
    _store_operation_payload,
)
from .views import CacheOperationView

if TYPE_CHECKING:
    from .service import ModelCacheService


class CancellationMixin:
    """Cancellation behavior of the cache service."""

    def _artifact_effects_settled(
        self, operation_id: str, manifest: ArtifactSetManifest
    ) -> bool:
        """Check this operation's issued object writers without blocking.

        A lock holder writes its operation ID while holding the flock. A busy
        lock owned by another operation is shared work and does not delay this
        cancellation. Missing or malformed owner bytes are treated
        conservatively because an older or interrupted worker may still hold
        the lock.
        """
        cache = cast("ModelCacheService", self)

        for spec in _unique_artifacts(manifest.artifacts).values():
            try:
                descriptor = (cache._root / "locks" / spec.sha256).open("a+b")
            except OSError:
                return False
            with descriptor:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    descriptor.seek(0)
                    owner = descriptor.read(36).decode("ascii", errors="ignore")
                    if not owner or owner == operation_id:
                        return False
                    continue
                # Acquiring the nonblocking lock proves no writer is
                # currently effecting this object. A stale worker that
                # starts later must read the durable cancellation fence.
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        return True

    def _try_settle_cancellation(
        self, operation_id: str, *, forced: bool = True
    ) -> bool:
        """Drive a durable cancel intent through the core; ``True`` once it ended.

        The core stops the transfer (idempotently), observes until no writer of
        the operation's objects is active, and ends the cancel as ``cancelled``
        (rule 4).  A stop that stays unconfirmed past its budget ends it anyway
        with the effect unknown and a residue note: a cancel never waits for an
        operator.  ``forced`` (the cancel itself and a worker that just ended)
        decides now; the reconciler's pass decides when the core's clock says.
        """
        cache = cast("ModelCacheService", self)

        now = cache._clock()
        with cache._session(write=True) as session:
            operation = session.scalar(
                select(ModelCacheOperation)
                .where(ModelCacheOperation.id == operation_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if operation is None:
                return True  # gone: there is nothing left to cancel
            if operation.state == "cancelled":
                return True
            cancellation = _operation_cancellation(operation)
            if cancellation is None:
                return False
            if operation.kind != "download" or operation.artifact_set_sha256 is None:
                return False
            row = cache._lifecycle.lifecycle(operation, now)
            when = (
                max(_aware(now), row.next_action_at)
                if forced and row.next_action_at is not None
                else now
            )
            event = (
                CancelRequested(cancellation.request_key, cancellation.reason)
                if row.state is not State.OBSERVING
                else Tick()
            )
            settled = cache._lifecycle.settle(operation, event, when)
            return settled.state is State.CANCELLED

    def _reconcile_pending_cancellations(self) -> int:
        """Settle what is due: pending cancels, lapsed leases.

        A cancel whose transfers are already idle ends at once (that costs no
        stop budget: it is the success path, and it is what a restarted
        Controller does for an intent its predecessor persisted); a cancel whose
        stop is still unconfirmed is left to the reconcile loop, which re-issues
        the stop at the core's bounded rate and, after its budget, ends it.
        """
        cache = cast("ModelCacheService", self)

        with cache._session() as session:
            candidates = [
                (operation.id, cache._payload_of(operation))
                for operation in session.scalars(
                    select(ModelCacheOperation)
                    .where(
                        ModelCacheOperation.kind == "download",
                        ModelCacheOperation.state.in_(model_cache_states.LIVE),
                    )
                    .order_by(ModelCacheOperation.updated_at, ModelCacheOperation.id)
                )
            ]
        settled = 0
        for operation_id, payload in candidates:
            if (
                payload is not None
                and payload.cancellation is not None
                and cache.effects_settled(operation_id, payload)
            ):
                settled += int(cache._try_settle_cancellation(operation_id))
        return settled + cache._reconciler.reconcile().changed

    @staticmethod
    def _payload_of(
        operation: ModelCacheOperation,
    ) -> ModelCacheOperationPayload | None:
        value = _operation_payload(operation)
        return None if isinstance(value, Residue) else value  # no readable intent

    def _cancellation_intent(
        self, *, actor: str, request_key: str, reason: str
    ) -> ModelCacheCancellation:
        cache = cast("ModelCacheService", self)
        try:
            request = ModelCacheCancellationRequest.model_validate(
                {"request_key": request_key, "reason": reason}
            )
            return ModelCacheCancellation(
                request_key=request.request_key,
                actor=actor,
                reason=request.reason,
                requested_at=_iso(cache._clock()) or "",
            )
        except (TypeError, ValueError, ValidationError) as error:
            raise ModelCacheConflictInvalid(
                ModelCacheCode.CANCELLATION_INVALID,
                "cancellation identity, actor, or reason is invalid",
            ) from error

    def _persist_cancellation(
        self,
        session: Session,
        operation_id: str,
        cancellation: ModelCacheCancellation,
        *,
        preserve_existing_owner: bool,
    ) -> bool:
        cache = cast("ModelCacheService", self)
        operation = session.scalar(
            select(ModelCacheOperation)
            .where(ModelCacheOperation.id == operation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if operation is None:
            return False  # gone: a parent waits for nothing
        payload = cache._payload_or_retire(operation)
        if payload is None:
            return False  # unreadable: retired (ended), nothing left to cancel
        existing = _operation_cancellation(operation)
        if existing is not None:
            if (existing.request_key, existing.actor, existing.reason) != (
                cancellation.request_key,
                cancellation.actor,
                cancellation.reason,
            ):
                if preserve_existing_owner:
                    return False
                raise ModelCacheConflictInvalid(
                    ModelCacheCode.CANCELLATION_KEY_REUSED,
                    "operation already has a different cancellation request",
                )
            return False
        if (
            operation.kind != "download"
            or operation.state not in model_cache_states.LIVE
            or operation.request_key == cancellation.request_key
        ):
            raise ModelCacheConflictInvalid(
                ModelCacheCode.NOT_CANCELLABLE,
                "model download is not active or cancellation key conflicts",
            )
        _store_operation_payload(
            operation,
            operation.kind,
            payload.model_copy(
                update={"cancellation": cancellation, "failure": None, "result": None}
            ),
        )
        now = cache._clock()
        operation.progress = progress_document(
            cache_phase(_operation_progress(operation), "cancelling", now, waiting=True)
        )
        operation.completed_at = None
        operation.updated_at = now
        return True

    def cancel_operation_in_session(
        self,
        session: Session,
        operation_id: str,
        *,
        actor: str,
        request_key: str,
        reason: str,
    ) -> bool:
        """Persist a parent-owned cancel intent in its consumer transaction.

        A pre-existing cancellation remains owned by its original actor and
        request. The parent waits for that exact child to settle instead of
        replacing its durable intent.
        """
        cache = cast("ModelCacheService", self)

        cancellation = cache._cancellation_intent(
            actor=actor, request_key=request_key, reason=reason
        )
        return cache._persist_cancellation(
            session,
            operation_id,
            cancellation,
            preserve_existing_owner=True,
        )

    def signal_cancelled_operation(self, operation_id: str) -> None:
        """Signal and attempt settlement only after the caller's commit."""
        cache = cast("ModelCacheService", self)

        cache._transfer_stop(operation_id).set()
        cache._try_settle_cancellation(operation_id)

    def cancel_operation(
        self,
        operation_id: str,
        *,
        actor: str,
        request_key: str,
        reason: str,
    ) -> CacheOperationView:
        """Persist cancellation intent before signalling local transfer workers."""
        cache = cast("ModelCacheService", self)

        cancellation = cache._cancellation_intent(
            actor=actor, request_key=request_key, reason=reason
        )
        with cache._session(write=True) as session:
            cache._persist_cancellation(
                session,
                operation_id,
                cancellation,
                preserve_existing_owner=False,
            )

        # The committed payload is the authority. This process-local event is
        # only a prompt to workers already sampling or reading a source.
        cache.signal_cancelled_operation(operation_id)
        return cache.get_operation(operation_id)
