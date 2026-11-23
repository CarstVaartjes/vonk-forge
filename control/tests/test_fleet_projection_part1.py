from __future__ import annotations

import json
from datetime import UTC, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, event, update
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.state_machines import NodeIdentityState
from vonk_control.agent_contacts import record_contact
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
    AgentNode,
    AgentNodeContact,
    Base,
    ClusterMapping,
    ClusterMappingNode,
    FleetEventCursor,
    FleetStreamEvent,
    InstallationNode,
    NodeTelemetryLatest,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)

from .fleet_projection_support import (
    NODE_A,
    NODE_B,
    NOW,
    _canonical_catalog_documents,
    _certificate,
    _inventory,
    _profile,
    _telemetry,
)
from .runtime_identity_support import (
    PACKAGED_RUNTIME_IDENTITY,
    inventory_items_fixture,
    set_inventory_hostname,
)

COMMIT = "a" * 64


NODE_C = "spk_" + "3" * 32


NODE_D = "spk_" + "4" * 32


EXTRA_NODE = "spk_" + "f" * 32


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
                contact=AgentNodeContact(last_seen_at=NOW),
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
                state=NodeIdentityState.RETIRED,
                revoked_at=NOW,
                contact=AgentNodeContact(last_seen_at=NOW),
            )
        )

    projection = FleetProjection(
        sessions,
        clock=lambda: NOW,
    )

    assert [node.id for node in projection.read().nodes] == []


