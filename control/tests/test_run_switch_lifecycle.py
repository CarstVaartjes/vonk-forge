"""Run/Switch on the lifecycle core (step 5 of the blocker audit's migration).

A Run/Switch operation is a composite: its phases are done by children, so it is
never irreversible, never waits for an operator (no action exists), and follows
the core's rules.  These tests pin what the migration changed:

* every legacy stored shape adopts, and a wait that nothing can end is healed;
* a running operation with a live child is left alone (the hardware sweep);
* ``needs-operator`` is unreachable for this kind, over the whole state space;
* a cancel always completes (a legacy parked row, a stop that stays unconfirmed,
  a restart in the middle);
* a bookkeeping mismatch (a receipt that does not validate, a final verification
  that cannot be observed, any non-security conflict of the phase path) is
  observed again at the core's backoff instead of failing the operation;
* a restart in the middle of a retry is safe.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from vonk_agent_protocol import LifecycleState
from vonk_control.failure_classification import is_security_failure
from vonk_control.lifecycle import (
    STOP_BUDGET,
    CancelRequested,
    Effect,
    LeaseLapsed,
    Observed,
    Outcome,
    Reported,
    State,
    Tick,
)
from vonk_control.lifecycle.run_switch import RunSwitchAdapter
from vonk_control.models import Job
from vonk_control.run_switch_contract import (
    RunSwitchApplyRequest,
    RunSwitchOperationResult,
)
from vonk_control.run_switch_operations import (
    PhaseExecution,
    RunSwitchOperationConflict,
    _phase_result,
    _read_progress,
)

from .test_recipe_operations import NOW, installed_recipe, setup_services
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
    _child_operation_id,
    _parked_start_switch,
    _replace_child,
    _request,
    _result,
    _service,
)

_ALLOWLIST = Path(__file__).resolve().parents[2] / "tools" / "blocker-allowlist.json"


class _Harness:
    """A real service over a real database with a clock the test advances."""

    def __init__(
        self,
        tmp_path: Path,
        executor: RecordingArtifactExecutor,
    ) -> None:
        self.sessions, self.lifecycle, _queue, mapping_id, build_id, self.nodes = (
            setup_services(tmp_path)
        )
        installed_recipe(
            self.lifecycle,
            mapping_id,
            build_id,
            self.nodes,
            request_id=str(uuid.uuid4()),
        )
        self.executor = executor
        self.now = [NOW]
        self.service = self.restart()
        request = _request(self.sessions, self.nodes[0])
        self.operation = self.service.apply(
            RunSwitchApplyRequest(
                **request.model_dump(), request_key=str(uuid.uuid4())
            ),
            actor="admin",
        )
        self.id = self.operation.operation_id

    def restart(self):
        """A new service over the same database: what a Controller restart is."""

        service = _service(
            self.sessions,
            NOW,
            self.lifecycle,
            self.executor,
            artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
        )
        service._clock = lambda: self.now[0]
        self.service = service
        return service

    def view(self):
        return self.service.get(self.id)

    def row(self) -> dict[str, Any]:
        with self.sessions() as session:
            job = session.get(Job, self.id)
            assert job is not None
            return {
                "state": job.state,
                "status_reason": job.status_reason,
                "result": json.loads(json.dumps(job.result, default=str)),
            }

    def make_legacy_operator_wait(self) -> None:
        """What an older Controller left: ``waiting-for-operator``, no clock."""

        with self.sessions.begin() as session:
            job = session.get(Job, self.id)
            assert job is not None and isinstance(job.result, dict)
            result = dict(job.result)
            for key in ("observation_due_at", "retry_attempt", "retry_reason"):
                result.pop(key, None)
            job.state = "waiting-for-operator"
            job.result = result

    def advance_to_due(self) -> None:
        due = _result(self.view()).observation_due_at
        if due is not None:
            self.now[0] = max(self.now[0], due)


class _ScriptedExecutor(RecordingArtifactExecutor):
    """Raise or return something invalid for chosen phases, then behave."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.faults: dict[str, Any] = {}

    def execute(self, plan, phase, **kwargs):
        fault = self.faults.get(phase.kind)
        if isinstance(fault, BaseException):
            self.calls.append(phase.kind)
            raise fault
        if fault is not None:
            self.calls.append(phase.kind)
            return fault(phase) if callable(fault) else fault
        return super().execute(plan, phase, **kwargs)


# --------------------------------------------------------------- adoption


