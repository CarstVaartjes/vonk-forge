"""Assessment support for Fleet profiles."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError
from vonk_agent_protocol import (
    LifecycleState,
    ProfileReasonCode,
    WaitReason,
    canonical_message,
)
from vonk_agent_protocol.agent_words import (
    ProfileAction,
    ProfileDocumentState,
    ProfileReasonSeverity,
)

from ..bounded_json import sequence
from ..failure_classification import is_security_failure
from ..fleet_profile_contract import (
    FleetProfileApplicationProgress,
    FleetProfileAssignmentAssessment,
    FleetProfileAssignmentPreview,
    FleetProfileOperationState,
    FleetProfilePreview,
    FleetProfileReason,
)
from ..lifecycle.evidence import BookkeepingReason, retire_as_unknown
from ..lifecycle.types import State as _LifecycleState
from ..operation_blockers import OperationBlocker, bound_blockers, make_blocker
from ..preparation_contract import RolloutPreparation
from ..storage_demands import STORAGE_INSUFFICIENT, StorageRelief
from ..strict_json import read_stored_model
from .contracts import (
    FleetProfileAdmissionEffectBusy,
    FleetProfileResourceRecheckUnavailable,
    _FleetProfileRecoveryBindingConflict,
)
from .dependencies import (
    _DISK_REFUSALS,
    _OPERATION_STATE_ADAPTER,
    _PREPARATION_RESOLVABLE_CODES,
)


def _recovery_preparation_identity(preparation: RolloutPreparation) -> object:
    """Retain typed artifact identities, excluding observations and build provenance."""

    return preparation.model_dump(
        mode="json",
        exclude={
            "model": {"controller", "targets", "completeness"},
            "runtime_image": {"controller", "targets", "build_id"},
            "exceptions": {"__all__": {"state", "reason"}},
            "controller_ready": True,
            "targets_ready": True,
            ProfileDocumentState.READY.value: True,
            "reasons": True,
        },
    )


def _require_recovery_preparation(
    assignment_id: str,
    expected: RolloutPreparation | None,
    observed: RolloutPreparation | None,
    *,
    first_binding: bool = False,
) -> None:
    if observed is None:
        image = expected.runtime_image if expected is not None else None
        exact = (
            f", image {image.image_digest}, archive {image.oci_layout_sha256}"
            if image is not None
            else ""
        )
        raise _FleetProfileRecoveryBindingConflict(
            f"{ProfileReasonCode.RECOVERY_CACHE_PENDING}: Controller cache preparation is pending for "
            f"assignment {assignment_id}{exact}"
            + ("; recovery retains these exact identities." if exact else ".")
        )
    if expected is None:
        # The accepted plan never bound an identity for this assignment (it was
        # still waiting for its preparation, or its record is gone): there is no
        # bound identity to be replaced, so the verified preparation observed now
        # is what is bound, and Run/Switch verifies that digest again at ingress
        # when its child starts.
        if not first_binding:
            retire_as_unknown(
                "profile-recovery-identity",
                assignment_id,
                BookkeepingReason.EVIDENCE_UNAVAILABLE,
                "the accepted artifact identity is unavailable; binding the "
                "verified preparation observed now",
            )
        return
    if _recovery_preparation_identity(expected) != _recovery_preparation_identity(
        observed
    ):
        raise _FleetProfileRecoveryBindingConflict(
            f"{ProfileReasonCode.RECOVERY_ARTIFACT_CHANGED}: Prepared assets for assignment "
            f"{assignment_id} differ from its accepted model/image identity; "
            "restore the exact accepted assets or use an explicit new load "
            "to bind the replacement.",
            reason=WaitReason.RETAINED_IDENTITY_MISMATCH,
        )


def _preview_blocker_codes(preview: FleetProfilePreview) -> list[str]:
    return [reason.code for reason in preview.reasons] + [
        blocker.code
        for item in preview.assessments
        for blocker in item.assessment.blockers
    ]


def _preview_blockers(preview: FleetProfilePreview) -> list[OperationBlocker]:
    """The typed reasons a reviewed plan cannot be admitted yet, node ids included."""

    blockers = [
        make_blocker(reason.code, reason.detail, severity=reason.severity)
        for reason in preview.reasons
        if reason.severity != ProfileReasonSeverity.INFO.value
    ]
    for item in preview.assessments:
        blockers.extend(
            make_blocker(
                reason.code,
                reason.detail,
                severity=ProfileReasonSeverity.ERROR.value
                if reason.severity == ProfileReasonSeverity.BLOCKER.value
                else reason.severity,
                node_ids=reason.node_ids,
            )
            for reason in item.assessment.blockers
        )
    return bound_blockers(blockers)


def _assignments_needing_preparation(
    assignments: Sequence[FleetProfileAssignmentPreview],
    prepared: set[str],
    reasons: Sequence[FleetProfileReason],
    assessments: Sequence[FleetProfileAssignmentAssessment],
) -> list[FleetProfileAssignmentPreview]:
    """Assignments that must place assets nobody has prepared yet.

    Anything the fleet or the recipe cannot resolve by itself (a Spark that is
    missing, an incomplete topology) never starts a preparation.
    """

    assessed_missing = {
        item.assignment_id
        for item in assessments
        if any(
            reason.code in _PREPARATION_RESOLVABLE_CODES
            for reason in item.assessment.blockers
        )
    }
    reason_missing = any(
        reason.code in _PREPARATION_RESOLVABLE_CODES for reason in reasons
    )
    return [
        assignment
        for assignment in assignments
        if ProfileAction.SWITCH.value in assignment.actions
        and not any(
            reason.severity == ProfileReasonSeverity.ERROR.value
            for reason in assignment.reasons
        )
        and (
            assignment.assignment_id in assessed_missing
            or (reason_missing and assignment.assignment_id not in prepared)
        )
    ]


def _progress_with_blockers(
    progress: FleetProfileApplicationProgress,
    blockers: Sequence[OperationBlocker],
    **changes: object,
) -> FleetProfileApplicationProgress:
    """Canonical progress document carrying the application's current blockers."""

    data = progress.model_dump(mode="json")
    data["blockers"] = [
        item.model_dump(mode="json") for item in bound_blockers(blockers)
    ]
    data.update(changes)
    return read_stored_model(
        FleetProfileApplicationProgress,
        canonical_message(data),
        strict=True,
        from_json=True,
    )


