"""Deletion safety and pre-effect validation stay separate from bookkeeping."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    ArtifactLifecycleCode,
    ErrorCategory,
    InvalidRequestReason,
    SecurityRefusalReason,
)
from vonk_control.artifact_lifecycle import ArtifactLifecycleError
from vonk_control.artifact_reference_scan import _profile_plan, require_model_sets_open
from vonk_control.categorized_errors import InvalidValue


def test_unverified_deletion_plan_is_a_destructive_security_edge() -> None:
    """Catch treating a destructive scope proof as ordinary admission contention."""
    with pytest.raises(ArtifactLifecycleError) as caught:
        _profile_plan({})
    assert caught.value.category is ErrorCategory.SECURITY_REFUSAL  # type: ignore[attr-defined]
    assert caught.value.typed_reason is SecurityRefusalReason.OPERATION_INVALID_ARTIFACT  # type: ignore[attr-defined]
    assert caught.value.code == ArtifactLifecycleCode.REFERENCE_SCAN_FAILED
    assert caught.value.retryable


@pytest.mark.parametrize(
    ("sets", "objects"),
    [(("a" * 64, "b" * 64), ("c" * 64,)), (("a" * 64,), ("invalid",))],
)
def test_invalid_membership_request_has_no_gate_effect(
    sets: tuple[str, ...], objects: tuple[str, ...]
) -> None:
    """Catch creating reference gates before rejecting caller-supplied identities."""
    session = Mock(spec=Session)
    with pytest.raises(InvalidValue) as caught:
        require_model_sets_open(
            session, sets, object_digests=objects, now=datetime.now(UTC)
        )
    assert caught.value.typed_reason in {
        InvalidRequestReason.CONFLICT,
        InvalidRequestReason.MALFORMED,
    }
    assert session.mock_calls == []
