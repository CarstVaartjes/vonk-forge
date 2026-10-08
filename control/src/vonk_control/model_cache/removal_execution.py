"""Removal execution."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from .. import model_cache_states
from ..agent_operation_facts import aware as _aware
from ..artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactLifecycleError,
    clear_removal,
    lock_removal_fences,
    retryable_artifact_database_error,
)
from ..artifact_reference_scan import model_set_reference_reasons
from ..lifecycle import Outcome, Reported
from ..lifecycle.evidence import Residue
from ..model_cache_contract import ModelCacheRemovalResult
from ..model_cache_progress import cache_phase, progress_document
from ..models import ModelCacheOperation, ModelCacheSet, ModelCacheSetArtifact
from .constants import _RETRY_BASE_SECONDS, SCHEMA_VERSION
from .errors import _ArtifactWriterBusy
from .persistence import (
    _operation_progress,
    _operation_removal,
    _store_operation_payload,
)

if TYPE_CHECKING:
    from .service import ModelCacheService


class RemovalExecutionMixin:
    """Removal execution behavior of the cache service."""

    def _retire_removal(
        self, operation_id: str, residue: Residue, *, now: datetime
    ) -> None:
        cache = cast("ModelCacheService", self)
        with cache._session(write=True) as session:
            row = session.scalar(
                select(ModelCacheOperation)
                .where(
                    ModelCacheOperation.id == operation_id,
                    ModelCacheOperation.kind == "remove",
                )
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if row is not None:
                cache._retire_unreadable(row, residue, now=now)

    def _advance_model_removal(
        self, operation_id: str, *, now: datetime | None = None
    ) -> bool:
        cache = cast("ModelCacheService", self)
        try:
            return cache._advance_model_removal_step(operation_id, now=now)
        except (DBAPIError, ArtifactLifecycleError) as error:
            translated = (
                retryable_artifact_database_error(error)
                if isinstance(error, DBAPIError)
                else error
                if error.retryable
                else None
            )
            if translated is None:
                raise
            # The failed transaction and any artifact lock have unwound before
            # recording a retry. A held owner row is skipped, never waited on.
            try:
                cache._defer_model_removal(operation_id, detail=translated.detail)
            except DBAPIError as retry_error:
                if retryable_artifact_database_error(retry_error) is None:
                    raise
            return False

    def _advance_model_removal_step(
        self, operation_id: str, *, now: datetime | None = None
    ) -> bool:
        cache = cast("ModelCacheService", self)
        now = now or cache._clock()
        with cache._session() as session:
            # Observe contention before reference scans or storage effects.
            operation = session.get(
                ModelCacheOperation, operation_id, with_for_update={"nowait": True}
            )
            if operation is None or operation.kind != "remove":
                return False
            if operation.state not in model_cache_states.LIVE:
                return False
            checkpoint = _operation_removal(operation)
            if isinstance(checkpoint, Residue):
                # Damaged and not re-derivable: the row ends (kept for
                # inspection) and unrelated removals carry on.
                cache._retire_removal(operation_id, checkpoint, now=now)
                return False
            # One clock: the column (a legacy payload clock is adopted by the
            # adapter, so a row the startup adoption missed is still honoured).
            due = cache._lifecycle.lifecycle(operation, now).next_action_at
            if due is not None and due > _aware(now):
                return False
            fence = checkpoint.removal_fence
            object_index = checkpoint.object_index
            set_index = checkpoint.set_index
            delete_objects = checkpoint.delete_objects
            selected_sets = checkpoint.selected
            in_use = (
                {
                    digest: owners
                    for digest, owners in model_set_reference_reasons(
                        session,
                        tuple(str(item) for item in selected_sets),
                    ).items()
                    if owners
                }
                if object_index < len(delete_objects) or set_index < len(selected_sets)
                else {}
            )
        if in_use:
            first_digest = min(in_use)
            cache._defer_model_removal(
                operation_id,
                detail=f"waiting for model cache set {first_digest} to be released by: "
                + ", ".join(in_use[first_digest][:4]),
                retry_after_seconds=_RETRY_BASE_SECONDS,
            )
            return False
        if object_index < len(delete_objects):
            digest = str(delete_objects[object_index])
            identity = ArtifactIdentity("model-object", digest)
            try:
                with cache._model_storage_lock(digest):
                    current = cache._model_removal_owner_snapshot(
                        operation_id, fence=fence, identity=identity
                    )
                    if current is None:
                        return False
                    if current.object_index != object_index:
                        return False
                    pending_bytes = current.object_pending_bytes
                    if pending_bytes is None:
                        pending_bytes = cache._model_object_size(digest)
                    if not cache._persist_model_removal_checkpoint(
                        operation_id,
                        fence=fence,
                        identity=identity,
                        expected_index=object_index,
                        object_step=True,
                        pending_bytes=pending_bytes,
                        complete_step=False,
                    ):
                        return False
                    cache._remove_model_object_files(digest)
                    return cache._persist_model_removal_checkpoint(
                        operation_id,
                        fence=fence,
                        identity=identity,
                        expected_index=object_index,
                        object_step=True,
                        pending_bytes=pending_bytes,
                        complete_step=True,
                    )
            except _ArtifactWriterBusy as error:
                cache._defer_model_removal(
                    operation_id,
                    detail=error.detail,
                    retry_after_seconds=error.retry_after_seconds or 5,
                )
                return False
            except ArtifactLifecycleError as error:
                if error.retryable:
                    cache._defer_model_removal(
                        operation_id, detail=error.detail, retry_after_seconds=5
                    )
                    return False
                raise
            except OSError as error:
                cache._defer_model_removal(
                    operation_id, detail=f"{type(error).__name__}: {error}"
                )
                return False
        if set_index < len(selected_sets):
            set_digest = str(selected_sets[set_index])
            identity = ArtifactIdentity("model-set", set_digest)
            try:
                with cache._model_storage_lock(set_digest, model_set=True):
                    current = cache._model_removal_owner_snapshot(
                        operation_id, fence=fence, identity=identity
                    )
                    if current is None or current.set_index != set_index:
                        return False
                    cache._remove_model_partial_set(set_digest)
                    return cache._persist_model_removal_checkpoint(
                        operation_id,
                        fence=fence,
                        identity=identity,
                        expected_index=set_index,
                        object_step=False,
                        pending_bytes=None,
                        complete_step=True,
                    )
            except _ArtifactWriterBusy as error:
                cache._defer_model_removal(
                    operation_id,
                    detail=error.detail,
                    retry_after_seconds=error.retry_after_seconds or 5,
                )
                return False
            except ArtifactLifecycleError as error:
                if error.retryable:
                    cache._defer_model_removal(
                        operation_id, detail=error.detail, retry_after_seconds=5
                    )
                    return False
                raise
            except OSError as error:
                cache._defer_model_removal(
                    operation_id, detail=f"{type(error).__name__}: {error}"
                )
                return False
        with cache._session(write=True) as session:
            identities = (
                *(ArtifactIdentity("model-set", str(item)) for item in selected_sets),
                *(
                    ArtifactIdentity("model-object", str(item))
                    for item in delete_objects
                ),
            )
            if not lock_removal_fences(
                session,
                identities,
                owner_kind="model-cache-operation",
                owner_id=operation_id,
                fence=fence,
                now=now,
            ):
                return False
            operation = session.scalar(
                select(ModelCacheOperation)
                .where(ModelCacheOperation.id == operation_id)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
            if operation is None or operation.kind != "remove":
                return False
            payload = cache._payload_or_retire(operation, now=now)
            if payload is None or payload.removal_fence != fence:
                return False
            if operation.state not in model_cache_states.LIVE:
                return False
            wait = cache._finish_model_removal_in_session(session, operation, now=now)
        if wait is not None:
            # The removal stays fenced and is retried: nothing is half finished.
            cache._defer_model_removal(operation_id, detail=wait)
            return False
        return True

    def _finish_model_removal_in_session(
        self, session: Session, operation: ModelCacheOperation, *, now: datetime
    ) -> str | None:
        """Finish a fully reconciled removal; else say what it waits for.

        Returns ``None`` when the removal finished (or its unreadable row was
        retired) and a wait reason when it cannot finish yet: the caller records
        that as an uncertain outcome and the lifecycle core retries it with
        backoff, so a changed scope never ends in a raise.
        """
        cache = cast("ModelCacheService", self)

        checkpoint = _operation_removal(operation)
        if isinstance(checkpoint, Residue):
            cache._retire_unreadable(operation, checkpoint, now=now)
            return None
        selected = checkpoint.selected
        delete_objects = checkpoint.delete_objects
        if checkpoint.object_index != len(
            delete_objects
        ) or checkpoint.set_index != len(selected):
            return "model removal cannot finish before every target is reconciled"
        fence = checkpoint.removal_fence
        identities = (
            *(ArtifactIdentity("model-set", str(item)) for item in selected),
            *(ArtifactIdentity("model-object", str(item)) for item in delete_objects),
        )
        for digest in delete_objects:
            external = session.scalar(
                select(ModelCacheSetArtifact.artifact_set_sha256)
                .where(
                    ModelCacheSetArtifact.artifact_sha256 == digest,
                    ModelCacheSetArtifact.artifact_set_sha256.not_in(selected)
                    if selected
                    else ModelCacheSetArtifact.artifact_set_sha256.is_not(None),
                )
                .limit(1)
            )
            if external is not None:
                # A new set references a target: the removal stays fenced and
                # waits (nothing was deleted yet in this transaction).
                return "a new model set references a removal target; removal remains fenced"
        for set_digest in selected:
            session.query(ModelCacheSetArtifact).filter(
                ModelCacheSetArtifact.artifact_set_sha256 == set_digest
            ).delete(synchronize_session=False)
            row = session.get(ModelCacheSet, set_digest)
            if row is not None:
                session.delete(row)
        clear_removal(
            session,
            identities,
            owner_kind="model-cache-operation",
            owner_id=operation.id,
            fence=fence,
            now=now,
        )
        result = ModelCacheRemovalResult(
            schema_version=SCHEMA_VERSION,
            removed_entries=list(selected),
            reclaimed_bytes=checkpoint.reclaimed_bytes,
            cancelled_operations=[],
        )
        operation.progress = progress_document(
            cache_phase(_operation_progress(operation), "completed", now)
        )
        _store_operation_payload(
            operation,
            "remove",
            checkpoint.model_copy(update={"result": result, "failure": None}),
        )
        operation.last_error = None
        cache._lifecycle.complete(operation, Reported(Outcome.DONE), now)
        return None
