from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from vonk_control.db import build_engine, session_factory
from vonk_control.models import AgentOperation, Base, Job, PackageValidationRun
from vonk_control.package_services import ProductionPackageProjectionService
from vonk_control.repository import RepositoryService


def test_production_package_projection_reads_git_authority_and_sql_state(tmp_path) -> None:
    root = Path(__file__).resolve().parents[2]
    engine = build_engine(f"sqlite:///{tmp_path / 'package-services.sqlite'}")
    sessions = session_factory(engine)
    # This test uses a private in-process SQLite database and never mutates the
    # repository.  The production adapter must read the same pinned tree as
    # reconciliation, not a mutable checkout path.
    Base.metadata.create_all(engine)
    service = ProductionPackageProjectionService(RepositoryService(root), sessions)

    families = service.families(None, 100)
    deployments = service.deployments(None, 100)

    assert families["total"] == 0
    assert families["families"] == []
    assert deployments["total"] == 0
    assert deployments["deployments"] == []
    assert service.inventory(None, None, None, 100)["nodes"] == []


def test_validation_status_projects_queue_identity_and_per_node_progress(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'package-validation-status.sqlite'}")
    sessions = session_factory(engine)
    Base.metadata.create_all(engine)
    now = datetime(2026, 8, 6, tzinfo=UTC)
    validation_id = "00000000-0000-4000-8000-000000000001"
    job_id = "00000000-0000-4000-8000-000000000002"
    node_id = "spk_" + "1" * 32
    with sessions.begin() as session:
        session.add(
            PackageValidationRun(
                id=validation_id,
                candidate_id="future-stack-candidate",
                resolution_id="00000000-0000-4000-8000-000000000003",
                validation_kind="artifact",
                release_digest="a" * 64,
                policy_digest="b" * 64,
                fleet_digest="c" * 64,
                state="running",
                actor="admin",
                progress={"completed": 1, "failed": 0, "running": 1, "total": 2},
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            Job(
                id=job_id,
                request_id=validation_id,
                kind="package.validation",
                state="running",
                actor="admin",
                base_commit="d" * 40,
                targets=[node_id],
                payload_digest="e" * 64,
                payload={},
                created_at=now,
                updated_at=now,
            )
        )
        for index, state in enumerate(("succeeded", "running")):
            session.add(
                AgentOperation(
                    id=f"00000000-0000-4000-8000-00000000001{index + 1}",
                    parent_job_id=job_id,
                    node_id=node_id,
                    kind="package.prepare" if index == 0 else "package.health",
                    payload_digest="f" * 64,
                    payload={},
                    base_commit="d" * 40,
                    state=state,
                    current_attempt=1,
                    created_at=now,
                    updated_at=now,
                )
            )
    service = ProductionPackageProjectionService(RepositoryService(Path(__file__).resolve().parents[2]), sessions)

    result = service.validation_status(validation_id)

    assert result["job_id"] == job_id
    assert result["audit_request_id"] == validation_id
    assert result["nodes"] == [
        {
            "node_id": node_id,
            "state": "running",
            "batch_index": 0,
            "completed": 1,
            "total": 2,
        }
    ]
