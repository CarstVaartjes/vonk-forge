from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from vonk_control.db import build_engine, session_factory
from vonk_control.models import AgentNode, Base, Observation, PackageActionPlan
from vonk_control.package_services import ProductionPackageProjectionService


def test_package_action_plan_persists_bounded_digest_bound_request(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'plans.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    now = datetime.now(UTC)
    digest = "a" * 64
    with sessions.begin() as session:
        session.add(
            PackageActionPlan(
                plan_digest=digest,
                action="package.remove",
                subject="synthetic-canary",
                request={"release_digest": "b" * 64, "node_ids": ["spk_" + "1" * 32]},
                state="planned",
                expires_at=now + timedelta(minutes=15),
                created_at=now,
                updated_at=now,
            )
        )
    with sessions() as session:
        stored = session.get(PackageActionPlan, digest)
        assert stored is not None
        assert stored.request["node_ids"] == ["spk_" + "1" * 32]


def test_package_action_plan_rejects_unknown_action(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'plans.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    now = datetime.now(UTC)
    with pytest.raises(IntegrityError), sessions.begin() as session:
        session.add(
            PackageActionPlan(
                plan_digest="a" * 64,
                action="shell.exec",
                subject="x",
                request={"x": 1},
                state="planned",
                expires_at=now,
                created_at=now,
                updated_at=now,
            )
        )


def test_projection_service_reuses_exact_preview_and_rejects_changed_apply(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'plans.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)

    class Repository:
        def head(self):
            return "a" * 40

        def inspect(self, commit):
            return type("Snapshot", (), {"commit": commit, "documents": ()})()

    service = ProductionPackageProjectionService(Repository(), sessions)
    first = service.create_action_plan(
        "package.remove", "synthetic-canary", {"node_ids": ["spk_" + "1" * 32]}
    )
    second = service.create_action_plan(
        "package.remove", "synthetic-canary", {"node_ids": ["spk_" + "1" * 32]}
    )
    assert first == second
    request = service.consume_action_plan(first, "package.remove", "synthetic-canary")
    assert request["node_ids"] == ["spk_" + "1" * 32]
    with pytest.raises(ValueError, match="plan action or subject"):
        service.consume_action_plan(first, "package.gc", "synthetic-canary")


def test_gc_preview_and_apply_fan_out_per_node_without_stopping_workloads(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'plans.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    node_id = "spk_" + "2" * 32
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=node_id, state="active", capabilities=[]))
        session.add(
            Observation(
                node_id=node_id,
                kind="health",
                payload={"storage": {"total_bytes": 1000, "free_bytes": 100, "reclaimable_bytes": 400}},
                observed_at=now,
            )
        )
    queued: list[tuple[str, str, dict[str, object]]] = []

    class Repository:
        def head(self):
            return "a" * 40

        def inspect(self, commit):
            return type("Snapshot", (), {"commit": commit, "documents": ()})()

    class Jobs:
        def enqueue_in_session(self, session, job_id, node_id, operation, base_commit, payload, *, operation_id):
            del session, job_id, base_commit, operation_id
            queued.append((node_id, operation, dict(payload)))

        def notify_available(self):
            return None

    service = ProductionPackageProjectionService(
        Repository(), sessions, agent_jobs=Jobs()
    )
    preview = service.gc_preview()
    assert preview["reclaim_bytes"] == 400
    result = service.gc(preview["digest"], "admin", "request-gc")
    assert result["state"] == "planned"
    assert queued == [(node_id, "package.gc", {"schema_version": 1, "dry_run": False, "target_bytes": 400})]


def test_package_promotion_stays_fenced_to_publication_service(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'plans.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    candidate_id = "00000000-0000-4000-8000-000000000006"
    release_digest = "d" * 64
    with sessions.begin() as session:
        from vonk_control.models import PackageCandidate

        session.add(
            PackageCandidate(
                id=candidate_id,
                family_id="future-stack",
                upstream_identity_digest="a" * 64,
                metadata_digest="b" * 64,
                upstream_version="1.0.0",
                channel="stable",
                source_provider="git",
                source_reference="future-stack/1.0.0",
                state="resolved",
                summary={"release": {"release_digest": release_digest}},
                discovered_by="test",
                first_seen_at=datetime.now(UTC),
                last_seen_at=datetime.now(UTC),
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )

    class Repository:
        def head(self):
            return "a" * 40

    published: list[tuple[str, str]] = []

    class Publication:
        def preview(self, candidate: str, commit: str):
            assert candidate == candidate_id
            return type("Preview", (), {"digest": "e" * 64, "release_digest": release_digest, "base_commit": commit})()

        def promote(self, preview_digest: str, actor: str):
            published.append((preview_digest, actor))
            return type("Target", (), {"digest": release_digest})()

    service = ProductionPackageProjectionService(Repository(), sessions, publication=Publication())
    preview = service.promotion_preview(candidate_id)
    assert preview["candidate_id"] == candidate_id
    result = service.promote(candidate_id, preview["digest"], "admin", "request-promote")
    assert result["state"] == "published"
    assert published == [("e" * 64, "admin")]
