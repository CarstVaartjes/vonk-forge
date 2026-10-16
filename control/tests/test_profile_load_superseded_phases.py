"""A load's install or start superseded before acceptance ends superseded, never failed.

A newer workload intent can reach a load's Spark while the load's Run/Switch
installs or starts: while it retires older unissued work, or as its lifecycle
call accepts the child under the intent it bound. Nothing was issued under the
older intent: the phase goes back to its Run/Switch, which reads the newer
intent and ends ``cancelled`` as superseded.

Wrong implementation: only the Stop phase re-planned a superseded refusal; the
install and start phases let it reach the generic handler, so the Run/Switch
ended failed with "workload intent was superseded".
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_control.agent_jobs import AgentJobService
from vonk_control.models import AgentNode, Job

from .lifecycle_simulation.invariants import ENDED
from .lifecycle_simulation.simulation import ROUND_SECONDS, Simulation
from .lifecycle_simulation.world import SOLO, build_world


@pytest.mark.slow(30)  # a whole load on PostgreSQL per moment
@pytest.mark.parametrize(
    ("moment", "kind"),
    [
        ("retire-unissued", "recipe.install"),
        ("accept", "start_installation"),
        ("retire-unissued", "recipe.uninstall"),  # the start phase's retirement
    ],
)
def test_a_load_superseded_while_installing_or_starting_is_not_failed(
    tmp_path: Path,
    postgres_engine,
    monkeypatch: pytest.MonkeyPatch,
    moment: str,
    kind: str,
) -> None:
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        sim.round()
        node_ids = tuple(world.nodes[:1])
        superseded: list[int] = []

        def newer_intent() -> None:
            # A newer workload intent reaches the load's Spark, as a newer
            # load's admission does, after the load bound its own intent.
            if superseded:
                return
            with world.sessions.begin() as session:
                nodes = tuple(
                    session.scalars(
                        select(AgentNode).where(AgentNode.node_id.in_(node_ids))
                    )
                )
                ordinal = max(node.workload_intent_ordinal for node in nodes) + 1
                for node in nodes:
                    node.workload_intent_ordinal = ordinal
                AgentJobService.request_superseded_workload_cancellation_in_session(
                    session, node_ids, ordinal, world.clock()
                )
            superseded.append(ordinal)

        lifecycle = world.controller.lifecycle
        if moment == "retire-unissued":
            retire = lifecycle.reconcile_superseded_unissued

            def retire_after_newer_intent(retired_kind, owner_id, ordinal, **kwargs):
                if retired_kind == kind:
                    newer_intent()
                return retire(retired_kind, owner_id, ordinal, **kwargs)

            monkeypatch.setattr(
                lifecycle, "reconcile_superseded_unissued", retire_after_newer_intent
            )
        else:
            accept = getattr(lifecycle, kind)

            def accept_after_newer_intent(*args, **kwargs):
                newer_intent()
                return accept(*args, **kwargs)

            monkeypatch.setattr(lifecycle, kind, accept_after_newer_intent)

        profile, _number = world.create_profile(
            "Superseded load", [world.assignment(SOLO, node_ids)]
        )
        load = world.controller.profiles.apply(
            profile, request_key=str(uuid.uuid4()), actor="admin"
        ).id
        profiles = world.controller.profiles
        # Rounds without the monitor: no newer load exists to own the injected
        # intent, which the monitor would report as a load superseded by nothing.
        for _ in range(40):
            if profiles.application(load).state in ENDED:
                break
            world.advance(ROUND_SECONDS)
            world.tick()
            sim.sparks.round()

        assert superseded, "the newer intent never reached the load"
        assert profiles.application(load).state == "superseded"
        with world.sessions() as session:
            (switch,) = session.scalars(
                select(Job).where(Job.kind == "recipe.run-switch.v2")
            )
            assert switch.state == "cancelled", switch.status_reason
    finally:
        world.close()
