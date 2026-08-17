import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from vonk_control.dashboard import DashboardService
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.models import AgentNode, AgentNodeProfile, Base, Observation

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


class Repository:
    def head(self): return "a" * 40
    def read_document(self, commit, path):
        raise AssertionError(f"unexpected document read: {commit} {path}")


def test_dashboard_does_not_read_the_repository_fleet_document(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'empty-dashboard.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)

    assert DashboardService(Repository(), sessions, clock=lambda: NOW).fleet() == {
        "commit": "a" * 40,
        "nodes": [],
    }


class PresenceRepository:
    def head(self): return "b" * 40
    def read_document(self, commit, path):
        raise AssertionError(f"unexpected document read: {commit} {path}")


def test_dashboard_joins_registered_fleet_with_latest_observation(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'dashboard.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        node_id = "spk_00000000000000000000000000000001"
        session.add_all(
            [
                AgentNode(node_id=node_id, state="active", capabilities=[]),
                AgentNodeProfile(
                    node_id=node_id,
                    display_name="Alpha",
                    hostname="alpha",
                    lifecycle="ready",
                    labels={"zone": "lab"},
                ),
                Observation(
                    node_id=node_id,
                    kind="health",
                    payload={"status": "healthy"},
                    observed_at=datetime(2026, 8, 3, tzinfo=UTC),
                ),
            ]
        )
    result = DashboardService(
        Repository(),
        sessions,
        clock=lambda: datetime(2026, 8, 3, 0, 5, 1, tzinfo=UTC),
    ).fleet()
    assert result["commit"] == "a" * 40
    node = result["nodes"][0]
    assert {key: node[key] for key in ("id", "display_name", "hostname", "lifecycle", "healthy", "labels")} == {
        "id": "spk_00000000000000000000000000000001", "display_name": "Alpha", "hostname": "alpha", "lifecycle": "ready", "healthy": True, "labels": {"zone": "lab"},
    }
    assert "management" not in result["nodes"][0]
    assert node["probe_age_seconds"] == 301.0
    assert node["stale"] is True


def test_fleet_queries_only_latest_health_per_node(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'latest-health.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    node_id = "spk_00000000000000000000000000000001"
    with sessions.begin() as session:
        session.add_all(
            (
                AgentNode(node_id=node_id, state="active", capabilities=[]),
                AgentNodeProfile(
                    node_id=node_id,
                    display_name="Alpha",
                    hostname="alpha",
                    lifecycle="ready",
                    labels={"zone": "lab"},
                ),
                Observation(
                    node_id=node_id,
                    kind="health",
                    payload={"status": "unhealthy", "memory_available_bytes": 100},
                    observed_at=NOW - timedelta(minutes=2),
                ),
                Observation(
                    node_id=node_id,
                    kind="health",
                    payload={"status": "warning", "memory_available_bytes": 200},
                    observed_at=NOW - timedelta(minutes=1),
                ),
                Observation(
                    node_id=node_id,
                    kind="health",
                    payload={"status": "healthy", "memory_available_bytes": 300},
                    observed_at=NOW,
                ),
                Observation(
                    node_id="spk_00000000000000000000000000000002",
                    kind="management-address",
                    payload={"address": "10.0.0.42"},
                    observed_at=NOW,
                ),
            )
        )
    statements = []

    @event.listens_for(engine, "before_cursor_execute")
    def capture_statement(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    result = DashboardService(Repository(), sessions, clock=lambda: NOW).fleet()

    assert result["nodes"][0]["healthy"] is True
    assert result["nodes"][0]["memory_available_bytes"] == 300
    assert len([row for row in result["nodes"] if row["id"] == node_id]) == 1
    health_queries = [
        statement
        for statement in statements
        if "FROM observations" in statement and "observations.kind" in statement
    ]
    assert len(health_queries) == 1
    assert "ROW_NUMBER() OVER" in health_queries[0].upper()


def test_dashboard_projects_agent_availability_without_addresses(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'presence-dashboard.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    active = "spk_" + "a" * 32
    stale = "spk_" + "b" * 32
    revoked = "spk_" + "c" * 32
    with sessions.begin() as session:
        session.add_all(
            (
                AgentNode(
                    node_id=active,
                    state="active",
                    capabilities=[],
                    agent_implementation="rust",
                    migration_state="complete",
                    protocol_version=3,
                    last_seen_at=NOW - timedelta(seconds=149),
                ),
                AgentNode(
                    node_id=stale,
                    state="active",
                    capabilities=[],
                    last_seen_at=NOW - timedelta(seconds=151),
                ),
                AgentNode(
                    node_id=revoked,
                    state="revoked",
                    capabilities=[],
                    last_seen_at=NOW - timedelta(seconds=1),
                    revoked_at=NOW - timedelta(seconds=1),
                ),
                Observation(
                    node_id=active,
                    kind="management-address",
                    payload={"address": "10.0.0.42"},
                    observed_at=NOW,
                ),
                AgentNodeProfile(
                    node_id=active,
                    display_name="Active",
                    hostname="active.lan",
                    lifecycle="ready",
                    labels={},
                ),
                AgentNodeProfile(
                    node_id=stale,
                    display_name="Stale",
                    hostname="stale.lan",
                    lifecycle="ready",
                    labels={},
                ),
                AgentNodeProfile(
                    node_id=revoked,
                    display_name="Revoked",
                    hostname="revoked.lan",
                    lifecycle="ready",
                    labels={},
                ),
            )
        )
    InventoryRepository(sessions, clock=lambda: NOW).record(
        InventorySnapshotInput(
            node_id=active,
            observed_at=NOW - timedelta(seconds=10),
            disk_total_bytes=10_000,
            disk_free_bytes=7_000,
            host_memory_total_bytes=20_000,
            host_memory_free_bytes=15_000,
            gpu_memory_total_bytes=20_000,
            gpu_memory_free_bytes=14_000,
            gpu_count=1,
            artifact_store_read_only=False,
            capabilities=(
                "recipe.operations.v1",
                "build.rootless-podman.v1",
                "runtime.spark-docker-nvidia.v1",
            ),
            nvidia_driver_version="580.65",
            container_runtime_version="5.4.2",
        )
    )

    result = DashboardService(
        PresenceRepository(),
        sessions,
        clock=lambda: NOW,
        protocol_maximum=3,
        agent_online_window_seconds=150,
    ).fleet()

    nodes = {node["display_name"]: node for node in result["nodes"]}
    assert nodes["Active"]["agent_state"] == "active"
    assert nodes["Active"]["agent_online"] is True
    assert nodes["Active"]["agent_implementation"] == "rust"
    assert nodes["Active"]["agent_migration_state"] == "complete"
    assert nodes["Active"]["compatibility"] == "supported"
    assert nodes["Active"]["inventory_stale"] is False
    assert nodes["Active"]["inventory_age_seconds"] == 10
    assert nodes["Active"]["inventory_capabilities"] == [
        "build.rootless-podman.v1",
        "recipe.operations.v1",
        "runtime.spark-docker-nvidia.v1",
    ]
    assert nodes["Active"]["memory_available_bytes"] == 15_000
    assert nodes["Active"]["disk_available_bytes"] == 7_000
    assert nodes["Stale"]["agent_migration_state"] == "required"
    assert nodes["Active"]["agent_last_seen_at"] == (NOW - timedelta(seconds=149)).isoformat()
    assert nodes["Stale"]["agent_state"] == "active"
    assert nodes["Stale"]["agent_online"] is False
    assert "Revoked" not in nodes
    encoded = json.dumps(result, sort_keys=True)
    assert "10.0.0.42" not in encoded
    assert "management-address" not in encoded
