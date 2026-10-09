"""Persistence."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

from pydantic import ValidationError
from sqlalchemy.orm import object_session
from vonk_agent_protocol import ModelCacheCode, SecurityRefusalReason

from ..lifecycle import State
from ..lifecycle.evidence import (
    BookkeepingReason,
    Damaged,
    Residue,
    read_or_rebuild,
    retire_as_unknown,
)
from ..logging import redact_text
from ..model_cache_contract import (
    ModelCacheCancellation,
    ModelCacheCounters,
    ModelCacheDownloadPayload,
    ModelCacheDownloadResult,
    ModelCacheOperationPayload,
    ModelCacheOperationPhase,
    ModelCacheOperationProgress,
    ModelCacheOperationResult,
    ModelCacheRemovalPayload,
    ModelCacheRemovalResult,
    ModelCacheRetry,
    parse_model_cache_payload,
    parse_model_cache_result,
)
from ..model_cache_progress import cache_progress
from ..models import ModelCacheOperation, ModelCacheSet
from ..operation_blockers import OperationBlocker, bound_blockers, make_blocker
from ..operation_contract import AvailabilityOperationFailure
from ..stored_json import read_row_column
from ..strict_json import read_stored_model, serialize_json_value
from .artifacts import ArtifactSetManifest, _sha256_json
from .constants import _CREDENTIAL_FAILURE_PUBLIC_CODES, _LOGGER, SCHEMA_VERSION
from .errors import ModelCacheResolutionError, ModelCacheStorageInvalid


def _model_removal_intent_digest(
    payload: ModelCacheRemovalPayload, *, actor: str, request_key: str
) -> str:
    """Bind immutable effects and authority, excluding advancing checkpoints."""
    return _sha256_json(
        {
            "action": "remove-model",
            "actor": actor,
            "request_key": request_key,
            "selector": payload.selector,
            "model_content_sha256": payload.model_content_sha256,
            "removal_fence": payload.removal_fence,
            "selected": payload.selected,
            "scope_pending": payload.scope_pending,
            "scope_from_content": payload.scope_from_content,
            "selected_objects": payload.selected_objects,
            "delete_objects": payload.delete_objects,
        }
    )


def _read_operation_payload(
    operation: ModelCacheOperation,
) -> ModelCacheOperationPayload | Damaged:
    """Read and normalize one persisted operation envelope.

    A damaged envelope is ``Damaged``: the contract of :func:`read_or_rebuild`,
    which every caller goes through (``_operation_payload``).
    """

    value = read_row_column(operation, "payload")
    if isinstance(value, Residue) or value is None:
        return Damaged("persisted cache operation payload is invalid")
    return _parse_operation_envelope(operation, serialize_json_value(value))


def _parse_operation_envelope(
    operation: ModelCacheOperation, envelope: object
) -> ModelCacheOperationPayload | Damaged:
    try:
        parsed = parse_model_cache_payload(operation.kind, envelope)
        if isinstance(
            parsed, ModelCacheRemovalPayload
        ) and operation.plan_digest != _model_removal_intent_digest(
            parsed, actor=operation.actor, request_key=operation.request_key
        ):
            return Damaged(
                "persisted model removal intent no longer matches its accepted plan"
            )
        if isinstance(parsed, ModelCacheDownloadPayload):
            manifest = _read_manifest_document(serialize_json_value(parsed.manifest))
            if isinstance(manifest, Damaged):
                return manifest
        return parsed
    except ValidationError:
        return Damaged("persisted cache operation payload is invalid")


def _read_manifest_document(document: object) -> ArtifactSetManifest | Damaged:
    """``ArtifactSetManifest.from_document`` for a *stored* document.

    The strict parser refuses a malformed manifest at ingress; a persisted copy
    that no longer parses is damaged state, which the caller reads through
    :func:`read_or_rebuild`: it is ``Damaged`` here.
    """

    try:
        return ArtifactSetManifest.from_document(document)
    except ModelCacheResolutionError as error:
        return Damaged(error.detail)


def _rebuild_operation_payload(
    operation: ModelCacheOperation,
) -> ModelCacheOperationPayload | None:
    """Re-derive a damaged download/repair envelope from what is still readable.

    The set row (keyed by the operation's own ``artifact_set_sha256``) holds the
    manifest and the retry counters restart: both are evidence the cache owns.
    A removal's plan cannot be re-derived (it is the accepted destructive intent,
    checked against its digest), so only its retry counters are; a plan that does
    not read stays damaged and the caller retires the row.

    The stored document is the one place that is still raw: it did not validate,
    so there is no model yet to read it through.
    """

    observed = read_row_column(operation, "payload")
    if not isinstance(observed, Residue) and observed is not None:
        parsed = _parse_operation_envelope(operation, serialize_json_value(observed))
        return None if isinstance(parsed, Damaged) else parsed
    # Only the repair boundary inspects a damaged raw copy, after the canonical
    # column reader has reported it. The repaired candidate is validated below.
    raw = operation.payload
    if not isinstance(raw, Mapping):
        return None
    candidate = dict(raw)
    if not isinstance(candidate.get("retry"), Mapping):
        candidate["retry"] = ModelCacheRetry(
            automatic_attempts=1, operator_retries=0
        ).model_dump(mode="json")
    if operation.kind in {"download", "repair"}:
        session = object_session(operation)
        set_digest = operation.artifact_set_sha256
        row = (
            None
            if session is None or set_digest is None
            else session.get(ModelCacheSet, set_digest)
        )
        if row is not None:
            manifest = read_row_column(row, "manifest")
            if manifest is not None and not isinstance(manifest, Residue):
                candidate["manifest"] = serialize_json_value(manifest)
            candidate["artifact_set_sha256"] = set_digest
    # A result that does not read is re-derived from the operation's own columns
    # (a download's result names its set; a removal's its selected sets).
    if candidate.get("result") is not None:
        try:
            parse_model_cache_result(operation.kind, candidate["result"])
        except (TypeError, ValueError, ValidationError):
            derived = _derived_result(
                operation,
                selected=candidate.get("selected"),
                reclaimed=candidate.get("reclaimed_bytes"),
            )
            candidate["result"] = (
                None if derived is None else derived.model_dump(mode="json")
            )
    # A removal keeps its accepted plan (checked against its digest): only the
    # retry counters are bookkeeping that can be restarted.
    rebuilt = _parse_operation_envelope(operation, candidate)
    return None if isinstance(rebuilt, Damaged) else rebuilt


def _derived_result(
    operation: ModelCacheOperation,
    *,
    selected: object = None,
    reclaimed: object = None,
) -> ModelCacheOperationResult | None:
    """The result a succeeded operation's own evidence implies, else ``None``."""

    if operation.state != State.SUCCEEDED:
        return None
    if operation.kind in {"download", "repair"}:
        if operation.artifact_set_sha256 is None:
            return None
        return ModelCacheDownloadResult(
            schema_version=SCHEMA_VERSION,
            artifact_set_sha256=operation.artifact_set_sha256,
            coverage="complete",
        )
    return ModelCacheRemovalResult(
        schema_version=SCHEMA_VERSION,
        removed_entries=list(selected) if isinstance(selected, list) else [],
        reclaimed_bytes=reclaimed if type(reclaimed) is int and reclaimed >= 0 else 0,
        cancelled_operations=[],
    )


