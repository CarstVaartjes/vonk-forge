"""A newer whole-fleet selection supersedes an older parked application."""

from datetime import timedelta

from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker
from vonk_control.fleet_profile_contract import FleetProfileChildOperation
from vonk_control.fleet_profiles import FleetProfileService
from vonk_control.models import AgentNode, Base, FleetProfileApplication

from .test_fleet_profiles import (
    NOW,
    _assessment,
    _exact_preparation,
    _input,
    _node_id,
    _seed,
    _SwitchAdapter,
    _uuid,
)


def test_new_whole_fleet_profile_supersedes_a_parked_parent(
    postgres_engine: Engine,
) -> None:
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    _recipe_id, revision_id = _seed(sessions)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=_node_id(2), state="active", last_seen_at=NOW))
    adapter = _SwitchAdapter()
    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        switch_adapter=adapter,
        assessment_provider=lambda _session, _assignment, node_ids, **_kwargs: (
            _assessment(_exact_preparation(node_ids))
        ),
    )
    parked_profile = service.create(_input(revision_id), actor="admin")
    parked = service.load(
        parked_profile.number,
        request_key=_uuid(930),
        actor="admin",
        expected_plan_digest=service.preview(parked_profile.id).plan_digest,
    )
    assert service.tick()
    parked_child_id = service.application(parked.id).current_operation_id
    assert parked_child_id is not None
    adapter._operations[parked_child_id] = [
        FleetProfileChildOperation(
            id=parked_child_id,
            state="waiting-for-operator",
            status_reason="Waiting for exact interrupted-effect reconciliation",
        )
    ]
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, parked.id)
        assert row is not None
        row.state = "waiting-for-operator"
        row.status_reason = "Waiting for exact interrupted-effect reconciliation"

    other_input = _input(revision_id, name="Unrelated node")
    other_input.assignments[0].spark_ids = [_node_id(2)]
    active_profile = service.create(other_input, actor="admin")
    active = service.load(
        active_profile.number,
        request_key=_uuid(931),
        actor="admin",
        expected_plan_digest=service.preview(active_profile.id).plan_digest,
    )
    with sessions.begin() as session:
        parked_row = session.get(FleetProfileApplication, parked.id)
        assert parked_row is not None
        # Keep the newly selected application first in the runnable queue.
        # The parked row remains older selection authority regardless of this
        # observation-order fixture.
        parked_row.created_at = active.created_at + timedelta(microseconds=1)
    assert service.tick()
    active_child_id = service.application(active.id).current_operation_id
    assert active_child_id is not None
    superseded = service.application(parked.id)
    assert superseded.state == "cancelled"
    assert "replaced" in (superseded.status_reason or "")
    assert service.application(active.id).state == "running"
