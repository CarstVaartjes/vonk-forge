from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, event, update
from sqlalchemy.orm import sessionmaker
from vonk_control.fleet_projection import (
    CapacityReservations,
    FleetProjection,
    FleetSnapshot,
    NodeConnection,
    RecipePresence,
    TelemetryDetails,
    TelemetryPoint,
)
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentNodeProfile,
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
NON_RFC_BOOT_ID = "00000000-0000-0000-0000-000000000001"


class Repository:
    def __init__(self, nodes: dict[str, dict[str, object]] | None = None) -> None:
        self.calls: list[str] = []
        self.nodes = (
            nodes
            if nodes is not None
            else {
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
        )

    def head(self) -> str:
        self.calls.append("head")
        return COMMIT

    def read_document(self, commit: str, path: str) -> SimpleNamespace:
        raise AssertionError(f"unexpected document read: {commit} {path}")


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
    boot_id: str = "00000000-0000-4000-8000-000000000001",
) -> NodeTelemetrySample:
    return NodeTelemetrySample(
        id=sample_id,
        node_id=node_id,
        boot_id=boot_id,
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


def _certificate(
    node_id: str,
    serial: str,
    *,
    generation: int = 1,
    state: str = "active",
    not_before: datetime = NOW - timedelta(days=1),
    not_after: datetime = NOW + timedelta(days=1),
    revoked_at: datetime | None = None,
    ca_revoked_at: datetime | None = None,
) -> AgentCertificate:
    return AgentCertificate(
        serial=serial,
        node_id=node_id,
        not_before=not_before,
        not_after=not_after,
        fingerprint=f"fingerprint-{serial}",
        state=state,
        generation=generation,
        revoked_at=revoked_at,
        ca_revoked_at=ca_revoked_at,
    )


def _profile(
    node_id: str,
    *,
    display_name: str,
    hostname: str,
    lifecycle: str = "managed",
    labels: dict[str, str] | None = None,
) -> AgentNodeProfile:
    return AgentNodeProfile(
        node_id=node_id,
        display_name=display_name,
        hostname=hostname,
        lifecycle=lifecycle,
        labels={} if labels is None else labels,
    )


def test_fleet_is_empty_when_repository_has_old_nodes_but_database_has_no_agents() -> (
    None
):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)

    projection = FleetProjection(Repository(), sessions, clock=lambda: NOW)

    assert projection.read().nodes == []


def test_fleet_contains_registered_node_absent_from_repository() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=NODE_A,
                state="active",
                capabilities=[],
                last_seen_at=NOW,
            )
        )

    projection = FleetProjection(Repository({}), sessions, clock=lambda: NOW)

    snapshot = projection.read()
    assert [node.id for node in snapshot.nodes] == ["spk_" + "1" * 32]
    assert snapshot.nodes[0].display_name == "spk_" + "1" * 32


