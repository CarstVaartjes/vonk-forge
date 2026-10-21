"""Reject unsafe or conflicting intent before effects; preserve accepted assignments."""

from __future__ import annotations

import json
from importlib.resources import files

import pytest
from pydantic import ValidationError
from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.gpu_clock import (
    GPU_CLOCK_POLICY,
    GpuClockIntent,
    GpuClockRange,
    GpuClockSupport,
    default_gpu_clock,
)
from vonk_agent_protocol.host_helper import ApplyGpuClockOperation, parse_host_operation
from vonk_control.fleet_profile_contract import FleetProfileDefinition

NODE = "spk_" + "a" * 32


def test_shared_gpu_clock_vectors_preserve_start_and_helper_intent() -> None:
    vectors = json.loads(
        files("vonk_agent_protocol").joinpath("vectors/gpu-clock-v1.json").read_text()
    )
    for raw in vectors["valid"]:
        desired = GpuClockIntent(
            generation=1, range=GpuClockRange.model_validate_json(json.dumps(raw))
        )
        operation = ApplyGpuClockOperation(desired=desired)
        assert (
            parse_host_operation(json.loads(canonical_message(operation))) == operation
        )
        assert (
            GpuClockIntent.model_validate_json(canonical_message(desired)).range
            == desired.range
        )
    for raw in vectors["invalid"]:
        with pytest.raises(ValidationError):
            GpuClockRange.model_validate_json(json.dumps(raw))


def test_hardware_safety_edge_explains_power_off_risk_and_never_clamps() -> None:
    with pytest.raises(
        ValidationError, match="hardware-safety edge.*hard-powered Sparks off"
    ):
        GpuClockRange(
            min_mhz=GPU_CLOCK_POLICY.default_min_mhz,
            max_mhz=GPU_CLOCK_POLICY.hard_max_mhz + 1,
        )


def test_profile_assignments_roundtrip_and_refuse_conflicting_ranges() -> None:
    definition = {
        "assignments": [
            {
                "recipe_selector": "vonk/model",
                "spark_ids": [NODE],
                "gpu_clock": {"min_mhz": 300, "max_mhz": 1800},
            }
        ]
    }
    profile = FleetProfileDefinition.model_validate_json(json.dumps(definition))
    assert (
        FleetProfileDefinition.model_validate_json(canonical_message(profile))
        == profile
    )
    default = FleetProfileDefinition.model_validate_json(
        json.dumps(
            {"assignments": [{"recipe_selector": "vonk/model", "spark_ids": [NODE]}]}
        )
    )
    assert (
        default.assignments[0].gpu_clock or default_gpu_clock()
    ).max_mhz == GPU_CLOCK_POLICY.default_max_mhz
    definition["assignments"].append(
        {"recipe_selector": "vonk/other", "spark_ids": [NODE]}
    )
    with pytest.raises(ValidationError, match="conflicting GPU clock ranges"):
        FleetProfileDefinition.model_validate_json(json.dumps(definition))


def test_hardware_supported_minimum_and_maximum_are_checked_independently_of_policy() -> (
    None
):
    support = GpuClockSupport(min_mhz=500, max_mhz=2000, levels_mhz=[500, 1800, 2000])
    for requested in [GpuClockRange(min_mhz=300, max_mhz=1800), default_gpu_clock()]:
        with pytest.raises(ValueError, match="outside this Spark's reported"):
            support.validate_range(requested)
    support.validate_range(GpuClockRange(min_mhz=500, max_mhz=1800))


def test_run_switch_warns_above_default_and_refuses_unknown_or_unsupported_hardware() -> (
    None
):
    """Catches using the policy ceiling as a substitute for driver evidence."""
    from datetime import UTC, datetime

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from vonk_agent_protocol import RunSwitchCode
    from vonk_agent_protocol.inventory import HostSettings
    from vonk_control.gpu_clock_validation import assess_gpu_clock
    from vonk_control.inventory_repository import (
        InventoryRepository,
        InventorySnapshotInput,
    )
    from vonk_control.models import AgentNode, Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=NODE, state="active"))
    repository = InventoryRepository(sessions, clock=lambda: now)
    observation = InventorySnapshotInput(
        node_id=NODE,
        observed_at=now,
        disk_total_bytes=1000,
        disk_free_bytes=500,
        host_memory_total_bytes=1000,
        host_memory_free_bytes=500,
        gpu_memory_total_bytes=0,
        gpu_memory_free_bytes=0,
        gpu_count=1,
        memory_pool="shared",
        artifact_store_read_only=False,
        capabilities=(),
        nvidia_driver_version="580",
        container_runtime_version="29",
        host_settings=HostSettings(
            display_manager_active=False,
            graphical_default_target=False,
            gpu_clock_support=GpuClockSupport(
                min_mhz=300, max_mhz=2400, levels_mhz=[300, 1800, 2200, 2300, 2400]
            ),
        ),
    )
    repository.record(observation)
    with sessions() as session:
        blockers, warnings = assess_gpu_clock(
            session, [NODE], GpuClockRange(min_mhz=300, max_mhz=2250)
        )
        assert not blockers
        assert [warning.code for warning in warnings] == [
            RunSwitchCode.GPU_CLOCK_ABOVE_DEFAULT
        ]
        blockers, _ = assess_gpu_clock(
            session, [NODE], GpuClockRange(min_mhz=200, max_mhz=1800)
        )
        assert blockers[0].code == RunSwitchCode.GPU_CLOCK_INVALID
        blockers, _ = assess_gpu_clock(
            session, ["spk_" + "b" * 32], default_gpu_clock()
        )
        assert "unknown" in blockers[0].detail


