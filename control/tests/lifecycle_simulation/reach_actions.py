"""The second action table: what reaches the model cache.

A table of its own, so the first table's seeds keep their meaning. The same
rule holds here: adding a kind or changing a weight renumbers every seed of
this table, so re-triage its recorded seeds when it changes.

Loads stay with shapes the first table already covers without a known bug (the
pair on both Sparks, the solo recipe on each Spark, the idle profile), so a
seed reaches the cache instead of an old finding. Besides the first table's
operator and Spark faults, an operator downloads or removes the model, the
provider is down, the NAS runs short of disk (the collector then evicts what
nothing uses), a Spark's reports are lost, slow (it renews its leases) or
stalled (it does not, and reports late), and a Spark's serving processes die.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import actions
from .actions import _JUMPS, Step, _load, _request_key
from .reach_sparks import Leases, ReachSparks
from .reach_world import MODEL_SELECTOR, ReachWorld
from .world import SOLO

if TYPE_CHECKING:
    from .simulation import Simulation


@dataclass(frozen=True)
class ReachStep(Step):
    def describe(self, nodes: tuple[str, ...]) -> str:
        spark = f"Spark {self.node + 1}"
        details = {
            "load-pair": "load pair on both Sparks",
            "load-solo": f"load solo on {spark}",
            "load-solo-both": "load solo on Spark 1 and solo on Spark 2",
            "stop": "load the idle profile (stop everything)",
            "cancel": "cancel the newest unfinished load",
            "restart": "restart the Controller worker",
            "wait": f"nobody acts for {self.amount * 2} rounds",
            "clock-jump": f"clock jumps {_JUMPS[self.amount % len(_JUMPS)]}s",
            "spark-down": f"{spark} unreachable for {self.amount} steps",
            "failed-answer": f"{spark} reports its next answers failed",
            "download-model": "an operator downloads the model to the NAS",
            "remove-model": "an operator removes the model from the NAS",
            "provider-down": f"the model provider is down for {self.amount} steps",
            "nas-short": f"the NAS is short of disk for {self.amount} steps",
            "duplicate-answers": f"{spark} reports twice for {self.amount} steps",
            "lost-results": f"{spark}'s reports are lost for {self.amount} steps",
            "runtime-crash": f"{spark}'s serving processes die",
            "slow-work": (
                f"{spark} works on its next orders for {self.amount} rounds,"
                " renewing their leases"
            ),
            "stalled-work": (
                f"{spark} stalls on its next orders for {self.amount * 2} rounds"
                " without renewing them, then reports late"
            ),
        }
        return details[self.kind]


def _world(sim: Simulation) -> ReachWorld:
    assert isinstance(sim.world, ReachWorld)
    return sim.world


def _load_solo_both(sim: Simulation, step: Step) -> None:
    world = sim.world
    _load(
        sim,
        "solo-both",
        [
            {**world.assignment(SOLO, (node,)), "assignment_name": f"solo-{index + 1}"}
            for index, node in enumerate(world.nodes)
        ],
    )


def _download_model(sim: Simulation, step: Step) -> None:
    _world(sim).reach.cache.download_model_selector(
        MODEL_SELECTOR, actor="admin", request_key=_request_key(sim)
    )


def _remove_model(sim: Simulation, step: Step) -> None:
    _world(sim).reach.cache.remove_model_selector(
        MODEL_SELECTOR, actor="admin", request_key=_request_key(sim)
    )


def _lost_results(sim: Simulation, step: Step) -> None:
    _leases(sim, step).lose_steps = step.amount


def _runtime_crash(sim: Simulation, step: Step) -> None:
    assert isinstance(sim.sparks, ReachSparks)
    sim.sparks.crash(step.node)


def _slow_work(sim: Simulation, step: Step) -> None:
    _leases(sim, step).slow_rounds = step.amount


def _stalled_work(sim: Simulation, step: Step) -> None:
    _leases(sim, step).stall_rounds = step.amount * 2


def _leases(sim: Simulation, step: Step) -> Leases:
    assert isinstance(sim.sparks, ReachSparks)
    return sim.sparks.leases[sim.world.nodes[step.node]]


def _clock_jump(sim: Simulation, step: Step) -> None:
    _world(sim).paused = True
    actions._clock_jump(sim, step)


def _provider_down(sim: Simulation, step: Step) -> None:
    _world(sim).provider.down_steps = step.amount


def _nas_short(sim: Simulation, step: Step) -> None:
    _world(sim).nas_short_steps = step.amount


Perform = Callable[["Simulation", Step], None]
#: Every action of this table and its weight.
ACTIONS: dict[str, tuple[int, Perform]] = {
    "load-pair": (4, actions._load_pair),
    "load-solo": (1, actions._load_solo),
    "load-solo-both": (3, _load_solo_both),
    "stop": (2, actions._stop),
    "cancel": (2, actions._cancel),
    "restart": (2, actions._restart),
    "wait": (4, actions._wait),
    "clock-jump": (1, _clock_jump),
    "spark-down": (1, actions._spark_down),
    "failed-answer": (1, actions._failed_answer),
    "download-model": (2, _download_model),
    "remove-model": (3, _remove_model),
    "provider-down": (2, _provider_down),
    "nas-short": (2, _nas_short),
    "duplicate-answers": (1, actions._duplicate_answers),
    "lost-results": (2, _lost_results),
    "runtime-crash": (2, _runtime_crash),
    "slow-work": (2, _slow_work),
    "stalled-work": (2, _stalled_work),
}


def draw(rng: random.Random) -> Step:
    kinds = tuple(ACTIONS)
    (kind,) = rng.choices(kinds, weights=[ACTIONS[kind][0] for kind in kinds])
    return ReachStep(kind, rng.randrange(2), rng.randrange(2), rng.randint(1, 4))


def perform(sim: Simulation, step: Step) -> None:
    ACTIONS[step.kind][1](sim, step)
