"""A collector's pass budget starts after its inventory, in every pass-budget module.

The registry ``PASS_BUDGET_MODULES`` lists every module whose deadline the tests
freeze. Each must either be probed here (its setup/inventory is slowed past the
budget and its first candidate must still be attempted) or be classified with
the reason its deadline cannot be spent by an inventory. A new registry entry
fails this file until it is one or the other.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime

import pytest
from sqlalchemy.orm import Session
from vonk_control.catalog_revision_collection import (
    SWEEP_BUDGET_SECONDS,
    CatalogRevisionCollector,
)
from vonk_control.model_cache import partials_collection

from .pass_budget_clocks import PASS_BUDGET_MODULES, PassClock
from .test_catalog_revision_collection import _refresh, catalog  # noqa: F401
from .test_model_cache import cache  # noqa: F401
from .test_model_partials_collection import (  # noqa: F401
    _age,
    _orphan,
    collector,
)

Probe = Callable[[pytest.FixtureRequest, PassClock, pytest.MonkeyPatch], bool]


def _catalog_revisions(
    request: pytest.FixtureRequest, clock: PassClock, monkeypatch: pytest.MonkeyPatch
) -> bool:
    world = request.getfixturevalue("catalog")
    old, _head = _refresh(world, "glm")
    candidates = CatalogRevisionCollector._candidates

    def slow(session: Session, cutoff: datetime) -> object:
        clock.advance(SWEEP_BUDGET_SECONDS + 1)
        return candidates(session, cutoff)

    monkeypatch.setattr(CatalogRevisionCollector, "_candidates", staticmethod(slow))
    world.collector().collect()
    return not world.exists(old)


def _model_partials(
    request: pytest.FixtureRequest, clock: PassClock, monkeypatch: pytest.MonkeyPatch
) -> bool:
    gatherer, service, test_clock = request.getfixturevalue("collector")
    directory = _orphan(service, "c" * 64)
    _age(directory)
    test_clock.advance(days=3)
    scandir = os.scandir

    def slow(path):  # type: ignore[no-untyped-def]
        clock.advance(partials_collection.PASS_BUDGET_SECONDS + 1)
        return scandir(path)

    monkeypatch.setattr(os, "scandir", slow)
    gatherer.collect()
    return not directory.exists()


PROBES: dict[str, Probe] = {
    "vonk_control.catalog_revision_collection": _catalog_revisions,
    "vonk_control.model_cache.partials_collection": _model_partials,
}

# Registry entries with no inventory before their deadline, or guarded elsewhere.
CLASSIFIED = {
    "vonk_control.due_waits": "deadline set on entry; work is the pass, no inventory",
    "vonk_control.terminal_history_collection": "deadline precedes only the batch it bounds",
    "vonk_control.unused_storage_collection.pressure": (
        "test_unused_storage_collection: every slow step, one shared attempt budget"
    ),
    "vonk_control.fleet_profile_adapter_conversion": "deadline bounds the scan itself",
    "vonk_control.recipe_image_availability.removal_acceptance": "bounds the checks",
    "vonk_control.recipe_image_availability.removal_execution": "bounds the removals",
    "vonk_control.model_cache.removal_execution": "bounds object deletes; one always runs",
    "vonk_control.model_cache.removal_reconcile": "step deadline set before the removals",
    "vonk_control.library_projection.views": "request budget, not a collector",
    "vonk_control.library_projection.local_state": "request budget, not a collector",
    "vonk_control.library_assessment": "probe budget, not a collector",
    "vonk_control.library_image_presence": "probe budget, not a collector",
    "vonk_control.profile_read_observation": "read budget, not a collector",
}


def test_every_pass_budget_module_is_probed_or_classified() -> None:
    """Catches a new pass-budget module nobody checked for the inventory mistake."""

    unknown = set(PASS_BUDGET_MODULES) - set(PROBES) - set(CLASSIFIED)
    assert not unknown, f"probe or classify: {sorted(unknown)}"
    assert not (set(PROBES) | set(CLASSIFIED)) - set(PASS_BUDGET_MODULES)


@pytest.mark.parametrize("module", [m for m in PASS_BUDGET_MODULES if m in PROBES])
def test_a_slow_inventory_still_attempts_the_first_candidate(
    module: str,
    request: pytest.FixtureRequest,
    frozen_pass_budgets: PassClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a pass budget that starts before the collector's inventory."""

    assert PROBES[module](request, frozen_pass_budgets, monkeypatch)
    assert frozen_pass_budgets.seconds > 0
