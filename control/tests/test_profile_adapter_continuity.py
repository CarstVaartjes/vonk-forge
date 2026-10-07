"""Original profile queue recovery stays inside the currently adopted lanes."""

from types import SimpleNamespace

from vonk_agent_protocol import LifecycleState
from vonk_control.fleet_profile_contract import (
    FleetProfileInput,
    FleetProfileSwitchAdapterResult,
    FleetProfileSwitchAdapterState,
    FleetProfileSwitchChildState,
    FleetProfileSwitchPendingChild,
    FleetProfileSwitchQueueItem,
)
from vonk_control.fleet_profiles import (
    RunSwitchFleetProfileAdapter,
    _persisted_profile_progress,
)
from vonk_control.models import FleetProfileApplication, FleetProfileSelection
from vonk_control.run_switch_contract import (
    RunSwitchMemberProgress,
    RunSwitchOperation,
    RunSwitchProgress,
)
from vonk_control.run_switch_operations import RunSwitchOperationService

from .test_fleet_profile_continuity import _world
from .test_fleet_profiles import NOW, _node_id, _uuid


def _running_child(operation_id, node_id):
    return RunSwitchOperation(
        operation_id=operation_id,
        kind="recipe.run-switch.v2",
        action="switch",
        state=LifecycleState.RUNNING,
        plan_digest="a" * 64,
        request_key=_uuid(18110),
        node_ids=[node_id],
        completed_phases=[],
        progress=RunSwitchProgress(
            phase_index=0,
            phase_count=1,
            phase="transfer",
            state=LifecycleState.RUNNING,
            total_bytes_known=False,
            members=[RunSwitchMemberProgress(node_id=node_id, state="running")],
        ),
    )