def _manifest_of(payload: ModelCacheDownloadPayload) -> ArtifactSetManifest:
    """The artifact-set manifest a download or repair payload carries."""

    return ArtifactSetManifest.from_contract(payload.manifest)


def _updated[P: ModelCacheOperationPayload](payload: P, **changes: object) -> P:
    """``payload`` with ``changes`` applied and checked against its own contract.

    ``model_copy`` skips validation; a checkpoint is only a checkpoint when it
    still reads back, so a change that breaks it raises ``ValidationError``.
    """

    return read_stored_model(
        type(payload),
        serialize_json_value(payload.model_copy(update=changes)),
        from_json=True,
    )


def _operation_payload(
    operation: ModelCacheOperation,
) -> ModelCacheOperationPayload | Residue:
    """The operation's payload, rebuilt from evidence, else a typed residue."""

    return read_or_rebuild(
        kind="model-cache-operation",
        subject=operation.id,
        read=lambda: _read_operation_payload(operation),
        rebuild=lambda: _rebuild_operation_payload(operation),
    )


def _removal_checkpoint(
    payload: ModelCacheOperationPayload,
) -> ModelCacheRemovalPayload | Residue:
    """The removal checkpoint of a payload, or a residue when it is not one."""

    if isinstance(payload, ModelCacheRemovalPayload):
        return payload
    return retire_as_unknown(
        "model-removal-checkpoint",
        "",
        BookkeepingReason.PERSISTED_STATE_DAMAGED,
        "model removal payload is invalid",
    )


