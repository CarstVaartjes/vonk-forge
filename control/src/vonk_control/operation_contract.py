"""Current nested contracts for durable Controller operations and progress."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from vonk_agent_protocol import (
    OperationCheckpoint,
    OperationMemberProgress,
    OperationProgress,
    normalize_operation_progress,
)
from vonk_agent_protocol.contracts import AgentFailureResult

from .logging import redact_text

_SENSITIVE = re.compile(
    r"(?i)(password|secret|token|private.?key|authorization|cookie)"
)
_MAX_EVIDENCE_ITEMS = 32
_MAX_EVIDENCE_DEPTH = 4
_MAX_FAILURE_EVIDENCE_BYTES = 8192


class OperationPhase(StrEnum):
    """Stable phases shared by downloads, lifecycle actions, and verification."""

    DOWNLOAD = "download"
    VERIFY = "verify"
    TRANSFER = "transfer"
    PREPARE = "prepare"
    CLEANUP = "cleanup"
    STOP = "stop"
    START = "start"
    FINAL_VERIFY = "final_verify"


class OperationRecoveryAction(StrEnum):
    RETRY = "retry"
    RESUME = "resume"
    CANCEL = "cancel"
    INSPECT = "inspect"


class OperationFailureEvidence(BaseModel):
    """Small, sanitized operator evidence safe to expose in status responses."""

    model_config = ConfigDict(extra="forbid", strict=True)

    error_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    summary: str = Field(min_length=1, max_length=256)
    detail: str | None = Field(default=None, max_length=1024)
    retryable: bool = False
    uncertain: bool = False


class AvailabilityRecoveryAction(StrEnum):
    RETRY = "retry"
    RESUME = "resume"
    DOWNLOAD_AGAIN = "download_again"
    FORCE_REBUILD = "force_rebuild"
    OPEN_MODEL_ACCESS = "open_model_access"
    CONFIGURE_HF_TOKEN = "configure_hf_token"
    CHECK_ACCESS_AND_RESUME = "check_access_and_resume"
    FREE_SPACE = "free_space"
    INSPECT = "inspect"


class AvailabilityOperationFailure(BaseModel):
    """Shared failure wire contract for model and image availability."""

    model_config = ConfigDict(extra="forbid", strict=True)

    code: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,95}$")
    detail: str = Field(min_length=1, max_length=512)
    recovery_actions: list[AvailabilityRecoveryAction] = Field(
        default_factory=list, max_length=8
    )
    retryable: bool = False
    retry_time: str | None = Field(default=None, max_length=64)
    retry_after_seconds: int | None = Field(default=None, ge=0)
    log_excerpt: str | None = Field(default=None, max_length=1024)
    required_bytes: int | None = Field(default=None, ge=0)
    free_bytes: int | None = Field(default=None, ge=0)
    shortfall_bytes: int | None = Field(default=None, ge=0)
    artifact_key: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("recovery_actions", mode="before")
    @classmethod
    def parse_recovery_actions(cls, value: object) -> object:
        """Accept the enum's string wire representation in Python mappings.

        Strict validation still rejects numeric and boolean values.  The
        explicit conversion only bridges FastAPI's decoded request mapping and
        the same canonical strings accepted by JSON-mode validation.
        """

        if not isinstance(value, (list, tuple)):
            return value
        try:
            return [
                item
                if isinstance(item, AvailabilityRecoveryAction)
                else AvailabilityRecoveryAction(item)
                for item in value
            ]
        except (TypeError, ValueError) as error:
            raise ValueError("recovery_actions contains an invalid action") from error

    @model_validator(mode="after")
    def validate_retry_and_capacity(self) -> AvailabilityOperationFailure:
        if self.retry_after_seconds is not None and self.retry_time is None:
            raise ValueError("retry_after_seconds requires retry_time")
        if self.retry_time is not None:
            if (
                re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
                    self.retry_time,
                )
                is None
            ):
                raise ValueError("retry_time must be RFC3339")
            try:
                parsed = datetime.fromisoformat(self.retry_time)
            except ValueError as error:
                raise ValueError("retry_time must be RFC3339") from error
            if parsed.tzinfo is None:
                raise ValueError("retry_time must include a timezone")
        capacity = (self.required_bytes, self.free_bytes, self.shortfall_bytes)
        if any(value is not None for value in capacity):
            if any(value is None for value in capacity):
                raise ValueError("capacity fields must be complete when present")
            assert self.required_bytes is not None
            assert self.free_bytes is not None
            assert self.shortfall_bytes is not None
            if self.shortfall_bytes != max(0, self.required_bytes - self.free_bytes):
                raise ValueError(
                    "shortfall_bytes does not match required and free bytes"
                )
        return self


OperationFailure = AgentFailureResult | AvailabilityOperationFailure | OperationFailureEvidence


class OperationEvidenceProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source: str = Field(min_length=1, max_length=128)
    collected_at: str | None = Field(default=None, max_length=64)
    evidence_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    authority_revision: str | None = Field(default=None, max_length=128)


class OperationEvidenceDownload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    media_type: str = Field(min_length=1, max_length=128)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    href: str = Field(min_length=1, max_length=512)


class OperationRecovery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    uncertain: bool = False
    actions: list[OperationRecoveryAction] = Field(default_factory=list, max_length=4)
    explanation: str | None = Field(default=None, max_length=512)


def validate_progress_update(
    previous: Mapping[str, object] | None, current: Mapping[str, object]
) -> dict[str, object]:
    """Validate monotonic bytes/checkpoint updates within one leased attempt."""

    normalized = normalize_operation_progress(current)
    if not previous:
        return normalized
    old = normalize_operation_progress(previous)
    # Lease renewal is independent of the executor's more specific progress.
    if normalized["phase"] == "executing" and old["phase"] != "executing":
        normalized["phase"] = old["phase"]
    # A phase-only heartbeat may report only a new phase. Keep the last durable
    # counters/checkpoint instead of treating omitted fields as zero/reset.
    for key in (
        "kind",
        "object_sha256",
        "completed_bytes",
        "total_bytes",
        "total_bytes_known",
        "completed_items",
        "total_items",
        "checkpoint",
        "members",
    ):
        if key not in old:
            continue
        if key == "total_bytes" and (
            "total_bytes" in current or current.get("total_bytes_known") is False
        ):
            continue
        omitted_total = (
            key == "total_bytes_known"
            and "total_bytes_known" not in current
            and "total_bytes" not in current
        )
        if key not in normalized or omitted_total:
            normalized[key] = old[key]
    old_bytes = int(old.get("completed_bytes", 0))
    new_bytes = int(normalized.get("completed_bytes", 0))
    if new_bytes < old_bytes:
        raise ValueError("operation progress bytes cannot move backwards")
    if int(normalized.get("completed_items") or 0) < int(old.get("completed_items") or 0):
        raise ValueError("operation progress items cannot move backwards")
    old_checkpoint = old.get("checkpoint")
    new_checkpoint = normalized.get("checkpoint")
    if isinstance(old_checkpoint, Mapping) and isinstance(new_checkpoint, Mapping):
        old_sequence = int(old_checkpoint.get("sequence", 0))
        new_sequence = int(new_checkpoint.get("sequence", 0))
        if new_sequence < old_sequence:
            raise ValueError("operation checkpoint sequence cannot move backwards")
        if new_sequence == old_sequence and dict(new_checkpoint) != dict(
            old_checkpoint
        ):
            raise ValueError("operation checkpoint was reused with different data")
    old_members = {
        str(item["member_id"]): item
        for item in old.get("members", [])
        if isinstance(item, Mapping) and isinstance(item.get("member_id"), str)
    }
    for item in normalized.get("members", []):
        if not isinstance(item, Mapping):
            continue
        member_id = item.get("member_id")
        prior = old_members.get(str(member_id))
        if prior is not None and int(item.get("completed_bytes", 0)) < int(
            prior.get("completed_bytes", 0)
        ):
            raise ValueError("operation member progress bytes cannot move backwards")
    return normalize_operation_progress(normalized)


def sanitize_failure_evidence(value: Mapping[str, object]) -> dict[str, object]:
    """Return bounded, secret-free failure evidence suitable for persistence."""

    def clean(item: object, depth: int = 0) -> object:
        if depth > _MAX_EVIDENCE_DEPTH:
            return "[truncated]"
        if isinstance(item, str):
            return redact_text(item)[:1024]
        if isinstance(item, Mapping):
            result: dict[str, object] = {}
            for key, child in list(item.items())[:_MAX_EVIDENCE_ITEMS]:
                if not isinstance(key, str) or _SENSITIVE.search(key):
                    continue
                result[key[:128]] = clean(child, depth + 1)
            return result
        if isinstance(item, list):
            return [clean(child, depth + 1) for child in item[:_MAX_EVIDENCE_ITEMS]]
        if item is None or isinstance(item, (bool, int, float)):
            return item
        return str(item)[:1024]

    result = clean(value)
    if not isinstance(result, dict):
        raise TypeError("failure evidence must be an object")
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > _MAX_FAILURE_EVIDENCE_BYTES:
        # Keep enough context to identify and recover the failure while placing
        # a hard upper bound on durable/operator-visible diagnostic data.
        result = {
            key: result[key]
            for key in ("error_code", "summary", "reason", "uncertain", "retryable")
            if key in result
        }
        result["detail"] = "failure evidence truncated"
    return result


def recovery_for_operation(
    state: str,
    *,
    supported_actions: object = None,
    available_actions: object = None,
    uncertain: bool = False,
) -> OperationRecovery:
    """Project only actions explicitly persisted by the Controller.

    State alone cannot prove that a retry, resume, or cancel route is safe for
    a particular operation. Every operation therefore defaults to inspection;
    workers may advertise a bounded subset in ``supported_actions``. API
    projections may further intersect those actions with routes that exist.
    """

    actions: list[OperationRecoveryAction] = [OperationRecoveryAction.INSPECT]
    advertised: list[OperationRecoveryAction] = []
    if isinstance(supported_actions, (list, tuple, set, frozenset)):
        for raw in supported_actions:
            try:
                action = OperationRecoveryAction(raw)
            except (TypeError, ValueError):
                continue
            if action not in advertised:
                advertised.append(action)
    if available_actions is None:
        permitted = set(advertised)
    else:
        permitted = {OperationRecoveryAction(raw) for raw in available_actions}
    actions.extend(action for action in advertised if action in permitted)
    return OperationRecovery(
        uncertain=uncertain or state in {"waiting-for-operator", "uncertain"},
        actions=actions,
        explanation=(
            "Inspect the durable outcome before taking recovery action."
            if uncertain or state in {"waiting-for-operator", "uncertain"}
            else None
        ),
    )


def recovery_for_state(state: str, *, uncertain: bool = False) -> OperationRecovery:
    """Compatibility wrapper with the conservative inspection-only default."""

    return recovery_for_operation(state, uncertain=uncertain)


__all__ = [
    "AvailabilityOperationFailure",
    "AvailabilityRecoveryAction",
    "OperationCheckpoint",
    "OperationEvidenceDownload",
    "OperationEvidenceProvenance",
    "OperationFailureEvidence",
    "OperationMemberProgress",
    "OperationPhase",
    "OperationProgress",
    "OperationRecovery",
    "OperationRecoveryAction",
    "normalize_operation_progress",
    "recovery_for_operation",
    "recovery_for_state",
    "sanitize_failure_evidence",
    "validate_progress_update",
]
