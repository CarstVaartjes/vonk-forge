"""Deletion safety and pre-effect validation stay separate from bookkeeping."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    InvalidRequestReason,
    UnknownOutcomeError,
)
from vonk_control.artifact_lifecycle import ArtifactLifecycleError
from vonk_control.artifact_reference_scan import _profile_plan, require_model_sets_open
from vonk_control.categorized_errors import InvalidValue


def test_damaged_stored_plan_is_unknown_without_any_deletion_effect() -> None:
    """Catch refusing local owner damage or treating it as an empty plan."""
    with pytest.raises(ArtifactLifecycleError) as caught:
        _profile_plan({})
    assert isinstance(caught.value, UnknownOutcomeError)
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
