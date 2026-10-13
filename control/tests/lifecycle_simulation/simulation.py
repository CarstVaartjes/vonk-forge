"""Run a seeded sequence of actions and check the invariants after every round.

A step is one action followed by a round of the Controller worker loop and the
Sparks' polls on the fake clock (waiting is an action of its own). After the
last step every fault is cleared and the world is left to quiesce; then a fresh
load of the whole fleet must be admitted and reach serving. The first violation ends the run with the seed and
the steps that led to it, which is the replayable trace.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from typing import NoReturn

from sqlalchemy import select
from vonk_control.models import RecipeRun, RunNode

from .actions import draw, perform
from .invariants import ENDED, Monitor, Violation, typed_refusal
from .sparks import Sparks
from .world import PAIR, World

ROUND_SECONDS = 30
#: Quiescing rounds are longer, still within a Spark's report cadence (route
#: evidence ages out at 120s); the stall window ends them if nothing settles.
QUIESCE_ROUND_SECONDS = 60
QUIESCE_ROUNDS = 150
FRESH_LOAD_ROUNDS = 60


class SimulationFailure(AssertionError):
    def __init__(self, seed: int, trace: list[str], violation: Violation) -> None:
        self.seed = seed
        self.trace = list(trace)
        self.violation = violation
        steps = "\n".join(
            f"  {index:>2}. {line}" for index, line in enumerate(trace, start=1)
        )
        super().__init__(
            f"seed {seed} violated '{violation.invariant}' after step"
            f" {len(trace)}:\n  {violation.detail}\nsteps:\n{steps}"
        )


@dataclass
class Simulation:
    world: World
    seed: int
    rng: random.Random = field(init=False)
    sparks: Sparks = field(init=False)
    monitor: Monitor = field(init=False)
    #: Saved profiles by name: their id and number.
    profiles: dict[str, tuple[str, int]] = field(default_factory=dict)
    trace: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)
        self.sparks = Sparks(self.world)
        self.monitor = Monitor(self.world, self.sparks)

    def run(self, steps: int) -> None:
        for _ in range(steps):
            self.step()
        self.quiesce()
        self.fresh_load()

    def step(self) -> None:
        step = draw(self.rng)
        self.trace.append(step.describe(self.world.nodes))
        try:
            perform(self, step)
        except Exception as error:  # noqa: BLE001 - a refusal is an answer; an internal error is a violation
            if not typed_refusal(error):
                self._fail(
                    Violation(
                        "operator requests are answered, not crashed",
                        f"{type(error).__name__}: {error}",
                    )
                )
            self.trace[-1] += f" (refused: {type(error).__name__})"
        self.round()
        self.sparks.end_step()

    def round(self, seconds: int = ROUND_SECONDS) -> None:
        self.world.advance(seconds)
        self.world.tick()
        self.sparks.round()
        violations = self.monitor.observe()
        if violations:
            self._fail(violations[0])

    def settle(self, application_id: str, rounds: int = FRESH_LOAD_ROUNDS) -> str:
        """Run rounds until the load ends; its last state."""

        profiles = self.world.controller.profiles
        for _ in range(rounds):
            if profiles.application(application_id).state in ENDED:
                break
            self.round()
        return profiles.application(application_id).state

    def quiesce(self) -> None:
        self.trace.append("faults clear; the world quiesces")
        self.sparks.clear_faults()
        for _ in range(QUIESCE_ROUNDS):
            self.round(QUIESCE_ROUND_SECONDS)
            if not self.monitor.unfinished() and self.monitor.stable():
                return
        unfinished = self.monitor.unfinished()
        if unfinished:
            self._fail(
                Violation(
                    "every request ends or progresses",
                    f"unfinished after quiescing: {sorted(unfinished.values())}",
                )
            )

    def fresh_load(self) -> None:
        """The whole fleet is admitted for a fresh load and serves it."""

        world = self.world
        self.trace.append("a fresh load of pair on both Sparks")
        profile_id, _number = self.profiles.get("pair") or world.create_profile(
            "fresh pair", [world.assignment(PAIR, world.nodes)]
        )
        try:
            application = world.controller.profiles.apply(
                profile_id,
                request_key=str(uuid.UUID(int=self.seed, version=4)),
                actor="admin",
            )
        except Exception as error:  # noqa: BLE001 - any refusal of a fresh load is the violation
            self._fail(
                Violation(
                    "a fresh load is admitted after quiescing",
                    f"{type(error).__name__}: {error}",
                )
            )
        self.settle(application.id)
        view = world.controller.profiles.application(application.id)
        if view.state != "succeeded" or not self.serving_everywhere():
            self._fail(
                Violation(
                    "a fresh load is admitted after quiescing and serves",
                    f"{view.state}: {view.status_reason}",
                )
            )

    def serving_everywhere(self) -> bool:
        """A running pair run whose ranks every Spark reports serving."""

        with self.world.sessions() as session:
            runs = tuple(
                session.scalars(
                    select(RecipeRun.id).where(RecipeRun.state == "running")
                )
            )
            for run_id in runs:
                nodes = set(
                    session.scalars(
                        select(RunNode.node_id).where(RunNode.run_id == run_id)
                    )
                )
                if nodes == set(self.world.nodes) and all(
                    run_id in self.sparks.by_node[node].serving for node in nodes
                ):
                    return True
        return False

    def _fail(self, violation: Violation) -> NoReturn:
        raise SimulationFailure(self.seed, self.trace, violation)
