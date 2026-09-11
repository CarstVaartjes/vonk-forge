"""Strict public contracts for saved Fleet profiles and their applications."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Literal, Protocol

from pydantic import ConfigDict, Field, StringConstraints, model_validator
from vonk_agent_protocol import OperationProgress

from .preparation_contract import RolloutPreparation
from .run_switch_contract import RunSwitchOperationResult
from .strict_json import StrictJSONModel

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

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
RecipeSelector = Annotated[
    str,
    StringConstraints(
        min_length=5,
        max_length=127,
        pattern=r"^[a-z0-9][a-z0-9-]{1,62}/[a-z0-9][a-z0-9-]{1,62}$",
    ),
]

# Each closed profile value set is named once here and used by the contract's
# own field annotations and by the Fleet profile helpers that build those
# fields. A shared alias is what keeps a helper signature from drifting away
# from the set the model will accept, so the two cannot disagree without a
# type error.
FleetProfileInstallationPolicy = Literal["keep-cached", "exact"]
FleetProfileOperationState = Literal[
    "queued",
    "running",
    "waiting-for-operator",
    "succeeded",
    "failed",
    "cancelled",
]
FleetProfileChildPhase = Literal[
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
FleetProfileAssignmentState = Literal[
    "not-placed", "placed", "installing", "installed", "running", "degraded"
]
FleetProfileAction = Literal[
    "stop",
    "create-placement",
    "build",
    "distribute-image",
    "install",
    "start",
    "switch",
    "keep",
]
FleetProfilePlanStepKind = Literal[
    "stop",
    "uninstall",
    "create-placement",
    "build",
    "distribute-image",
    "install",
    "start",
    "switch",
]
FleetProfileOperationKind = Literal["fleet-profile.apply"]


class _StrictModel(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class FleetProfileNode(_StrictModel):
    node_id: NodeId
    rank: int = Field(ge=0, le=31)
    role: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    endpoint_owner: bool = False


class FleetProfileScope(_StrictModel):
    """Frozen complete fleet boundary for a single execution plan.

    User profiles do not author this field.  It is captured from the enrolled
    roster when preview/load admits an operation and is retained so a running
    operation cannot silently expand or shrink with fleet membership changes.
    """

    node_ids: list[NodeId] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_scope(self) -> FleetProfileScope:
        if len(self.node_ids) != len(set(self.node_ids)):
            raise ValueError("execution scope node IDs must be unique")
        if self.node_ids != sorted(self.node_ids):
            raise ValueError("execution scope node IDs must be sorted")
        return self


class FleetProfileAssignmentInput(_StrictModel):
    """Permissive autosaved recipe choice, not an execution assignment.

    A logical model variant is selected by its canonical identity.  The
    content digest is resolved from that identity for a load and is never a
    profile-pinned execution revision.  Topology/rank validation belongs to
    preview/load, so incomplete distributed drafts can be saved.
    """

    recipe_selector: RecipeSelector
    spark_ids: list[NodeId] = Field(min_length=1, max_length=32)
    assignment_name: Alias | None = None
    model_variant: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    desired_state: Literal["installed", "running"] = "running"

    @model_validator(mode="after")
    def validate_assignment(self) -> FleetProfileAssignmentInput:
        if len(self.spark_ids) != len(set(self.spark_ids)):
            raise ValueError("profile assignment Spark IDs must be unique")
        if self.spark_ids != sorted(self.spark_ids):
            raise ValueError("profile assignment Spark IDs must be sorted")
        return self


class StoredFleetProfileAssignment(_StrictModel):
    """Resolved assignment retained in an immutable execution snapshot."""

    id: UuidId
    recipe_revision_id: UuidId
    topology_name: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    desired_state: Literal["installed", "running"]
    alias: Alias | None = None
    nodes: list[FleetProfileNode] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_execution_assignment(self) -> StoredFleetProfileAssignment:
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
            raise ValueError("installed-only profile assignments cannot declare an endpoint alias")
        return self


class FleetProfileAssignment(StoredFleetProfileAssignment):
    recipe_id: UuidId
    recipe_title: Name
    model_title: (
        Annotated[str, StringConstraints(min_length=1, max_length=200)] | None
    ) = None


class FleetProfileAssignmentView(_StrictModel):
    """Canonical read projection of one logical profile assignment."""

    selector: Alias
    display_name: Name
    recipe_selector: RecipeSelector
    recipe_id: UuidId | None = None
    spark_ids: list[NodeId] = Field(min_length=1, max_length=32)
    required_sparks: int | None = Field(default=None, ge=1, le=32)
    assigned_sparks: int = Field(ge=1, le=32)
    model: dict[str, object] = Field(default_factory=dict)
    recipe: dict[str, object] = Field(default_factory=dict)
    resources: dict[str, object] = Field(default_factory=dict)
    observed_state: str = "Not loaded"

    @model_validator(mode="after")
    def validate_spark_projection(self) -> FleetProfileAssignmentView:
        if self.spark_ids != sorted(set(self.spark_ids)):
            raise ValueError("profile assignment Spark IDs must be sorted and unique")
        if self.assigned_sparks != len(self.spark_ids):
            raise ValueError("assigned_sparks must match spark_ids")
        return self


class FleetProfileInput(_StrictModel):
    name: Name = "Default"
    description: Description = ""
    installation_policy: FleetProfileInstallationPolicy = "keep-cached"
    labels: dict[LabelName, LabelValue] = Field(default_factory=dict, max_length=16)
    favorite: bool = False
    expected_revision: int | None = Field(default=None, ge=1)
    assignments: list[FleetProfileAssignmentInput] = Field(
        default_factory=list, max_length=64
    )

    @model_validator(mode="after")
    def validate_profile(self) -> FleetProfileInput:
        identities = [
            (assignment.recipe_selector, tuple(assignment.spark_ids), assignment.assignment_name)
            for assignment in self.assignments
        ]
        if len(identities) != len(set(identities)):
            raise ValueError(
                "profile assignments must be unique by recipe revision and Spark group"
            )
        aliases = [
            assignment.assignment_name
            for assignment in self.assignments
            if assignment.assignment_name
        ]
        if len(aliases) != len(set(aliases)):
            raise ValueError("running profile assignment aliases must be unique")
        return self


class FleetProfileView(_StrictModel):
    schema_version: Literal[2] = 2
    id: UuidId
    number: int = Field(ge=1)
    revision: int = Field(ge=1)
    name: Name
    description: Description
    installation_policy: FleetProfileInstallationPolicy
    labels: dict[LabelName, LabelValue]
    favorite: bool
    assignments: list[FleetProfileAssignmentView]
    fleet: list[dict[str, object]] = Field(default_factory=list)
    status: str = "draft"
    loaded_revision: int | None = Field(default=None, ge=1)
    cache_summary: dict[str, object] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list, max_length=128)
    next_actions: list[str] = Field(default_factory=list, max_length=32)
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
    current_state: FleetProfileAssignmentState
    node_ids: list[NodeId] = Field(min_length=1, max_length=32)
    actions: list[FleetProfileAction] = Field(max_length=7)
    reasons: list[FleetProfileReason] = Field(max_length=32)

    @model_validator(mode="after")
    def validate_nodes(self) -> FleetProfileAssignmentPreview:
        if len(self.node_ids) != len(set(self.node_ids)):
            raise ValueError("assignment preview node IDs must be unique")
        return self


class FleetProfileScopePreview(_StrictModel):
    node_ids: list[NodeId] = Field(max_length=32)
    idle_node_ids: list[NodeId] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_scope(self) -> FleetProfileScopePreview:
        if len(self.node_ids) != len(set(self.node_ids)):
            raise ValueError("preview scope node IDs must be unique")
        if len(self.idle_node_ids) != len(set(self.idle_node_ids)):
            raise ValueError("preview idle node IDs must be unique")
        if not set(self.idle_node_ids) <= set(self.node_ids):
            raise ValueError("preview idle node IDs must be inside the profile scope")
        return self


class FleetProfilePlanStep(_StrictModel):
    index: int = Field(ge=0, le=1023)
    kind: FleetProfilePlanStepKind
    assignment_id: UuidId | None = None
    owner_id: UuidId | None = None
    recipe_revision_id: UuidId | None = None
    node_ids: list[NodeId] = Field(default_factory=list, max_length=32)
    label: Annotated[str, StringConstraints(min_length=1, max_length=240)]

    @model_validator(mode="after")
    def validate_nodes(self) -> FleetProfilePlanStep:
        if len(self.node_ids) != len(set(self.node_ids)):
            raise ValueError("plan step node IDs must be unique")
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

    operation: OperationProgress | None = None

    phase: FleetProfileChildPhase
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

class FleetProfileSwitchQueueItem(_StrictModel):
    """One durable Run/Switch child in the profile reconciliation queue."""

    kind: Literal["run", "stop"]
    id: UuidId


class FleetProfileSwitchChildState(_StrictModel):
    """Terminal receipt for a child already completed by the adapter."""

    operation_id: UuidId
    kind: Literal["run", "stop"]
    state: Literal["succeeded", "failed", "cancelled"]
    result: FleetProfileSwitchChildResult | None = None


class FleetProfileSwitchAdapterResult(_StrictModel):
    """Complete result of reconciling every assignment in a profile scope."""

    children: list[FleetProfileSwitchChildState] = Field(max_length=128)
    assignment_ids: list[UuidId] = Field(max_length=64)

    @model_validator(mode="after")
    def assignment_ids_are_unique(self) -> FleetProfileSwitchAdapterResult:
        if len(self.assignment_ids) != len(set(self.assignment_ids)):
            raise ValueError("profile switch assignment IDs must be unique")
        return self


class FleetProfileSwitchAdapterState(_StrictModel):
    """Persisted state used to resume a profile switch after restart."""

    schema_version: Literal[2] = 2
    child_id: UuidId
    scope_node_ids: list[NodeId] = Field(max_length=32)
    assignment_ids: list[UuidId] = Field(max_length=64)
    assignments: list[FleetProfileAssignment] = Field(max_length=64)
    queue: list[FleetProfileSwitchQueueItem] = Field(max_length=128)
    position: int = Field(default=0, ge=0, le=128)
    active_operation_id: UuidId | None = None
    active_kind: Literal["run", "stop"] | None = None
    children: list[FleetProfileSwitchChildState] = Field(default_factory=list, max_length=128)
    actor: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    request_id: UuidId
    state: FleetProfileOperationState = "queued"
    child_progress: FleetProfileChildProgress | None = None
    status_reason: Annotated[str, StringConstraints(max_length=512)] | None = None
    result: FleetProfileSwitchAdapterResult | None = None

    @model_validator(mode="after")
    def identities_are_consistent(self) -> FleetProfileSwitchAdapterState:
        if self.scope_node_ids != sorted(set(self.scope_node_ids)):
            raise ValueError("profile switch scope node IDs must be sorted and unique")
        if self.assignment_ids != [assignment.id for assignment in self.assignments]:
            raise ValueError("profile switch assignments must match assignment IDs")
        if self.active_kind is None and self.active_operation_id is not None:
            raise ValueError("profile switch active operation must have a kind")
        if self.active_kind is not None and self.active_operation_id is None:
            raise ValueError("profile switch active kind must have an operation")
        return self

class FleetProfileSwitchChildResult(_StrictModel):
    """Profile child receipt containing the public Run/Switch result tree."""

    run_switch_operation_id: UuidId
    run_switch: RunSwitchOperationResult


# The terminal child receipt is declared above its own type so a completed
# child can carry it; resolve that forward reference once both exist.
FleetProfileSwitchChildState.model_rebuild()


class FleetProfileVerificationResult(_StrictModel):
    """Small result used by profile switch adapters that verify directly."""

    verified: bool


class FleetProfileAssignmentContext(_StrictModel):
    """Persisted mapping and installation identities for one assignment."""

    mapping_id: UuidId | None = None
    mapping_generation: int | None = Field(default=None, ge=1)
    installation_id: UuidId | None = None


FleetProfileChildResult = (
    FleetProfileSwitchChildResult
    | FleetProfileSwitchAdapterResult
    | FleetProfileVerificationResult
)


class FleetProfileStepResult(_StrictModel):
    """Result receipt for one completed profile plan step."""

    operation_id: UuidId
    owner_id: UuidId | None = None
    kind: Annotated[str, StringConstraints(min_length=1, max_length=80)] | None = None
    result: FleetProfileChildResult | None = None

class FleetProfileIntendedConfiguration(_StrictModel):
    """Immutable desired configuration captured when execution is admitted."""

    profile_digest: Digest
    installation_policy: FleetProfileInstallationPolicy
    scope: FleetProfileScope
    assignments: list[FleetProfileAssignment] = Field(max_length=64)


class FleetProfileApplicationProgress(_StrictModel):
    """Typed progress tree persisted with every profile application."""

    attempt: int = Field(default=1, ge=1)
    retry_of_application_id: UuidId | None = None
    intended_profile: FleetProfileIntendedConfiguration | None = None
    operation_kind: FleetProfileOperationKind | None = None
    completed_steps: int = Field(default=0, ge=0, le=1024)
    total_steps: int = Field(default=0, ge=0, le=1024)
    current_label: Annotated[str, StringConstraints(max_length=240)] | None = None
    child_source: Literal["recipe", "switch-adapter"] | None = None
    child_progress: FleetProfileChildProgress | None = None
    step_results: dict[str, FleetProfileStepResult] = Field(default_factory=dict)
    switch_adapter: FleetProfileSwitchAdapterState | None = None
    assignments: dict[UuidId, FleetProfileAssignmentContext] = Field(default_factory=dict)

    @model_validator(mode="after")
    def progress_is_consistent(self) -> FleetProfileApplicationProgress:
        if self.total_steps < self.completed_steps:
            raise ValueError("profile progress completed steps exceed total steps")
        return self

class FleetProfileApplicationResult(_StrictModel):
    """Terminal result for one profile application."""

    changed: bool
    completed_steps: int = Field(ge=0, le=1024)


class FleetProfileChildOperation(_StrictModel):
    """Stable child operation envelope independent of the Run service module."""

    id: UuidId
    state: FleetProfileOperationState
    progress: FleetProfileChildProgress | None = None
    status_reason: Annotated[str, StringConstraints(max_length=512)] | None = None
    result: FleetProfileChildResult | None = None


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

        ...

    def get(
        self, operation_id: str, *, session: Session | None = None
    ) -> FleetProfileChildOperation:
        """Return the durable child state for inspection or resumption.

        A caller that already holds this application's row passes its session so
        the adapter joins that transaction instead of opening a second one on a
        row the caller has locked.
        """

        ...


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


class FleetProfileLoadRequest(_StrictModel):
    dry_run: bool = False
    request_key: UuidId | None = None


class FleetProfileApplicationView(_StrictModel):
    schema_version: Literal[2] = 2
    id: UuidId
    profile_id: UuidId
    profile_digest: Digest
    plan_digest: Digest
    attempt: int = Field(default=1, ge=1)
    retry_of_application_id: UuidId | None = None
    state: FleetProfileOperationState
    current_step: int = Field(ge=0, le=1024)
    total_steps: int = Field(ge=0, le=1024)
    current_operation_id: UuidId | None
    status_reason: Annotated[str, StringConstraints(max_length=512)] | None
    progress: FleetProfileApplicationProgress
    result: FleetProfileApplicationResult | None
    created_at: datetime
    updated_at: datetime


    @model_validator(mode="after")
    def application_state_is_consistent(self) -> FleetProfileApplicationView:
        if self.current_step > self.total_steps:
            raise ValueError("application step exceeds total steps")
        if self.attempt != self.progress.attempt or self.retry_of_application_id != self.progress.retry_of_application_id:
            raise ValueError("application recovery identity disagrees with persisted progress")
        if self.state == "succeeded":
            if self.result is None or self.status_reason is not None:
                raise ValueError("successful application requires a result and no failure reason")
            if self.current_step != self.total_steps or self.result.completed_steps != self.total_steps:
                raise ValueError("successful application must complete every planned step")
        if self.state in {"failed", "waiting-for-operator"} and not (self.status_reason or "").strip():
            raise ValueError("failed or waiting application requires a failure reason")
        return self


__all__ = [
    "FleetProfileApplicationProgress",
    "FleetProfileApplicationResult",
    "FleetProfileApplicationView",
    "FleetProfileAssignment",
    "FleetProfileAssignmentInput",
    "FleetProfileAssignmentPreparation",
    "FleetProfileAssignmentPreview",
    "FleetProfileAssignmentView",
    "FleetProfileChildOperation",
    "FleetProfileChildProgress",
    "FleetProfileChildResult",
    "FleetProfileInput",
    "FleetProfileIntendedConfiguration",
    "FleetProfileList",
    "FleetProfileLoadRequest",
    "FleetProfileNode",
    "FleetProfilePlanStep",
    "FleetProfilePlanSummary",
    "FleetProfilePreview",
    "FleetProfileReason",
    "FleetProfileScope",
    "FleetProfileScopePreview",
    "FleetProfileStepResult",
    "FleetProfileSwitchAdapter",
    "FleetProfileSwitchAdapterResult",
    "FleetProfileSwitchAdapterState",
    "FleetProfileSwitchChildResult",
    "FleetProfileSwitchChildState",
    "FleetProfileSwitchQueueItem",
    "FleetProfileVerificationResult",
    "FleetProfileView",
]
