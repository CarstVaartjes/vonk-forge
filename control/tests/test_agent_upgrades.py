import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control.agent_jobs import AgentJobService
from vonk_control.agent_upgrades import AgentUpgradeConflict, AgentUpgradeService
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    Base,
    Job,
)

NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32
REVISION = "c" * 64
PACKAGE = {
    "architecture": "linux-arm64",
    "package_bytes": 5_000_000,
    "package_sha256": "d" * 64,
    "package_signature": "e" * 128,
    "package_url": (
        "https://install.vonkforge.ai/dev/releases/example/"
        "spark/current/linux-arm64/vonk-forge-agent.deb"
    ),
    "package_version": "0.1.0~dev.330+g0123456789ab",
    "schema_version": 1,
    "target_binary_digest": "a" * 64,
    "target_build_digest": "sha256:" + "b" * 64,
}
OLD_IDENTITY = {
    "architecture": "linux-arm64",
    "binary_digest": "f" * 64,
    "build_digest": "sha256:" + "f" * 64,
    "semantic_version": "0.1.0",
    "self_test_passed": True,
}
NEW_IDENTITY = {
    **OLD_IDENTITY,
    "binary_digest": PACKAGE["target_binary_digest"],
    "build_digest": PACKAGE["target_build_digest"],
}


