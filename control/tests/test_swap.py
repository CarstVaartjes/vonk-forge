"""Swap size: policy bounds, the grant only a swap-reporting agent reads, and visibility."""

from __future__ import annotations

import json
from datetime import timedelta
from importlib.resources import files
from typing import TypedDict

import pytest
from pydantic import JsonValue, TypeAdapter, ValidationError
from vonk_agent_protocol import ProjectionCode, canonical_message
from vonk_agent_protocol.host_helper import ApplySwapOperation, parse_host_operation
from vonk_agent_protocol.inventory import HostSettings, InventoryRequest
from vonk_agent_protocol.swap import (
    GIB,
    SWAP_POLICY,
    SwapApplyState,
    SwapCheckpoint,
    SwapIntent,
    SwapObservation,
    SwapWaitReason,
    default_swap,
    swap_needed_bytes,
    swap_reaches,
    swap_satisfied,
    usable_swap_bytes,
)
from vonk_control.fleet_projection.nodes import _swap_warning

from .test_agent_api import NODE_A, Clock, agent_headers
from .test_agent_api import agent_system as _agent_system

agent_system = _agent_system
OS_SWAP_BYTES = 16 * GIB - 4096


class _Usable(TypedDict):
    total_bytes: int
    usable_bytes: int


class _Needed(TypedDict):
    load_bytes: int
    swap_bytes: int


class _Vectors(TypedDict):
    valid: list[dict[str, JsonValue]]
    invalid: list[dict[str, JsonValue]]
    observation: dict[str, JsonValue]
    usable: list[_Usable]
    needed: list[_Needed]


def _vectors() -> _Vectors:
    return TypeAdapter(_Vectors).validate_json(
        files("vonk_agent_protocol")
        .joinpath("vectors/swap-v1.json")
        .read_text(encoding="utf-8")
    )


def test_shared_vectors_bind_the_policy_and_refuse_out_of_range_sizes() -> None:
    vectors = _vectors()
    for value in vectors["valid"]:
        intent = SwapIntent.model_validate_json(json.dumps(value))
        operation = parse_host_operation(
            json.loads(canonical_message(ApplySwapOperation(desired=intent)))
        )
        assert isinstance(operation, ApplySwapOperation)
        assert operation.desired == intent
    for value in vectors["invalid"]:
        with pytest.raises(ValidationError):
            SwapIntent.model_validate_json(json.dumps(value))
    SwapObservation.model_validate_json(json.dumps(vectors["observation"]))
    assert (
        SWAP_POLICY.minimum_gib,
        SWAP_POLICY.default_gib,
        SWAP_POLICY.maximum_gib,
    ) == (
        16,
        64,
        128,
    )
    # Refused with the policy's own explanation, never clamped to a bound.
    with pytest.raises(ValidationError, match="from 16 to 128"):
        SwapIntent(generation=1, size_gib=256)


def test_usable_and_needed_swap_follow_the_shared_vectors_at_each_margin() -> None:
    """Catches admission and the host memory guard disagreeing on swap margins.

    The Rust guard test reads the same vectors: a load at the usable bound is
    never stopped, one byte past it is.
    """
    vectors = _vectors()
    for pair in vectors["usable"]:
        assert usable_swap_bytes(pair["total_bytes"]) == pair["usable_bytes"]
    for pair in vectors["needed"]:
        needed = swap_needed_bytes(pair["load_bytes"])
        assert needed == pair["swap_bytes"]
        assert usable_swap_bytes(needed) >= pair["load_bytes"]
        # The smallest whole-GiB total: one GiB less no longer holds it.
        assert needed == 0 or usable_swap_bytes(needed - GIB) < pair["load_bytes"]


def test_active_swap_reaches_a_size_within_the_header_page_tolerance() -> None:
    """Catches an exact byte comparison against a size the kernel reports less of."""
    two_areas = 128 * GIB - 2 * 4096
    assert swap_reaches(two_areas, 128 * GIB)
    assert not swap_reaches(
        128 * GIB - SWAP_POLICY.tolerance_mib * 1024**2 - 1, 128 * GIB
    )


def test_the_os_swap_meets_16_gib_but_not_the_64_gib_default() -> None:
    assert swap_satisfied(OS_SWAP_BYTES, SwapIntent(generation=0, size_gib=16))
    assert not swap_satisfied(OS_SWAP_BYTES, default_swap())
    assert swap_satisfied(64 * GIB, default_swap())


