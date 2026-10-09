from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError
from sqlalchemy import Table, create_engine, event, literal, select, text, update
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import canonical_message
from vonk_control.fleet_events import FleetEventRepository
from vonk_control.fleet_projection import (
    _AUTHORITY_REVISION,
    CapacityReservations,
    FleetProjection,
    FleetSnapshot,
    NodeConnection,
    RecipePresence,
    TelemetryPoint,
)
from vonk_control.fleet_stream_contract import FleetChangeEvent
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentNodeProfile,
    AgentPresence,
    Base,
    CatalogDocument,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    FleetEventCursor,
    FleetStreamEvent,
    InstallationNode,
    NodeInventorySnapshot,
    NodeTelemetryLatest,
    NodeTelemetrySample,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from vonk_control.recipe_execution_contract import installation_plan_document
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, document_sha256

from tests.observation_transfer_peer import observation_document

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
COMMIT = "a" * 64
NODE_A = "spk_" + "1" * 32
NODE_B = "spk_" + "2" * 32
NODE_C = "spk_" + "3" * 32
NODE_D = "spk_" + "4" * 32
EXTRA_NODE = "spk_" + "f" * 32
NON_RFC_BOOT_ID = "00000000-0000-0000-0000-000000000001"


def _canonical_catalog_documents(
    recipe_id: str,
    revision_id: str,
    model_id: str,
    model_revision_id: str,
    *,
    slug: str,
    title: str,
    interfaces: list[dict[str, object]] | None = None,
) -> tuple[list[CatalogDocument], list[CatalogDocumentRevision]]:
    model = ModelDefinition.model_validate(
        json.loads(
            files("vonk_forge_contracts")
            .joinpath("examples", "model-definition.json")
            .read_text(encoding="utf-8")
        )
    )
    recipe_document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-source-build.json")
        .read_text(encoding="utf-8")
    )
    recipe_document["identity"]["slug"] = slug
    recipe_document["metadata"]["title"] = title
    recipe_document["models"][0]["model"]["content_sha256"] = document_sha256(
        model.model_dump(mode="json")
    )
    if interfaces is not None:
        recipe_document["interfaces"] = interfaces
    recipe = RecipeDefinition.model_validate(recipe_document)
    model_document = model.model_dump(mode="json")
    canonical_recipe = recipe.model_dump(mode="json")
    return (
        [
            CatalogDocument(
                id=recipe_id,
                kind="recipe",
                publisher=recipe.identity.publisher,
                slug=recipe.identity.slug,
                title=recipe.metadata.title,
                created_by="admin",
                created_at=NOW,
                updated_at=NOW,
            ),
            CatalogDocument(
                id=model_id,
                kind="model",
                publisher=model.identity.publisher,
                slug=model.identity.slug,
                title=model.identity.model.title,
                created_by="admin",
                created_at=NOW,
                updated_at=NOW,
            ),
        ],
        [
            CatalogDocumentRevision(
                id=revision_id,
                document_id=recipe_id,
                kind="recipe",
                publisher=recipe.identity.publisher,
                slug=recipe.identity.slug,
                revision_number=1,
                schema_version=2,
                state="active",
                document=canonical_recipe,
                content_digest=document_sha256(recipe.model_dump(mode="json")),
                projected={},
                created_by="admin",
                created_at=NOW,
            ),
            CatalogDocumentRevision(
                id=model_revision_id,
                document_id=model_id,
                kind="model",
                publisher=model.identity.publisher,
                slug=model.identity.slug,
                revision_number=1,
                schema_version=2,
                state="active",
                document=model_document,
                content_digest=document_sha256(model.model_dump(mode="json")),
                projected={},
                created_by="admin",
                created_at=NOW,
            ),
        ],
    )


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
        capabilities=["runtime.vonk.v1", "recipe.image.pull.v1"],
        evidence_digest=((suffix + f"{free_bytes:x}") * 64)[:64],
        memory_pool="separate",
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
        observed_at=observed_at,
        received_at=observed_at + timedelta(milliseconds=250),
        gpu_utilization_percent=cpu,
        memory_total_bytes=2_000,
        memory_available_bytes=1_400,
        disk_total_bytes=1_000,
        disk_free_bytes=700,
        gpu_memory_total_bytes=2_000,
        gpu_memory_free_bytes=1_300,
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

    projection = FleetProjection(sessions, clock=lambda: NOW)

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
                last_seen_at=NOW,
            )
        )

    projection = FleetProjection(sessions, clock=lambda: NOW)

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
                last_seen_at=NOW,
                revoked_at=NOW,
            )
        )

    projection = FleetProjection(
        sessions,
        clock=lambda: NOW,
    )

    assert [node.id for node in projection.read().nodes] == []


