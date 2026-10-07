"""A canonical durable Stop keeps its original authority across profile loads.

PostgreSQL execution belongs to hosted CI. Only the unrelated healthy lane's
physical execution is simulated; the Stop producer, queue, claims and receipts
use the production services and their stored contracts.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from sqlalchemy import select
from vonk_agent_protocol import AgentResult, LifecycleState, canonical_message
from vonk_control.agent_jobs import AgentJobService
from vonk_control.fleet_profile_contract import (
    FleetProfileInput,
    profile_switch_child_request_key,
)
from vonk_control.fleet_profiles import (
    FleetProfileService,
    RunSwitchFleetProfileAdapter,
    _persisted_profile_plan,
    _persisted_profile_progress,
)
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    CatalogDocumentRevision,
    FleetProfileApplication,
    Job,
    ResourceReservation,
)
from vonk_control.recipe_operations import RecipeOperationService
from vonk_control.stored_json import write_guard_mode

from .agent_fences import fenced_attempt, fenced_operation
from .test_fleet_profiles import NOW, _node_id, _uuid
from .test_profile_adapter_continuity import _running_child
from .test_profile_adapter_parallel_postgres import _stored
from .test_profile_continuity_postgres import (
    _assessment_with_promises,
    _SQLCancellationAdapter,
)
from .test_recipe_operations import (
    _issue_exact_stop_grant,
    installed_recipe,
    setup_services,
    started_recipe,
)
from .test_run_switch_operations import RecordingArtifactExecutor, _service


def _claims(sessions, node_id):
    with sessions() as session:
        return {
            (claim.id, claim.owner_id, claim.node_id, claim.kind, claim.state)
            for claim in session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.node_id == node_id
                )
            )
        }


@pytest.fixture
def pending_stop(postgres_engine, tmp_path, monkeypatch):
    sessions, fixture_lifecycle, _queue, mapping, build, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    installation = installed_recipe(
        fixture_lifecycle, mapping, build, nodes, request_id=_uuid(18700)
    )
    run = started_recipe(
        sessions,
        fixture_lifecycle,
        installation.owner_id,
        nodes,
        request_id=_uuid(18701),
    )
    clock = [NOW]
    jobs = AgentJobService(sessions, clock=lambda: clock[0])
    lifecycle = RecipeOperationService(
        sessions,
        install_admission=fixture_lifecycle._install_admission,
        run_admission=fixture_lifecycle._run_admission,
        agent_jobs=jobs,
        clock=lambda: clock[0],
    )
    jobs.set_result_consumer(lifecycle.consume_agent_result)
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
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe"
            )
        )
        assert revision is not None
        selector = f"{revision.publisher}/{revision.slug}"
    profiles = FleetProfileService(
        sessions,
        clock=lambda: clock[0],
        switch_adapter=_SQLCancellationAdapter(),
        assessment_provider=_assessment_with_promises,
    )

    def profile(name):
        return profiles.create(
            FleetProfileInput.model_validate(
                {
                    "name": name,
                    "assignments": [
                        {
                            "recipe_selector": selector,
                            "spark_ids": [_node_id(2)],
                            "desired_state": "running",
                            "assignment_name": name,
                        }
                    ],
                }
            ),
            actor="admin",
        )

    original_profile = profile("original-healthy-lane")
    original = profiles.apply(
        original_profile.id, request_key=_uuid(18702), actor="admin"
    )
    coordinator = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
    adapter = RunSwitchFleetProfileAdapter(sessions, coordinator)
    native_start = adapter._start_child
    healthy_child = _running_child(_uuid(18703), _node_id(2))
    healthy_dispatches = []

    def dispatch(
        application_id, item, assignments, scope, actor, request, index, ordinal
    ):
        if item.kind == "stop":
            return native_start(
                application_id, item, assignments, scope, actor, request, index, ordinal
            )
        assert item.kind == "run" and tuple(scope) == (_node_id(2),)
        healthy_dispatches.append(item.id)
        return healthy_child

    monkeypatch.setattr(adapter, "_start_child", dispatch)
    native_observe = adapter._observed_child
    monkeypatch.setattr(
        adapter,
        "_observed_child",
        lambda identity: (
            healthy_child
            if identity == healthy_child.operation_id
            else native_observe(identity)
        ),
    )
    with sessions() as session:
        row = session.get(FleetProfileApplication, original.id)
        assert row is not None
        intended = _persisted_profile_progress(row).intended_profile
        assert intended is not None
        assignments = tuple(intended.assignments)
    adapter.start(
        application_id=original.id,
        assignments=assignments,
        scope_node_ids=(nodes[0], _node_id(2)),
        actor="admin",
        request_id=_uuid(18704),
    )
    for _ in range(8):
        coordinator.tick()
        adapter.advance(original.id)
    stored = _stored(sessions, original.id)
    stop_index = next(i for i, item in enumerate(stored.queue) if item.kind == "stop")
    assert stored.queue[stop_index].id == run.owner_id
    request_key = profile_switch_child_request_key(
        original.id, stop_index, "stop", run.owner_id
    )
    with sessions() as session:
        stop = session.scalar(select(Job).where(Job.request_id == request_key))
        assert stop is not None and stop.kind == "recipe.stop.v2"
        stop_id = stop.id
        native = list(
            session.scalars(
                select(AgentOperation).where(AgentOperation.kind == "recipe.stop")
            )
        )
        assert len(native) == 1
        assert native[0].payload["run_id"] == run.owner_id
        native_id = native[0].id
        original_row = session.get(FleetProfileApplication, original.id)
        assert original_row is not None
        original_plan_digest = original_row.plan_digest
        original_ordinal = _persisted_profile_progress(
            original_row
        ).workload_intent_ordinal
    assert len(healthy_dispatches) == 1
    claim, stop_plan, _grant = _issue_exact_stop_grant(
        sessions,
        node_id=nodes[0],
        certificate_serial="serial-0",
    )
    assert stop_plan.run_id == run.owner_id
    assert stop_plan.target_runtime_id == run.owner_id

    assert claim is not None and claim.operation.value == "recipe.stop"
    assert fenced_operation(sessions, claim).id == native_id
    with sessions.begin() as session:
        node = session.get(AgentNode, nodes[0])
        assert node is not None
        node.last_seen_at = NOW - timedelta(hours=1)
    return (
        sessions,
        profiles,
        profile,
        clock,
        original,
        lifecycle,
        jobs,
        nodes,
        stop_id,
        request_key,
        stop_index,
        native_id,
        claim,
        original_plan_digest,
        original_ordinal,
        healthy_child,
    )


def test_postgres_original_stop_is_adopted_across_replacements_and_fresh_receipt(
    pending_stop,
    monkeypatch,
):
    (
        sessions,
        profiles,
        profile,
        clock,
        original,
        lifecycle,
        _jobs,
        nodes,
        stop_id,
        request_key,
        stop_index,
        native_id,
        old_claim,
        plan_digest,
        ordinal,
        healthy_child,
    ) = pending_stop
    before = _stored(sessions, original.id)
    original_claims = _claims(sessions, nodes[0])
    assert original_claims
    adopted_stop = None
    for index in range(2):
        replacement = profile(f"replacement-healthy-lane-{index}")
        review = profiles.preview(replacement.id)
        links = [
            item
            for item in review.effects.adopted
            if item.application_id == original.id
        ]
        assert len(links) == 1
        link = links[0]
        assert link.assignment_ids == []
        assert link.plan_digest == plan_digest
        assert link.workload_intent_ordinal == ordinal
        assert link.node_ids == [nodes[0]]
        assert len(link.stops) == 1
        stop = link.stops[0]
        assert stop.queue_index == stop_index
        assert stop.operation_id == stop_id and stop.request_key == request_key
        assert stop.effect.run_id == before.queue[stop_index].id
        assert stop.effect.node_ids == [nodes[0]]
        assert stop.effect.profile_stop_scope is None
        if adopted_stop is not None:
            assert stop == adopted_stop
        adopted_stop = stop
        selected = profiles.apply(
            replacement.id, request_key=_uuid(18710 + index), actor="admin"
        )
        with sessions() as session:
            row = session.get(FleetProfileApplication, original.id)
            selected_row = session.get(FleetProfileApplication, selected.id)
            assert row is not None and selected_row is not None
            assert profiles._adopted_application_scope(session, row) == (nodes[0],)
            bound = _persisted_profile_plan(selected_row)
            assert bound.effects.adopted == review.effects.adopted
            assert session.get(AgentNode, nodes[0]).workload_intent_ordinal == ordinal
        restarted_core = _service(
            sessions, clock[0], lifecycle, RecordingArtifactExecutor()
        )
        restarted = RunSwitchFleetProfileAdapter(sessions, restarted_core)
        native_observe = restarted._observed_child
        monkeypatch.setattr(
            restarted,
            "_observed_child",
            lambda identity, observe=native_observe: (
                healthy_child
                if identity == healthy_child.operation_id
                else observe(identity)
            ),
        )
        monkeypatch.setattr(
            restarted,
            "_start_child",
            lambda *_args: pytest.fail(
                "adopted cleanup must retain its original child"
            ),
        )
        restarted.advance(original.id)
        stored = _stored(sessions, original.id)
        assert stored.queue == before.queue and stored.request_id == before.request_id
        assert any(
            item.queue_index == stop_index and item.operation_id == stop_id
            for item in stored.pending_children
        )
        with sessions() as session:
            assert (
                session.scalar(select(Job.id).where(Job.request_id == request_key))
                == stop_id
            )
            assert {
                item.id
                for item in session.scalars(
                    select(AgentOperation).where(AgentOperation.kind == "recipe.stop")
                )
            } == {native_id}
        assert original_claims == _claims(sessions, nodes[0])

    # Expire the transport lease through the real reconciler. An old successful
    # receipt is diagnostic, and cannot finish the newer claim or drop capacity.
    clock[0] += timedelta(hours=1)
    restarted_jobs = AgentJobService(sessions, clock=lambda: clock[0])
    restarted_jobs.set_result_consumer(lifecycle.consume_agent_result)
    restarted_jobs.reconcile_orders()
    with sessions() as session:
        native = session.get(AgentOperation, native_id)
        assert native is not None
        if native.next_action_at is not None:
            clock[0] = max(clock[0], native.next_action_at)
    assert adopted_stop is not None
    fresh, fresh_stop, _fresh_grant = _issue_exact_stop_grant(
        sessions,
        node_id=nodes[0],
        certificate_serial="serial-0",
        grant_now=clock[0],
    )
    assert fresh_stop.run_id == adopted_stop.effect.run_id
    assert fresh is not None and fresh.fence != old_claim.fence
    assert fenced_operation(sessions, fresh).id == native_id
    assert fresh.payload == old_claim.payload
    assert (
        fenced_attempt(sessions, fresh).attempt
        > fenced_attempt(sessions, old_claim).attempt
    )
    assert restarted_jobs.record_late_result(
        AgentResult.model_validate(
            {
                "fence": old_claim.fence,
                "state": "succeeded",
                "result": {},
            }
        )
    )
    assert fenced_operation(sessions, fresh).state == "running"
    assert original_claims == _claims(sessions, nodes[0])
    restarted_jobs.record_result(
        AgentResult.model_validate(
            {
                "fence": fresh.fence,
                "state": "succeeded",
                "result": {},
            }
        )
    )
    final_core = _service(sessions, clock[0], lifecycle, RecordingArtifactExecutor())
    final_adapter = RunSwitchFleetProfileAdapter(sessions, final_core)
    for _ in range(3):
        final_core.tick()
        final_adapter.advance(original.id)
    completed = _stored(sessions, original.id)
    assert any(
        item.queue_index == stop_index
        and item.operation_id == stop_id
        and item.state == LifecycleState.SUCCEEDED
        for item in completed.children
    )
    assert completed.queue == before.queue
    with sessions() as session:
        assert (
            session.scalar(select(Job.id).where(Job.request_id == request_key))
            == stop_id
        )
        assert {
            item.id
            for item in session.scalars(
                select(AgentOperation).where(AgentOperation.kind == "recipe.stop")
            )
        } == {native_id}


@pytest.mark.parametrize(
    "fault", ["selected-cancel", "wrong-child-request", "malformed-child-plan"]
)
def test_postgres_stop_adoption_loses_authority_on_exact_refusal(pending_stop, fault):
    (
        sessions,
        profiles,
        profile,
        _clock,
        original,
        _lifecycle,
        _jobs,
        nodes,
        stop_id,
        _request_key,
        _stop_index,
        _native_id,
        _claim,
        _plan_digest,
        _ordinal,
        _healthy_child,
    ) = pending_stop
    replacement = profile("selected-replacement")
    review = profiles.preview(replacement.id)
    assert any(link.stops for link in review.effects.adopted)
    selected = profiles.apply(replacement.id, request_key=_uuid(18720), actor="admin")
    with sessions() as session:
        original_row = session.get(FleetProfileApplication, original.id)
        assert original_row is not None
        assert profiles._adopted_application_scope(session, original_row) == (nodes[0],)
    if fault == "selected-cancel":
        profiles.cancel(
            selected.id,
            profile_number=replacement.number,
            request_key=_uuid(18721),
            actor="admin",
        )
    else:
        # Deliberately corrupt only the exact queued child identity; inference
        # from its matching run/node must never grant continuing authority.
        with write_guard_mode(strict=False), sessions.begin() as session:
            stop = session.get(Job, stop_id)
            assert stop is not None
            if fault == "wrong-child-request":
                stop.request_id = _uuid(18722)
            else:
                stop.payload = {"plan": ["corrupt"]}
                stop.payload_digest = hashlib.sha256(
                    canonical_message(stop.payload)
                ).hexdigest()
    with sessions() as session:
        original_row = session.get(FleetProfileApplication, original.id)
        assert original_row is not None
        assert profiles._adopted_application_scope(session, original_row) is None