def test_fleet_lists_nodes_in_enrolment_order_not_node_id_order() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    node_c = "spk_" + "3" * 32
    with sessions.begin() as session:
        # B enrolled first, then C, then A: the reverse of node_id order for A.
        # A node enrolled before the column existed (None) lists first.
        for node_id, days in ((NODE_A, 1), (NODE_B, 3), (node_c, 2)):
            session.add(
                AgentNode(
                    node_id=node_id,
                    state="active",
                    enrolled_at=NOW - timedelta(days=days),
                )
            )
        session.add(AgentNode(node_id="spk_" + "9" * 32, state="active"))

    snapshot = FleetProjection(sessions, clock=lambda: NOW).read()

    assert [node.id for node in snapshot.nodes] == [
        "spk_" + "9" * 32,
        NODE_B,
        node_c,
        NODE_A,
    ]


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
                    inventory_snapshot=inventory_items_fixture(
                        NODE_A,
                        observed_at=NOW - timedelta(seconds=5),
                        identity=AgentRuntimeIdentity.model_validate(
                            {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                        ),
                    ),
                    contact=AgentNodeContact(last_seen_at=NOW - timedelta(seconds=5)),
                ),
                AgentNode(
                    node_id=NODE_B,
                    state="active",
                    inventory_snapshot=inventory_items_fixture(
                        NODE_B,
                        observed_at=NOW - timedelta(seconds=7),
                        identity=AgentRuntimeIdentity.model_validate(
                            {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                        ),
                    ),
                    contact=AgentNodeContact(last_seen_at=NOW - timedelta(seconds=7)),
                ),
                AgentNode(
                    node_id=EXTRA_NODE,
                    state=NodeIdentityState.RETIRED,
                    revoked_at=NOW,
                    inventory_snapshot=inventory_items_fixture(
                        EXTRA_NODE,
                        observed_at=NOW,
                        identity=AgentRuntimeIdentity.model_validate(
                            {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                        ),
                    ),
                    contact=AgentNodeContact(last_seen_at=NOW),
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                _profile(NODE_A, display_name="Alpha", labels={"rack": "left"}),
                _profile(NODE_B, display_name="Beta", labels={"rack": "right"}),
            ]
        )
        set_inventory_hostname(session, NODE_A, "alpha.internal")
        set_inventory_hostname(session, NODE_B, "beta.internal")
        session.add_all(
            [
                _certificate(NODE_A, "bounded-a"),
                _certificate(NODE_B, "bounded-b"),
                _certificate(EXTRA_NODE, "bounded-external"),
            ]
        )
        record_contact(
            session,
            NODE_A,
            contact_certificate_serial="bounded-a",
            management_address="192.168.1.211",
            management_observed_at=NOW,
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

    expected_document = {
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
                    "schema_version": 1,
                    "memory_pool": "separate",
                    "observed_at": "2026-08-15T11:59:50Z",
                    "received_at": "2026-08-15T11:59:51Z",
                    "age_seconds": 9.0,
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
                    "age_seconds": 1.75,
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
                    "schema_version": 1,
                    "memory_pool": "separate",
                    "observed_at": "2026-08-15T11:59:48Z",
                    "received_at": "2026-08-15T11:59:49Z",
                    "age_seconds": 11.0,
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
    from vonk_agent_protocol.gpu_clock import default_gpu_clock
    from vonk_agent_protocol.swap import default_swap

    expected = FleetSnapshot.model_validate_json(json.dumps(expected_document))
    for node in expected.nodes:
        node.gpu_clock_desired = default_gpu_clock()
        node.swap_desired = default_swap()
        node.agent_runtime = AgentRuntimeIdentity.model_validate(
            {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
        )
    assert snapshot == expected
    assert EXTRA_NODE not in {node.id for node in snapshot.nodes}
    # PostgreSQL sorts NULL last on ASC and SQLite first, so the rows alone cannot show
    # that Sparks without an enrolment time list first: the SQL has to say so.
    registered = [
        statement
        for statement in statements
        if "from agent_nodes" in statement
        and "order by" in statement
        and "node_inventory_snapshots" not in statement
    ]
    assert registered
    assert all("enrolled_at asc nulls first" in statement for statement in registered)
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
        session.add(_profile(NODE_A, display_name=NODE_A))
        set_inventory_hostname(session, NODE_A, "spark-3542.internal")
        session.add(_certificate(NODE_A, "profile-a"))
        record_contact(
            session,
            NODE_A,
            contact_certificate_serial="profile-a",
            management_address="192.168.1.211",
            management_observed_at=NOW,
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
            memory_total_bytes=None,
            memory_available_bytes=None,
            disk_total_bytes=None,
            disk_free_bytes=None,
            gpu_utilization_percent=None,
            gpu_memory_total_bytes=None,
            gpu_memory_free_bytes=None,
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
    from vonk_agent_protocol.telemetry import TelemetrySample

    assert (
        telemetry["boot_id"]["pattern"]
        == TelemetrySample.model_json_schema()["properties"]["boot_id"]["pattern"]
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
                    state=NodeIdentityState.RETIRED,
                    revoked_at=NOW,
                    contact=AgentNodeContact(last_seen_at=NOW),
                ),
                AgentNode(
                    node_id=node_ids[2],
                    state=NodeIdentityState.RETIRED,
                    contact=AgentNodeContact(last_seen_at=NOW),
                ),
                AgentNode(
                    node_id=node_ids[3],
                    state="active",
                    contact=AgentNodeContact(last_seen_at=NOW),
                ),
                AgentNode(
                    node_id=node_ids[4],
                    state="active",
                    contact=AgentNodeContact(last_seen_at=NOW),
                ),
                AgentNode(
                    node_id=node_ids[5],
                    state="active",
                    contact=AgentNodeContact(last_seen_at=NOW),
                ),
                AgentNode(
                    node_id=node_ids[6],
                    state="active",
                    contact=AgentNodeContact(last_seen_at=NOW),
                ),
                AgentNode(
                    node_id=node_ids[7],
                    state="active",
                    contact=AgentNodeContact(last_seen_at=NOW),
                ),
                AgentNode(node_id=node_ids[8], state="active"),
                AgentNode(
                    node_id=node_ids[9],
                    state="active",
                    contact=AgentNodeContact(
                        last_seen_at=NOW + timedelta(microseconds=1)
                    ),
                ),
                AgentNode(
                    node_id=node_ids[10],
                    state="active",
                    contact=AgentNodeContact(last_seen_at=NOW - timedelta(seconds=151)),
                ),
                AgentNode(
                    node_id=node_ids[11],
                    state="active",
                    contact=AgentNodeContact(last_seen_at=NOW),
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
        ("retired", "revoked", "offline", "agent-inactive"),
        ("active", "missing", "offline", "certificate-missing"),
        ("active", "revoked", "offline", "certificate-revoked"),
        ("active", "inactive", "offline", "certificate-inactive"),
        ("active", "not-yet-valid", "offline", "certificate-not-yet-valid"),
        ("active", "expired", "offline", "certificate-expired"),
        ("active", "valid", "offline", "never-seen"),
        ("active", "valid", "online", None),
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
                    inventory_snapshot=inventory_items_fixture(
                        node_id,
                        observed_at=NOW
                        - timedelta(seconds=150 if node_id != NODE_D else 151),
                        identity=AgentRuntimeIdentity.model_validate(
                            {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                        ),
                    ),
                    contact=AgentNodeContact(
                        last_seen_at=NOW
                        - timedelta(seconds=150 if node_id != NODE_D else 151)
                    ),
                )
            )
            session.flush()
            session.add(_certificate(node_id, f"freshness-{index}"))
            inventory_age = timedelta(
                seconds=300, milliseconds=1 if node_id == NODE_B else 0
            )
            inventory = _inventory(node_id, NOW - inventory_age, free_bytes=700 + index)
            inventory.received_at = NOW - inventory_age
            session.add(inventory)
            sample = _telemetry(
                node_id,
                f"00000000-0000-4000-8000-{index:012d}",
                NOW - ages[node_id],
                sequence=index,
                cpu=float(index),
            )
            sample.received_at = NOW - ages[node_id]
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
                    inventory_snapshot=inventory_items_fixture(
                        NODE_A,
                        observed_at=NOW,
                        identity=AgentRuntimeIdentity.model_validate(
                            {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                        ),
                    ),
                    contact=AgentNodeContact(last_seen_at=NOW),
                ),
                AgentNode(
                    node_id=NODE_B,
                    state="active",
                    inventory_snapshot=inventory_items_fixture(
                        NODE_B,
                        observed_at=NOW,
                        identity=AgentRuntimeIdentity.model_validate(
                            {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                        ),
                    ),
                    contact=AgentNodeContact(last_seen_at=NOW),
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
    assert "1 workload serving" in rendered.out
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

    # A route pending its first publication, with nothing recorded against it,
    # is not degraded. A waiting publication and a health-recovery re-publish
    # carry a route_error and stay degraded.
    for route_error, expected in (
        (None, None),
        ("supervisor busy; publication waits", "route-not-published"),
        ("recipe rank health requires recovery", "route-not-published"),
    ):
        with sessions.begin() as session:
            run = session.get(RecipeRun, route_failed_run_id)
            assert run is not None
            run.route_state = "pending"
            run.route_error = route_error
        reread = FleetProjection(sessions, clock=lambda: NOW).read().nodes[0]
        pending = next(v for v in reread.loaded if v.run_id == route_failed_run_id)
        assert pending.degraded_reason == expected
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
        node.state = NodeIdentityState.RETIRED
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
