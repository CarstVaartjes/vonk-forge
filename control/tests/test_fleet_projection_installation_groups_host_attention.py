"""Fleet projection: installation groups host attention."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tests.observation_transfer_peer import observation_document
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.host_tuning import HostTuning, MemoryFragmentation
from vonk_agent_protocol.inventory import HostSettings, InventoryRequest
from vonk_agent_protocol.reason_codes.admission import InstallDegradedReason
from vonk_agent_protocol.reason_codes.profiles import ProjectionCode
from vonk_agent_protocol.state_machines import InstallationState, NodeIdentityState
from vonk_agent_protocol.store_cleanup import StoreRetentionFailure
from vonk_agent_protocol.wire_model import canonical_message
from vonk_control.agent_contacts import record_contact
from vonk_control.fleet_projection import service
from vonk_control.fleet_projection.common import (
    FleetNode,
    FleetSnapshot,
    RecipePresence,
    _install_partial_warnings,
    telemetry_point,
)
from vonk_control.fleet_projection.host_checks import (
    SERVICE_UNITS,
    HostCheck,
    host_checks,
)
from vonk_control.fleet_projection.memory_fragmentation import assess
from vonk_control.fleet_projection.service import FleetProjection
from vonk_control.inventory.repository import (
    InventoryRepository,
    NodeInventoryReport,
)
from vonk_control.model_primitives import Base
from vonk_control.models.cache import RecipeBuild
from vonk_control.models.execution import (
    InstallationNode,
    RecipeInstallation,
)
from vonk_control.models.fleet import (
    AgentCertificate,
    AgentNode,
    AgentNodeContact,
)
from vonk_control.models.jobs import Job
from vonk_control.models.mapping import ClusterMapping, ClusterMappingNode
from vonk_control.models.operations import AgentOperation, AgentOperationAttempt
from vonk_control.models.telemetry import (
    NodeInventorySnapshot,
    NodeTelemetryLatest,
    NodeTelemetrySample,
)
from vonk_control.recipe_operations.execution_contract import installation_plan_document
from vonk_control.telemetry.samples import TelemetrySampleView

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
from .test_operation_api import _client


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
                inventory_snapshot=inventory_items_fixture(
                    NODE_A,
                    observed_at=NOW,
                    identity=AgentRuntimeIdentity.model_validate(
                        {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                    ),
                ),
                contact=AgentNodeContact(last_seen_at=NOW),
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
                inventory_snapshot=inventory_items_fixture(
                    NODE_A,
                    observed_at=NOW,
                    identity=AgentRuntimeIdentity.model_validate(
                        {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                    ),
                ),
                contact=AgentNodeContact(last_seen_at=NOW),
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
                required_payload_bytes=payload_expectation_bytes,
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
            _set(installation_state="planned", role="worker"),
            "rank-membership-mismatch",
            "planned",
            "installed",
            [],
        ),
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


@pytest.mark.parametrize(
    "mutation",
    [
        _set(),
        # In flight: its operation finishes it or the residue sweep ends it as
        # partial, which warns then.
        _set(installation_state="planned", rank_state="planned"),
        _set(installation_state="installing", rank_state="planned"),
    ],
)
def test_a_complete_or_in_flight_installation_raises_no_install_partial_warning(
    mutation,
) -> None:
    _installation_id, snapshot = _mutated_group(mutation)
    assert not [w for w in snapshot.nodes[0].warnings if w.code == "install.partial"]


def test_an_in_flight_installation_with_unreadable_evidence_still_warns() -> None:
    presence = RecipePresence(
        installation_id="00000000-0000-4000-8000-000000000001",
        recipe_id="00000000-0000-4000-8000-000000000002",
        recipe_revision_id="00000000-0000-4000-8000-000000000003",
        title="Recipe",
        topology_name="pair",
        expected_rank_count=2,
        present_ranks=[0, 1],
        member_node_ids=[NODE_A],
        rank=0,
        role="entrypoint",
        group_state=InstallationState.PLANNED,
        rank_state=InstallationState.PLANNED,
        complete=None,
        projection_issue="Stored recipe revision is unreadable.",
        degraded_reason=InstallDegradedReason.INSTALLATION_NOT_INSTALLED,
    )
    (warning,) = _install_partial_warnings([presence])
    assert warning.code == "install.partial"


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
                inventory_snapshot=inventory_items_fixture(
                    NODE_A,
                    observed_at=NOW - timedelta(seconds=1),
                    identity=AgentRuntimeIdentity.model_validate(
                        {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                    ),
                ),
                contact=AgentNodeContact(last_seen_at=NOW - timedelta(seconds=1)),
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


def _oom_codes(*readings: tuple[int, str, int], skew_minutes: int = 0) -> list[str]:
    """Feed one node (age in minutes, boot, oom_kills) samples; return its warning codes.

    The agent clock runs ``skew_minutes`` behind: received_at is that much later.
    """
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=NODE_A,
                state="active",
                inventory_snapshot=inventory_items_fixture(
                    NODE_A,
                    observed_at=NOW - timedelta(seconds=1),
                    identity=AgentRuntimeIdentity.model_validate(
                        {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                    ),
                ),
                contact=AgentNodeContact(last_seen_at=NOW - timedelta(seconds=1)),
            )
        )
        session.flush()
        session.add(_certificate(NODE_A, "oom-kill"))
        session.add(_inventory(NODE_A, NOW - timedelta(seconds=1), free_bytes=700))
        latest = None
        for index, (age_minutes, boot, kills) in enumerate(readings, start=1):
            sample = _telemetry(
                NODE_A,
                f"00000000-0000-4000-8000-{index:012d}",
                NOW - timedelta(minutes=age_minutes),
                sequence=index,
                cpu=5.0,
                boot_id=f"00000000-0000-4000-8000-0000000000{boot}",
            )
            sample.oom_kills = kills
            sample.received_at += timedelta(minutes=skew_minutes)
            session.add(sample)
            latest = sample
        session.flush()
        assert latest is not None
        session.add(NodeTelemetryLatest(node_id=NODE_A, sample_id=latest.id))
    return [
        w.code
        for w in FleetProjection(sessions, clock=lambda: NOW).read().nodes[0].warnings
    ]


def test_oom_kill_is_advised_only_for_a_rise_within_the_hour() -> None:
    assert "host.oom-kill" in _oom_codes((50, "a1", 0), (20, "a1", 1), (0, "a1", 1))
    assert "host.oom-kill" not in _oom_codes((90, "a1", 0), (70, "a1", 1), (0, "a1", 1))
    assert "host.oom-kill" not in _oom_codes((40, "a1", 5), (0, "b2", 0))
    # The window is the Controller's receive time, not the agent's clock.
    skewed = ((90, "a1", 0), (70, "a1", 1), (0, "a1", 1))
    assert "host.oom-kill" in _oom_codes(*skewed, skew_minutes=60)


def test_low_cpu_clock_is_raised_only_when_sustained_and_hot_or_loaded() -> None:
    codes, details = _cpu_clock_codes(span_seconds=80)
    assert codes == ["cpu.low-clock"]
    assert "2000 of 3900 MHz" in details[0]
    assert "85" in details[0]

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
                inventory_snapshot=inventory_items_fixture(
                    NODE_A,
                    observed_at=NOW - timedelta(seconds=1),
                    identity=AgentRuntimeIdentity.model_validate(
                        {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                    ),
                ),
                contact=AgentNodeContact(last_seen_at=NOW - timedelta(seconds=1)),
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
    assert "over Wi-Fi (wlP9s9, 2.402 Gb/s link)." in warning.detail
    assert warning.recommendation is not None
    assert "network cable to the RJ45 port enP7s7" in warning.recommendation

    up = {"name": "enP7s7", "kind": "wired", "link_speed_mbps": 10000, "carrier": True}
    (warning,) = _route_warnings([up, _WIFI], "wlP9s9")
    assert "10 Gb/s" in (warning.recommendation or "")

    (warning,) = _route_warnings([_WIFI], "wlP9s9")


@pytest.mark.parametrize("with_link", [False, True])
def test_fleet_names_the_spark_that_copies_from_the_nas_far_slower(
    with_link: bool,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    # spark-bbbb: 48.8 GB in 553 s; spark-aaaa: 25.8 GB in 7089 s (2026-10-17).
    transfers = {NODE_A: (48_755_056_484, 553.3), NODE_B: (25_800_000_000, 7089.0)}
    with sessions.begin() as session:
        session.add(
            Job(
                id="job",
                request_id="request",
                kind="recipe.install",
                state="running",
                actor="test",
                authority_revision="a" * 64,
                targets=list(transfers),
                payload_digest="b" * 64,
                payload={},
                created_at=NOW - timedelta(hours=3),
                updated_at=NOW,
            )
        )
        for node_id, name in ((NODE_A, "spark-bbbb"), (NODE_B, "spark-aaaa")):
            session.add(
                AgentNode(
                    node_id=node_id,
                    state="active",
                    inventory_snapshot=inventory_items_fixture(
                        node_id,
                        observed_at=NOW - timedelta(seconds=1),
                        identity=AgentRuntimeIdentity.model_validate(
                            {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                        ),
                    ),
                    contact=AgentNodeContact(last_seen_at=NOW - timedelta(seconds=1)),
                )
            )
            session.flush()
            session.add(_certificate(node_id, f"nas-rate-{node_id}"))
            session.add(_profile(node_id, display_name=name))
            set_inventory_hostname(session, node_id, name)
            size, seconds = transfers[node_id]
            session.add(
                AgentOperation(
                    id=f"transfer-{node_id}",
                    parent_job_id="job",
                    node_id=node_id,
                    kind="artifact.distribution.v1",
                    payload_digest="c" * 64,
                    payload={},
                    authority_revision="a" * 64,
                    state="succeeded",
                    current_attempt=1,
                    created_at=NOW - timedelta(hours=1),
                    updated_at=NOW - timedelta(minutes=30),
                )
            )
            session.add(
                AgentOperationAttempt(
                    operation_id=f"transfer-{node_id}",
                    attempt=1,
                    fence=f"fence-{node_id}",
                    lease_deadline=NOW,
                    agent_certificate_serial=f"nas-rate-{node_id}",
                    state="succeeded",
                    progress={
                        "phase": "completed",
                        "completed_bytes": size,
                        "total_bytes_known": False,
                        "elapsed_seconds": seconds,
                        "fetched": {"bytes": size, "seconds": seconds},
                    },
                )
            )
    if with_link:
        vector = (
            Path(__file__).parents[2]
            / "agent_protocol/src/vonk_agent_protocol/vectors/inventory-wifi-link-v1.json"
        )
        request = InventoryRequest.model_validate_json(vector.read_bytes())
        with sessions.begin() as session:
            inventory = _inventory(NODE_B, NOW, free_bytes=100)
            inventory.network_interfaces = request.network_interfaces
            inventory.nas_route_interface = request.nas_route_interface
            session.add(inventory)
    nodes = {
        node.id: node
        for node in FleetProjection(sessions, clock=lambda: NOW).read().nodes
    }

    def slow(node_id: str) -> list[str]:
        return [
            warning.detail
            for warning in nodes[node_id].warnings
            if warning.code == "network.nas-transfer-slow"
        ]

    assert slow(NODE_A) == []
    (detail,) = slow(NODE_B)
    assert detail.startswith("Copies from the NAS at 3.6 MB/s vs 88.1 MB/s")
    assert "on spark-bbbb" in detail
    if with_link:
        assert (
            "Wi-Fi signal -78 dBm, on 2.4 GHz, TX 866.7 Mbit/s, RX 6.5 Mbit/s" in detail
        )
        assert nodes[NODE_B].inventory is not None
    else:
        assert "Wi-Fi" not in detail


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

    engine = create_engine(f"sqlite:///{tmp_path / 'observations.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    sample_id = "00000000-0000-4000-8000-000000000011"
    inventory = _inventory(NODE_A, NOW, free_bytes=800)
    sample = _telemetry(NODE_A, sample_id, NOW, sequence=1, cpu=50)
    with sessions.begin() as session:
        session.add_all(
            [
                AgentNode(
                    node_id=NODE_A,
                    state="active",
                    contact=AgentNodeContact(last_seen_at=NOW),
                ),
                AgentNode(
                    node_id=NODE_B,
                    state="active",
                    contact=AgentNodeContact(last_seen_at=NOW),
                ),
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
    assert healthy_inventory is not None
    assert healthy_inventory.disk_free_bytes == 750
    if damaged == "inventory":
        inventory_state = nodes[NODE_A].inventory
        assert inventory_state is not None
        assert inventory_state.network_interfaces is None
        assert inventory_state.disk_free_bytes == inventory.disk_free_bytes
    else:
        assert nodes[NODE_A].telemetry is None
    if damaged == "telemetry":
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
                inventory_snapshot=inventory_items_fixture(
                    NODE_A,
                    observed_at=NOW - timedelta(seconds=1),
                    identity=AgentRuntimeIdentity.model_validate(
                        {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                    ),
                ),
                contact=AgentNodeContact(last_seen_at=NOW - timedelta(seconds=1)),
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


def _host_codes(host_settings: dict[str, object] | None) -> list[str]:
    node = _host_node(host_settings)
    return [w.code for w in node.warnings if w.code.startswith("host.")]


def _host_node(
    host_settings: dict[str, object] | None,
    *,
    fabric: dict[str, object] | None = None,
) -> FleetNode:
    """Report one agent inventory through the store and project its node."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=NODE_A,
                state="active",
                inventory_snapshot=inventory_items_fixture(
                    NODE_A,
                    observed_at=NOW - timedelta(seconds=1),
                    identity=AgentRuntimeIdentity.model_validate(
                        {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                    ),
                ),
                contact=AgentNodeContact(last_seen_at=NOW - timedelta(seconds=1)),
            )
        )
        session.flush()
        session.add(_certificate(NODE_A, "host-settings"))
    report = InventoryRequest.model_validate_json(
        canonical_message(
            {
                "schema_version": 1,
                "observed_at": (NOW - timedelta(seconds=1)).isoformat(),
                "disk_total_bytes": 1_000,
                "disk_free_bytes": 700,
                "host_memory_total_bytes": 2_000,
                "host_memory_free_bytes": 1_500,
                "gpu_memory_total_bytes": 2_000,
                "gpu_memory_free_bytes": 1_400,
                "gpu_count": 1,
                "memory_pool": "shared",
                "artifact_store_read_only": False,
                "capabilities": ["runtime.vonk.v1"],
                "nvidia_driver_version": "580.173.02",
                "container_runtime_version": "29.2.1",
            }
            | ({} if host_settings is None else {"host_settings": host_settings})
            | ({} if fabric is None else fabric)
        )
    )
    InventoryRepository(sessions, clock=lambda: NOW).record(
        NodeInventoryReport.model_validate_json(
            canonical_message(
                {
                    "node_id": NODE_A,
                    "observed_at": report.observed_at,
                    "disk_total_bytes": report.disk_total_bytes,
                    "disk_free_bytes": report.disk_free_bytes,
                    "host_memory_total_bytes": report.host_memory_total_bytes,
                    "host_memory_free_bytes": report.host_memory_free_bytes,
                    "gpu_memory_total_bytes": report.gpu_memory_total_bytes,
                    "gpu_memory_free_bytes": report.gpu_memory_free_bytes,
                    "gpu_count": report.gpu_count,
                    "artifact_store_read_only": report.artifact_store_read_only,
                    "capabilities": tuple(report.capabilities),
                    "memory_pool": report.memory_pool,
                    "host_settings": report.host_settings,
                    "fabric_address": report.fabric_address,
                    "fabric_bandwidth_mbps": report.fabric_bandwidth_mbps,
                    "schema_version": 1,
                    "nvidia_driver_version": "unknown",
                    "container_runtime_version": "unknown",
                }
            )
        )
    )
    node = FleetProjection(sessions, clock=lambda: NOW).read().nodes[0]
    assert node.inventory is not None
    assert node.inventory.host_settings == report.host_settings
    return node


