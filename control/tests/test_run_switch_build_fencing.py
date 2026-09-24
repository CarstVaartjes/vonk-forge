"""Build admission and observation stay bound to the accepted Run/Switch intent."""

import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from vonk_control.models import (
    AgentNode,
    Job,
    NodeInventorySnapshot,
    RecipeInstallation,
)
from vonk_control.run_switch_contract import (
    RunSwitchApplyRequest,
    RunSwitchCleanupApplyRequest,
)

from .test_build_cancellation_recovery import _evidence
from .test_profile_build_memory import _accepted_build_profile
from .test_run_switch_operations import _request


def _direct_parent(tmp_path, engine):
    sessions, _profiles, planner, node_id, _application, selected = (
        _accepted_build_profile(tmp_path, engine, accept_profile=False)
    )
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.host_memory_free_bytes = snapshot.gpu_memory_free_bytes = 10_000
    request = RunSwitchApplyRequest(
        **_request(sessions, node_id).model_dump(), request_key=str(uuid4())
    )
    parent = planner.apply(request, actor="admin")
    return sessions, planner, parent, request, selected


def _snapshot(sessions, operation_id):
    with sessions() as session:
        parent = session.get(Job, operation_id)
        assert parent is not None
        return deepcopy(
            (parent.state, parent.payload, parent.result, parent.status_reason)
        )


def _change_intent(sessions, planner, parent, change):
    if change == "cancel":
        planner.cancel(
            parent.operation_id,
            actor="admin",
            request_key=str(uuid4()),
            reason="Stop this preparation",
        )
    else:
        with sessions() as session:
            installation_id = session.scalar(select(RecipeInstallation.id))
            assert installation_id is not None
        planner.apply_cleanup(
            RunSwitchCleanupApplyRequest(
                installation_id=installation_id, request_key=str(uuid4())
            ),
            actor="admin",
        )
    return _snapshot(sessions, parent.operation_id)


@pytest.mark.parametrize("change", ["cancel", "supersede"])
@pytest.mark.parametrize("boundary", ["dispatch", "checkpoint", "returned"])
def test_build_dispatch_and_checkpoint_respect_current_parent_intent(
    tmp_path, postgres_engine, monkeypatch, change, boundary
):
    sessions, planner, parent, _request, selected = _direct_parent(
        tmp_path, postgres_engine
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    original = lifecycle.build
    changed = []

    def replace_intent():
        changed.append(_change_intent(sessions, planner, parent, change))

    def paused_build(*args, **kwargs):
        if boundary == "dispatch":
            replace_intent()
        result = original(*args, **kwargs)
        if boundary == "checkpoint":
            replace_intent()
        return result

    monkeypatch.setattr(lifecycle, "build", paused_build)
    if boundary == "returned":
        executor = planner._phase_executor
        assert executor is not None
        execute = executor.execute

        def paused_execute(*args, **kwargs):
            result = execute(*args, **kwargs)
            replace_intent()
            return result

        monkeypatch.setattr(executor, "execute", paused_execute)
    planner._advance(parent.operation_id)
    assert len(changed) == 1
    assert _snapshot(sessions, parent.operation_id) == changed[0]
    with sessions() as session:
        children = tuple(
            session.scalars(
                select(Job).where(
                    Job.kind == "recipe.build.v1",
                    Job.payload["owner_id"].as_string() == selected.build_id,
                )
            )
        )
        assert len(children) == (0 if boundary == "dispatch" else 1)
    # A normal subsequent reconciliation settles the original intent without
    # reviving it or dispatching another build.
    planner._advance(parent.operation_id)
    assert planner.get(parent.operation_id).state == "cancelled"


@pytest.mark.parametrize("change", ["cancel", "supersede"])
@pytest.mark.parametrize("state", ["running", "failed", "succeeded"])
def test_late_build_observation_cannot_overwrite_changed_parent(
    tmp_path, postgres_engine, monkeypatch, change, state
):
    sessions, planner, parent, _request, selected = _direct_parent(
        tmp_path, postgres_engine
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    assert planner._advance(parent.operation_id)
    with sessions() as session:
        child_id = session.scalar(select(Job.id).where(Job.kind == "recipe.build.v1"))
        assert child_id is not None
    if state != "running":
        lifecycle.record_node_result(
            child_id,
            selected.builder_node_id,
            succeeded=state == "succeeded",
            evidence=_evidence(selected)
            if state == "succeeded"
            else {"reason": "source rejected"},
        )
    assert lifecycle.get(child_id).state == state
    original = planner._get_child_operation
    changed = []

    def late_observation(operation_id):
        result = original(operation_id)
        changed.append(_change_intent(sessions, planner, parent, change))
        return result

    monkeypatch.setattr(planner, "_get_child_operation", late_observation)
    planner._advance(parent.operation_id)
    assert len(changed) == 1
    assert _snapshot(sessions, parent.operation_id) == changed[0]
    monkeypatch.setattr(planner, "_get_child_operation", original)
    planner._advance(parent.operation_id)
    # The parent relinquishes demand immediately; the lifecycle owner keeps
    # shared work running or reconciles the last consumer's exact cleanup.
    assert planner.get(parent.operation_id).state == "cancelled"


def test_build_admission_holds_parent_and_scope_until_child_commit(
    tmp_path, postgres_engine, monkeypatch
):
    sessions, planner, parent, _request, selected = _direct_parent(
        tmp_path, postgres_engine
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    original = lifecycle._start_build_in_session
    reached = threading.Event()
    resume = threading.Event()

    def paused_admission(*args, **kwargs):
        reached.set()
        assert resume.wait(10), "build admission barrier was not released"
        return original(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "_start_build_in_session", paused_admission)
    with ThreadPoolExecutor(max_workers=1) as pool:
        advancing = pool.submit(planner._advance, parent.operation_id)
        try:
            assert reached.wait(10), "build admission did not reach the barrier"
            for model, identity in (
                (Job, parent.operation_id),
                (AgentNode, selected.builder_node_id),
            ):
                with sessions.begin() as session:
                    with pytest.raises(DBAPIError) as caught:
                        session.get(model, identity, with_for_update={"nowait": True})
                    assert getattr(caught.value.orig, "sqlstate", None) == "55P03"
        finally:
            resume.set()
        assert advancing.result(timeout=10)
    with sessions() as session:
        assert (
            len(
                tuple(
                    session.scalars(select(Job.id).where(Job.kind == "recipe.build.v1"))
                )
            )
            == 1
        )
