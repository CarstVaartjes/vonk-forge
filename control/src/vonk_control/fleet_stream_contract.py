"""Typed JSON envelopes emitted by the Fleet Server-Sent Events stream."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import Field, RootModel, field_validator, model_validator

from .fleet_projection import FleetSnapshot, TelemetryPoint
from .strict_json import StrictJSONModel


class FleetSnapshotEvent(StrictJSONModel):
    schema_version: Literal[1] = 1
    reset_reason: Annotated[str, Field(min_length=1, max_length=64)]
    snapshot: FleetSnapshot


class FleetTelemetryEvent(StrictJSONModel):
    schema_version: Literal[1] = 1
    node_id: Annotated[str, Field(min_length=1, max_length=128)]
    sample: TelemetryPoint

    @model_validator(mode="after")
    def node_matches_sample(self) -> FleetTelemetryEvent:
        if self.sample.node_id != self.node_id:
            raise ValueError("Fleet telemetry envelope node does not match its sample")
        return self


class FleetChange(StrictJSONModel):
    entity_kind: Annotated[str, Field(min_length=1, max_length=64)]
    entity_id: Annotated[str, Field(min_length=1, max_length=256)]
    node_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    occurred_at: datetime
    fields: dict[str, object] = Field(max_length=128)

    @field_validator("occurred_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Fleet change timestamp must be timezone-aware")
        return value.astimezone(UTC)


class FleetChangeEvent(StrictJSONModel):
    schema_version: Literal[1] = 1
    projection_refresh_required: Literal[True] = True
    change: FleetChange


class FleetStreamEvent(
    RootModel[FleetSnapshotEvent | FleetTelemetryEvent | FleetChangeEvent]
):
    """OpenAPI union for the JSON payload carried by one SSE frame."""


__all__ = [
    "FleetChange",
    "FleetChangeEvent",
    "FleetSnapshotEvent",
    "FleetStreamEvent",
    "FleetTelemetryEvent",
]
