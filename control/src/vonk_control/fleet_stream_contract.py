"""Typed JSON envelopes emitted by the Fleet Server-Sent Events stream."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import (
    ConfigDict,
    Field,
    RootModel,
    TypeAdapter,
    field_validator,
    model_validator,
)

from .fleet_event_contract import (
    AgentOperationPayload,
    InstallationNodePayload,
    JobPayload,
    NodeProfilePayload,
    RecipeInstallationPayload,
    RecipeRunPayload,
    RunNodePayload,
)
from .fleet_projection import FleetSnapshot, TelemetryPoint
from .strict_json import StrictJSONModel


class _FleetStreamModel(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class FleetSnapshotEvent(_FleetStreamModel):
    schema_version: Literal[1] = 1
    reset_reason: Annotated[str, Field(min_length=1, max_length=64)]
    snapshot: FleetSnapshot


class FleetTelemetryEvent(_FleetStreamModel):
    schema_version: Literal[1] = 1
    node_id: Annotated[str, Field(min_length=1, max_length=128)]
    sample: TelemetryPoint

    @model_validator(mode="after")
    def node_matches_sample(self) -> FleetTelemetryEvent:
        if self.sample.node_id != self.node_id:
            raise ValueError("Fleet telemetry envelope node does not match its sample")
        return self


class _FleetChangeBase(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    entity_id: Annotated[str, Field(min_length=1, max_length=256)]
    node_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Fleet change timestamp must be timezone-aware")
        return value.astimezone(UTC)


class NodeProfileChange(_FleetChangeBase):
    entity_kind: Literal["node-profile"]
    node_id: Annotated[str, Field(min_length=1, max_length=128)]
    fields: NodeProfilePayload


class RecipeInstallationChange(_FleetChangeBase):
    entity_kind: Literal["recipe-installation"]
    node_id: None = None
    fields: RecipeInstallationPayload


class InstallationNodeChange(_FleetChangeBase):
    entity_kind: Literal["installation-node"]
    node_id: Annotated[str, Field(min_length=1, max_length=128)]
    fields: InstallationNodePayload


class RecipeRunChange(_FleetChangeBase):
    entity_kind: Literal["recipe-run"]
    node_id: None = None
    fields: RecipeRunPayload


class RunNodeChange(_FleetChangeBase):
    entity_kind: Literal["run-node"]
    node_id: Annotated[str, Field(min_length=1, max_length=128)]
    fields: RunNodePayload


class JobChange(_FleetChangeBase):
    entity_kind: Literal["job"]
    node_id: None = None
    fields: JobPayload


class AgentOperationChange(_FleetChangeBase):
    entity_kind: Literal["agent-operation"]
    node_id: Annotated[str, Field(min_length=1, max_length=128)]
    fields: AgentOperationPayload


type FleetChange = Annotated[
    NodeProfileChange
    | RecipeInstallationChange
    | InstallationNodeChange
    | RecipeRunChange
    | RunNodeChange
    | JobChange
    | AgentOperationChange,
    Field(discriminator="entity_kind"),
]
FleetChangeAdapter = TypeAdapter(FleetChange)


class FleetChangeEvent(_FleetStreamModel):
    schema_version: Literal[1] = 1
    projection_refresh_required: Literal[True] = True
    change: FleetChange


class FleetStreamEvent(
    RootModel[FleetSnapshotEvent | FleetTelemetryEvent | FleetChangeEvent]
):
    """OpenAPI union for the JSON payload carried by one SSE frame."""


__all__ = [
    "FleetChange",
    "FleetChangeAdapter",
    "FleetChangeEvent",
    "FleetSnapshotEvent",
    "FleetStreamEvent",
    "FleetTelemetryEvent",
]
