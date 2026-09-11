"""Strict payload contracts for the durable Fleet outbox."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator

from .strict_json import StrictJSONModel


class _FleetEventModel(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class NodeProfilePayload(_FleetEventModel):
    schema_version: Literal[1] = 1
    node_id: Annotated[str, Field(min_length=1, max_length=128)]
    profile_changed: bool | None = None
    display_name_changed: bool | None = None

    @model_validator(mode="after")
    def has_one_change(self) -> NodeProfilePayload:
        if (self.profile_changed is True) == (self.display_name_changed is True):
            raise ValueError("node profile event must identify one profile change")
        return self


class NodeTelemetryPayload(_FleetEventModel):
    schema_version: Literal[1] = 1
    node_id: Annotated[str, Field(min_length=1, max_length=128)]
    sample_id: Annotated[str, Field(min_length=1, max_length=128)]


class RecipeInstallationPayload(_FleetEventModel):
    schema_version: Literal[1] = 1
    entity_kind: Literal["recipe-installation"]
    entity_id: Annotated[str, Field(min_length=1, max_length=256)]
    recipe_revision_id: Annotated[str, Field(min_length=1)]
    mapping_id: Annotated[str, Field(min_length=1)]
    mapping_generation: int = Field(strict=True, ge=1)
    state: Annotated[str, Field(min_length=1)]


class InstallationNodePayload(_FleetEventModel):
    schema_version: Literal[1] = 1
    entity_kind: Literal["installation-node"]
    entity_id: Annotated[str, Field(min_length=1, max_length=256)]
    installation_id: Annotated[str, Field(min_length=1)]
    node_id: Annotated[str, Field(min_length=1, max_length=128)]
    rank: int = Field(strict=True, ge=0)
    role: Annotated[str, Field(min_length=1)]
    state: Annotated[str, Field(min_length=1)]
    installed_bytes: int = Field(strict=True, ge=0)
    required_bytes: int = Field(strict=True, ge=0)


class RecipeRunPayload(_FleetEventModel):
    schema_version: Literal[1] = 1
    entity_kind: Literal["recipe-run"]
    entity_id: Annotated[str, Field(min_length=1, max_length=256)]
    installation_id: Annotated[str, Field(min_length=1)]
    mapping_id: Annotated[str, Field(min_length=1)]
    mapping_generation: int = Field(strict=True, ge=1)
    alias: Annotated[str, Field(min_length=1)]
    state: Annotated[str, Field(min_length=1)]
    route_state: Annotated[str, Field(min_length=1)]


class RunNodePayload(_FleetEventModel):
    schema_version: Literal[1] = 1
    entity_kind: Literal["run-node"]
    entity_id: Annotated[str, Field(min_length=1, max_length=256)]
    run_id: Annotated[str, Field(min_length=1)]
    node_id: Annotated[str, Field(min_length=1, max_length=128)]
    rank: int = Field(strict=True, ge=0)
    role: Annotated[str, Field(min_length=1)]
    state: Annotated[str, Field(min_length=1)]
    reserved_memory_bytes: int = Field(strict=True, ge=0)
    observed_memory_bytes: int | None = Field(default=None, strict=True, ge=0)


class JobPayload(_FleetEventModel):
    schema_version: Literal[1] = 1
    entity_kind: Literal["job"]
    entity_id: Annotated[str, Field(min_length=1, max_length=256)]
    kind: Annotated[str, Field(min_length=1)]
    state: Annotated[str, Field(min_length=1)]
    target_count: int = Field(strict=True, ge=0)


class AgentOperationPayload(_FleetEventModel):
    schema_version: Literal[1] = 1
    entity_kind: Literal["agent-operation"]
    entity_id: Annotated[str, Field(min_length=1, max_length=256)]
    parent_job_id: Annotated[str, Field(min_length=1)]
    node_id: Annotated[str, Field(min_length=1, max_length=128)]
    kind: Annotated[str, Field(min_length=1)]
    state: Annotated[str, Field(min_length=1)]
    attempt: int = Field(strict=True, ge=0)


type _FleetEntityPayload = (
    RecipeInstallationPayload
    | InstallationNodePayload
    | RecipeRunPayload
    | RunNodePayload
    | JobPayload
    | AgentOperationPayload
)

type FleetEventPayload = NodeProfilePayload | NodeTelemetryPayload | _FleetEntityPayload


def validate_fleet_event_payload(
    event_type: str,
    entity_kind: str,
    entity_id: str,
    node_id: str | None,
    payload: Mapping[str, object],
) -> FleetEventPayload:
    """Validate one outbox payload against its producer/source identity."""

    allowed_kinds: dict[str, frozenset[str]] = {
        "node-profile": frozenset({"node-profile"}),
        "node-telemetry": frozenset({"node-telemetry-latest"}),
        "recipe-state": frozenset(
            {
                "recipe-installation",
                "installation-node",
                "recipe-run",
                "run-node",
            }
        ),
        "operation-state": frozenset({"job", "agent-operation"}),
    }
    if entity_kind not in allowed_kinds.get(event_type, frozenset()):
        raise ValueError("Fleet event source kind does not match event type")
    if event_type == "node-profile":
        value = NodeProfilePayload.model_validate(payload)
        if value.node_id != entity_id or value.node_id != node_id:
            raise ValueError("node profile event identity is inconsistent")
        return value
    if event_type == "node-telemetry":
        value = NodeTelemetryPayload.model_validate(payload)
        if value.node_id != entity_id or value.node_id != node_id:
            raise ValueError("node telemetry event identity is inconsistent")
        return value
    classes: dict[str, type[_FleetEntityPayload]] = {
        "recipe-installation": RecipeInstallationPayload,
        "installation-node": InstallationNodePayload,
        "recipe-run": RecipeRunPayload,
        "run-node": RunNodePayload,
        "job": JobPayload,
        "agent-operation": AgentOperationPayload,
    }
    model = classes.get(entity_kind)
    if model is None:
        raise ValueError("Fleet event source kind is invalid")
    value = model.model_validate(payload)
    if value.entity_kind != entity_kind or value.entity_id != entity_id:
        raise ValueError("Fleet event entity identity is inconsistent")
    if getattr(value, "node_id", None) != node_id and entity_kind in {
        "installation-node",
        "run-node",
        "agent-operation",
    }:
        raise ValueError("Fleet event node identity is inconsistent")
    return value


__all__ = [
    "AgentOperationPayload",
    "FleetEventPayload",
    "InstallationNodePayload",
    "JobPayload",
    "NodeProfilePayload",
    "NodeTelemetryPayload",
    "RecipeInstallationPayload",
    "RecipeRunPayload",
    "RunNodePayload",
    "validate_fleet_event_payload",
]
