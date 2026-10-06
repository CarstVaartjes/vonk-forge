"""Every Fleet profile and runtime-plan refusal names one of the three categories."""

from __future__ import annotations

from typing import Any

import pytest
from vonk_agent_protocol import (
    ErrorCategory,
    InvalidRequestReason,
    SecurityRefusalReason,
    WaitReason,
)
from vonk_control import fleet_profiles as fp
from vonk_control.host_runtime_plan_authority import (
    RuntimePlanAuthorityError,
    RuntimePlanAuthorityRefused,
    RuntimePlanAuthorityStale,
    RuntimePlanEvidenceUnavailable,
)


@pytest.mark.parametrize(
    ("error", "category", "builtin"),
    [
        (fp.FleetProfileInvalid, ErrorCategory.INVALID_REQUEST, RuntimeError),
        (fp.FleetProfileUnavailable, ErrorCategory.UNKNOWN, RuntimeError),
        (fp.FleetProfileStalePlanConflict, ErrorCategory.INVALID_REQUEST, RuntimeError),
        (fp.FleetProfileReviewStale, ErrorCategory.INVALID_REQUEST, RuntimeError),
        (fp.FleetProfileAdmissionBusy, ErrorCategory.UNKNOWN, RuntimeError),
        (fp.FleetProfileAdmissionEffectBusy, ErrorCategory.UNKNOWN, RuntimeError),
        (fp.FleetProfileAdmissionStorageError, ErrorCategory.UNKNOWN, RuntimeError),
        (
            fp.FleetProfilePermissionDenied,
            ErrorCategory.SECURITY_REFUSAL,
            PermissionError,
        ),
        (fp.FleetProfileChildPlanBlocked, ErrorCategory.UNKNOWN, RuntimeError),
        (RuntimePlanAuthorityRefused, ErrorCategory.SECURITY_REFUSAL, ValueError),
        (RuntimePlanAuthorityStale, ErrorCategory.INVALID_REQUEST, ValueError),
        (RuntimePlanEvidenceUnavailable, ErrorCategory.UNKNOWN, ValueError),
    ],
)
def test_each_refusal_has_one_category_and_keeps_its_builtin(
    error: Any, category: ErrorCategory, builtin: type[Exception]
) -> None:
    reason = {
        ErrorCategory.INVALID_REQUEST: InvalidRequestReason.CONFLICT,
        ErrorCategory.UNKNOWN: WaitReason.OBSERVATION_UNAVAILABLE,
        ErrorCategory.SECURITY_REFUSAL: (
            SecurityRefusalReason.HELPER_REQUEST_PLAN_BINDING_INVALID
        ),
    }[category]
    raised = error("x", reason=reason)
    assert raised.category is category
    assert raised.typed_error() is not None
    assert isinstance(raised, builtin)


def test_handlers_of_the_old_bases_still_catch_every_subclass() -> None:
    for error in (
        fp.FleetProfileInvalid,
        fp.FleetProfileUnavailable,
        fp.FleetProfileStalePlanConflict,
        fp.FleetProfileAdmissionBusy,
        fp.FleetProfileAdmissionEffectBusy,
    ):
        assert issubclass(error, fp.FleetProfileConflict)
    for error in (
        RuntimePlanAuthorityRefused,
        RuntimePlanAuthorityStale,
        RuntimePlanEvidenceUnavailable,
    ):
        assert issubclass(error, RuntimePlanAuthorityError)


def test_default_reasons_are_closed_words() -> None:
    assert fp.FleetProfileStalePlanConflict("x").typed_reason is (
        InvalidRequestReason.SUPERSEDED
    )
    assert fp.FleetProfileAdmissionBusy("x", holder="h").holder == "h"
    assert fp.FleetProfileAdmissionBusy("x").typed_reason is (
        WaitReason.OBSERVATION_UNAVAILABLE
    )
    assert fp.FleetProfilePermissionDenied("x").typed_reason is (
        SecurityRefusalReason.PERMISSION_DENIED
    )
