"""Deletion safety and pre-effect validation stay separate from bookkeeping."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from sqlalchemy.orm import Session
from vonk_control.artifact_reference_scan import require_model_sets_open
from vonk_control.categorized_errors import InvalidValue


@pytest.mark.parametrize(
    ("sets", "objects"),
    [(("a" * 64, "b" * 64), ("c" * 64,)), (("a" * 64,), ("invalid",))],
)
def test_invalid_membership_request_has_no_gate_effect(
    sets: tuple[str, ...], objects: tuple[str, ...]
) -> None:
    """Catch creating reference gates before rejecting caller-supplied identities."""
    session = Mock(spec=Session)
    with pytest.raises(InvalidValue):
        require_model_sets_open(
            session, sets, object_digests=objects, now=datetime.now(UTC)
        )
    assert session.mock_calls == []
