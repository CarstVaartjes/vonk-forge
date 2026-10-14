"""Only a real security edge fails closed; bookkeeping drift waits or re-plans.

A profile Stop is how a load clears a Spark.  Its accepted authority is the
Controller's own records, so an owner that is briefly unavailable is a wait and
a parent that changed is a conflict the run switch re-plans; neither may end the
load as a terminal security refusal.  A grant that names another node's plan is
still refused closed.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    ContainerRuntimeAction,
    InvalidRequestError,
    InvalidRequestReason,
    LifecycleState,
    ModelCacheCode,
    SecurityRefusalError,
    canonical_message,
)
from vonk_control.failure_classification import (
    error_code,
    is_definite,
    is_security_failure,
)
from vonk_control.host_runtime_plan_authority import derive_runtime_plan_binding
from vonk_control.model_cache.catalog_helpers import _validate_source
from vonk_control.models import (
    AgentOperation,
    FleetProfileApplication,
    Job,
    NodeInventorySnapshot,
)
from vonk_control.run_switch_contract import RunSwitchOperationResult

from . import test_recipe_operations as recipe_fixtures
from .test_profile_port_claims import _load, _ready_profile
from .test_recipe_operation_bookkeeping import _running_recipe

_ROOT = "recipe.run-switch.v2"
_STOP = "recipe.stop"
# The load continues toward its replacement Start with a fresh preflight.
_NEXT = "runtime.preflight.v1"


def _advance(monkeypatch, seconds: int) -> None:
    """Move the shared fixture clock that every service in the profile reads."""

    monkeypatch.setattr(
        recipe_fixtures,
        "NOW",
        recipe_fixtures.NOW + timedelta(seconds=seconds),
    )


def _drive(profiles, planner, sessions, monkeypatch, kind: str) -> str:
    """Tick the profile with the clock moving until a new ``kind`` job exists."""

    with sessions() as session:
        before = set(session.scalars(select(Job.id).where(Job.kind == kind)))
    for _ in range(40):
        planner.tick()
        profiles.tick()
        with sessions() as session:
            assert not session.scalars(
                select(Job.id).where(
                    Job.kind == _ROOT, Job.state == LifecycleState.FAILED.value
                )
            ).first(), "a drifted Stop must never end the load"
            job_id = session.scalar(
                select(Job.id).where(Job.kind == kind, Job.id.not_in(before or {""}))
            )
        if job_id is not None:
            return job_id
        _advance(monkeypatch, 10)
    with sessions() as session:
        jobs = [
            (row.kind, row.state, row.status_reason)
            for row in session.scalars(select(Job).order_by(Job.created_at))
        ]
    raise AssertionError(f"no {kind} job was admitted: {jobs}")


def _inventory_reported_again(sessions, monkeypatch) -> None:
    """The Sparks report inventory again once the old run has stopped."""

    _advance(monkeypatch, 60)
    with sessions.begin() as session:
        for snapshot in session.scalars(select(NodeInventorySnapshot)):
            snapshot.observed_at = recipe_fixtures.NOW
    _advance(monkeypatch, 1)


def _children_of(sessions, stop_id: str) -> list[str]:
    with sessions() as session:
        return list(
            session.scalars(
                select(AgentOperation.id).where(AgentOperation.parent_job_id == stop_id)
            )
        )


def test_profile_stop_waits_for_a_briefly_unavailable_owner_then_completes(
    tmp_path, postgres_engine, monkeypatch
):
    sessions, profiles, planner, profile, api, headers, nodes, installation = (
        _ready_profile(tmp_path, postgres_engine)
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    recipe_fixtures.started_recipe(
        sessions,
        lifecycle,
        installation,
        nodes,
        request_id=str(uuid4()),
        alias="previous",
    )
    application_id, _ = _load(profile, api, headers)

    def lost_withdrawal(_run_id: str) -> None:
        raise OSError("route withdrawal response lost")

    # The Stop is accepted, then deferred to its continuation.
    lifecycle._route_withdrawer = lost_withdrawal
    stop_id = _drive(profiles, planner, sessions, monkeypatch, _STOP)

    # The profile owner cannot be read for one pass.
    unavailable = {application_id}
    read = Session.get

    def read_owner(self, entity, ident, *args, **kwargs):
        if entity is FleetProfileApplication and ident in unavailable:
            return None
        return read(self, entity, ident, *args, **kwargs)

    monkeypatch.setattr(Session, "get", read_owner)
    lifecycle._route_withdrawer = lambda _run_id: None
    _advance(monkeypatch, 6)
    lifecycle.reconcile_pending_service_stops()
    with sessions() as session:
        waiting = session.get(Job, stop_id)
        assert waiting is not None and waiting.state == LifecycleState.RUNNING.value
    assert _children_of(sessions, stop_id) == []

    # The owner is back: the same accepted Stop is dispatched and completes.
    unavailable.clear()
    _advance(monkeypatch, 6)
    lifecycle.reconcile_pending_service_stops()
    assert _children_of(sessions, stop_id)
    lifecycle.record_node_result(stop_id, nodes[0], succeeded=True, evidence={})
    _inventory_reported_again(sessions, monkeypatch)
    with sessions() as session:
        stopped = session.get(Job, stop_id)
        assert stopped is not None
        assert stopped.state == LifecycleState.SUCCEEDED.value
    # The load carries on toward its replacement Start.
    assert _drive(profiles, planner, sessions, monkeypatch, _NEXT)


def test_profile_stop_whose_parent_changed_replans_and_completes(
    tmp_path, postgres_engine, monkeypatch
):
    sessions, profiles, planner, profile, api, headers, nodes, installation = (
        _ready_profile(tmp_path, postgres_engine)
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    recipe_fixtures.started_recipe(
        sessions,
        lifecycle,
        installation,
        nodes,
        request_id=str(uuid4()),
        alias="previous",
    )
    _load(profile, api, headers)
    accept = lifecycle.stop
    changed: list[str] = []

    def stop_after_parent_changed(*args, **kwargs):
        # A concurrent writer changes the Run/Switch parent just before its
        # first Stop child is accepted.
        if not changed:
            with sessions.begin() as session:
                root = session.scalar(select(Job).where(Job.kind == _ROOT))
                assert root is not None
                root.payload_digest = "0" * 64
                changed.append(root.id)
        return accept(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "stop", stop_after_parent_changed)
    stop_id = _drive(profiles, planner, sessions, monkeypatch, _STOP)
    assert changed
    with sessions() as session:
        root = session.get(Job, changed[0])
        assert root is not None and root.state != LifecycleState.FAILED.value
        progress = RunSwitchOperationResult.model_validate_json(
            canonical_message(root.result), strict=True
        )
        # Re-planned under a fresh child request generation.
        assert progress.phase_retry_generation
        assert root.payload_digest != "0" * 64
    assert _children_of(sessions, stop_id)
    lifecycle.record_node_result(stop_id, nodes[0], succeeded=True, evidence={})
    _inventory_reported_again(sessions, monkeypatch)
    assert _drive(profiles, planner, sessions, monkeypatch, _NEXT)


def test_cache_resolution_shape_is_an_invalid_request_not_a_refusal() -> None:
    with pytest.raises(InvalidRequestError) as raised:
        _validate_source("ftp://models.example/weights.safetensors")
    error = raised.value
    assert not isinstance(error, SecurityRefusalError)
    assert error.typed_reason is InvalidRequestReason.MALFORMED
    assert error_code(error) == ModelCacheCode.SOURCE_INVALID
    assert not is_security_failure(error_code(error))


def test_grant_for_another_nodes_plan_is_still_refused_closed(tmp_path) -> None:
    sessions, _service, _queue, _installation, started, nodes = _running_recipe(
        tmp_path, nodes=2
    )
    with sessions() as session:
        parent = session.get(Job, started.id)
        child = session.scalars(
            select(AgentOperation).where(AgentOperation.parent_job_id == started.id)
        ).first()
        assert parent is not None and child is not None
        own = derive_runtime_plan_binding(
            session,
            parent=parent,
            operation=child,
            node_id=child.node_id,
            action=ContainerRuntimeAction.START,
            cancellation_requested=False,
            now=recipe_fixtures.NOW,
        )
        assert own.runtime_run_id == started.owner_id
        other = next(node for node in nodes if node != child.node_id)
        with pytest.raises(SecurityRefusalError) as raised:
            derive_runtime_plan_binding(
                session,
                parent=parent,
                operation=child,
                node_id=other,
                action=ContainerRuntimeAction.START,
                cancellation_requested=False,
                now=recipe_fixtures.NOW,
            )
    assert is_security_failure(error_code(raised.value))
    assert is_definite(raised.value)