def test_read_uses_postgresql_registration_latest_rows_and_a_bounded_query_set() -> (
    None
):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
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
                    last_seen_at=NOW - timedelta(seconds=5),
                ),
                AgentNode(
                    node_id=NODE_B,
                    state="active",
                    architecture="linux-arm64",
                    last_seen_at=NOW - timedelta(seconds=7),
                ),
                AgentNode(
                    node_id=EXTRA_NODE,
                    state="revoked",
                    architecture="linux-arm64",
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
        session.add(
            AgentPresence(
                node_id=NODE_A,
                certificate_serial="bounded-a",
                certificate_fingerprint="fingerprint-bounded-a",
                management_address="192.168.1.211",
                observed_at=NOW,
            )
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
    snapshot = FleetProjection(sessions, clock=lambda: NOW).read()
    event.remove(engine, "before_cursor_execute", record_statement)

    assert snapshot.model_dump(mode="json") == {
        "event_cursor": 7,
        "generated_at": "2026-08-15T12:00:00Z",
        "authority_revision": _AUTHORITY_REVISION,
        "nodes": [
            {
                "id": NODE_A,
                "display_name": "Alpha",
                "hostname": "alpha.internal",
                "ip_address": "192.168.1.211",
                "lifecycle": "managed",
                "labels": {"rack": "left"},
                "projection_issues": None,
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
                    "capabilities": ["runtime.vonk.v1", "recipe.image.pull.v1"],
                    "fabric_address": "10.0.0.1",
                    "fabric_bandwidth_mbps": 100000,
                    "nvidia_driver_version": "580.1",
                    "container_runtime_version": "1.2.3",
                    "network_interfaces": None,
                    "nas_route_interface": None,
                },
                "telemetry": {
                    "age_seconds": 2.0,
                    "freshness": "live",
                    "sample": {
                        "id": "00000000-0000-4000-8000-000000000012",
                        "node_id": NODE_A,
                        "boot_id": "00000000-0000-4000-8000-000000000001",
                        "observed_at": "2026-08-15T11:59:58Z",
                        "received_at": "2026-08-15T11:59:58.250000Z",
                        "memory_total_bytes": 2000,
                        "memory_available_bytes": 1400,
                        "disk_total_bytes": 1000,
                        "disk_free_bytes": 700,
                        "gpu_unavailable_reason": None,
                        "gpu_utilization_percent": 12.5,
                        "gpu_memory_total_bytes": 2000,
                        "gpu_memory_free_bytes": 1300,
                        "gpu_temperature_c": None,
                        "cpu_frequency_avg_mhz": None,
                        "cpu_frequency_min_mhz": None,
                        "cpu_frequency_max_mhz": None,
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
                "ip_address": None,
                "lifecycle": "managed",
                "labels": {"rack": "right"},
                "projection_issues": None,
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
                    "capabilities": ["runtime.vonk.v1", "recipe.image.pull.v1"],
                    "fabric_address": "10.0.0.2",
                    "fabric_bandwidth_mbps": 100000,
                    "nvidia_driver_version": "580.1",
                    "container_runtime_version": "1.2.3",
                    "network_interfaces": None,
                    "nas_route_interface": None,
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
                        "install_partial": None,
                        "recommendation": None,
                    }
                ],
            },
        ],
    }
    assert EXTRA_NODE not in {node.id for node in snapshot.nodes}
    # A fresh read consumes the same committed observations without mutating
    # registrations, reservations or their reported capacity.
    again = FleetProjection(sessions, clock=lambda: NOW).read()
    assert again.nodes == snapshot.nodes


def test_display_name_update_preserves_identity_and_emits_projection_refresh() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        node = AgentNode(node_id=NODE_A, state="active")
        session.add(node)
        session.flush()
        session.add(
            _profile(
                NODE_A,
                display_name=NODE_A,
                hostname="spark-3542.internal",
            )
        )
        session.add(_certificate(NODE_A, "profile-a"))
        session.add(
            AgentPresence(
                node_id=NODE_A,
                certificate_serial="profile-a",
                certificate_fingerprint="fingerprint-profile-a",
                management_address="192.168.1.211",
                observed_at=NOW,
            )
        )

    projection = FleetProjection(sessions, clock=lambda: NOW)
    identity = projection.update_display_name(NODE_A, "Studio Spark")

    assert identity.model_dump() == {
        "id": NODE_A,
        "display_name": "Studio Spark",
        "hostname": "spark-3542.internal",
        "ip_address": "192.168.1.211",
    }
    snapshot = projection.read()
    assert snapshot.nodes[0].display_name == "Studio Spark"
    assert snapshot.nodes[0].ip_address == "192.168.1.211"
    assert snapshot.event_cursor == 1
    with sessions() as session:
        event = session.get(FleetStreamEvent, 1)
        assert event is not None
        FleetChangeEvent.model_validate(
            {
                "event_cursor": event.id,
                "projection_refresh_required": True,
                "change": {
                    "entity_kind": event.entity_kind,
                    "entity_id": event.entity_id,
                    "node_id": event.node_id,
                    "occurred_at": event.occurred_at.replace(tzinfo=UTC),
                    "fields": event.payload,
                },
            }
        )


def test_read_captures_the_committed_cursor() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    order: list[str] = []

    class Events(FleetEventRepository):
        def high_watermark_in_session(self, session) -> int:
            order.append("watermark")
            return 41

    snapshot = FleetProjection(
        sessions,
        clock=lambda: NOW,
        events=Events(sessions, clock=lambda: NOW),
    ).read()

    assert order == ["watermark"]
    assert snapshot.event_cursor == 41


def test_projection_dtos_reject_coercion_unbounded_values_and_open_vocabularies() -> (
    None
):
    with pytest.raises(ValidationError):
        FleetSnapshot.model_validate(
            {
                "event_cursor": "1",
                "generated_at": NOW,
                "authority_revision": COMMIT,
                "nodes": [],
            }
        )
    with pytest.raises(ValidationError):
        CapacityReservations(
            disk_bytes=9_223_372_036_854_775_808,
            unified_memory_bytes=0,
            host_memory_bytes=0,
            gpu_memory_bytes=0,
            port_count=0,
        )
    with pytest.raises(ValidationError):
        NodeConnection.model_validate(
            {
                "agent_state": "invented",
                "certificate_state": "valid",
                "online_state": "online",
                "offline_reason": None,
                "last_seen_at": NOW,
                "last_seen_age_seconds": 0.0,
            }
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
    with pytest.raises(ValidationError):
        RecipePresence(**{**presence, "member_node_ids": ["external-node"]})
    with pytest.raises(ValidationError):
        RecipePresence(**{**presence, "role": "x" * 65})
    with pytest.raises(ValidationError):
        RecipePresence(**{**presence, "degraded_reason": "invented"})

    point = {
        "id": "00000000-0000-4000-8000-000000000004",
        "node_id": NODE_A,
        "boot_id": "00000000-0000-4000-8000-000000000005",
        "observed_at": NOW,
        "received_at": NOW,
    }
    with pytest.raises(ValidationError):
        TelemetryPoint(**{**point, "memory_total_bytes": 16 * 1024**4 + 1})
    with pytest.raises(ValidationError):
        TelemetryPoint(**{**point, "gpu_utilization_percent": 100.01})


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
    with pytest.raises(ValidationError):
        TelemetryPoint(
            id="00000000-0000-4000-8000-000000000004",
            node_id=NODE_A,
            boot_id=boot_id,
            observed_at=NOW,
            received_at=NOW,
        )


def test_projection_schema_is_finite_for_states_items_and_task3_numbers() -> None:
    definitions = FleetSnapshot.model_json_schema()["$defs"]

    assert definitions["NodeConnection"]["properties"]["agent_state"] == {
        "enum": ["unregistered", "pending", "active", "retired", "revoked"],
        "title": "Agent State",
        "type": "string",
    }
    assert definitions["RecipePresence"]["properties"]["group_state"] == {
        "$ref": "#/$defs/InstallationState"
    }
    assert definitions["InstallationState"]["enum"] == [
        "planned",
        "installing",
        "installed",
        "partial",
        "failed",
        "uninstalled",
    ]
    assert definitions["RunPresence"]["properties"]["run_state"] == {
        "$ref": "#/$defs/RunState"
    }
    assert definitions["RunState"]["enum"] == [
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
    assert telemetry["memory_total_bytes"]["anyOf"][0]["maximum"] == (16 * 1024**4)


def test_connection_uses_certificate_authority_and_finite_offline_precedence() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    node_ids = [f"spk_{index:032x}" for index in range(1, 13)]
    {
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
                    last_seen_at=NOW,
                    revoked_at=NOW,
                ),
                AgentNode(
                    node_id=node_ids[2],
                    state="pending",
                    last_seen_at=NOW,
                ),
                AgentNode(
                    node_id=node_ids[3],
                    state="active",
                    last_seen_at=NOW,
                ),
                AgentNode(
                    node_id=node_ids[4],
                    state="active",
                    last_seen_at=NOW,
                ),
                AgentNode(
                    node_id=node_ids[5],
                    state="active",
                    last_seen_at=NOW,
                ),
                AgentNode(
                    node_id=node_ids[6],
                    state="active",
                    last_seen_at=NOW,
                ),
                AgentNode(
                    node_id=node_ids[7],
                    state="active",
                    last_seen_at=NOW,
                ),
                AgentNode(node_id=node_ids[8], state="active"),
                AgentNode(
                    node_id=node_ids[9],
                    state="active",
                    last_seen_at=NOW + timedelta(microseconds=1),
                ),
                AgentNode(
                    node_id=node_ids[10],
                    state="active",
                    last_seen_at=NOW - timedelta(seconds=151),
                ),
                AgentNode(
                    node_id=node_ids[11],
                    state="active",
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

    snapshot = FleetProjection(sessions, clock=lambda: NOW).read()

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

    snapshot = FleetProjection(sessions, clock=lambda: NOW).read()

    assert [
        (
            node.id,
            node.telemetry.freshness if node.telemetry else None,
            node.connection.online_state,
            node.inventory.freshness if node.inventory else None,
        )
        for node in snapshot.nodes
    ] == [
        (NODE_A, "live", "online", "fresh"),
        (
            NODE_B,
            "delayed",
            "online",
            "stale",
        ),
        (NODE_C, "delayed", "online", "fresh"),
        (
            NODE_D,
            "stale",
            "offline",
            "fresh",
        ),
    ]


def test_installed_and_loaded_groups_require_every_exact_current_rank(capsys) -> None:
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
    with sessions.begin() as session:
        session.add_all(
            [
                AgentNode(
                    node_id=NODE_A,
                    state="active",
                    architecture="linux-arm64",
                    last_seen_at=NOW,
                ),
                AgentNode(
                    node_id=NODE_B,
                    state="active",
                    architecture="linux-arm64",
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
        documents, revisions = _canonical_catalog_documents(
            recipe_id,
            revision_id,
            "00000000-0000-4000-8000-000000000305",
            "00000000-0000-4000-8000-000000000306",
            slug="pair-recipe",
            title="Pair Recipe",
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
        session.add_all(documents)
        session.flush()
        session.add_all([*revisions, mapping, build])
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

    snapshot = FleetProjection(sessions, clock=lambda: NOW).read()
    from cluster_profiles.cli_render import render_payload

    render_payload(snapshot.model_dump(mode="json"), "fleet")
    rendered = capsys.readouterr()
    assert "3 workloads running" in rendered.out
    assert rendered.out.count("Recipe: Pair Recipe") == 3
    attention = rendered.out[rendered.out.index("Needs attention") :]
    assert "pair-healthy" not in attention
    assert "pair-stale" in attention and "rank-stale" in attention
    assert "pair-route-failed" in attention and "route-not-published" in attention
    assert "incomplete" in attention
    render_payload(snapshot.model_dump(mode="json"), "fleet", wide=True)
    rendered = capsys.readouterr()
    assert NODE_A in rendered.out and NODE_B in rendered.out
    assert str(partial_installation_id) in rendered.out
    selected = snapshot.model_copy(update={"nodes": snapshot.nodes[:1]})
    render_payload(selected.model_dump(mode="json"), "fleet")
    rendered = capsys.readouterr()
    # Members outside the selection stay named rather than dropped.
    assert NODE_B in rendered.out
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

    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_B)
        assert node is not None
        node.state = "revoked"
        node.revoked_at = NOW

    registered_visible = FleetProjection(sessions, clock=lambda: NOW).read().nodes[0]
    external_install = next(
        value
        for value in registered_visible.installed
        if value.installation_id == complete_installation_id
    )
    external_run = next(
        value for value in registered_visible.loaded if value.run_id == healthy_run_id
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


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize(
    "damage",
    (
        "document",
        "digest",
        "candidate",
        "mapping",
        "mapping_rank",
        "rank",
        "state",
        "labels",
    ),
)
def test_a_damaged_active_revision_preserves_known_presence_and_recovers(
    damage: str,
) -> None:
    """A node whose catalog revision is unreadable must not read as empty.

    Skipping the group would show an operator a node with nothing installed,
    which says "nothing is deployed" rather than "the catalog is
    inconsistent". An ineligible revision is different: it is simply not this
    node's business, so it disappears without an error.
    """
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    recipe_id = "00000000-0000-4000-8000-000000000401"
    revision_id = "00000000-0000-4000-8000-000000000402"
    model_id = "00000000-0000-4000-8000-000000000403"
    model_revision_id = "00000000-0000-4000-8000-000000000404"
    mapping_id = "00000000-0000-4000-8000-000000000405"
    build_id = "00000000-0000-4000-8000-000000000406"
    installation_id = "00000000-0000-4000-8000-000000000407"
    run_id = "00000000-0000-4000-8000-000000000408"
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=NODE_A,
                state="active",
                architecture="linux-arm64",
                last_seen_at=NOW,
            )
        )
        session.flush()
        session.add(_certificate(NODE_A, "broken-revision"))
        documents, revisions = _canonical_catalog_documents(
            recipe_id,
            revision_id,
            model_id,
            model_revision_id,
            slug="broken-recipe",
            title="Broken Recipe",
        )
        mapping = ClusterMapping(
            id=mapping_id,
            recipe_revision_id=revision_id,
            topology_name="single",
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
        session.add_all(documents)
        session.flush()
        session.add_all([*revisions, mapping, build])
        session.flush()
        session.add(
            ClusterMappingNode(
                id="00000000-0000-4000-8000-000000000409",
                mapping_id=mapping_id,
                node_id=NODE_A,
                rank=0,
                role="entrypoint",
                endpoint_owner=True,
                created_at=NOW,
            )
        )
        session.add(
            RecipeInstallation(
                id=installation_id,
                recipe_revision_id=revision_id,
                mapping_id=mapping_id,
                mapping_generation=1,
                recipe_build_id=build_id,
                image_digest="sha256:" + "5" * 64,
                plan_digest="7" * 64,
                plan={},
                state="installed",
                actor="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.flush()
        session.add(
            InstallationNode(
                id="00000000-0000-4000-8000-000000000410",
                installation_id=installation_id,
                node_id=NODE_A,
                rank=0,
                role="entrypoint",
                state="installed",
                required_bytes=100,
                installed_bytes=100,
                updated_at=NOW,
            )
        )
        session.add(
            RecipeRun(
                id=run_id,
                installation_id=installation_id,
                mapping_id=mapping_id,
                mapping_generation=1,
                alias="broken-run",
                plan_digest="8" * 64,
                plan={},
                state="running",
                route_state="published",
                actor="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.flush()
        session.add(
            RunNode(
                id="00000000-0000-4000-8000-000000000411",
                run_id=run_id,
                node_id=NODE_A,
                rank=0,
                role="entrypoint",
                state="running",
                port=8000,
                reserved_memory_bytes=200,
                observed_memory_bytes=180,
                updated_at=NOW,
            )
        )
    # A second known group on the same node must survive the damaged row.
    healthy_installation_id = "00000000-0000-4000-8000-000000000907"
    healthy_run_id = "00000000-0000-4000-8000-000000000908"
    with sessions.begin() as session:
        session.add(_profile(NODE_A, display_name="Spark A", hostname="spark-a"))
        for model, source_id, replacements in (
            (
                ClusterMapping,
                mapping_id,
                {
                    "id": "00000000-0000-4000-8000-000000000905",
                    "placement_digest": "9" * 64,
                },
            ),
            (
                ClusterMappingNode,
                "00000000-0000-4000-8000-000000000409",
                {
                    "id": "00000000-0000-4000-8000-000000000909",
                    "mapping_id": "00000000-0000-4000-8000-000000000905",
                },
            ),
            (
                RecipeInstallation,
                installation_id,
                {
                    "id": healthy_installation_id,
                    "mapping_id": "00000000-0000-4000-8000-000000000905",
                },
            ),
            (
                InstallationNode,
                "00000000-0000-4000-8000-000000000410",
                {
                    "id": "00000000-0000-4000-8000-000000000910",
                    "installation_id": healthy_installation_id,
                },
            ),
            (
                RecipeRun,
                run_id,
                {
                    "id": healthy_run_id,
                    "installation_id": healthy_installation_id,
                    "mapping_id": "00000000-0000-4000-8000-000000000905",
                    "alias": "healthy-run",
                },
            ),
            (
                RunNode,
                "00000000-0000-4000-8000-000000000411",
                {
                    "id": "00000000-0000-4000-8000-000000000911",
                    "run_id": healthy_run_id,
                },
            ),
        ):
            table = cast(Table, model.__table__)
            session.execute(
                table.insert().from_select(
                    [column.name for column in table.columns],
                    select(
                        *[
                            literal(replacements[column.name])
                            if column.name in replacements
                            else column
                            for column in table.columns
                        ]
                    ).where(table.c.id == source_id),
                )
            )
    # Damage the row the way an out-of-band write would, in its own session:
    # the ORM refuses to rewrite an active revision (before_update,
    # before_delete, and a commit-time digest check), so only a restore, manual
    # surgery or drift against a newer canonical model leaves this state on disk.
    with sessions.begin() as session:
        session.execute(text("PRAGMA ignore_check_constraints = ON"))
        if damage == "mapping":
            session.execute(
                update(ClusterMapping)
                .where(ClusterMapping.id == mapping_id)
                .values(node_count=-1)
            )
        elif damage == "mapping_rank":
            session.execute(
                update(ClusterMappingNode)
                .where(ClusterMappingNode.mapping_id == mapping_id)
                .values(rank=-1)
            )
        elif damage == "rank":
            session.execute(
                update(InstallationNode)
                .where(InstallationNode.installation_id == installation_id)
                .values(rank=-1)
            )
            session.execute(
                update(RunNode).where(RunNode.run_id == run_id).values(rank=-1)
            )
        elif damage == "state":
            session.execute(
                update(RecipeInstallation)
                .where(RecipeInstallation.id == installation_id)
                .values(state="invalid")
            )
            session.execute(
                update(RecipeRun).where(RecipeRun.id == run_id).values(state="invalid")
            )
        elif damage == "labels":
            session.execute(
                update(AgentNodeProfile)
                .where(AgentNodeProfile.node_id == NODE_A)
                .values(labels=["invalid"])
            )
        elif damage == "candidate":
            session.execute(
                update(CatalogDocumentRevision)
                .where(CatalogDocumentRevision.id == revision_id)
                .values(state="candidate")
            )
        else:
            column = "document" if damage == "document" else "content_digest"
            value = '{"schema_version": 2}' if damage == "document" else "0" * 64
            session.execute(
                text(
                    "UPDATE catalog_document_revisions "
                    f"SET {column} = :value WHERE id = :id"
                ),
                {"value": value, "id": revision_id},
            )
        session.execute(text("PRAGMA ignore_check_constraints = OFF"))

    projection = FleetProjection(sessions, clock=lambda: NOW)
    if damage == "candidate":
        snapshot = projection.read()
        assert (snapshot.nodes[0].installed, snapshot.nodes[0].loaded) == ([], [])
        return
    from .test_operation_api import _client

    client, operator, *_ = _client(fleet_projection=projection)
    snapshot = projection.read()
    observed = client.get("/api/fleet", headers=operator)
    assert observed.status_code == 200
    observed_snapshot = FleetSnapshot.model_validate_json(
        canonical_message(observation_document(observed))
    )
    if damage != "labels":
        assert observed_snapshot.nodes[0].installed[0].complete is None
        assert observed_snapshot.nodes[0].loaded[0].healthy is None
    snapshot = projection.read()
    if damage == "labels":
        assert observed_snapshot.nodes[0].labels is None
        issues = observed_snapshot.nodes[0].projection_issues
        assert issues is not None and "unknown" in issues[0]
        assert all(value.complete is True for value in snapshot.nodes[0].installed)
        with sessions.begin() as session:
            session.execute(
                update(AgentNodeProfile)
                .where(AgentNodeProfile.node_id == NODE_A)
                .values(labels={"role": "inference"})
            )
        recovered = client.get("/api/fleet", headers=operator)
        recovered_snapshot = FleetSnapshot.model_validate_json(
            canonical_message(observation_document(recovered))
        )
        assert recovered_snapshot.nodes[0].labels == {"role": "inference"}
        assert not recovered_snapshot.nodes[0].projection_issues
        return
    if damage in {"mapping", "mapping_rank", "rank", "state"}:
        assert (
            next(
                value
                for value in snapshot.nodes[0].installed
                if value.installation_id == healthy_installation_id
            ).complete
            is True
        )
        assert (
            next(
                value
                for value in snapshot.nodes[0].loaded
                if value.run_id == healthy_run_id
            ).healthy
            is True
        )
    assert snapshot.nodes[0].installed[0].installation_id == installation_id
    assert snapshot.nodes[0].loaded[0].run_id == run_id
    assert snapshot.nodes[0].installed[0].complete is None
    assert snapshot.nodes[0].loaded[0].healthy is None
    installation_issue = snapshot.nodes[0].installed[0].projection_issue
    run_issue = snapshot.nodes[0].loaded[0].projection_issue
    assert installation_issue is not None and "unknown" in installation_issue
    assert run_issue is not None and "unknown" in run_issue
    with sessions.begin() as session:
        session.execute(
            update(ClusterMapping)
            .where(ClusterMapping.id == mapping_id)
            .values(node_count=1)
        )
        session.execute(
            update(ClusterMappingNode)
            .where(ClusterMappingNode.mapping_id == mapping_id)
            .values(rank=0)
        )
        session.execute(
            update(InstallationNode)
            .where(InstallationNode.installation_id == installation_id)
            .values(rank=0)
        )
        session.execute(update(RunNode).where(RunNode.run_id == run_id).values(rank=0))
        session.execute(
            update(RecipeInstallation)
            .where(RecipeInstallation.id == installation_id)
            .values(state="installed")
        )
        session.execute(
            update(RecipeRun).where(RecipeRun.id == run_id).values(state="running")
        )
        session.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == revision_id)
            .values(
                document=revisions[0].document,
                content_digest=revisions[0].content_digest,
            )
        )
    recovered = client.get("/api/fleet", headers=operator)
    assert recovered.status_code == 200
    recovered_snapshot = FleetSnapshot.model_validate_json(
        canonical_message(observation_document(recovered))
    )
    assert recovered_snapshot.nodes[0].loaded[0].healthy is True
    restored = projection.read()
    assert restored.nodes[0].installed[0].complete is True
    assert restored.nodes[0].loaded[0].healthy is True


def test_non_rfc_non_nil_boot_id_flows_through_snapshot() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=NODE_A, state="active"))
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

    projection = FleetProjection(sessions, clock=lambda: NOW)
    snapshot = projection.read()

    node_telemetry = snapshot.nodes[0].telemetry
    assert node_telemetry is not None
    assert node_telemetry.sample.boot_id == NON_RFC_BOOT_ID


def test_projection_preserves_all_current_installation_groups_beyond_512() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    recipe_id = "00000000-0000-4000-8000-000000000301"
    revision_id = "00000000-0000-4000-8000-000000000302"
    mapping_id = "00000000-0000-4000-8000-000000000303"
    build_id = "00000000-0000-4000-8000-000000000304"
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=NODE_A,
                state="active",
                architecture="linux-arm64",
                last_seen_at=NOW,
            )
        )
        documents, revisions = _canonical_catalog_documents(
            recipe_id,
            revision_id,
            "00000000-0000-4000-8000-000000000306",
            "00000000-0000-4000-8000-000000000307",
            slug="bounded-recipe",
            title="Bounded Recipe",
        )
        session.add_all(documents)
        session.flush()
        session.add_all(revisions)
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

    snapshot = FleetProjection(sessions, clock=lambda: NOW).read()

    installation_ids = [value.installation_id for value in snapshot.nodes[0].installed]
    assert len(installation_ids) == 513
    assert installation_ids[0] == "install-000"
    assert installation_ids[-1] == "install-512"
    assert len(set(installation_ids)) == len(installation_ids)


def test_projection_keeps_every_registered_node_visible_beyond_500() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add_all(
            [
                AgentNode(
                    node_id=f"spk_{index:032x}",
                    state="active",
                )
                for index in range(1, 502)
            ]
        )
    snapshot = FleetProjection(sessions, clock=lambda: NOW).read()
    assert len(snapshot.nodes) == 501
    assert {item.id for item in snapshot.nodes} == {
        f"spk_{index:032x}" for index in range(1, 502)
    }


def _stored_installation_plan(
    *,
    mapping_id: str,
    revision_id: str,
    node_id: str,
    node: dict[str, object],
) -> dict[str, object]:
    """Build the admission plan document the way the producer persists it.

    The compiled execution plan is the real fixture the wire contract tests use,
    so the plan validates as the current contract rather than a hand-written
    approximation.
    """

    compiled_plan = json.loads(
        (Path(__file__).parent / "fixtures" / "compiled_workload_v2.json").read_text()
    )
    return installation_plan_document(
        {
            "schema_version": 1,
            "mapping_id": mapping_id,
            "mapping_generation": 1,
            "recipe_build_id": None,
            "image_digest": "sha256:" + "5" * 64,
            "recipe_revision_id": revision_id,
            "recipe_content_sha256": "7" * 64,
            "allowed": True,
            "plan_digest": "7" * 64,
            "nodes": [
                {
                    "node_id": node_id,
                    "rank": 0,
                    "role": "entrypoint",
                    "allowed": True,
                    "inventory_observed_at": None,
                    "free_bytes": 1024,
                    "active_reserved_bytes": 0,
                    "reused_bytes": 0,
                    "required_download_bytes": 0,
                    "required_bytes": 120,
                    "disk_floor_bytes": 0,
                    "free_after_bytes": 0,
                    "blockers": [],
                    "warnings": [],
                    **node,
                }
            ],
            "compiled_execution_plans": {node_id: compiled_plan},
        }
    )


def _installed_byte_group(
    sessions,
    *,
    installation_id: str,
    reservation_bytes: int,
    payload_expectation_bytes: int | None,
    installed_bytes: int,
) -> None:
    """Persist one installed single-rank group the way the producer records it.

    ``reservation_bytes`` is the disk reservation admission stores on the
    ``installation_nodes`` row, ``payload_expectation_bytes`` is the materialized
    payload the admitted plan records (``None`` for a plan written before the
    expectation existed), and ``installed_bytes`` is the installation-tree total
    the agent measured and reported as install evidence.
    """

    recipe_id = "00000000-0000-4000-8000-000000000501"
    revision_id = "00000000-0000-4000-8000-000000000502"
    mapping_id = "00000000-0000-4000-8000-000000000503"
    build_id = "00000000-0000-4000-8000-000000000504"
    node: dict[str, object] = {"required_bytes": reservation_bytes}
    if payload_expectation_bytes is not None:
        node["required_payload_bytes"] = payload_expectation_bytes
    plan = _stored_installation_plan(
        mapping_id=mapping_id,
        revision_id=revision_id,
        node_id=NODE_A,
        node=node,
    )
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=NODE_A,
                state="active",
                architecture="linux-arm64",
                last_seen_at=NOW,
            )
        )
        session.add(_certificate(NODE_A, "byte-recipe"))
        documents, revisions = _canonical_catalog_documents(
            recipe_id,
            revision_id,
            "00000000-0000-4000-8000-000000000505",
            "00000000-0000-4000-8000-000000000506",
            slug="byte-recipe",
            title="Byte Recipe",
        )
        session.add_all(documents)
        session.flush()
        session.add_all(revisions)
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
                id="00000000-0000-4000-8000-000000000507",
                mapping_id=mapping_id,
                node_id=NODE_A,
                rank=0,
                role="entrypoint",
                endpoint_owner=True,
                created_at=NOW,
            )
        )
        session.add(
            RecipeInstallation(
                id=installation_id,
                recipe_revision_id=revision_id,
                mapping_id=mapping_id,
                mapping_generation=1,
                recipe_build_id=build_id,
                image_digest="sha256:" + "5" * 64,
                plan_digest="7" * 64,
                plan=plan,
                state="installed",
                actor="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.flush()
        session.add(
            InstallationNode(
                id="00000000-0000-4000-8000-000000000508",
                installation_id=installation_id,
                node_id=NODE_A,
                rank=0,
                role="entrypoint",
                state="installed",
                required_bytes=reservation_bytes,
                installed_bytes=installed_bytes,
                updated_at=NOW,
            )
        )


def _installed_presence(sessions, installation_id: str) -> RecipePresence:
    snapshot = FleetProjection(sessions, clock=lambda: NOW).read()
    presence = next(
        value
        for value in snapshot.nodes[0].installed
        if value.installation_id == installation_id
    )
    assert isinstance(presence, RecipePresence)
    return presence


@pytest.mark.parametrize(
    (
        "reservation_bytes",
        "payload_expectation_bytes",
        "installed_bytes",
        "complete",
        "reason",
    ),
    [
        # Admission reserves the materialized payload plus staging, cache and
        # rollback headroom, so the reservation exceeds the tree the agent
        # measures for an honest, finished installation.
        (120, 100, 100, True, None),
        # A tree that really is short of the admitted payload stays incomplete.
        (120, 100, 99, False, "rank-incomplete-bytes"),
    ],
)
def test_installed_bytes_flag_compares_the_persisted_payload_expectation(
    reservation_bytes: int,
    payload_expectation_bytes: int,
    installed_bytes: int,
    complete: bool,
    reason: str | None,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    installation_id = "00000000-0000-4000-8000-000000000509"
    _installed_byte_group(
        sessions,
        installation_id=installation_id,
        reservation_bytes=reservation_bytes,
        payload_expectation_bytes=payload_expectation_bytes,
        installed_bytes=installed_bytes,
    )
    presence = _installed_presence(sessions, installation_id)
    assert (presence.complete, presence.degraded_reason) == (complete, reason)


def _mutated_group(mutate):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    installation_id = "00000000-0000-4000-8000-000000000511"
    _installed_byte_group(
        sessions,
        installation_id=installation_id,
        reservation_bytes=120,
        payload_expectation_bytes=100,
        installed_bytes=100,
    )
    with sessions.begin() as session:
        mutate(
            session.get(RecipeInstallation, installation_id),
            session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == installation_id
                )
            ).one(),
        )
    return installation_id, FleetProjection(sessions, clock=lambda: NOW).read()


def _set(installation_state=None, rank_state=None, role=None, installed_bytes=None):
    def mutate(installation, rank):
        if installation_state is not None:
            installation.state = installation_state
        if rank_state is not None:
            rank.state = rank_state
        if role is not None:
            rank.role = role
        if installed_bytes is not None:
            rank.installed_bytes = installed_bytes

    return mutate


@pytest.mark.parametrize(
    ("mutation", "reason", "group_state", "rank_state", "affected"),
    [
        (
            _set(installation_state="partial"),
            "installation-not-installed",
            "partial",
            "installed",
            [],
        ),
        (
            _set(installation_state="failed"),
            "installation-not-installed",
            "failed",
            "installed",
            [],
        ),
        (_set(rank_state="failed"), "rank-not-installed", "installed", "failed", [0]),
        (_set(role="worker"), "rank-membership-mismatch", "installed", "installed", []),
        (
            _set(installed_bytes=40),
            "rank-incomplete-bytes",
            "installed",
            "installed",
            [0],
        ),
    ],
)
def test_install_partial_names_the_installation_the_rank_and_the_reason(
    mutation, reason, group_state, rank_state, affected
) -> None:
    installation_id, snapshot = _mutated_group(mutation)
    warnings = [
        warning
        for warning in snapshot.nodes[0].warnings
        if warning.code == "install.partial"
    ]
    assert len(warnings) == 1
    evidence = warnings[0].install_partial
    assert evidence is not None
    assert evidence.installation_id == installation_id
    assert evidence.title == "Byte Recipe"
    assert evidence.rank == 0
    assert evidence.reason == reason
    assert (evidence.group_state, evidence.rank_state) == (group_state, rank_state)
    assert evidence.affected_ranks == affected
    assert evidence.required_bytes == 100


def test_a_complete_installation_raises_no_install_partial_warning() -> None:
    _installation_id, snapshot = _mutated_group(_set())
    assert not [w for w in snapshot.nodes[0].warnings if w.code == "install.partial"]


def test_presence_does_not_fire_the_byte_reason_without_a_persisted_expectation() -> (
    None
):
    """A plan written before the expectation existed reads and stays complete.

    The measured tree is far below the disk reservation here, so the assertion
    fails if the byte check falls back to comparing against the reservation or
    treats an absent expectation as a short install.
    """

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    installation_id = "00000000-0000-4000-8000-000000000510"
    _installed_byte_group(
        sessions,
        installation_id=installation_id,
        reservation_bytes=120,
        payload_expectation_bytes=None,
        installed_bytes=1,
    )
    presence = _installed_presence(sessions, installation_id)
    assert (presence.complete, presence.degraded_reason) == (True, None)


def _cpu_clock_codes(
    *,
    span_seconds: int,
    avg_mhz: int = 2_000,
    temperature: int | None = 85,
    gpu_util: float = 5.0,
    recover_at: int | None = None,
) -> tuple[list[str], list[str]]:
    """Feed one node a 2-second sample series ending at NOW; return its warnings."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=NODE_A,
                state="active",
                architecture="linux-arm64",
                last_seen_at=NOW - timedelta(seconds=1),
            )
        )
        session.flush()
        session.add(_certificate(NODE_A, "cpu-clock"))
        session.add(_inventory(NODE_A, NOW - timedelta(seconds=1), free_bytes=700))
        latest = None
        for index, age in enumerate(range(span_seconds, -1, -2), start=1):
            sample = _telemetry(
                NODE_A,
                f"00000000-0000-4000-8000-{index:012d}",
                NOW - timedelta(seconds=age),
                sequence=index,
                cpu=gpu_util,
            )
            healthy_clock = recover_at is not None and age > recover_at
            sample.cpu_frequency_avg_mhz = 3_900 if healthy_clock else avg_mhz
            sample.cpu_frequency_min_mhz = 1_000
            sample.cpu_frequency_max_mhz = 3_900
            sample.gpu_temperature_c = temperature
            session.add(sample)
            latest = sample
        session.flush()
        assert latest is not None
        session.add(NodeTelemetryLatest(node_id=NODE_A, sample_id=latest.id))
    node = FleetProjection(sessions, clock=lambda: NOW).read().nodes[0]
    return [w.code for w in node.warnings], [w.detail for w in node.warnings]


def test_low_cpu_clock_is_raised_only_when_sustained_and_hot_or_loaded() -> None:
    codes, details = _cpu_clock_codes(span_seconds=80)
    assert codes == ["cpu.low-clock"]
    assert "2000 of 3900 MHz" in details[0] and "85" in details[0]

    # Loaded but cool also counts.
    assert _cpu_clock_codes(span_seconds=80, temperature=50, gpu_util=90.0)[0] == [
        "cpu.low-clock"
    ]
    # Not for a full minute yet.
    assert _cpu_clock_codes(span_seconds=40)[0] == []
    # A clock that only recently dropped has not been low for a minute.
    assert _cpu_clock_codes(span_seconds=80, recover_at=40)[0] == []
    # Low but idle and cool is normal.
    assert _cpu_clock_codes(span_seconds=80, temperature=45)[0] == []
    # At or above 70% of the maximum is fine, and unreported temperature is not hot.
    assert _cpu_clock_codes(span_seconds=80, avg_mhz=2_800)[0] == []
    assert _cpu_clock_codes(span_seconds=80, temperature=None)[0] == []


def _route_warnings(interfaces, route):
    """Project one node whose inventory carries the given network evidence."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=NODE_A,
                state="active",
                architecture="linux-arm64",
                last_seen_at=NOW - timedelta(seconds=1),
            )
        )
        session.flush()
        session.add(_certificate(NODE_A, "nas-route"))
        inventory = _inventory(NODE_A, NOW - timedelta(seconds=1), free_bytes=700)
        inventory.network_interfaces = interfaces
        inventory.nas_route_interface = route
        session.add(inventory)
    node = FleetProjection(sessions, clock=lambda: NOW).read().nodes[0]
    return [w for w in node.warnings if w.code.startswith("network.")]


