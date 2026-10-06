"""A profile load on the lifecycle core (step 6 of the blocker audit's migration).

A profile application is a composite over Run/Switch: its state is
``aggregate(children)``, it never waits for an operator (no profile action
exists), and its cancel always completes.  These tests pin what the migration
changed:

* a legacy ``waiting-for-operator`` load (the mirror of a child that waited for a
  person) adopts and heals: it runs again and its child is observed, never left
  and never touched;
* ``needs-operator`` is unreachable for this kind, over the whole state space;
* a cancel completes: for a child issued without a recorded workload intent (it
  used to be refused with "cannot reconcile"), for a child that cannot be
  stopped (the core's budget ends it with the effect recorded unknown), across a
  restart, and for evidence that cannot be read;
* a child record that is gone is observed again by issuing the step again, not
  failed;
* a restart in the middle of any of it is safe.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest
from vonk_agent_protocol import LifecycleState
from vonk_control import fleet_profile_states
from vonk_control.agent_operation_facts import SUPERSEDED_CANCELLATION_SECONDS
from vonk_control.fleet_profile_contract import FleetProfileApplicationProgress
from vonk_control.fleet_profiles import (
    FleetProfileConflict,
    build_production_fleet_profile_service,
)
from vonk_control.lifecycle import (
    CancelRequested,
    Effect,
    LeaseLapsed,
    Observed,
    Outcome,
    Reported,
    State,
    Tick,
)
from vonk_control.lifecycle.fleet_profile import (
    CANCEL_BUDGET,
    FleetProfileAdapter,
)
from vonk_control.models import FleetProfileApplication, Job

from .test_fleet_profile_cancel import _application
from .test_fleet_profiles import _uuid
from .test_recipe_operations import NOW

_PROGRESS = FleetProfileApplicationProgress().model_dump(mode="json")


class _World:
    """A started profile load over a real Run/Switch child, with a movable clock."""

    def __init__(self, tmp_path) -> None:
        (
            self.sessions,
            self.run_switch,
            self.service,
            self.profile,
            self.application,
            self.adapter,
            self.nodes,
        ) = _application(tmp_path)
        self.now = [self.service._clock()]
        self._pin(self.service)
        self.id = self.application.id
        assert self.service.tick() is True  # issues the first child
        assert self.service.application(self.id).state == "running"

    def _pin(self, service) -> None:
        service._clock = lambda: self.now[0]
        service._lifecycle._clock = service._clock
        service._switch_adapter._run_switch._clock = service._clock

    def restart(self):
        """What a Controller restart is: a new service over the same database."""

        service = build_production_fleet_profile_service(
            self.sessions,
            clock=lambda: self.now[0],
            run_switch_operations=self.run_switch,
        )
        self._pin(service)
        self.service = service
        self.adapter = service._switch_adapter
        return service

    def row(self) -> FleetProfileApplication:
        with self.sessions() as session:
            row = session.get(FleetProfileApplication, self.id)
            assert row is not None
            session.expunge(row)
            return row

    def edit(self, **fields) -> None:
        with self.sessions.begin() as session:
            row = session.get(FleetProfileApplication, self.id)
            assert row is not None
            for key, value in fields.items():
                setattr(row, key, value)

    def edit_progress(self, change) -> None:
        with self.sessions.begin() as session:
            row = session.get(FleetProfileApplication, self.id)
            assert row is not None
            progress = FleetProfileApplicationProgress.model_validate_json(
                _json(row.progress)
            ).model_dump(mode="json")
            change(progress)
            row.progress = FleetProfileApplicationProgress.model_validate_json(
                _json(progress)
            ).model_dump(mode="json")

    def child_id(self) -> str:
        document = self.service.application(self.id).progress.switch_adapter
        assert document is not None and document.active_operation_id is not None
        return document.active_operation_id

    def cancel(self, key: int = 700):
        return self.service.cancel(
            self.id,
            profile_number=self.profile.number,
            request_key=_uuid(key),
            actor="admin",
        )


def _json(value) -> str:
    return json.dumps(value, default=str)


# -------------------------------------------------------- adoption and heal


def test_a_legacy_operator_wait_is_healed_and_its_child_is_left_alone(tmp_path) -> None:
    """The mirror of a child that waited for a person: nothing can end that wait,
    so the load runs again and its exact child is observed; it is never touched."""

    world = _World(tmp_path)
    child_id = world.child_id()
    with world.sessions() as session:
        before = session.get(Job, child_id)
        assert before is not None
        snapshot = (before.state, before.status_reason, dict(before.result or {}))
    world.edit(state="waiting-for-operator", status_reason="the child is parked")
    assert world.service.application(world.id).state == LifecycleState.NEEDS_OPERATOR

    assert world.service.tick() is True  # healed
    healed = world.service.application(world.id)
    assert healed.state in {"running", "queued"}
    assert healed.state != LifecycleState.NEEDS_OPERATOR
    assert world.child_id() == child_id  # the same child
    with world.sessions() as session:
        after = session.get(Job, child_id)
        assert after is not None
        assert (after.state, after.status_reason, dict(after.result or {})) == snapshot

    for _ in range(3):  # and it never goes back to waiting for anyone
        world.service.tick()
        assert (
            world.service.application(world.id).state != LifecycleState.NEEDS_OPERATOR
        )


def test_a_legacy_child_state_is_mirrored_as_running_never_as_a_wait(tmp_path) -> None:
    """The switch-adapter document of an older Controller carried the child's wait."""

    world = _World(tmp_path)

    def legacy(progress) -> None:
        document = progress["switch_adapter"]
        document["state"] = "waiting-for-operator"
        document["status_reason"] = "the child is parked"

    world.edit_progress(legacy)
    world.edit(state="waiting-for-operator")
    for _ in range(4):
        world.service.tick()
    view = world.service.application(world.id)
    assert view.state != LifecycleState.NEEDS_OPERATOR
    assert view.progress.switch_adapter is not None
    assert view.progress.switch_adapter.state != LifecycleState.NEEDS_OPERATOR


