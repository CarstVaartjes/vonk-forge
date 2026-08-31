"""Strict public contracts for saved Fleet profiles and their applications."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

_UUID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_NODE_PATTERN = r"^spk_[0-9a-f]{32}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"

UuidId = Annotated[str, StringConstraints(pattern=_UUID_PATTERN)]
NodeId = Annotated[str, StringConstraints(pattern=_NODE_PATTERN)]
Digest = Annotated[str, StringConstraints(pattern=_DIGEST_PATTERN)]
Name = Annotated[
    str, StringConstraints(min_length=1, max_length=120, strip_whitespace=True)
]
Description = Annotated[str, StringConstraints(max_length=1000, strip_whitespace=True)]
LabelName = Annotated[
    str,
    StringConstraints(
        min_length=1, max_length=63, pattern=r"^[a-z0-9](?:[a-z0-9_.-]{0,61}[a-z0-9])?$"
    ),
]
LabelValue = Annotated[str, StringConstraints(min_length=1, max_length=63)]
Alias = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$",
    ),
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class FleetProfileNode(_StrictModel):
    node_id: NodeId
    rank: int = Field(ge=0, le=31)
    role: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    endpoint_owner: bool = False


class FleetProfileAssignmentInput(_StrictModel):
    recipe_revision_id: UuidId
    topology_name: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    desired_state: Literal["installed", "running"]
    alias: Alias | None = None
    nodes: list[FleetProfileNode] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_assignment(self) -> FleetProfileAssignmentInput:
        node_ids = [node.node_id for node in self.nodes]
        ranks = [node.rank for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("profile assignment node IDs must be unique")
        if sorted(ranks) != list(range(len(ranks))):
            raise ValueError("profile assignment ranks must be contiguous from zero")
        if sum(node.endpoint_owner for node in self.nodes) != 1:
            raise ValueError("profile assignment must have exactly one endpoint owner")
        if self.desired_state == "running" and self.alias is None:
            raise ValueError("running profile assignments require an endpoint alias")
        if self.desired_state == "installed" and self.alias is not None:
            raise ValueError(
                "installed-only profile assignments cannot declare an alias"
            )
        return self


class FleetProfileAssignment(FleetProfileAssignmentInput):
    id: UuidId
    recipe_id: UuidId
    recipe_title: Name
    model_title: (
        Annotated[str, StringConstraints(min_length=1, max_length=200)] | None
    ) = None


class FleetProfileInput(_StrictModel):
    name: Name
    description: Description = ""
    installation_policy: Literal["keep-cached", "exact"] = "keep-cached"
    labels: dict[LabelName, LabelValue] = Field(default_factory=dict, max_length=16)
    favorite: bool = False
    assignments: list[FleetProfileAssignmentInput] = Field(
        default_factory=list, max_length=64
    )

    @model_validator(mode="after")
    def validate_profile(self) -> FleetProfileInput:
        identities = [
            (
                assignment.recipe_revision_id,
                tuple(node.node_id for node in assignment.nodes),
            )
            for assignment in self.assignments
        ]
        if len(identities) != len(set(identities)):
            raise ValueError(
                "profile assignments must be unique by recipe revision and Spark group"
            )
        aliases = [
            assignment.alias for assignment in self.assignments if assignment.alias
        ]
        if len(aliases) != len(set(aliases)):
            raise ValueError("running profile assignment aliases must be unique")
        return self


class FleetProfileView(_StrictModel):
    schema_version: Literal[1] = 1
    id: UuidId
    name: Name
    description: Description
    installation_policy: Literal["keep-cached", "exact"]
    labels: dict[LabelName, LabelValue]
    favorite: bool
    assignments: list[FleetProfileAssignment]
    profile_digest: Digest
    created_by: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    created_at: datetime
    updated_at: datetime


class FleetProfileList(_StrictModel):
    schema_version: Literal[1] = 1
    generated_at: datetime
    profiles: list[FleetProfileView] = Field(max_length=128)


class FleetProfileReason(_StrictModel):
    code: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    detail: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    severity: Literal["info", "warning", "error"]


class FleetProfileAssignmentPreview(_StrictModel):
    assignment_id: UuidId
    recipe_revision_id: UuidId
    recipe_title: Name
    desired_state: Literal["installed", "running"]
    current_state: Literal[
        "not-placed", "placed", "installing", "installed", "running", "degraded"
    ]
    node_ids: list[NodeId] = Field(min_length=1, max_length=32)
    actions: list[
        Literal[
            "stop",
            "create-placement",
            "build",
            "distribute-image",
            "install",
            "start",
            "keep",
        ]
    ] = Field(max_length=7)
    reasons: list[FleetProfileReason] = Field(max_length=32)


class FleetProfilePlanStep(_StrictModel):
    index: int = Field(ge=0, le=1023)
    kind: Literal[
        "stop",
        "uninstall",
        "create-placement",
        "build",
        "distribute-image",
        "install",
        "start",
    ]
    assignment_id: UuidId | None = None
    owner_id: UuidId | None = None
    recipe_revision_id: UuidId | None = None
    node_ids: list[NodeId] = Field(default_factory=list, max_length=32)
    label: Annotated[str, StringConstraints(min_length=1, max_length=240)]


class FleetProfilePlanSummary(_StrictModel):
    already_correct: int = Field(ge=0, le=64)
    placements: int = Field(ge=0, le=64)
    builds: int = Field(ge=0, le=64)
    distributions: int = Field(ge=0, le=64)
    installs: int = Field(ge=0, le=64)
    starts: int = Field(ge=0, le=64)
    stops: int = Field(ge=0, le=512)
    uninstalls: int = Field(ge=0, le=512)
    blockers: int = Field(ge=0, le=2048)


class FleetProfilePreview(_StrictModel):
    schema_version: Literal[1] = 1
    profile_id: UuidId
    profile_name: Name
    profile_digest: Digest
    generated_at: datetime
    allowed: bool
    summary: FleetProfilePlanSummary
    assignments: list[FleetProfileAssignmentPreview] = Field(max_length=64)
    steps: list[FleetProfilePlanStep] = Field(max_length=1024)
    reasons: list[FleetProfileReason] = Field(max_length=128)
    plan_digest: Digest


class FleetProfileApplyRequest(_StrictModel):
    plan_digest: Digest
    request_key: UuidId


class FleetProfilePreviewRequest(_StrictModel):
    """Explicit empty body keeps CSRF-protected preview calls typed."""


class FleetProfileApplicationView(_StrictModel):
    schema_version: Literal[1] = 1
    id: UuidId
    profile_id: UuidId
    profile_digest: Digest
    plan_digest: Digest
    state: Literal[
        "queued", "running", "waiting-for-operator", "succeeded", "failed", "cancelled"
    ]
    current_step: int = Field(ge=0, le=1024)
    total_steps: int = Field(ge=0, le=1024)
    current_operation_id: UuidId | None
    status_reason: Annotated[str, StringConstraints(max_length=512)] | None
    progress: dict[str, object]
    result: dict[str, object] | None
    created_at: datetime
    updated_at: datetime


__all__ = [
    "FleetProfileApplicationView",
    "FleetProfileApplyRequest",
    "FleetProfileAssignment",
    "FleetProfileAssignmentInput",
    "FleetProfileAssignmentPreview",
    "FleetProfileInput",
    "FleetProfileList",
    "FleetProfileNode",
    "FleetProfilePlanStep",
    "FleetProfilePlanSummary",
    "FleetProfilePreview",
    "FleetProfilePreviewRequest",
    "FleetProfileReason",
    "FleetProfileView",
]
