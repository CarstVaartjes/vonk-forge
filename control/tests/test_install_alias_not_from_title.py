from __future__ import annotations

from uuid import uuid4

from vonk_agent_protocol import DesiredAssignmentState
from vonk_control.fleet_profile_contract import FleetProfileAssignment, FleetProfileNode
from vonk_control.fleet_profiles import RunSwitchFleetProfileAdapter


def _assignment(
    title: str, state: DesiredAssignmentState, alias: str | None, ident: str
) -> FleetProfileAssignment:
    return FleetProfileAssignment(
        id=ident,
        recipe_revision_id=str(uuid4()),
        topology_name="single",
        desired_state=state,
        alias=alias,
        nodes=[
            FleetProfileNode(
                node_id="spk_" + uuid4().hex, rank=0, role="main", endpoint_owner=True
            )
        ],
        recipe_id=str(uuid4()),
        recipe_title=title,
    )


def test_install_plan_alias_does_not_follow_the_recipe_title() -> None:
    ident = str(uuid4())
    before = _assignment("Old Title", DesiredAssignmentState.INSTALLED, None, ident)
    after = _assignment("Renamed Recipe", DesiredAssignmentState.INSTALLED, None, ident)
    intent = RunSwitchFleetProfileAdapter._assignment_intent
    assert intent(before) == intent(after) == ("install", ident)


def test_running_alias_is_the_stored_alias_whatever_the_title() -> None:
    for title in ("Old Title", "Renamed Recipe"):
        running = _assignment(
            title, DesiredAssignmentState.RUNNING, "studio-chat", str(uuid4())
        )
        assert RunSwitchFleetProfileAdapter._assignment_intent(running) == (
            "switch",
            "studio-chat",
        )