def test_a_spark_off_its_headless_settings_needs_attention() -> None:
    # spark-bbbb: the desktop runs and starts again at every boot.
    assert _host_codes(
        {"display_manager_active": True, "graphical_default_target": True}
    ) == ["host.desktop-running", "host.desktop-at-boot"]
    # spark-aaaa: the desktop failed to start, but would be tried at boot.
    assert _host_codes(
        {"display_manager_active": False, "graphical_default_target": True}
    ) == ["host.desktop-at-boot"]
    assert (
        _host_codes(
            {"display_manager_active": False, "graphical_default_target": False}
        )
        == []
    )
    # An older agent reports nothing: unknown is never a warning.
    assert _host_codes(None) == []


def _shared_tuning() -> dict[str, object]:
    vector = json.loads(
        files("vonk_agent_protocol")
        .joinpath("vectors/host-tuning-v1.json")
        .read_text(encoding="utf-8")
    )
    return vector["observation"]


def test_host_tuning_reaches_the_fleet_node_as_advice_never_attention() -> None:
    # Catches a fact lost in storage, or a host check raised as a warning
    # (which would mark every stock Spark as needing attention).

    headless: dict[str, object] = {
        "display_manager_active": False,
        "graphical_default_target": False,
    }
    node = _host_node(headless | {"tuning": _shared_tuning()})
    assert [advice.check for advice in node.host_checks] == list(HostCheck)
    # Every change can be undone, except the reading that changes nothing.
    assert [a.check for a in node.host_checks if not a.undo] == [
        HostCheck.MEMORY_FRAGMENTATION
    ]
    assert node.host_checks_note
    assert [w.code for w in node.warnings if w.code.startswith("host.")] == []
    # An older agent reports no tuning: no advice, nothing guessed.
    assert _host_node(headless).host_checks == []
    assert _host_node(headless).host_checks_note is None


