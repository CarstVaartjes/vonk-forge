"""Removal execution."""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, cast

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from vonk_agent_protocol import Disposition, InvalidRequestError, UnknownOutcomeError

from .. import model_cache_states
from ..agent_operation_facts import aware as _aware
from ..artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactLifecycleError,
    check_removal_fence_nowait,
    clear_removal,
    lock_removal_fences,
    retryable_artifact_database_error,
)
from ..artifact_reference_scan import (
    ArtifactReferenceFinding,
    _reason_projection,
    model_set_reference_findings,
)
from ..failure_classification import disposition
from ..lifecycle import Effect, Outcome, Reported
from ..lifecycle.evidence import Residue
from ..model_cache_contract import (
    ModelCacheRemovalPayload,
    ModelCacheRemovalResult,
    ModelCacheSetState,
)
from ..model_cache_progress import cache_phase, progress_document
from ..models import (
    ArtifactDistributionAssignment,
    FleetProfileApplication,
    Job,
    ModelCacheOperation,
    ModelCacheSet,
    ModelCacheSetArtifact,
    RecipeInstallation,
)
from ..validation_detail import validation_error_detail
from .constants import _RETRY_BASE_SECONDS, REMOVAL_BATCH_SECONDS, SCHEMA_VERSION
from .errors import ModelCacheStorageRefused, _ArtifactWriterBusy
from .persistence import (
    _operation_progress,
    _operation_removal,
    _store_operation_payload,
    _updated,
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
        self,
        operation_id: str,
        *,
        now: datetime | None = None,
        deadline: float | None = None,
    ) -> bool:
        cache = cast("ModelCacheService", self)
        observed_at = now or cache._clock()
        try:
            if not cache._advance_fleet_removal(operation_id, now=observed_at):
                return False
            changed = cache._advance_model_removal_step(
                operation_id, now=observed_at, deadline=deadline
            )
            if changed:
                return True
            with cache._session() as session:
                operation = session.get(ModelCacheOperation, operation_id)
                if operation is None or operation.state not in model_cache_states.LIVE:
                    return False
                due = operation.next_action_at
                if due is not None and _aware(due) > _aware(observed_at):
                    return False
            # A live owner with no recorded wait lost an observation of its
            # fence/checkpoint. Storage locks and transactions have unwound.
            cache._defer_model_removal(
                operation_id, detail="model removal ownership observation unavailable"
            )
        except (DBAPIError, ArtifactLifecycleError) as error:
            translated = (
                retryable_artifact_database_error(error)
                if isinstance(error, DBAPIError)
                else error
            )
            try:
                cache._defer_model_removal(
                    operation_id,
                    detail=translated.detail
                    if translated is not None
                    else "model removal database observation unavailable",
                    # Contention is a wait; an unconfirmed observation spends one.
                    cause=translated
                    if translated is not None
                    and disposition(translated) is Disposition.WAIT
                    else Disposition.OBSERVE,
                )
            except DBAPIError:
                # The creation timestamp remains the durable budget anchor;
                # failed retry recording cannot extend the request lifetime.
                return False
        except (InvalidRequestError, UnknownOutcomeError, OSError, ValueError) as error:
            cache._defer_model_removal(
                operation_id,
                detail=validation_error_detail(error)
                if isinstance(error, ValidationError)
                else str(error),
                cause=disposition(error),
            )
        return False

    def _advance_model_removal_step(
        self,
        operation_id: str,
        *,
        now: datetime | None = None,
        deadline: float | None = None,
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
                # The terminal writer uses its own short transaction. Release
                # this read's FOR UPDATE first, or PostgreSQL skips our own row.
                session.rollback()
                cache._retire_removal(operation_id, checkpoint, now=now)
                return False
            # One clock: the column (a legacy payload clock is adopted by the
            # adapter, so a row the startup adoption missed is still honoured).
            due = cache._lifecycle.lifecycle(operation, now).next_action_at
            if due is not None and due > _aware(now):
                return False
            pending_scope = checkpoint.scope_pending
            fence = checkpoint.removal_fence
            object_index = checkpoint.object_index
            set_index = checkpoint.set_index
            delete_objects = checkpoint.delete_objects
            selected_sets = checkpoint.selected
            findings = (
                model_set_reference_findings(
                    session, tuple(str(item) for item in selected_sets)
                )
                if not pending_scope
                else {}
            )
            in_use = {
                digest: owners
                for digest, owners in _reason_projection(findings).items()
                if owners
            }
            newer, owned = _newer_owners(session, operation.created_at, findings)
        if pending_scope:
            if not cache._observe_model_removal_scope(operation_id):
                return False
            # The scope and fences are committed before any byte effect.
            # Continue once, through the same executor, with the resolved scope.
            return cache._advance_model_removal_step(
                operation_id, now=now, deadline=deadline
            )
        # Newer intent wins. A removal that has removed nothing yet ends. One that
        # already removed something yields the sets a newer active owner holds
        # (``owned``): it neither waits on that owner nor removes what it needs,
        # and it still removes every other target.
        if newer is not None and _untouched(checkpoint):
            cache._supersede_waiting_removal(
                operation_id, reason=f"Removal superseded by newer {newer}", now=now
            )
            return True
        owned = owned if newer is not None else frozenset()
        blocking = {
            digest: owners for digest, owners in in_use.items() if digest not in owned
        }
        if blocking:
            first_digest = min(blocking)
            cache._defer_model_removal(
                operation_id,
                detail=f"waiting for model cache set {first_digest} to be released by: "
                + ", ".join(blocking[first_digest][:4]),
                retry_after_seconds=_RETRY_BASE_SECONDS,
                cause=Disposition.WAIT,
            )
            return False
        if object_index < len(delete_objects):
            # A model is a set of files: delete this removal's exclusive objects
            # in a time-bounded batch, one checkpoint per batch. A crash before
            # the checkpoint only undercounts bytes; the unlinks are idempotent.
            # The worker step shares one deadline; each removal still deletes
            # at least one object.
            if deadline is None:
                deadline = time.monotonic() + REMOVAL_BATCH_SECONDS
            completed = 0
            reclaimed = 0
            failure: Exception | None = None
            batch = tuple(str(item) for item in delete_objects[object_index:])
            retained: set[str] = set()
            for position, digest in enumerate(batch):
                identity = ArtifactIdentity("model-object", digest)
                try:
                    with cache._model_storage_lock(digest):
                        with cache._session() as session:
                            if position % _OBJECT_BATCH_LIMIT == 0:
                                # One indexed query per slice of the objects
                                # this step is about to reach: the newer
                                # request wins, so an object a set outside
                                # this removal's own sets lists is yielded.
                                retained = _externally_listed(
                                    session,
                                    batch[position : position + _OBJECT_BATCH_LIMIT],
                                    [
                                        item
                                        for item in selected_sets
                                        if item not in owned
                                    ],
                                )
                            if digest in retained:
                                completed += 1
                                continue
                            if not check_removal_fence_nowait(
                                session,
                                identity,
                                owner_kind="model-cache-operation",
                                owner_id=operation_id,
                                fence=fence,
                            ):
                                break
                        size = cache._model_object_size(digest)
                        cache._remove_model_object_files(digest)
                except (
                    _ArtifactWriterBusy,
                    ArtifactLifecycleError,
                    ModelCacheStorageRefused,
                    OSError,
                ) as error:
                    failure = error
                    break
                completed += 1
                reclaimed += size
                if time.monotonic() >= deadline:
                    break
            if completed and not cache._persist_model_removal_checkpoint(
                operation_id,
                fence=fence,
                expected_index=object_index,
                object_step=True,
                completed=completed,
                reclaimed_bytes=reclaimed,
            ):
                return False
            if isinstance(failure, _ArtifactWriterBusy):
                cache._defer_model_removal(
                    operation_id,
                    detail=failure.detail,
                    retry_after_seconds=failure.retry_after_seconds or 5,
                    cause=Disposition.WAIT,
                )
                return False
            if isinstance(failure, ArtifactLifecycleError):
                cache._defer_model_removal(
                    operation_id, detail=failure.detail, retry_after_seconds=5
                )
                return False
            if isinstance(failure, ModelCacheStorageRefused):
                cache._defer_model_removal(
                    operation_id, detail=failure.detail, cause=failure
                )
                return False
            if isinstance(failure, OSError):
                cache._defer_model_removal(
                    operation_id,
                    detail=f"{type(failure).__name__}: {failure}",
                    cause=failure,
                )
                return False
            return bool(completed)
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
                    if set_digest not in owned:
                        cache._remove_model_partial_set(set_digest)
                    return cache._persist_model_removal_checkpoint(
                        operation_id,
                        fence=fence,
                        expected_index=set_index,
                        object_step=False,
                        completed=1,
                        reclaimed_bytes=0,
                    )
            except _ArtifactWriterBusy as error:
                cache._defer_model_removal(
                    operation_id,
                    detail=error.detail,
                    retry_after_seconds=error.retry_after_seconds or 5,
                    cause=Disposition.WAIT,
                )
                return False
            except ArtifactLifecycleError as error:
                cache._defer_model_removal(
                    operation_id, detail=error.detail, retry_after_seconds=5
                )
                return False
            except OSError as error:
                cache._defer_model_removal(
                    operation_id, detail=f"{type(error).__name__}: {error}", cause=error
                )
                return False
        try:
            # Stat outside the finish transaction (it holds the row and the
            # fences): which targets are still stored decides a rewind.
            stored = tuple(
                str(item)
                for item in delete_objects
                if cache._model_object_size(str(item)) > 0
            )
        except ModelCacheStorageRefused as error:
            cache._defer_model_removal(operation_id, detail=error.detail, cause=error)
            return False
        except OSError as error:
            cache._defer_model_removal(
                operation_id, detail=f"{type(error).__name__}: {error}", cause=error
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
            if identities and not lock_removal_fences(
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
            wait = cache._finish_model_removal_in_session(
                session, operation, now=now, owned=owned, stored=stored
            )
        if wait == _REWOUND:
            return True  # progress, not a wait: the next step re-runs the batch
        if wait is not None:
            # The removal stays fenced and is retried: nothing is half finished.
            cache._defer_model_removal(
                operation_id, detail=wait, cause=Disposition.WAIT
            )
            return False
        return True

    def _supersede_waiting_removal(
        self, operation_id: str, *, reason: str, now: datetime
    ) -> None:
        cache = cast("ModelCacheService", self)
        with cache._session(write=True) as session:
            operation = session.scalar(
                select(ModelCacheOperation)
                .where(
                    ModelCacheOperation.id == operation_id,
                    ModelCacheOperation.kind == "remove",
                    ModelCacheOperation.state.in_(model_cache_states.LIVE),
                )
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if operation is None:
                return
            checkpoint = cache._removal_or_none(operation)
            if checkpoint is None or not _untouched(checkpoint):
                return
            cache._lifecycle.settle(
                operation,
                Reported(Outcome.CANCELLED, effect=Effect.UNKNOWN, reason=reason),
                now,
            )
            operation.last_error = reason
        cache.reconcile_removal_gates()

    def _finish_model_removal_in_session(
        self,
        session: Session,
        operation: ModelCacheOperation,
        *,
        now: datetime,
        owned: frozenset[str] = frozenset(),
        stored: tuple[str, ...] = (),
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
            *(
                ArtifactIdentity("model-object", str(item))
                for item in sorted(
                    set(delete_objects)
                    | (
                        set()
                        if checkpoint.fleet is None
                        else checkpoint.fleet.removal_objects
                    )
                )
            ),
        )
        # A set outside this removal that lists a target keeps that object: the
        # removal yields per object (see the executor) and still completes.
        # An object kept for a set that was owned then but is not now would be
        # stranded by deleting that set's rows: rewind, so the next step runs
        # the batch again with the current owners and unlinks it.
        released = [str(item) for item in selected if item not in owned]
        listed = _externally_listed(session, stored, released) if stored else set()
        stranded = [item for item in stored if item not in listed]
        if stranded:
            _store_operation_payload(
                operation,
                operation.kind,
                _updated(checkpoint, object_index=0, set_index=0),
            )
            return _REWOUND
        for set_digest in selected:
            row = session.get(ModelCacheSet, set_digest)
            if set_digest in owned:
                # A newer live owner writes this set's row into its own
                # operation; deleting it would break that write. Keep the
                # row, but never as ``cached``: its objects may be gone.
                if row is not None:
                    row.state = ModelCacheSetState.NEEDS_REPAIR
                    row.last_error = "a removal yielded this set to a newer request"
                    row.updated_at = now
                continue
            session.query(ModelCacheSetArtifact).filter(
                ModelCacheSetArtifact.artifact_set_sha256 == set_digest
            ).delete(synchronize_session=False)
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
            removed_entries=[item for item in selected if item not in owned],
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


_OWNER_ROWS: dict[
    str,
    type[
        ArtifactDistributionAssignment
        | FleetProfileApplication
        | Job
        | ModelCacheOperation
        | RecipeInstallation
    ],
] = {
    "artifact-distribution-assignment": ArtifactDistributionAssignment,
    "fleet-profile-application": FleetProfileApplication,
    "model-cache-operation": ModelCacheOperation,
    "recipe-installation": RecipeInstallation,
    "run-switch-operation": Job,
}


def _externally_listed(
    session: Session, digests: tuple[str, ...], selected: Sequence[str]
) -> set[str]:
    """Objects among ``digests`` that a set outside ``selected`` lists."""

    return set(
        session.scalars(
            select(ModelCacheSetArtifact.artifact_sha256)
            .where(
                ModelCacheSetArtifact.artifact_sha256.in_(digests),
                ModelCacheSetArtifact.artifact_set_sha256.not_in(tuple(selected)),
            )
            .distinct()
        )
    )


_OBJECT_BATCH_LIMIT = 64
_REWOUND = "rewound: a released set still has stored objects"


def _untouched(checkpoint: ModelCacheRemovalPayload) -> bool:
    """Whether the removal has not removed (or begun removing) anything yet."""

    return (
        checkpoint.object_index == 0
        and checkpoint.set_index == 0
        and checkpoint.reclaimed_bytes == 0
    )


def _newer_owners(
    session: Session,
    accepted_at: datetime,
    findings: dict[str, tuple[ArtifactReferenceFinding, ...]],
) -> tuple[str | None, frozenset[str]]:
    """The reason of an active owner accepted after this removal (else ``None``)
    and the sets such an owner holds."""

    reason: str | None = None
    owned: set[str] = set()
    ids: dict[str, set[str]] = {}
    for entries in findings.values():
        for finding in entries:
            if (
                finding.classification == "active-work"
                and finding.owner_kind in _OWNER_ROWS
            ):
                ids.setdefault(finding.owner_kind, set()).add(finding.owner_id)
    created = {}
    for kind, owner_ids in ids.items():
        model = _OWNER_ROWS[kind]
        for owner_id, created_at in session.execute(
            select(model.id, model.created_at).where(model.id.in_(owner_ids))
        ):
            created[(kind, owner_id)] = _aware(created_at)
    for digest, entries in findings.items():
        for finding in entries:
            made = created.get((finding.owner_kind, finding.owner_id))
            if (
                finding.classification == "active-work"
                and made is not None
                and made > _aware(accepted_at)
            ):
                reason = reason or finding.reason
                owned.add(digest)
    return reason, frozenset(owned)
