from __future__ import annotations

import pytest

from vonk_control.recipe_operations import _cancel_reason


def test_cancellation_reason_is_bounded_and_requires_text() -> None:
    with pytest.raises(Exception, match="required"):
        _cancel_reason("   ")
    assert len(_cancel_reason("x" * 900)) == 512