def _operation_removal(
    operation: ModelCacheOperation,
) -> ModelCacheRemovalPayload | Residue:
    """The removal checkpoint of a stored operation, or the residue of its damage."""

    payload = _operation_payload(operation)
    return payload if isinstance(payload, Residue) else _removal_checkpoint(payload)


def _operation_cancellation(
    operation: ModelCacheOperation,
) -> ModelCacheCancellation | None:
    """The accepted cancel request; an unreadable operation has none."""

    payload = _operation_payload(operation)
    return None if isinstance(payload, Residue) else payload.cancellation


def _fresh_progress(now: datetime) -> ModelCacheOperationProgress:
    return cache_progress(
        ModelCacheCounters(
            phase=cast(ModelCacheOperationPhase, State.QUEUED.value),
            completed_artifacts=0,
            total_artifacts=0,
            downloaded_bytes=0,
        ),
        previous=None,
        now=now,
    )


def _read_operation_progress(
    operation: ModelCacheOperation,
) -> ModelCacheOperationProgress | Damaged:
    value = read_row_column(operation, "progress")
    return (
        value
        if isinstance(value, ModelCacheOperationProgress)
        else Damaged("persisted cache operation progress is invalid")
    )


def _operation_progress(
    operation: ModelCacheOperation, now: datetime | None = None
) -> ModelCacheOperationProgress:
    """The operation's progress; a damaged measurement restarts from zero.

    Progress is a derived measurement (the transfer ledger and the receipts are
    the evidence), so it is never a reason to stop: the next sample rebuilds it.
    """

    result = read_or_rebuild(
        kind="model-cache-progress",
        subject=operation.id,
        read=lambda: _read_operation_progress(operation),
        rebuild=lambda: _fresh_progress(now or datetime.now(UTC)),
    )
    if isinstance(result, Residue):  # pragma: no cover - a fresh sample always builds
        return _fresh_progress(now or datetime.now(UTC))
    return result


def _write_operation_payload(
    kind: str, value: ModelCacheOperationPayload
) -> ModelCacheOperationPayload:
    """Validate and normalize a newly assembled operation envelope."""

    try:
        parsed = parse_model_cache_payload(
            kind,
            serialize_json_value(
                value.model_copy(update={"blockers": _wait_blockers(value)})
            ),
        )
        if isinstance(parsed, ModelCacheDownloadPayload):
            ArtifactSetManifest.from_document(serialize_json_value(parsed.manifest))
        return parsed
    except (TypeError, ValueError, ValidationError) as error:
        raise ModelCacheStorageInvalid(
            ModelCacheCode.PAYLOAD_INVALID,
            "cache operation payload is invalid",
        ) from error