def test_partial_adoption_keeps_active_identity_and_skips_changed_queued_lane(
    monkeypatch,
):
    """Catches renumbering child identities or issuing the old replaced lane."""
    sessions, service, _fake, first, _, both_profile = _world()
    # Begin this fixture with no standing single-lane authority; the two-lane
    # executor below is the original owner of both independent assignments.
    with sessions.begin() as session:
        initial = session.get(FleetProfileApplication, first.id)
        selection = session.get(FleetProfileSelection, 1)
        assert initial is not None and selection is not None
        initial.state = "succeeded"
        session.delete(selection)
    both = service.apply(both_profile.id, request_key=_uuid(18100), actor="admin")
    choices = [
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
            "assignment_name": "replacement-two",
        },
    ]
    changed = service.create(
        FleetProfileInput.model_validate(
            {"name": "Changed second", "assignments": choices}
        ),
        actor="admin",
    )
    reviewed = service.preview(changed.id)
    assert [
        (item.application_id, item.node_ids) for item in reviewed.effects.adopted
    ] == [(both.id, [_node_id(1)])]
    selected = service.apply(changed.id, request_key=_uuid(18101), actor="admin")
    assert selected.id != both.id
    adapter = RunSwitchFleetProfileAdapter(
        sessions, RunSwitchOperationService(sessions, clock=lambda: NOW)
    )
    active_id = _uuid(18102)
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, both.id)
        assert row is not None
        intended = _persisted_profile_progress(row).intended_profile
        assert intended is not None
        assignments = tuple(intended.assignments)
        by_node = {
            assignment.nodes[0].node_id: assignment for assignment in assignments
        }
        state = FleetProfileSwitchAdapterState(
            child_id=row.id,
            scope_node_ids=[_node_id(1), _node_id(2)],
            assignment_ids=[item.id for item in assignments],
            assignments=list(assignments),
            queue=[
                FleetProfileSwitchQueueItem(kind="run", id=by_node[_node_id(1)].id),
                FleetProfileSwitchQueueItem(kind="run", id=by_node[_node_id(2)].id),
                FleetProfileSwitchQueueItem(kind="run", id=by_node[_node_id(1)].id),
            ],
            pending_children=[
                FleetProfileSwitchPendingChild(
                    queue_index=0, operation_id=active_id, kind="run"
                )
            ],
            actor="admin",
            request_id=_uuid(18103),
            state=LifecycleState.RUNNING,
        )
        adapter._write_state(session, row, state)
    child = _running_child(active_id, _node_id(1))
    monkeypatch.setattr(adapter, "_observed_child", lambda identity: child)
    monkeypatch.setattr(
        adapter,
        "_start_child",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("active identity must be observed")
        ),
    )
    adapter.advance(both.id)
    with sessions() as session:
        row = session.get(FleetProfileApplication, both.id)
        assert row is not None
        stored = _persisted_profile_progress(row).switch_adapter
        assert stored is not None
        assert [child.operation_id for child in stored.pending_children] == [active_id]
    # Resume after a persisted checkpoint and a new adapter instance. The old
    # replaced item remains in history but its original index is never issued.
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, both.id)
        assert row is not None
        state = adapter._state(row)
        assert state is not None
        state.position = 1
        state.pending_children = []
        state.children = [
            FleetProfileSwitchChildState(
                queue_index=0,
                operation_id=active_id,
                kind="run",
                state=LifecycleState.SUCCEEDED,
            )
        ]
        adapter._write_state(session, row, state)
    restarted = RunSwitchFleetProfileAdapter(
        sessions, RunSwitchOperationService(sessions, clock=lambda: NOW)
    )
    issued = []

    def start(*args):
        issued.append((args[1].id, args[3], args[6]))
        return _running_child(_uuid(18104), _node_id(1))

    monkeypatch.setattr(restarted, "_start_child", start)
    restarted.advance(both.id)
    assert issued == [(by_node[_node_id(1)].id, (_node_id(1),), 2)]
    with sessions() as session:
        row = session.get(FleetProfileApplication, both.id)
        assert row is not None
        stored = _persisted_profile_progress(row).switch_adapter
        assert stored is not None
        assert stored.position == 2
        assert stored.queue[1].id == by_node[_node_id(2)].id
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, both.id)
        assert row is not None
        state = restarted._state(row)
        assert state is not None
        state.position = 3
        state.pending_children = []
        state.children.append(
            FleetProfileSwitchChildState(
                queue_index=2,
                operation_id=_uuid(18104),
                kind="run",
                state=LifecycleState.SUCCEEDED,
            )
        )
        restarted._write_state(session, row, state)
    completed = restarted.advance(both.id)
    assert isinstance(completed.result, FleetProfileSwitchAdapterResult)
    assert completed.status_reason is not None
    assert completed.result.assignment_ids == [by_node[_node_id(1)].id]
    assert "other assignments were replaced" in completed.status_reason


def test_selected_cancel_observes_borrowed_agent_receipt_before_completion(monkeypatch):
    """Catches clean cancellation while a borrowed exact effect remains live."""
    from datetime import timedelta

    from vonk_control.agent_jobs import AgentJobService

    sessions, service, _fake, _first, _original, second_profile = _world()
    second = service.apply(second_profile.id, request_key=_uuid(18200), actor="admin")
    service.cancel(
        second.id,
        profile_number=second_profile.number,
        request_key=_uuid(18201),
        actor="admin",
    )
    pending = [
        SimpleNamespace(
            operation_id=_uuid(18202),
            observe_due_at=NOW + timedelta(seconds=2),
            observation_deadline=NOW + timedelta(seconds=60),
        )
    ]
    observations = []

    def assess(session, scope, ordinal, now):
        observations.append((scope, ordinal))
        return tuple(pending) if scope == (_node_id(1),) else ()

    monkeypatch.setattr(
        AgentJobService, "assess_superseded_agent_effects_in_session", assess
    )
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, second.id)
        assert row is not None
        assert not service._stop_children(session, row)
        progress = _persisted_profile_progress(row)
        assert progress.cancellation is not None
        assert row.status_reason is not None
        assert progress.cancellation.pending_operation_ids == [_uuid(18202)]
        assert "adopted assignment effects" in row.status_reason
        ordinal = progress.cancellation.workload_intent_ordinal
    # Receipt arrives after a lost-response wait; normal cancellation observes
    # the same exact authority and can now complete without a fresh decision.
    pending.clear()
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, second.id)
        assert row is not None
        assert service._stop_children(session, row)
    assert observations[:2] == [((_node_id(1),), ordinal)] * 2