def test_failed_reassertion_warns_even_when_the_previous_receipt_matches_desired() -> (
    None
):
    """A failed hourly write must not look healthy because an older command succeeded."""
    import uuid
    from datetime import UTC, datetime

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from vonk_agent_protocol import ProjectionCode
    from vonk_agent_protocol.gpu_clock import GpuClockApplyState, GpuClockObservation
    from vonk_control.fleet_projection import FleetProjection
    from vonk_control.models import AgentNode, Base
    from vonk_control.telemetry import TelemetryRepository, TelemetrySampleInput

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=NODE, state="active"))
    TelemetryRepository(sessions, clock=lambda: now).record_batch(
        NODE,
        (
            TelemetrySampleInput(
                boot_id=uuid.uuid4(),
                observed_at=now,
                memory_total_bytes=None,
                memory_available_bytes=None,
                disk_total_bytes=None,
                disk_free_bytes=None,
                gpu_utilization_percent=None,
                gpu_memory_total_bytes=None,
                gpu_memory_free_bytes=None,
                gpu_clock=GpuClockObservation(
                    locked_range=default_gpu_clock(),
                    apply_state=GpuClockApplyState.WAITING,
                    detail="injected driver outage",
                ),
            ),
        ),
    )
    node = FleetProjection(sessions, clock=lambda: now).read().nodes[0]
    assert ProjectionCode.HOST_GPU_CLOCK_UNCAPPED in [
        warning.code for warning in node.warnings
    ]


def test_older_agent_default_start_omits_the_new_strict_wire_field() -> None:
    """An old agent accepts the unchanged start payload and keeps its boot cap."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from vonk_control.gpu_clock import gpu_clock_start_intent
    from vonk_control.models import Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        assert gpu_clock_start_intent(session, NODE) is None


from .test_agent_api import NODE_A, agent_headers, make_agent_system, telemetry_payload
from .test_agent_api import agent_system as _agent_system

agent_system = _agent_system


def test_shared_agent_observation_crosses_controller_storage_projection_and_cli(
    tmp_path, postgres_engine, capsys
) -> None:
    """Catches any producer/store/projection/client dropping real clock evidence."""
    from vonk_agent_protocol import TelemetryRequest
    from vonk_agent_protocol.gpu_clock import GpuClockObservation
    from vonk_control.fleet_projection import FleetProjection

    from cluster_profiles.cli_render import render_payload

    client, services, _, clock = make_agent_system(tmp_path, engine=postgres_engine)
    vectors = json.loads(
        files("vonk_agent_protocol").joinpath("vectors/gpu-clock-v1.json").read_bytes()
    )
    base = TelemetryRequest.model_validate_json(
        canonical_message(telemetry_payload(clock))
    )
    observation = GpuClockObservation.model_validate_json(
        json.dumps(vectors["observation"])
    )
    report = TelemetryRequest(
        samples=[base.samples[0].model_copy(update={"gpu_clock": observation})]
    )
    response = client.post(
        "/agent/telemetry",
        headers=agent_headers(NODE_A, "serial-a"),
        json=report.document(),
    )
    assert response.status_code == 204
    node = next(
        node
        for node in FleetProjection(services.sessions, clock=lambda: clock.now)
        .read()
        .nodes
        if node.id == NODE_A
    )
    assert node.telemetry is not None and node.telemetry.sample is not None
    assert node.telemetry.sample.gpu_clock == report.samples[0].gpu_clock
    render_payload(node.model_dump(mode="json"), "fleet", action="detail")
    output = capsys.readouterr().out
    assert "graphics 1700 / SM 1695 MHz" in output
    assert "last applied 300–2200 MHz (helper receipt)" in output
    assert "reported max graphics 2400 / SM 2300 MHz" in output
    assert "throttle bits 4" in output


def test_inventory_without_supported_levels_still_gets_a_clock_reconciliation_grant(
    agent_system,
) -> None:
    """Catches the silent grant gate when GB10 supported-clock inventory is absent."""
    from vonk_agent_protocol import InventoryVerdict
    from vonk_agent_protocol.inventory import HostSettings, InventoryRequest

    client, services, _, clock = agent_system
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from vonk_control.host_helper_authority import (
        HostHelperGrantIssuer,
        HostRuntimeAuthorityService,
    )

    signer = HostHelperGrantIssuer(ed25519.Ed25519PrivateKey.generate(), clock=clock)
    object.__setattr__(
        services,
        "host_runtime_authority",
        HostRuntimeAuthorityService(services.sessions, signer, clock=clock),
    )
    report = InventoryRequest(
        schema_version=1,
        observed_at=clock.now,
        disk_total_bytes=1000,
        disk_free_bytes=500,
        host_memory_total_bytes=1000,
        host_memory_free_bytes=500,
        gpu_memory_total_bytes=0,
        gpu_memory_free_bytes=0,
        gpu_count=1,
        memory_pool="shared",
        artifact_store_read_only=False,
        capabilities=[],
        nvidia_driver_version="580",
        container_runtime_version="29",
        host_settings=HostSettings(
            display_manager_active=False, graphical_default_target=False
        ),
    )
    response = client.post(
        "/agent/inventory",
        headers=agent_headers(NODE_A, "serial-a"),
        json=json.loads(canonical_message(report)),
    )
    assert response.status_code == 200
    verdict = InventoryVerdict.model_validate_json(response.content)
    assert verdict.gpu_clock_grant is not None