def _job(state: str, progress: dict[str, Any]) -> Job:
    """A transient (never persisted) legacy row in ``state``."""

    return Job(
        id=str(uuid.uuid4()),
        kind="recipe.run-switch.v2",
        state=state,
        actor="admin",
        targets=["spk_a"],
        payload={"workload_intent_ordinal": 1},
        result=progress,
    )


_DUE = (NOW + timedelta(seconds=30)).isoformat()
_SHAPES: dict[str, dict[str, Any]] = {
    "bare": {},
    "clock": {"observation_due_at": _DUE, "retry_attempt": 3},
    "retry": {
        "observation_due_at": _DUE,
        "retry_attempt": 3,
        "retry_reason": "run-switch.phase-failed",
    },
    "child": {"child_operation_id": str(uuid.uuid4())},
    "child-clock": {
        "child_operation_id": str(uuid.uuid4()),
        "observation_due_at": _DUE,
    },
    "cancelling": {
        "child_operation_id": str(uuid.uuid4()),
        "cancellation": {
            "request_key": str(uuid.uuid4()),
            "actor": "admin",
            "reason": "stop",
            "requested_at": NOW.isoformat(),
        },
    },
}
_STORED = ("queued", "running", "waiting", "waiting-for-operator")
_EVENTS = (
    Reported(Outcome.FAILED, retryable=True, reason="x"),
    Reported(Outcome.UNKNOWN, reason="x"),
    Reported(Outcome.FAILED, retryable=False, reason="x"),
    LeaseLapsed(),
    Observed(Effect.UNKNOWN),
    Observed(Effect.NONE),
    Observed(Effect.ESTABLISHED),
    CancelRequested("k", "stop"),
    Tick(),
)


@pytest.mark.parametrize("stored", _STORED)
@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_no_run_switch_state_can_wait_for_an_operator(stored: str, shape: str) -> None:
    """Rule 3 for this kind, over the whole state space: nothing is irreversible,
    no action exists, so no event leaves a row waiting for a person."""

    adapter = RunSwitchAdapter()
    job = _job(stored, dict(_SHAPES[shape]))
    now = NOW + timedelta(hours=1)
    row = adapter.lifecycle(job, _read_progress(job.result), now)
    assert adapter.irreversible(row) is False
    assert adapter.actions(row) == ()
    for event in _EVENTS:
        if row.state is State.NEEDS_OPERATOR and isinstance(event, LeaseLapsed):
            continue  # a lease lapse says nothing about a row that holds none
        decision = adapter.decide(job, _read_progress(job.result), event, now)
        assert decision.row.state is not State.NEEDS_OPERATOR, (event, stored, shape)


def test_a_legacy_operator_wait_is_re_evaluated_by_the_first_decision() -> None:
    adapter = RunSwitchAdapter()
    job = _job("waiting-for-operator", {})
    now = NOW
    row = adapter.lifecycle(job, _read_progress(job.result), now)
    assert row.state is State.NEEDS_OPERATOR  # legacy, only ever read
    healed = adapter.decide(job, _read_progress(job.result), Tick(), now).row
    assert healed.state is State.BACKOFF
    assert healed.next_action_at is not None and healed.next_action_at > now
    # with a clock it was already an observation (what its view presents)
    clocked = _job("waiting-for-operator", dict(_SHAPES["clock"]))
    assert (
        adapter.lifecycle(clocked, _read_progress(clocked.result), now).state
        is State.OBSERVING
    )


# ------------------------------------------------------------ live work


def test_a_live_child_is_left_untouched_by_a_tick(tmp_path: Path) -> None:
    """The hardware sweep: a running operation with a running child is not touched."""

    harness = _Harness(tmp_path, RecordingArtifactExecutor(child_transfer=True))
    harness.service.tick()
    child_id = _child_operation_id(harness.view())
    _replace_child(harness.executor, child_id, state="running")
    harness.service.tick()
    before = harness.row()
    for _ in range(3):
        harness.restart().tick()
    assert harness.row() == before
    assert harness.executor.abandoned == []
    assert harness.executor.calls.count("transfer") == 1
    assert _child_operation_id(harness.view()) == child_id


def test_a_legacy_operator_wait_with_a_live_child_is_observed_not_failed(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path, RecordingArtifactExecutor(child_transfer=True))
    harness.service.tick()
    child_id = _child_operation_id(harness.view())
    _replace_child(harness.executor, child_id, state="running")
    harness.make_legacy_operator_wait()
    assert harness.view().state == LifecycleState.NEEDS_OPERATOR

    assert harness.service.tick() is True  # healed
    healed = harness.view()
    assert healed.state not in {LifecycleState.NEEDS_OPERATOR, "failed"}
    assert _child_operation_id(healed) == child_id  # never re-issued
    harness.advance_to_due()
    harness.service.tick()
    assert harness.view().state == "running"
    assert harness.executor.calls.count("transfer") == 1


