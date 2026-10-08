"""PostgreSQL profile dispatch keeps independent effects live across restart."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import (
    AgentResult,
    AgentResultState,
    LifecycleState,
    OutcomeDone,
    OutcomeKind,
    RecipeStopResult,
    canonical_message,
)
from vonk_control.agent_jobs import AgentJobService
from vonk_control.fleet_profile_contract import (
    FleetProfileInput,
    FleetProfileSwitchAdapterState,
    profile_switch_child_request_key,
)
from vonk_control.fleet_profiles import (
    FleetProfileService,
    RunSwitchFleetProfileAdapter,
    _persisted_profile_progress,
)
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    Base,
    CatalogDocumentRevision,
    FleetProfileApplication,
    Job,
    ResourceReservation,
)
from vonk_control.run_switch_contract import RunSwitchMemberProgress
from vonk_control.run_switch_operations import RunSwitchOperationService

from .agent_fences import fenced_operation
from .test_fleet_profiles import (
    NOW,
    _node_id,
    _seed,
    _seed_dual_solo_without_runtime_state,
    _uuid,
)
from .test_profile_adapter_continuity import _running_child
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


def _parallel_world(engine: Engine, *, gang: bool = False):
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    (_seed_dual_solo_without_runtime_state if gang else _seed)(sessions)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=_node_id(3 if gang else 2),
                state="active",
                protocol_version=2,
                architecture="linux-arm64",
                last_seen_at=NOW,
            )
        )
    clock = [NOW]
    profiles = FleetProfileService(
        sessions,
        clock=lambda: clock[0],
        switch_adapter=_SQLCancellationAdapter(),
        assessment_provider=_assessment_with_promises,
    )
    profile = profiles.create(
        FleetProfileInput.model_validate(
            {
                "name": "Independent profile lanes",
                "assignments": [
                    {
                        "recipe_selector": "vonk-forge/synthetic-tiny-solo"
                        if gang and index == 3
                        else "vonk-forge/synthetic-tiny-build",
                        "spark_ids": [_node_id(1), _node_id(2)]
                        if gang and index == 1
                        else [_node_id(index)],
                        "desired_state": "running",
                        "assignment_name": f"lane-{index}",
                    }
                    for index in ((1, 3) if gang else (1, 2))
                ],
            }
        ),
        actor="admin",
    )
    application = profiles.apply(profile.id, request_key=_uuid(18500), actor="admin")
    assert profiles.tick()
    adapter = RunSwitchFleetProfileAdapter(
        sessions, RunSwitchOperationService(sessions, clock=lambda: clock[0])
    )
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        intended = _persisted_profile_progress(row).intended_profile
        assert intended is not None
        assignments = sorted(
            intended.assignments, key=lambda item: item.nodes[0].node_id
        )
        queue = [{"kind": "run", "id": item.id} for item in assignments]
        if gang:
            queue.insert(1, dict(queue[0]))
        adapter._write_state(
            session,
            row,
            FleetProfileSwitchAdapterState.model_validate_json(
                canonical_message(
                    {
                        "child_id": application.id,
                        "scope_node_ids": [_node_id(1), _node_id(2), _node_id(3)]
                        if gang
                        else [_node_id(1), _node_id(2)],
                        "assignment_ids": [item.id for item in assignments],
                        "assignments": [
                            item.model_dump(mode="json") for item in assignments
                        ],
                        "queue": queue,
                        "position": 0,
                        "pending_children": [],
                        "children": [],
                        "skipped_indices": [],
                        "actor": "admin",
                        "request_id": _uuid(18501),
                        "state": "queued",
                    }
                )
            ),
        )
    return sessions, profiles, clock, application, adapter, assignments, queue


def _stored(sessions, application_id):
    with sessions() as session:
        row = session.get(FleetProfileApplication, application_id)
        assert row is not None
        stored = _persisted_profile_progress(row).switch_adapter
        assert stored is not None
        return stored


def _claims(sessions, application_id):
    with sessions() as session:
        return {
            (row.id, row.node_id, row.kind, row.state)
            for row in session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == application_id
                )
            )
        }


@pytest.mark.parametrize(
    "pending_state", [LifecycleState.RUNNING, LifecycleState.NEEDS_OPERATOR]
)
def test_postgres_pending_lane_does_not_serialize_disjoint_work_after_restart(
    postgres_engine: Engine, monkeypatch, pending_state: LifecycleState
) -> None:
    """Catches the single-active-child early return and regenerated sibling identities."""
    sessions, _profiles, clock, app, adapter, assignments, queue = _parallel_world(
        postgres_engine
    )
    claims = _claims(sessions, app.id)
    assert claims
    observed = {}
    issued = []

    def dispatch(
        application_id, item, _assignments, scope, _actor, _request, index, _ordinal
    ):
        request_key = profile_switch_child_request_key(
            application_id, index, item.kind, item.id
        )
        issued.append((index, item.id, tuple(scope), request_key))
        child = _running_child(_uuid(18510 + index), scope[0])
        child.state = (
            LifecycleState.NEEDS_OPERATOR
            if index == 0 and pending_state == LifecycleState.NEEDS_OPERATOR
            else LifecycleState.RUNNING
        )
        observed[child.operation_id] = child
        return child

    monkeypatch.setattr(adapter, "_start_child", dispatch)
    monkeypatch.setattr(adapter, "_observed_child", observed.get)
    for _ in range(3):
        adapter.advance(app.id)
    assert issued == [
        (
            index,
            assignment.id,
            (assignment.nodes[0].node_id,),
            profile_switch_child_request_key(app.id, index, "run", assignment.id),
        )
        for index, assignment in enumerate(assignments)
    ]
    before = _stored(sessions, app.id)
    assert [
        (item.queue_index, item.operation_id) for item in before.pending_children
    ] == [
        (0, _uuid(18510)),
        (1, _uuid(18511)),
    ]
    assert [(item.kind, item.id) for item in before.queue] == [
        (item["kind"], item["id"]) for item in queue
    ]
    assert _claims(sessions, app.id) == claims

    # A new worker reads PostgreSQL; no in-memory dispatch position or child
    # cache can manufacture another request or reserve the same node twice.
    restarted = RunSwitchFleetProfileAdapter(
        sessions, RunSwitchOperationService(sessions, clock=lambda: clock[0])
    )
    monkeypatch.setattr(restarted, "_observed_child", observed.get)
    monkeypatch.setattr(
        restarted,
        "_start_child",
        lambda *args: pytest.fail("restart must reconnect to both exact live children"),
    )
    clock[0] += timedelta(seconds=1)
    restarted.advance(app.id)
    after = _stored(sessions, app.id)
    assert after.pending_children == before.pending_children
    assert after.queue == before.queue
    assert after.request_id == before.request_id
    assert _claims(sessions, app.id) == claims


def test_postgres_pending_gang_blocks_overlap_but_not_an_independent_lane(
    postgres_engine: Engine, monkeypatch
) -> None:
    """Catches rank-only claims and a fleet-wide lock around one pending gang."""
    sessions, _profiles, _clock, app, adapter, assignments, _queue = _parallel_world(
        postgres_engine, gang=True
    )
    issued = []
    observed = {}

    def dispatch(
        _application_id, item, _assignments, scope, _actor, _request, index, _ordinal
    ):
        issued.append((index, item.id, tuple(scope)))
        child = _running_child(_uuid(18710 + index), scope[0])
        child.node_ids = list(scope)
        child.progress.members = [
            RunSwitchMemberProgress(node_id=node, state="running") for node in scope
        ]
        observed[child.operation_id] = child
        return child

    monkeypatch.setattr(adapter, "_start_child", dispatch)
    monkeypatch.setattr(adapter, "_observed_child", observed.get)
    for _ in range(3):
        adapter.advance(app.id)
    assert issued == [
        (0, assignments[0].id, (_node_id(1), _node_id(2))),
        (2, assignments[1].id, (_node_id(3),)),
    ]
    stored = _stored(sessions, app.id)
    assert {item.queue_index for item in stored.pending_children} == {0, 2}
    assert stored.position == 1
    assert stored.skipped_indices == []


def test_postgres_real_pending_stop_allows_disjoint_load_and_reconnects_after_restart(
    postgres_engine: Engine, tmp_path, monkeypatch
) -> None:
    """Catches an exact offline Stop serializing the profile's healthy-node load."""
    sessions, lifecycle, _queue, mapping, build, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    installation = installed_recipe(
        lifecycle, mapping, build, nodes, request_id=_uuid(18600)
    )
    run = started_recipe(
        sessions, lifecycle, installation.owner_id, nodes, request_id=_uuid(18601)
    )
    # The accepted profile and its native receipt use the same current epoch.
    # The historical Start was established at the recipe fixture's earlier time.
    lifecycle._clock = lambda: NOW
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
        clock=lambda: NOW,
        switch_adapter=_SQLCancellationAdapter(),
        assessment_provider=_assessment_with_promises,
    )
    profile = profiles.create(
        FleetProfileInput.model_validate(
            {
                "name": "Stop offline lane and load healthy lane",
                "assignments": [
                    {
                        "recipe_selector": selector,
                        "spark_ids": [_node_id(2)],
                        "desired_state": "running",
                        "assignment_name": "healthy-lane",
                    }
                ],
            }
        ),
        actor="admin",
    )
    app = profiles.apply(profile.id, request_key=_uuid(18602), actor="admin")
    coordinator = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
    adapter = RunSwitchFleetProfileAdapter(sessions, coordinator)
    children = {}
    healthy_dispatches = []
    native_start = adapter._start_child
    native_observe = adapter._observed_child

    def dispatch(
        application_id, item, assignments, scope, actor, request, index, ordinal
    ):
        if item.kind == "stop":
            # The exact Stop is created by the real Run/Switch producer and
            # lifecycle; only the healthy node's physical load is intercepted.
            return native_start(
                application_id, item, assignments, scope, actor, request, index, ordinal
            )
        assert item.kind == "run"
        assert tuple(scope) == (_node_id(2),)
        healthy_dispatches.append((index, item.id))
        child = _running_child(_uuid(18603), _node_id(2))
        children[child.operation_id] = child
        return child

    monkeypatch.setattr(adapter, "_start_child", dispatch)
    monkeypatch.setattr(
        adapter,
        "_observed_child",
        lambda identity: children.get(identity) or native_observe(identity),
    )
    # The saved-profile worker establishes the accepted parent's current
    # authority and derives its canonical child request. A direct adapter start
    # while the application is queued cannot authorize destructive cleanup.
    profiles._switch_adapter = adapter
    for _ in range(8):
        profiles.tick()
        coordinator.tick()
    before = _stored(sessions, app.id)
    stop_index = next(
        index for index, item in enumerate(before.queue) if item.kind == "stop"
    )
    stop_item = before.queue[stop_index]
    assert stop_item.id == run.owner_id
    stop_key = profile_switch_child_request_key(
        app.id, stop_index, "stop", run.owner_id
    )
    with sessions() as session:
        stop = session.scalar(select(Job).where(Job.request_id == stop_key))
        assert stop is not None and stop.kind == "recipe.stop.v2"
        stop_id = stop.id
        exact = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.kind == "recipe.stop")
            )
        )
        assert exact and {item.node_id for item in exact} == {nodes[0]}
        assert all(item.payload["run_id"] == run.owner_id for item in exact)
        exact_ids = {item.id for item in exact}
        exact_parents = {item.parent_job_id for item in exact}
    assert len(healthy_dispatches) == 1
    assert (stop_index, stop_id) in {
        (item.queue_index, item.operation_id) for item in before.pending_children
    }
    with sessions.begin() as session:
        node = session.get(AgentNode, nodes[0])
        assert node is not None
        node.last_seen_at = NOW - timedelta(hours=1)
    claims = _claims(sessions, app.id)
    restarted_coordinator = _service(
        sessions, NOW, lifecycle, RecordingArtifactExecutor()
    )
    restarted = RunSwitchFleetProfileAdapter(sessions, restarted_coordinator)
    monkeypatch.setattr(
        restarted,
        "_observed_child",
        lambda identity: children.get(identity) or restarted_coordinator.get(identity),
    )
    monkeypatch.setattr(
        restarted,
        "_start_child",
        lambda *args: pytest.fail(
            "restart must observe the exact accepted Stop and load"
        ),
    )
    lifecycle.reconcile_offline_stops()
    with sessions() as session:
        assert (
            set(
                session.scalars(
                    select(Job.id).where(
                        Job.id.in_(exact_parents), Job.state == LifecycleState.SUCCEEDED
                    )
                )
            )
            == exact_parents
        )
    # The wrapper observes the native Stop, verifies the foreground result,
    # then completes on separate worker turns. None requires agent contact or
    # advancing the frozen clock; one tick only observes the native receipt.
    for _ in range(3):
        restarted_coordinator.tick()
        restarted.advance(app.id)
    after = _stored(sessions, app.id)
    assert {item.operation_id for item in after.pending_children} == {_uuid(18603)}
    assert any(
        item.operation_id == stop_id and item.state == "succeeded"
        for item in after.children
    )
    assert after.queue == before.queue
    assert after.request_id == before.request_id
    assert _claims(sessions, app.id) == claims
    with sessions() as session:
        assert {
            item.id
            for item in session.scalars(
                select(AgentOperation).where(AgentOperation.kind == "recipe.stop")
            )
        } == exact_ids
        assert (
            session.scalar(select(Job.id).where(Job.request_id == stop_key)) == stop_id
        )

    # Recovery claims the SAME native Stop and obtains its production signed
    # exact-plan grant before accepting the fenced agent receipt. It does not
    # mark the parent complete through the unfenced fixture projection helper.
    claim, stop_plan, _grant = _issue_exact_stop_grant(
        sessions, node_id=nodes[0], certificate_serial="serial-0", grant_now=NOW
    )
    assert stop_plan.run_id == run.owner_id
    issued = fenced_operation(sessions, claim)
    assert issued.id in exact_ids and issued.parent_job_id in exact_parents
    restarted_jobs = AgentJobService(sessions, clock=lambda: NOW)
    restarted_jobs.set_result_consumer(lifecycle.consume_agent_result)
    restarted_jobs.record_result(
        AgentResult(
            fence=claim.fence,
            state=AgentResultState.SUCCEEDED,
            result=OutcomeDone(kind=OutcomeKind.DONE, result=RecipeStopResult()),
        )
    )
    for _ in range(3):
        restarted_coordinator.tick()
        restarted.advance(app.id)
    recovered = _stored(sessions, app.id)
    assert any(
        item.queue_index == stop_index
        and item.operation_id == stop_id
        and item.state == "succeeded"
        for item in recovered.children
    )
    assert {item.operation_id for item in recovered.pending_children} == {_uuid(18603)}
    assert len(healthy_dispatches) == 1
    assert recovered.queue == before.queue

    # The old physical gang is free even while the disjoint load stays pending.
    # Exercise normal admission and a fenced native Start on that exact gang.
    from .profile_stop_readmission_support import start_on_released_gang

    fresh_run_id = start_on_released_gang(
        sessions,
        lifecycle,
        run.owner_id,
        nodes,
        clock=lifecycle._clock,
        request_id=_uuid(18604),
    )
    assert fresh_run_id != run.owner_id
    assert _stored(sessions, app.id).pending_children == recovered.pending_children
    assert len(healthy_dispatches) == 1
