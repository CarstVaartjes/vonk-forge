"""Faults end their own attempt; only exact observed effects release capacity."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_agent_protocol import LifecycleState, ReservationState, SecurityRefusalError
from vonk_control.agent_operation_facts import SUPERSEDED_CANCELLATION_SECONDS
from vonk_control.models import (
    AgentOperation,
    Job,
    RecipeRun,
    ResourceReservation,
    RunNode,
)

from .test_recipe_operations import (
    NOW,
    _required,
    installed_recipe,
    setup_services,
    started_recipe,
)


@pytest.mark.parametrize(
    "boundary", ["refresh_install_receipts", "accept_install_in_session"]
)
@pytest.mark.parametrize("persistent", [False, True])
def test_install_reobserves_fault_and_fresh_request_commits(
    tmp_path: Path, monkeypatch, boundary: str, persistent: bool
) -> None:
    """Catches wrappers that turn a receipt/SQL observation into invalid input."""
    sessions, service, _queue, mapping, build, _nodes = setup_services(tmp_path)
    plan = service.preview_install(mapping, build)
    original = getattr(service._install_admission, boundary)
    calls = 0

    def damaged(*args, **kwargs):
        nonlocal calls
        calls += 1
        # Inject after acceptance as well: rollback must remove its effects.
        result = original(*args, **kwargs)
        if persistent or calls == 1:
            raise ValueError("unreadable local admission observation")
        return result

    monkeypatch.setattr(service._install_admission, boundary, damaged)
    first_key = str(uuid4())
    if persistent:
        with pytest.raises(Exception) as _ending:
            service.install(
                plan, plan_digest=plan.plan_digest, actor="admin", request_id=first_key
            )
        assert 1 < calls <= 3
        with sessions() as session:
            assert (
                session.scalar(select(Job.id).where(Job.request_id == first_key))
                is None
            )
        monkeypatch.setattr(service._install_admission, boundary, original)
        fresh = service.install(
            plan, plan_digest=plan.plan_digest, actor="admin", request_id=str(uuid4())
        )
    else:
        fresh = service.install(
            plan, plan_digest=plan.plan_digest, actor="admin", request_id=first_key
        )
        assert calls == 2
    assert fresh.state == LifecycleState.QUEUED
    with sessions() as session:
        assert session.get(Job, fresh.id) is not None


def test_start_acceptance_rolls_back_unknown_and_then_reobserves(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches a partially accepted run becoming a permanent invalid request."""
    sessions, service, _queue, mapping, build, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        service, mapping, build, nodes, request_id=str(uuid4())
    )
    plan = service.preview_run(installation.owner_id, "recovered")
    original = service._run_admission.accept_run_in_session
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        if calls == 1:
            raise ValueError("lost acceptance observation")
        return result

    monkeypatch.setattr(service._run_admission, "accept_run_in_session", interrupted)
    operation = service.start(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id=str(uuid4())
    )
    assert calls == 2
    with sessions() as session:
        assert tuple(session.scalars(select(RecipeRun.id))) == (operation.owner_id,)
        assert (
            session.scalar(
                select(AgentOperation.id).where(
                    AgentOperation.parent_job_id == operation.id
                )
            )
            is not None
        )


def _pending_stop(tmp_path: Path):
    def unavailable(_run):
        raise OSError("route supervisor unavailable")

    sessions, service, _queue, mapping, build, nodes = setup_services(
        tmp_path, route_withdrawer=unavailable
    )
    installation = installed_recipe(
        service, mapping, build, nodes, request_id=str(uuid4())
    )
    run = started_recipe(
        sessions, service, installation.owner_id, nodes, request_id=str(uuid4())
    )
    plan = service.preview_stop(run.owner_id)
    stop = service.stop(
        run.owner_id,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id=str(uuid4()),
    )
    return sessions, service, installation, run, stop, nodes


