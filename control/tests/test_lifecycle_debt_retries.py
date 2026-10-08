"""Request entry points must retry uncertainty without losing the exact request.

These tests catch a one-shot admission/result consumer and a retry that sleeps
or repeats while the failed transaction is still open. Security refusals must
continue to escape on their first attempt.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import import_module
from types import SimpleNamespace
from typing import Any, cast

import pytest
from vonk_agent_protocol import (
    RecipeStopResult,
    SecurityRefusalError,
    SecurityRefusalReason,
    UnknownOutcomeError,
    WaitReason,
)
from vonk_control import agent_jobs, install_admission, run_admission
from vonk_control.agent_jobs import AgentJobService
from vonk_control.install_admission import InstallAdmissionBusy, InstallAdmissionService
from vonk_control.model_cache import ModelCacheService
from vonk_control.recipe_operations import RecipeOperationService, result_consumption
from vonk_control.run_admission import RunAdmissionBusy, RunAdmissionService


@pytest.mark.parametrize(
    "module,cls,method,error",
    [
        (
            install_admission,
            InstallAdmissionService,
            "accept_install",
            InstallAdmissionBusy("busy"),
        ),
        (run_admission, RunAdmissionService, "accept_run", RunAdmissionBusy("busy")),
    ],
)
def test_direct_acceptance_retries_after_rolling_back(
    module, cls, method, error, monkeypatch
):
    service = object.__new__(cls)
    open_transaction = [False]
    transactions = []
    calls = []
    plan, now = object(), datetime.now(UTC)

    @contextmanager
    def begin():
        assert not open_transaction[0]
        open_transaction[0] = True
        try:
            yield object()
        except BaseException:
            transactions.append("rollback")
            raise
        else:
            transactions.append("commit")
        finally:
            open_transaction[0] = False

    sessions = SimpleNamespace(begin=begin)
    service._sessions = sessions

    def attempts():
        for attempt in range(3):
            assert not open_transaction[0], "retry held the failed transaction"
            yield attempt

    def accept(_session, same_plan, *, actor, now):
        calls.append((same_plan, actor, now))
        if len(calls) < 3:
            raise error
        return "exact-owner"

    monkeypatch.setattr(module, "admission_attempts", attempts, raising=False)
    monkeypatch.setattr(service, f"{method}_in_session", accept)
    if method == "accept_install":
        monkeypatch.setattr(service, "refresh_install_receipts", lambda *a, **k: None)

    assert getattr(service, method)(plan, actor="operator", now=now) == "exact-owner"
    assert calls == [(plan, "operator", now)] * 3
    assert transactions == ["rollback", "rollback", "commit"]


@pytest.mark.parametrize("method", ["succeed", "fail"])
@pytest.mark.parametrize("refusals", [2, 99])
def test_direct_agent_completion_is_bounded_and_preserves_fence(
    method, refusals, monkeypatch
):
    service = object.__new__(AgentJobService)
    error = RunAdmissionBusy("result projection is busy")
    fence = object()
    calls = []

    def finish(same_fence, state, **kwargs):
        calls.append((same_fence, state, kwargs))
        if len(calls) <= refusals:
            raise error

    monkeypatch.setattr(
        agent_jobs, "admission_attempts", lambda: iter(range(3)), raising=False
    )
    monkeypatch.setattr(service, "_finish", finish)
    if refusals > 3:
        with pytest.raises(RunAdmissionBusy) as caught:
            getattr(service, method)(fence, {} if method == "succeed" else "failed")
        assert caught.value is error
    else:
        getattr(service, method)(fence, {} if method == "succeed" else "failed")
    assert len(calls) == 3
    assert all(call == calls[0] for call in calls)
    assert calls[0][0] is fence


def test_direct_agent_completion_never_retries_security(monkeypatch):
    service = object.__new__(AgentJobService)
    calls = []

    def finish(*args, **kwargs):
        calls.append(args)
        raise SecurityRefusalError("revoked", reason=SecurityRefusalReason.STALE_FENCE)

    monkeypatch.setattr(service, "_finish", finish)
    with pytest.raises(SecurityRefusalError):
        service.succeed("exact-fence", RecipeStopResult.model_validate({}))
    assert len(calls) == 1


def test_node_result_retries_the_same_evidence(monkeypatch):
    service = object.__new__(RecipeOperationService)
    calls = []
    evidence = {"image": "exact"}

    def once(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) < 3:
            raise InstallAdmissionBusy("cleanup queue is busy")
        return "projected"

    monkeypatch.setattr(
        result_consumption, "admission_attempts", lambda: iter(range(3))
    )
    monkeypatch.setattr(service, "_record_node_result_once", once, raising=False)
    assert (
        service.record_node_result(
            "operation", "node", succeeded=True, evidence=evidence
        )
        == "projected"
    )
    assert (
        calls
        == [(("operation", "node"), {"succeeded": True, "evidence": evidence})] * 3
    )


def test_cleanup_preview_reobserves_unknown_authority(monkeypatch):
    from vonk_control.run_switch_operations import (
        RunSwitchOperationService,
        stop_planning,
    )

    service = object.__new__(RunSwitchOperationService)
    calls = []

    def once(request, *, actor):
        calls.append((request, actor))
        if len(calls) < 3:
            raise InstallAdmissionBusy("authority is being reconciled")
        return "exact-cleanup"

    monkeypatch.setattr(
        stop_planning,
        "admission_attempts",
        lambda: iter(range(3)),
    )
    monkeypatch.setattr(service, "_preview_cleanup_once", once, raising=False)
    assert service.preview_cleanup("installation", actor="operator") == "exact-cleanup"
    assert calls == [("installation", "operator")] * 3


@pytest.mark.parametrize("refusals", [2, 99])
def test_shutdown_retries_checkpointing_then_releases_lifespan(refusals, monkeypatch):
    import asyncio

    from vonk_control.api import production as api

    calls, pauses = [], []

    def close():
        calls.append(True)
        if len(calls) <= refusals:
            raise RunAdmissionBusy("checkpoint writer is busy")

    async def sleep(seconds):
        pauses.append(seconds)

    monkeypatch.setattr(api.asyncio, "sleep", sleep)
    asyncio.run(
        api._close_model_cache(cast(ModelCacheService, SimpleNamespace(close=close)))
    )
    assert len(calls) == 3
    assert len(pauses) == 2 and all(0 < pause < 1 for pause in pauses)


@pytest.mark.parametrize(
    "method,once",
    [
        ("install", "_install_once"),
        ("start", "_start_once"),
        ("activate_job_run", "_activate_job_run_once"),
    ],
)
def test_recipe_request_retries_raw_admission_contention(method, once, monkeypatch):
    from vonk_control.admission_locking import AdmissionLockBusy

    service = object.__new__(RecipeOperationService)
    calls = []
    plan = object()

    def attempt(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) < 3:
            raise AdmissionLockBusy("node writer is busy")
        return "accepted"

    monkeypatch.setattr(
        import_module(getattr(service, method).__module__),
        "admission_attempts",
        lambda: iter(range(3)),
    )
    monkeypatch.setattr(service, once, attempt)
    assert (
        getattr(service, method)(
            plan, plan_digest="exact", actor="operator", request_id="request"
        )
        == "accepted"
    )
    assert len(calls) == 3 and all(call == calls[0] for call in calls)


def test_unknown_builder_evidence_is_waiting_and_reobserved(monkeypatch):
    from vonk_control.run_switch_operations import RunSwitchOperationService

    service: Any = object.__new__(RunSwitchOperationService)
    calls = []
    selected = SimpleNamespace(recipe_revision_id="revision")

    def preview(*args):
        calls.append(args)
        if len(calls) == 1:
            raise RunAdmissionBusy("builder receipt unavailable")
        return SimpleNamespace(build_id="same-build")

    service._lifecycle = SimpleNamespace(preview_build=preview)
    monkeypatch.setattr(
        service, "_builder_nodes", lambda *args: [SimpleNamespace(node_id="builder")]
    )
    monkeypatch.setattr(
        service, "_builder_admission", lambda *args, **kwargs: (None, True)
    )
    session = SimpleNamespace(get=lambda *args: selected, refresh=lambda *args: None)
    args = (
        session,
        SimpleNamespace(id="revision"),
        None,
        None,
        SimpleNamespace(nodes=[]),
    )
    waiting = service._select_build(*args, now=datetime.now(UTC))
    assert waiting.build is None and waiting.candidate is None
    assert "Waiting for exact builder evidence" in waiting.blockers[0].detail
    recovered = service._select_build(*args, now=datetime.now(UTC))
    assert recovered.candidate is selected
    assert calls == [("revision", "builder")] * 2


def test_accepted_plan_refresh_records_unknown_cause_and_resumes(tmp_path, monkeypatch):
    """Catches swallowing the cause or forgetting the accepted intent on retry."""
    import uuid
    from datetime import timedelta

    from sqlalchemy import select
    from vonk_control.models import Job
    from vonk_control.run_switch_contract import RunSwitchApplyRequest

    from .test_recipe_operations import setup_services
    from .test_run_switch_operations import (
        NOW,
        RecordingArtifactExecutor,
        _request,
        _service,
    )

    sessions, lifecycle, _queue, _mapping, _build, nodes = setup_services(tmp_path)
    clock = [NOW]
    service = _service(
        sessions, NOW, lifecycle, RecordingArtifactExecutor(), phase_executor=None
    )
    service._clock = lambda: clock[0]
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="operator")
    parent = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            request_key=str(uuid.uuid4()),
            plan_digest=plan.plan_digest,
        ),
        actor="operator",
    )
    with sessions() as session:
        original_jobs = set(session.scalars(select(Job.id)))
    service._fail(
        parent.operation_id, "receipt disappeared", replan=True, checkpoint=(0, 0, None)
    )
    with sessions() as session:
        row = session.get(Job, parent.operation_id)
        assert row is not None and isinstance(row.result, dict)
        next_attempt = row.result["observation_due_at"]
        assert isinstance(next_attempt, str)
        clock[0] = datetime.fromisoformat(next_attempt)
    preview = service.preview
    calls = []

    def unknown(*args, **kwargs):
        calls.append((args, kwargs))
        raise RunAdmissionBusy("exact builder receipt temporarily unreadable")

    monkeypatch.setattr(service, "preview", unknown)
    assert service._refresh_blocked_plan(parent.operation_id, clock[0])
    with sessions() as session:
        row = session.get(Job, parent.operation_id)
        assert row is not None and isinstance(row.result, dict)
        assert "exact builder receipt temporarily unreadable" in (
            row.status_reason or ""
        )
        stored_plan = row.payload["plan"]
        assert isinstance(stored_plan, dict)
        assert stored_plan["plan_digest"] == plan.plan_digest
        next_attempt = row.result["observation_due_at"]
        assert isinstance(next_attempt, str)
        due = datetime.fromisoformat(next_attempt)
        assert due > clock[0]
    assert not service._refresh_blocked_plan(parent.operation_id, clock[0])
    assert len(calls) == 1
    monkeypatch.setattr(service, "preview", preview)
    clock[0] = due + timedelta(seconds=1)
    assert service._refresh_blocked_plan(parent.operation_id, clock[0])
    with sessions() as session:
        assert set(session.scalars(select(Job.id))) == original_jobs
        row = session.get(Job, parent.operation_id)
        assert row is not None and isinstance(row.result, dict)
        stored_plan = row.payload["plan"]
        assert isinstance(stored_plan, dict)
        assert stored_plan["plan_digest"] == plan.plan_digest
        assert row.result["force_replan"] is False
        assert row.result["observation_due_at"] is None


@pytest.mark.parametrize(
    "method,once",
    [
        ("install", "_install_once"),
        ("start", "_start_once"),
        ("activate_job_run", "_activate_job_run_once"),
    ],
)
def test_recipe_unknown_after_commit_replays_without_duplicate_effects(
    method, once, tmp_path, monkeypatch
):
    """A lost committed response must not reserve or dispatch the workload twice."""
    from sqlalchemy import select
    from vonk_control.models import (
        AgentNode,
        AgentOperation,
        Job,
        RecipeInstallation,
        RecipeRun,
        ResourceReservation,
    )

    from .test_artifact_jobs import _configure_artifact_recipe
    from .test_recipe_operations import installed_recipe, setup_services

    sessions, service, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path,
        recipe_transform=(
            _configure_artifact_recipe if method == "activate_job_run" else None
        ),
    )
    if method == "install":
        plan = service.preview_install(mapping_id, build_id)
    else:
        installed = installed_recipe(
            service, mapping_id, build_id, nodes, request_id="1" * 36
        )
        plan = service.preview_run(
            installed.owner_id, "image-job" if method == "activate_job_run" else "qwen"
        )

    def effects():
        with sessions() as session:
            return (
                tuple(
                    tuple(sorted(session.scalars(select(model.id))))
                    for model in (
                        Job,
                        RecipeInstallation,
                        RecipeRun,
                        ResourceReservation,
                        AgentOperation,
                    )
                ),
                tuple(
                    session.execute(
                        select(
                            AgentNode.node_id, AgentNode.workload_intent_ordinal
                        ).order_by(AgentNode.node_id)
                    )
                ),
                queue.available,
            )

    original = getattr(service, once)
    calls, committed = [], []

    def lose_response(*args, **kwargs):
        calls.append((args, kwargs))
        result = original(*args, **kwargs)
        if len(calls) == 1:
            committed.append((result, effects()))

            def admission_unavailable(*_args, **_kwargs):
                raise AssertionError("committed replay must adopt before admission")

            monkeypatch.setattr(
                service._install_admission
                if method == "install"
                else service._run_admission,
                "plan_install" if method == "install" else "plan_run",
                admission_unavailable,
            )
            raise UnknownOutcomeError(
                "response lost after committed workload admission",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        return result

    monkeypatch.setattr(
        import_module(getattr(service, method).__module__),
        "admission_attempts",
        lambda: iter(range(3)),
    )
    monkeypatch.setattr(service, once, lose_response)
    result = getattr(service, method)(
        plan, plan_digest=plan.plan_digest, actor="operator", request_id="2" * 36
    )
    assert len(calls) == 2 and calls[0] == calls[1]
    assert result == committed[0][0]
    assert effects() == committed[0][1]


@pytest.mark.parametrize("refusals", [2, 3])
def test_run_switch_cancel_reports_exhausted_contention_then_can_progress(
    monkeypatch, refusals, tmp_path
):
    """A busy boundary cannot look completed or poison the next attempt."""
    from uuid import uuid4

    from vonk_agent_protocol import LifecycleState, RecipeBuildCode
    from vonk_control.run_switch_contract import RunSwitchApplyRequest
    from vonk_control.run_switch_operations import (
        RunSwitchRetryLater,
        cancellation_retry,
    )

    from .test_run_switch_lifecycle import _Harness
    from .test_run_switch_operations import RecordingArtifactExecutor, _request

    harness = _Harness(tmp_path, RecordingArtifactExecutor())
    service = harness.service
    original_once, original_get = service._cancel_once, service.get
    calls = []
    busy = RunSwitchRetryLater(
        RecipeBuildCode.CONSUMER_BUSY, reason=WaitReason.OBSERVATION_UNAVAILABLE
    )
    completed = object()

    def once(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) <= refusals:
            raise busy
        return completed

    monkeypatch.setattr(
        cancellation_retry, "admission_attempts", lambda: iter(range(3))
    )
    monkeypatch.setattr(service, "_cancel_once", once)
    # Reading the receipt used to hide exhausted contention; it must never
    # replace the typed outcome of the attempted boundary.
    monkeypatch.setattr(service, "get", lambda _: pytest.fail("contention hidden"))
    request = {"actor": "admin", "request_key": str(uuid4()), "reason": "detach"}
    if refusals == 3:
        with pytest.raises(RunSwitchRetryLater) as caught:
            service.cancel("operation", **request)
        assert caught.value is busy
    else:
        assert service.cancel("operation", **request) is completed
    assert calls == [(("operation",), request)] * 3
    assert service.cancel("operation", **request) is completed

    monkeypatch.setattr(service, "_cancel_once", original_once)
    monkeypatch.setattr(service, "get", original_get)
    ended = service.cancel(harness.id, **request)
    assert ended.state == LifecycleState.CANCELLED
    fresh = service.apply(
        RunSwitchApplyRequest(
            **_request(harness.sessions, harness.nodes[0]).model_dump(),
            request_key=str(uuid4()),
        ),
        actor="admin",
    )
    assert fresh.operation_id != ended.operation_id
    assert fresh.state in {LifecycleState.QUEUED, LifecycleState.RUNNING}