def _profile_preview_is_waitable(preview: FleetProfilePreview) -> bool:
    """Any blocker except a security boundary parks the intent for re-planning."""
    return not any(
        is_security_failure(code) for code in _preview_blocker_codes(preview)
    )


def _disk_shortfalls(assessment: Any) -> tuple[tuple[str, int], ...]:
    """``(node_id, free_bytes_needed)`` for every Spark an assessment refuses
    (or plans an eviction) for lack of disk."""

    refused = {
        node_id
        for reason in (*assessment.blockers, *assessment.warnings)
        if reason.code in _DISK_REFUSALS
        for node_id in reason.node_ids
    }
    fit = assessment.fit_after_stop or assessment.fit_current
    return tuple(
        (node.node_id, node.disk_free_bytes - node.disk_free_after_bytes)
        for node in fit.nodes
        if node.node_id in refused
        and node.disk_free_bytes is not None
        and node.disk_free_after_bytes is not None
        and node.disk_free_after_bytes < 0
    )


def _storage_wait_of_preview(
    preview: FleetProfilePreview,
) -> FleetProfileAdmissionEffectBusy | None:
    """The disk a blocked plan is waiting for, as a bounded storage wait."""

    shortfalls = tuple(
        shortfall
        for item in preview.assessments
        for shortfall in _disk_shortfalls(item.assessment)
    )
    if not shortfalls:
        return None
    return FleetProfileAdmissionEffectBusy(
        "Waiting for disk on "
        + ", ".join(sorted({node_id for node_id, _needed in shortfalls}))
        + ".",
        shortfalls=shortfalls,
    )


def _storage_wait_of(error: BaseException) -> FleetProfileAdmissionEffectBusy | None:
    if isinstance(error, FleetProfileAdmissionEffectBusy) and error.shortfalls:
        return error
    return None


def _relief_blocker(node_id: str, found: StorageRelief) -> OperationBlocker:
    return make_blocker(
        found.code,
        found.detail,
        severity=ProfileReasonSeverity.ERROR.value
        if found.code == STORAGE_INSUFFICIENT
        else ProfileReasonSeverity.WARNING.value,
        node_ids=(node_id,),
    )


def _deferral_code(error: BaseException) -> str:
    """The blocker code a parked admission shows for the refusal that parked it."""

    if isinstance(error, FleetProfileResourceRecheckUnavailable):
        return error.code
    return ProfileReasonCode.ADMISSION_BUSY


def _require_recovery_preparations(
    accepted: FleetProfilePreview, current: FleetProfilePreview
) -> None:
    expected = {item.assignment_id: item.preparation for item in accepted.preparations}
    observed = {item.assignment_id: item.preparation for item in current.preparations}
    current_assignments = {item.assignment_id: item for item in current.assignments}
    for assignment in accepted.assignments:
        current_assignment = current_assignments.get(assignment.assignment_id)
        if (
            assignment.actions == [ProfileAction.KEEP.value]
            and current_assignment is not None
            and current_assignment.actions == [ProfileAction.KEEP.value]
        ):
            # Kept runtime state needs no cache preparation. A later run child
            # still checks its accepted identity before it can issue any work.
            continue
        _require_recovery_preparation(
            assignment.assignment_id,
            expected.get(assignment.assignment_id),
            observed.get(assignment.assignment_id),
            first_binding=not accepted.allowed,
        )


def _operation_state(
    value: object, *, default: FleetProfileOperationState
) -> FleetProfileOperationState:
    """Read one stored child operation state, keeping absence distinct.

    ``None`` is genuinely absent, so the caller's deliberate default stands.  A
    stored state outside the closed contract is unknown, and unknown is observed
    again (the caller's default is the observing state), never parked.
    """

    if value is None:
        return default
    try:
        return _OPERATION_STATE_ADAPTER.validate_python(value, strict=True)
    except ValidationError:
        retire_as_unknown(
            "profile-child-state",
            str(value)[:80],
            BookkeepingReason.PERSISTED_STATE_DAMAGED,
            "stored child operation state is outside the contract",
        )
        return default


def _stored_state(state: _LifecycleState | None) -> FleetProfileOperationState:
    """The stored label of a profile's aggregate state (``succeeded`` when empty)."""

    return _operation_state(
        LifecycleState.SUCCEEDED.value if state is None else state.value,
        default=LifecycleState.RUNNING,
    )


def _string_items(value: object, *, fallback: Sequence[str] = ()) -> list[str]:
    """Read a decoded JSON string array; damaged elements are dropped.

    A document that is not an array reads as ``fallback`` (the value derived from
    another receipt); an element that is not a string is skipped, never a reason
    to refuse the rest.
    """

    items = sequence(value)
    if items is None:
        if value is not None:
            retire_as_unknown(
                "profile-string-list",
                "state",
                BookkeepingReason.PERSISTED_STATE_DAMAGED,
                "stored value is not an array",
            )
        return list(fallback)
    return [item for item in items if isinstance(item, str)]
