"""The simulation's "no in-flight row outlives its owner" invariant.

A wrong implementation that hardcodes the in-flight states, ignores the live
operation that names a row, or flags a row inside the owner's settle grace
fails here. The simulation itself (``test_lifecycle_liveness_simulation``) is
what proves the Controller never leaves such a row behind.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, cast

from vonk_agent_protocol import InstallationState, RunState
from vonk_control.models import RecipeInstallation

from .lifecycle_simulation import invariants
from .lifecycle_simulation.invariants import (
    IN_FLIGHT_KINDS,
    ORPHAN_GRACE,
    InFlightKind,
    Monitor,
    _orphans,
    _Snapshot,
)
from .test_stuck_installing_installs import (
    SETTLED,
    _fresh_install,
    _residues,
    _setup,
    _supersede,
)


class _Colour(Enum):
    RED = "red"
    GREEN = "green"
    BLUE = "blue"


def _snapshot(orphans: frozenset[str]) -> _Snapshot:
    return _Snapshot({}, frozenset(), frozenset(), orphans)


def test_in_flight_states_are_the_enum_minus_the_settled_ones() -> None:
    kind = InFlightKind(
        "colour",
        RecipeInstallation,
        _Colour,
        frozenset({_Colour.RED}),
        lambda session, ids: frozenset(),
    )
    # A state added to the enum is checked until it is named settled.
    assert kind.in_flight == ("green", "blue")


def test_the_kinds_checked_and_their_in_flight_states() -> None:
    by_name = {kind.name: kind.in_flight for kind in IN_FLIGHT_KINDS}
    # ``planned`` installations join with the release path that owns them.
    assert by_name == {
        "installation": (InstallationState.INSTALLING.value,),
        "run": (
            RunState.PLANNED.value,
            RunState.STARTING.value,
            RunState.STOPPING.value,
        ),
    }


def test_dead_owner_states_are_states_of_the_owner() -> None:
    assert invariants._DEAD_OWNER_STATES["run"] <= {state.value for state in RunState}
    assert "failed" in invariants._DEAD_OWNER_STATES["run"]
    assert "cancelled" not in invariants._DEAD_OWNER_STATES["recipe-build"]


def test_an_installing_row_is_an_orphan_only_when_no_live_operation_names_it(
    tmp_path: Path,
) -> None:
    sessions, service, mapping_id, build_id = _setup(tmp_path)
    operation = _fresh_install(sessions, service, mapping_id, build_id)
    with sessions() as session:
        assert _orphans(session) == frozenset()
    _supersede(sessions, service, operation)
    with sessions() as session:
        assert _orphans(session) == {
            f"installation {operation.owner_id} stays installing with no live operation"
        }
    # The sweep that ends the row is what clears the invariant, and a fresh
    # request is admitted afterwards.
    _residues(sessions, service, SETTLED).tick()
    with sessions() as session:
        assert _orphans(session) == frozenset()
    _fresh_install(sessions, service, mapping_id, build_id)


def test_an_orphan_is_a_violation_only_beyond_the_grace_and_clears_when_healed() -> (
    None
):
    monitor = Monitor(world=cast(Any, None), sparks=cast(Any, None))
    start = datetime(2026, 1, 1, tzinfo=UTC)
    orphan = frozenset({"installation x stays installing with no live operation"})
    assert monitor._orphans(_snapshot(orphan), start) == []
    assert monitor._orphans(_snapshot(orphan), start + ORPHAN_GRACE) == []
    (violation,) = monitor._orphans(
        _snapshot(orphan), start + ORPHAN_GRACE + timedelta(seconds=1)
    )
    assert violation.invariant == "no in-flight row outlives its owner"
    # Healed, then orphaned again: the clock starts over.
    assert monitor._orphans(_snapshot(frozenset()), start + 2 * ORPHAN_GRACE) == []
    assert monitor._orphans(_snapshot(orphan), start + 3 * ORPHAN_GRACE) == []
