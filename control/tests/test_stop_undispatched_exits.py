"""Every exit of ``stop()`` that does not dispatch the Stop says so by type.

Progress is read from the returned type (``UndispatchedStopView``), never from
the row, so a new non-dispatch exit that returns a plain view would be counted
as progress by ``reconcile_pending_service_stops``.  Each exit is driven here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError
from vonk_agent_protocol import LifecycleState, UnknownOutcomeError
from vonk_control.admission_locking import AdmissionLockBusy
from vonk_control.recipe_operations import stop as stop_module
from vonk_control.recipe_operations.errors import _RouteNotWithdrawn
from vonk_control.recipe_operations.interfaces import (
    RecipeOperationView,
    UndispatchedStopView,
)
from vonk_control.recipe_operations.stop import StopMixin
from vonk_control.recipe_routes import RecipeRouteError
from vonk_control.run_admission import RunAdmissionBusy

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _view() -> RecipeOperationView:
    return RecipeOperationView(
        id="stop-1",
        kind="recipe.stop",
        owner_id="run-1",
        state=LifecycleState.RUNNING.value,
        plan_digest="digest",
        nodes=(),
        lifecycle_result=None,
    )


class _Row:
    status_reason: str | None = None
    state = None


class _Sessions:
    def __call__(self):
        return self

    def begin(self):
        return self

    def __enter__(self):
        return SimpleNamespace(get=lambda *_a, **_k: _Row(), flush=lambda: None)

    def __exit__(self, *_exc):
        return False


def _raiser(error):
    def raise_it(*_args, **_kwargs):
        raise error

    return raise_it


class _Routes:
    def __init__(self, error=None):
        self.error = error

    def withdraw_run(self, *_args, **_kwargs):
        if self.error is not None:
            raise self.error


class _Service(StopMixin):
    """The stop path over stubs: only the exit under test differs."""

    def __init__(self, *, routes, dispatch, accept=None, replay=None):
        self._route_publications = routes
        self._dispatch = dispatch
        self._accept = accept or (lambda *_a, **_k: SimpleNamespace(id="stop-1"))
        self._replay = replay
        self._sessions = _Sessions()
        self._clock = lambda: NOW
        self._agent_jobs = SimpleNamespace(notify_available=lambda: None)
        self._lookups = 0

    def _idempotent(self, *_args, **_kwargs):
        # The entry check sees nothing; the busy replay finds the committed
        # receipt.
        self._lookups += 1
        return self._replay if self._lookups > 1 else None

    def _service_stop_pending(self, *_args):
        return False

    def _stop_logical_job_run(self, *_args, **_kwargs):
        return None

    def _accept_service_stop(self, *args, **kwargs):
        return self._accept(*args, **kwargs)

    def _check_service_stop(self, *_args):
        return None

    def _dispatch_stop_after_withdrawal(self, *_args, **_kwargs):
        return self._dispatch()

    def _view(self, *_args, **_kwargs):
        return _view()


def _busy_lock_replay() -> _Service:
    return _Service(
        routes=_Routes(),
        dispatch=_raiser(AssertionError("must not dispatch")),
        accept=_raiser(AdmissionLockBusy("node keys are busy")),
        replay=_view(),
    )


def _withdrawal_oserror() -> _Service:
    return _Service(
        routes=_Routes(OSError("route publication unavailable")),
        dispatch=_raiser(AssertionError("must not dispatch")),
    )


def _withdrawal_route_error() -> _Service:
    return _Service(
        routes=_Routes(RecipeRouteError("route owner is busy")),
        dispatch=_raiser(AssertionError("must not dispatch")),
    )


def _unsettled_route() -> _Service:
    return _Service(routes=_Routes(), dispatch=_raiser(_RouteNotWithdrawn()))


def _unknown_outcome_on_dispatch() -> _Service:
    return _Service(
        routes=_Routes(), dispatch=_raiser(UnknownOutcomeError("response lost"))
    )


@pytest.mark.parametrize(
    "build",
    [
        _busy_lock_replay,
        _withdrawal_oserror,
        _withdrawal_route_error,
        _unsettled_route,
        _unknown_outcome_on_dispatch,
    ],
)
def test_every_non_dispatch_exit_of_stop_returns_an_undispatched_view(
    build, monkeypatch
):
    monkeypatch.setattr(stop_module, "_bound_workload_intent", lambda _job: 1)
    monkeypatch.setattr(stop_module, "schedule_wait", lambda *_a, **_k: None)
    view = build().stop(
        "run-1", plan_digest="digest", actor="admin", request_id="request-1"
    )
    assert isinstance(view, UndispatchedStopView)


def test_deadlock_before_any_receipt_is_a_retryable_admission_refusal(monkeypatch):
    monkeypatch.setattr(stop_module, "_bound_workload_intent", lambda _job: 1)
    monkeypatch.setattr(stop_module, "schedule_wait", lambda *_a, **_k: None)

    class Deadlock(Exception):
        sqlstate = "40P01"

    deadlock = OperationalError("stmt", {}, Deadlock())
    service = _Service(
        routes=_Routes(),
        dispatch=_raiser(AssertionError("must not dispatch")),
        accept=_raiser(deadlock),
    )
    with pytest.raises(RunAdmissionBusy) as refused:
        service.stop(
            "run-1", plan_digest="digest", actor="admin", request_id="request-1"
        )
    assert refused.value.__cause__ is deadlock


def test_lock_timeout_before_any_receipt_is_not_retried(monkeypatch):
    monkeypatch.setattr(stop_module, "_bound_workload_intent", lambda _job: 1)
    monkeypatch.setattr(stop_module, "schedule_wait", lambda *_a, **_k: None)

    class LockTimeout(Exception):
        sqlstate = "55P03"

    attempts = []

    def accept(*_args, **_kwargs):
        attempts.append(1)
        raise OperationalError("stmt", {}, LockTimeout())

    service = _Service(
        routes=_Routes(), dispatch=_raiser(AssertionError), accept=accept
    )
    with pytest.raises(RunAdmissionBusy):
        service.stop(
            "run-1", plan_digest="digest", actor="admin", request_id="request-1"
        )
    assert len(attempts) == 1
