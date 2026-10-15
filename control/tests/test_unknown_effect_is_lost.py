"""An unknown effect is lost, never failed (Class 2 of the principle audit).

A Stop its Spark did not confirm, or a failed Start nobody can stop exactly,
leaves a container that may still run. The run is ``lost``: stoppable, its
ports and memory held, observed until its Spark reports the process gone, or
stopped by a newer exact Stop. ``failed`` lets the ownership rule free those
claims under a running container, and the next load collides with it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from vonk_agent_protocol import RunState
from vonk_agent_protocol.lifecycle_vocabulary import RunAdmissionCode
from vonk_control.distributed_recovery import (
    release_inactive_run_claims_in_session,
    settle_observed_absent_runs_in_session,
)
from vonk_control.models import AgentOperation, RecipeRun, ResourceReservation, RunNode
from vonk_control.recipe_operations import terminal_projection
from vonk_control.recipe_stop_payloads import RecipeStopAuthorityError

from .test_absent_run_release import _report_absent
from .test_recipe_operations import (
    NOW,
    complete_started_recipe,
    installed_recipe,
    setup_services,
    started_recipe,
)
from .test_unused_storage_collection import _agents_succeed


def _claims(sessions, run_id: str) -> int:
    with sessions() as session:
        return len(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id == run_id,
                    ResourceReservation.state == "active",
                )
            ).all()
        )


def _states(sessions, run_id: str) -> tuple[str, dict[str, str]]:
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        ranks = session.scalars(select(RunNode).where(RunNode.run_id == run_id))
        return run.state, {rank.node_id: rank.state for rank in ranks}


def _ownership_rule(sessions) -> None:
    """The recovery tick's claim pass: release what no plan can stop."""
    with sessions.begin() as session:
        release_inactive_run_claims_in_session(session, NOW)
        settle_observed_absent_runs_in_session(session, NOW)


def _children(sessions, job_id: str) -> list[str]:
    with sessions() as session:
        return sorted(
            child.node_id
            for child in session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == job_id)
            )
        )


def _fresh_load(sessions, service, installation_id: str, request_id: str) -> None:
    """A newer load of the same model on the same Sparks is admitted and runs."""
    plan = service.preview_run(installation_id, "next")
    assert plan.allowed, plan.nodes
    start = service.start(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id=request_id
    )
    complete_started_recipe(sessions, service, start.id)
    assert _states(sessions, start.owner_id)[0] == RunState.RUNNING


def _unconfirmed_stop(tmp_path):
    """A two-Spark run whose Stop the second Spark never confirmed."""
    sessions, service, _queue, mapping, build, nodes = setup_services(tmp_path, nodes=2)
    installation_id = installed_recipe(
        service, mapping, build, nodes, request_id="i" * 36
    ).owner_id
    run_id = started_recipe(
        sessions, service, installation_id, nodes, request_id="r" * 36
    ).owner_id
    held = _claims(sessions, run_id)
    plan = service.preview_stop(run_id)
    stop = service.stop(
        run_id, plan_digest=plan.plan_digest, actor="admin", request_id="s" * 36
    )
    service.record_node_result(stop.id, nodes[0], succeeded=True, evidence={})
    # The agent's Stop stayed unconfirmed past its budget: the order ends
    # failed with its effect unknown.
    service.record_node_result(
        stop.id, nodes[1], succeeded=False, evidence={"code": "stop.unconfirmed"}
    )
    assert service.get(stop.id).state == "failed"
    _ownership_rule(sessions)
    return sessions, service, nodes, installation_id, run_id, held


def test_an_unconfirmed_stop_is_lost_with_its_claims_until_a_newer_stop(tmp_path):
    """Wrong implementation (main before this fix): the run ended ``failed``
    and the ownership rule released its ports and memory while the second
    Spark's container may still run, so the next load collided with it."""
    sessions, service, nodes, installation_id, run_id, held = _unconfirmed_stop(
        tmp_path
    )

    state, ranks = _states(sessions, run_id)
    assert state == RunState.LOST
    assert ranks == {nodes[0]: RunState.STOPPED, nodes[1]: RunState.LOST}
    assert held > 0 and _claims(sessions, run_id) == held
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.route_error  # the evidence stays
    # Still observed: nothing settles it while no Spark says it is gone.
    _ownership_rule(sessions)
    assert _states(sessions, run_id)[0] == RunState.LOST
    # A load that does not stop it waits with a typed reason; one that plans
    # its exact Stop is admitted: the lost run never blocks new requests.
    waiting = service.preview_run(installation_id, "next")
    assert RunAdmissionCode.UNRECONCILED_LOST_RANK in {
        blocker.code for node in waiting.nodes for blocker in node.blockers
    }
    replacing = service.preview_run(installation_id, "next", released_run_ids=(run_id,))
    assert replacing.allowed, replacing.nodes

    plan = service.preview_stop(run_id)
    assert plan.allowed, plan.blockers
    stop = service.stop(
        run_id, plan_digest=plan.plan_digest, actor="admin", request_id="t" * 36
    )
    assert _children(sessions, stop.id)  # the exact Stop is issued again
    _agents_succeed(sessions, service, stop.id)

    assert service.get(stop.id).state == "succeeded"
    assert _states(sessions, run_id) == (
        RunState.STOPPED,
        {node: RunState.STOPPED for node in nodes},
    )
    assert _claims(sessions, run_id) == 0
    _fresh_load(sessions, service, installation_id, "n" * 36)


def test_a_lost_run_its_sparks_report_gone_is_stopped_and_released(tmp_path):
    sessions, service, _nodes, installation_id, run_id, _held = _unconfirmed_stop(
        tmp_path
    )

    _report_absent(sessions, run_id)
    _ownership_rule(sessions)

    assert _states(sessions, run_id)[0] == RunState.STOPPED
    assert _claims(sessions, run_id) == 0
    _fresh_load(sessions, service, installation_id, "n" * 36)


def test_a_failed_start_without_exact_cleanup_authority_is_lost(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Wrong implementation: the Start was recorded ``failed`` and its claims
    released, though what it launched was never stopped or observed gone."""
    sessions, service, _queue, mapping, build, nodes = setup_services(tmp_path, nodes=1)
    installation_id = installed_recipe(
        service, mapping, build, nodes, request_id="i" * 36
    ).owner_id
    plan = service.preview_run(installation_id, "qwen")
    start = service.start(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="r" * 36
    )
    held = _claims(sessions, start.owner_id)

    def unbuildable(*_args, **_kwargs):
        raise RecipeStopAuthorityError("the revision row is gone")

    with monkeypatch.context() as patched:
        patched.setattr(terminal_projection, "durable_run_stop_payloads", unbuildable)
        service.record_node_result(
            start.id, nodes[0], succeeded=False, evidence={"code": "start.unconfirmed"}
        )
    _ownership_rule(sessions)

    assert _states(sessions, start.owner_id) == (
        RunState.LOST,
        {nodes[0]: RunState.LOST},
    )
    assert held > 0 and _claims(sessions, start.owner_id) == held

    _report_absent(sessions, start.owner_id)
    _ownership_rule(sessions)
    assert _states(sessions, start.owner_id)[0] == RunState.STOPPED
    assert _claims(sessions, start.owner_id) == 0
    _fresh_load(sessions, service, installation_id, "n" * 36)