@pytest.mark.parametrize("denied", [False, True])
def test_pending_stop_ends_without_claiming_capacity_and_fresh_stop_is_admitted(
    tmp_path: Path, monkeypatch, denied: bool
) -> None:
    """Catches indefinite accepted-stop retries and denial swallowed as a wait."""
    sessions, service, _installation, run, stop, nodes = _pending_stop(tmp_path)
    if denied:

        def denied_stop(*_args, **_kwargs):
            raise SecurityRefusalError("authority denied")

        original = service.stop
        monkeypatch.setattr(service, "stop", denied_stop)
        service._clock = lambda: NOW + timedelta(seconds=10)
    else:
        service._clock = lambda: NOW + timedelta(seconds=10)
        service.reconcile_pending_service_stops()
        assert service.get(stop.id).state == LifecycleState.RUNNING
        service._clock = lambda: (
            NOW + timedelta(seconds=SUPERSEDED_CANCELLATION_SECONDS + 1)
        )
    service.reconcile_pending_service_stops()
    assert service.get(stop.id).state == LifecycleState.FAILED
    with sessions() as session:
        assert _required(session.get(RecipeRun, run.owner_id)).stopped_at is None
        assert (
            session.scalar(
                select(ResourceReservation.id).where(
                    ResourceReservation.owner_id == run.owner_id,
                    ResourceReservation.state == ReservationState.ACTIVE,
                )
            )
            is not None
        )
    if denied:
        monkeypatch.setattr(service, "stop", original)
    service._route_withdrawer = lambda _run: None
    plan = service.preview_stop(run.owner_id)
    fresh = service.stop(
        run.owner_id,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id=str(uuid4()),
    )
    assert fresh.id != stop.id
    for node in nodes:
        service.record_node_result(fresh.id, node, succeeded=True, evidence={})
    assert service.get(fresh.id).state == LifecycleState.SUCCEEDED


def test_cancellation_receipt_survives_missing_rank_and_releases_only_that_rank(
    tmp_path: Path,
) -> None:
    """Catches local scope disappearance rolling back an authenticated Stop receipt."""
    sessions, service, _queue, mapping, build, nodes = setup_services(tmp_path, nodes=2)
    installation = installed_recipe(
        service, mapping, build, nodes, request_id=str(uuid4())
    )
    plan = service.preview_run(installation.owner_id, "cancelled")
    run = service.start(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id=str(uuid4())
    )
    service.cancel(
        run.id, actor="admin", request_id=str(uuid4()), reason="cancel exact runtime"
    )
    with sessions.begin() as session:
        rank = _required(
            session.scalar(
                select(RunNode).where(
                    RunNode.run_id == run.owner_id, RunNode.node_id == nodes[0]
                )
            )
        )
        session.delete(rank)
        session.flush()
        child = _required(
            session.scalar(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == run.id,
                    AgentOperation.node_id == nodes[0],
                )
            )
        )
        service.consume_agent_result(
            session,
            child,
            None,
            SimpleNamespace(state=LifecycleState.CANCELLED, result={}),
        )
        session.flush()
        assert (
            session.scalar(
                select(ResourceReservation.id).where(
                    ResourceReservation.owner_id == run.owner_id,
                    ResourceReservation.node_id == nodes[0],
                    ResourceReservation.state == ReservationState.ACTIVE,
                )
            )
            is None
        )
        assert (
            session.scalar(
                select(ResourceReservation.id).where(
                    ResourceReservation.owner_id == run.owner_id,
                    ResourceReservation.node_id == nodes[1],
                    ResourceReservation.state == ReservationState.ACTIVE,
                )
            )
            is not None
        )
    # A new exact Stop is admitted without the missing projection vetoing it.
    fresh = service.preview_stop(run.owner_id)
    admitted = service.stop(
        run.owner_id,
        plan_digest=fresh.plan_digest,
        actor="admin",
        request_id=str(uuid4()),
    )
    assert admitted.id != run.id


