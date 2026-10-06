"""Fixture adapter for continuity across Controller/agent upgrade and restart.

Service tests own the real persistence and process boundary. This helper runs
that supplied boundary and checks its observations; it does not simulate an
upgrade or claim that a passing fixture proves physical continuity.
"""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ServingSnapshot:
    run_id: str
    state: str
    route_target: str


def assert_upgrade_keeps_serving(
    *, observe: Callable[[], ServingSnapshot], upgrade: Callable[[], None]
) -> None:
    before = observe()
    assert before.state == "running", "fixture must begin with a running workload"
    assert before.run_id and before.route_target, (
        "fixture must begin with a serving route"
    )
    upgrade()
    after = observe()
    assert after.run_id == before.run_id, "upgrade replaced the accepted workload"
    assert after.state == "running", "upgrade settled a running workload"
    assert after.route_target == before.route_target, (
        "upgrade withdrew or replaced a serving route"
    )
