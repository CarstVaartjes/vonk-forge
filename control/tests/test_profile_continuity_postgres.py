"""Real SQL promises and issued cancellation receipts survive partial adoption."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import AgentResult
from vonk_control.agent_jobs import AgentJobService
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import (
    FleetProfileService,
    RunSwitchFleetProfileAdapter,
    _persisted_profile_progress,
)
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    Base,
    FleetProfileApplication,
    Job,
    ResourceReservation,
)
from vonk_control.platform_ports import HOST_ENDPOINT_PORT
from vonk_control.run_switch_operations import RunSwitchOperationService

from .recipe_stop_fixtures import recipe_stop_payload
from .runtime_identity_support import claim_agent
from .test_fleet_profiles import (
    NOW,
    _assessment,
    _exact_preparation,
    _node_id,
    _seed,
    _SwitchAdapter,
    _uuid,
)
from .test_profile_adapter_continuity import _running_child


class _SQLCancellationAdapter(_SwitchAdapter):
    def request_superseded_workload_cancellation_in_session(
        self, session, targets, ordinal, now
    ):
        AgentJobService.request_superseded_workload_cancellation_in_session(
            session, targets, ordinal, now
        )


def _assessment_with_promises(_session, _assignment, expected_nodes, **_kwargs):
    assessment = _assessment(_exact_preparation(tuple(expected_nodes)))
    assessment.fit_current.nodes[0].ports_required = [HOST_ENDPOINT_PORT]
    assessment.fit_current.nodes[0].disk_required_bytes = 100
    return assessment


def test_postgres_partial_adoption_preserves_promises_and_waits_exact_issued_cleanup(
    postgres_engine, monkeypatch
):
    """Catches duplicate A promises, lost child identity and C dispatch before B ack."""
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    _seed(sessions)
    clock = [NOW]
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=_node_id(2),
                state="active",
                protocol_version=2,
                architecture="linux-arm64",
                last_seen_at=NOW,
            )
        )
        for index in (1, 2):
            session.add(
                AgentCertificate(
                    serial=f"continuity-{index}",
                    node_id=_node_id(index),
                    not_before=NOW - timedelta(seconds=1),
                    not_after=NOW + timedelta(hours=1),
                    fingerprint=f"continuity-{index}",
                )
            )
    boundary = _SQLCancellationAdapter()
    profiles = FleetProfileService(
        sessions,
        clock=lambda: clock[0],
        switch_adapter=boundary,
        assessment_provider=_assessment_with_promises,
    )
    choices = [
        {
            "recipe_selector": "vonk-forge/synthetic-tiny-build",
            "spark_ids": [_node_id(index)],
            "desired_state": "running",
            "assignment_name": f"lane-{index}",
        }
        for index in (1, 2)
    ]
    original_profile = profiles.create(
        FleetProfileInput.model_validate({"name": "A plus B", "assignments": choices}),
        actor="admin",
    )
    original = profiles.apply(
        original_profile.id, request_key=_uuid(18300), actor="admin"
    )
    assert profiles.tick()
    original_child = profiles.application(original.id).current_operation_id
    with sessions() as session:
        original_claims = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == original.id
                )
            )
        )
        kept_claim_ids = {
            claim.id for claim in original_claims if claim.node_id == _node_id(1)
        }
        changed_claim_ids = {
            claim.id for claim in original_claims if claim.node_id == _node_id(2)
        }
        assert kept_claim_ids and changed_claim_ids
    jobs = AgentJobService(sessions, clock=lambda: clock[0])
    job_id = str(uuid4())
    with sessions.begin() as session:
        session.add(
            Job(
                id=job_id,
                request_id=str(uuid4()),
                kind="agent.operations",
                state="queued",
                actor="admin",
                authority_revision="a" * 64,
                targets=[_node_id(2)],
                payload_digest="b" * 64,
                payload={
                    "workload_intent_ordinal": original.progress.workload_intent_ordinal
                },
                created_at=NOW,
                updated_at=NOW,
            )
        )
    issued = jobs.enqueue(
        job_id,
        _node_id(2),
        "recipe.stop",
        "a" * 64,
        recipe_stop_payload(_node_id(2), plan_digest="a" * 64),
    )
    claim = claim_agent(jobs, _node_id(2), "continuity-2")
    assert claim is not None
    replacement_choices = [choices[0], {**choices[1], "assignment_name": "lane-c"}]
    newer_profile = profiles.create(
        FleetProfileInput.model_validate(
            {"name": "A plus C", "assignments": replacement_choices}
        ),
        actor="admin",
    )
    clock[0] += timedelta(seconds=1)
    intermediate = profiles.apply(
        newer_profile.id, request_key=_uuid(18301), actor="admin"
    )
    # A second replacement supersedes the aggregate which borrowed A. The
    # durable link must stay flattened to A's original executor, never transfer
    # its child or promises to either later aggregate.
    final_profile = profiles.create(
        FleetProfileInput.model_validate(
            {
                "name": "A plus D",
                "assignments": [
                    choices[0],
                    {**choices[1], "assignment_name": "lane-d"},
                ],
            }
        ),
        actor="admin",
    )
    final_review = profiles.preview(final_profile.id)
    assert [
        (effect.application_id, effect.node_ids)
        for effect in final_review.effects.adopted
    ] == [(original.id, [_node_id(1)])]
    clock[0] += timedelta(seconds=1)
    newer = profiles.apply(final_profile.id, request_key=_uuid(18304), actor="admin")
    assert profiles.application(intermediate.id).state == "superseded"
    assert profiles.application(original.id).current_operation_id == original_child
    restarted = FleetProfileService(
        sessions,
        clock=lambda: clock[0],
        switch_adapter=boundary,
        assessment_provider=_assessment_with_promises,
    )
    with sessions() as session:
        old = session.get(FleetProfileApplication, original.id)
        assert old is not None
        assert restarted._adopted_application_scope(session, old) == (_node_id(1),)
        assert {
            claim.id
            for claim in session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == original.id,
                    ResourceReservation.node_id == _node_id(1),
                    ResourceReservation.state.in_(("active", "promised")),
                )
            )
        } == kept_claim_ids
        for identity in changed_claim_ids:
            changed_claim = session.get(ResourceReservation, identity)
            assert changed_claim is not None
            assert changed_claim.state == "released"
        port_claims = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.kind == "port",
                    ResourceReservation.state == "promised",
                )
            )
        )
        assert {(value.node_id, value.owner_id) for value in port_claims} == {
            (_node_id(1), original.id),
            (_node_id(2), newer.id),
        }
        newer_ordinal = newer.progress.workload_intent_ordinal
        assert newer_ordinal is not None
        pending = AgentJobService.assess_superseded_agent_effects_in_session(
            session, (_node_id(2),), newer_ordinal, clock[0]
        )
        assert [effect.operation_id for effect in pending] == [issued.id]
    # Resume the replacement adapter's persisted queue with the real issued
    # effect in SQL. Only physical child dispatch is intercepted here; cleanup
    # observation, fencing, attempts and late receipts are the production path.
    adapter = RunSwitchFleetProfileAdapter(
        sessions, RunSwitchOperationService(sessions, clock=lambda: clock[0])
    )
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, newer.id)
        assert row is not None
        intended = _persisted_profile_progress(row).intended_profile
        assert intended is not None
        assignment = next(
            value
            for value in intended.assignments
            if value.nodes[0].node_id == _node_id(2)
        )
        adapter._write_state(
            session,
            row,
            {
                "child_id": newer.id,
                "scope_node_ids": [_node_id(2)],
                "assignment_ids": [assignment.id],
                "assignments": [assignment.model_dump(mode="json")],
                "queue": [{"kind": "run", "id": assignment.id}],
                "actor": "admin",
                "request_id": _uuid(18302),
                "position": 0,
                "state": "queued",
            },
        )
    dispatched = []

    def dispatch(*args):
        dispatched.append(args[1]["id"])
        return _running_child(_uuid(18303), _node_id(2))

    monkeypatch.setattr(adapter, "_start_child", dispatch)
    adapter.advance(newer.id)
    assert not dispatched
    with sessions() as session:
        row = session.get(FleetProfileApplication, newer.id)
        assert row is not None
        stored = _persisted_profile_progress(row).switch_adapter
        assert stored is not None
        assert stored.pending_operation_ids == [issued.id]
    # A late exact cancellation receipt closes the original attempt. Advancing
    # the normal persisted backoff then releases C without another profile load.
    clock[0] += timedelta(seconds=700)
    assert jobs.record_late_result(
        AgentResult.model_validate(
            {
                "fence": claim.fence,
                "state": "cancelled",
                "result": {
                    "error_code": "operation_cancelled",
                    "reason": "exact stop completed",
                },
            }
        )
    )
    adapter.advance(newer.id)
    assert dispatched == [assignment.id]
    assert restarted.application(original.id).current_operation_id == original_child


def test_postgres_new_profile_never_adopts_half_of_a_dual_spark_effect(postgres_engine):
    """Catches preserving one rank of a replaced gang as independent work."""
    from .test_fleet_profiles import _seed_dual_solo_without_runtime_state

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    _seed_dual_solo_without_runtime_state(sessions)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=_node_id(3),
                state="active",
                protocol_version=2,
                architecture="linux-arm64",
                last_seen_at=NOW,
            )
        )
    boundary = _SQLCancellationAdapter()
    profiles = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        switch_adapter=boundary,
        assessment_provider=_assessment_with_promises,
    )
    solo = {
        "recipe_selector": "vonk-forge/synthetic-tiny-solo",
        "spark_ids": [_node_id(3)],
        "desired_state": "running",
        "assignment_name": "independent",
    }
    dual = {
        "recipe_selector": "vonk-forge/synthetic-tiny-build",
        "spark_ids": [_node_id(1), _node_id(2)],
        "desired_state": "running",
        "assignment_name": "gang",
    }
    original_profile = profiles.create(
        FleetProfileInput.model_validate(
            {"name": "Gang and solo", "assignments": [dual, solo]}
        ),
        actor="admin",
    )
    original = profiles.apply(
        original_profile.id, request_key=_uuid(18400), actor="admin"
    )
    newer_profile = profiles.create(
        FleetProfileInput.model_validate(
            {"name": "Solo continues", "assignments": [solo]}
        ),
        actor="admin",
    )
    preview = profiles.preview(newer_profile.id)
    assert [
        (effect.application_id, effect.node_ids) for effect in preview.effects.adopted
    ] == [(original.id, [_node_id(3)])]
    assert not any(
        node_id in effect.node_ids
        for effect in preview.effects.adopted
        for node_id in (_node_id(1), _node_id(2))
    )
