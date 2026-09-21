"""Retiring an exhausted order never certifies that its physical effect stopped."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_agent_protocol import AgentResult, canonical_message
from vonk_control.agent_jobs import (
    AgentJobService,
    OperatorRetirementRefused,
    StaleAgentAttempt,
)
from vonk_control.auth import TokenCodec
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Job,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
)
from vonk_control.operation_api import durable_operation_services
from vonk_control.recipe_operation_worker import RecipeOperationWorker
from vonk_control.recipe_operations import RecipeOperationService
from vonk_control.recovery_policy import RecoveryPolicy

from .runtime_identity_support import claim_agent
from .test_recipe_operations import (
    NOW,
    _required,
    installed_recipe,
    setup_services,
    start_evidence,
)

CAPABILITIES = (
    "agent.runtime.rust.v1",
    "runtime.vonk.v1",
    "recipe.start",
    "recipe.stop",
    "agent.lifecycle.resume.exact.v1",
)


class QuietRoutes:
    def publish_run(self, run_id):
        raise AssertionError(f"retired run must not be published: {run_id}")

    def maintain(self, *, renew_before_seconds=10):
        return False


def _result(claim, evidence, *, state="succeeded"):
    return AgentResult.model_validate_json(
        canonical_message(
            {
                "schema_version": 1,
                "job_id": claim.job_id,
                "operation_id": claim.operation_id,
                "attempt": claim.attempt,
                "fence": claim.fence,
                "node_id": claim.node_id,
                "deadline": claim.deadline.isoformat(),
                "state": state,
                "result": evidence,
            }
        )
    )


def _parked_start(tmp_path: Path, engine):
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, engine=engine
    )
    installed = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id="retirement-install"
    )
    with sessions.begin() as session:
        node = session.get(AgentNode, nodes[0])
        assert node is not None
        node.capabilities = [*node.capabilities, "recipe.start", "recipe.stop"]
    jobs = AgentJobService(sessions, clock=lambda: NOW)
    jobs.set_result_consumer(lifecycle.consume_agent_result)
    lifecycle._agent_jobs = jobs
    plan = lifecycle.preview_run(installed.owner_id, "retirement")
    started = lifecycle.start(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="retirement-start"
    )
    claim = claim_agent(
        jobs,
        nodes[0],
        "serial-0",
        30,
        capabilities=CAPABILITIES,
    )
    assert claim is not None
    with sessions.begin() as session:
        operation = session.get(AgentOperation, claim.operation_id)
        parent = session.get(Job, started.id)
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == claim.operation_id
            )
        )
        assert operation is not None and parent is not None and attempt is not None
        operation.current_attempt = RecoveryPolicy().max_failures
        operation.state = "waiting-for-operator"
        attempt.attempt = operation.current_attempt
        attempt.lease_deadline = NOW - timedelta(seconds=1)
        parent.state = "waiting-for-operator"
    claim = claim.model_copy(
        update={
            "attempt": RecoveryPolicy().max_failures,
            "deadline": NOW - timedelta(seconds=1),
        }
    )
    projection = durable_operation_services(
        sessions,
        tmp_path / "routes",
        clock=lambda: NOW,
        cursors=TokenCodec(b"k" * 32).cursor_codec(),
    )
    return sessions, lifecycle, jobs, projection, started, claim


def test_retirement_preserves_uncertain_capacity_until_exact_stop(
    tmp_path: Path, postgres_engine
) -> None:
    sessions, lifecycle, jobs, projection, started, claim = _parked_start(
        tmp_path, postgres_engine
    )
    assert projection.retire_job is not None
    projection.retire_job(started.id)
    with sessions() as session:
        run = session.get(RecipeRun, started.owner_id)
        reservation = session.scalar(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == started.owner_id,
            )
        )
        assert run is not None and reservation is not None
        assert run.state == "lost"
        assert reservation.state == "active"
        assert reservation.released_at is None
        old_operation = session.get(AgentOperation, claim.operation_id)
        assert old_operation is not None
        evidence = start_evidence(old_operation.payload)
        ordinal = old_operation.workload_intent_ordinal

    # A delayed start success is historical evidence, never permission to
    # resurrect the retired run or give its unobserved memory to a replacement.
    late = _result(
        claim, {"evidence": evidence, "evidence_digest": evidence["evidence_digest"]}
    )
    with pytest.raises(StaleAgentAttempt):
        jobs.record_result(late)
    assert jobs.record_late_result(late)

    # Reconstruct both worker-facing services after the API transaction has
    # committed: cleanup cannot depend on an in-process notification.
    now = [NOW + timedelta(seconds=6)]
    restarted_jobs = AgentJobService(sessions, clock=lambda: now[0])
    restarted = RecipeOperationService(
        sessions,
        install_admission=lifecycle._install_admission,
        run_admission=lifecycle._run_admission,
        agent_jobs=restarted_jobs,
        clock=lambda: now[0],
    )
    restarted_jobs.set_result_consumer(restarted.consume_agent_result)
    withdrawals = []

    def unavailable_once(run_id):
        withdrawals.append(run_id)
        if len(withdrawals) == 1:
            raise OSError("route publication temporarily unavailable")

    restarted._route_withdrawer = unavailable_once
    worker = RecipeOperationWorker(
        sessions,
        QuietRoutes(),
        clock=lambda: now[0],
        retirement_cleanup=restarted.reconcile_retired_operations,
    )
    assert not worker.tick()
    with sessions() as session:
        assert session.scalar(select(Job.id).where(Job.kind == "recipe.stop")) is None
        retired = session.get(Job, started.id)
        assert retired is not None
        assert "route publication temporarily unavailable" in (
            retired.status_reason or ""
        )
        assert "next reconciliation" in (retired.status_reason or "")
    now[0] += timedelta(seconds=6)
    # Two workers recovering the same durable handoff may race. The normal
    # stop request key and admission transaction still create one child.
    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = tuple(
            workers.map(lambda _: restarted.reconcile_retired_operations(), range(2))
        )
    assert any(outcomes)
    with sessions() as session:
        cleanup = session.scalar(select(Job).where(Job.kind == "recipe.stop"))
        assert cleanup is not None
        assert cleanup.payload["workload_intent_ordinal"] == ordinal
        assert cleanup.payload["owner_id"] == started.owner_id
        original = session.get(AgentOperation, claim.operation_id)
        assert original is not None and original.state == "failed"
        assert _required(session.get(RecipeRun, started.owner_id)).state == "stopping"
        assert (
            session.scalar(
                select(ResourceReservation.state).where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id == started.owner_id,
                )
            )
            == "active"
        )
    # A lost worker response re-enters the same child instead of issuing a
    # second stop or advancing the workload intent.
    now[0] += timedelta(seconds=6)
    assert not worker.tick()
    with sessions() as session:
        assert (
            len(tuple(session.scalars(select(Job).where(Job.kind == "recipe.stop"))))
            == 1
        )
    stop = claim_agent(
        restarted_jobs,
        claim.node_id,
        "serial-0",
        30,
        capabilities=CAPABILITIES,
    )
    assert stop is not None and stop.job_id == cleanup.id
    restarted_jobs.record_result(
        _result(
            stop,
            {
                "status": "failed",
                "error_code": "runtime_observation_unavailable",
                "reason": "runtime temporarily unavailable",
                "failure_kind": "temporary-dependency",
            },
            state="failed",
        )
    )
    with sessions() as session:
        child = _required(session.get(AgentOperation, stop.operation_id))
        assert _required(session.get(Job, cleanup.id)).state == "queued"
        assert child.retry_due_at is not None
        now[0] = child.retry_due_at + timedelta(seconds=1)
    retry = claim_agent(
        restarted_jobs, claim.node_id, "serial-0", 30, capabilities=CAPABILITIES
    )
    assert retry is not None and retry.operation_id == stop.operation_id
    assert retry.attempt == stop.attempt + 1 and retry.fence != stop.fence
    restarted_jobs.record_result(_result(retry, {"stopped": True}))
    now[0] += timedelta(seconds=6)
    worker.tick()
    with sessions() as session:
        run = session.get(RecipeRun, started.owner_id)
        assert run is not None and run.state == "stopped"
        assert (
            session.scalar(
                select(ResourceReservation.state).where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id == started.owner_id,
                )
            )
            == "released"
        )
        retired = session.get(Job, started.id)
        assert retired is not None
        assert _required(retired.result)["recovery"] == "retry creates a new operation"
        assert "exact cleanup confirmed" in (retired.status_reason or "")


def test_retirement_refuses_expired_lease_inside_issued_launch_budget(
    tmp_path: Path, postgres_engine
) -> None:
    sessions, _lifecycle, _jobs, projection, started, claim = _parked_start(
        tmp_path, postgres_engine
    )
    with sessions.begin() as session:
        operation = session.get(AgentOperation, claim.operation_id)
        assert operation is not None
        operation.payload = {
            **operation.payload,
            "start_deadline": (NOW + timedelta(minutes=1)).isoformat(),
        }
    assert projection.retire_job is not None
    with pytest.raises(OperatorRetirementRefused, match="open launch budget"):
        projection.retire_job(started.id)
    with sessions() as session:
        assert _required(session.get(Job, started.id)).state == "waiting-for-operator"
        assert _required(session.get(RecipeRun, started.owner_id)).state == "starting"


def test_retirement_cleanup_never_takes_newer_workload_authority(
    tmp_path: Path, postgres_engine
) -> None:
    sessions, lifecycle, _jobs, projection, started, claim = _parked_start(
        tmp_path, postgres_engine
    )
    assert projection.retire_job is not None
    projection.retire_job(started.id)
    with sessions.begin() as session:
        node = session.get(AgentNode, claim.node_id)
        assert node is not None
        node.workload_intent_ordinal += 1
        newer = node.workload_intent_ordinal
    lifecycle._clock = lambda: NOW + timedelta(seconds=6)
    assert not lifecycle.reconcile_retired_operations()
    with sessions() as session:
        assert session.scalar(select(Job.id).where(Job.kind == "recipe.stop")) is None
        assert (
            _required(session.get(AgentNode, claim.node_id)).workload_intent_ordinal
            == newer
        )
        assert "newer workload intent owns cleanup" in (
            _required(session.get(Job, started.id)).status_reason or ""
        )
        assert (
            session.scalar(
                select(ResourceReservation.state).where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id == started.owner_id,
                )
            )
            == "active"
        )


def test_retired_installation_requires_uninstall_receipt_and_preserves_denial(
    tmp_path: Path, postgres_engine
) -> None:
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    now = [NOW]
    capabilities = (*CAPABILITIES, "recipe.install", "recipe.uninstall")
    with sessions.begin() as session:
        node = _required(session.get(AgentNode, nodes[0]))
        node.capabilities = sorted(set(node.capabilities) | set(capabilities))
    jobs = AgentJobService(sessions, clock=lambda: now[0])
    lifecycle._clock = lambda: now[0]
    lifecycle._agent_jobs = jobs
    jobs.set_result_consumer(lifecycle.consume_agent_result)
    plan = lifecycle.preview_install(mapping_id, build_id)
    install = lifecycle.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="retired-install"
    )
    claim = claim_agent(jobs, nodes[0], "serial-0", 30, capabilities=capabilities)
    assert claim is not None
    with sessions.begin() as session:
        child = _required(session.get(AgentOperation, claim.operation_id))
        child.state = "waiting-for-operator"
        child.current_attempt = RecoveryPolicy().max_failures
        attempt = _required(
            session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == child.id,
                )
            )
        )
        attempt.attempt = child.current_attempt
        attempt.lease_deadline = NOW - timedelta(seconds=1)
        _required(session.get(Job, install.id)).state = "waiting-for-operator"
    projection = durable_operation_services(
        sessions,
        tmp_path / "routes",
        clock=lambda: now[0],
        cursors=TokenCodec(b"k" * 32).cursor_codec(),
    )
    assert projection.retire_job is not None
    projection.retire_job(install.id)
    now[0] += timedelta(seconds=6)
    assert lifecycle.reconcile_retired_operations()
    cleanup = claim_agent(jobs, nodes[0], "serial-0", 30, capabilities=capabilities)
    assert cleanup is not None and cleanup.operation == "recipe.uninstall"
    jobs.record_result(
        _result(
            cleanup,
            {
                "status": "failed",
                "error_code": "authority_revoked",
                "reason": "installation cleanup access denied",
                "failure_kind": "invalid-authority",
            },
            state="failed",
        )
    )
    now[0] += timedelta(seconds=6)
    assert not lifecycle.reconcile_retired_operations()
    with sessions() as session:
        assert (
            _required(session.get(RecipeInstallation, install.owner_id)).state
            != "uninstalled"
        )
        child = _required(session.get(AgentOperation, cleanup.operation_id))
        assert child.state == "failed" and child.retry_due_at is None
        assert (
            session.scalar(
                select(ResourceReservation.state).where(
                    ResourceReservation.owner_kind == "installation",
                    ResourceReservation.owner_id == install.owner_id,
                )
            )
            == "active"
        )
        retired = _required(session.get(Job, install.id))
        assert "inspect/correct the blocker" in (retired.status_reason or "")
