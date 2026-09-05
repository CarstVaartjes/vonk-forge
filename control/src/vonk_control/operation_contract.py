"""Shared, bounded contracts for durable Controller operations.

The agent wire protocol intentionally keeps progress as a bounded JSON object.
This module gives the Controller that object a stable meaning without making
the older ``{"phase": ...}`` heartbeat shape invalid.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .logging import redact_text

# ``token`` is overloaded by runtime parameters (for example ``max_tokens``),
# so it is handled as an exact credential field below.  The other terms retain
# field-boundary matching so names such as ``database_password`` remain blocked.
_SENSITIVE_FIELD = re.compile(
    r"(?:^|_)(?:api_key|authorization|cookie|credential|password|private_key|secret)(?:$|_)",
    re.IGNORECASE,
)
_TOKEN_FIELDS = frozenset(
    {
        "access_token",
        "bearer_token",
        "gh_token",
        "github_token",
        "gitlab_token",
        "hf_token",
        "huggingface_token",
        "id_token",
        "refresh_token",
        "session_token",
        "token",
    }
)
_MAX_EVIDENCE_ITEMS = 32
_MAX_EVIDENCE_DEPTH = 4
_MAX_FAILURE_EVIDENCE_BYTES = 8192


def is_secret_field(name: object) -> bool:
    """Return whether a key names a credential field owned by the platform."""

    if not isinstance(name, str):
        return False
    # Split camelCase before case folding so API-shaped names cannot evade the
    # same semantics as their snake_case and kebab-case spellings.
    separated = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", name)
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", separated)
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", separated).strip("_").casefold()
    return bool(_SENSITIVE_FIELD.search(normalized)) or normalized in _TOKEN_FIELDS


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


class OperationCheckpoint(BaseModel):
    """A restart-safe cursor identifying the last completed durable unit."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=0)
    cursor: str | None = Field(default=None, max_length=512)
    digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class OperationMemberProgress(BaseModel):
    """Progress for one node, rank, shard, or other operation member."""

    model_config = ConfigDict(extra="forbid")

    member_id: str = Field(min_length=1, max_length=128)
    phase: str = Field(min_length=1, max_length=80)
    completed_bytes: int = Field(default=0, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    bytes_per_second: float | None = Field(default=None, ge=0, le=10**15)
    eta_seconds: float | None = Field(default=None, ge=0, le=10**9)
    state: str = Field(default="running", min_length=1, max_length=32)

    @model_validator(mode="after")
    def totals_are_consistent(self) -> OperationMemberProgress:
        if self.total_bytes is not None and self.completed_bytes > self.total_bytes:
            raise ValueError("completed bytes cannot exceed total bytes")
        return self


class OperationProgress(BaseModel):
    """Canonical progress payload persisted on the current operation attempt."""

    model_config = ConfigDict(extra="forbid")

    phase: str = Field(min_length=1, max_length=80)
    completed_bytes: int = Field(default=0, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    bytes_per_second: float | None = Field(default=None, ge=0, le=10**15)
    eta_seconds: float | None = Field(default=None, ge=0, le=10**9)
    total_bytes_known: bool = False
    checkpoint: OperationCheckpoint | None = None
    members: list[OperationMemberProgress] = Field(
        default_factory=list, max_length=1024
    )

    @model_validator(mode="before")
    @classmethod
    def accept_compact_wire_names(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        value = dict(value)
        # Keep one canonical API vocabulary while accepting common agent names
        # at the boundary during the rollout of this contract.
        aliases = {
            "bytes_done": "completed_bytes",
            "bytes_completed": "completed_bytes",
            "bytes_total": "total_bytes",
            "rate_bytes_per_second": "bytes_per_second",
            "rate": "bytes_per_second",
        }
        for source, target in aliases.items():
            if target not in value and source in value:
                value[target] = value[source]
            value.pop(source, None)
        if "total_unknown" in value and "total_bytes_known" not in value:
            value["total_bytes_known"] = not bool(value["total_unknown"])
        value.pop("total_unknown", None)
        if value.get("total_bytes") is not None and "total_bytes_known" not in value:
            value["total_bytes_known"] = True
        return value

    @model_validator(mode="after")
    def totals_are_explicit_and_consistent(self) -> OperationProgress:
        if self.total_bytes_known != (self.total_bytes is not None):
            raise ValueError(
                "total_bytes_known must be false when total_bytes is unknown and true when present"
            )
        if (
            self.completed_bytes > self.total_bytes
            if self.total_bytes is not None
            else False
        ):
            raise ValueError("completed bytes cannot exceed total bytes")
        member_ids = [member.member_id for member in self.members]
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("operation progress members must be unique")
        return self


class OperationFailureEvidence(BaseModel):
    """Small, sanitized operator evidence safe to expose in status responses."""

    model_config = ConfigDict(extra="forbid")

    error_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    summary: str = Field(min_length=1, max_length=256)
    detail: str | None = Field(default=None, max_length=1024)
    retryable: bool = False
    uncertain: bool = False


class OperationEvidenceProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=128)
    collected_at: str | None = Field(default=None, max_length=64)
    evidence_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    authority_revision: str | None = Field(default=None, max_length=128)


class OperationEvidenceDownload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_type: str = Field(min_length=1, max_length=128)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    href: str = Field(min_length=1, max_length=512)


class OperationRecovery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uncertain: bool = False
    actions: list[OperationRecoveryAction] = Field(default_factory=list, max_length=4)
    explanation: str | None = Field(default=None, max_length=512)


def normalize_operation_progress(value: Mapping[str, object]) -> dict[str, object]:
    """Validate and canonicalize progress while retaining the legacy phase-only shape."""

    parsed = OperationProgress.model_validate(value)
    document = parsed.model_dump(mode="json", exclude_none=True)
    # Empty optional collections are omitted so the old phase-only response is
    # byte-for-byte stable for callers that have not adopted the contract.
    if not document.get("members"):
        document.pop("members", None)
    if parsed.checkpoint is None:
        document.pop("checkpoint", None)
    if (
        parsed.completed_bytes == 0
        and "completed_bytes" not in value
        and "bytes_done" not in value
        and "bytes_completed" not in value
    ):
        document.pop("completed_bytes", None)
    extended = bool(
        set(value)
        & {
            "completed_bytes",
            "bytes_done",
            "bytes_completed",
            "total_bytes",
            "bytes_total",
            "bytes_per_second",
            "rate_bytes_per_second",
            "rate",
            "eta_seconds",
            "checkpoint",
            "members",
            "total_bytes_known",
            "total_unknown",
        }
    )
    if (
        parsed.total_bytes_known is False
        and not extended
        and "total_bytes_known" not in value
        and "total_unknown" not in value
    ):
        document.pop("total_bytes_known", None)
    return document


def validate_progress_update(
    previous: Mapping[str, object] | None, current: Mapping[str, object]
) -> dict[str, object]:
    """Validate monotonic bytes/checkpoint updates within one leased attempt."""

    normalized = normalize_operation_progress(current)
    if not previous:
        return normalized
    old = normalize_operation_progress(previous)
    # A legacy heartbeat may report only a new phase. Keep the last durable
    # counters/checkpoint instead of treating omitted fields as zero/reset.
    for key in (
        "completed_bytes",
        "total_bytes",
        "total_bytes_known",
        "bytes_per_second",
        "eta_seconds",
        "checkpoint",
        "members",
    ):
        if key not in normalized and key in old:
            normalized[key] = old[key]
    old_bytes = int(old.get("completed_bytes", 0))
    new_bytes = int(normalized.get("completed_bytes", 0))
    if new_bytes < old_bytes:
        raise ValueError("operation progress bytes cannot move backwards")
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
    return normalized


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
                if not isinstance(key, str) or is_secret_field(key):
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
    "OperationCheckpoint",
    "OperationEvidenceDownload",
    "OperationEvidenceProvenance",
    "OperationFailureEvidence",
    "OperationMemberProgress",
    "OperationPhase",
    "OperationProgress",
    "OperationRecovery",
    "OperationRecoveryAction",
    "is_secret_field",
    "normalize_operation_progress",
    "recovery_for_operation",
    "recovery_for_state",
    "sanitize_failure_evidence",
    "validate_progress_update",
]