def _report(clock: Clock, swap: SwapObservation | None) -> InventoryRequest:
    return InventoryRequest(
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
            display_manager_active=False, graphical_default_target=False, swap=swap
        ),
    )


def test_only_an_agent_that_reports_swap_gets_a_signed_swap_grant(
    agent_system,
) -> None:
    """An older agent's strict reader would refuse the field; a newer one needs it."""
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from vonk_agent_protocol import InventoryVerdict
    from vonk_control.host_helper_authority import (
        HostHelperGrantIssuer,
        HostRuntimeAuthorityService,
    )

    client, services, _, clock = agent_system
    signer = HostHelperGrantIssuer(ed25519.Ed25519PrivateKey.generate(), clock=clock)
    object.__setattr__(
        services,
        "host_runtime_authority",
        HostRuntimeAuthorityService(services.sessions, signer, clock=clock),
    )

    def verdict(swap: SwapObservation | None) -> InventoryVerdict:
        response = client.post(
            "/agent/inventory",
            headers=agent_headers(NODE_A, "serial-a"),
            json=json.loads(canonical_message(_report(clock, swap))),
        )
        assert response.status_code == 200, response.text
        return InventoryVerdict.model_validate_json(response.content)

    older = verdict(None)
    assert older.gpu_clock_grant is not None
    assert older.swap_grant is None
    clock.now += timedelta(seconds=30)
    newer = verdict(SwapObservation(total_bytes=OS_SWAP_BYTES, free_bytes=0))
    assert newer.swap_grant is not None
    operation = newer.swap_grant.claims.operation
    assert isinstance(operation, ApplySwapOperation)
    assert operation.desired.size_gib == SWAP_POLICY.default_gib
    assert newer.swap_grant.claims.node_id == NODE_A


def test_less_swap_than_desired_is_a_typed_warning_with_the_helper_cause() -> None:
    receipt = SwapCheckpoint(
        desired=default_swap(1),
        state=SwapApplyState.WAITING,
        reason=SwapWaitReason.INSUFFICIENT_DISK,
        detail="a swap file needs more space",
        next_attempt_at=1060,
        deadline=1600,
    )
    host = HostSettings(
        display_manager_active=False,
        graphical_default_target=False,
        swap=SwapObservation(
            total_bytes=OS_SWAP_BYTES, free_bytes=OS_SWAP_BYTES, receipt=receipt
        ),
    )
    warning = _swap_warning(host, default_swap(1))
    assert warning is not None
    assert warning.code == ProjectionCode.HOST_SWAP_SHORT
    assert "16.0 GiB of 64 GiB desired" in warning.detail
    assert SwapWaitReason.INSUFFICIENT_DISK.value in warning.detail
    assert warning.recommendation is not None and "1600" in warning.recommendation
    # Enough swap, or an agent that does not report swap: no warning.
    enough = host.model_copy(
        update={"swap": SwapObservation(total_bytes=64 * GIB, free_bytes=64 * GIB)}
    )
    assert _swap_warning(enough, default_swap(1)) is None
    unknown = host.model_copy(update={"swap": None})
    assert _swap_warning(unknown, default_swap(1)) is None


def test_fleet_detail_shows_active_swap_beside_desired_and_the_receipt(
    capsys,
) -> None:
    from cluster_profiles.cli_render.fleet import _swap

    host = {
        "swap": {
            "total_bytes": OS_SWAP_BYTES,
            "free_bytes": OS_SWAP_BYTES,
            "receipt": {
                "desired": {"generation": 1, "size_gib": 64},
                "state": SwapApplyState.WAITING.value,
                "reason": SwapWaitReason.RELEASE_DEFERRED.value,
                "detail": "old swap file kept",
                "deadline": 1600,
            },
        }
    }
    node = {
        "swap_desired": {"generation": 1, "size_gib": 64},
        "swap_policy": SWAP_POLICY.model_dump(mode="json"),
    }
    text = _swap({**node, "inventory": {"host_settings": host}})
    assert "desired 64 GiB (policy 16–128 GiB)" in text
    assert f"waiting ({SwapWaitReason.RELEASE_DEFERRED.value})" in text
    assert "old swap file kept" in text
    assert "unknown" in _swap(node).lower()
