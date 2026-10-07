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
from vonk_agent_protocol.reason_codes import ProjectionCode

from cluster_profiles.control_limits import MAX_CONTROL_DOCUMENT_BYTES

from .fleet_event_contract import (
    AgentOperationPayload,
    InstallationNodePayload,
    JobPayload,
    NodeProfilePayload,
    RecipeInstallationPayload,
    RecipeRunPayload,
    RunNodePayload,
)
from .fleet_projection import TelemetryPoint
from .strict_json import StrictJSONModel


class _FleetStreamModel(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class FleetFrameIssue(_FleetStreamModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {
                            "reason_code": {
                                "enum": [
                                    ProjectionCode.FLEET_FRAME_ENCODING_UNAVAILABLE,
                                    ProjectionCode.FLEET_STORED_EVENT_PAYLOAD_UNAVAILABLE,
                                ]
                            }
                        },
                        "required": ["reason_code"],
                    },
                    "then": {
                        "properties": {"observed_bytes_at_least": {"type": "null"}}
                    },
                },
                {
                    "if": {
                        "properties": {
                            "reason_code": {
                                "const": ProjectionCode.FLEET_FRAME_BUDGET_EXCEEDED
                            }
                        },
                        "required": ["reason_code"],
                    },
                    "then": {
                        "properties": {
                            "observed_bytes_at_least": {
                                "type": "integer",
                                "minimum": MAX_CONTROL_DOCUMENT_BYTES + 1,
                            }
                        }
                    },
                },
            ]
        }
    )
    reason_code: Literal[
        ProjectionCode.FLEET_FRAME_BUDGET_EXCEEDED,
        ProjectionCode.FLEET_FRAME_ENCODING_UNAVAILABLE,
        ProjectionCode.FLEET_STORED_EVENT_PAYLOAD_UNAVAILABLE,
    ]
    observed_bytes_at_least: (
        Annotated[int, Field(ge=MAX_CONTROL_DOCUMENT_BYTES + 1)] | None
    )
    budget_bytes: Annotated[
        int, Field(ge=MAX_CONTROL_DOCUMENT_BYTES, le=MAX_CONTROL_DOCUMENT_BYTES)
    ]

    @model_validator(mode="after")
    def truthful_measurement(self) -> FleetFrameIssue:
        if self.reason_code in {
            ProjectionCode.FLEET_FRAME_ENCODING_UNAVAILABLE,
            ProjectionCode.FLEET_STORED_EVENT_PAYLOAD_UNAVAILABLE,
        }:
            if self.observed_bytes_at_least is not None:
                raise ValueError("unavailable frame has no byte measurement")
        elif (
            self.observed_bytes_at_least is None
            or self.observed_bytes_at_least <= self.budget_bytes
        ):
            raise ValueError(
                "oversized frame requires a measured lower bound above budget"
            )
        return self


FleetCursor = Annotated[int, Field(ge=0, le=9_223_372_036_854_775_807)]


class FleetRefreshEvent(_FleetStreamModel):
    reset_reason: Literal[
        "initial",
        "cursor-ahead",
        "retention-gap",
        "missing-telemetry-sample",
        "frame-unavailable",
    ]
    event_cursor: FleetCursor
    issue: FleetFrameIssue | None = None


class FleetTelemetryEvent(_FleetStreamModel):
    event_cursor: FleetCursor
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
    # Required on the abstract base: every concrete change states whether it is
    # node-scoped or explicitly node-less. A base default that a subclass
    # removes is a legal Pydantic override but not something a dataclass-based
    # checker accepts, and inlining a default here would change the changes'
    # schemas.
    node_id: Annotated[str, Field(min_length=1, max_length=128)] | None
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
    event_cursor: FleetCursor
    projection_refresh_required: Literal[True] = True
    change: FleetChange


class FleetStreamEvent(
    RootModel[FleetRefreshEvent | FleetTelemetryEvent | FleetChangeEvent]
):
    """OpenAPI union for the JSON payload carried by one SSE frame."""


FLEET_SSE_EVENTS = {
    "fleet-refresh": {"$ref": "#/components/schemas/FleetRefreshEvent"},
    "node-telemetry": {"$ref": "#/components/schemas/FleetTelemetryEvent"},
    "node-profile": {"$ref": "#/components/schemas/FleetChangeEvent"},
    "recipe-state": {"$ref": "#/components/schemas/FleetChangeEvent"},
    "operation-state": {"$ref": "#/components/schemas/FleetChangeEvent"},
}


__all__ = [
    "FLEET_SSE_EVENTS",
    "FleetChange",
    "FleetChangeAdapter",
    "FleetChangeEvent",
    "FleetFrameIssue",
    "FleetRefreshEvent",
    "FleetStreamEvent",
    "FleetTelemetryEvent",
]