def test_rollout_queues_only_one_spark_until_new_identity_is_proven(tmp_path) -> None:
    now = datetime(2026, 8, 27, tzinfo=UTC)
    engine = create_engine(f"sqlite:///{tmp_path / 'upgrades.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        for node_id, serial in ((NODE_A, "serial-a"), (NODE_B, "serial-b")):
            session.add(
                AgentNode(
                    node_id=node_id,
                    state="active",
                    capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
                    architecture="linux-arm64",
                    semantic_version="0.1.0",
                    build_digest=OLD_IDENTITY["build_digest"],
                    binary_digest=OLD_IDENTITY["binary_digest"],
                    self_test_passed=True,
                    last_seen_at=now,
                )
            )
            session.add(
                AgentCertificate(
                    serial=serial,
                    node_id=node_id,
                    not_before=now - timedelta(minutes=1),
                    not_after=now + timedelta(hours=1),
                    fingerprint=f"fingerprint-{serial}",
                )
            )
    operations = AgentJobService(sessions, clock=lambda: now)
    upgrades = AgentUpgradeService(
        sessions,
        operations,
        clock=lambda: now,
        current_revision=lambda: REVISION,
    )
    operations.set_result_consumer(upgrades.consume_agent_result)
    plan = upgrades.preview(None, PACKAGE)
    assert plan.node_ids == (NODE_A, NODE_B)
    job = upgrades.apply(
        None,
        PACKAGE,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )

    assert _operation_nodes(sessions, job.id) == [NODE_A]
    _upgrade_node(operations, NODE_A, "serial-a")
    assert set(_operation_nodes(sessions, job.id)) == {NODE_A, NODE_B}
    with sessions() as session:
        stored = session.get(Job, job.id)
        assert stored is not None and stored.state == "queued"

    _upgrade_node(operations, NODE_B, "serial-b")
    with sessions() as session:
        stored = session.get(Job, job.id)
        assert stored is not None and stored.state == "succeeded"


def test_all_at_once_rollout_dispatches_every_selected_spark(tmp_path) -> None:
    now = datetime(2026, 8, 27, tzinfo=UTC)
    engine = create_engine(f"sqlite:///{tmp_path / 'parallel-upgrades.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        for node_id in (NODE_A, NODE_B):
            session.add(
                AgentNode(
                    node_id=node_id,
                    state="active",
                    capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
                    architecture="linux-arm64",
                    semantic_version="0.1.0",
                    build_digest=OLD_IDENTITY["build_digest"],
                    binary_digest=OLD_IDENTITY["binary_digest"],
                    self_test_passed=True,
                    last_seen_at=now,
                )
            )
    operations = AgentJobService(sessions, clock=lambda: now)
    upgrades = AgentUpgradeService(
        sessions,
        operations,
        clock=lambda: now,
        current_revision=lambda: REVISION,
    )
    plan = upgrades.preview(None, PACKAGE, strategy="all-at-once")

    job = upgrades.apply(
        None,
        PACKAGE,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id=str(uuid.uuid4()),
        strategy="all-at-once",
    )

    assert set(_operation_nodes(sessions, job.id)) == {NODE_A, NODE_B}


def test_controller_selection_excludes_offline_sparks_and_individual_preview_explains(
    tmp_path,
) -> None:
    now = datetime(2026, 8, 27, tzinfo=UTC)
    engine = create_engine(f"sqlite:///{tmp_path / 'online-upgrades.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        for node_id, last_seen_at in (
            (NODE_A, now),
            (NODE_B, now - timedelta(minutes=10)),
        ):
            session.add(
                AgentNode(
                    node_id=node_id,
                    state="active",
                    capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
                    architecture="linux-arm64",
                    semantic_version="0.1.0",
                    build_digest=OLD_IDENTITY["build_digest"],
                    binary_digest=OLD_IDENTITY["binary_digest"],
                    self_test_passed=True,
                    last_seen_at=last_seen_at,
                )
            )
    upgrades = AgentUpgradeService(
        sessions,
        AgentJobService(sessions, clock=lambda: now),
        clock=lambda: now,
        current_revision=lambda: REVISION,
    )

    assert upgrades.preview(None, PACKAGE).node_ids == (NODE_A,)
    with pytest.raises(AgentUpgradeConflict, match="is not currently online"):
        upgrades.preview([NODE_B], PACKAGE)


def test_current_candidate_is_derived_from_the_published_arm64_release(tmp_path) -> None:
    generation = "9" * 64
    package_path = (
        f"artifacts/dev/releases/{generation}/spark/current/"
        "linux-arm64/vonk-forge-agent.deb"
    )
    signature_path = f"{package_path}.host.sig"
    signature_raw = ("e" * 128 + "\n").encode()
    release = {
        "artifacts": {
            "agent-package-linux-arm64": {
                "architecture": "linux-arm64",
                "host_signature": "e" * 128,
                "package_version": PACKAGE["package_version"],
                "path": package_path,
                "sha256": PACKAGE["package_sha256"],
                "size": PACKAGE["package_bytes"],
                "target_binary_digest": PACKAGE["target_binary_digest"],
                "target_build_digest": PACKAGE["target_build_digest"],
            },
            "agent-package-signature-linux-arm64": {
                "path": signature_path,
                "sha256": hashlib.sha256(signature_raw).hexdigest(),
                "size": len(signature_raw),
            },
        },
        "channel": "dev",
        "generation": generation,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/artifacts/dev/current.manifest":
            return httpx.Response(
                200,
                text=(
                    f"generation={generation}\n"
                    f"release_path=artifacts/dev/releases/{generation}/release.json\n"
                ),
            )
        if request.url.path == f"/artifacts/dev/releases/{generation}/release.json":
            return httpx.Response(200, content=json.dumps(release).encode())
        if request.url.path == f"/{signature_path}":
            return httpx.Response(200, content=signature_raw)
        return httpx.Response(404)

    engine = create_engine(f"sqlite:///{tmp_path / 'candidate.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    operations = AgentJobService(
        sessions, clock=lambda: datetime(2026, 8, 27, tzinfo=UTC)
    )
    upgrades = AgentUpgradeService(
        sessions,
        operations,
        clock=lambda: datetime(2026, 8, 27, tzinfo=UTC),
        current_revision=lambda: REVISION,
        transport=httpx.MockTransport(handler),
    )

    assert upgrades.current_package() == {
        **PACKAGE,
        "package_url": f"https://install.vonkforge.ai/{package_path}",
    }


def _operation_nodes(sessions, job_id: str) -> list[str]:
    with sessions() as session:
        return list(
            session.scalars(
                select(AgentOperation.node_id)
                .where(AgentOperation.parent_job_id == job_id)
                .order_by(AgentOperation.created_at, AgentOperation.id)
            )
        )


def _upgrade_node(
    operations: AgentJobService, node_id: str, certificate_serial: str
) -> None:
    claim = operations.claim(
        node_id,
        certificate_serial,
        30,
        capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
        runtime_identity=OLD_IDENTITY,
    )
    assert claim is not None
    assert (
        operations.claim(
            node_id,
            certificate_serial,
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=NEW_IDENTITY,
        )
        is None
    )