_WIFI = {"name": "wlP9s9", "kind": "wifi", "link_speed_mbps": 2402, "carrier": True}

_FABRIC = [
    {
        "name": "enP2p1s0f1np1",
        "kind": "fabric",
        "link_speed_mbps": 200000,
        "carrier": True,
    },
    {
        "name": "enp1s0f1np1",
        "kind": "fabric",
        "link_speed_mbps": 200000,
        "carrier": True,
    },
]


def test_production_shape_never_recommends_a_fabric_port_for_the_nas() -> None:
    # Linked 200 Gb/s fabric ports plus an unplugged RJ45 port, NAS over Wi-Fi.
    rj45 = {"name": "enP7s7", "kind": "wired", "carrier": False}
    wifi = {"name": "wlP9s9", "kind": "wifi", "carrier": True}
    (warning,) = _route_warnings([*_FABRIC, rj45, wifi], "wlP9s9")
    assert "unknown speed" not in warning.detail
    assert warning.recommendation is not None
    assert "network cable to the RJ45 port enP7s7" in warning.recommendation
    assert "enP2p1s0f1np1" not in warning.recommendation
    assert "enp1s0f1np1" not in warning.recommendation

    # Without any RJ45 port, only the fabric ports are wired: still no port.
    (warning,) = _route_warnings([*_FABRIC, wifi], "wlP9s9")


