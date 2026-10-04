"""Canonical recipe start payload construction."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from vonk_agent_protocol import RecipeStartPayload, canonical_message

from .run_switch_contract import MemoryKind


class RecipeStartPayloadError(ValueError):
    """A Controller start payload cannot cross the agent wire boundary."""


def validate_distributed_start_timeout_seconds(value: object) -> int:
    """Validate the operator budget shared by initial start and exact recovery."""

    if type(value) is not int or not 60 <= value <= 3600:
        raise RecipeStartPayloadError("distributed start timeout is invalid")
    return value


@dataclass(frozen=True, slots=True)
class RecipeStartPlacement:
    node_id: str
    rank: int
    role: str
    port: int
    reserved_memory_bytes: int
    memory_floor_bytes: int
    memory_kind: MemoryKind
    fabric_address: str | None


def build_recipe_start_payload(
    *,
    run_id: str,
    installation_id: str,
    recipe_revision_id: str,
    mapping_id: str,
    run_generation: int,
    plan_digest: str,
    placement: RecipeStartPlacement,
    compiled_endpoint_address: str | None,
    world_size: int,
    compiled_execution_plan: Mapping[str, object],
    master_address: str | None,
    master_port: int | None,
    phase: str | None = None,
    start_deadline: str | None = None,
) -> dict[str, object]:
    """Build and validate one start payload; placement lives in the plan."""

    try:
        payload: dict[str, object] = {
            "run_id": run_id,
            "installation_id": installation_id,
            "recipe_revision_id": recipe_revision_id,
            "mapping_id": mapping_id,
            "plan_digest": plan_digest,
            "compiled_execution_plan": _bind_compiled_execution_plan(
                compiled_execution_plan,
                placement=placement,
                endpoint_address=compiled_endpoint_address,
                master_address=master_address,
                master_port=master_port,
                world_size=world_size,
            ),
        }
        payload["run_generation"] = run_generation
        if phase is not None:
            payload.update(
                {
                    "phase": phase,
                    "start_deadline": start_deadline,
                }
            )
        RecipeStartPayload.model_validate(payload)
    except (KeyError, TypeError, ValueError) as error:
        raise RecipeStartPayloadError("recipe start payload is invalid") from error
    return payload


def _bind_compiled_execution_plan(
    value: Mapping[str, object],
    *,
    placement: RecipeStartPlacement,
    endpoint_address: str | None,
    master_address: str | None,
    master_port: int | None,
    world_size: int,
) -> dict[str, object]:
    payload = json.loads(canonical_message(value))
    runtime = payload.get("runtime")
    compiled_placement = (
        runtime.get("placement") if isinstance(runtime, Mapping) else None
    )
    if not isinstance(runtime, dict) or not isinstance(compiled_placement, dict):
        raise RecipeStartPayloadError("compiled execution plan placement is invalid")
    compiled_placement.update(
        {
            "endpoint_address": endpoint_address,
            "rank": placement.rank,
            "role": placement.role,
            "world_size": world_size,
            "local_address": placement.fabric_address if world_size > 1 else None,
            "master_address": master_address,
            "master_port": master_port,
            "port": placement.port,
            "reserved_memory_bytes": placement.reserved_memory_bytes,
            "memory_floor_bytes": placement.memory_floor_bytes,
        }
    )
    if world_size > 1 and master_port is not None:
        _serve_on_placement_port(payload, runtime, placement.port)
    security = payload.get("security")
    if isinstance(security, dict):
        native_fabric = world_size > 1 and master_port is not None
        security["network_mode"] = (
            "host"
            if native_fabric
            else "bridge"
            if endpoint_address is not None
            else "none"
        )
    return payload


def _serve_on_placement_port(
    payload: dict[str, object], runtime: dict[str, object], port: int
) -> None:
    """Make a host-networked engine serve on the port the run was admitted with.

    Installation compiles the recipe's declared port into the endpoint and the
    engine command, but a host-networked rank has no Docker publication to map
    it: the engine must itself listen on the port the Spark firewall
    authorises. Rewriting here, where every start and recovery payload is
    built, also covers installations compiled before the platform chose it.
    """

    endpoint = payload.get("endpoint")
    if not isinstance(endpoint, dict) or type(endpoint.get("port")) is not int:
        return
    declared = endpoint["port"]
    if declared == port:
        return
    endpoint["port"] = port
    argv = runtime.get("argv")
    if not isinstance(argv, list):
        return
    runtime["argv"] = [
        _rebound_port_argument(
            argument, argv[index - 1] if index else None, declared, port
        )
        for index, argument in enumerate(argv)
    ]


def _rebound_port_argument(
    argument: object, previous: object, declared: int, port: int
) -> object:
    if argument == f"--port={declared}":
        return f"--port={port}"
    if argument == str(declared) and previous == "--port":
        return str(port)
    return argument
