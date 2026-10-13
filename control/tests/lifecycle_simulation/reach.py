"""A simulation over ``ReachWorld`` with the second action table and its checks.

It starts with the model on the NAS, as an operator's download leaves it, so
the first loads find it there; removals, evictions and the provider decide
what the later ones find.

Known bugs do not end a run (see ``reach_invariants``): the run goes on to the
end, and then fails with the first known bug it met. Anything else fails at once.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial

from . import reach_actions
from .reach_invariants import ReachMonitor
from .reach_sparks import ReachSparks
from .reach_world import MODEL_SELECTOR, ReachWorld
from .simulation import Simulation, SimulationFailure

#: Rounds the opening download may take.
_OPENING_ROUNDS = 10


@dataclass
class ReachSimulation(Simulation):
    @property
    def reach_monitor(self) -> ReachMonitor:
        assert isinstance(self.monitor, ReachMonitor)
        return self.monitor

    def run(self, steps: int) -> None:
        monitor = self.reach_monitor
        try:
            super().run(steps)
        except SimulationFailure as failure:
            if not monitor.is_known(failure.violation):
                raise
            monitor.set_aside(failure.violation)
        if monitor.tolerated:
            raise SimulationFailure(self.seed, self.trace, monitor.tolerated[0])


def reach_simulation(
    world: ReachWorld, seed: int, *, known: Sequence[str] = ()
) -> ReachSimulation:
    sim = ReachSimulation(
        world,
        seed,
        draw=reach_actions.draw,
        perform=reach_actions.perform,
        sparks_type=ReachSparks,
        monitor_type=partial(ReachMonitor, known=tuple(known)),
    )
    download = world.reach.cache.download_model_selector(
        MODEL_SELECTOR,
        actor="admin",
        request_key=str(uuid.UUID(int=seed, version=4)),
    )
    for _ in range(_OPENING_ROUNDS):
        if world.reach.cache.get_operation(download.id).state == "succeeded":
            break
        sim.round()
    assert world.reach.cache.get_operation(download.id).state == "succeeded"
    return sim