def test_each_host_check_cites_its_reading_and_unknown_or_tuned_gives_none() -> None:

    tuning = HostTuning.model_validate(_shared_tuning())
    advice = {item.check: item for item in host_checks(tuning)}
    # A service check disables every unit of it installed on the Spark, active
    # or not (cups.path), and never one that is not (snapd.snap-repair.timer).
    installed = {item.unit for item in tuning.units or ()}
    for check, units in SERVICE_UNITS.items():
        assert advice[check].commands[-1].split()[4:] == [
            unit.value for unit in units if unit in installed
        ]
    assert "snap-repair" not in advice[HostCheck.SNAPD].commands[-1]
    assert "cups.path" not in advice[HostCheck.PRINTING].finding
    assert "15000" in advice[HostCheck.WATERMARK_BOOST].finding
    assert "20" in advice[HostCheck.PROACTIVE_COMPACTION].finding
    # Measured: 78.7 of 116.7 GiB free in 2 MiB blocks.
    assert "78.7" in advice[HostCheck.MEMORY_FRAGMENTATION].finding
    assert "116.7" in advice[HostCheck.MEMORY_FRAGMENTATION].finding
    # A snap that is not a desktop app is named.
    server = host_checks(tuning.model_copy(update={"snaps": ["core22", "lxd"]}))
    assert "lxd" in next(i for i in server if i.check is HostCheck.SNAPD).explanation
    # The longest explanation fits its bound (every snap unknown, max length).
    many = [f"s{index:02d}" + "x" * 37 for index in range(128)]
    assert host_checks(tuning.model_copy(update={"snaps": many}))
    # Unknown readings and an already tuned host give no advice.
    assert host_checks(None) == []
    assert host_checks(HostTuning()) == []
    assert (
        host_checks(
            HostTuning(
                units=[],
                watermark_boost_factor=0,
                compaction_proactiveness=0,
                kernel_splash=False,
            )
        )
        == []
    )