def test_fleet_excludes_revoked_agent_nodes() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=NODE_A,
                state="revoked",
                capabilities=[],
                last_seen_at=NOW,
                revoked_at=NOW,
            )
        )

    projection = FleetProjection(
        Repository(
            {
                NODE_A: {
                    "display_name": "Alpha",
                    "hostname": "alpha.internal",
                    "lifecycle": "managed",
                    "labels": {},
                }
            }
        ),
        sessions,
        clock=lambda: NOW,
    )

    assert [node.id for node in projection.read().nodes] == []


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
                    state="revoked",
                    architecture="linux-arm64",
                    capabilities=[],
                    last_seen_at=NOW,
                    revoked_at=NOW,
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                _profile(
                    NODE_A,
                    display_name="Alpha",
                    hostname="alpha.internal",
                    labels={"rack": "left"},
                ),
                _profile(
                    NODE_B,
                    display_name="Beta",
                    hostname="beta.internal",
                    labels={"rack": "right"},
                ),
            ]
        )
        session.add_all(
            [
                _certificate(NODE_A, "bounded-a"),
                _certificate(NODE_B, "bounded-b"),
                _certificate(EXTRA_NODE, "bounded-external"),
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

    assert repository.calls == ["head"]
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
                    "certificate_state": "valid",
                    "online_state": "online",
                    "offline_reason": None,
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
                    "certificate_state": "valid",
                    "online_state": "online",
                    "offline_reason": None,
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
    assert len(selects) == 10
    certificate_reads = [
        statement for statement in selects if "agent_certificates" in statement
    ]
    assert len(certificate_reads) == 1
    assert (
        "row_number() over (partition by agent_certificates.node_id"
        in (certificate_reads[0])
    )
    telemetry_reads = [
        statement for statement in selects if "node_telemetry_samples" in statement
    ]
    assert len(telemetry_reads) == 1
    assert "node_telemetry_latest" in telemetry_reads[0]
    inventory_reads = [
        statement for statement in selects if "node_inventory_snapshots" in statement
    ]
    assert len(inventory_reads) == 1
    assert (
        "row_number() over (partition by node_inventory_snapshots.node_id"
        in (inventory_reads[0])
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


def test_projection_dtos_reject_coercion_unbounded_values_and_open_vocabularies() -> (
    None
):
    with pytest.raises(ValidationError, match="event_cursor"):
        FleetSnapshot(
            event_cursor="1",
            generated_at=NOW,
            repository_commit=COMMIT,
            nodes=[],
        )
    with pytest.raises(ValidationError, match="disk_bytes"):
        CapacityReservations(
            disk_bytes=9_223_372_036_854_775_808,
            unified_memory_bytes=0,
            host_memory_bytes=0,
            gpu_memory_bytes=0,
            port_count=0,
        )
    with pytest.raises(ValidationError, match="agent_state"):
        NodeConnection(
            agent_state="invented",
            certificate_state="valid",
            online_state="online",
            offline_reason=None,
            last_seen_at=NOW,
            last_seen_age_seconds=0.0,
        )
    presence = {
        "installation_id": "00000000-0000-4000-8000-000000000001",
        "recipe_id": "00000000-0000-4000-8000-000000000002",
        "recipe_revision_id": "00000000-0000-4000-8000-000000000003",
        "title": "Recipe",
        "topology_name": "pair",
        "expected_rank_count": 1,
        "present_ranks": [0],
        "member_node_ids": [NODE_A],
        "rank": 0,
        "role": "entrypoint",
        "group_state": "installed",
        "rank_state": "installed",
        "complete": True,
        "degraded_reason": None,
    }
    with pytest.raises(ValidationError, match="member_node_ids"):
        RecipePresence(**{**presence, "member_node_ids": ["external-node"]})
    with pytest.raises(ValidationError, match="role"):
        RecipePresence(**{**presence, "role": "x" * 65})
    with pytest.raises(ValidationError, match="degraded_reason"):
        RecipePresence(**{**presence, "degraded_reason": "invented"})

    point = {
        "id": "00000000-0000-4000-8000-000000000004",
        "node_id": NODE_A,
        "boot_id": "00000000-0000-4000-8000-000000000005",
        "sequence": 1,
        "observed_at": NOW,
        "received_at": NOW,
        "gap_samples": 0,
        "details": TelemetryDetails(),
    }
    with pytest.raises(ValidationError, match="memory_total_bytes"):
        TelemetryPoint(**{**point, "memory_total_bytes": 16 * 1024**4 + 1})
    with pytest.raises(ValidationError, match="load_average_1m"):
        TelemetryPoint(**{**point, "load_average_1m": 1_000_000.01})
    with pytest.raises(ValidationError, match="temperature_c"):
        TelemetryPoint(**{**point, "temperature_c": float("nan")})


@pytest.mark.parametrize(
    "boot_id",
    [
        "00000000-0000-0000-0000-000000000000",
        "00000000-0000-0000-0000-00000000000A",
        "00000000000000000000000000000001",
    ],
    ids=["nil", "uppercase", "compact"],
)
def test_fleet_telemetry_dto_rejects_nil_and_noncanonical_boot_ids(
    boot_id: str,
) -> None:
    with pytest.raises(ValidationError, match="boot_id"):
        TelemetryPoint(
            id="00000000-0000-4000-8000-000000000004",
            node_id=NODE_A,
            boot_id=boot_id,
            sequence=1,
            observed_at=NOW,
            received_at=NOW,
            gap_samples=0,
            details=TelemetryDetails(),
        )


def test_projection_schema_is_finite_for_states_items_and_task3_numbers() -> None:
    definitions = FleetSnapshot.model_json_schema()["$defs"]

    assert definitions["NodeConnection"]["properties"]["agent_state"] == {
        "enum": ["unregistered", "pending", "active", "retired", "revoked"],
        "title": "Agent State",
        "type": "string",
    }
    assert definitions["RecipePresence"]["properties"]["group_state"]["enum"] == [
        "planned",
        "installing",
        "installed",
        "partial",
        "failed",
        "uninstalled",
    ]
    assert definitions["RunPresence"]["properties"]["run_state"]["enum"] == [
        "planned",
        "starting",
        "running",
        "stopping",
        "stopped",
        "failed",
        "lost",
    ]
    assert (
        definitions["RecipePresence"]["properties"]["member_node_ids"]["items"][
            "pattern"
        ]
        == "^spk_[0-9a-f]{32}$"
    )
    telemetry = definitions["TelemetryPoint"]["properties"]
    assert telemetry["boot_id"]["pattern"] == (
        "^(?!00000000-0000-0000-0000-000000000000$)"
        "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    assert telemetry["load_average_1m"]["anyOf"][0]["maximum"] == 1_000_000
    assert telemetry["memory_total_bytes"]["anyOf"][0]["maximum"] == (16 * 1024**4)
    assert telemetry["temperature_c"]["anyOf"][0] == {
        "maximum": 300.0,
        "minimum": -100.0,
        "type": "number",
    }
    assert (
        telemetry["network_receive_bytes_per_second"]["anyOf"][0]["maximum"]
        == 1_000_000_000_000_000
    )


def test_connection_uses_certificate_authority_and_finite_offline_precedence() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    node_ids = [f"spk_{index:032x}" for index in range(1, 13)]
    nodes = {
        node_id: {
            "display_name": node_id,
            "hostname": "node.internal",
            "lifecycle": "managed",
            "labels": {},
        }
        for node_id in node_ids
    }
    with sessions.begin() as session:
        session.add_all(
            [
                AgentNode(
                    node_id=node_ids[1],
                    state="revoked",
                    capabilities=[],
                    last_seen_at=NOW,
                    revoked_at=NOW,
                ),
                AgentNode(
                    node_id=node_ids[2],
                    state="pending",
                    capabilities=[],
                    last_seen_at=NOW,
                ),
                AgentNode(
                    node_id=node_ids[3],
                    state="active",
                    capabilities=[],
                    last_seen_at=NOW,
                ),
                AgentNode(
                    node_id=node_ids[4],
                    state="active",
                    capabilities=[],
                    last_seen_at=NOW,
                ),
                AgentNode(
                    node_id=node_ids[5],
                    state="active",
                    capabilities=[],
                    last_seen_at=NOW,
                ),
                AgentNode(
                    node_id=node_ids[6],
                    state="active",
                    capabilities=[],
                    last_seen_at=NOW,
                ),
                AgentNode(
                    node_id=node_ids[7],
                    state="active",
                    capabilities=[],
                    last_seen_at=NOW,
                ),
                AgentNode(node_id=node_ids[8], state="active", capabilities=[]),
                AgentNode(
                    node_id=node_ids[9],
                    state="active",
                    capabilities=[],
                    last_seen_at=NOW + timedelta(microseconds=1),
                ),
                AgentNode(
                    node_id=node_ids[10],
                    state="active",
                    capabilities=[],
                    last_seen_at=NOW - timedelta(seconds=151),
                ),
                AgentNode(
                    node_id=node_ids[11],
                    state="active",
                    capabilities=[],
                    last_seen_at=NOW,
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                _certificate(node_ids[1], "agent-revoked-valid"),
                _certificate(
                    node_ids[2],
                    "agent-inactive-revoked",
                    ca_revoked_at=NOW,
                ),
                _certificate(
                    node_ids[4],
                    "certificate-revoked",
                    ca_revoked_at=NOW,
                ),
                _certificate(node_ids[5], "certificate-inactive", state="staged"),
                _certificate(
                    node_ids[6],
                    "certificate-future",
                    not_before=NOW + timedelta(microseconds=1),
                ),
                _certificate(
                    node_ids[7],
                    "certificate-expired",
                    not_after=NOW,
                ),
                _certificate(node_ids[8], "never-seen-valid"),
                _certificate(node_ids[9], "future-seen-valid"),
                _certificate(node_ids[10], "stale-valid"),
                _certificate(node_ids[11], "valid-older", generation=1),
                _certificate(
                    node_ids[11],
                    "staged-newer",
                    generation=2,
                    state="staged",
                    not_after=NOW + timedelta(days=2),
                ),
            ]
        )

    snapshot = FleetProjection(Repository(nodes), sessions, clock=lambda: NOW).read()

    assert [
        (
            node.connection.agent_state,
            node.connection.certificate_state,
            node.connection.online_state,
            node.connection.offline_reason,
        )
        for node in snapshot.nodes
    ] == [
        ("pending", "revoked", "offline", "agent-inactive"),
        ("active", "missing", "offline", "certificate-missing"),
        ("active", "revoked", "offline", "certificate-revoked"),
        ("active", "inactive", "offline", "certificate-inactive"),
        ("active", "not-yet-valid", "offline", "certificate-not-yet-valid"),
        ("active", "expired", "offline", "certificate-expired"),
        ("active", "valid", "offline", "never-seen"),
        ("active", "valid", "offline", "last-seen-in-future"),
        ("active", "valid", "offline", "stale"),
        ("active", "valid", "online", None),
    ]


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
            session.flush()
            session.add(_certificate(node_id, f"freshness-{index}"))
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

    snapshot = FleetProjection(Repository(nodes), sessions, clock=lambda: NOW).read()

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
        session.flush()
        session.add_all(
            [
                _certificate(NODE_A, "groups-a"),
                _certificate(NODE_B, "groups-b"),
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
            topology_name="pair",
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

    snapshot = FleetProjection(Repository(nodes), sessions, clock=lambda: NOW).read()
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
    degraded = next(value for value in alpha.loaded if value.run_id == degraded_run_id)
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

    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_B)
        assert node is not None
        node.state = "revoked"
        node.revoked_at = NOW

    repository_only = FleetProjection(
        Repository({NODE_A: nodes[NODE_A]}), sessions, clock=lambda: NOW
    ).read().nodes[0]
    external_install = next(
        value
        for value in repository_only.installed
        if value.installation_id == complete_installation_id
    )
    external_run = next(
        value for value in repository_only.loaded if value.run_id == healthy_run_id
    )

    assert (
        external_install.present_ranks,
        external_install.member_node_ids,
        external_install.complete,
        external_install.degraded_reason,
    ) == ([0], [NODE_A], False, "external-member")
    assert (
        external_run.present_ranks,
        external_run.member_node_ids,
        external_run.healthy,
        external_run.group_state,
        external_run.degraded_reason,
    ) == ([0], [NODE_A], False, "degraded", "external-member")


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
        resolution="raw",
    )

    document = history.model_dump(mode="json")
    assert {key: value for key, value in document.items() if key != "points"} == {
        "schema_version": 1,
        "node_id": NODE_A,
        "start": "2026-08-15T11:00:00Z",
        "end": "2026-08-15T12:00:00Z",
        "resolution": "raw",
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
            resolution="raw",
        )
    with pytest.raises(ValueError, match="maximum points"):
        projection.telemetry_history(
            NODE_A,
            start=NOW - timedelta(hours=1),
            end=NOW,
            maximum_points=1_501,
            resolution="raw",
        )
    with pytest.raises(ValueError, match="raw window"):
        projection.telemetry_history(
            NODE_A,
            start=NOW - timedelta(hours=24, microseconds=1),
            end=NOW,
            maximum_points=2,
            resolution="raw",
        )


def test_non_rfc_non_nil_boot_id_flows_through_snapshot_and_history() -> None:
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
        session.add(AgentNode(node_id=NODE_A, state="active", capabilities=[]))
        sample = _telemetry(
            NODE_A,
            "00000000-0000-4000-8000-000000000299",
            NOW - timedelta(seconds=1),
            sequence=1,
            cpu=1.0,
            boot_id=NON_RFC_BOOT_ID,
        )
        session.add(sample)
        session.flush()
        session.add(NodeTelemetryLatest(node_id=NODE_A, sample_id=sample.id))

    projection = FleetProjection(repository, sessions, clock=lambda: NOW)
    snapshot = projection.read()
    history = projection.telemetry_history(
        NODE_A,
        start=NOW - timedelta(minutes=1),
        end=NOW,
        maximum_points=1,
        resolution="raw",
    )

    assert snapshot.nodes[0].telemetry.sample.boot_id == NON_RFC_BOOT_ID
    assert [point.boot_id for point in history.points] == [NON_RFC_BOOT_ID]


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
                topology_name="solo",
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

    snapshot = FleetProjection(Repository(nodes), sessions, clock=lambda: NOW).read()

    installation_ids = [value.installation_id for value in snapshot.nodes[0].installed]
    assert len(installation_ids) == 512
    assert installation_ids[0] == "install-001"
    assert installation_ids[-1] == "install-512"
    assert "install-000" not in installation_ids


def test_projection_rejects_more_than_500_registered_nodes_before_state_queries() -> (
    None
):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add_all(
            [
                AgentNode(
                    node_id=f"spk_{index:032x}",
                    state="active",
                    capabilities=[],
                )
                for index in range(1, 502)
            ]
        )
    statements: list[str] = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(" ".join(statement.split()).lower())

    event.listen(engine, "before_cursor_execute", record_statement)
    with pytest.raises(ValueError, match="more than 500 registered nodes"):
        FleetProjection(Repository({}), sessions, clock=lambda: NOW).read()
    event.remove(engine, "before_cursor_execute", record_statement)

    selects = [value for value in statements if value.startswith("select")]
    assert len(selects) == 2
    assert "from fleet_event_cursor" in selects[0]
    assert "from agent_nodes" in selects[1]
    assert "agent_node_profiles" not in " ".join(selects)