# ----------------------------------------------------------------- cancel


def test_a_cancel_completes_for_a_legacy_parked_operation(tmp_path: Path) -> None:
    """Before: a ``waiting-for-operator`` operation could not be cancelled at all."""

    harness = _Harness(tmp_path, RecordingArtifactExecutor())
    harness.make_legacy_operator_wait()
    cancelled = harness.service.cancel(
        harness.id, actor="admin", request_key=str(uuid.uuid4()), reason="no longer"
    )
    assert cancelled.state == "cancelled"
    assert harness.view().state == "cancelled"
    assert harness.view().status_reason == "no longer"


def test_a_cancel_completes_when_the_stop_stays_unconfirmed(tmp_path: Path) -> None:
    """Rule 4: a child that cannot be stopped does not hold the cancel forever.

    The child keeps its own lifecycle; the operation ends ``cancelled`` after the
    core's stop budget with a residue note, keeping the child's identity.
    """

    harness = _Harness(tmp_path, RecordingArtifactExecutor(child_transfer=True))
    harness.service.tick()
    child_id = _child_operation_id(harness.view())
    _replace_child(harness.executor, child_id, state="running")
    harness.service.cancel(
        harness.id, actor="admin", request_key=str(uuid.uuid4()), reason="stop it"
    )
    assert harness.view().state == "running"  # being driven, not yet ended
    for _step in range(STOP_BUDGET * 3):
        harness.service.tick()
        if harness.view().state == "cancelled":
            break
        due = _result(harness.view()).observation_due_at
        if due is not None:
            harness.now[0] = max(harness.now[0], due)
    final = harness.view()
    assert final.state == "cancelled"
    assert "cancel-effect-unknown" in (final.status_reason or "")
    assert final.status_reason is not None and "stop it" in final.status_reason
    assert _child_operation_id(final) == child_id  # the evidence is kept
    assert harness.executor.children[child_id].state == "running"  # never aborted


def test_a_cancel_survives_a_restart_in_the_middle(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, RecordingArtifactExecutor(child_transfer=True))
    harness.service.tick()
    child_id = _child_operation_id(harness.view())
    _replace_child(harness.executor, child_id, state="running")
    harness.service.cancel(
        harness.id, actor="admin", request_key=str(uuid.uuid4()), reason="stop it"
    )
    for _step in range(STOP_BUDGET * 3):
        harness.restart().tick()  # a new process every pass
        if harness.view().state == "cancelled":
            break
        due = _result(harness.view()).observation_due_at
        if due is not None:
            harness.now[0] = max(harness.now[0], due)
    assert harness.view().state == "cancelled"


def test_a_cancel_ends_when_the_child_ends_without_waiting_for_its_clock(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path, RecordingArtifactExecutor(child_transfer=True))
    harness.service.tick()
    child_id = _child_operation_id(harness.view())
    _replace_child(harness.executor, child_id, state="running")
    harness.service.cancel(
        harness.id, actor="admin", request_key=str(uuid.uuid4()), reason="stop it"
    )
    harness.service.tick()
    assert harness.view().state == "running"
    _replace_child(harness.executor, child_id, state="cancelled")
    harness.service.tick()  # the clock has not moved
    assert harness.view().state == "cancelled"
    assert "cancel-effect-unknown" not in (harness.view().status_reason or "")


def test_a_cancel_that_is_not_due_is_not_reported_as_progress(tmp_path: Path) -> None:
    """The worker must not spin on a cancel whose next stop is not yet due."""

    harness = _Harness(tmp_path, RecordingArtifactExecutor(child_transfer=True))
    harness.service.tick()
    child_id = _child_operation_id(harness.view())
    _replace_child(harness.executor, child_id, state="running")
    harness.service.cancel(
        harness.id, actor="admin", request_key=str(uuid.uuid4()), reason="stop it"
    )
    harness.service.tick()
    before = harness.row()
    assert harness.service.tick() is False
    assert harness.row() == before


# ------------------------------------------- bookkeeping becomes unknown


def _retrying(view) -> bool:
    result = _result(view)
    return (
        view.state in {"running", LifecycleState.OBSERVING}
        and result.retry_reason is not None
        and result.observation_due_at is not None
        and result.failure_code is None
    )