def test_retention_failure_survives_inventory_storage_and_projects_one_warning() -> (
    None
):
    # Catches journal-only faults and duplicating attention on every retry.

    host = HostSettings(
        display_manager_active=False,
        graphical_default_target=False,
        store_retention_failure=StoreRetentionFailure(
            path="runs/10000000-0000-4000-8000-000000000001",
            diagnostic="Operation not permitted (os error 1)",
        ),
    )
    for _ in range(2):
        assert _host_codes(host.model_dump(mode="json")) == [
            ProjectionCode.HOST_STORE_RETENTION
        ]
    assert (
        _host_codes(
            host.model_copy(update={"store_retention_failure": None}).model_dump(
                mode="json"
            )
        )
        == []
    )


def test_controller_contact_ahead_of_projection_clock_is_fresh() -> None:
    """Catches treating Controller clock rollback as loss of agent contact."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=NODE_A,
                state=NodeIdentityState.ACTIVE,
                contact=AgentNodeContact(last_seen_at=NOW + timedelta(seconds=2)),
            )
        )
        session.flush()
        session.add(_certificate(NODE_A, "future-contact"))
    connection = FleetProjection(sessions, clock=lambda: NOW).read().nodes[0].connection
    assert connection.offline_reason is None
    assert connection.last_seen_age_seconds == 0
    assert connection.last_seen_at == NOW + timedelta(seconds=2)


def test_contact_committed_between_clock_read_and_postgres_snapshot_stays_online(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inject the scheduling gap that lets a later contact enter the snapshot."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=NODE_A,
                state=NodeIdentityState.ACTIVE,
                contact=AgentNodeContact(last_seen_at=NOW - timedelta(seconds=151)),
            )
        )
        session.flush()
        session.add(_certificate(NODE_A, "snapshot-race"))
    begin_capture = service.begin_observation_capture
    clock_sampled = False

    def clock() -> datetime:
        nonlocal clock_sampled
        clock_sampled = True
        return NOW

    def delayed_capture(session: Session) -> None:
        assert clock_sampled
        # Advance the contact producer while capture is delayed, without a
        # timing-sensitive sleep. Its independent transaction commits first.
        with sessions.begin() as writer:
            record_contact(writer, NODE_A, last_seen_at=NOW + timedelta(seconds=2))
        begin_capture(session)
        assert session.scalar(text("SHOW transaction_isolation")) == "repeatable read"

    monkeypatch.setattr(service, "begin_observation_capture", delayed_capture)
    snapshot = FleetProjection(sessions, clock=clock).read()
    assert snapshot.generated_at == NOW
    connection = snapshot.nodes[0].connection
    assert connection.last_seen_at == NOW + timedelta(seconds=2)
    assert connection.offline_reason is None
    assert connection.last_seen_age_seconds == 0


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, 7_400_108_032),
        ({"gpu_process_memory_bytes": 130_000_000_000}, 0),
        ({"gpu_process_memory_bytes": None}, None),
        ({"memory_total_bytes": None, "memory_available_bytes": None}, None),
    ],
)
def test_system_memory_is_everything_in_use_outside_gpu_processes(
    changes: dict[str, int | None], expected: int | None
) -> None:

    vector = json.loads(
        (
            files("vonk_agent_protocol") / "vectors" / "telemetry-sample-v1.json"
        ).read_text()
    )
    view = TelemetrySampleView.model_validate_json(
        canonical_message(
            {
                **vector["sample"],
                **changes,
                "id": "00000000-0000-4000-8000-000000000012",
                "node_id": NODE_A,
                "received_at": "2026-09-05T12:00:00.250000Z",
            }
        )
    )
    assert telemetry_point(view).system_memory_bytes == expected


def _fragmentation_vector() -> dict[str, Any]:
    return json.loads(
        files("vonk_agent_protocol")
        .joinpath("vectors/memory-fragmentation-v1.json")
        .read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    "case", _fragmentation_vector()["cases"], ids=lambda case: case["name"]
)
def test_memory_fragmentation_readings_decide_the_reboot_advice(
    case: dict[str, object],
) -> None:
    host: dict[str, object] = {
        "display_manager_active": False,
        "graphical_default_target": False,
    }
    if case["readings"] is not None:
        host["memory_fragmentation"] = case["readings"]
        readings = MemoryFragmentation.model_validate(case["readings"])
        assert assess(readings).grown_bytes == case["grown_bytes"]
    advised = "host.memory-fragmented" in _host_codes(host)
    assert advised is case["fragmented"]


def test_page_table_growth_is_judged_in_pages_not_bytes() -> None:
    # 512 leftover pages is the margin at any page size: 2 MiB at 4 KiB, 32 MiB
    # at 64 KiB; one page more advises a reboot.
    for page_size in (4096, 65536):
        for pages, advised in ((512, False), (513, True)):
            readings = MemoryFragmentation.model_validate(
                {
                    "boot_id": "6f1c1d8e-0000-4000-8000-000000000001",
                    "page_size_bytes": page_size,
                    "idle_stop_first_bytes": 1000,
                    "idle_stop_latest_bytes": 1000 + pages * page_size,
                }
            )
            assert assess(readings).reboot_advised is advised


def _rail_vector() -> dict[str, list[dict[str, object]]]:
    return json.loads(
        files("vonk_agent_protocol")
        .joinpath("vectors/fabric-devices-v1.json")
        .read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("case", _rail_vector()["cases"], ids=lambda case: case["name"])
def test_fabric_rails_and_fabric_health_reach_the_node_as_attention(
    case: dict[str, object],
) -> None:
    host: dict[str, object] = {
        "display_manager_active": False,
        "graphical_default_target": False,
        "fabric_devices": case["fabric_devices"],
    }
    fabric = {"fabric_address": "10.0.0.1", "fabric_bandwidth_mbps": 200_000}
    if "fabric_bandwidth_mbps" in case:
        fabric["fabric_bandwidth_mbps"] = case["fabric_bandwidth_mbps"]
    node = _host_node(host, fabric=fabric)
    reasons = [w for w in node.warnings if w.code == "network.rails-underused"]
    assert bool(reasons) is case["underused"]
    assert [
        w.code for w in node.warnings if w.code.startswith("network.fabric-")
    ] == case.get("codes", [])


@pytest.mark.parametrize(
    ("fabric_address", "codes"),
    [("10.0.0.1", ["network.fabric-down"]), (None, [])],
)
def test_no_active_fabric_port_on_a_spark_with_a_fabric_is_attention(
    fabric_address: str | None, codes: list[str]
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    sample_id = "00000000-0000-4000-8000-000000000011"
    inventory = _inventory(NODE_A, NOW - timedelta(seconds=1), free_bytes=700)
    inventory.fabric_address = fabric_address
    inventory.fabric_bandwidth_mbps = None if fabric_address is None else 200_000
    sample = _telemetry(
        NODE_A, sample_id, NOW - timedelta(seconds=1), sequence=1, cpu=5
    )
    sample.fabric_ports_active = 0
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=NODE_A,
                state="active",
                contact=AgentNodeContact(last_seen_at=NOW - timedelta(seconds=1)),
            )
        )
        session.flush()
        session.add_all(
            [
                inventory,
                sample,
                NodeTelemetryLatest(node_id=NODE_A, sample_id=sample_id),
            ]
        )
    node = FleetProjection(sessions, clock=lambda: NOW).read().nodes[0]
    assert [
        w.code for w in node.warnings if w.code.startswith("network.fabric-")
    ] == codes
