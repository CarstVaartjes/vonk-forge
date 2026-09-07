"""Canonical schema-2 recipe start payload construction."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from vonk_agent_protocol import RecipeStartPayload, canonical_message


class RecipeStartPayloadError(ValueError):
    """A Controller start payload cannot cross the agent wire boundary."""


@dataclass(frozen=True, slots=True)
class RecipeStartPlacement:
    node_id: str
    rank: int
    role: str
    port: int
    reserved_memory_bytes: int
    fabric_address: str | None


def build_recipe_start_payload(
    *,
    run_id: str,
    installation_id: str,
    recipe_revision_id: str,
    recipe_content_sha256: str,
    mapping_id: str,
    mapping_generation: int,
    run_generation: int | None,
    image_digest: str,
    plan_digest: str,
    alias: str,
    placement: RecipeStartPlacement,
    endpoint_address: str,
    compiled_endpoint_address: str | None,
    world_size: int,
    compiled_execution_plan: Mapping[str, object],
    local_address: str | None,
    master_address: str | None,
    master_port: int | None,
    phase: str | None = None,
    start_deadline: str | None = None,
) -> dict[str, object]:
    """Build and validate one complete current schema-2 start payload."""

    try:
        payload: dict[str, object] = {
            "schema_version": 2,
            "run_id": run_id,
            "installation_id": installation_id,
            "recipe_revision_id": recipe_revision_id,
            "recipe_content_sha256": recipe_content_sha256,
            "mapping_id": mapping_id,
            "mapping_generation": mapping_generation,
            "image_digest": image_digest,
            "plan_digest": plan_digest,
            "alias": alias,
            "rank": placement.rank,
            "role": placement.role,
            "port": placement.port,
            "reserved_memory_bytes": placement.reserved_memory_bytes,
            "endpoint_address": endpoint_address,
            "world_size": world_size,
            "compiled_execution_plan": _bind_compiled_execution_plan(
                compiled_execution_plan,
                placement=placement,
                endpoint_address=compiled_endpoint_address,
                master_address=master_address,
                master_port=master_port,
                world_size=world_size,
            ),
            "local_address": local_address,
            "master_address": master_address,
            "master_port": master_port,
        }
        if phase is not None:
            payload.update(
                {
                    "phase": phase,
                    "start_deadline": start_deadline,
                    "run_generation": run_generation,
                }
            )
        elif run_generation is not None:
            payload["run_generation"] = run_generation
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
        }
    )
    security = payload.get("security")
    if isinstance(security, dict):
        security["network_mode"] = (
            "bridge"
            if endpoint_address is not None or master_port is not None
            else "none"
        )
    topology = payload.get("topology")
    if isinstance(topology, dict):
        topology.update(
            {"rank": placement.rank, "role": placement.role, "world_size": world_size}
        )
    return payload
