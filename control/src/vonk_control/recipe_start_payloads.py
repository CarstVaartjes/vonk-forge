"""Canonical recipe start payload construction."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from vonk_agent_protocol import (
    InvalidRequestError,
    RecipeStartPayload,
    canonical_message,
)
from vonk_agent_protocol.compiled_execution_plan import (
    CompiledExecutionPlan as WireCompiledExecutionPlan,
)

from .run_switch_contract import MemoryKind


class RecipeStartPayloadError(InvalidRequestError, ValueError):
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
    compiled_execution_plan: WireCompiledExecutionPlan,
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
    value: WireCompiledExecutionPlan,
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
