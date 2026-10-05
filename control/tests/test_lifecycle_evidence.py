from __future__ import annotations

from vonk_control.lifecycle.evidence import (
    BookkeepingReason,
    Residue,
    read_or_rebuild,
    unknown,
)
from vonk_control.lifecycle.types import Effect


def _damaged() -> int:
    raise ValueError("damaged")


def test_a_readable_value_is_returned() -> None:
    assert read_or_rebuild(kind="k", subject="s", read=lambda: 3) == 3


def test_damaged_state_is_rebuilt_from_evidence() -> None:
    assert read_or_rebuild(kind="k", subject="s", read=_damaged, rebuild=lambda: 7) == 7


def test_unrebuildable_state_retires_as_unknown_without_raising() -> None:
    result = read_or_rebuild(kind="k", subject="s", read=_damaged, rebuild=lambda: None)
    assert isinstance(result, Residue)
    assert result.effect is Effect.UNKNOWN
    assert result.reason is BookkeepingReason.PERSISTED_STATE_DAMAGED
    assert result.observed().effect is Effect.UNKNOWN


def test_a_failing_rebuild_still_retires() -> None:
    result = read_or_rebuild(kind="k", subject="s", read=_damaged, rebuild=_damaged)
    assert isinstance(result, Residue)
    assert "rebuild" in result.note


def test_unknown_names_its_reason() -> None:
    assert unknown(BookkeepingReason.EVIDENCE_MISMATCH, "x").reason == (
        "evidence-mismatch:x"
    )