def test_a_legacy_wait_still_being_admitted_retries_its_admission(tmp_path) -> None:
    world = _World(tmp_path)
    world.edit(state="waiting-for-operator", current_operation_id=None, current_step=0)

    def pending(progress) -> None:
        progress["admission_pending"] = True
        progress["admission_attempt"] = 1
        progress["admission_retry_at"] = (world.now[0] + timedelta(hours=1)).isoformat()
        progress["workload_intent_ordinal"] = None
        progress["switch_adapter"] = None

    world.edit_progress(pending)
    assert world.service.tick() is True
    healed = world.service.application(world.id)
    assert healed.state == "queued"
    assert healed.progress.admission_pending is True


# ------------------------------------------------------------- state space


def _application_row(state: str, progress: dict) -> FleetProfileApplication:
    return FleetProfileApplication(
        id=str(uuid.uuid4()),
        request_key=str(uuid.uuid4()),
        state=state,
        current_step=0,
        current_operation_id=None,
        progress=progress,
        plan={},
        actor="admin",
    )


_SHAPES = {
    "bare": {},
    "admission": {
        "admission_pending": True,
        "admission_attempt": 2,
        "admission_retry_at": NOW.isoformat(),
    },
    "ordinal": {"workload_intent_ordinal": 3},
    "cancelling": {
        "workload_intent_ordinal": 3,
        "cancellation": {
            "request_key": _uuid(5),
            "actor": "admin",
            "requested_at": NOW.isoformat(),
            "cause": "operator",
        },
    },
}
_EVENTS = (
    Reported(Outcome.FAILED, retryable=True, reason="x"),
    Reported(Outcome.UNKNOWN, reason="x"),
    Reported(Outcome.FAILED, retryable=False, reason="x"),
    Reported(Outcome.DONE),
    LeaseLapsed(),
    Observed(Effect.UNKNOWN),
    Observed(Effect.NONE),
    Observed(Effect.STOPPED),
    CancelRequested("k", "stop"),
    Tick(),
)


@pytest.mark.parametrize("stored", ["queued", "running", "waiting-for-operator"])
@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_no_profile_state_can_wait_for_an_operator(stored: str, shape: str) -> None:
    """Rule 3 for this kind, over the whole state space: nothing is irreversible
    and no action exists, so no event leaves a load waiting for a person."""

    adapter = FleetProfileAdapter()
    row = _application_row(stored, {**_PROGRESS, **_SHAPES[shape]})
    now = NOW + timedelta(hours=1)
    lifecycle = adapter.adopt(row)
    assert adapter.irreversible(lifecycle) is False
    assert adapter.actions(lifecycle) == ()
    for event in _EVENTS:
        if lifecycle.state is State.NEEDS_OPERATOR and isinstance(event, LeaseLapsed):
            continue  # a lease lapse says nothing about a load that holds none
        decision = adapter.decide(row, event, now)
        assert decision.row.state is not State.NEEDS_OPERATOR, (event, stored, shape)


