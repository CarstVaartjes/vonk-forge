"""What operators, Sparks and faults do to the world, one seeded step at a time.

A step is drawn from the seed alone (kind first, then its parameters in a fixed
order), so a seed names the same sequence whatever the world does. Adding a
kind or changing a weight renumbers every seed's sequence: re-triage the
recorded seeds when the table changes.

To add an action: write ``_name(sim, step)`` and add it to ``ACTIONS`` with a
weight.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import patch

from sqlalchemy import select
from vonk_control.models import FleetProfileApplication
from vonk_control.run_switch_operations import RunSwitchOperationService

from .world import PAIR, SOLO, SOLO_B

if TYPE_CHECKING:
    from .simulation import Simulation

#: Clock jumps: a short pause, a lease, an hour of backoff, a night.
_JUMPS = (120, 900, 3600, 6 * 3600)
_ENDED = ("succeeded", "failed", "cancelled", "superseded")


@dataclass(frozen=True)
class Step:
    kind: str
    node: int
    other: int
    amount: int

    def describe(self, nodes: tuple[str, ...]) -> str:
        spark = f"Spark {self.node + 1}"
        details = {
            "load-pair": "load pair on both Sparks",
            "load-solo": f"load solo on {spark}",
            "load-concurrently": (
                "two requesters load pair and solo"
                if self.node == self.other
                else "two requesters load solo on Spark 1 and on Spark 2"
            ),
            "load-stacked": f"load {_stack_name(self)} (two recipes on {spark})",
            "stale-stacked-load": (
                f"load {_stack_name(self)} as reviewed by an older Controller"
                " (its stored plan promises one port twice)"
            ),
            "cancel": "cancel the newest unfinished load",
            "stop": "load the idle profile (stop everything)",
            "restart": "restart the Controller worker",
            "spark-down": f"{spark} unreachable for {self.amount} steps",
            "late-answers": f"{spark} answers {self.amount} rounds late",
            "duplicate-answers": f"{spark} answers twice for {self.amount} steps",
            "failed-answer": f"{spark} reports its next answers failed",
            "clock-jump": f"clock jumps {_JUMPS[self.amount % len(_JUMPS)]}s",
            "disk-pressure": f"{spark} is short of disk for {self.amount} steps",
            "wait": f"nobody acts for {self.amount * 2} rounds",
            "revision-unreadable": (
                f"the {(PAIR, SOLO)[self.node]} revision becomes unreadable;"
                " a successor is published"
            ),
        }
        return details[self.kind]


def _request_key(sim: Simulation) -> str:
    return str(uuid.UUID(int=sim.rng.getrandbits(128), version=4))


def _load(sim: Simulation, name: str, assignments: list[dict[str, object]]) -> None:
    if name not in sim.profiles:
        sim.profiles[name] = sim.world.create_profile(name, assignments)
    profile_id, _number = sim.profiles[name]
    sim.world.controller.profiles.apply(
        profile_id, request_key=_request_key(sim), actor="admin"
    )


def _load_pair(sim: Simulation, step: Step) -> None:
    world = sim.world
    _load(sim, "pair", [world.assignment(PAIR, world.nodes)])


def _load_solo(sim: Simulation, step: Step) -> None:
    world = sim.world
    node = world.nodes[step.node]
    _load(sim, f"solo-{step.node + 1}", [world.assignment(SOLO, (node,))])


def _load_concurrently(sim: Simulation, step: Step) -> None:
    if step.node == step.other:
        _load_pair(sim, step)
        _load_solo(sim, step)
        return
    _load_solo(sim, Step("load-solo", 0, 0, 0))
    _load_solo(sim, Step("load-solo", 1, 1, 0))


def _stack_name(step: Step) -> str:
    spark = f"Spark {step.node + 1}"
    if step.other:
        return f"pair on both Sparks + solo-b on {spark}"
    return f"solo + solo-b on {spark}"


def _stacked(sim: Simulation, step: Step) -> None:
    """One load placing two different recipes on one Spark."""

    world = sim.world
    node = (world.nodes[step.node],)
    first = (
        world.assignment(PAIR, world.nodes)
        if step.other
        else world.assignment(SOLO, node)
    )
    _load(sim, _stack_name(step), [first, world.assignment(SOLO_B, node)])


def _stale_stacked_load(sim: Simulation, step: Step) -> None:
    """The same load, reviewed the way a Controller before #1454 did: every
    assignment on a Spark is offered the same first free port, so the stored
    plan the retained load keeps promises one port twice."""

    inspect = RunSwitchOperationService.inspect_request

    def older_review(self, request, **kwargs):
        return inspect(self, request, **{**kwargs, "planned_ports": frozenset()})

    with patch.object(RunSwitchOperationService, "inspect_request", older_review):
        _stacked(sim, step)


def _cancel(sim: Simulation, step: Step) -> None:
    with sim.world.sessions() as session:
        live = tuple(
            session.scalars(
                select(FleetProfileApplication.id)
                .where(FleetProfileApplication.state.not_in(_ENDED))
                .order_by(
                    FleetProfileApplication.created_at.desc(),
                    # The request key comes from the seed; the id does not.
                    FleetProfileApplication.request_key,
                )
            )
        )
    if not live:
        return
    application_id = live[0]
    profiles = sim.world.controller.profiles
    application = profiles.application(application_id)
    numbers = dict(sim.profiles.values())
    sim.monitor.cancel_requested.add(application_id)
    profiles.cancel(
        application_id,
        profile_number=numbers[application.profile_id],
        request_key=_request_key(sim),
        actor="admin",
    )


def _stop(sim: Simulation, step: Step) -> None:
    _load(sim, "idle", [])


def _wait(sim: Simulation, step: Step) -> None:
    for _ in range(step.amount * 2 - 1):
        sim.round()


def _restart(sim: Simulation, step: Step) -> None:
    sim.world.restart()


def _spark_down(sim: Simulation, step: Step) -> None:
    sim.sparks.spark(step.node).down_steps = step.amount


def _late_answers(sim: Simulation, step: Step) -> None:
    sim.sparks.spark(step.node).late_rounds = step.amount


def _duplicate_answers(sim: Simulation, step: Step) -> None:
    sim.sparks.spark(step.node).duplicate_steps = step.amount


def _failed_answer(sim: Simulation, step: Step) -> None:
    sim.sparks.spark(step.node).fail_next = True


def _clock_jump(sim: Simulation, step: Step) -> None:
    sim.world.advance(_JUMPS[step.amount % len(_JUMPS)])


def _disk_pressure(sim: Simulation, step: Step) -> None:
    sim.sparks.spark(step.node).pressure_steps = step.amount


def _revision_unreadable(sim: Simulation, step: Step) -> None:
    sim.world.make_unreadable((PAIR, SOLO)[step.node])


Perform = Callable[["Simulation", Step], None]
#: Every action and its weight.
ACTIONS: dict[str, tuple[int, Perform]] = {
    "load-pair": (4, _load_pair),
    "load-solo": (4, _load_solo),
    "load-concurrently": (2, _load_concurrently),
    "load-stacked": (3, _stacked),
    "stale-stacked-load": (1, _stale_stacked_load),
    "cancel": (2, _cancel),
    "stop": (2, _stop),
    "restart": (2, _restart),
    "wait": (4, _wait),
    "spark-down": (1, _spark_down),
    "late-answers": (1, _late_answers),
    "duplicate-answers": (1, _duplicate_answers),
    "failed-answer": (1, _failed_answer),
    "clock-jump": (1, _clock_jump),
    "disk-pressure": (1, _disk_pressure),
    "revision-unreadable": (1, _revision_unreadable),
}


def draw(rng: random.Random) -> Step:
    kinds = tuple(ACTIONS)
    (kind,) = rng.choices(kinds, weights=[ACTIONS[kind][0] for kind in kinds])
    return Step(kind, rng.randrange(2), rng.randrange(2), rng.randint(1, 4))


def perform(sim: Simulation, step: Step) -> None:
    ACTIONS[step.kind][1](sim, step)