def test_wifi_nas_route_is_a_typed_warning_with_a_recommendation() -> None:
    down = {"name": "enP7s7", "kind": "wired", "carrier": False}
    (warning,) = _route_warnings([down, _WIFI], "wlP9s9")
    assert warning.severity == "warning"
    assert "over Wi-Fi (wlP9s9, 2.402 Gb/s, shared airtime)" in warning.detail
    assert warning.recommendation is not None
    assert "network cable to the RJ45 port enP7s7" in warning.recommendation

    up = {"name": "enP7s7", "kind": "wired", "link_speed_mbps": 10000, "carrier": True}
    (warning,) = _route_warnings([up, _WIFI], "wlP9s9")
    assert "10 Gb/s" in (warning.recommendation or "")

    (warning,) = _route_warnings([_WIFI], "wlP9s9")


def test_wired_unknown_or_unreported_nas_route_raises_no_warning() -> None:
    wired = {
        "name": "enP7s7",
        "kind": "wired",
        "link_speed_mbps": 10000,
        "carrier": True,
    }
    assert _route_warnings([wired, _WIFI], "enP7s7") == []
    # An older agent never reports network evidence: unknown, not a warning.
    assert _route_warnings(None, None) == []
    assert _route_warnings([wired, _WIFI], None) == []


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize("damaged", ["inventory", "telemetry"])
def test_fleet_api_isolates_damaged_observation_and_recovers(tmp_path, damaged) -> None:
    from .test_operation_api import _client

    engine = create_engine(f"sqlite:///{tmp_path / 'observations.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    sample_id = "00000000-0000-4000-8000-000000000011"
    inventory = _inventory(NODE_A, NOW, free_bytes=800)
    sample = _telemetry(NODE_A, sample_id, NOW, sequence=1, cpu=50)
    with sessions.begin() as session:
        session.add_all(
            [
                AgentNode(node_id=NODE_A, state="active", last_seen_at=NOW),
                AgentNode(node_id=NODE_B, state="active", last_seen_at=NOW),
                inventory,
                sample,
                NodeTelemetryLatest(node_id=NODE_A, sample_id=sample_id),
                _inventory(NODE_B, NOW, free_bytes=750),
            ]
        )
    with sessions.begin() as session:
        if damaged == "inventory":
            session.execute(
                update(NodeInventorySnapshot)
                .where(NodeInventorySnapshot.id == inventory.id)
                .values(network_interfaces=[{"unexpected": True}])
            )
        else:
            # Simulate a damaged historical row despite today's writer constraint.
            session.execute(text("PRAGMA ignore_check_constraints = ON"))
            session.execute(
                update(NodeTelemetrySample)
                .where(NodeTelemetrySample.id == sample_id)
                .values(boot_id="invalid")
            )
            session.execute(text("PRAGMA ignore_check_constraints = OFF"))
    client, operator, *_ = _client(
        fleet_projection=FleetProjection(sessions, clock=lambda: NOW)
    )
    response = client.get("/api/fleet", headers=operator)
    assert response.status_code == 200
    observed_snapshot = FleetSnapshot.model_validate_json(
        canonical_message(observation_document(response))
    )
    nodes = {node.id: node for node in observed_snapshot.nodes}
    assert set(nodes) == {NODE_A, NODE_B}
    healthy_inventory = nodes[NODE_B].inventory
    assert healthy_inventory is not None and healthy_inventory.disk_free_bytes == 750
    if damaged == "inventory":
        assert nodes[NODE_A].inventory is None
    else:
        assert nodes[NODE_A].telemetry is None
    assert any(
        "unreadable" in warning.detail and "unknown" in warning.detail
        for warning in nodes[NODE_A].warnings
    )
    with sessions.begin() as session:
        if damaged == "inventory":
            session.execute(
                update(NodeInventorySnapshot)
                .where(NodeInventorySnapshot.id == inventory.id)
                .values(network_interfaces=None)
            )
        else:
            session.execute(
                update(NodeTelemetrySample)
                .where(NodeTelemetrySample.id == sample_id)
                .values(boot_id="00000000-0000-4000-8000-000000000001")
            )
    recovered = client.get("/api/fleet", headers=operator)
    assert recovered.status_code == 200
    recovered_snapshot = FleetSnapshot.model_validate_json(
        canonical_message(observation_document(recovered))
    )
    restored = next(node for node in recovered_snapshot.nodes if node.id == NODE_A)
    if damaged == "inventory":
        assert restored.inventory is not None
    else:
        assert restored.telemetry is not None
    assert not any("unreadable" in warning.detail for warning in restored.warnings)


def test_expired_certificate_attention_preserves_security_and_names_enrollment_authority() -> (
    None
):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=NODE_A,
                state="active",
                architecture="linux-arm64",
                last_seen_at=NOW - timedelta(seconds=1),
            )
        )
        session.flush()
        session.add(_certificate(NODE_A, "expired-attention", not_after=NOW))
    node = FleetProjection(sessions, clock=lambda: NOW).read().nodes[0]
    assert node.connection.online_state == "offline"
    assert any("vonkctl fleet re-enroll" in warning.detail for warning in node.warnings)
    # A fresh read with an authorized replacement no longer reports expiry.
    with sessions.begin() as session:
        certificate = session.get(AgentCertificate, "expired-attention")
        assert certificate is not None
        certificate.not_after = NOW + timedelta(days=1)
    node = FleetProjection(sessions, clock=lambda: NOW).read().nodes[0]
    assert node.connection.online_state == "online"
    assert not any(
        "vonkctl fleet re-enroll" in warning.detail for warning in node.warnings
    )