def test_a_receipt_that_does_not_validate_is_retried_not_failed(tmp_path: Path) -> None:
    """Audit top-10 #2: a mismatched receipt ends no load whose bytes are fine."""

    executor = _ScriptedExecutor()
    executor.faults["verify"] = lambda phase: PhaseExecution(
        result=_phase_result({"verified": False}, phase=phase)
    )
    harness = _Harness(tmp_path, executor)
    for _ in range(8):
        harness.service.tick()
        if executor.calls.count("verify"):
            break
    held = harness.view()
    assert executor.calls.count("verify") == 1
    assert _retrying(held), (held.state, held.status_reason)
    assert _result(held).retry_reason == "run-switch phase receipt is invalid"
    assert _result(held).failed_phase is None

    # restart in the middle of the retry: the same checkpoint, nothing repeated
    harness.restart()
    assert harness.service.tick() is False  # not due: nothing happens
    assert executor.calls.count("verify") == 1
    assert _result(harness.view()).phase_index == _result(held).phase_index

    executor.faults.clear()
    for _ in range(12):
        harness.advance_to_due()
        harness.service.tick()
        if harness.view().state in {"succeeded", "failed"}:
            break
    assert harness.view().state != "failed", harness.view().status_reason
    assert executor.calls.count("verify") == 2  # entered again, once


def test_final_verification_that_cannot_be_observed_is_retried_not_failed(
    tmp_path: Path,
) -> None:
    """Audit top-10 #3: a workload that started is not declared failed because a
    check could not be observed; it is observed again, visibly."""

    service, operation, _start = _parked_start_switch(tmp_path, healthy=True)
    now = [NOW]
    inner = service._phase_executor
    assert inner is not None
    state = {"fault": True, "calls": 0}

    class _Faulty:
        def __getattr__(self, name: str):
            return getattr(inner, name)

        def execute(self, plan, phase, **kwargs):
            if phase.kind == "final_verify" and state["fault"]:
                state["calls"] += 1
                raise RunSwitchOperationConflict(
                    "run-switch.final-verification-unavailable"
                )
            return inner.execute(plan, phase, **kwargs)

    service._phase_executor = _Faulty()
    service._clock = lambda: now[0]

    def drive(limit=8) -> None:
        for _ in range(limit):
            service.tick()
            view = service.get(operation.operation_id)
            if view.state in {"succeeded", "failed"}:
                return
            due = _result(view).observation_due_at
            if due is not None and due > now[0]:
                now[0] = due

    drive(3)
    held = service.get(operation.operation_id)
    assert state["calls"] >= 1
    assert held.state in {"running", LifecycleState.OBSERVING}, held.status_reason
    assert _result(held).failure_code is None
    assert _result(held).retry_reason == "run-switch.final-verification-unavailable"
    assert held.blockers  # a retry is a visible wait, never a silent one

    state["fault"] = False
    drive()
    assert service.get(operation.operation_id).state == "succeeded"


def _retried_codes() -> list[str]:
    document = json.loads(_ALLOWLIST.read_text(encoding="utf-8"))
    family = next(
        item
        for item in document["fail_closed"]
        if item["family"] == "runswitch.phase-retried"
    )
    codes = {site[3] for site in family["sites"]}
    return sorted(code for code in codes if code.startswith("run-switch"))


def test_every_conflict_of_the_phase_path_is_retried_unless_it_is_a_security_edge(
    tmp_path: Path,
) -> None:
    """The class, not the sites: the allowlist's ``runswitch.phase-retried`` codes
    are each raised on the phase path of a real operation and each one leaves it
    retrying at the core's backoff, never failed."""

    codes = _retried_codes()
    assert len(codes) > 20  # the family is real, not an empty promise
    executor = _ScriptedExecutor()
    harness = _Harness(tmp_path, executor)
    seen: list[str] = []
    for code in codes:
        assert not is_security_failure(code), code
        executor.faults["prepare"] = RunSwitchOperationConflict(code)
        executor.faults["transfer"] = RunSwitchOperationConflict(code)
        executor.faults["stop"] = RunSwitchOperationConflict(code)
        executor.faults["verify"] = RunSwitchOperationConflict(code)
        harness.service.tick()
        view = harness.view()
        assert view.state in {"queued", "running", LifecycleState.OBSERVING}, (
            code,
            view.state,
        )
        if _result(view).retry_reason == code:
            seen.append(code)
        harness.advance_to_due()
    assert view.state != "failed"
    assert len(seen) >= len(codes) // 2  # the phase that raised it retried


