"""Build cancellation survives worker process death with exact cleanup ownership.

PostgreSQL and managed Controller storage are real. The Spark cleanup receipt
is deterministic fixture evidence; this does not claim physical Spark
acceptance.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from vonk_control.agent_jobs import AgentJobService
from vonk_control.install_admission import InstallAdmissionService
from vonk_control.models import (
    AgentOperation,
    Job,
    RecipeBuild,
    ResourceReservation,
    RuntimeImageAuthorization,
)
from vonk_control.recipe_builds import RecipeBuildService
from vonk_control.recipe_operation_worker import RecipeOperationWorker
from vonk_control.recipe_operations import (
    RecipeOperationConflict,
    RecipeOperationService,
)
from vonk_control.run_admission import RunAdmissionService
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage
from vonk_control.source_bundles import SourceBundleStore

from .test_build_cancellation_recovery import (
    _active_claims,
    _evidence,
    _issue,
    _services,
)


class _Routes:
    def publish_run(self, run_id: str) -> object:
        del run_id
        raise AssertionError("cancelled build recovery must not publish a route")

    def maintain(self, *, renew_before_seconds: int = 10) -> bool:
        del renew_before_seconds
        return False


def _reconstructed_services(config: dict[str, object]):
    root = Path(str(config["root"]))
    now = datetime.fromisoformat(str(config["now"]))
    engine = create_engine(
        str(config["database"]), connect_args={"options": "-c lock_timeout=3000"}
    )
    sessions = sessionmaker(engine, expire_on_commit=False)
    storage = FilesystemRuntimeImageStorage(root / "images")
    builds = RecipeBuildService(
        sessions,
        bundles=SourceBundleStore(root / "bundles"),
        build_archive_available=storage.build_archive_available,
        prepared_builds=storage.find_build,
    )
    agent_jobs = AgentJobService(sessions, clock=lambda: now)
    operations = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(sessions),
        run_admission=RunAdmissionService(sessions),
        agent_jobs=agent_jobs,
        clock=lambda: now,
        builds=builds,
    )
    return engine, sessions, storage, builds, operations


def _process(config_path: str, action: str) -> None:
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    engine, sessions, _storage, _builds, operations = _reconstructed_services(config)
    now = datetime.fromisoformat(str(config["now"]))
    try:
        if action == "cancel-and-die":
            view = operations.cancel(
                str(config["job_id"]),
                actor=str(config["cancel_actor"]),
                request_id=str(config["cancel_request_id"]),
                reason=str(config["cancel_reason"]),
            )
            # Reaching this point means cancel() returned after its transaction
            # committed. Skip Python cleanup to model abrupt worker death.
            print(json.dumps({"id": view.id, "state": view.state}), flush=True)
            os._exit(23)
        if action != "reconcile":
            raise ValueError(f"unknown process action: {action}")
        worker = RecipeOperationWorker(
            sessions,
            _Routes(),
            clock=lambda: now,
            build_cleanup=operations.reconcile_cancelled_builds,
        )
        progressed = worker.tick()
        with sessions() as session:
            original = session.get(Job, str(config["job_id"]))
            assert original is not None
            child = session.scalar(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == original.id
                )
            )
            cleanup_rows = tuple(
                session.scalars(
                    select(Job).where(Job.kind == "recipe.build.cleanup.v1")
                )
            )
            cleanup = cleanup_rows[0] if len(cleanup_rows) == 1 else None
            cleanup_operation = (
                None
                if cleanup is None
                else session.scalar(
                    select(AgentOperation).where(
                        AgentOperation.parent_job_id == cleanup.id
                    )
                )
            )
            build = session.get(RecipeBuild, str(config["build_id"]))
            claims = tuple(
                sorted(
                    session.scalars(
                        select(ResourceReservation.id).where(
                            ResourceReservation.owner_kind == "recipe-build",
                            ResourceReservation.owner_id == str(config["build_id"]),
                            ResourceReservation.state == "active",
                        )
                    )
                )
            )
            print(
                json.dumps(
                    {
                        "progressed": progressed,
                        "original_state": original.state,
                        "cancellation": original.result,
                        "child_id": None if child is None else child.id,
                        "cleanup_count": len(cleanup_rows),
                        "cleanup_id": None if cleanup is None else cleanup.id,
                        "cleanup_request_id": (
                            None if cleanup is None else cleanup.request_id
                        ),
                        "cleanup_payload": None if cleanup is None else cleanup.payload,
                        "cleanup_operation_id": (
                            None if cleanup_operation is None else cleanup_operation.id
                        ),
                        "cleanup_operation_state": (
                            None
                            if cleanup_operation is None
                            else cleanup_operation.state
                        ),
                        "build_state": None if build is None else build.state,
                        "build_image_digest": (
                            None if build is None else build.image_digest
                        ),
                        "build_archive_digest": (
                            None if build is None else build.oci_layout_sha256
                        ),
                        "claim_ids": claims,
                    }
                ),
                flush=True,
            )
    finally:
        engine.dispose()


if __name__ == "__main__":
    _process(sys.argv[1], sys.argv[2])


def test_issued_build_cancellation_reconstructs_exact_cleanup_after_process_death(
    tmp_path, postgres_engine
):
    sessions, builds, operations, _storage, now, node_id, revision, plan = _services(
        tmp_path, postgres_engine
    )
    original_key = str(uuid.uuid4())
    original = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="operator",
        request_id=original_key,
    )
    child_id = _issue(sessions, original.id)
    claim_ids = _active_claims(sessions, plan.build_id)
    assert claim_ids

    cancellation_key = str(uuid.uuid4())
    cancellation_actor = "operator:restart-test"
    cancellation_reason = "stop the issued build before restart"
    config = tmp_path / "cancel-process.json"
    config.write_text(
        json.dumps(
            {
                "database": postgres_engine.url.render_as_string(hide_password=False),
                "root": str(tmp_path),
                "now": now.isoformat(),
                "job_id": original.id,
                "build_id": plan.build_id,
                "cancel_request_id": cancellation_key,
                "cancel_actor": cancellation_actor,
                "cancel_reason": cancellation_reason,
            }
        ),
        encoding="utf-8",
    )
    config.chmod(0o600)

    def run(action: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "tests.test_build_cancellation_process_recovery",
                str(config),
                action,
            ],
            cwd=Path(__file__).parents[1],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    crashed = run("cancel-and-die")
    assert crashed.returncode == 23, crashed.stderr

    cleanup_request_id = str(uuid5(NAMESPACE_URL, f"vonk:build-cleanup:{child_id}"))
    with sessions() as session:
        original_row = session.get(Job, original.id)
        cleanup = session.scalar(
            select(Job).where(Job.request_id == cleanup_request_id)
        )
        assert cleanup is not None
        cleanup_id = cleanup.id
        cleanup_operation = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == cleanup_id)
        )
        build = session.get(RecipeBuild, plan.build_id)
        assert original_row is not None and original_row.result is not None
        assert original_row.result["cancel_requested"] is True
        assert original_row.result["cancel_request_id"] == cancellation_key
        assert original_row.result["cancel_actor"] == cancellation_actor
        assert original_row.result["reason"] == cancellation_reason
        assert cleanup.state == "running"
        assert cleanup.request_id == cleanup_request_id
        assert cleanup.payload["owner_id"] == plan.build_id
        cancellation_payload = cleanup.payload.get("build_cancellation")
        assert isinstance(cancellation_payload, dict)
        assert cancellation_payload.get("cancel_request_id") == cancellation_key
        assert cleanup_operation is not None and cleanup_operation.state == "queued"
        assert cleanup_operation.payload == {
            "schema_version": 1,
            "build_id": plan.build_id,
            "operation_id": child_id,
        }
        assert build is not None and build.state == "failed"
        assert build.image_digest is None and build.oci_layout_sha256 is None
        assert not tuple(
            session.scalars(
                select(RuntimeImageAuthorization).where(
                    RuntimeImageAuthorization.build_id == plan.build_id
                )
            )
        )
    assert _active_claims(sessions, plan.build_id) == claim_ids

    restarted = run("reconcile")
    assert restarted.returncode == 0, restarted.stderr
    observation = json.loads(restarted.stdout)
    assert observation["child_id"] == child_id
    assert observation["cleanup_count"] == 1
    assert observation["cleanup_id"] == cleanup_id
    assert observation["cleanup_request_id"] == cleanup_request_id
    assert observation["cleanup_payload"]["owner_id"] == plan.build_id
    assert (
        observation["cleanup_payload"]["build_cancellation"]["cancel_request_id"]
        == cancellation_key
    )
    assert observation["cleanup_operation_id"] is not None
    assert observation["cleanup_operation_state"] == "queued"
    assert observation["claim_ids"] == sorted(claim_ids)
    assert observation["build_state"] == "failed"
    assert observation["build_image_digest"] is None
    assert observation["build_archive_digest"] is None
    assert _active_claims(sessions, plan.build_id) == claim_ids

    with pytest.raises(RecipeOperationConflict, match="cancel|cleanup"):
        operations.build(
            plan,
            build_input_sha256=plan.build_input_sha256,
            actor="operator",
            request_id=str(uuid.uuid4()),
        )

    # A result racing cancellation cannot publish an image. The exact cleanup
    # receipt still owns the claims until it proves this attempt stopped.
    operations.record_node_result(
        original.id, node_id, succeeded=True, evidence=_evidence(plan)
    )
    assert _active_claims(sessions, plan.build_id) == claim_ids
    with sessions() as session:
        build = session.get(RecipeBuild, plan.build_id)
        assert build is not None and build.state == "failed"
        assert build.image_digest is None and build.oci_layout_sha256 is None
        assert not tuple(
            session.scalars(
                select(RuntimeImageAuthorization).where(
                    RuntimeImageAuthorization.build_id == plan.build_id
                )
            )
        )
    # Re-enter through a fresh worker after the late result. It must discover
    # the same pending cleanup rather than release or duplicate it.
    restarted_after_result = run("reconcile")
    assert restarted_after_result.returncode == 0, restarted_after_result.stderr
    assert json.loads(restarted_after_result.stdout)["cleanup_count"] == 1
    assert _active_claims(sessions, plan.build_id) == claim_ids

    cleanup_view = operations.get(cleanup_id)
    assert cleanup_view.state == "running"
    cleanup_evidence = {
        "schema_version": 1,
        "build_id": plan.build_id,
        "operation_id": child_id,
        "stopped": True,
    }
    operations.record_node_result(
        cleanup_id, node_id, succeeded=True, evidence=cleanup_evidence
    )
    assert operations.get(original.id).state == "cancelled"
    assert not _active_claims(sessions, plan.build_id)

    # Cleanup proof permits a new request. Replaying old effects after that
    # admission cannot release the new attempt's claims or publish old bytes.
    fresh_plan = builds.plan(revision.id, node_id, now=now)
    fresh = operations.build(
        fresh_plan,
        build_input_sha256=fresh_plan.build_input_sha256,
        actor="operator",
        request_id=str(uuid.uuid4()),
    )
    assert fresh.id != original.id
    fresh_claims = _active_claims(sessions, plan.build_id)
    assert fresh_claims
    operations.record_node_result(
        original.id, node_id, succeeded=True, evidence=_evidence(plan)
    )
    operations.record_node_result(
        cleanup_id, node_id, succeeded=True, evidence=cleanup_evidence
    )
    assert operations.get(original.id).state == "cancelled"
    assert operations.get(cleanup_id).state == "succeeded"
    assert _active_claims(sessions, plan.build_id) == fresh_claims
    with sessions() as session:
        build = session.get(RecipeBuild, plan.build_id)
        assert build is not None and build.state == "building"
        assert build.image_digest is None and build.oci_layout_sha256 is None
        assert not tuple(
            session.scalars(
                select(RuntimeImageAuthorization).where(
                    RuntimeImageAuthorization.build_id == plan.build_id
                )
            )
        )
        assert (
            session.scalar(
                select(func.count(Job.id)).where(Job.kind == "recipe.build.cleanup.v1")
            )
            == 1
        )
