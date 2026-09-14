"""The production profile coordinator recovers a separately committed child."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import (
    RunSwitchFleetProfileAdapter,
    build_production_fleet_profile_service,
)
from vonk_control.models import (
    AgentOperation,
    CatalogDocumentRevision,
    FleetProfileApplication,
    Job,
    RecipeInstallation,
)
from vonk_control.run_switch_operations import RunSwitchOperationService

from .test_fleet_profiles import _uuid
from .test_recipe_operations import installed_recipe, setup_services
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
    _child_operation_id,
)


@pytest.mark.parametrize(
    "child_disposition", ["running", "completed", "wrong-owner", "wrong-scope"]
)
def test_postgres_profile_adopts_child_committed_before_parent_checkpoint(
    tmp_path: Path, postgres_engine, monkeypatch, child_disposition: str
) -> None:
    """A crash after child commit cannot replan that child's bound request."""

    engine = create_engine(
        postgres_engine.url.render_as_string(hide_password=False),
        connect_args={"options": "-c lock_timeout=3000"},
    )
    try:
        sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
            tmp_path, engine=engine
        )
        with sessions() as session:
            revision = session.scalar(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.state == "active",
                )
            )
        assert revision is not None

        def run_switch_service():
            return RunSwitchOperationService(
                sessions,
                lifecycle=lifecycle,
                clock=lifecycle._clock,
                artifacts=CompleteArtifactInspector(),
                artifact_phase_executor=RecordingArtifactExecutor(),
                memory_floor_bytes=50,
            )

        service = build_production_fleet_profile_service(
            sessions,
            clock=lifecycle._clock,
            run_switch_operations=run_switch_service(),
            recipe_operations=lifecycle,
        )
        profile = service.create(
            FleetProfileInput.model_validate(
                {
                    "name": "Crash after child commit",
                    "assignments": [
                        {
                            "recipe_selector": f"vonk-forge/{revision.slug}",
                            "spark_ids": list(nodes),
                            "desired_state": "running",
                            "assignment_name": "crash-gap",
                        }
                    ],
                }
            ),
            actor="admin",
        )
        preview = service.preview(profile.id)
        assert preview.allowed
        assert [step.kind for step in preview.steps] == ["switch"]
        application = service.apply(
            profile.id,
            plan_digest=preview.plan_digest,
            request_key=_uuid(911),
            actor="admin",
        )
        adapter = service._switch_adapter
        assert isinstance(adapter, RunSwitchFleetProfileAdapter)
        write_state = adapter._write_state
        crashed = [False]

        def crash_after_child(session, row, state):
            if isinstance(state.get("active_operation_id"), str) and not crashed[0]:
                crashed[0] = True
                raise SystemExit("worker died after child commit")
            return write_state(session, row, state)

        monkeypatch.setattr(adapter, "_write_state", crash_after_child)
        with pytest.raises(SystemExit, match="worker died"):
            service.tick()
        assert crashed[0]

        with sessions.begin() as session:
            children = tuple(
                session.scalars(select(Job).where(Job.kind == "recipe.run-switch.v2"))
            )
            assert len(children) == 1
            child_id = children[0].id
            parent = session.get(FleetProfileApplication, application.id)
            assert parent is not None
            assert parent.current_operation_id is None
            if child_disposition == "completed":
                raw_plan = children[0].payload["plan"]
                assert isinstance(raw_plan, dict)
                phases = raw_plan["phases"]
                assert isinstance(phases, list)
                result = children[0].result
                assert isinstance(result, dict)
                children[0].result = {
                    **result,
                    "phase_index": len(phases),
                    "completed_phases": [phase["kind"] for phase in phases],
                    "child_operation_id": None,
                }
                children[0].state = "succeeded"
            elif child_disposition == "wrong-owner":
                raw_plan = children[0].payload["plan"]
                assert isinstance(raw_plan, dict)
                plan = dict(raw_plan)
                plan["recipe_revision_id"] = _uuid(999)
                children[0].payload = {**children[0].payload, "plan": plan}
            elif child_disposition == "wrong-scope":
                children[0].targets = [_uuid(999)]

        restarted = build_production_fleet_profile_service(
            sessionmaker(engine, expire_on_commit=False),
            clock=lifecycle._clock,
            run_switch_operations=run_switch_service(),
            recipe_operations=lifecycle,
        )
        for _ in range(4):
            restarted.tick()
            resumed = restarted.application(application.id)
            adapter_progress = resumed.progress.switch_adapter
            if child_disposition == "completed" and resumed.state == "succeeded":
                break
            if (
                child_disposition == "running"
                and adapter_progress is not None
                and adapter_progress.active_operation_id == child_id
            ):
                break
            if child_disposition.startswith("wrong-") and resumed.state == "failed":
                break
        assert resumed.progress.switch_adapter is not None
        with sessions() as session:
            children = tuple(
                session.scalars(select(Job).where(Job.kind == "recipe.run-switch.v2"))
            )
        assert [child.id for child in children] == [child_id]
        if child_disposition == "completed":
            assert resumed.state == "succeeded", resumed.status_reason
        elif child_disposition == "running":
            assert resumed.progress.switch_adapter.active_operation_id == child_id
        else:
            assert resumed.state == "failed"
            assert resumed.status_reason is not None
            assert (
                "owner or assignment"
                if child_disposition == "wrong-owner"
                else "Spark scope"
            ) in resumed.status_reason
    finally:
        engine.dispose()