@pytest.mark.parametrize(
    "code",
    [
        "run-switch.artifact-digest-verification-failed",
        "run-switch.cleanup-nas-eviction-forbidden",
        "run-switch.cleanup-reclaimed-digest-not-planned",
        "run-switch.runtime-image-preparation-digest-mismatch",
    ],
)
def test_a_reviewed_destructive_or_digest_guard_ends_the_operation(
    tmp_path: Path, code: str
) -> None:
    executor = _ScriptedExecutor()
    executor.faults["prepare"] = RunSwitchOperationConflict(code)
    executor.faults["transfer"] = RunSwitchOperationConflict(code)
    harness = _Harness(tmp_path, executor)
    for _ in range(3):
        harness.service.tick()
    view = harness.view()
    assert view.state == "failed"
    assert _result(view).failure_code == code


def test_an_error_nobody_classified_is_retried_by_the_tick_guard(
    tmp_path: Path,
) -> None:
    """Outside the phase handler the tick guard covers the same class, on the
    same clock: a bookkeeping conflict anywhere in an advance retries."""

    harness = _Harness(tmp_path, RecordingArtifactExecutor())
    original = harness.service._advance
    state = {"raise": True}

    def advance(operation_id: str) -> bool:
        if state["raise"]:
            raise RunSwitchOperationConflict(
                "run-switch.container-build-parent-invalid"
            )
        return original(operation_id)

    harness.service._advance = advance  # type: ignore[method-assign]
    harness.service.tick()
    held = harness.view()
    assert _retrying(held), (held.state, held.status_reason)
    assert _result(held).retry_reason == "run-switch.container-build-parent-invalid"
    state["raise"] = False
    harness.advance_to_due()
    harness.service.tick()
    assert harness.view().state != "failed"


def test_the_retry_clock_is_the_cores_and_resets_on_a_new_cause(tmp_path: Path) -> None:
    executor = _ScriptedExecutor()
    harness = _Harness(tmp_path, executor)
    delays: list[float] = []
    executor.faults["transfer"] = RunSwitchOperationConflict("run-switch.a")
    executor.faults["prepare"] = RunSwitchOperationConflict("run-switch.a")
    for _ in range(6):
        harness.service.tick()
        view = harness.view()
        due = _result(view).observation_due_at
        if due is None:
            break
        delays.append((due - harness.now[0]).total_seconds())
        harness.now[0] = due
    assert delays and all(1 <= delay <= 75 for delay in delays), delays
    assert max(delays) <= 75  # one bounded clock, not the old 5 minutes


def test_the_adapter_projects_children_through_the_composite_aggregate(
    tmp_path: Path,
) -> None:
    """The operation follows its children (``aggregate``); it has no effect of
    its own, so observing it is reading them."""

    harness = _Harness(tmp_path, RecordingArtifactExecutor())
    with harness.sessions() as session:
        job = session.get(Job, harness.id)
        assert job is not None
        adapter = RunSwitchAdapter(session)
        row = adapter.adopt(job)
        assert adapter.children(row) == ()
        assert adapter.observe(row).effect is Effect.NONE  # nothing was issued


def _phase_path_classes(family_name: str) -> set[str]:
    """The exception classes the allowlist lists for a family on the phase path."""

    document = json.loads(_ALLOWLIST.read_text(encoding="utf-8"))
    family = next(
        item for item in document["fail_closed"] if item["family"] == family_name
    )
    return {
        site[1]
        for site in family["sites"]
        if site[2].startswith("RecipeLifecyclePhaseExecutor.")
        or site[2] == "_require_profile_runtime_image"
    }


def test_the_allowlist_category_decides_whether_a_phase_conflict_is_definite() -> None:
    """The class, tied to the review: on the phase path, an ``input-validation``
    refusal of the accepted request is a typed definite conflict, and a
    bookkeeping one (``phase-retried``) is not, so the allowlist and the behaviour
    cannot drift apart."""

    from vonk_control import run_switch_operations as module

    for name in _phase_path_classes("runswitch.input-validation"):
        assert getattr(module, name).definite is True, name
    for name in _phase_path_classes("runswitch.phase-retried"):
        if name == "RunSwitchOperationConflict" or name.startswith("_RunSwitch"):
            assert getattr(module, name).definite is False, name


