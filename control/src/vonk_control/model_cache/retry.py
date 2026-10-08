"""Download recovery and access rechecks."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import httpx2
from sqlalchemy import select
from vonk_agent_protocol import ModelCacheCode

from ..lifecycle import Outcome, Reported
from ..lifecycle.model_cache import ModelCacheAdapter
from ..logging import redact_text
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
    ArtifactSpec,
    _optional_digest,
    _unique_artifacts,
)
from .catalog_helpers import _iso_now
from .constants import _TRANSFER_CLAIM_SECONDS, SCHEMA_VERSION
from .errors import (
    ModelCacheConflictInvalid,
    ModelCacheConflictRefused,
    ModelCacheNotFoundInvalid,
    ModelCacheStorageError,
    _huggingface_access_url,
    _retryable_failure,
)
from .persistence import (
    _cache_failure,
    _manifest_of,
    _operation_progress,
    _store_operation_payload,
    _updated,
    _write_operation_payload,
)
from .source_helpers import _is_hf_canonical_url, _request_key
from .views import CacheOperationView

if TYPE_CHECKING:
    from .service import ModelCacheService


class RetryMixin:
    """Download recovery behavior of the cache service."""

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
                or previous.state != "failed"
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
        """Recheck terminal HF access, then queue the exact retained transfer.

        Authentication failures are deliberately terminal for automatic
        scheduling.  This action is the explicit operator boundary after the
        configured token file or upstream access has changed.  It never
        rebuilds a manifest or changes the pinned revision.
        """
        cache = cast("ModelCacheService", self)

        request_key = _request_key(request_key)
        requested_set = _optional_digest(artifact_set_sha256)
        requested_plan = _optional_digest(plan_digest)
        assert requested_set is not None and requested_plan is not None
        auth_codes = {
            "access_required",
            "access_denied",
            "credentials_invalid",
        }
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
                raise ModelCacheConflictRefused(
                    ModelCacheCode.IDENTITY_MISMATCH,
                    "access recheck identity does not match the persisted operation",
                )
            existing = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == request_key
                )
            )
            if existing is not None:
                if existing.id == previous.id:
                    raise ModelCacheConflictInvalid(
                        ModelCacheCode.REQUEST_KEY_REUSED,
                        "access recheck requires a new operator request key",
                    )
                if (
                    existing.kind == previous.kind
                    and existing.artifact_set_sha256 == previous.artifact_set_sha256
                    and existing.plan_digest == previous.plan_digest
                ):
                    return cache._operation_view(existing)
                raise ModelCacheConflictInvalid(
                    ModelCacheCode.REQUEST_KEY_REUSED,
                    "request key was already used for another cache operation",
                )
            failure = cache._canonical_failure(previous)
            if (
                previous.kind not in {"download", "repair"}
                or previous.state != "failed"
                or failure is None
                or failure.code not in auth_codes
            ):
                raise ModelCacheConflictInvalid(
                    ModelCacheCode.ACCESS_RECHECK_UNAVAILABLE,
                    "the operation does not have a terminal Hugging Face access failure",
                )
            previous_payload = cache._transfer_or_retire(previous)
            if previous_payload is None:
                return cache._operation_view(previous)  # unreadable: retired
            prior_check = previous_payload.access_recheck
            if prior_check is not None and prior_check.request_key == request_key:
                return cache._operation_view(previous)
            manifest = _manifest_of(previous_payload)
            failed_artifact_key = failure.artifact_key or previous.current_artifact_key

        try:
            cache._check_huggingface_access(
                manifest,
                failed_artifact_key=(
                    failed_artifact_key
                    if isinstance(failed_artifact_key, str)
                    else None
                ),
            )
        except (ModelCacheStorageError, httpx2.HTTPError, OSError) as error:
            if _retryable_failure(error):
                cache._finish_failed(
                    operation_id,
                    requested_set,
                    manifest,
                    error,
                    failed_artifact_key=(
                        failed_artifact_key
                        if isinstance(failed_artifact_key, str)
                        else None
                    ),
                )
                return cache.get_operation(operation_id)
            if not isinstance(error, ModelCacheStorageError):
                raise
            safe_detail = redact_text(error.detail)[:512]
            now = cache._clock()
            failure_payload = _cache_failure(
                error.code,
                safe_detail,
                retryable=False,
                recovery=error.recovery or "check_access_and_resume",
                artifact_key=failed_artifact_key,
            )
            with cache._lock, cache._session(write=True) as session:
                previous = session.get(
                    ModelCacheOperation, operation_id, with_for_update=True
                )
                if previous is None:
                    raise ModelCacheNotFoundInvalid(
                        ModelCacheCode.OPERATION_MISSING,
                        "cache operation was not found",
                    )
                previous.last_error = safe_detail
                cache._lifecycle.complete(
                    previous,
                    Reported(Outcome.FAILED, retryable=False, reason=safe_detail),
                    now,
                )
                current = cache._payload_or_none(previous)
                if current is not None:
                    _store_operation_payload(
                        previous,
                        previous.kind,
                        current.model_copy(
                            update={
                                "failure": failure_payload,
                                "access_recheck": ModelCacheAccessRecheck(
                                    request_key=request_key,
                                    checked_at=_iso_now(now),
                                    authorized=False,
                                ),
                            }
                        ),
                    )
                return cache._operation_view(previous)

        now = cache._clock()
        with cache._lock, cache._session(write=True) as session:
            previous = session.get(
                ModelCacheOperation, operation_id, with_for_update=True
            )
            if previous is None:
                raise ModelCacheNotFoundInvalid(
                    ModelCacheCode.OPERATION_MISSING, "cache operation was not found"
                )
            current_payload = cache._transfer_or_retire(previous, now=now)
            if current_payload is None:
                return cache._operation_view(previous)  # unreadable: retired
            payload = current_payload.model_copy(
                update={
                    "failure": None,
                    "result": None,
                    "claim": None,
                    "retry": current_payload.retry.model_copy(
                        update={
                            "automatic_attempts": 1,
                            "next_retry_at": None,
                            "retry_after_seconds": None,
                        }
                    ),
                    "resume_of": previous.id,
                    "access_recheck": ModelCacheAccessRecheck(
                        request_key=request_key,
                        checked_at=_iso_now(now),
                        authorized=True,
                    ),
                }
            )
            total, received = cache._transfer_totals(payload)
            prior_progress = _operation_progress(previous)
            cache._require_model_sets_open(session, (requested_set,), now=now)
            progress = cache._progress(
                manifest,
                phase="queued",
                completed_artifacts=prior_progress.completed_artifacts,
                downloaded_bytes=received,
                expected_bytes=total,
                current_artifact_key=(
                    previous.current_artifact_key
                    if isinstance(previous.current_artifact_key, str)
                    else None
                ),
                transfer=payload.transfer,
            )
            operation = ModelCacheAdapter.new_operation(
                request_key=request_key,
                schema_version=SCHEMA_VERSION,
                kind=previous.kind,
                attempt=1,
                artifact_set_sha256=requested_set,
                plan_digest=previous.plan_digest,
                payload=serialize_json_value(
                    _write_operation_payload(previous.kind, payload)
                ),
                progress=progress_document(progress),
                actor=actor,
                current_artifact_key=previous.current_artifact_key,
                created_at=now,
                updated_at=now,
            )
            session.add(operation)
            session.flush()
            return cache._operation_view(operation)

    def _check_huggingface_access(
        self,
        manifest: ArtifactSetManifest,
        *,
        failed_artifact_key: str | None = None,
    ) -> None:
        cache = cast("ModelCacheService", self)
        unique_specs = _unique_artifacts(manifest.artifacts)
        exact = (
            next(
                (
                    spec
                    for spec in unique_specs.values()
                    if failed_artifact_key in {spec.key, spec.artifact_id}
                ),
                None,
            )
            if failed_artifact_key
            else None
        )
        if exact is not None and _is_hf_canonical_url(exact.source):
            specs = [exact]
        else:
            # A missing key can occur after an interrupted/recovered worker.
            # Probe one representative per model repository rather than every
            # shard while still checking public and gated dependencies.
            by_repository: dict[str, ArtifactSpec] = {}
            for spec in unique_specs.values():
                if _is_hf_canonical_url(spec.source):
                    by_repository.setdefault(_huggingface_access_url(spec.source), spec)
            specs = list(by_repository.values())
        if not specs:
            raise ModelCacheConflictInvalid(
                ModelCacheCode.ACCESS_RECHECK_UNAVAILABLE,
                "the persisted operation has no canonical Hugging Face source to check",
            )
        client = cache._http
        owns_client = client is None
        if client is None:
            client = httpx2.Client(
                follow_redirects=False,
                timeout=httpx2.Timeout(30.0),
                trust_env=False,
            )
        try:
            for spec in specs:
                response = cache._open_http_response(
                    client, spec.source, {"Range": "bytes=0-0"}
                )
                response.close()
        finally:
            if owns_client:
                client.close()

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
                _store_operation_payload(
                    operation,
                    operation.kind,
                    payload.model_copy(update={"failure": None}),
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
                    _updated(payload, result=result, failure=None),
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
