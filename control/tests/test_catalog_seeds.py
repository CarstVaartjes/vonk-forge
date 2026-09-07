from __future__ import annotations

from datetime import UTC, datetime

import pytest
from vonk_control.catalog_seeds import seed_builtin_harnesses


def test_bootstrap_does_not_seed_retired_catalog_entities() -> None:
    result = seed_builtin_harnesses(
        object(), datetime(2026, 9, 5, tzinfo=UTC)  # type: ignore[arg-type]
    )

    assert result.created == 0
    assert result.identifiers == ()


def test_seed_timestamp_must_be_timezone_aware() -> None:
    class _NaiveTime:
        tzinfo = None

    with pytest.raises(ValueError, match="timezone-aware"):
        seed_builtin_harnesses(object(), _NaiveTime())  # type: ignore[arg-type]
