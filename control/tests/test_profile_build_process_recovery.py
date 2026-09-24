"""A killed profile worker recovers committed build/start effects in PostgreSQL.

The external Spark and route publisher use deterministic fixture evidence.
Controller ownership, transactions, source/archive storage and process death
are real. This is not native builder, model-quality or physical acceptance.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import fields
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import RecipeStartPayload, canonical_message
from vonk_control.distribution_executor import CompositeDistributionPhaseExecutor
from vonk_control.execution_plan_service import ControllerExecutionPlanService
from vonk_control.fleet_profiles import build_production_fleet_profile_service
from vonk_control.install_admission import InstallAdmissionService
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.models import (
    AgentOperation,
    FleetProfileApplication,
    Job,
    NodeInventorySnapshot,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
)
from vonk_control.presence import ManagementAddressPolicy
from vonk_control.recipe_builds import RecipeBuildService
from vonk_control.recipe_execution_contract import parse_stored_installation_plan
from vonk_control.recipe_operation_worker import RecipeOperationWorker
from vonk_control.recipe_operations import RecipeOperationService
from vonk_control.recipe_routes import RecipeRouteService
from vonk_control.run_admission import RunAdmissionService
from vonk_control.run_switch_operations import PhaseExecution, RunSwitchOperationService
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    RuntimeImageReceipt,
)
from vonk_control.source_bundles import SourceBundleStore

from .test_lifecycle_preflight import _finish as complete_preflight
from .test_profile_build_memory import _accepted_build_profile
from .test_recipe_operations import (
    ConcurrentPublisher,
    RecordingQueue,
    _CanonicalModelCache,
    complete_started_recipe,
)
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
    _target_copy_evidence,
)


class _PreparedArtifactExecutor(RecordingArtifactExecutor):
    """Prepare from the real archive; simulate only already-cached target bytes."""

    def __init__(self, preparation):
        super().__init__()
        self.preparation = preparation

    def execute(self, plan, phase, **kwargs):
        if phase.kind == "prepare" and phase.subphase == "runtime-image":
            return self.preparation.execute(plan, phase, **kwargs)
        if phase.subphase == "target-copy" and phase.kind in {"transfer", "verify"}:
            evidence = _target_copy_evidence(plan, phase, kwargs["progress"])
            if phase.kind == "verify":
                return PhaseExecution(
                    result={
                        "verified": True,
                        "verified_digests": evidence["verified_digests"],
                        "verified_build_id": plan.recipe_build_id,
                        "verified_image_digest": evidence["verified_image_digest"],
                        "verified_oci_layout_sha256": evidence[
                            "verified_oci_layout_sha256"
                        ],
                    }
                )
            return PhaseExecution(result=evidence)
        return super().execute(plan, phase, **kwargs)


def _complete_agent_work(sessions, lifecycle, root: Path) -> None:
    """Complete actual queued lifecycle jobs using deterministic Spark results."""
    with sessions() as session:
        jobs = tuple(
            session.scalars(
                select(Job).where(
                    Job.state == "running",
                    Job.kind.in_(
                        (
                            "recipe.build.v1",
                            "recipe.install",
                            "recipe.start",
                            "recipe.stop",
                            "recipe.uninstall",
                            "runtime.preflight.v1",
                        )
                    ),
                )
            )
        )
    for job in jobs:
        if job.kind == "recipe.build.v1":
            with sessions() as session:
                build = session.get(RecipeBuild, job.payload["owner_id"])
                assert build is not None
            storage = FilesystemRuntimeImageStorage(root / "runtime-images")
            staged = storage.prepare_path()
            staged.write_bytes((root / "expected-build.archive").read_bytes())
            expected = RuntimeImageReceipt.model_validate_json(
                (root / "expected-build.receipt.json").read_text()
            )
            receipt = storage.commit(
                staged,
                receipt=expected.model_copy(
                    update={
                        "archive_path": str(staged),
                        "build_id": build.id,
                        "build_input_sha256": build.build_input_sha256,
                    }
                ),
            )
            lifecycle.record_node_result(
                job.id,
                job.targets[0],
                succeeded=True,
                evidence={
                    "build_input_sha256": build.build_input_sha256,
                    "image_digest": receipt.image_digest,
                    "oci_layout_sha256": receipt.oci_archive_sha256,
                    "image_bytes": receipt.image_bytes,
                    "policy": {
                        "passed": True,
                        "findings": [],
                        "dockerfile": "Dockerfile",
                    },
                },
            )
        elif job.kind == "recipe.install":
            for node_id in job.targets:
                lifecycle.record_node_result(
                    job.id, node_id, succeeded=True, evidence={"installed_bytes": 120}
                )
        elif job.kind in {"recipe.stop", "recipe.uninstall"}:
            for node_id in job.targets:
                lifecycle.record_node_result(
                    job.id,
                    node_id,
                    succeeded=True,
                    evidence={"stopped": True}
                    if job.kind == "recipe.stop"
                    else {"removed": True},
                )
        elif job.kind == "runtime.preflight.v1":
            complete_preflight(
                sessions, SimpleNamespace(pending_job_id=job.id), lifecycle._clock()
            )
        else:
            complete_started_recipe(sessions, lifecycle, job.id)


def _worker_process(config: dict, crash: str) -> None:
    root = Path(config["root"])
    now = [datetime.fromisoformat(config["now"])]
    engine = create_engine(
        config["database"], connect_args={"options": "-c lock_timeout=3000"}
    )
    sessions = sessionmaker(engine, expire_on_commit=False)
    storage = FilesystemRuntimeImageStorage(root / "runtime-images")

    def prepared_image(
        _recipe,
        _runtime,
        build,
        *,
        before_publish: Callable[[RuntimeImageReceipt], object] | None = None,
    ):
        receipt = storage.find_build(
            build.build_input_sha256,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
            expected_archive_sha256=build.oci_layout_sha256,
        )
        assert receipt is not None, "the exact prepared image receipt is missing"
        if before_publish is not None:
            # Match the production cache-hit path: establish the same exact
            # archive fence before the durable owner validates and records its
            # provisional reference. Propagate fence/callback failures so the
            # fixture cannot authorize a stale or cancelled publication.
            with storage.publication_lock(receipt.oci_archive_sha256):
                before_publish(receipt)
        return receipt

    compilation = ControllerExecutionPlanService(
        _CanonicalModelCache(), runtime_image_preparer=prepared_image
    )
    routes = RecipeRouteService(
        sessions,
        publisher=ConcurrentPublisher(),
        management_policy=ManagementAddressPolicy.parse("192.168.1.0/24"),
        clock=lambda: now[0],
    )
    lifecycle = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(
            sessions,
            disk_floor_bytes=10,
            compiled_plan_provider=compilation.compile_installation,
        ),
        run_admission=RunAdmissionService(sessions, memory_floor_bytes=50),
        agent_jobs=RecordingQueue(),
        clock=lambda: now[0],
        builds=RecipeBuildService(
            sessions,
            bundles=SourceBundleStore(root / "sources"),
            build_archive_available=storage.build_archive_available,
            prepared_builds=storage.find_build,
        ),
        route_publications=routes,
    )
    planner = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: now[0],
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=_PreparedArtifactExecutor(
            CompositeDistributionPhaseExecutor(
                sessions,
                lifecycle._agent_jobs,
                None,  # Target transfer is outside this ownership/process test.
                model_cache=compilation._model_cache,
                runtime_image_preparer=prepared_image,
                clock=lambda: now[0],
            )
        ),
        memory_floor_bytes=50,
        build_archive_available=storage.build_archive_available,
    )
    profiles = build_production_fleet_profile_service(
        sessions, clock=lambda: now[0], run_switch_operations=planner
    )
    worker = RecipeOperationWorker(
        sessions,
        routes,
        clock=lambda: now[0],
        fleet_profiles=profiles,
        run_switches=planner,
        build_cleanup=lifecycle.reconcile_cancelled_builds,
    )
    if crash == "build-after":
        build = lifecycle.build

        def die_after_build(*args, **kwargs):
            build(*args, **kwargs)
            os._exit(23)

        lifecycle.build = die_after_build
    elif crash == "start-after":
        start = lifecycle.start

        def die_after_start(*args, **kwargs):
            start(*args, **kwargs)
            os._exit(23)

        lifecycle.start = die_after_start
    elif crash in {"build-before", "start-before"}:
        queue = lifecycle._queue_in_session
        crash_kind = "recipe.build.v1" if crash == "build-before" else "recipe.start"

        def die_before_commit(session, **kwargs):
            child = queue(session, **kwargs)
            if kwargs["kind"] == crash_kind:
                # Flush child, agent operation and capacity changes to PostgreSQL;
                # abrupt exit must roll them all back together.
                session.flush()
                os._exit(23)
            return child

        lifecycle._queue_in_session = die_before_commit

    try:
        if crash == "complete-child":
            # Simulated external completion while no parent worker is running.
            _complete_agent_work(sessions, lifecycle, root)
            return
        # One synchronous worker serves parents and their children. A parent
        # that blocks instead of returning to the loop hits the process timeout.
        for _ in range(80):
            if config.get("refresh_inventory"):
                # Physical readings are deterministic fixture evidence; the
                # inventory repository owns their actual receipt/time boundary.
                with sessions() as session:
                    samples = [
                        {
                            field.name: getattr(snapshot, field.name)
                            for field in fields(InventorySnapshotInput)
                        }
                        for snapshot in {
                            row.node_id: row
                            for row in session.scalars(
                                select(NodeInventorySnapshot).order_by(
                                    NodeInventorySnapshot.observed_at
                                )
                            )
                        }.values()
                    ]
                inventory = InventoryRepository(sessions, clock=lambda: now[0])
                for sample in samples:
                    sample["observed_at"] = now[0]
                    inventory.record(InventorySnapshotInput(**sample))
            worker.tick()
            _complete_agent_work(sessions, lifecycle, root)
            application = profiles.application(config["application"])
            if application.state in {"succeeded", "failed", "cancelled"}:
                print(
                    json.dumps(
                        {
                            "application_id": application.id,
                            "state": application.state,
                            **(
                                {"reason": application.status_reason}
                                if application.state != "succeeded"
                                else {}
                            ),
                        }
                    )
                )
                return
            now[0] += timedelta(seconds=1)
        with sessions() as session:
            pending = [
                (job.kind, job.state, job.status_reason, job.result)
                for job in session.scalars(select(Job))
            ]
        raise AssertionError(
            f"profile did not settle: {application.model_dump(mode='json')}; {pending}"
        )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("boundary", "complete_while_down", "initially_installed", "replacement"),
    [
        ("build-before", False, False, None),
        ("build-after", False, False, None),
        ("build-after", True, False, None),
        ("start-before", False, False, None),
        ("start-after", False, False, None),
        ("start-after", True, False, None),
        pytest.param("build-after", True, True, None, id="installed-restore"),
        pytest.param(
            "build-after", True, False, "image", id="first-install-changed-image"
        ),
        pytest.param("build-after", True, True, "image", id="installed-changed-image"),
        pytest.param(
            "build-after", True, True, "archive", id="installed-changed-archive"
        ),
    ],
)
def test_profile_recovers_after_worker_process_death(
    tmp_path,
    postgres_engine,
    boundary,
    complete_while_down,
    initially_installed,
    replacement,
):
    sessions, profiles, _planner, _node, application_id, selected = (
        _accepted_build_profile(
            tmp_path, postgres_engine, initially_installed=initially_installed
        )
    )
    assert application_id is not None
    storage = FilesystemRuntimeImageStorage(tmp_path / "runtime-images")
    original_archive = next(storage.root.glob("*.receipt.json"))
    original = RuntimeImageReceipt.model_validate_json(original_archive.read_text())
    # Lose the actual cache bytes after acceptance. The deterministic Spark
    # result recreates the exact reviewed image; a different rebuilt image is
    # the separate W09f consent boundary, not an acceptable test shortcut.
    archive_path = Path(original.archive_path)
    assert archive_path.parent == storage.root
    (tmp_path / "expected-build.archive").write_bytes(archive_path.read_bytes())
    (tmp_path / "expected-build.receipt.json").write_text(original.model_dump_json())
    if replacement == "image":
        changed = original.model_copy(
            update={
                "image_digest": "sha256:" + "b" * 64,
                "platform_manifest_digest": "sha256:" + "b" * 64,
            }
        )
        (tmp_path / "expected-build.receipt.json").write_text(changed.model_dump_json())
    elif replacement == "archive":
        changed_bytes = archive_path.read_bytes() + b"replacement"
        (tmp_path / "expected-build.archive").write_bytes(changed_bytes)
        changed = original.model_copy(
            update={
                "oci_archive_sha256": hashlib.sha256(changed_bytes).hexdigest(),
                "image_bytes": len(changed_bytes),
            }
        )
        (tmp_path / "expected-build.receipt.json").write_text(changed.model_dump_json())
    archive_path.unlink()
    original_archive.unlink()
    with sessions() as session:
        original_installations = {
            row.id: row.plan for row in session.scalars(select(RecipeInstallation))
        }
        claim_ids = {
            claim.id
            for claim in session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == application_id,
                    ResourceReservation.kind == "unified-memory",
                )
            )
        }
        assert claim_ids
    config = tmp_path / "worker.json"
    config.write_text(
        json.dumps(
            {
                "database": postgres_engine.url.render_as_string(hide_password=False),
                "root": str(tmp_path),
                "now": profiles._clock().isoformat(),
                "application": application_id,
            }
        )
    )
    config.chmod(0o600)

    def run(crash):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "tests.test_profile_build_process_recovery",
                str(config),
                crash,
            ],
            cwd=Path(__file__).parents[1],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    crashed = run(boundary)
    assert crashed.returncode == 23, crashed.stderr
    kind = "recipe.build.v1" if boundary.startswith("build") else "recipe.start"
    with sessions() as session:
        child = session.scalar(select(Job).where(Job.kind == kind))
        if boundary.endswith("before"):
            assert child is None
            assert not tuple(session.scalars(select(RecipeRun)))
            child_id = None
        else:
            assert child is not None and child.state == "running"
            child_id = child.id
        parent = session.scalar(select(Job).where(Job.kind == "recipe.run-switch.v2"))
        assert parent is not None and parent.result is not None
        assert parent.result.get("child_operation_id") is None
        for claim_id in claim_ids:
            claim = session.get(ResourceReservation, claim_id)
            assert claim is not None
            assert claim.owner_kind == (
                "run" if boundary == "start-after" else "fleet-profile"
            )
            assert claim.state == (
                "active" if boundary == "start-after" else "promised"
            )

    def advance_restart_clock():
        with sessions() as session:
            last_commit = session.scalar(select(func.max(Job.updated_at)))
        assert last_commit is not None
        values = json.loads(config.read_text())
        values["now"] = (last_commit + timedelta(seconds=1)).isoformat()
        config.write_text(json.dumps(values))

    advance_restart_clock()
    if complete_while_down:
        completed = run("complete-child")
        assert completed.returncode == 0, completed.stderr
        with sessions() as session:
            child = session.get(Job, child_id)
            assert child is not None and child.state == "succeeded"
        advance_restart_clock()
    resumed = run("resume")
    assert resumed.returncode == 0, resumed.stderr
    if replacement is not None:
        outcome = json.loads(resumed.stdout)
        assert outcome["state"] == "failed", outcome
        assert "profile.runtime-image-changed" in outcome["reason"], outcome
        assert "review" in outcome["reason"], outcome
        with sessions() as session:
            assert not tuple(session.scalars(select(RecipeRun)))
            assert not tuple(
                session.scalars(select(Job.id).where(Job.kind == "recipe.start"))
            )
            assert {
                row.id: row.plan for row in session.scalars(select(RecipeInstallation))
            } == original_installations
            original_application = session.get(FleetProfileApplication, application_id)
            assert original_application is not None
            profile_id = original_application.profile_id
            actor = original_application.actor
        assert profile_id is not None
        review = profiles.preview(profile_id)
        assert review.allowed, review.model_dump(mode="json")
        approved_image = review.preparation_decisions[0].runtime_image
        expected_receipt = RuntimeImageReceipt.model_validate_json(
            (tmp_path / "expected-build.receipt.json").read_text()
        )
        assert approved_image.image_digest == expected_receipt.image_digest
        assert approved_image.oci_layout_sha256 == expected_receipt.oci_archive_sha256
        accepted = profiles.apply(
            profile_id,
            plan_digest=review.plan_digest,
            request_key=str(uuid4()),
            actor=actor,
        )
        assert accepted.id != application_id
        with sessions() as session:
            disk_claims = tuple(
                session.scalars(
                    select(ResourceReservation).where(
                        ResourceReservation.owner_id == accepted.id,
                        ResourceReservation.kind == "disk",
                        ResourceReservation.state == "active",
                    )
                )
            )
            assert disk_claims, "replacement installation needs reviewed disk capacity"
            disk_claim_ids = {claim.id for claim in disk_claims}
        values = json.loads(config.read_text())
        values["application"] = accepted.id
        config.write_text(json.dumps(values))
        advance_restart_clock()
        replacement_run = run("resume")
        assert replacement_run.returncode == 0, replacement_run.stderr
        assert json.loads(replacement_run.stdout) == {
            "application_id": accepted.id,
            "state": "succeeded",
        }
        with sessions() as session:
            running = session.scalar(select(RecipeRun))
            assert running is not None and running.state == "running"
            assert (
                running.route_state == "published"
                and running.route_generation is not None
            )
            installed = session.get(RecipeInstallation, running.installation_id)
            assert installed is not None and installed.id not in original_installations
            compiled = parse_stored_installation_plan(
                installed.plan
            ).compiled_execution_plans
            for payload in compiled.values():
                assert payload.runtime_image.image_digest == approved_image.image_digest
                assert (
                    payload.runtime_image.oci_layout_sha256
                    == approved_image.oci_layout_sha256
                )
            starts = tuple(
                session.scalars(
                    select(AgentOperation).where(
                        AgentOperation.kind == "recipe.start",
                    )
                )
            )
            assert starts
            for start in starts:
                payload = RecipeStartPayload.model_validate_json(
                    canonical_message(start.payload)
                )
                assert (
                    payload.compiled_execution_plan.runtime_image.image_digest
                    == approved_image.image_digest
                )
                assert (
                    payload.compiled_execution_plan.runtime_image.oci_layout_sha256
                    == approved_image.oci_layout_sha256
                )
            for original_id, original_plan in original_installations.items():
                old = session.get(RecipeInstallation, original_id)
                assert old is not None and old.plan == original_plan
            for claim_id in disk_claim_ids:
                claim = session.get(ResourceReservation, claim_id)
                assert claim is not None and claim.owner_id == installed.id
                assert claim.owner_kind == "installation" and claim.state == "active"
        return
    assert json.loads(resumed.stdout) == {
        "application_id": application_id,
        "state": "succeeded",
    }
    with sessions() as session:
        for child_kind in ("recipe.build.v1", "recipe.install", "recipe.start"):
            jobs = tuple(session.scalars(select(Job).where(Job.kind == child_kind)))
            assert len(jobs) == 1 and jobs[0].state == "succeeded"
            if child_kind == kind and child_id is not None:
                assert jobs[0].id == child_id
        runs = tuple(session.scalars(select(RecipeRun)))
        assert len(runs) == 1 and runs[0].state == "running"
        assert runs[0].route_state == "published"
        assert runs[0].route_generation is not None
        assert len(tuple(session.scalars(select(RecipeInstallation)))) == 1
        if initially_installed:
            assert {
                row.id: row.plan for row in session.scalars(select(RecipeInstallation))
            } == original_installations
        application = session.get(FleetProfileApplication, application_id)
        assert application is not None and application.state == "succeeded"
        for claim_id in claim_ids:
            claim = session.get(ResourceReservation, claim_id)
            assert (
                claim is not None
                and claim.owner_kind == "run"
                and claim.state == "active"
            )
        assert not tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == selected.build_id,
                    ResourceReservation.state == "active",
                )
            )
        )


if __name__ == "__main__":
    _worker_process(json.loads(Path(sys.argv[1]).read_text()), sys.argv[2])
