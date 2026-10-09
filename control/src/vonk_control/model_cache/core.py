"""Core."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session, object_session
from vonk_agent_protocol import ModelCacheCode

from .. import model_cache_states
from ..lifecycle import Lifecycle, State
from ..lifecycle.evidence import BookkeepingReason, Damaged, Residue, read_or_rebuild
from ..logging import log_event
from ..model_cache_contract import (
    CacheManifest,
    ModelCacheDownloadPayload,
    ModelCacheOperationPayload,
    ModelCacheRemovalPayload,
)
from ..model_cache_progress import cache_phase, progress_document
from ..models import ModelCacheOperation, ModelCacheSet
from ..operation_contract import AvailabilityOperationFailure
from ..stored_json import read_row_column
from .artifacts import ArtifactSetManifest, _unique_artifacts
from .constants import _LOGGER
from .errors import ModelCacheError
from .persistence import (
    _manifest_of,
    _operation_payload,
    _operation_progress,
    _store_operation_payload,
)

if TYPE_CHECKING:
    from .service import ModelCacheService


class CoreMixin:
    """Core behavior of the cache service."""

    def close(self) -> None:
        """Stop the Controller-wide transfer pool during service shutdown."""
        cache = cast("ModelCacheService", self)

        cache._closed.set()
        # Let active streams observe the shutdown signal and checkpoint before
        # releasing the service. HTTP clients have bounded read timeouts, so
        # this wait is finite while preventing post-shutdown DB/file writes.
        cache._executor.shutdown(wait=True, cancel_futures=True)
        cache._upstream_executor.shutdown(wait=False, cancel_futures=True)
        with cache._lock:
            cache._advance_background_operations()
            cache._progress_checkpoint_at.clear()

    @property
    def root(self) -> Path:
        cache = cast("ModelCacheService", self)
        return cache._root

    @property
    def reserve_bytes(self) -> int:
        cache = cast("ModelCacheService", self)
        return cache._reserve_bytes

    @contextmanager
    def _session(self, *, write: bool = False) -> Iterator[Session]:
        cache = cast("ModelCacheService", self)
        if write:
            with cache._sessions.begin() as session:
                yield session
        else:
            with cache._sessions() as session:
                yield session

    def running_here(self) -> frozenset[str]:
        """Operations this process is transferring right now (``CacheEffects``)."""
        cache = cast("ModelCacheService", self)

        return frozenset(list(cache._background_operations))

    def objects_present(self, payload: ModelCacheOperationPayload) -> bool | None:
        """Whether every object of the operation's set has a storage receipt."""
        cache = cast("ModelCacheService", self)

        if not isinstance(payload, ModelCacheDownloadPayload):
            return None
        try:
            return cache._objects_present(_manifest_of(payload))
        except (TypeError, ValueError, OSError, ModelCacheError):
            return None

    def _objects_present(self, manifest: ArtifactSetManifest) -> bool:
        cache = cast("ModelCacheService", self)
        return cache._managed_cached_objects(manifest) == frozenset(
            spec.sha256 for spec in manifest.artifacts
        )

    def set_is_cached(self, set_digest: str | None) -> bool:
        cache = cast("ModelCacheService", self)
        if set_digest is None:
            return False
        with cache._session() as session:
            row = session.get(ModelCacheSet, set_digest)
            return row is not None and row.state == "cached"

    def signal_stop(self, operation_id: str) -> None:
        """Signal local transfers without acquiring storage or database locks."""
        cache = cast("ModelCacheService", self)
        cache._transfer_stop(operation_id).set()

    def effects_settled(
        self, operation_id: str, payload: ModelCacheOperationPayload | None
    ) -> bool:
        """Signal the operation's transfers to stop; whether none is still writing."""
        cache = cast("ModelCacheService", self)

        self.signal_stop(operation_id)
        if not isinstance(payload, ModelCacheDownloadPayload):
            return False  # unreadable: a writer may still be active
        try:
            manifest = _manifest_of(payload)
        except (TypeError, ValueError):
            return False
        return cache._artifact_effects_settled(operation_id, manifest)

    def cooldown_until(self, payload: ModelCacheOperationPayload) -> datetime | None:
        """The provider cooldown (rate limit) the operation's sources are under."""
        cache = cast("ModelCacheService", self)

        until = cache._hf_cooldown_until
        if until is None or until <= cache._clock():
            return None
        try:
            return until if cache._payload_has_huggingface_source(payload) else None
        except (KeyError, TypeError, ValueError):
            return None

    def _payload_or_none(
        self, operation: ModelCacheOperation
    ) -> ModelCacheOperationPayload | None:
        """The operation's payload, rebuilt from evidence; ``None`` when nothing can.

        Read-only: a caller that holds no write lock on the row skips it and
        carries on.  The worker paths use :meth:`_payload_or_retire`, which also
        retires the row so the damage is dealt with once.
        """

        value = _operation_payload(operation)
        return None if isinstance(value, Residue) else value

    def _payload_or_retire(
        self, operation: ModelCacheOperation, *, now: datetime | None = None
    ) -> ModelCacheOperationPayload | None:
        """As :meth:`_payload_or_none`, and end an unreadable operation as failed.

        The caller holds the row's write lock.  The damaged document is kept for
        inspection (``fail_corrupt`` records the end through the lifecycle core)
        and the caller skips the row: one damaged operation never stops the rest.
        """
        cache = cast("ModelCacheService", self)

        value = _operation_payload(operation)
        if not isinstance(value, Residue):
            return value
        cache._retire_unreadable(operation, value, now=now)
        return None

    def _removal_or_none(
        self, operation: ModelCacheOperation
    ) -> ModelCacheRemovalPayload | None:
        """:meth:`_payload_or_none` for a removal; ``None`` for any other kind."""
        cache = cast("ModelCacheService", self)

        value = cache._payload_or_none(operation)
        return value if isinstance(value, ModelCacheRemovalPayload) else None

    def _transfer_or_none(
        self, operation: ModelCacheOperation
    ) -> ModelCacheDownloadPayload | None:
        """:meth:`_payload_or_none` for a download or repair (its manifest, ledger)."""
        cache = cast("ModelCacheService", self)

        value = cache._payload_or_none(operation)
        return value if isinstance(value, ModelCacheDownloadPayload) else None

    def _transfer_or_retire(
        self, operation: ModelCacheOperation, *, now: datetime | None = None
    ) -> ModelCacheDownloadPayload | None:
        """:meth:`_payload_or_retire` for a download or repair."""
        cache = cast("ModelCacheService", self)

        value = cache._payload_or_retire(operation, now=now)
        return value if isinstance(value, ModelCacheDownloadPayload) else None

    def _retire_unreadable(
        self,
        operation: ModelCacheOperation,
        residue: Residue,
        *,
        now: datetime | None = None,
    ) -> None:
        cache = cast("ModelCacheService", self)
        at = now or cache._clock()
        if not cache._lifecycle.lifecycle(operation, at).terminal:
            cache._lifecycle.fail_corrupt(
                operation, f"{residue.reason.value}: {residue.note}", at
            )
        log_event(
            _LOGGER,
            ModelCacheCode.OPERATION_UNREADABLE,
            service="controller",
            operation_id=operation.id,
            code=residue.reason.value,
            detail=residue.note[:200],
        )

    def _store_failure(
        self, operation: ModelCacheOperation, failure: AvailabilityOperationFailure
    ) -> ModelCacheOperationPayload | None:
        """Record ``failure`` in the operation's document; ``None`` when unreadable.

        The lifecycle core already holds the outcome (state, retry clock); the
        document only carries the explanation, so an unreadable one is skipped.
        """
        cache = cast("ModelCacheService", self)

        current = cache._payload_or_none(operation)
        if current is None:
            return None
        payload = current.model_copy(update={"failure": failure})
        _store_operation_payload(operation, operation.kind, payload)
        return payload

    def _stored_manifest(self, row: ModelCacheSet) -> ArtifactSetManifest | None:
        """The manifest a set row stores, re-derived from the catalog when damaged.

        The evidence is the row's own pins (model content, recipe revision): the
        catalog resolves them to a manifest, which counts only when it names the
        same artifact set.  ``None`` (a typed unknown) when nothing re-derives it:
        the caller skips this set; the next request or sweep reconciles it.
        """
        cache = cast("ModelCacheService", self)

        def rebuild() -> ArtifactSetManifest | None:
            try:
                candidate = cache.resolve_artifact_set(
                    model_content_sha256=row.model_content_sha256,
                    recipe_revision_sha256=row.recipe_revision_sha256,
                )
            except ModelCacheError:
                return None
            return candidate if candidate.digest == row.artifact_set_sha256 else None

        def read() -> ArtifactSetManifest | Damaged:
            value = read_row_column(row, "manifest")
            if isinstance(value, CacheManifest):
                try:
                    return ArtifactSetManifest.from_contract(value)
                except ModelCacheError:
                    return Damaged("persisted cache manifest is invalid")
            return Damaged("persisted cache manifest is invalid")

        value = read_or_rebuild(
            kind="model-cache-set",
            subject=row.artifact_set_sha256,
            read=read,
            rebuild=rebuild,
            reason=BookkeepingReason.PERSISTED_STATE_DAMAGED,
        )
        return None if isinstance(value, Residue) else value

    def _project_end(
        self, operation: ModelCacheOperation, before: Lifecycle, after: Lifecycle
    ) -> None:
        """What a cancelled download leaves behind, in the transaction that ends it.

        The set's row follows what storage proves (cached when every object has a
        receipt, else incomplete) unless a sibling operation still works on it.
        """
        cache = cast("ModelCacheService", self)

        if after.state is not State.CANCELLED or operation.kind != "download":
            return
        set_digest = operation.artifact_set_sha256
        now = cache._clock()
        payload = _operation_payload(operation)
        operation.progress = progress_document(
            cache_phase(_operation_progress(operation), "completed", now)
        )
        operation.current_artifact_key = None
        if isinstance(payload, Residue):
            # The cancel stands; the set row is left to the storage sweep, which
            # follows what the receipts prove.
            return
        payload = payload.model_copy(update={"failure": None})
        _store_operation_payload(operation, operation.kind, payload)
        if set_digest is None or not isinstance(payload, ModelCacheDownloadPayload):
            return
        try:
            manifest = _manifest_of(payload)
        except ValueError:
            return
        unique_specs = _unique_artifacts(manifest.artifacts)
        stored_bytes = {
            digest: cache._stored_object(digest, spec.expected_bytes)
            for digest, spec in unique_specs.items()
        }
        verified_bytes = sum(value or 0 for value in stored_bytes.values())
        coverage_complete = all(
            stored_bytes[digest] == spec.expected_bytes
            for digest, spec in unique_specs.items()
        )
        # The operation's own transaction: the set projection commits with it.
        session = object_session(operation)
        if session is None:
            # The storage sweep rebuilds this disposable projection.
            return
        cache._project_cancelled_set(
            session,
            set_digest,
            exclude_operation_id=operation.id,
            verified_bytes=verified_bytes,
            coverage_complete=coverage_complete,
            now=now,
        )

    @staticmethod
    def _project_cancelled_set(
        session: Session,
        set_digest: str,
        *,
        exclude_operation_id: str,
        verified_bytes: int,
        coverage_complete: bool,
        now: datetime,
    ) -> None:
        sibling_work = session.scalar(
            select(func.count())
            .select_from(ModelCacheOperation)
            .where(
                ModelCacheOperation.artifact_set_sha256 == set_digest,
                ModelCacheOperation.id != exclude_operation_id,
                ModelCacheOperation.kind.in_(["download", "repair"]),
                ModelCacheOperation.state.in_(model_cache_states.LIVE),
            )
        )
        row = session.get(ModelCacheSet, set_digest)
        if row is not None and not sibling_work:
            row.state = "cached" if coverage_complete else "incomplete"
            row.verified_bytes = verified_bytes
            row.updated_at = now
            if coverage_complete:
                row.verified_at = now
                row.last_error = None
