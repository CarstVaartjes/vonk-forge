"""Download recovery and access rechecks."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, cast

from sqlalchemy import select
from vonk_agent_protocol import (
    InvalidRequestReason,
    LifecycleState,
    ModelAccessReason,
    ModelCacheCode,
)

from .. import model_cache_states
from ..lifecycle import Effect, Outcome, Reported
from ..lifecycle.model_cache import ModelCacheAdapter
from ..model_cache_contract import (
    ModelCacheAccessRecheck,
    ModelCacheDownloadResult,
    ModelCacheOperationPhase,
    ModelCacheTransfer,
)
from ..model_cache_progress import cache_phase, progress_document
from ..models import ModelCacheOperation
from ..strict_json import serialize_json_value
from .artifacts import (
    ArtifactSetManifest,
    _optional_digest,
)
from .constants import _TRANSFER_CLAIM_SECONDS
from .errors import (
    ModelCacheConflictInvalid,
    ModelCacheNotFoundInvalid,
)
from .persistence import (
    _manifest_of,
    _operation_progress,
    _store_operation_payload,
    _updated,
    _write_operation_payload,
)
from .source_helpers import _request_key
from .views import CacheOperationView

if TYPE_CHECKING:
    from .service import ModelCacheService


class RetryMixin:
    """Download recovery behavior of the cache service."""

    @staticmethod
    def _access_retry_delay(operation_id: str, checks: int) -> int:
        """Five-minute exponential backoff with stable jitter, capped at one hour."""
        ceiling = min(3600, 360 * 2 ** min(max(checks - 1, 0), 4))
        jitter = (
            int.from_bytes(
                hashlib.sha256(f"{operation_id}:{checks}".encode()).digest()[:2], "big"
            )
            / 65535
        )
        return round(ceiling * (5 + jitter) / 6)

    def retry(
        self,
        operation_id: str,
        *,
        actor: str,
        request_key: str,
    ) -> CacheOperationView:
        """Queue one operator retry from the persisted exact cache operation."""
        cache = cast("ModelCacheService", self)

        request_key = _request_key(request_key)
        with cache._lock, cache._session(write=True) as session:
            previous = session.get(
                ModelCacheOperation, operation_id, with_for_update=True
            )
            if previous is None:
                raise ModelCacheNotFoundInvalid(
                    ModelCacheCode.OPERATION_MISSING, "cache operation was not found"
                )
            existing = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == request_key
                )
            )
            if existing is not None:
                if (
                    existing.kind != previous.kind
                    or existing.plan_digest != previous.plan_digest
                    or existing.artifact_set_sha256 != previous.artifact_set_sha256
                ):
                    raise ModelCacheConflictInvalid(
                        ModelCacheCode.REQUEST_KEY_REUSED,
                        "request key was already used for another cache operation",
                    )
                return cache._operation_view(existing)
            if (
                previous.kind not in {"download", "repair"}
                or previous.state != LifecycleState.FAILED
            ):
                raise ModelCacheConflictInvalid(
                    ModelCacheCode.OPERATION_NOT_RETRYABLE,
                    "cache operation is not retryable",
                )
            previous_payload = cache._transfer_or_retire(previous)
            if previous_payload is None:
                # Nothing readable to retry from: the unreadable operation stays
                # ended (kept for inspection) and the caller sees it as it is; a
                # new download request starts the work again.
                return cache._operation_view(previous)
            now = cache._clock()
            # The set is the manifest's own digest; the column is bookkeeping.
            previous_set = (
                previous.artifact_set_sha256 or _manifest_of(previous_payload).digest
            )
            cache._require_model_sets_open(session, (previous_set,), now=now)
            payload = previous_payload.model_copy(
                update={
                    "retry": previous_payload.retry.model_copy(
                        update={
                            "automatic_attempts": 1,
                            "operator_retries": previous_payload.retry.operator_retries
                            + 1,
                        }
                    ),
                    "retry_of": previous.id,
                }
            )
            previous_progress = _operation_progress(previous)
            operation = ModelCacheAdapter.new_operation(
                request_key=request_key,
                schema_version=2,
                kind=previous.kind,
                attempt=1,
                artifact_set_sha256=previous_set,
                plan_digest=previous.plan_digest,
                payload=serialize_json_value(
                    _write_operation_payload(previous.kind, payload)
                ),
                progress=progress_document(
                    cache_phase(previous_progress, "queued", now)
                ),
                actor=actor,
                current_artifact_key=previous.current_artifact_key,
                created_at=now,
                updated_at=now,
            )
            session.add(operation)
            session.flush()
            return cache._operation_view(operation)

    def check_access_and_resume(
        self,
        operation_id: str,
        *,
        actor: str,
        request_key: str,
        artifact_set_sha256: str,
        plan_digest: str,
    ) -> CacheOperationView:
        """Queue an immediate access check through the normal transfer worker.

        Automatic waits and operator checks retain the exact transfer, source
        and configured credential. Cancellation and newer intent win.
        """
        cache = cast("ModelCacheService", self)

        request_key = _request_key(request_key)
        requested_set = _optional_digest(artifact_set_sha256)
        requested_plan = _optional_digest(plan_digest)
        assert requested_set is not None and requested_plan is not None
        auth_codes = set(ModelAccessReason)
        with cache._lock, cache._session(write=True) as session:
            previous = session.get(
                ModelCacheOperation, operation_id, with_for_update=True
            )
            if previous is None:
                raise ModelCacheNotFoundInvalid(
                    ModelCacheCode.OPERATION_MISSING, "cache operation was not found"
                )
            if (
                previous.artifact_set_sha256 != requested_set
                or previous.plan_digest != requested_plan
            ):
                raise ModelCacheConflictInvalid(
                    ModelCacheCode.IDENTITY_MISMATCH,
                    "access recheck identity does not match the persisted operation",
                )
            existing = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == request_key
                )
            )
            if existing is not None:
                retained = cache._payload_or_none(existing)
                if (
                    existing.actor == actor
                    and existing.kind == previous.kind
                    and existing.artifact_set_sha256 == requested_set
                    and existing.plan_digest == requested_plan
                    and retained is not None
                    and retained.resume_of == previous.id
                ):
                    return cache._operation_view(existing)
                raise ModelCacheConflictInvalid(
                    ModelCacheCode.REQUEST_KEY_REUSED,
                    "request key was already used for another cache operation",
                )
            previous_payload = cache._transfer_or_retire(previous)
            if previous_payload is None:
                raise ModelCacheConflictInvalid(
                    ModelCacheCode.DOCUMENT_UNREADABLE,
                    "cannot record access recheck in unreadable operation",
                )
            failure = cache._canonical_failure(previous)
            if (
                previous.kind not in {"download", "repair"}
                or previous.state not in model_cache_states.WAITING_OR_FAILED
                or previous_payload.cancellation is not None
                or failure is None
                or failure.code not in auth_codes
            ):
                raise ModelCacheConflictInvalid(
                    ModelCacheCode.ACCESS_RECHECK_UNAVAILABLE,
                    "the operation is not waiting for Hugging Face access",
                )
            newer = session.scalar(
                select(ModelCacheOperation.id)
                .where(
                    ModelCacheOperation.artifact_set_sha256 == requested_set,
                    ModelCacheOperation.created_at > previous.created_at,
                )
                .limit(1)
            )
            if newer is not None:
                raise ModelCacheConflictInvalid(
                    ModelCacheCode.ACCESS_RECHECK_UNAVAILABLE,
                    "a newer request owns this artifact set",
                )

            # One durable request row precedes HTTP effects. The normal worker
            # checks the exact retained source using only the configured token.
            now = cache._clock()
            cache._require_model_sets_open(session, (requested_set,), now=now)
            payload = _updated(
                previous_payload,
                failure=None,
                blockers=[],
                claim=None,
                result=None,
                resume_of=previous.id,
                retry=previous_payload.retry.model_copy(
                    update={"automatic_attempts": 1, "access_checks": 0}
                ),
                access_recheck=ModelCacheAccessRecheck(request_key=request_key),
            )
            operation = ModelCacheAdapter.new_operation(
                request_key=request_key,
                schema_version=2,
                kind=previous.kind,
                attempt=1,
                artifact_set_sha256=requested_set,
                plan_digest=requested_plan,
                payload=serialize_json_value(
                    _write_operation_payload(previous.kind, payload)
                ),
                progress=progress_document(
                    cache_phase(
                        _operation_progress(previous), LifecycleState.QUEUED.value, now
                    )
                ),
                actor=actor,
                current_artifact_key=previous.current_artifact_key,
                created_at=now,
                updated_at=now,
            )
            session.add(operation)
            session.flush()
            cache._lifecycle.reopen(previous, now)
            cache._lifecycle.settle(
                previous,
                Reported(
                    Outcome.CANCELLED,
                    effect=Effect.UNKNOWN,
                    reason=InvalidRequestReason.SUPERSEDED.value,
                ),
                now,
            )
            return cache._operation_view(operation)

    def _mark_running(self, operation_id: str) -> int | None:
        """The worker starts (or resumes) the transfer: claim or renew, then run."""
        cache = cast("ModelCacheService", self)

        stop = cache._transfer_stop(operation_id)
        now = cache._clock()
        with cache._session(write=True) as session:
            operation = session.get(
                ModelCacheOperation, operation_id, with_for_update=True
            )
            if operation is None or operation.state == "cancelled":
                return  # gone or cancelled: nothing left to record (rule 5)
            payload = cache._payload_or_none(operation)
            if payload is None or payload.cancellation is not None:
                return  # unreadable: the claim loop reconciles it
            if cache._lifecycle.renew(
                operation, cache._claim_owner, _TRANSFER_CLAIM_SECONDS, now
            ):
                if payload.failure is not None and payload.failure.code in {
                    ModelAccessReason.ACCESS_DENIED,
                    ModelAccessReason.ACCESS_REQUIRED,
                }:
                    operation.progress_advanced_at = now
                _store_operation_payload(
                    operation,
                    operation.kind,
                    payload.model_copy(update={"failure": None, "blockers": []}),
                )
                # A settled failed transfer stopped its siblings. A newly
                # accepted attempt resumes; durable cancellation above wins.
                stop.clear()
                return operation.attempt
        return None

    def _finish_succeeded(
        self, operation_id: str, result: ModelCacheDownloadResult
    ) -> None:
        cache = cast("ModelCacheService", self)
        now = cache._clock()
        with cache._session(write=True) as session:
            operation = session.get(
                ModelCacheOperation, operation_id, with_for_update=True
            )
            if operation is None or operation.state == "cancelled":
                return  # gone or cancelled: nothing left to record (rule 5)
            payload = cache._transfer_or_none(operation)
            if payload is not None and payload.cancellation is not None:
                return
            if payload is not None:
                # The bytes are in storage whatever the document says: an
                # unreadable one only loses the result note, never the success.
                _store_operation_payload(
                    operation,
                    operation.kind,
                    _updated(
                        payload,
                        result=result,
                        failure=None,
                        blockers=[],
                        access_recheck=(
                            payload.access_recheck.model_copy(
                                update={
                                    "checked_at": now.isoformat(),
                                    "authorized": True,
                                }
                            )
                            if payload.access_recheck is not None
                            else None
                        ),
                    ),
                )
            operation.progress = progress_document(
                cache_phase(_operation_progress(operation), "completed", now)
            )
            cache._lifecycle.complete(
                operation, Reported(Outcome.DONE, fence=cache._claim_owner), now
            )

    def _set_operation_progress(
        self,
        operation_id: str,
        manifest: ArtifactSetManifest,
        *,
        phase: ModelCacheOperationPhase,
        completed_artifacts: int,
        downloaded_bytes: int,
        current_artifact_key: str | None,
        expected_bytes: int | None = None,
        transfer: ModelCacheTransfer | None = None,
    ) -> None:
        cache = cast("ModelCacheService", self)
        now = cache._clock()
        with cache._session(write=True) as session:
            operation = session.get(
                ModelCacheOperation, operation_id, with_for_update=True
            )
            if operation is None or operation.state == "cancelled":
                return  # gone or cancelled: nothing left to record (rule 5)
            payload = cache._transfer_or_none(operation)
            if payload is None:
                return  # unreadable: the claim loop reconciles it
            if payload.cancellation is not None:
                cache._transfer_stop(operation_id).set()
                return
            old_progress = _operation_progress(operation)
            old_downloaded = old_progress.downloaded_bytes
            old_completed = old_progress.completed_artifacts
            operation.progress = progress_document(
                cache._progress(
                    manifest,
                    previous=old_progress,
                    phase=phase,
                    completed_artifacts=max(old_completed, completed_artifacts),
                    downloaded_bytes=max(old_downloaded, downloaded_bytes),
                    expected_bytes=expected_bytes,
                    current_artifact_key=current_artifact_key,
                    transfer=transfer if transfer is not None else payload.transfer,
                )
            )
            operation.current_artifact_key = current_artifact_key
            cache._lifecycle.renew(
                operation, cache._claim_owner, _TRANSFER_CLAIM_SECONDS, now, take=False
            )
