"""An independent unchanged executor survives a newer whole-fleet decision."""

from datetime import timedelta

import pytest
from sqlalchemy import select
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import FleetProfileInvalid, FleetProfileService
from vonk_control.models import (
    AgentNode,
    FleetProfileApplication,
    FleetProfileSelection,
)

from .test_fleet_profiles import NOW, _database, _node_id, _seed, _SwitchAdapter, _uuid


def _world():
    sessions = _database()
    _seed(sessions)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=_node_id(2),
                state="active",
                protocol_version=1,
                architecture="linux-arm64",
                last_seen_at=NOW,
            )
        )
    adapter = _SwitchAdapter()
    clock = [NOW]
    service = FleetProfileService(
        sessions, clock=lambda: clock[0], switch_adapter=adapter
    )
    first_choice = {
        "recipe_selector": "vonk-forge/synthetic-tiny-build",
        "spark_ids": [_node_id(1)],
        "desired_state": "running",
        "assignment_name": "lane-one",
    }
    first_profile = service.create(
        FleetProfileInput.model_validate(
            {"name": "First lane", "assignments": [first_choice]}
        ),
        actor="admin",
    )
    first = service.apply(first_profile.id, request_key=_uuid(18001), actor="admin")
    assert service.tick()
    original = service.application(first.id)
    clock[0] += timedelta(seconds=1)
    second_choice = {
        **first_choice,
        "spark_ids": [_node_id(2)],
        "assignment_name": "lane-two",
    }
    second_profile = service.create(
        FleetProfileInput.model_validate(
            {"name": "Both lanes", "assignments": [first_choice, second_choice]}
        ),
        actor="admin",
    )
    return sessions, service, adapter, first, original, second_profile


def test_new_lane_adopts_original_identity_without_fencing_its_spark():
    """Catches whole-roster fences cancelling the lane still downloading."""
    sessions, service, adapter, first, original, second_profile = _world()
    preview = service.preview(second_profile.id)
    assert len(preview.effects.adopted) == 1
    adopted = preview.effects.adopted[0]
    assert adopted.application_id == first.id
    assert adopted.node_ids == [_node_id(1)]
    assert [step.node_ids for step in preview.steps] == [[_node_id(2)]]
    second = service.apply(second_profile.id, request_key=_uuid(18002), actor="admin")
    retained = service.application(first.id)
    assert retained.state == "running"
    assert retained.current_operation_id == original.current_operation_id
    assert retained.progress == original.progress
    assert adapter.cancellations[-1][0] == (_node_id(2),)
    with sessions() as session:
        selection = session.get(FleetProfileSelection, 1)
        assert selection is not None and selection.application_id == second.id
        old = session.get(FleetProfileApplication, first.id)
        assert old is not None
        assert service._application_is_current_selection(session, old)
        assert service._adopted_application_scope(session, old) == (_node_id(1),)
        assert not service._superseding_intent(session, old, retained.progress)
    # A third whole-fleet load replaces B while A still copies. The adoption
    # link is flattened to original A; superseding aggregate B must not cancel A.
    third_profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "A plus C",
                "assignments": [
                    {
                        "recipe_selector": "vonk-forge/synthetic-tiny-build",
                        "spark_ids": [_node_id(1)],
                        "desired_state": "running",
                        "assignment_name": "lane-one",
                    },
                    {
                        "recipe_selector": "vonk-forge/synthetic-tiny-build",
                        "spark_ids": [_node_id(2)],
                        "desired_state": "running",
                        "assignment_name": "lane-c",
                    },
                ],
            }
        ),
        actor="admin",
    )
    assert [
        effect.application_id
        for effect in service.preview(third_profile.id).effects.adopted
    ] == [first.id]
    service.apply(third_profile.id, request_key=_uuid(18006), actor="admin")
    assert service.application(second.id).state == "superseded"
    assert (
        service.application(first.id).current_operation_id
        == original.current_operation_id
    )
    assert adapter.cancellations[-1][0] == (_node_id(2),)


def test_selected_cancel_fences_both_original_and_new_node_ordinals():
    """Catches cancel leaving borrowed agent work authorized under an old fence."""
    sessions, service, adapter, first, _original, second_profile = _world()
    second = service.apply(second_profile.id, request_key=_uuid(18002), actor="admin")
    # The executor's old receipt does not revoke a newer accepted policy. Its
    # original child identity continues, but cancellation belongs to selection.
    with pytest.raises(FleetProfileInvalid, match=second.id):
        service.cancel(
            first.id,
            profile_number=service.get_number(1).number,
            request_key=_uuid(18005),
            actor="admin",
        )
    assert service.application(first.id).cancellation is None
    service.cancel(
        second.id,
        profile_number=second_profile.number,
        request_key=_uuid(18003),
        actor="admin",
    )
    assert adapter.cancellations[-1][0] == (_node_id(1), _node_id(2))
    with sessions() as session:
        nodes = tuple(session.scalars(select(AgentNode).order_by(AgentNode.node_id)))
        assert len({node.workload_intent_ordinal for node in nodes}) == 1
        old = session.get(FleetProfileApplication, first.id)
        assert old is not None
        assert service._adopted_application_scope(session, old) is None
        assert not service._application_is_current_selection(session, old)


def test_replaced_alias_is_not_adopted_as_equivalent_work():
    """Catches borrowing a placement solely by Spark or recipe identity."""
    _sessions, service, _adapter, first, _original, _second_profile = _world()
    choice = {
        "recipe_selector": "vonk-forge/synthetic-tiny-build",
        "spark_ids": [_node_id(1)],
        "desired_state": "running",
        "assignment_name": "replacement",
    }
    changed = service.create(
        FleetProfileInput.model_validate(
            {"name": "Replace lane", "assignments": [choice]}
        ),
        actor="admin",
    )
    assert not service.preview(changed.id).effects.adopted
    service.apply(changed.id, request_key=_uuid(18004), actor="admin")
    assert service.application(first.id).state == "superseded"
