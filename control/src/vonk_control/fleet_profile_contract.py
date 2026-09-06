"""Strict public contracts for saved Fleet profiles and their applications."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .preparation_contract import RolloutPreparation

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


class FleetProfileScope(_StrictModel):
    """The complete set of Sparks reconciled by a profile.

    Scope is deliberately independent from assignments.  A member with no
    assignment is an intentional idle outcome when the profile is applied.
    """

    node_ids: list[NodeId] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_scope(self) -> FleetProfileScope:
        if len(self.node_ids) != len(set(self.node_ids)):
            raise ValueError("profile scope node IDs must be unique")
        if self.node_ids != sorted(self.node_ids):
            raise ValueError("profile scope node IDs must be sorted")
        return self


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
        ranked_node_ids = [
            node.node_id for node in sorted(self.nodes, key=lambda node: node.rank)
        ]
        if ranked_node_ids != sorted(ranked_node_ids):
            raise ValueError(
                "profile assignment rank order must match deterministic Spark identity order"
            )
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
    scope: FleetProfileScope
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
        scope = set(self.scope.node_ids)
        assigned = {
            node.node_id for assignment in self.assignments for node in assignment.nodes
        }
        if not assigned <= scope:
            raise ValueError("profile assignment nodes must be inside profile scope")
        return self


class FleetProfileView(_StrictModel):
    schema_version: Literal[2] = 2
    id: UuidId
    name: Name
    description: Description
    installation_policy: Literal["keep-cached", "exact"]
    labels: dict[LabelName, LabelValue]
    favorite: bool
    scope: FleetProfileScope
    assignments: list[FleetProfileAssignment]
    profile_digest: Digest
    created_by: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    created_at: datetime
    updated_at: datetime


class FleetProfileList(_StrictModel):
    schema_version: Literal[2] = 2
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
            "switch",
            "keep",
        ]
    ] = Field(max_length=7)
    reasons: list[FleetProfileReason] = Field(max_length=32)

    @model_validator(mode="after")
    def validate_nodes(self) -> FleetProfileAssignmentPreview:
        if self.node_ids != sorted(self.node_ids) or len(self.node_ids) != len(
            set(self.node_ids)
        ):
            raise ValueError("assignment preview node IDs must be sorted and unique")
        return self


class FleetProfileScopePreview(_StrictModel):
    node_ids: list[NodeId] = Field(max_length=32)
    idle_node_ids: list[NodeId] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_scope(self) -> FleetProfileScopePreview:
        if self.node_ids != sorted(self.node_ids) or len(self.node_ids) != len(
            set(self.node_ids)
        ):
            raise ValueError("preview scope node IDs must be sorted and unique")
        if self.idle_node_ids != sorted(self.idle_node_ids) or len(
            self.idle_node_ids
        ) != len(set(self.idle_node_ids)):
            raise ValueError("preview idle node IDs must be sorted and unique")
        if not set(self.idle_node_ids) <= set(self.node_ids):
            raise ValueError("preview idle node IDs must be inside the profile scope")
        return self


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
        "switch",
    ]
    assignment_id: UuidId | None = None
    owner_id: UuidId | None = None
    recipe_revision_id: UuidId | None = None
    node_ids: list[NodeId] = Field(default_factory=list, max_length=32)
    label: Annotated[str, StringConstraints(min_length=1, max_length=240)]

    @model_validator(mode="after")
    def validate_nodes(self) -> FleetProfilePlanStep:
        if self.node_ids != sorted(self.node_ids) or len(self.node_ids) != len(
            set(self.node_ids)
        ):
            raise ValueError("plan step node IDs must be sorted and unique")
        return self


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


class FleetProfileAssignmentPreparation(_StrictModel):
    assignment_id: UuidId
    preparation: RolloutPreparation


class FleetProfileChildProgress(_StrictModel):
    """Typed progress emitted by the profile-owned Run switch adapter."""

    phase: Literal[
        "model-download",
        "container-download",
        "container-build",
        "target-copy",
        "runtime-install",
        "start",
        "final-verify",
        "transfer",
        "verify",
        "prepare",
        "cleanup",
        "stop",
        "final_verify",
    ]
    node_ids: list[NodeId] = Field(default_factory=list, max_length=32)
    bytes: int | None = Field(default=None, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def byte_progress_is_consistent(self) -> FleetProfileChildProgress:
        if self.node_ids != sorted(self.node_ids) or len(self.node_ids) != len(
            set(self.node_ids)
        ):
            raise ValueError("child progress node IDs must be sorted and unique")
        if (
            self.bytes is not None
            and self.total_bytes is not None
            and self.bytes > self.total_bytes
        ):
            raise ValueError("child progress bytes cannot exceed total bytes")
        return self


class FleetProfileChildOperation(_StrictModel):
    """Stable child operation envelope independent of the Run service module."""

    id: UuidId
    state: Literal[
        "queued",
        "running",
        "waiting-for-operator",
        "succeeded",
        "failed",
        "cancelled",
    ]
    progress: FleetProfileChildProgress | None = None
    status_reason: Annotated[str, StringConstraints(max_length=512)] | None = None
    result: dict[str, object] | None = None


class FleetProfileSwitchAdapter(Protocol):
    """Profile boundary for the integrated automatic Run switch service."""

    def start(
        self,
        *,
        application_id: str,
        assignments: tuple[FleetProfileAssignment, ...],
        scope_node_ids: tuple[str, ...],
        actor: str,
        request_id: str,
    ) -> FleetProfileChildOperation:
        """Reconcile the complete desired assignment set as one child operation.

        ``assignments`` is ordered by stable assignment identity and
        ``scope_node_ids`` is the complete sorted profile boundary.  The
        implementation must plan conflicts once and preserve healthy desired
        assignments while preparing or stopping other members.
        """

    def get(self, operation_id: str) -> FleetProfileChildOperation:
        """Return the durable child state for inspection or resumption."""


class FleetProfilePreview(_StrictModel):
    schema_version: Literal[2] = 2
    profile_id: UuidId
    profile_name: Name
    profile_digest: Digest
    generated_at: datetime
    allowed: bool
    scope: FleetProfileScopePreview
    summary: FleetProfilePlanSummary
    assignments: list[FleetProfileAssignmentPreview] = Field(max_length=64)
    preparations: list[FleetProfileAssignmentPreparation] = Field(
        default_factory=list, max_length=64
    )
    steps: list[FleetProfilePlanStep] = Field(max_length=1024)
    reasons: list[FleetProfileReason] = Field(max_length=128)
    plan_digest: Digest


class FleetProfileApplyRequest(_StrictModel):
    plan_digest: Digest
    request_key: UuidId


class FleetProfilePreviewRequest(_StrictModel):
    """Explicit empty body keeps CSRF-protected preview calls typed."""


class FleetProfileApplicationView(_StrictModel):
    schema_version: Literal[2] = 2
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


class FleetProfileStatusView(_StrictModel):
    schema_version: Literal[2] = 2
    profile_id: UuidId
    profile_digest: Digest
    state: Literal[
        "draft",
        "needs-preparation",
        "ready",
        "matched",
        "switching",
        "partially-applied",
        "blocked",
        "drifted",
    ]
    matched: bool
    drifted: bool
    scope: FleetProfileScopePreview
    reasons: list[FleetProfileReason] = Field(max_length=128)
    generated_at: datetime


class FleetProfileDuplicateInput(_StrictModel):
    name: Name
    description: Description | None = None


class FleetProfilePrepareRequest(_StrictModel):
    plan_digest: Digest
    request_key: UuidId


class FleetProfilePreparePreviewRequest(_StrictModel):
    """Explicit empty body for the digest-bound preparation preview."""


class FleetProfileCaptureInput(_StrictModel):
    name: Name
    description: Description = "Captured current Fleet setup"
    installation_policy: Literal["keep-cached", "exact"] = "keep-cached"
    labels: dict[LabelName, LabelValue] = Field(default_factory=dict, max_length=16)
    favorite: bool = False


__all__ = [
    "FleetProfileApplicationView",
    "FleetProfileApplyRequest",
    "FleetProfileAssignment",
    "FleetProfileAssignmentInput",
    "FleetProfileAssignmentPreparation",
    "FleetProfileAssignmentPreview",
    "FleetProfileCaptureInput",
    "FleetProfileChildOperation",
    "FleetProfileChildProgress",
    "FleetProfileDuplicateInput",
    "FleetProfileInput",
    "FleetProfileList",
    "FleetProfileNode",
    "FleetProfilePlanStep",
    "FleetProfilePlanSummary",
    "FleetProfilePreparePreviewRequest",
    "FleetProfilePrepareRequest",
    "FleetProfilePreview",
    "FleetProfilePreviewRequest",
    "FleetProfileReason",
    "FleetProfileScope",
    "FleetProfileScopePreview",
    "FleetProfileStatusView",
    "FleetProfileSwitchAdapter",
    "FleetProfileView",
]