def _wait_blockers(payload: ModelCacheOperationPayload) -> list[OperationBlocker]:
    """What the operation waits for, taken from the failure it will retry.

    A failure the Controller retries by itself (or resumes once a credential
    changes) is a wait, so its reason is stored as a blocker; a terminal failure
    or a running operation waits for nothing.
    """

    failure = payload.failure
    if failure is None:
        return []
    waiting = (
        failure.retryable
        or failure.retry_time is not None
        or failure.code in _CREDENTIAL_FAILURE_PUBLIC_CODES
    )
    if not waiting:
        return []
    return bound_blockers(
        [
            make_blocker(
                failure.code,
                failure.detail,
                severity="info"
                if failure.code == ModelCacheCode.OBJECT_BUSY
                else "warning",
            )
        ]
    )


def _store_operation_payload(
    operation: ModelCacheOperation, kind: str, payload: ModelCacheOperationPayload
) -> None:
    """Persist a payload and log one line when the reason it waits changes."""

    current = read_row_column(operation, "payload")
    before = [] if current is None or isinstance(current, Residue) else current.blockers
    stored = _write_operation_payload(kind, payload)
    after = stored.blockers
    if after and {(b.code, tuple(b.node_ids)) for b in before} != {
        (b.code, tuple(b.node_ids)) for b in after
    }:
        _LOGGER.log(
            logging.WARNING
            if any(item.severity == "error" for item in after)
            else logging.INFO,
            "model cache %s %s is waiting: %s",
            operation.kind,
            operation.id,
            "; ".join(f"{item.code}: {item.detail}" for item in after[:4]),
        )
    operation.payload = serialize_json_value(stored)


def _cache_failure(
    code: str,
    detail: str,
    *,
    retryable: bool,
    recovery: str,
    retry_time: str | None = None,
    retry_after_seconds: int | None = None,
    required_bytes: int | None = None,
    free_bytes: int | None = None,
    shortfall_bytes: int | None = None,
    artifact_key: str | None = None,
) -> AvailabilityOperationFailure:
    """Translate an exception once, then persist the canonical public contract."""
    semantic_codes = {
        ModelCacheCode.CREDENTIALS_MISSING: "access_required",
        SecurityRefusalReason.MODEL_CACHE_CREDENTIALS_DENIED.value: "access_denied",
        SecurityRefusalReason.MODEL_CACHE_CREDENTIALS_INVALID.value: "credentials_invalid",
        ModelCacheCode.RATE_LIMITED: "rate_limited",
        ModelCacheCode.DIGEST_MISMATCH: "integrity_mismatch",
        ModelCacheCode.SOURCE_SIZE_MISMATCH: "integrity_mismatch",
        ModelCacheCode.CAPACITY: "capacity",
        ModelCacheCode.INTERRUPTED: "interrupted",
    }
    recovery_actions = {
        "access_required": [
            "open_model_access",
            "configure_hf_token",
            "check_access_and_resume",
        ],
        "access_denied": ["open_model_access", "check_access_and_resume"],
        "credentials_invalid": ["configure_hf_token", "check_access_and_resume"],
        "resume": ["resume"],
        "download_again": ["download_again"],
        "retry": ["retry"],
        "capacity": ["free_space", "resume"],
        "check_access_and_resume": ["check_access_and_resume"],
        "inspect": ["inspect"],
    }
    return read_stored_model(
        AvailabilityOperationFailure,
        {
            "code": semantic_codes.get(code, code),
            "detail": redact_text(detail)[:512],
            "retryable": retryable,
            "recovery_actions": recovery_actions[recovery],
            "retry_time": retry_time,
            "retry_after_seconds": retry_after_seconds,
            "required_bytes": required_bytes,
            "free_bytes": free_bytes,
            "shortfall_bytes": shortfall_bytes,
            "artifact_key": artifact_key,
            "log_excerpt": redact_text(detail)[:1024],
        },
    )
