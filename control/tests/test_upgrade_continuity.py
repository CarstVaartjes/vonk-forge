"""Reject upgrade bookkeeping that changes a live workload or serving route."""

import pytest

from .upgrade_continuity import ServingSnapshot, assert_upgrade_keeps_serving


@pytest.mark.parametrize(
    "after",
    [
        ServingSnapshot("run", "failed", "endpoint"),
        ServingSnapshot("run", "running", ""),
        ServingSnapshot("other", "running", "endpoint"),
    ],
)
def test_upgrade_fixture_rejects_loss_of_continuity(after):
    states = iter([ServingSnapshot("run", "running", "endpoint"), after])
    with pytest.raises(AssertionError):
        assert_upgrade_keeps_serving(observe=lambda: next(states), upgrade=lambda: None)


def test_upgrade_fixture_accepts_preserved_serving_and_runs_boundary():
    called = []
    assert_upgrade_keeps_serving(
        observe=lambda: ServingSnapshot("run", "running", "endpoint"),
        upgrade=lambda: called.append(True),
    )
    assert called == [True]
