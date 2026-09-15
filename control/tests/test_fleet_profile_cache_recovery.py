"""Automatic profile recovery for typed Controller cache loss."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from vonk_control.fleet_profiles import build_production_fleet_profile_service
from vonk_control.models import AgentNode, FleetProfileApplication, Job, NodeArtifact
from vonk_control.run_switch_contract import RunSwitchApplyRequest
from vonk_control.run_switch_operations import PhaseExecution, RunSwitchOperationService
from vonk_control.runtime_image_preparation import RuntimeImagePreparationError

from .test_fleet_profile_recovery_current import _failed_profile
from .test_fleet_profiles import NOW, _node_id
from .test_recipe_operations import setup_services
from .test_run_switch_operations import (
    ColdStartPhaseExecutor,
    CompleteArtifactInspector,
    _request,
)


def _typed_cache_failure(
    sessions,
    application_id: str,
    child_id: str,
    code: str,
    *,
    make_due: bool = True,
) -> None:
    with sessions.begin() as session:
        child = session.get(Job, child_id)
        application = session.get(FleetProfileApplication, application_id)
        assert child is not None and child.result is not None
        assert application is not None
        child.result = {
            **child.result,
            "phase": "prepare",
            "subphase": "runtime-image",
            "child_operation_id": None,
            "failed_phase": "prepare",
            "failure_code": code,
            "retryable": False,
        }
        if make_due:
            application.updated_at = datetime(2020, 1, 1, tzinfo=UTC)


def test_typed_cache_loss_queues_one_scope_bound_profile_retry(tmp_path: Path) -> None:
    sessions, lifecycle, service, _profile, _desired, first, child_id, nodes = (
        _failed_profile(tmp_path)
    )
    _typed_cache_failure(
        sessions,
        first.id,
        child_id,
        "runtime_image.cache_missing",
        make_due=False,
    )
    with sessions() as session:
        original_child = session.get(Job, child_id)
        assert original_child is not None
        original_plan = deepcopy(original_child.payload["plan"])
        original_ordinal = original_child.payload["workload_intent_ordinal"]

    assert service.tick() is False
    with sessions.begin() as session:
        application = session.get(FleetProfileApplication, first.id)
        assert application is not None
        application.updated_at = datetime(2020, 1, 1, tzinfo=UTC)
    assert service.tick() is True
    with sessions() as session:
        applications = tuple(
            session.scalars(
                select(FleetProfileApplication).order_by(FleetProfileApplication.created_at)
            )
        )
        children = tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.run-switch.v2"))
        )
        assert len(applications) == 2
        retry = next(row for row in applications if row.id != first.id)
        assert retry.progress["retry_of_application_id"] == first.id
        assert retry.progress["attempt"] == 2
        assert retry.progress["workload_intent_ordinal"] == original_ordinal + 1
        assert tuple(retry.plan["scope"]["node_ids"]) == tuple(nodes)
        original_child = session.get(Job, child_id)
        assert original_child is not None
        assert original_child.payload["plan"] == original_plan
        assert original_child.result is not None
        assert original_child.result["failure_code"] == "runtime_image.cache_missing"
        assert len(children) == 1

    restarted = build_production_fleet_profile_service(
        sessions,
        clock=lifecycle._clock,
        run_switch_operations=service._switch_adapter._run_switch,
    )
    assert restarted.tick() is True
    assert restarted.tick() in {False, True}
    with sessions() as session:
        assert len(tuple(session.scalars(select(FleetProfileApplication)))) == 2
        retried_children = tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.run-switch.v2"))
        )
        assert len(retried_children) == 2
        retry_child = next(child for child in retried_children if child.id != child_id)
        assert all(
            phase["subphase"] != "model-download"
            for phase in retry_child.payload["plan"]["phases"]
        )


def test_cache_recovery_does_not_expand_to_a_new_spark(tmp_path: Path) -> None:
    sessions, _lifecycle, service, _profile, _desired, first, child_id, _nodes = (
        _failed_profile(tmp_path)
    )
    _typed_cache_failure(sessions, first.id, child_id, "runtime_image.cache_missing")
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=_node_id(99),
                state="active",
                protocol_version=1,
                architecture="linux-arm64",
                capabilities=[],
                last_seen_at=NOW,
            )
        )
    assert service.tick() is False
    with sessions() as session:
        assert len(tuple(session.scalars(select(FleetProfileApplication)))) == 1


def test_cache_recovery_refuses_access_and_integrity_failures(tmp_path: Path) -> None:
    for code in (
        "runtime_image.archive_unavailable",
        "runtime_image.archive_mismatch",
        "runtime_image.receipt_invalid",
    ):
        case = tmp_path / code.rsplit(".", 1)[-1]
        case.mkdir()
        sessions, _lifecycle, service, _profile, _desired, first, child_id, _nodes = (
            _failed_profile(case)
        )
        _typed_cache_failure(sessions, first.id, child_id, code)
        assert service.tick() is False
        with sessions() as session:
            assert len(tuple(session.scalars(select(FleetProfileApplication)))) == 1


class _MissingRuntimeImage(ColdStartPhaseExecutor):
    def execute(self, plan, phase, **kwargs) -> PhaseExecution:
        if phase.subphase == "runtime-image":
            raise RuntimeImagePreparationError(
                "runtime_image.cache_missing",
                "OCI archive is not present in Controller storage",
            )
        return super().execute(plan, phase, **kwargs)


class _ColdInspector(CompleteArtifactInspector):
    def inspect(self, *args, **kwargs):
        return replace(
            super().inspect(*args, **kwargs),
            missing_nas_bytes=1024,
            nas_coverage="partial",
        )


def test_run_switch_persists_typed_cache_failure(tmp_path: Path) -> None:
    sessions, lifecycle, _queue, _mapping, _build, nodes = setup_services(tmp_path)
    with sessions.begin() as session:
        session.query(NodeArtifact).delete()
    executor = _MissingRuntimeImage()
    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=_ColdInspector(missing_spark_bytes=1024),
        phase_executor=executor,
        artifact_phase_executor=executor,
        memory_floor_bytes=50,
    )
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key="00000000-0000-4000-8000-000000009001",
        ),
        actor="admin",
    )
    for _ in range(12):
        service._advance(operation.operation_id)
        failed = service.get(operation.operation_id)
        if failed.state == "failed":
            break
    else:
        raise AssertionError("Run/Switch did not reach the runtime-image failure")
    assert failed.result is not None
    assert failed.result.failure_code == "runtime_image.cache_missing"
    assert failed.result.retryable is False
