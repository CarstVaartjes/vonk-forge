"""Strict public contracts for saved Fleet profiles and their applications."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Literal, Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import ConfigDict, Field, StringConstraints, model_validator
from vonk_agent_protocol import OperationProgress
from vonk_agent_protocol.inventory import MemoryPool

from .endpoint_contract import EndpointResponse
from .preparation_contract import (
    CompatibilityIdentity,
    ModelArtifactIdentity,
    PreparationReason,
    RolloutPreparation,
    RuntimeImageIdentity,
)
from .run_switch_contract import (
    ConditionalPostStopMemoryCheck,
    EffectiveSettingsSelection,
    MemoryKind,
    PortNumber,
    ResourceDemandEvidence,
    RunSwitchAssessment,
    RunSwitchOperationResult,
    RunSwitchReason,
    StopImpact,
)
from .strict_json import StrictJSONModel

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def profile_switch_child_request_key(
    application_id: str, position: int, kind: str, owner_id: str
) -> str:
    """One child identity for dispatch and recovery before its checkpoint exists."""
    return str(
        uuid5(
            NAMESPACE_URL,
            f"vonk-forge:profile-run-switch:{application_id}:{position}:{kind}:{owner_id}",
        )
    )


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
    "uninstall",
    "final_verify",
]
FleetProfileAssignmentState = Literal[
    "not-placed", "placed", "installing", "installed", "running", "degraded"
]
FleetProfileAction = Literal["switch", "keep"]
FleetProfilePlanStepKind = Literal["switch"]
FleetProfileOperationKind = Literal["fleet-profile.apply"]
FleetProfileEndpointState = Literal[
    "installed-only",
    "not-published-yet",
    "published",
    "expired",
    "withdrawn",
    "unavailable",
]


class _StrictModel(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


@dataclass(frozen=True)
class FleetProfileEndpointAssignmentIntent:
    """Loaded application assignment and exact current run candidate."""

    assignment_id: str
    recipe_title: str
    desired_state: Literal["installed", "running"]
    alias: str | None
    state: FleetProfileEndpointState
    expected_run_id: str | None = None


@dataclass(frozen=True)
class FleetProfileEndpointIntent:
    """Loaded profile intent captured from the durable application record."""

    number: int
    profile_id: str | None
    application_id: str | None
    application_state: FleetProfileOperationState | None
    assignments: tuple[FleetProfileEndpointAssignmentIntent, ...]


class FleetProfileEndpointAssignmentView(_StrictModel):
    assignment_id: UuidId
    recipe_title: Name
    desired_state: Literal["installed", "running"]
    alias: Alias | None = None
    state: FleetProfileEndpointState
    endpoint: EndpointResponse | None = None

    @model_validator(mode="after")
    def endpoint_matches_assignment(self) -> FleetProfileEndpointAssignmentView:
        if self.state == "published":
            if self.alias is None or self.endpoint is None:
                raise ValueError("published profile endpoint is incomplete")
            if self.endpoint.alias != self.alias:
                raise ValueError("published profile endpoint alias is inconsistent")
        elif self.endpoint is not None:
            raise ValueError("unpublished profile endpoint contains route data")
        if self.state == "installed-only" and self.desired_state != "installed":
            raise ValueError("installed-only endpoint has a running assignment")
        return self


class FleetProfileEndpointsView(_StrictModel):
    number: int = Field(ge=1)
    profile_id: UuidId | None = None
    application_id: UuidId | None = None
    application_state: FleetProfileOperationState | None = None
    observed_at: datetime
    assignments: list[FleetProfileEndpointAssignmentView] = Field(max_length=64)

    @model_validator(mode="after")
    def application_identity_is_consistent(self) -> FleetProfileEndpointsView:
        if (self.application_id is None) != (self.application_state is None):
            raise ValueError("profile endpoint application identity is incomplete")
        if self.application_id is not None and self.profile_id is None:
            raise ValueError("profile endpoint application has no profile identity")
        return self


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
    model_variant: (
        Annotated[str, StringConstraints(min_length=1, max_length=200)] | None
    ) = None
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
            raise ValueError(
                "installed-only profile assignments cannot declare an endpoint alias"
            )
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


class FleetProfileDefinition(_StrictModel):
    """Saved authoring intent, independent of execution and cache projections."""

    name: Name = "Default"
    description: Description = ""
    installation_policy: FleetProfileInstallationPolicy = "keep-cached"
    labels: dict[LabelName, LabelValue] = Field(default_factory=dict, max_length=16)
    favorite: bool = False
    assignments: list[FleetProfileAssignmentInput] = Field(
        default_factory=list, max_length=64
    )

    @model_validator(mode="after")
    def validate_profile(self) -> FleetProfileDefinition:
        identities = [
            (
                assignment.recipe_selector,
                tuple(assignment.spark_ids),
                assignment.assignment_name,
            )
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


class FleetProfileInput(FleetProfileDefinition):
    # Zero means create only: an absent profile read cannot authorize replacing
    # somebody else's intervening first save.
    expected_revision: int = Field(default=0, ge=0)


class FleetProfileDefinitionView(_StrictModel):
    schema_version: Literal[2] = 2
    id: UuidId | None
    number: int = Field(ge=1)
    revision: int = Field(ge=0)
    definition: FleetProfileDefinition

    @model_validator(mode="after")
    def validate_identity(self) -> FleetProfileDefinitionView:
        if (self.id is None) != (self.revision == 0):
            raise ValueError(
                "only an uncreated profile has revision zero and no identity"
            )
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
    definition: FleetProfileDefinition
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


class FleetProfileAssignmentAssessment(_StrictModel):
    assignment_id: UuidId
    assessment: RunSwitchAssessment


class FleetProfileResourceRequirement(_StrictModel):
    """Stable demand and eligibility, separate from observed free capacity."""

    node_id: NodeId
    allowed: bool
    ports_required: list[PortNumber]
    disk_required_bytes: int | None = Field(ge=0)
    memory_required_bytes: int | None = Field(ge=0)
    memory_kind: MemoryKind | None
    memory_pool: MemoryPool | None
    memory_floor_bytes: int | None = Field(ge=0)
    resource_demand: ResourceDemandEvidence | None


class FleetProfileAdmissionDecision(_StrictModel):
    assignment_id: UuidId
    alias: Alias | None
    allowed: bool
    blockers: list[RunSwitchReason] = Field(max_length=128)
    requirements: list[FleetProfileResourceRequirement] = Field(max_length=32)
    effective_settings: EffectiveSettingsSelection | None
    stops: list[StopImpact] = Field(max_length=128)
    stop_before_prepare: bool
    stop_before_transfer: bool
    post_stop_memory_check: ConditionalPostStopMemoryCheck | None = None

    @classmethod
    def from_assessment(
        cls, value: FleetProfileAssignmentAssessment
    ) -> FleetProfileAdmissionDecision:
        assessment = value.assessment
        fit = assessment.fit_after_stop or assessment.fit_current
        return cls(
            assignment_id=value.assignment_id,
            alias=assessment.alias,
            allowed=assessment.allowed,
            blockers=sorted(
                assessment.blockers,
                key=lambda item: (item.code, tuple(item.node_ids), item.detail),
            ),
            requirements=[
                FleetProfileResourceRequirement(
                    **{
                        name: getattr(node, name)
                        for name in FleetProfileResourceRequirement.model_fields
                    }
                )
                for node in sorted(fit.nodes, key=lambda node: node.node_id)
            ],
            effective_settings=assessment.effective_settings,
            stops=sorted(assessment.stops, key=lambda stop: stop.run_id),
            stop_before_prepare=assessment.stop_before_prepare,
            stop_before_transfer=assessment.stop_before_transfer,
            post_stop_memory_check=assessment.post_stop_memory_check,
        )


class FleetProfileCompatibilityDecision(_StrictModel):
    kind: Literal["engine-generation", "jit", "tuning"]
    stage: Literal["controller-prepare", "target-prepare"]
    compatibility: CompatibilityIdentity
    node_ids: list[NodeId] = Field(min_length=1, max_length=64)
    artifact_sha256: Digest | None
    ready: bool


class FleetProfilePreparationDecision(_StrictModel):
    """Exact assets and reuse decisions; byte counters are observations only."""

    assignment_id: UuidId
    model: ModelArtifactIdentity
    runtime_image: RuntimeImageIdentity
    model_complete: bool
    model_controller_ready: bool
    image_controller_ready: bool
    model_reuse_node_ids: list[NodeId] = Field(max_length=64)
    image_reuse_node_ids: list[NodeId] = Field(max_length=64)
    exceptions: list[FleetProfileCompatibilityDecision] = Field(max_length=64)
    blockers: list[PreparationReason] = Field(max_length=128)

    @classmethod
    def from_preparation(
        cls, value: FleetProfileAssignmentPreparation
    ) -> FleetProfilePreparationDecision:
        preparation = value.preparation
        return cls(
            assignment_id=value.assignment_id,
            model=ModelArtifactIdentity.model_validate(
                {
                    name: getattr(preparation.model, name)
                    for name in ModelArtifactIdentity.model_fields
                }
            ),
            runtime_image=RuntimeImageIdentity.model_validate(
                {
                    name: getattr(preparation.runtime_image, name)
                    for name in RuntimeImageIdentity.model_fields
                }
            ),
            model_complete=preparation.model.completeness == "complete",
            model_controller_ready=preparation.model.controller.state == "ready",
            image_controller_ready=preparation.runtime_image.controller.state
            == "ready",
            model_reuse_node_ids=sorted(
                target.node_id
                for target in preparation.model.targets
                if target.state == "ready"
            ),
            image_reuse_node_ids=sorted(
                target.node_id
                for target in preparation.runtime_image.targets
                if target.state == "ready"
            ),
            exceptions=[
                FleetProfileCompatibilityDecision(
                    kind=item.kind,
                    stage=item.stage,
                    compatibility=item.compatibility,
                    node_ids=item.node_ids,
                    artifact_sha256=item.artifact_sha256,
                    ready=item.state == "ready",
                )
                for item in sorted(
                    preparation.exceptions,
                    key=lambda item: (
                        item.kind,
                        item.stage,
                        item.compatibility_key_sha256,
                    ),
                )
            ],
            blockers=sorted(
                (
                    reason
                    for reason in preparation.reasons
                    if reason.severity == "blocker"
                ),
                key=lambda reason: (reason.code, tuple(reason.node_ids), reason.detail),
            ),
        )


class FleetProfileRunEffect(_StrictModel):
    run_id: UuidId
    installation_id: UuidId
    alias: Alias
    node_ids: list[NodeId] = Field(min_length=1, max_length=32)
    action: Literal["keep", "stop"]


class FleetProfileInstallationEffect(_StrictModel):
    installation_id: UuidId
    node_ids: list[NodeId] = Field(min_length=1, max_length=32)
    action: Literal["keep", "remove"]


class FleetProfilePendingEffect(_StrictModel):
    kind: Literal["job", "profile-application"]
    id: UuidId
    node_ids: list[NodeId] = Field(min_length=1, max_length=32)


class FleetProfileEffects(_StrictModel):
    """Identified live effects, including complete distributed membership."""

    runs: list[FleetProfileRunEffect]
    installations: list[FleetProfileInstallationEffect]
    superseded: list[FleetProfilePendingEffect]


class FleetProfileChildProgress(_StrictModel):
    """Typed progress emitted by the profile-owned Run switch adapter."""

    operation: OperationProgress | None = None
    startup_budget_seconds: int | None = Field(default=None, ge=1)
    start_deadline: datetime | None = None

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


FleetProfileSwitchChildKind = Literal["install", "run", "stop", "cleanup"]


class FleetProfileSwitchQueueItem(_StrictModel):
    """One durable Run/Switch child in the profile reconciliation queue."""

    kind: FleetProfileSwitchChildKind
    id: UuidId


class FleetProfileSwitchChildState(_StrictModel):
    """Terminal receipt for a child already completed by the adapter."""

    operation_id: UuidId
    kind: FleetProfileSwitchChildKind
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
    active_kind: FleetProfileSwitchChildKind | None = None
    children: list[FleetProfileSwitchChildState] = Field(
        default_factory=list, max_length=128
    )
    actor: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    request_id: UuidId
    state: FleetProfileOperationState = "queued"
    child_progress: FleetProfileChildProgress | None = None
    status_reason: Annotated[str, StringConstraints(max_length=512)] | None = None
    observation_due_at: datetime | None = None
    observation_deadline_at: datetime | None = None
    pending_operation_ids: list[UuidId] = Field(default_factory=list, max_length=128)
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


FleetProfileChildResult = (
    FleetProfileSwitchChildResult
    | FleetProfileSwitchAdapterResult
    | FleetProfileVerificationResult
)


class FleetProfileStepResult(_StrictModel):
    """Result receipt for one completed profile plan step."""

    operation_id: UuidId
    kind: Literal["switch"]
    result: FleetProfileChildResult | None = None


class FleetProfileIntendedConfiguration(_StrictModel):
    """Immutable desired configuration captured when execution is admitted."""

    profile_digest: Digest
    reviewed_plan_digest: Digest
    reviewed_application_id: UuidId
    installation_policy: FleetProfileInstallationPolicy
    scope: FleetProfileScope
    assignments: list[FleetProfileAssignment] = Field(max_length=64)


class FleetProfileApplicationCancellationIntent(_StrictModel):
    """Durable identity and authority for an explicit or superseding cancel."""

    request_key: UuidId
    actor: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    requested_at: datetime
    state: Literal["cancelling", "cancelled"] = "cancelling"
    cause: Literal["operator", "superseded"]
    successor_application_id: UuidId | None = None
    workload_intent_ordinal: int | None = Field(default=None, ge=1)
    pending_operation_ids: list[UuidId] = Field(default_factory=list, max_length=128)
    observation_due_at: datetime | None = None
    observation_deadline_at: datetime | None = None


class FleetProfileApplicationEffect(_StrictModel):
    """One exact profile or child effect in the cancellation receipt."""

    effect_id: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    kind: Literal[
        "profile-step", "run", "install", "stop", "cleanup", "agent-operation"
    ]
    label: Annotated[str, StringConstraints(min_length=1, max_length=240)]
    operation_id: UuidId | None = None
    outcome: Literal["succeeded", "failed", "cancelled", "pending", "not-issued"]


class FleetProfileApplicationCancellationView(_StrictModel):
    """Live projection of completed, pending, and unissued cancelled effects."""

    request_key: UuidId
    actor: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    requested_at: datetime
    state: Literal["cancelling", "cancelled"]
    cause: Literal["operator", "superseded"]
    completed_effects: list[FleetProfileApplicationEffect] = Field(max_length=1024)
    pending_effects: list[FleetProfileApplicationEffect] = Field(max_length=1024)
    cancelled_effects: list[FleetProfileApplicationEffect] = Field(max_length=1024)
    owner: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    dependency: UuidId | None = None
    deadline_at: datetime | None = None


class FleetProfileApplicationProgress(_StrictModel):
    """Typed progress tree persisted with every profile application."""

    attempt: int = Field(default=1, ge=1)
    retry_of_application_id: UuidId | None = None
    intended_profile: FleetProfileIntendedConfiguration | None = None
    workload_intent_ordinal: int | None = Field(default=None, ge=1)
    operation_kind: FleetProfileOperationKind | None = None
    completed_steps: int = Field(default=0, ge=0, le=1024)
    total_steps: int = Field(default=0, ge=0, le=1024)
    current_label: Annotated[str, StringConstraints(max_length=240)] | None = None
    child_source: Literal["switch-adapter"] | None = None
    child_progress: FleetProfileChildProgress | None = None
    step_results: dict[str, FleetProfileStepResult] = Field(default_factory=dict)
    switch_adapter: FleetProfileSwitchAdapterState | None = None
    cancellation: FleetProfileApplicationCancellationIntent | None = None

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

    def validate_resources_in_session(
        self,
        session: Session,
        assignments: tuple[FleetProfileAssignment, ...],
        reviewed: FleetProfilePreview,
    ) -> None:
        """Recheck resource eligibility inside the admission writer fence."""

        ...

    def request_superseded_workload_cancellation_in_session(
        self,
        session: Session,
        targets: tuple[str, ...],
        ordinal: int,
        now: datetime,
    ) -> None:
        """Cancel older exact agent orders in the profile admission transaction."""

        ...

    def recoverable_cache_loss(self, application_id: str, *, session: Session) -> bool:
        """Whether the current exact child failed only because managed bytes vanished."""

        ...

    def request_cancellation(
        self,
        application_id: str,
        *,
        request_key: str,
        actor: str,
    ) -> None:
        """Request cancellation from the existing Run/Switch child owner."""

        ...

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
        """Observe the durable child without ticking or dispatching work."""

        ...

    def advance(
        self, operation_id: str, *, session: Session | None = None
    ) -> FleetProfileChildOperation:
        """Advance the durable child from the worker's execution path.

        A caller that already holds this application's row passes its session so
        the adapter joins that transaction instead of opening a second one on a
        row the caller has locked.
        """

        ...


class FleetProfileReviewedDecision(_StrictModel):
    """The reviewable semantic decision that owns the reconciliation digest."""

    schema_version: Literal[2] = 2
    profile_id: UuidId
    profile_name: Name
    profile_digest: Digest
    profile_revision: int | None = Field(ge=1)
    profile_definition: FleetProfileDefinition | None
    allowed: bool
    scope: FleetProfileScopePreview
    summary: FleetProfilePlanSummary
    assignments: list[FleetProfileAssignmentPreview] = Field(max_length=64)
    resolved_assignments: list[FleetProfileAssignment] = Field(max_length=64)
    admission_decisions: list[FleetProfileAdmissionDecision] = Field(max_length=64)
    preparation_decisions: list[FleetProfilePreparationDecision] = Field(max_length=64)
    effects: FleetProfileEffects
    steps: list[FleetProfilePlanStep] = Field(max_length=1024)
    reasons: list[FleetProfileReason] = Field(max_length=128)


class FleetProfilePreview(FleetProfileReviewedDecision):
    generated_at: datetime
    assessments: list[FleetProfileAssignmentAssessment] = Field(max_length=64)
    preparations: list[FleetProfileAssignmentPreparation] = Field(
        default_factory=list, max_length=64
    )
    plan_digest: Digest

    def reviewed_decision(self) -> FleetProfileReviewedDecision:
        return FleetProfileReviewedDecision.model_validate(
            {
                name: getattr(self, name)
                for name in FleetProfileReviewedDecision.model_fields
            }
        )


class FleetProfileLoadRequest(_StrictModel):
    plan_digest: Digest
    request_key: UuidId


class FleetProfileApplicationCancelRequest(_StrictModel):
    profile_number: int = Field(ge=1)
    request_key: UuidId


class FleetProfileApplicationView(_StrictModel):
    schema_version: Literal[2] = 2
    id: UuidId
    request_key: UuidId
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
    cancellation: FleetProfileApplicationCancellationView | None = None
    result: FleetProfileApplicationResult | None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def application_state_is_consistent(self) -> FleetProfileApplicationView:
        if self.current_step > self.total_steps:
            raise ValueError("application step exceeds total steps")
        if (
            self.attempt != self.progress.attempt
            or self.retry_of_application_id != self.progress.retry_of_application_id
        ):
            raise ValueError(
                "application recovery identity disagrees with persisted progress"
            )
        if self.state == "succeeded":
            if self.result is None or self.status_reason is not None:
                raise ValueError(
                    "successful application requires a result and no failure reason"
                )
            if (
                self.current_step != self.total_steps
                or self.result.completed_steps != self.total_steps
            ):
                raise ValueError(
                    "successful application must complete every planned step"
                )
        if (
            self.state in {"failed", "waiting-for-operator"}
            and not (self.status_reason or "").strip()
        ):
            raise ValueError("failed or waiting application requires a failure reason")
        return self


__all__ = [
    "FleetProfileApplicationCancelRequest",
    "FleetProfileApplicationCancellationIntent",
    "FleetProfileApplicationCancellationView",
    "FleetProfileApplicationEffect",
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
    "FleetProfileCompatibilityDecision",
    "FleetProfileEffects",
    "FleetProfileInput",
    "FleetProfileInstallationEffect",
    "FleetProfileIntendedConfiguration",
    "FleetProfileList",
    "FleetProfileLoadRequest",
    "FleetProfileNode",
    "FleetProfilePendingEffect",
    "FleetProfilePlanStep",
    "FleetProfilePlanSummary",
    "FleetProfilePreparationDecision",
    "FleetProfilePreview",
    "FleetProfileReason",
    "FleetProfileReviewedDecision",
    "FleetProfileRunEffect",
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