def test_the_legacy_wait_is_the_only_needs_operator_and_it_is_read_only() -> None:
    adapter = FleetProfileAdapter()
    legacy = adapter.adopt(_application_row("waiting-for-operator", dict(_PROGRESS)))
    assert legacy.state is State.NEEDS_OPERATOR
    for stored in ("queued", "running", "succeeded", "failed", "cancelled"):
        assert (
            adapter.adopt(_application_row(stored, dict(_PROGRESS))).state
            is not State.NEEDS_OPERATOR
        )


# -------------------------------------------------------------------- cancel


def test_a_cancel_completes_for_a_child_issued_without_a_recorded_intent(
    tmp_path,
) -> None:
    """Before: refused with "cannot reconcile operation ... without a recorded
    workload intent", leaving the load running with no way to stop it."""

    world = _World(tmp_path)
    world.edit_progress(lambda progress: progress.update(workload_intent_ordinal=None))

    accepted = world.cancel()  # no longer a refusal
    assert accepted.cancellation is not None
    for _ in range(12):
        if world.service.application(world.id).state == "cancelled":
            break
        world.now[0] += timedelta(seconds=10)
        world.service.tick()
        world.run_switch.tick()
    final = world.service.application(world.id)
    assert final.state == "cancelled"
    assert final.cancellation is not None and final.cancellation.state == "cancelled"
    assert final.state != LifecycleState.NEEDS_OPERATOR


def _stuck_child(world: _World) -> None:
    """A child that can neither be stopped nor observed to end."""

    world.adapter.request_cancellation = lambda *_a, **_k: None  # type: ignore[method-assign]


def test_a_cancel_completes_when_the_stop_cannot_be_confirmed(tmp_path) -> None:
    """Rule 4: the core's budget ends the cancel; it never parks the load.

    The child is left to its own lifecycle; the load ends ``cancelled`` with the
    child's effect recorded as unknown.
    """

    world = _World(tmp_path)
    _stuck_child(world)
    world.cancel()
    states = []
    for _ in range(200):
        world.now[0] += timedelta(seconds=15)
        world.service.tick()
        view = world.service.application(world.id)
        states.append(view.state)
        if view.state == "cancelled":
            break
    final = world.service.application(world.id)
    assert final.state == "cancelled", states[-5:]
    assert "waiting-for-operator" not in states
    assert world.now[0] - NOW >= timedelta(0)
    assert final.status_reason is not None and "effect unknown" in final.status_reason
    assert final.cancellation is not None and final.cancellation.state == "cancelled"
    # the child kept running on its own lifecycle: it was never aborted
    with world.sessions() as session:
        child = session.get(Job, world.child_id())
        assert child is not None and child.state not in {"cancelled", "failed"}


def test_the_cancel_budget_is_the_one_cancellation_authority_of_an_agent_order() -> (
    None
):
    """One constant (``agent_operation_facts``), not a copy per kind."""

    assert CANCEL_BUDGET == timedelta(seconds=SUPERSEDED_CANCELLATION_SECONDS)


def test_a_cancel_survives_a_restart_in_the_middle(tmp_path) -> None:
    world = _World(tmp_path)
    _stuck_child(world)
    world.cancel()
    for _ in range(200):
        world.restart()
        _stuck_child(world)
        world.now[0] += timedelta(seconds=15)
        world.service.tick()
        if world.service.application(world.id).state == "cancelled":
            break
    assert world.service.application(world.id).state == "cancelled"


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_cancel_completes_when_its_evidence_cannot_be_read(tmp_path) -> None:
    """Unreadable evidence of what was issued is an unknown effect, not a reason to
    park the cancel: the load ends ``cancelled`` and the document is retained."""

    world = _World(tmp_path)
    world.cancel()
    damaged = {**world.row().progress, "intended_profile": {"unreadable": True}}
    world.edit(progress=damaged)
    for _ in range(6):
        world.now[0] += timedelta(seconds=15)
        world.service.tick()
        if world.row().state == "cancelled":
            break
    ended = world.row()
    assert ended.state == "cancelled"
    assert "effect is unknown" in (ended.status_reason or "")
    assert ended.progress["intended_profile"] == {"unreadable": True}  # retained


