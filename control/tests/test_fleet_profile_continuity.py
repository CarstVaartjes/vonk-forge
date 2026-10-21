"""An independent unchanged executor survives a newer whole-fleet decision."""

from datetime import timedelta

import pytest
from sqlalchemy import select
from vonk_agent_protocol import InvalidRequestError, InvalidRequestReason
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import FleetProfileService
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
    with pytest.raises(InvalidRequestError) as refused:
        service.cancel(
            first.id,
            profile_number=service.get_number(1).number,
            request_key=_uuid(18005),
            actor="admin",
        )
    assert refused.value.typed_reason == InvalidRequestReason.CONFLICT
    assert (
        service.application(first.id).current_operation_id
        == _original.current_operation_id
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


def test_a_stale_replay_is_superseded_by_the_load_that_continues_it():
    """Production 2026-10-16 (fc49c950 continued by 5acd2d42): a load fences
    only its own Sparks, at one more than their highest ordinal, so the newer
    load that continued the old load's lane fenced the other Spark at the very
    ordinal the old load holds. The old load's child failed; its replay would
    stop the newer workload, so its review was stale, and comparing ordinals
    never saw the newer load: it backed off for hours while the newer load
    waited for it. The newest request steers: the old load is superseded."""
    from vonk_control.fleet_profiles import FleetProfileReviewStale

    sessions, service, _adapter, _first, _original, both_profile = _world()
    old = service.apply(both_profile.id, request_key=_uuid(18002), actor="admin")
    assert service.tick()
    replaced = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Lane two plus a new lane",
                "assignments": [
                    {
                        "recipe_selector": "vonk-forge/synthetic-tiny-build",
                        "spark_ids": [_node_id(2)],
                        "desired_state": "running",
                        "assignment_name": "lane-two",
                    },
                    {
                        "recipe_selector": "vonk-forge/synthetic-tiny-build",
                        "spark_ids": [_node_id(1)],
                        "desired_state": "running",
                        "assignment_name": "lane-c",
                    },
                ],
            }
        ),
        actor="admin",
    )
    assert [
        effect.application_id for effect in service.preview(replaced.id).effects.adopted
    ] == [old.id]
    service.apply(replaced.id, request_key=_uuid(18007), actor="admin")
    accepted = service.application(old.id).progress
    assert accepted.intended_profile is not None
    assert _node_id(1) in accepted.intended_profile.scope.node_ids
    with sessions() as session:
        newer_spark = session.get(AgentNode, _node_id(1))
        assert newer_spark is not None
        assert newer_spark.workload_intent_ordinal == accepted.workload_intent_ordinal
    with sessions.begin() as session:
        # Its child failed retryably; the real executor backs it off for a replay.
        row = session.get(FleetProfileApplication, old.id)
        assert row is not None
        row.state = "backoff"
        row.status_reason = "run-switch.final-verification-timeout"

    ended = service._decline_stale_retry(
        old.id,
        FleetProfileReviewStale(
            "Recovery would stop an unreviewed workload; review and load the "
            "current profile again"
        ),
    )

    assert ended.state == "superseded"


def test_an_older_load_holding_a_higher_ordinal_elsewhere_is_superseded():
    """#1608's root cause at admission: a load fences only its own Sparks, at
    one more than their highest ordinal, so an older load can hold a higher
    ordinal (fenced on other Sparks) than a newer load that takes a Spark the
    older one works on. Comparing the two kept the older load alive beside
    the newer one; the newest request steers, by acceptance order."""
    sessions, service, _adapter, first, _original, _both = _world()
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, first.id)
        assert row is not None
        # As if fenced on other Sparks whose ordinals ran higher.
        row.progress = {**row.progress, "workload_intent_ordinal": 50}
    newer_profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Newer",
                "assignments": [
                    {
                        "recipe_selector": "vonk-forge/synthetic-tiny-build",
                        "spark_ids": [_node_id(1)],
                        "desired_state": "running",
                        "assignment_name": "lane-c",
                    }
                ],
            }
        ),
        actor="admin",
    )
    newer = service.apply(newer_profile.id, request_key=_uuid(18021), actor="admin")
    took = service.application(newer.id).progress.workload_intent_ordinal
    assert took is not None and took < 50

    older = service.application(first.id)
    assert older.state == "superseded" and older.superseded_by == newer.id


def test_running_application_keeps_its_stored_recipe_after_catalog_head_changes():
    """A projection refresh must not replace the exact recipe already accepted."""
    from vonk_control.catalog_entities import CatalogEntityService
    from vonk_control.fleet_profiles.persistence import _stored_recipe
    from vonk_control.models import CatalogDocumentRevision
    from vonk_forge_contracts import RecipeDefinition

    sessions, service, _adapter, first, original, _ = _world()
    assert original.progress.intended_profile is not None
    pinned = original.progress.intended_profile.assignments[0].recipe_revision_id
    with sessions() as session:
        old = session.get(CatalogDocumentRevision, pinned)
        assert old is not None
        old_definition = _stored_recipe(old)
        assert isinstance(old_definition, RecipeDefinition)
        changed = old_definition.model_dump(mode="json")
    changed["metadata"]["description"] += " current head"
    catalog = CatalogEntityService(sessions, clock=lambda: NOW)
    successor = catalog.revise(old.document_id, changed, actor="admin")
    catalog.resolve(successor.id, actor="admin")
    assert successor.id != pinned
    with sessions() as session:
        stored = session.get(CatalogDocumentRevision, pinned)
        assert stored is not None
        assert _stored_recipe(stored) == old_definition
    from .test_fleet_profiles import _follow_profile_retry

    now = [NOW + timedelta(seconds=1)]
    service._clock = lambda: now[0]
    for _ in range(8):
        _follow_profile_retry(service, first.id, now)
        service.tick()
        if service.application(first.id).state == "succeeded":
            break
    finished = service.application(first.id)
    assert finished.state == "succeeded"
    assert finished.progress.intended_profile is not None
    assert (
        finished.progress.intended_profile.assignments[0].recipe_revision_id == pinned
    )