def test_postgres_completed_cleanup_child_is_adopted_after_checkpoint_crash(
    tmp_path: Path, postgres_engine, monkeypatch
) -> None:
    """Exact cleanup already completed remains the sole child after restart."""

    engine = create_engine(postgres_engine.url.render_as_string(hide_password=False))
    try:
        sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(
            tmp_path, engine=engine
        )
        installed = installed_recipe(
            lifecycle, mapping_id, build_id, nodes, request_id=_uuid(920)
        )
        run_switch = RunSwitchOperationService(
            sessions,
            lifecycle=lifecycle,
            clock=lifecycle._clock,
            artifacts=CompleteArtifactInspector(),
            artifact_phase_executor=RecordingArtifactExecutor(),
            memory_floor_bytes=50,
        )
        service = build_production_fleet_profile_service(
            sessions,
            clock=lifecycle._clock,
            run_switch_operations=run_switch,
            recipe_operations=lifecycle,
        )
        profile = service.create(
            FleetProfileInput.model_validate(
                {
                    "name": "Exact cleanup after crash",
                    "installation_policy": "exact",
                    "assignments": [],
                }
            ),
            actor="admin",
        )
        preview = service.preview(profile.id)
        assert preview.allowed
        assert [step.kind for step in preview.steps] == ["switch"], [
            step.kind for step in preview.steps
        ]
        application = service.apply(
            profile.id,
            plan_digest=preview.plan_digest,
            request_key=_uuid(921),
            actor="admin",
        )
        adapter = service._switch_adapter
        assert isinstance(adapter, RunSwitchFleetProfileAdapter)
        write_state = adapter._write_state

        def crash_after_child(session, row, state):
            if isinstance(state.get("active_operation_id"), str):
                raise SystemExit("worker died after cleanup child commit")
            return write_state(session, row, state)

        monkeypatch.setattr(adapter, "_write_state", crash_after_child)
        with pytest.raises(SystemExit, match="cleanup child commit"):
            service.tick()
        with sessions() as session:
            child = session.scalar(select(Job).where(Job.kind == "recipe.cleanup.v2"))
            assert child is not None
            child_id = child.id
            raw_plan = child.payload["plan"]
            assert isinstance(raw_plan, dict)
            assert raw_plan["installation_id"] == installed.owner_id

        # Let the real Run/Switch and lifecycle services finish cleanup while
        # the parent is still at its pre-checkpoint state. Only the agent's
        # result boundary is supplied by the test.
        assert run_switch.tick()
        uninstall_id = _child_operation_id(run_switch.get(child_id))
        assert uninstall_id is not None
        for node_id in nodes:
            lifecycle.record_node_result(
                uninstall_id,
                node_id,
                succeeded=True,
                evidence={"uninstalled": True, "removed_model_bytes": 1},
            )
        for _ in range(4):
            if run_switch.get(child_id).state == "succeeded":
                break
            run_switch.tick()
        assert run_switch.get(child_id).state == "succeeded"

        restarted = build_production_fleet_profile_service(
            sessionmaker(engine, expire_on_commit=False),
            clock=lifecycle._clock,
            run_switch_operations=RunSwitchOperationService(
                sessions,
                lifecycle=lifecycle,
                clock=lifecycle._clock,
                artifacts=CompleteArtifactInspector(),
                artifact_phase_executor=RecordingArtifactExecutor(),
                memory_floor_bytes=50,
            ),
            recipe_operations=lifecycle,
        )
        for _ in range(4):
            restarted.tick()
            if restarted.application(application.id).state == "succeeded":
                break
        resumed = restarted.application(application.id)
        assert resumed.state == "succeeded", resumed.status_reason
        with sessions() as session:
            children = tuple(
                session.scalars(select(Job).where(Job.kind == "recipe.cleanup.v2"))
            )
            effects = tuple(
                session.scalars(
                    select(AgentOperation).where(
                        AgentOperation.parent_job_id == uninstall_id
                    )
                )
            )
            installation = session.get(RecipeInstallation, installed.owner_id)
        assert [child.id for child in children] == [child_id]
        assert {effect.node_id for effect in effects} == set(nodes)
        assert len(effects) == len(nodes)
        assert installation is None or installation.state == "uninstalled"
    finally:
        engine.dispose()