def test_a_changed_accepted_image_ends_the_operation_but_a_stop_gap_is_observed(
    tmp_path: Path,
) -> None:
    executor = _ScriptedExecutor()
    for kind in ("prepare", "transfer", "stop", "verify"):
        executor.faults[kind] = module_conflict("profile.runtime-image-changed: x")
    harness = _Harness(tmp_path, executor)
    for _ in range(3):
        harness.service.tick()
    assert harness.view().state == "failed"

    other = _ScriptedExecutor()
    for kind in ("prepare", "transfer", "stop", "verify"):
        other.faults[kind] = RunSwitchOperationConflict(
            "run-switch.stop-still-unresolved-after-cancellation"
        )
    (tmp_path / "other").mkdir()
    retrying = _Harness(tmp_path / "other", other)
    retrying.service.tick()
    assert _retrying(retrying.view())


def module_conflict(message: str) -> RunSwitchOperationConflict:
    from vonk_control.run_switch_operations import _RunSwitchDefiniteConflict

    return _RunSwitchDefiniteConflict(message)


def test_a_retry_never_lands_before_the_evidence_it_waits_for_can_exist() -> None:
    """The retry clock is jittered per operation, so a wait with a known instant (the
    post-stop inventory must be collected strictly after it) gives the core that
    instant as a lower bound.  Without it, a retry that happened to land exactly on
    the threshold found no evidence and lost a whole backoff step (a flake seen in
    the profile image-replacement test on the Linux lane)."""

    adapter = RunSwitchAdapter()
    threshold = NOW + timedelta(seconds=26)
    for _ in range(200):
        job = _job("running", {})
        progress = RunSwitchOperationResult()
        adapter.retry(
            job,
            progress,
            "run-switch.post-stop-inventory-pending",
            NOW,
            retry_after=threshold,
        )
        due = progress.observation_due_at
        assert due is not None
        assert due >= threshold, (due, job.id)


def test_an_unknown_outcome_of_a_phase_is_retried_even_when_not_flagged_retryable(
    tmp_path: Path,
) -> None:
    """Unconfirmed storage or bookkeeping is observed again, never ended: the
    preparation owner's ``retryable`` flag defaults to false, and an unknown
    outcome must not read that as a definite failure."""

    from vonk_agent_protocol import RuntimeImageCode
    from vonk_agent_protocol.agent_words import ProfileChildPhase
    from vonk_control.runtime_image_preparation import RuntimeImagePreparationUnknown

    executor = _ScriptedExecutor()
    unknown = RuntimeImagePreparationUnknown(
        RuntimeImageCode.RECEIPT_UNAVAILABLE, "the image receipt is unavailable"
    )
    assert unknown.retryable is False
    for kind in (
        ProfileChildPhase.PREPARE,
        ProfileChildPhase.TRANSFER,
        ProfileChildPhase.STOP,
        ProfileChildPhase.VERIFY,
    ):
        executor.faults[kind] = unknown
    harness = _Harness(tmp_path, executor)
    for _ in range(3):
        harness.service.tick()
        harness.advance_to_due()
    view = harness.view()
    assert view.state != LifecycleState.FAILED, (view.state, view.status_reason)
    assert _retrying(view) or view.state in {
        LifecycleState.QUEUED,
        LifecycleState.RUNNING,
    }
    held_phase = _result(view).phase_index
    executor.faults.clear()
    harness.restart()
    for _ in range(12):
        harness.advance_to_due()
        harness.service.tick()
        if _result(harness.view()).phase_index > held_phase:
            break
    assert _result(harness.view()).phase_index > held_phase
    assert harness.view().state != LifecycleState.FAILED
    request = _request(harness.sessions, harness.nodes[0])
    fresh = harness.service.apply(
        RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid.uuid4())),
        actor="admin",
    )
    assert fresh.operation_id != harness.id
    assert fresh.state != LifecycleState.FAILED


def test_an_unknown_outcome_outside_the_phase_handler_is_retried_by_the_tick(
    tmp_path: Path,
) -> None:
    from vonk_control.categorized_faults import OperationInterrupted

    harness = _Harness(tmp_path, RecordingArtifactExecutor())
    original = harness.service._advance
    state = {"raise": True}

    def advance(operation_id: str) -> bool:
        if state["raise"]:
            raise OperationInterrupted("the observation was interrupted")
        return original(operation_id)

    harness.service._advance = advance  # type: ignore[method-assign]
    harness.service.tick()
    held = harness.view()
    assert _retrying(held), (held.state, held.status_reason)
    state["raise"] = False
    harness.advance_to_due()
    harness.service.tick()
    assert harness.view().state != "failed"
