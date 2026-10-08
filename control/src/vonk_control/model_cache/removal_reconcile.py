"""Removal reconcile."""

from __future__ import annotations

import time
from datetime import timedelta
from typing import TYPE_CHECKING, cast

from pydantic import ValidationError
from sqlalchemy import and_, or_, select, true
from sqlalchemy.orm import Session
from vonk_agent_protocol import ModelCacheCode

from .. import model_cache_states
from ..agent_operation_facts import aware as _aware
from ..artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactLifecycleError,
    check_removal_fence_nowait,
    dead_removal_identities,
    release_dead_removal_nowait,
    supersede_removal_nowait,
)
from ..categorized_errors import InvalidValue
from ..lifecycle import Effect, Outcome, Reported
from ..lifecycle.evidence import Residue
from ..logging import log_event, redact_text
from ..model_cache_contract import (
    ModelCacheDownloadPayload,
    ModelCacheRemovalPayload,
    ModelCacheRepairPayload,
    ModelCacheRetry,
)
from ..model_cache_progress import cache_phase, progress_document
from ..models import ArtifactLifecycleGate, Job, ModelCacheOperation
from ..operation_contract import AvailabilityOperationFailure
from ..recovery_policy import RecoveryPolicy
from .catalog_helpers import _iso
from .constants import _LOGGER, _TRANSFER_CLAIM_SECONDS
from .errors import ModelCacheStorageError, _ArtifactWriterBusy
from .persistence import (
    _cache_failure,
    _manifest_of,
    _operation_progress,
    _operation_removal,
    _store_operation_payload,
    _updated,
)

if TYPE_CHECKING:
    from .service import ModelCacheService