def test_retirement_owner_loss_retains_capacity_until_exact_cleanup_receipt(
    tmp_path: Path,
) -> None:
    """Catches owner deletion being treated as a host Stop and free memory."""
    from vonk_control.models import AgentOperationAttempt

    sessions, service, _queue, mapping, build, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        service, mapping, build, nodes, request_id=str(uuid4())
    )
    run = started_recipe(
        sessions, service, installation.owner_id, nodes, request_id=str(uuid4())
    )
    with sessions() as session:
        original = _required(session.get(Job, run.id))
        ordinal = original.payload["workload_intent_ordinal"]
        assert isinstance(ordinal, int)
    request = str(uuid4())
    _reason, completed, advanced = service._retirement_cleanup(
        original, request, "recipe.stop", "run", run.owner_id, ordinal
    )
    assert advanced and not completed
    with sessions.begin() as session:
        for node in session.scalars(
            select(RunNode).where(RunNode.run_id == run.owner_id)
        ):
            session.delete(node)
        session.delete(_required(session.get(RecipeRun, run.owner_id)))
    _reason, completed, advanced = service._retirement_cleanup(
        original, request, "recipe.stop", "run", run.owner_id, ordinal
    )
    assert not completed and not advanced
    with sessions.begin() as session:
        assert (
            session.scalar(
                select(ResourceReservation.id).where(
                    ResourceReservation.owner_id == run.owner_id,
                    ResourceReservation.state == ReservationState.ACTIVE,
                )
            )
            is not None
        )
        cleanup = _required(
            session.scalar(select(Job).where(Job.request_id == request))
        )
        for child in session.scalars(
            select(AgentOperation).where(AgentOperation.parent_job_id == cleanup.id)
        ):
            child.current_attempt = 1
            child.state = LifecycleState.SUCCEEDED
            session.add(
                AgentOperationAttempt(
                    operation_id=child.id,
                    attempt=1,
                    fence=str(uuid4()),
                    lease_deadline=NOW + timedelta(minutes=1),
                    agent_certificate_serial="serial-0",
                    state=LifecycleState.SUCCEEDED,
                    result={},
                )
            )
        session.flush()
        # Deliver a late authenticated receipt after the owner row is gone;
        # foreground polling/retirement is no longer necessary to free it.
        for child in session.scalars(
            select(AgentOperation).where(AgentOperation.parent_job_id == cleanup.id)
        ):
            service.consume_agent_result(
                session,
                child,
                None,
                SimpleNamespace(state=LifecycleState.SUCCEEDED, result={}),
            )
        session.flush()
        assert (
            session.scalar(
                select(ResourceReservation.id).where(
                    ResourceReservation.owner_id == run.owner_id,
                    ResourceReservation.state == ReservationState.ACTIVE,
                )
            )
            is None
        )
    _reason, completed, advanced = service._retirement_cleanup(
        original, request, "recipe.stop", "run", run.owner_id, ordinal
    )
    assert completed and not advanced
    fresh = service.preview_run(installation.owner_id, "after-reconciled-stop")
    assert fresh.allowed
    admitted = service.start(
        fresh, plan_digest=fresh.plan_digest, actor="admin", request_id=str(uuid4())
    )
    assert admitted.owner_id != run.owner_id


@pytest.mark.parametrize(
    "boundary", ["refresh_install_receipts", "accept_install_in_session"]
)
def test_preparation_observes_bounded_fault_then_fresh_install_is_admitted(
    tmp_path: Path, monkeypatch, boundary: str
) -> None:
    """Catches standalone preparation wrapping local evidence as caller invalidity."""
    sessions, service, _queue, mapping, build, _nodes = setup_services(tmp_path)
    plan = service.preview_install(mapping, build)
    original = getattr(service._install_admission, boundary)
    calls = 0

    def unreadable(*args, **kwargs):
        nonlocal calls
        calls += 1
        original(*args, **kwargs)
        raise ValueError("receipt observation is malformed")

    monkeypatch.setattr(service._install_admission, boundary, unreadable)
    with pytest.raises(Exception) as ended:
        service.prepare_installation(plan, actor="admin")
    assert 1 < calls <= 3
    assert "malformed" in str(ended.value)
    with sessions() as session:
        assert session.scalar(select(Job.id)) is None
    monkeypatch.setattr(service._install_admission, boundary, original)
    installation = service.prepare_installation(plan, actor="admin")
    fresh = service.start_installation(
        installation, actor="admin", request_id=str(uuid4())
    )
    assert fresh.owner_id == installation
    assert fresh.state == LifecycleState.QUEUED