def test_a_repeated_cancel_request_replays_and_a_different_one_is_refused(
    tmp_path,
) -> None:
    world = _World(tmp_path)
    first = world.cancel(710)
    again = world.cancel(710)
    assert again.cancellation is not None and first.cancellation is not None
    assert again.cancellation.request_key == first.cancellation.request_key
    with pytest.raises(FleetProfileConflict):
        world.cancel(711)


# ----------------------------------------------- a bookkeeping mismatch heals


def test_a_child_record_that_is_gone_is_issued_again_not_failed(tmp_path) -> None:
    """Bookkeeping becomes unknown, then reconcile: the child's record is gone
    (pruned, or lost with a restore), but its identity is deterministic, so the
    step is entered again and the Run/Switch owner reconciles what it did."""

    world = _World(tmp_path)
    gone = world.child_id()
    with world.sessions.begin() as session:
        job = session.get(Job, gone)
        assert job is not None
        session.delete(job)

    for _ in range(4):
        world.service.tick()
    view = world.service.application(world.id)
    assert view.state in {"running", "queued", "succeeded"}, view.status_reason
    assert view.state != "failed"
    document = view.progress.switch_adapter
    assert document is not None
    assert document.active_operation_id is not None
    with world.sessions() as session:
        assert session.get(Job, document.active_operation_id) is not None


# ---------------------------------------------------- state = aggregate(children)


def test_the_loads_state_is_the_aggregate_of_its_children(tmp_path) -> None:
    world = _World(tmp_path)
    adapter = FleetProfileAdapter(sessions=world.sessions)
    with world.sessions() as session:
        row = session.get(FleetProfileApplication, world.id)
        assert row is not None
        children = adapter.children_of(session, row)
    assert children, "the load has a Run/Switch child"
    state = adapter.aggregate_state(children)
    assert state in {State.QUEUED, State.RUNNING, State.OBSERVING, State.BACKOFF}
    # recorded children: the worst outcome wins; an empty record has no state
    assert adapter.recorded_aggregate([]) is None
    done = {"state": "succeeded"}
    assert adapter.recorded_aggregate([done, done]) is State.SUCCEEDED
    assert adapter.recorded_aggregate([done, {"state": "failed"}]) is State.FAILED
    assert adapter.recorded_aggregate([done, {"state": "cancelled"}]) is State.CANCELLED


def test_a_load_with_nothing_to_do_ends_by_its_children_not_by_a_label(
    tmp_path,
) -> None:
    world = _World(tmp_path)
    for _ in range(40):
        world.now[0] += timedelta(seconds=5)
        world.service.tick()
        world.run_switch.tick()
        if world.service.application(world.id).state in {"succeeded", "failed"}:
            break
    assert world.service.application(world.id).state != LifecycleState.NEEDS_OPERATOR


def test_a_restart_in_the_middle_of_a_load_is_safe(tmp_path) -> None:
    world = _World(tmp_path)
    child = world.child_id()
    for _ in range(3):
        world.restart()
        world.service.tick()
        world.run_switch.tick()
        assert world.child_id() == child  # the exact child, never replaced
        assert world.service.application(world.id).state != "failed"


@pytest.mark.usefixtures("damaged_json_rows")
def test_an_application_row_is_written_only_through_the_adapter() -> None:
    """The class, guarded: the module no longer writes an application's state."""

    from tests.lifecycle_writer_boundaries import scan_lifecycle_writes

    writes = [
        write
        for write in scan_lifecycle_writes()
        if write.path.endswith("fleet_profiles.py")
    ]
    assert writes == []


def test_profile_stop_selection_never_uses_the_active_run_set() -> None:
    """The class, guarded: a stop reads the stoppable set, which keeps ``lost``.

    ``ACTIVE_RUN_STATES`` excludes a lost run, so a stop selected from it would
    leave that run's residue on the Spark forever.  Admission and capacity code
    elsewhere may keep the active set; the profile module decides only what to
    stop and what is observed, so it must not name it at all.
    """

    import ast
    from pathlib import Path

    import vonk_control.fleet_profiles as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    uses = sorted(
        node.lineno
        for node in ast.walk(tree)
        if (isinstance(node, ast.Name) and node.id == "ACTIVE_RUN_STATES")
        or (isinstance(node, ast.alias) and node.name == "ACTIVE_RUN_STATES")
        or (isinstance(node, ast.Attribute) and node.attr == "ACTIVE_RUN_STATES")
    )
    assert uses == []


# ----------------------------------------- the exact rows an older Controller left


