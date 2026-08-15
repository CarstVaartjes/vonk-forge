from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, update
from sqlalchemy.orm import sessionmaker
from vonk_control.fleet_projection import FleetProjection
from vonk_control.models import (
    AgentNode,
    Base,
    ClusterMapping,
    ClusterMappingNode,
    FleetEventCursor,
    InstallationNode,
    LocalRecipe,
    LocalRecipeRevision,
    NodeInventorySnapshot,
    NodeTelemetryLatest,
    NodeTelemetrySample,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
COMMIT = "a" * 40
NODE_A = "spk_" + "1" * 32
NODE_B = "spk_" + "2" * 32
NODE_C = "spk_" + "3" * 32
NODE_D = "spk_" + "4" * 32
EXTRA_NODE = "spk_" + "f" * 32


class Repository:
    def __init__(self, nodes: dict[str, dict[str, object]] | None = None) -> None:
        self.calls: list[str] = []
        self.nodes = nodes if nodes is not None else {
            NODE_B: {
                "display_name": "Beta",
                "hostname": "beta.internal",
                "lifecycle": "managed",
                "labels": {"rack": "right"},
            },
            NODE_A: {
                "display_name": "Alpha",
                "hostname": "alpha.internal",
                "lifecycle": "managed",
                "labels": {"rack": "left"},
            },
        }

    def head(self) -> str:
        self.calls.append("head")
        return COMMIT

    def read_document(self, commit: str, path: str) -> SimpleNamespace:
        assert (commit, path) == (COMMIT, "inventory/fleet.toml")
        self.calls.append("fleet")
        return SimpleNamespace(parsed={"nodes": self.nodes})


def _inventory(
    node_id: str, observed_at: datetime, *, free_bytes: int
) -> NodeInventorySnapshot:
    suffix = node_id.removeprefix("spk_")[0]
    return NodeInventorySnapshot(
        id=f"00000000-0000-4000-8000-{free_bytes:012d}",
        node_id=node_id,
        observed_at=observed_at,
        received_at=observed_at + timedelta(seconds=1),
        disk_total_bytes=1_000,
        disk_free_bytes=free_bytes,
        host_memory_total_bytes=2_000,
        host_memory_free_bytes=1_500,
        gpu_memory_total_bytes=2_000,
        gpu_memory_free_bytes=1_400,
        gpu_count=1,
        fabric_address=f"10.0.0.{int(suffix, 16)}",
        fabric_bandwidth_mbps=100_000,
        nvidia_driver_version="580.1",
        container_runtime_version="1.2.3",
        artifact_store_read_only=False,
        capabilities=["runtime.vonk.v1"],
        evidence_digest=((suffix + f"{free_bytes:x}") * 64)[:64],
    )


def _telemetry(
    node_id: str,
    sample_id: str,
    observed_at: datetime,
    *,
    sequence: int,
    cpu: float,
) -> NodeTelemetrySample:
    return NodeTelemetrySample(
        id=sample_id,
        node_id=node_id,
        boot_id="00000000-0000-4000-8000-000000000001",
        sequence=sequence,
        observed_at=observed_at,
        received_at=observed_at + timedelta(milliseconds=250),
        cpu_utilization_percent=cpu,
        load_average_1m=1.5,
        memory_total_bytes=2_000,
        memory_available_bytes=1_400,
        disk_total_bytes=1_000,
        disk_free_bytes=700,
        gpu_utilization_percent=25.0,
        gpu_memory_total_bytes=2_000,
        gpu_memory_free_bytes=1_300,
        temperature_c=42.5,
        power_watts=18.25,
        network_receive_bytes_per_second=1_024.5,
        network_transmit_bytes_per_second=512.25,
        gap_samples=2,
        details={
            "accelerator_name": "NVIDIA GB10",
            "accelerator_performance_state": "P0",
        },
    )


def test_read_uses_repository_membership_latest_rows_and_a_bounded_query_set() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    repository = Repository()
    with sessions.begin() as session:
        session.execute(
            update(FleetEventCursor)
            .where(FleetEventCursor.singleton_id == 1)
            .values(last_id=7)
        )
        session.add_all(
            [
                AgentNode(
                    node_id=NODE_A,
                    state="active",
                    architecture="linux-arm64",
                    capabilities=["runtime.vonk.v1"],
                    last_seen_at=NOW - timedelta(seconds=5),
                ),
                AgentNode(
                    node_id=NODE_B,
                    state="active",
                    architecture="linux-arm64",
                    capabilities=["runtime.vonk.v1"],
                    last_seen_at=NOW - timedelta(seconds=7),
                ),
                AgentNode(
                    node_id=EXTRA_NODE,
                    state="active",
                    architecture="linux-arm64",
                    capabilities=[],
                    last_seen_at=NOW,
                ),
            ]
        )
        session.add_all(
            [
                _inventory(NODE_A, NOW - timedelta(minutes=2), free_bytes=600),
                _inventory(NODE_A, NOW - timedelta(seconds=10), free_bytes=800),
                _inventory(NODE_B, NOW - timedelta(seconds=12), free_bytes=750),
                _inventory(EXTRA_NODE, NOW, free_bytes=999),
            ]
        )
        old = _telemetry(
            NODE_A,
            "00000000-0000-4000-8000-000000000011",
            NOW - timedelta(seconds=5),
            sequence=1,
            cpu=10.0,
        )
        latest = _telemetry(
            NODE_A,
            "00000000-0000-4000-8000-000000000012",
            NOW - timedelta(seconds=2),
            sequence=2,
            cpu=12.5,
        )
        extra = _telemetry(
            EXTRA_NODE,
            "00000000-0000-4000-8000-000000000013",
            NOW,
            sequence=1,
            cpu=99.0,
        )
        session.add_all([old, latest, extra])
        session.flush()
        session.add_all(
            [
                NodeTelemetryLatest(node_id=NODE_A, sample_id=latest.id),
                NodeTelemetryLatest(node_id=EXTRA_NODE, sample_id=extra.id),
            ]
        )

    statements: list[str] = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(" ".join(statement.split()).lower())

    event.listen(engine, "before_cursor_execute", record_statement)
    snapshot = FleetProjection(repository, sessions, clock=lambda: NOW).read()
    event.remove(engine, "before_cursor_execute", record_statement)

    assert repository.calls == ["head", "fleet"]
    assert snapshot.model_dump(mode="json") == {
        "schema_version": 1,
        "event_cursor": 7,
        "generated_at": "2026-08-15T12:00:00Z",
        "repository_commit": COMMIT,
        "nodes": [
            {
                "id": NODE_A,
                "display_name": "Alpha",
                "hostname": "alpha.internal",
                "lifecycle": "managed",
                "labels": {"rack": "left"},
                "connection": {
                    "agent_state": "active",
                    "online_state": "online",
                    "last_seen_at": "2026-08-15T11:59:55Z",
                    "last_seen_age_seconds": 5.0,
                },
                "inventory": {
                    "observed_at": "2026-08-15T11:59:50Z",
                    "received_at": "2026-08-15T11:59:51Z",
                    "age_seconds": 10.0,
                    "freshness": "fresh",
                    "disk_total_bytes": 1000,
                    "disk_free_bytes": 800,
                    "host_memory_total_bytes": 2000,
                    "host_memory_free_bytes": 1500,
                    "gpu_memory_total_bytes": 2000,
                    "gpu_memory_free_bytes": 1400,
                    "gpu_count": 1,
                    "artifact_store_read_only": False,
                    "capabilities": ["runtime.vonk.v1"],
                    "fabric_address": "10.0.0.1",
                    "fabric_bandwidth_mbps": 100000,
                    "nvidia_driver_version": "580.1",
                    "container_runtime_version": "1.2.3",
                },
                "telemetry": {
                    "age_seconds": 2.0,
                    "freshness": "live",
                    "sample": {
                        "id": "00000000-0000-4000-8000-000000000012",
                        "node_id": NODE_A,
                        "boot_id": "00000000-0000-4000-8000-000000000001",
                        "sequence": 2,
                        "observed_at": "2026-08-15T11:59:58Z",
                        "received_at": "2026-08-15T11:59:58.250000Z",
                        "cpu_utilization_percent": 12.5,
                        "load_average_1m": 1.5,
                        "memory_total_bytes": 2000,
                        "memory_available_bytes": 1400,
                        "disk_total_bytes": 1000,
                        "disk_free_bytes": 700,
                        "gpu_utilization_percent": 25.0,
                        "gpu_memory_total_bytes": 2000,
                        "gpu_memory_free_bytes": 1300,
                        "temperature_c": 42.5,
                        "power_watts": 18.25,
                        "network_receive_bytes_per_second": 1024.5,
                        "network_transmit_bytes_per_second": 512.25,
                        "gap_samples": 2,
                        "details": {
                            "accelerator_name": "NVIDIA GB10",
                            "accelerator_performance_state": "P0",
                        },
                    },
                },
                "installed": [],
                "loaded": [],
                "reservations": {
                    "disk_bytes": 0,
                    "unified_memory_bytes": 0,
                    "host_memory_bytes": 0,
                    "gpu_memory_bytes": 0,
                    "port_count": 0,
                },
                "warnings": [],
            },
            {
                "id": NODE_B,
                "display_name": "Beta",
                "hostname": "beta.internal",
                "lifecycle": "managed",
                "labels": {"rack": "right"},
                "connection": {
                    "agent_state": "active",
                    "online_state": "online",
                    "last_seen_at": "2026-08-15T11:59:53Z",
                    "last_seen_age_seconds": 7.0,
                },
                "inventory": {
                    "observed_at": "2026-08-15T11:59:48Z",
                    "received_at": "2026-08-15T11:59:49Z",
                    "age_seconds": 12.0,
                    "freshness": "fresh",
                    "disk_total_bytes": 1000,
                    "disk_free_bytes": 750,
                    "host_memory_total_bytes": 2000,
                    "host_memory_free_bytes": 1500,
                    "gpu_memory_total_bytes": 2000,
                    "gpu_memory_free_bytes": 1400,
                    "gpu_count": 1,
                    "artifact_store_read_only": False,
                    "capabilities": ["runtime.vonk.v1"],
                    "fabric_address": "10.0.0.2",
                    "fabric_bandwidth_mbps": 100000,
                    "nvidia_driver_version": "580.1",
                    "container_runtime_version": "1.2.3",
                },
                "telemetry": None,
                "installed": [],
                "loaded": [],
                "reservations": {
                    "disk_bytes": 0,
                    "unified_memory_bytes": 0,
                    "host_memory_bytes": 0,
                    "gpu_memory_bytes": 0,
                    "port_count": 0,
                },
                "warnings": [
                    {
                        "code": "telemetry.missing",
                        "detail": "No telemetry sample is available.",
                        "severity": "warning",
                    }
                ],
            },
        ],
    }
    selects = [statement for statement in statements if statement.startswith("select")]
    assert len(selects) == 8
    telemetry_reads = [
        statement for statement in selects if "node_telemetry_samples" in statement
    ]
    assert len(telemetry_reads) == 1
    assert "node_telemetry_latest" in telemetry_reads[0]
    inventory_reads = [
        statement for statement in selects if "node_inventory_snapshots" in statement
    ]
    assert len(inventory_reads) == 1
    assert "row_number() over (partition by node_inventory_snapshots.node_id" in (
        inventory_reads[0]
    )
    assert EXTRA_NODE not in {node.id for node in snapshot.nodes}


def test_read_captures_the_committed_cursor_before_repository_projection() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    order: list[str] = []

    class OrderedRepository(Repository):
        def head(self) -> str:
            order.append("repository")
            return super().head()

    class Events:
        def high_watermark(self) -> int:
            order.append("watermark")
            return 41

    snapshot = FleetProjection(
        OrderedRepository({}),
        sessions,
        clock=lambda: NOW,
        events=Events(),
    ).read()

    assert order == ["watermark", "repository"]
    assert snapshot.event_cursor == 41


def test_freshness_boundaries_keep_telemetry_agent_and_inventory_independent() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    nodes = {
        node_id: {
            "display_name": node_id,
            "hostname": "node.internal",
            "lifecycle": "managed",
            "labels": {},
        }
        for node_id in (NODE_A, NODE_B, NODE_C, NODE_D)
    }
    ages = {
        NODE_A: timedelta(seconds=6),
        NODE_B: timedelta(seconds=6, milliseconds=1),
        NODE_C: timedelta(seconds=20),
        NODE_D: timedelta(seconds=20, milliseconds=1),
    }
    with sessions.begin() as session:
        for index, node_id in enumerate(nodes, start=1):
            session.add(
                AgentNode(
                    node_id=node_id,
                    state="active",
                    architecture="linux-arm64",
                    capabilities=[],
                    last_seen_at=NOW
                    - timedelta(seconds=150 if node_id != NODE_D else 151),
                )
            )
            inventory_age = timedelta(
                seconds=300, milliseconds=1 if node_id == NODE_B else 0
            )
            session.add(
                _inventory(
                    node_id,
                    NOW - inventory_age,
                    free_bytes=700 + index,
                )
            )
            sample = _telemetry(
                node_id,
                f"00000000-0000-4000-8000-{index:012d}",
                NOW - ages[node_id],
                sequence=index,
                cpu=float(index),
            )
            session.add(sample)
            session.flush()
            session.add(NodeTelemetryLatest(node_id=node_id, sample_id=sample.id))

    snapshot = FleetProjection(
        Repository(nodes), sessions, clock=lambda: NOW
    ).read()

    assert [
        (
            node.id,
            node.telemetry.freshness if node.telemetry else None,
            node.connection.online_state,
            node.inventory.freshness if node.inventory else None,
            [warning.code for warning in node.warnings],
        )
        for node in snapshot.nodes
    ] == [
        (NODE_A, "live", "online", "fresh", []),
        (
            NODE_B,
            "delayed",
            "online",
            "stale",
            ["inventory.stale", "telemetry.delayed"],
        ),
        (NODE_C, "delayed", "online", "fresh", ["telemetry.delayed"]),
        (
            NODE_D,
            "stale",
            "offline",
            "fresh",
            ["node.offline", "telemetry.stale"],
        ),
    ]


def test_installed_and_loaded_groups_require_every_exact_current_rank() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    recipe_id = "00000000-0000-4000-8000-000000000101"
    revision_id = "00000000-0000-4000-8000-000000000102"
    mapping_id = "00000000-0000-4000-8000-000000000103"
    build_id = "00000000-0000-4000-8000-000000000104"
    complete_installation_id = "00000000-0000-4000-8000-000000000105"
    partial_installation_id = "00000000-0000-4000-8000-000000000106"
    healthy_run_id = "00000000-0000-4000-8000-000000000107"
    degraded_run_id = "00000000-0000-4000-8000-000000000108"
    route_failed_run_id = "00000000-0000-4000-8000-000000000121"
    nodes = {
        NODE_A: {
            "display_name": "Alpha",
            "hostname": "alpha.internal",
            "lifecycle": "managed",
            "labels": {},
        },
        NODE_B: {
            "display_name": "Beta",
            "hostname": "beta.internal",
            "lifecycle": "managed",
            "labels": {},
        },
    }
    with sessions.begin() as session:
        session.add_all(
            [
                AgentNode(
                    node_id=NODE_A,
                    state="active",
                    architecture="linux-arm64",
                    capabilities=[],
                    last_seen_at=NOW,
                ),
                AgentNode(
                    node_id=NODE_B,
                    state="active",
                    architecture="linux-arm64",
                    capabilities=[],
                    last_seen_at=NOW,
                ),
            ]
        )
        recipe = LocalRecipe(
            id=recipe_id,
            slug="pair-recipe",
            title="Pair Recipe",
            description="Two ranks",
            source_kind="local",
            created_by="admin",
            created_at=NOW,
            updated_at=NOW,
        )
        revision = LocalRecipeRevision(
            id=revision_id,
            recipe_id=recipe_id,
            revision_number=1,
            lifecycle="resolved",
            schema_version=1,
            document={},
            content_sha256="1" * 64,
            created_by="admin",
            created_at=NOW,
        )
        mapping = ClusterMapping(
            id=mapping_id,
            recipe_revision_id=revision_id,
            profile_name="pair",
            generation=1,
            node_count=2,
            state="ready",
            parameters={},
            placement_digest="2" * 64,
            endpoint_owner_node_id=NODE_A,
            created_by="admin",
            created_at=NOW,
            updated_at=NOW,
        )
        build = RecipeBuild(
            id=build_id,
            recipe_revision_id=revision_id,
            builder_node_id=NODE_A,
            source_bundle_sha256="3" * 64,
            build_input_sha256="4" * 64,
            state="succeeded",
            policy_report={},
            plan={},
            image_digest="sha256:" + "5" * 64,
            oci_layout_sha256="6" * 64,
            image_bytes=100,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add_all([recipe, revision, mapping, build])
        session.flush()
        session.add_all(
            [
                ClusterMappingNode(
                    id="00000000-0000-4000-8000-000000000109",
                    mapping_id=mapping_id,
                    node_id=NODE_A,
                    rank=0,
                    role="entrypoint",
                    endpoint_owner=True,
                    created_at=NOW,
                ),
                ClusterMappingNode(
                    id="00000000-0000-4000-8000-000000000110",
                    mapping_id=mapping_id,
                    node_id=NODE_B,
                    rank=1,
                    role="worker",
                    endpoint_owner=False,
                    created_at=NOW,
                ),
            ]
        )
        for installation_id, digest in (
            (complete_installation_id, "7" * 64),
            (partial_installation_id, "8" * 64),
        ):
            session.add(
                RecipeInstallation(
                    id=installation_id,
                    recipe_revision_id=revision_id,
                    mapping_id=mapping_id,
                    mapping_generation=1,
                    recipe_build_id=build_id,
                    image_digest="sha256:" + "5" * 64,
                    plan_digest=digest,
                    plan={},
                    state="installed",
                    actor="admin",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        session.flush()
        session.add_all(
            [
                InstallationNode(
                    id="00000000-0000-4000-8000-000000000111",
                    installation_id=complete_installation_id,
                    node_id=NODE_A,
                    rank=0,
                    role="entrypoint",
                    state="installed",
                    required_bytes=100,
                    installed_bytes=100,
                    updated_at=NOW,
                ),
                InstallationNode(
                    id="00000000-0000-4000-8000-000000000112",
                    installation_id=complete_installation_id,
                    node_id=NODE_B,
                    rank=1,
                    role="worker",
                    state="installed",
                    required_bytes=100,
                    installed_bytes=100,
                    updated_at=NOW,
                ),
                InstallationNode(
                    id="00000000-0000-4000-8000-000000000113",
                    installation_id=partial_installation_id,
                    node_id=NODE_A,
                    rank=0,
                    role="entrypoint",
                    state="installed",
                    required_bytes=100,
                    installed_bytes=100,
                    updated_at=NOW,
                ),
            ]
        )
        for run_id, alias, route_state, digest in (
            (healthy_run_id, "pair-healthy", "published", "9" * 64),
            (degraded_run_id, "pair-stale", "published", "a" * 64),
            (route_failed_run_id, "pair-route-failed", "failed", "f" * 64),
        ):
            session.add(
                RecipeRun(
                    id=run_id,
                    installation_id=complete_installation_id,
                    mapping_id=mapping_id,
                    mapping_generation=1,
                    alias=alias,
                    plan_digest=digest,
                    plan={},
                    state="running",
                    route_state=route_state,
                    actor="admin",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        session.flush()
        session.add_all(
            [
                RunNode(
                    id="00000000-0000-4000-8000-000000000114",
                    run_id=healthy_run_id,
                    node_id=NODE_A,
                    rank=0,
                    role="entrypoint",
                    state="running",
                    port=8000,
                    reserved_memory_bytes=200,
                    observed_memory_bytes=180,
                    updated_at=NOW - timedelta(seconds=1),
                ),
                RunNode(
                    id="00000000-0000-4000-8000-000000000115",
                    run_id=healthy_run_id,
                    node_id=NODE_B,
                    rank=1,
                    role="worker",
                    state="running",
                    port=8001,
                    reserved_memory_bytes=200,
                    observed_memory_bytes=175,
                    updated_at=NOW - timedelta(seconds=2),
                ),
                RunNode(
                    id="00000000-0000-4000-8000-000000000116",
                    run_id=degraded_run_id,
                    node_id=NODE_A,
                    rank=0,
                    role="entrypoint",
                    state="running",
                    port=8002,
                    reserved_memory_bytes=200,
                    observed_memory_bytes=190,
                    updated_at=NOW - timedelta(seconds=3),
                ),
                RunNode(
                    id="00000000-0000-4000-8000-000000000122",
                    run_id=degraded_run_id,
                    node_id=NODE_B,
                    rank=1,
                    role="worker",
                    state="running",
                    port=8003,
                    reserved_memory_bytes=200,
                    observed_memory_bytes=185,
                    updated_at=NOW - timedelta(seconds=300),
                ),
                RunNode(
                    id="00000000-0000-4000-8000-000000000123",
                    run_id=route_failed_run_id,
                    node_id=NODE_A,
                    rank=0,
                    role="entrypoint",
                    state="running",
                    port=8004,
                    reserved_memory_bytes=200,
                    observed_memory_bytes=180,
                    updated_at=NOW - timedelta(seconds=1),
                ),
                RunNode(
                    id="00000000-0000-4000-8000-000000000124",
                    run_id=route_failed_run_id,
                    node_id=NODE_B,
                    rank=1,
                    role="worker",
                    state="running",
                    port=8005,
                    reserved_memory_bytes=200,
                    observed_memory_bytes=175,
                    updated_at=NOW - timedelta(seconds=2),
                ),
            ]
        )
        session.add_all(
            [
                ResourceReservation(
                    id="00000000-0000-4000-8000-000000000117",
                    node_id=NODE_A,
                    kind="disk",
                    resource_key="install",
                    amount_bytes=100,
                    owner_kind="install",
                    owner_id=complete_installation_id,
                    state="active",
                    plan_digest="b" * 64,
                    created_at=NOW,
                ),
                ResourceReservation(
                    id="00000000-0000-4000-8000-000000000118",
                    node_id=NODE_A,
                    kind="unified-memory",
                    resource_key="run",
                    amount_bytes=200,
                    owner_kind="run",
                    owner_id=healthy_run_id,
                    state="active",
                    plan_digest="c" * 64,
                    created_at=NOW,
                ),
                ResourceReservation(
                    id="00000000-0000-4000-8000-000000000119",
                    node_id=NODE_A,
                    kind="port",
                    resource_key="8000",
                    amount_bytes=0,
                    owner_kind="run",
                    owner_id=healthy_run_id,
                    state="active",
                    plan_digest="d" * 64,
                    created_at=NOW,
                ),
                ResourceReservation(
                    id="00000000-0000-4000-8000-000000000120",
                    node_id=NODE_A,
                    kind="disk",
                    resource_key="released",
                    amount_bytes=999,
                    owner_kind="install",
                    owner_id=partial_installation_id,
                    state="released",
                    plan_digest="e" * 64,
                    created_at=NOW,
                    released_at=NOW,
                ),
            ]
        )

    snapshot = FleetProjection(
        Repository(nodes), sessions, clock=lambda: NOW
    ).read()
    alpha, beta = snapshot.nodes
    complete = next(
        value
        for value in alpha.installed
        if value.installation_id == complete_installation_id
    )
    partial = next(
        value
        for value in alpha.installed
        if value.installation_id == partial_installation_id
    )
    healthy = next(value for value in alpha.loaded if value.run_id == healthy_run_id)
    degraded = next(
        value for value in alpha.loaded if value.run_id == degraded_run_id
    )
    route_failed = next(
        value for value in alpha.loaded if value.run_id == route_failed_run_id
    )

    assert (
        complete.expected_rank_count,
        complete.present_ranks,
        complete.member_node_ids,
        complete.complete,
        complete.degraded_reason,
    ) == (2, [0, 1], [NODE_A, NODE_B], True, None)
    assert (
        partial.expected_rank_count,
        partial.present_ranks,
        partial.member_node_ids,
        partial.complete,
        partial.degraded_reason,
    ) == (2, [0], [NODE_A], False, "missing-ranks")
    assert (
        healthy.expected_rank_count,
        healthy.present_ranks,
        healthy.member_node_ids,
        healthy.healthy,
        healthy.group_state,
        healthy.degraded_reason,
    ) == (2, [0, 1], [NODE_A, NODE_B], True, "healthy", None)
    assert (
        degraded.expected_rank_count,
        degraded.present_ranks,
        degraded.member_node_ids,
        degraded.route_state,
        degraded.healthy,
        degraded.group_state,
        degraded.degraded_reason,
    ) == (
        2,
        [0, 1],
        [NODE_A, NODE_B],
        "published",
        False,
        "degraded",
        "rank-stale",
    )
    assert (
        route_failed.present_ranks,
        route_failed.member_node_ids,
        route_failed.route_state,
        route_failed.healthy,
        route_failed.degraded_reason,
    ) == ([0, 1], [NODE_A, NODE_B], "failed", False, "route-not-published")
    assert [value.installation_id for value in beta.installed] == [
        complete_installation_id
    ]
    assert [value.run_id for value in beta.loaded] == [
        healthy_run_id,
        degraded_run_id,
        route_failed_run_id,
    ]
    assert alpha.reservations.model_dump() == {
        "disk_bytes": 100,
        "unified_memory_bytes": 200,
        "host_memory_bytes": 0,
        "gpu_memory_bytes": 0,
        "port_count": 1,
    }
    assert [warning.code for warning in alpha.warnings] == [
        "inventory.missing",
        "telemetry.missing",
        "install.partial",
        "run.degraded",
    ]


def test_history_is_repository_authorized_raw_bounded_and_chronological() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    repository = Repository(
        {
            NODE_A: {
                "display_name": "Alpha",
                "hostname": "alpha.internal",
                "lifecycle": "managed",
                "labels": {},
            }
        }
    )
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=NODE_A,
                state="active",
                architecture="linux-arm64",
                capabilities=[],
            )
        )
        session.add_all(
            [
                _telemetry(
                    NODE_A,
                    "00000000-0000-4000-8000-000000000201",
                    NOW - timedelta(minutes=50),
                    sequence=1,
                    cpu=1.0,
                ),
                _telemetry(
                    NODE_A,
                    "00000000-0000-4000-8000-000000000202",
                    NOW - timedelta(minutes=20),
                    sequence=2,
                    cpu=2.0,
                ),
                _telemetry(
                    NODE_A,
                    "00000000-0000-4000-8000-000000000203",
                    NOW - timedelta(minutes=10),
                    sequence=3,
                    cpu=3.0,
                ),
            ]
        )
    projection = FleetProjection(repository, sessions, clock=lambda: NOW)

    history = projection.telemetry_history(
        NODE_A,
        start=NOW - timedelta(hours=1),
        end=NOW,
        maximum_points=2,
    )

    document = history.model_dump(mode="json")
    assert {key: value for key, value in document.items() if key != "points"} == {
        "schema_version": 1,
        "node_id": NODE_A,
        "start": "2026-08-15T11:00:00Z",
        "end": "2026-08-15T12:00:00Z",
        "maximum_points": 2,
    }
    assert [
        (
            point["id"],
            point["sequence"],
            point["observed_at"],
            point["cpu_utilization_percent"],
        )
        for point in document["points"]
    ] == [
        (
            "00000000-0000-4000-8000-000000000202",
            2,
            "2026-08-15T11:40:00Z",
            2.0,
        ),
        (
            "00000000-0000-4000-8000-000000000203",
            3,
            "2026-08-15T11:50:00Z",
            3.0,
        ),
    ]
    with pytest.raises(KeyError, match=EXTRA_NODE):
        projection.telemetry_history(
            EXTRA_NODE,
            start=NOW - timedelta(hours=1),
            end=NOW,
            maximum_points=2,
        )
    with pytest.raises(ValueError, match="maximum points"):
        projection.telemetry_history(
            NODE_A,
            start=NOW - timedelta(hours=1),
            end=NOW,
            maximum_points=1_501,
        )
    with pytest.raises(ValueError, match="raw window"):
        projection.telemetry_history(
            NODE_A,
            start=NOW - timedelta(hours=24, microseconds=1),
            end=NOW,
            maximum_points=2,
        )


def test_projection_selects_only_the_latest_512_current_installation_groups() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    recipe_id = "00000000-0000-4000-8000-000000000301"
    revision_id = "00000000-0000-4000-8000-000000000302"
    mapping_id = "00000000-0000-4000-8000-000000000303"
    build_id = "00000000-0000-4000-8000-000000000304"
    nodes = {
        NODE_A: {
            "display_name": "Alpha",
            "hostname": "alpha.internal",
            "lifecycle": "managed",
            "labels": {},
        }
    }
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=NODE_A,
                state="active",
                architecture="linux-arm64",
                capabilities=[],
                last_seen_at=NOW,
            )
        )
        session.add(
            LocalRecipe(
                id=recipe_id,
                slug="bounded-recipe",
                title="Bounded Recipe",
                description="Bounded groups",
                source_kind="local",
                created_by="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            LocalRecipeRevision(
                id=revision_id,
                recipe_id=recipe_id,
                revision_number=1,
                lifecycle="resolved",
                schema_version=1,
                document={},
                content_sha256="1" * 64,
                created_by="admin",
                created_at=NOW,
            )
        )
        session.add(
            ClusterMapping(
                id=mapping_id,
                recipe_revision_id=revision_id,
                profile_name="solo",
                generation=1,
                node_count=1,
                state="ready",
                parameters={},
                placement_digest="2" * 64,
                endpoint_owner_node_id=NODE_A,
                created_by="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            RecipeBuild(
                id=build_id,
                recipe_revision_id=revision_id,
                builder_node_id=NODE_A,
                source_bundle_sha256="3" * 64,
                build_input_sha256="4" * 64,
                state="succeeded",
                policy_report={},
                plan={},
                image_digest="sha256:" + "5" * 64,
                oci_layout_sha256="6" * 64,
                image_bytes=100,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.flush()
        session.add(
            ClusterMappingNode(
                id="00000000-0000-4000-8000-000000000305",
                mapping_id=mapping_id,
                node_id=NODE_A,
                rank=0,
                role="entrypoint",
                endpoint_owner=True,
                created_at=NOW,
            )
        )
        for index in range(513):
            installation_id = f"install-{index:03d}"
            updated_at = NOW + timedelta(seconds=index)
            session.add(
                RecipeInstallation(
                    id=installation_id,
                    recipe_revision_id=revision_id,
                    mapping_id=mapping_id,
                    mapping_generation=1,
                    recipe_build_id=build_id,
                    image_digest="sha256:" + "5" * 64,
                    plan_digest=f"{index + 16:064x}",
                    plan={},
                    state="installed",
                    actor="admin",
                    created_at=updated_at,
                    updated_at=updated_at,
                )
            )
            session.add(
                InstallationNode(
                    id=f"rank-{index:03d}",
                    installation_id=installation_id,
                    node_id=NODE_A,
                    rank=0,
                    role="entrypoint",
                    state="installed",
                    required_bytes=100,
                    installed_bytes=100,
                    updated_at=updated_at,
                )
            )

    snapshot = FleetProjection(
        Repository(nodes), sessions, clock=lambda: NOW
    ).read()

    installation_ids = [value.installation_id for value in snapshot.nodes[0].installed]
    assert len(installation_ids) == 512
    assert installation_ids[0] == "install-001"
    assert installation_ids[-1] == "install-512"
    assert "install-000" not in installation_ids


def test_projection_rejects_more_than_500_repository_nodes_before_state_queries() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    nodes = {
        f"spk_{index:032x}": {
            "display_name": f"Node {index}",
            "hostname": f"node-{index}.internal",
            "lifecycle": "managed",
            "labels": {},
        }
        for index in range(1, 502)
    }
    statements: list[str] = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(" ".join(statement.split()).lower())

    event.listen(engine, "before_cursor_execute", record_statement)
    with pytest.raises(ValueError, match="more than 500 nodes"):
        FleetProjection(Repository(nodes), sessions, clock=lambda: NOW).read()
    event.remove(engine, "before_cursor_execute", record_statement)

    assert [value for value in statements if value.startswith("select")] == [
        (
            "select fleet_event_cursor.last_id from fleet_event_cursor "
            "where fleet_event_cursor.singleton_id = ?"
        )
    ]
