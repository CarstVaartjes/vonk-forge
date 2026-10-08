"""Failure."""

from __future__ import annotations

import errno
import re
import shutil
from datetime import timedelta
from typing import TYPE_CHECKING, cast

from sqlalchemy.orm import Session
from vonk_agent_protocol import ModelCacheCode

from ..agent_operation_facts import aware as _aware
from ..lifecycle import Outcome, Reported, State
from ..logging import redact_text
from ..model_cache_contract import ModelCacheMissingSourceObservation
from ..model_cache_progress import cache_phase, progress_document
from ..models import ModelCacheOperation, ModelCacheSet
from .artifacts import ArtifactSetManifest
from .catalog_helpers import _iso
from .constants import (
    _CREDENTIAL_FAILURE_CODES,
    _RETRY_BASE_SECONDS,
    _SOURCE_GONE_ATTEMPTS,
    _SOURCE_GONE_STATUSES,
)
from .errors import ModelCacheError, _ArtifactWriterBusy, _retryable_failure
from .persistence import (
    _cache_failure,
    _operation_cancellation,
    _operation_progress,
    _store_operation_payload,
)

if TYPE_CHECKING:
    from .service import ModelCacheService


class FailureMixin:
    """Failure behavior of the cache service."""

    def _finish_failed(
        self,
        operation_id: str,
        set_digest: str,
        manifest: ArtifactSetManifest,
        error: BaseException,
        failed_artifact_key: str | None = None,
        transfer_attempt: int | None = None,
    ) -> None:
        cache = cast("ModelCacheService", self)
        if isinstance(error, _ArtifactWriterBusy):
            cache._defer_artifact_writer(operation_id, error, failed_artifact_key)
            return
        detail = (
            error.detail
            if isinstance(error, ModelCacheError)
            else f"{type(error).__name__}: {str(error)[:400]}"
        )
        detail = redact_text(detail)[:512]
        now = cache._clock()
        required_bytes = free_bytes = shortfall_bytes = None
        if isinstance(error, OSError) and error.errno == errno.ENOSPC:
            try:
                measured_required = manifest.expected_bytes
                measured_free = shutil.disk_usage(cache._root).free
                cache._request_storage(measured_required, ModelCacheCode.CAPACITY)
                required_bytes, free_bytes, shortfall_bytes = (
                    measured_required,
                    measured_free,
                    max(0, measured_required - measured_free),
                )
            except (OSError, RuntimeError, ValueError):
                pass
        failure_code = getattr(error, "code", None)
        if isinstance(error, OSError) and error.errno == errno.ENOSPC:
            failure_code = ModelCacheCode.CAPACITY
        if (
            not isinstance(failure_code, str)
            or re.fullmatch(r"[a-z][a-z0-9_.:-]{0,95}", failure_code) is None
        ):
            failure_code = ModelCacheCode.OPERATION_FAILED
        cancellation_pending = False
        source_status = getattr(error, "source_status", None)
        with cache._session(write=True) as session:
            operation = session.get(
                ModelCacheOperation, operation_id, with_for_update=True
            )
            if operation is not None and operation.state == "cancelled":
                return
            if transfer_attempt is not None and source_status in _SOURCE_GONE_STATUSES:
                if operation is None:
                    return
                claim = cache._lifecycle.lifecycle(operation, now)
                if (
                    claim.state is not State.RUNNING
                    or claim.fence != cache._claim_owner
                    or claim.attempt != transfer_attempt
                    or claim.lease_deadline is None
                    or claim.lease_deadline <= _aware(now)
                ):
                    # The same failed Future may be reported again after a
                    # lost commit acknowledgement. Only its exact live attempt
                    # can contribute a new provider file observation.
                    return
            cancellation_pending = (
                operation is not None and _operation_cancellation(operation) is not None
            )
            row = (
                None if cancellation_pending else session.get(ModelCacheSet, set_digest)
            )
            if row is not None:
                row.verified_bytes = cache._verified_bytes(session, set_digest)
                all_valid = all(
                    cache._object_is_stored(spec) for spec in manifest.artifacts
                )
                row.state = "cached" if all_valid else "needs-repair"
                row.updated_at = now
                row.last_error = detail[:512]
            operation = (
                None
                if cancellation_pending
                else session.get(ModelCacheOperation, operation_id)
            )
            if operation is not None:
                retryable = _retryable_failure(error)
                payload_before = cache._payload_or_none(operation)
                retained_retry = (
                    payload_before.retry if payload_before is not None else None
                )
                gone_recovery: str | None = None
                missing_source: ModelCacheMissingSourceObservation | None = None
                artifact_key = failed_artifact_key or operation.current_artifact_key
                if (
                    retryable
                    and transfer_attempt is not None
                    and source_status in _SOURCE_GONE_STATUSES
                    and artifact_key
                ):
                    previous_observation = (
                        retained_retry.missing_source
                        if retained_retry is not None
                        else None
                    )
                    observations = (
                        previous_observation.observations + 1
                        if previous_observation is not None
                        and previous_observation.artifact_key == artifact_key
                        and previous_observation.status == source_status
                        else 1
                    )
                    missing_source = ModelCacheMissingSourceObservation(
                        artifact_key=artifact_key,
                        status=404 if source_status == 404 else 410,
                        observations=observations,
                    )
                    if observations >= _SOURCE_GONE_ATTEMPTS:
                        # Observed gone, not a blocker: end the download with a
                        # typed reason naming the file. A new download request
                        # resolves the model's newest catalog revision.
                        retryable = False
                        failure_code = ModelCacheCode.SOURCE_GONE
                        gone_recovery = "download_again"
                        detail = cache._source_gone_detail(
                            session,
                            manifest,
                            failed_artifact_key or operation.current_artifact_key,
                            int(source_status),
                            observations,
                        )
                        if row is not None:
                            row.last_error = detail

                operator_retries = (
                    0
                    if payload_before is None
                    else payload_before.retry.operator_retries
                )
                if operation.state == "failed":
                    # A recheck that finds the failure transient: withdraw the
                    # end, and let the core decide again (retry, with backoff).
                    cache._lifecycle.reopen(operation, now)
                provider_hint = getattr(error, "retry_after_seconds", None)
                operation.last_error = detail[:512]
                # The core owns the schedule (bounded backoff, never earlier than
                # the provider's own hint) and the end of a terminal failure.
                settled = cache._lifecycle.complete(
                    operation,
                    Reported(
                        Outcome.FAILED,
                        fence=cache._claim_owner,
                        retryable=retryable,
                        retry_after=(
                            now + timedelta(seconds=provider_hint)
                            if retryable
                            and type(provider_hint) is int
                            and provider_hint >= 0
                            else None
                        ),
                        reason=detail[:512],
                    ),
                    now,
                    interrupted=False,
                )
                if settled.state not in (State.BACKOFF, State.FAILED):
                    return  # a report under a fence that is not this worker's
                bounded_retry = settled.state is State.BACKOFF
                next_retry = operation.next_action_at if bounded_retry else None
                retry_delay = (
                    max(1, round((_aware(next_retry) - now).total_seconds()))
                    if next_retry is not None
                    else None
                )
                payload_after = cache._payload_or_none(operation)
                retry = (
                    None
                    if payload_after is None
                    else payload_after.retry.model_copy(
                        update={
                            "operator_retries": operator_retries,
                            "missing_source": missing_source,
                        }
                    )
                )
                if retry is not None and failure_code in _CREDENTIAL_FAILURE_CODES:
                    # The worker resumes this exact transfer once the
                    # configured credential file changes.
                    retry = retry.model_copy(
                        update={
                            "credential_fingerprint": (
                                cache._huggingface_credential_fingerprint()
                            )
                        }
                    )
                provider_rate_limited = (
                    getattr(error, "code", None) == ModelCacheCode.RATE_LIMITED
                )
                if provider_rate_limited and cache._manifest_has_huggingface_source(
                    manifest
                ):
                    cache._record_huggingface_cooldown(
                        next_retry or now + timedelta(seconds=_RETRY_BASE_SECONDS)
                    )
                failure_payload = _cache_failure(
                    failure_code,
                    detail,
                    retryable=retryable,
                    recovery=gone_recovery
                    or getattr(error, "recovery", None)
                    or ("capacity" if failure_code == ModelCacheCode.CAPACITY else None)
                    or ("resume" if bounded_retry else "retry"),
                    retry_time=_iso(next_retry) if bounded_retry else None,
                    retry_after_seconds=retry_delay if bounded_retry else None,
                    required_bytes=required_bytes,
                    free_bytes=free_bytes,
                    shortfall_bytes=shortfall_bytes,
                    artifact_key=failed_artifact_key,
                )
                if payload_after is not None and retry is not None:
                    _store_operation_payload(
                        operation,
                        operation.kind,
                        payload_after.model_copy(
                            update={"failure": failure_payload, "retry": retry}
                        ),
                    )
                operation.progress = progress_document(
                    cache_phase(
                        _operation_progress(operation),
                        "queued" if bounded_retry else "failed",
                        now,
                    )
                )
        if cancellation_pending:
            cache._try_settle_cancellation(operation_id)

    @staticmethod
    def _source_gone_detail(
        session: Session,
        manifest: ArtifactSetManifest,
        artifact_key: str | None,
        status: int,
        attempts: int,
    ) -> str:
        """Name the missing file, and say whether the catalog already has a successor."""

        spec = next(
            (
                item
                for item in manifest.artifacts
                if artifact_key in {item.key, item.artifact_id}
            ),
            None,
        )
        where = "a file"
        if spec is not None:
            repository = (spec.repository or "").removeprefix("https://huggingface.co/")
            revision = (spec.revision or "")[:12]
            where = f"file {spec.path}" + (
                f" ({repository}@{revision})" if repository or revision else ""
            )
        from .service import ModelCacheService

        successor = ModelCacheService._model_update_candidate(session, manifest)
        action = (
            "a newer catalog revision of this model exists; download it again to use it"
            if successor is not None
            else "the model needs a catalog refresh before it can be downloaded again"
        )
        return (
            f"source gone: {where} answered HTTP {status} on {attempts} attempts; "
            f"{action}"
        )[:512]