def _legacy_cancel_parking(world: _World) -> None:
    """Audit C9: a cancel parked as ``waiting-for-operator`` because it "cannot
    reconcile" its child: the load, its cancellation and the document all wait."""

    requested = world.now[0].isoformat()

    def legacy(progress) -> None:
        progress["cancellation"] = {
            "request_key": _uuid(720),
            "actor": "admin",
            "requested_at": requested,
            "state": "cancelling",
            "cause": "operator",
            "workload_intent_ordinal": (progress.get("workload_intent_ordinal") or 0)
            + 1,
        }
        progress["switch_adapter"]["state"] = "waiting-for-operator"
        progress["switch_adapter"]["status_reason"] = (
            "Cancellation cannot reconcile Run/Switch child state unknown"
        )

    world.edit_progress(legacy)
    world.edit(
        state="waiting-for-operator",
        status_reason="Cancellation cannot reconcile Run/Switch child state unknown",
    )


def test_a_legacy_parked_cancel_leaves_the_wait_and_completes(tmp_path) -> None:
    world = _World(tmp_path)
    _stuck_child(world)
    _legacy_cancel_parking(world)
    assert world.row().state in fleet_profile_states.NEEDS_OPERATOR

    world.now[0] += timedelta(seconds=6)
    world.service.tick()
    assert world.row().state != LifecycleState.NEEDS_OPERATOR  # on the first pass
    states = []
    for _ in range(200):
        world.now[0] += timedelta(seconds=15)
        world.service.tick()
        states.append(world.row().state)
        if states[-1] == "cancelled":
            break
    assert states[-1] == "cancelled"
    assert "waiting-for-operator" not in states
    final = world.service.application(world.id)
    assert final.cancellation is not None and final.cancellation.state == "cancelled"
    assert final.status_reason is not None and "effect unknown" in final.status_reason
    assert (
        "its stop" not in final.status_reason
        or final.status_reason.count("cancelled") == 1
    )


def test_a_legacy_parked_cancel_ends_as_soon_as_its_child_does(tmp_path) -> None:
    world = _World(tmp_path)
    _legacy_cancel_parking(world)
    for _ in range(12):
        world.now[0] += timedelta(seconds=10)
        world.service.tick()
        world.run_switch.tick()
        if world.row().state == "cancelled":
            break
    assert world.row().state == "cancelled"
    assert "effect unknown" not in (world.row().status_reason or "")


def test_a_legacy_wait_whose_child_already_ended_records_that_ending(tmp_path) -> None:
    """Audit C10, the terminal-child shape the old parked-application observer
    handled: the parent heals, and the ordinary tick records what its child did."""

    world = _World(tmp_path)
    child_id = world.child_id()
    with world.sessions.begin() as session:
        child = session.get(Job, child_id)
        assert child is not None
        child.state = "failed"
        child.status_reason = "run-switch.the-child-ended"
    world.edit(state="waiting-for-operator", status_reason="the child is parked")

    for _ in range(4):
        world.service.tick()
        if world.row().state == "failed":
            break
    ended = world.row()
    assert ended.state == "failed"  # (shown as queued while a retry is due)
    assert "run-switch.the-child-ended" in (ended.status_reason or "")


def test_a_child_that_is_a_legacy_wait_is_mirrored_as_running_not_as_a_wait(
    tmp_path,
) -> None:
    """Audit C10, the mirror itself: a Run/Switch child an older Controller left in
    ``waiting-for-operator``.  The profile never mirrors that wait; its child is
    healed by its own tick, and the load follows."""

    world = _World(tmp_path)
    child_id = world.child_id()
    with world.sessions.begin() as session:
        child = session.get(Job, child_id)
        assert child is not None and isinstance(child.result, dict)
        result = dict(child.result)
        result.pop("observation_due_at", None)
        child.result = result
        child.state = "waiting-for-operator"
    for _ in range(3):
        world.service.tick()
        view = world.service.application(world.id)
        assert view.state != LifecycleState.NEEDS_OPERATOR
        assert view.progress.switch_adapter is not None
        assert view.progress.switch_adapter.state != LifecycleState.NEEDS_OPERATOR
    world.run_switch.tick()  # the child's own tick heals it
    world.now[0] += timedelta(seconds=10)
    world.service.tick()
    with world.sessions() as session:
        healed = session.get(Job, child_id)
        assert healed is not None and healed.state != LifecycleState.NEEDS_OPERATOR
    assert world.service.application(world.id).state != LifecycleState.NEEDS_OPERATOR