class RemovalReconcileMixin:
    """Removal reconcile behavior of the cache service."""

    def _model_removal_owner_snapshot(
        self,
        operation_id: str,
        *,
        fence: str,
        identity: ArtifactIdentity,
    ) -> ModelCacheRemovalPayload | None:
        cache = cast("ModelCacheService", self)
        with cache._session() as session:
            if not check_removal_fence_nowait(
                session,
                identity,
                owner_kind="model-cache-operation",
                owner_id=operation_id,
                fence=fence,
            ):
                return None
            operation = session.scalar(
                select(ModelCacheOperation)
                .where(ModelCacheOperation.id == operation_id)
                .execution_options(populate_existing=True)
                .with_for_update(nowait=True)
            )
            if (
                operation is None
                or operation.kind != "remove"
                or operation.state not in model_cache_states.LIVE
            ):
                return None
            payload = cache._removal_or_none(operation)
            if payload is None or payload.removal_fence != fence:
                return None  # unreadable: the step's entry retires the row
            return payload

    def _persist_model_removal_checkpoint(
        self,
        operation_id: str,
        *,
        fence: str,
        identity: ArtifactIdentity,
        expected_index: int,
        object_step: bool,
        pending_bytes: int | None,
        complete_step: bool,
    ) -> bool:
        cache = cast("ModelCacheService", self)
        now = cache._clock()
        with cache._session(write=True) as session:
            if not check_removal_fence_nowait(
                session,
                identity,
                owner_kind="model-cache-operation",
                owner_id=operation_id,
                fence=fence,
            ):
                return False
            operation = session.scalar(
                select(ModelCacheOperation)
                .where(ModelCacheOperation.id == operation_id)
                .execution_options(populate_existing=True)
                .with_for_update(nowait=True)
            )
            if (
                operation is None
                or operation.kind != "remove"
                or operation.state not in model_cache_states.LIVE
            ):
                return False
            payload = cache._removal_or_none(operation)
            if payload is None or payload.removal_fence != fence:
                return False  # unreadable: the step's entry retires the row
            current_index = payload.object_index if object_step else payload.set_index
            if current_index != expected_index:
                return False
            object_index = payload.object_index
            object_pending_bytes = payload.object_pending_bytes
            reclaimed_bytes = payload.reclaimed_bytes
            set_index = payload.set_index
            if object_step:
                if pending_bytes is None:
                    return False  # no byte checkpoint yet: the step measures again
                if complete_step:
                    if payload.object_pending_bytes != pending_bytes:
                        return False
                    object_index = expected_index + 1
                    object_pending_bytes = None
                    reclaimed_bytes += pending_bytes
                else:
                    object_pending_bytes = pending_bytes
            elif complete_step:
                set_index = expected_index + 1
            try:
                checkpoint = _updated(
                    payload,
                    retry=ModelCacheRetry(automatic_attempts=1, operator_retries=0),
                    failure=None,
                    object_index=object_index,
                    object_pending_bytes=object_pending_bytes,
                    reclaimed_bytes=reclaimed_bytes,
                    set_index=set_index,
                )
            except ValidationError:
                return False  # the step's entry retires the unreadable row
            previous = _operation_progress(operation)
            object_index = checkpoint.object_index
            set_index = checkpoint.set_index
            total_items = len(checkpoint.delete_objects) + len(checkpoint.selected)
            completed_items = object_index + set_index
            current_key: str | None = None
            if object_index < len(checkpoint.delete_objects):
                current_key = f"object:{checkpoint.delete_objects[object_index]}"
            elif set_index < len(checkpoint.selected):
                current_key = f"set:{checkpoint.selected[set_index]}"
            operation.progress = progress_document(
                cache._model_removal_progress(
                    phase="reclaiming",
                    total_items=total_items,
                    completed_items=completed_items,
                    reclaimed_bytes=checkpoint.reclaimed_bytes,
                    current_key=current_key,
                    previous=previous,
                    now=now,
                )
            )
            _store_operation_payload(operation, "remove", checkpoint)
            cache._lifecycle.renew(
                operation, cache._claim_owner, _TRANSFER_CLAIM_SECONDS, now
            )
        return True

    def _defer_model_removal(
        self, operation_id: str, *, detail: str, retry_after_seconds: int = 5
    ) -> None:
        cache = cast("ModelCacheService", self)
        now = cache._clock()
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
            if cache._payload_or_retire(operation, now=now) is None:
                return
            exhausted = (
                cache._lifecycle.lifecycle(operation, now).retry_count + 1
                >= RecoveryPolicy().max_failures
            )
            if exhausted:
                # Ending fences this executor; exact storage locks release its
                # gates after this transaction, without asserting deletion.
                cache._lifecycle.settle(
                    operation,
                    Reported(Outcome.FAILED, reason=detail),
                    now,
                )
                checkpoint = cache._removal_or_none(operation)
                if checkpoint is not None:
                    checkpoint = checkpoint.model_copy(
                        update={
                            "failure": AvailabilityOperationFailure(
                                code=ModelCacheCode.REMOVAL_WAIT,
                                detail=redact_text(detail)[:512],
                                retryable=False,
                                recovery_actions=[],
                            )
                        }
                    )
                    _store_operation_payload(operation, "remove", checkpoint)
            else:
                # The dependency's own hint is a floor; the core's bounded backoff
                # is the schedule (one policy for every kind, one clock).
                cache._lifecycle.settle(
                    operation,
                    Reported(
                        Outcome.UNKNOWN,
                        retry_after=now + timedelta(seconds=retry_after_seconds),
                        reason=detail,
                    ),
                    now,
                    interrupted=True,
                )
                next_retry = operation.next_action_at
                assert next_retry is not None
                delay = max(1, round((_aware(next_retry) - now).total_seconds()))
                checkpoint = cache._removal_or_none(operation)
                if checkpoint is None:
                    return
                artifact_key = (
                    f"object:{checkpoint.delete_objects[checkpoint.object_index]}"
                    if checkpoint.object_index < len(checkpoint.delete_objects)
                    else f"set:{checkpoint.selected[checkpoint.set_index]}"
                    if checkpoint.set_index < len(checkpoint.selected)
                    else "removal-finalization"
                )
                checkpoint = checkpoint.model_copy(
                    update={
                        "failure": _cache_failure(
                            ModelCacheCode.REMOVAL_WAIT,
                            "Automatic retry resumes this exact checkpoint when its "
                            "storage or ownership dependency clears. " + detail,
                            retryable=True,
                            recovery="inspect",
                            retry_time=_iso(next_retry),
                            retry_after_seconds=delay,
                            artifact_key=artifact_key,
                        )
                    }
                )
                operation.progress = progress_document(
                    cache_phase(
                        _operation_progress(operation), "reclaiming", now, waiting=True
                    )
                )
                operation.last_error = redact_text(detail)[:512]
                _store_operation_payload(operation, "remove", checkpoint)
        if exhausted:
            cache.reconcile_removal_gates()

    def reconcile_requested_removals(self, *, limit: int = 64) -> int:
        """Accepted newer downloads fence older removers before transfer dispatch."""
        cache = cast("ModelCacheService", self)
        if isinstance(cache._sessions, Session):
            return 0  # a borrowed SQL transaction cannot precede a storage lock
        with cache._session() as session:
            accepted_requests = []
            observed_request = False
            for operation in session.scalars(
                select(ModelCacheOperation)
                .where(
                    ModelCacheOperation.kind.in_(("download", "repair")),
                    ModelCacheOperation.state.in_(model_cache_states.LIVE),
                )
                .where(
                    ModelCacheOperation.id > cache._removal_request_after
                    if cache._removal_request_after is not None
                    else true()
                )
                .order_by(ModelCacheOperation.id)
                .limit(limit)
            ):
                observed_request = True
                cache._removal_request_after = operation.id
                payload = cache._payload_or_none(operation)
                if (
                    not isinstance(
                        payload, (ModelCacheDownloadPayload, ModelCacheRepairPayload)
                    )
                    or payload.cancellation is not None
                ):
                    continue
                manifest = _manifest_of(payload)
                identities = (
                    ArtifactIdentity("model-set", manifest.digest),
                    *(
                        ArtifactIdentity("model-object", digest)
                        for digest in sorted(
                            {item.sha256 for item in manifest.artifacts}
                        )
                    ),
                )
                identity_map = {(item.kind, item.sha256): item for item in identities}
                for gate in session.scalars(
                    select(ArtifactLifecycleGate).where(
                        ArtifactLifecycleGate.removal_owner_kind
                        == "model-cache-operation",
                        ArtifactLifecycleGate.removal_owner_id.is_not(None),
                        or_(
                            and_(
                                ArtifactLifecycleGate.artifact_kind == "model-set",
                                ArtifactLifecycleGate.artifact_sha256
                                == manifest.digest,
                            ),
                            and_(
                                ArtifactLifecycleGate.artifact_kind == "model-object",
                                ArtifactLifecycleGate.artifact_sha256.in_(
                                    tuple(item.sha256 for item in manifest.artifacts)
                                ),
                            ),
                        ),
                    )
                ):
                    accepted_requests.append(
                        (
                            operation.id,
                            identity_map[(gate.artifact_kind, gate.artifact_sha256)],
                        )
                    )
            if not observed_request:
                cache._removal_request_after = None
        changed = 0
        deadline = time.monotonic() + 0.25
        for request_id, identity in accepted_requests[:limit]:
            if time.monotonic() >= deadline:
                break

            def validate(
                requester: ModelCacheOperation | Job,
                remover: ModelCacheOperation | Job,
                fence: str,
                identity: ArtifactIdentity = identity,
            ) -> bool:
                if not isinstance(requester, ModelCacheOperation) or not isinstance(
                    remover, ModelCacheOperation
                ):
                    return False
                if (
                    requester.kind not in ("download", "repair")
                    or remover.kind != "remove"
                ):
                    return False
                payload = cache._payload_or_none(requester)
                removal = _operation_removal(remover)
                if (
                    not isinstance(
                        payload, (ModelCacheDownloadPayload, ModelCacheRepairPayload)
                    )
                    or payload.cancellation is not None
                    or isinstance(removal, Residue)
                ):
                    return False
                manifest = _manifest_of(payload)
                if (
                    requester.artifact_set_sha256 != manifest.digest
                    or payload.artifact_set_sha256 != manifest.digest
                    or requester.plan_digest != payload.plan_digest
                ):
                    return False
                wanted = (
                    manifest.digest == identity.sha256
                    if identity.kind == "model-set"
                    else any(
                        item.sha256 == identity.sha256 for item in manifest.artifacts
                    )
                )
                covered = identity.sha256 in (
                    removal.selected
                    if identity.kind == "model-set"
                    else removal.delete_objects
                )
                return wanted and covered and removal.removal_fence == fence

            def cancel(remover: ModelCacheOperation | Job, accepted_id: str) -> None:
                assert isinstance(remover, ModelCacheOperation)
                reason = f"Removal superseded by accepted model request {accepted_id}"
                cache._lifecycle.settle(
                    remover,
                    Reported(Outcome.CANCELLED, effect=Effect.UNKNOWN, reason=reason),
                    cache._clock(),
                )
                remover.last_error = reason

            try:
                with (
                    cache._model_storage_lock(
                        identity.sha256, model_set=identity.kind == "model-set"
                    ),
                    cache._session(write=True) as session,
                ):
                    changed += int(
                        supersede_removal_nowait(
                            session,
                            identity,
                            owner_kind="model-cache-operation",
                            request_id=request_id,
                            validate=validate,
                            cancel=cancel,
                            now=cache._clock(),
                        )
                    )
            except (_ArtifactWriterBusy, ArtifactLifecycleError, OSError):
                continue  # the queued request retries; no transfer slot is held
        return changed

    def reconcile_removal_gates(self, *, limit: int = 64) -> int:
        # A retained caller transaction cannot safely precede an artifact lock.
        cache = cast("ModelCacheService", self)
        if isinstance(cache._sessions, Session):
            return 0
        with cache._session() as session:
            identities = dead_removal_identities(
                session,
                owner_kind="model-cache-operation",
                limit=limit,
                after=cache._removal_gate_after,
            )
        if not identities:
            cache._removal_gate_after = None
        released = 0
        deadline = time.monotonic() + 0.25
        for identity in identities:
            if time.monotonic() >= deadline:
                break
            cache._removal_gate_after = (identity.kind, identity.sha256)
            try:
                with (
                    cache._model_storage_lock(
                        identity.sha256, model_set=identity.kind == "model-set"
                    ),
                    cache._session(write=True) as session,
                ):
                    changed = release_dead_removal_nowait(
                        session,
                        identity,
                        owner_kind="model-cache-operation",
                        now=cache._clock(),
                    )
                if changed:
                    released += 1
                    log_event(
                        _LOGGER,
                        "artifact.removal_gate_reconciled",
                        service="controller",
                        artifact_kind=identity.kind,
                        artifact_sha256=identity.sha256,
                    )
            except (_ArtifactWriterBusy, ArtifactLifecycleError, OSError) as error:
                # The next bounded worker pass retries; storage uncertainty keeps the fence.
                log_event(
                    _LOGGER,
                    "artifact.removal_gate_deferred",
                    service="controller",
                    artifact_kind=identity.kind,
                    artifact_sha256=identity.sha256,
                    code=getattr(error, "code", type(error).__name__),
                )
                continue
        return released

    def advance_removals(self, *, limit: int = 1) -> int:
        """Advance bounded durable model removals without holding transfer slots."""
        cache = cast("ModelCacheService", self)

        if not 1 <= limit <= 100:
            raise InvalidValue("model removal batch limit is invalid")
        cache.reconcile_removal_gates()
        now = cache._clock()
        with cache._session() as session:
            operation_ids = tuple(
                session.scalars(
                    select(ModelCacheOperation.id)
                    .where(
                        ModelCacheOperation.kind == "remove",
                        ModelCacheOperation.state.in_(model_cache_states.LIVE),
                    )
                    .where(
                        or_(
                            ModelCacheOperation.next_action_at.is_(None),
                            ModelCacheOperation.next_action_at <= now,
                        ),
                        # A row the startup adoption has not reached still carries
                        # its clock in the payload; it must not take a batch slot
                        # from a due one.  Retired once no such row can exist.
                        or_(
                            ModelCacheOperation.payload["retry"]["next_retry_at"]
                            .as_string()
                            .is_(None),
                            ModelCacheOperation.payload["retry"][
                                "next_retry_at"
                            ].as_string()
                            <= _iso(now),
                        ),
                    )
                    .order_by(ModelCacheOperation.updated_at, ModelCacheOperation.id)
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            )
        advanced = 0
        for operation_id in operation_ids:
            if advanced >= limit:
                break
            try:
                advanced += int(cache._advance_model_removal(operation_id, now=now))
            except ModelCacheStorageError as error:
                if error.code != ModelCacheCode.PAYLOAD_INVALID:
                    cache._defer_model_removal(operation_id, detail=error.detail)
                    continue
                # A corrupt owner's document cannot be rewritten into a valid
                # default. Preserve it and its fences for inspection, isolate
                # its failure, and allow unrelated eligible work to continue.
                with cache._session(write=True) as session:
                    row = session.scalar(
                        select(ModelCacheOperation)
                        .where(
                            ModelCacheOperation.id == operation_id,
                            ModelCacheOperation.kind == "remove",
                        )
                        .with_for_update(skip_locked=True)
                    )
                    if row is not None:
                        cache._lifecycle.fail_corrupt(
                            row, f"{error.code}: {error.detail}", now
                        )
                log_event(
                    _LOGGER,
                    ModelCacheCode.REMOVAL_INVALID,
                    service="controller",
                    operation_id=operation_id,
                    code=error.code,
                    detail=error.detail,
                )
        return advanced
