"""Canonical result serialization and phase evidence."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import (
    Any,
)

from pydantic import BaseModel, TypeAdapter, ValidationError
from vonk_agent_protocol import (
    AgentFailureResult,
    Disposition,
    LifecycleState,
    OperationProgress,
    WaitReason,
    canonical_message,
)

from ..bounded_json import require_mapping
from ..distribution_assignment import NodeDistributionAssignment
from ..failure_classification import disposition, error_detail, is_security_failure
from ..lifecycle.evidence import (
    Residue,
)
from ..models import (
    Job,
)
from ..operation_blockers import (
    OperationBlocker,
)
from ..operation_progress import sample_progress
from ..persisted_waits import read_wait
from ..recipe_lifecycle_contract import RecipeLifecycleResult
from ..recipe_operations import (
    RecipeOperationView,
)
from ..recovery_policy import (
    FailureKind,
    kind_for_failure_fields,
)
from ..run_switch_contract import (
    RunSwitchDistributionChildResult,
    RunSwitchMemberReceipt,
    RunSwitchMemberState,
    RunSwitchOperation,
    RunSwitchOperationResult,
    RunSwitchPhase,
    RunSwitchPhaseKind,
    RunSwitchPhaseResult,
    RunSwitchProgressState,
    RunSwitchSubphase,
)
from ..run_switch_observation_contract import (
    RunSwitchObservedEvidence,
)
from .constants import (
    _MEMBER_STATE_ADAPTER,
    _PHASES,
    _PROGRESS_STATE_ADAPTER,
    _SUBPHASE_ADAPTER,
)
from .errors import RunSwitchRetryLater, retry_later_unless_deterministic
from .results import _stored_result


def _bound_workload_intent(progress: RunSwitchOperationResult) -> int:
    ordinal = progress.workload_intent_ordinal
    if type(ordinal) is not int or ordinal < 1:
        raise RunSwitchRetryLater("run-switch workload intent is unbound")
    return ordinal


def _progress_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _progress_state(value: object) -> RunSwitchMemberState | None:
    # A canonical unissued recipe role is pending in the switch projection.
    if value == LifecycleState.QUEUED:
        return "pending"
    try:
        return _MEMBER_STATE_ADAPTER.validate_python(value, strict=True)
    except ValidationError:
        return None


def _progress_operation_state(value: object) -> RunSwitchProgressState:
    try:
        return _PROGRESS_STATE_ADAPTER.validate_python(value, strict=True)
    except ValidationError:
        return "unknown"


def _progress_subphase(value: object) -> RunSwitchSubphase | None:
    try:
        return _SUBPHASE_ADAPTER.validate_python(value, strict=True)
    except ValidationError:
        return None


def _observe_progress(
    previous: OperationProgress | None, current: OperationProgress, now: datetime
) -> OperationProgress:
    """Sample the canonical operation meter without a JSON round trip."""
    return sample_progress(previous, current, now)


def _progress_phase(value: object) -> RunSwitchPhaseKind | None:
    return value if value in _PHASES else None


def _parse_persisted_result(value: object) -> RunSwitchOperationResult | None:
    result = _stored_result(value)
    return None if isinstance(result, Residue) else result


def _progress_damaged(value: object) -> bool:
    """Whether the stored result exists but cannot be read (evidence unknown)."""

    return isinstance(_stored_result(value), Residue)


def _read_progress(value: object) -> RunSwitchOperationResult:
    result = _parse_persisted_result(value)
    return result if result is not None else RunSwitchOperationResult()


def _wait_blockers(
    job: Job, progress: RunSwitchOperationResult
) -> list[OperationBlocker]:
    wait = read_wait(job)
    return wait.blockers if wait else []


def _persisted_result(value: RunSwitchOperationResult) -> Any:
    """The single canonical result serializer at the ORM boundary."""
    # In-place mutations of default collections do not enter model_fields_set,
    # so persist every field's value (explicit None included): readers index
    # keys such as ``observation_due_at`` directly.
    return value.model_dump(mode="json")


_PHASE_RESULT_ADAPTER = TypeAdapter(RunSwitchPhaseResult)


def _phase_result(
    value: object,
    *,
    phase: RunSwitchPhase | None = None,
) -> RunSwitchPhaseResult:
    """Validate one phase receipt before it enters durable progress."""

    result: RunSwitchPhaseResult | None = None
    failure: Exception | None = None
    try:
        normalized = (
            value.model_dump(mode="json")
            if isinstance(value, BaseModel)
            else dict(require_mapping(value, "phase receipt"))
        )
        belongs = True
        if phase is not None:
            belongs = "phase" not in normalized or normalized["phase"] == phase.kind
            normalized.setdefault("phase", phase.kind)
            subphase = phase.subphase
            if subphase is None and phase.kind in {"transfer", "verify"}:
                subphase = "target-copy"
            # A receipt of another phase or subphase is not this phase's.
            belongs = belongs and (
                "subphase" not in normalized or normalized["subphase"] == subphase
            )
            normalized.setdefault("subphase", subphase)
        assignments = normalized.get("assignments")
        if isinstance(assignments, Mapping):
            normalized["assignments"] = {
                node_id: NodeDistributionAssignment.parse(raw)
                if isinstance(raw, Mapping)
                else raw
                for node_id, raw in assignments.items()
            }
        if belongs:
            result = _PHASE_RESULT_ADAPTER.validate_json(
                canonical_message(normalized), strict=True
            )
    except (TypeError, ValueError) as error:
        failure = error
    if failure is not None:
        # A receipt that does not validate fails the same way on every read.
        raise retry_later_unless_deterministic(
            f"run-switch phase receipt is invalid: {error_detail(failure)}",
            failure,
            reason=WaitReason.RECEIPT_MISSING,
        ) from failure
    if result is None:
        raise RunSwitchRetryLater(
            "run-switch phase receipt is invalid",
            reason=WaitReason.RECEIPT_MISSING,
        )
    return result


def _child_result(
    child: object,
) -> (
    RunSwitchDistributionChildResult
    | RunSwitchPhaseResult
    | RecipeLifecycleResult
    | None
):
    """Read the receipt retained by the owning canonical child DTO."""
    from ..distribution_executor.receipts import _ChildView

    if isinstance(child, RecipeOperationView):
        return child.lifecycle_result
    if isinstance(child, _ChildView):
        return child.result
    return None


def _child_progress_payload(child: object) -> RunSwitchObservedEvidence:
    """Consume explicit executor DTOs at their typed observation boundary."""
    from ..distribution_executor.receipts import _ChildView

    if isinstance(child, RecipeOperationView):
        result, state, reason = _child_result(child), child.state, child.status_reason
    elif isinstance(child, _ChildView):
        result, state, reason = _child_result(child), child.state, None
    elif isinstance(child, RunSwitchOperation):
        result, state, reason = child.progress, child.state, child.status_reason
    else:
        return RunSwitchObservedEvidence(uncertain=True)
    try:
        raw = (
            result.model_dump(mode="json") if isinstance(result, BaseModel) else result
        )
        observed = RunSwitchObservedEvidence.model_validate_json(
            canonical_message(raw), strict=True
        )
    except (TypeError, ValueError):
        observed = RunSwitchObservedEvidence(uncertain=True)
    if isinstance(child, RecipeOperationView) and child.progress is not None:
        measured = child.progress
        observed.operation = measured
        observed.completed_bytes = measured.completed_bytes
        observed.total_bytes = measured.total_bytes
        observed.members = [
            RunSwitchMemberReceipt(
                node_id=member.member_id,
                state=_progress_state(member.state) or "unknown",
                phase=_progress_phase(member.phase),
                completed_bytes=member.completed_bytes,
                total_bytes=member.total_bytes,
            )
            for member in measured.members
        ]
    observed.child_state = state
    observed.status_reason = reason[:512] if reason is not None else None
    return observed


def _child_failure_code(evidence: RunSwitchObservedEvidence) -> str | None:
    codes = [evidence.error_code] if evidence.error_code else []
    for members in (evidence.node_evidence, evidence.launch_evidence):
        if members is not None:
            codes.extend(
                item.error_code
                for item in members.values()
                if isinstance(item, AgentFailureResult) and item.error_code
            )
    return next(
        (value for value in codes if is_security_failure(value)),
        codes[0] if codes else None,
    )


def _child_failure_kind(child: object) -> FailureKind:
    payload = _child_progress_payload(child)
    if is_security_failure(_child_failure_code(payload)):
        return FailureKind.INVALID_AUTHORITY
    if payload.uncertain:
        return FailureKind.UNCERTAIN_EFFECT
    if (
        payload.failure_kind is not None
        or payload.error_code is not None
        or payload.uncertain
    ):
        return kind_for_failure_fields(
            payload.failure_kind, payload.error_code, payload.uncertain
        )
    kinds = []
    for members in (payload.node_evidence, payload.launch_evidence):
        if members is not None:
            kinds.extend(
                kind_for_failure_fields(
                    item.failure_kind, item.error_code, item.uncertain is True
                )
                if isinstance(item, AgentFailureResult)
                else FailureKind.INVALID_CONTRACT
                for item in members.values()
            )
    # Every member waits: the child waits (on the first member's cause).
    if kinds and all(disposition(kind) is Disposition.WAIT for kind in kinds):
        return kinds[0]
    if kinds and all(kind is FailureKind.UNCERTAIN_EFFECT for kind in kinds):
        return FailureKind.UNCERTAIN_EFFECT
    return FailureKind.INVALID_CONTRACT


def _without_observation_time(
    value: RunSwitchOperationResult,
) -> RunSwitchOperationResult:
    """Compare durable progress while excluding only the measured clock/rate."""
    result = value.model_copy(deep=True)
    if result.operation is not None:
        result.operation = result.operation.model_copy(
            update={
                "observed_at": None,
                "last_progress_at": None,
                "elapsed_seconds": None,
                "bytes_per_second": None,
                "smoothed_bytes_per_second": None,
                "eta_seconds": None,
            }
        )
    return result
