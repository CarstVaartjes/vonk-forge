"""Strict public contracts for saved Fleet profiles and their applications."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Literal, Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import (
    BeforeValidator,
    Field,
    StringConstraints,
    model_validator,
)
from vonk_agent_protocol import (
    DesiredAssignmentState,
    EndpointState,
    LifecycleState,
    LifecycleSubject,
    ObservedAssignmentState,
    OperationProgress,
    ProfileReasonCode,
    SupersedeCode,
    machine_adopter,
    state_adopter,
)
from vonk_agent_protocol.inventory import MemoryPool

from .endpoint_contract import EndpointResponse
from .model_cache_contract import CachedResourceEstimate
from .operation_blockers import OperationBlocker
from .preparation_contract import (
    CompatibilityIdentity,
    ModelArtifactIdentity,
    PreparationReason,
    RolloutPreparation,
    RuntimeImageIdentity,
)
from .recipe_update_notice import RecipeUpdateNotice
from .run_switch_contract import (
    ConditionalPostStopMemoryCheck,
    EffectiveSettingsSelection,
    MemoryKind,
    PortNumber,
    ResourceDemandEvidence,
    RunSwitchAssessment,
    RunSwitchOperationResult,
    RunSwitchProfileStopScope,
    RunSwitchReason,
    SparkGroupNode,
    StopImpact,
)
from .strict_json import StrictModel

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from .lifecycle.evidence import Residue


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


#: The most warnings one profile view carries.
MAX_PROFILE_WARNINGS = 128

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
OptionSlug = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
]
# Recipe option name -> chosen value, as the recipe declares them.
OptionChoices = Annotated[dict[OptionSlug, OptionSlug], Field(max_length=16)]
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
if TYPE_CHECKING:
    # A type checker reads the state as the contract's enum: comparing it with a
    # state word then narrows nothing, as a closed set of enum literals would.
    FleetProfileOperationState = LifecycleState
    FleetProfileCancellationState = LifecycleState
else:
    FleetProfileOperationState = Annotated[
        Literal[
            LifecycleState.QUEUED,
            LifecycleState.RUNNING,
            LifecycleState.NEEDS_OPERATOR,
            LifecycleState.SUCCEEDED,
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
            LifecycleState.SUPERSEDED,
        ],
        # An application written before the rename may still say ``waiting-for-operator``.
        BeforeValidator(state_adopter(LifecycleSubject.FLEET_PROFILE_APPLICATION)),
    ]
    #: The state of a cancellation intent: observed while it is being driven (it was
    #: ``cancelling``), then cancelled.
    FleetProfileCancellationState = Annotated[
        Literal[LifecycleState.OBSERVING, LifecycleState.CANCELLED],
        BeforeValidator(state_adopter(LifecycleSubject.FLEET_PROFILE_APPLICATION)),
    ]
#: Why an application ended ``superseded`` (never ``failed``): a newer accepted
#: intent, or the Controller's own automatic retry, took over its work.
FleetProfileSupersedeCode = SupersedeCode
#: Application states from which nothing more happens; one name for every consumer.
FLEET_PROFILE_ENDED_STATES = frozenset(
    {
        LifecycleState.SUCCEEDED.value,
        LifecycleState.FAILED.value,
        LifecycleState.CANCELLED.value,
        "superseded",
    }
)
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
DesiredAssignmentStateField = Annotated[
    DesiredAssignmentState, BeforeValidator(machine_adopter(DesiredAssignmentState))
]
FleetProfileAssignmentState = Annotated[
    ObservedAssignmentState, BeforeValidator(machine_adopter(ObservedAssignmentState))
]
FleetProfileAction = Literal["switch", "keep", "adopt"]
FleetProfilePlanStepKind = Literal["switch", "prepare"]
FleetProfileOperationKind = Literal["fleet-profile.apply"]
FleetProfileEndpointState = Annotated[
    EndpointState, BeforeValidator(machine_adopter(EndpointState))
]


class FleetProfileEndpointProjectionIssue(StrictModel):
    """Safe diagnostic for immutable profile history that cannot be read."""

    code: Literal[ProfileReasonCode.APPLICATION_INTENT_INVALID]
    detail: Annotated[str, StringConstraints(min_length=1, max_length=512)]


@dataclass(frozen=True)
class FleetProfileEndpointAssignmentIntent:
    """Loaded application assignment and exact current run candidate."""

    assignment_id: str
    recipe_title: str
    desired_state: DesiredAssignmentStateField
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
    assignments: tuple[FleetProfileEndpointAssignmentIntent, ...] | None
    projection_issue: FleetProfileEndpointProjectionIssue | None = None


class FleetProfileEndpointAssignmentView(StrictModel):
    assignment_id: UuidId
    recipe_title: Name
    desired_state: DesiredAssignmentStateField
    alias: Alias | None = None
    state: FleetProfileEndpointState
    endpoint: EndpointResponse | None = None

    @model_validator(mode="after")
    def endpoint_matches_assignment(self) -> FleetProfileEndpointAssignmentView:
        if self.state == EndpointState.PUBLISHED:
            if self.alias is None or self.endpoint is None:
                raise ValueError("published profile endpoint is incomplete")
            if self.endpoint.alias != self.alias:
                raise ValueError("published profile endpoint alias is inconsistent")
        elif self.endpoint is not None:
            raise ValueError("unpublished profile endpoint contains route data")
        if (
            self.state == EndpointState.INSTALLED_ONLY
            and self.desired_state != DesiredAssignmentState.INSTALLED
        ):
            raise ValueError("installed-only endpoint has a running assignment")
        return self


class FleetProfileEndpointsView(StrictModel):
    number: int = Field(ge=1)
    profile_id: UuidId | None = None
    application_id: UuidId | None = None
    application_state: FleetProfileOperationState | None = None
    observed_at: datetime
    # Required nullable: [] means membership is known to be empty, while null
    # means the immutable application could not be validated.
    assignments: list[FleetProfileEndpointAssignmentView] | None = Field(max_length=64)
    projection_issue: FleetProfileEndpointProjectionIssue | None = None

    @model_validator(mode="after")
    def application_identity_is_consistent(self) -> FleetProfileEndpointsView:
        if self.application_state is not None and self.application_id is None:
            raise ValueError("profile endpoint application identity is incomplete")
        if (
            self.application_id is not None
            and self.application_state is None
            and self.projection_issue is None
        ):
            raise ValueError(
                "profile endpoint application state is unknown without a reason"
            )
        if self.application_id is not None and self.profile_id is None:
            raise ValueError("profile endpoint application has no profile identity")
        if (self.assignments is None) != (self.projection_issue is not None):
            raise ValueError("profile endpoint membership availability is inconsistent")
        if self.projection_issue is not None and self.application_id is None:
            raise ValueError("profile endpoint projection issue has no application")
        return self


class FleetProfileNode(SparkGroupNode):
    """One rank of a profile assignment, the same shape a Spark group names it."""


class FleetProfileScope(StrictModel):
    """Frozen complete fleet boundary for a single execution plan.

    User profiles do not author this field.  It is captured from the enrolled
    roster when preview/load admits an operation and is retained so a running
    operation cannot silently expand or shrink with fleet membership changes.
    """

    node_ids: list[NodeId] = Field(max_length=32)

    @model_validator(mode="after")
    def validate_scope(self) -> FleetProfileScope:
        if len(self.node_ids) != len(set(self.node_ids)):
            raise ValueError("execution scope node IDs must be unique")
        if self.node_ids != sorted(self.node_ids):
            raise ValueError("execution scope node IDs must be sorted")
        return self


class FleetProfileAssignmentInput(StrictModel):
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
    desired_state: DesiredAssignmentStateField = DesiredAssignmentState.RUNNING
    # Saved with every option the recipe declares (a choice left out is filled
    # with the recipe default when the profile is saved). Changing a choice
    # changes the profile revision and needs a reload of the workload.
    option_choices: OptionChoices = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_assignment(self) -> FleetProfileAssignmentInput:
        if len(self.spark_ids) != len(set(self.spark_ids)):
            raise ValueError("profile assignment Spark IDs must be unique")
        if self.spark_ids != sorted(self.spark_ids):
            raise ValueError("profile assignment Spark IDs must be sorted")
        return self


class StoredFleetProfileAssignment(StrictModel):
    """Resolved assignment retained in an immutable execution snapshot."""

    id: UuidId
    recipe_revision_id: UuidId
    topology_name: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    desired_state: DesiredAssignmentStateField
    alias: Alias | None = None
    nodes: list[FleetProfileNode] = Field(min_length=1, max_length=32)
    # Effective recipe-option choices; a snapshot from before options existed
    # has none, which reads as every option's default.
    option_choices: OptionChoices = Field(default_factory=dict)

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
        if self.desired_state == DesiredAssignmentState.RUNNING and self.alias is None:
            raise ValueError("running profile assignments require an endpoint alias")
        if (
            self.desired_state == DesiredAssignmentState.INSTALLED
            and self.alias is not None
        ):
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


class FleetAssignmentModelView(StrictModel):
    """The model an assignment runs, as the operator reads it."""

    selector: str | None = None
    name: str | None = None
    variant: str | None = None
    state: str
    content_sha256: str | None = None


class FleetAssignmentRecipeView(StrictModel):
    """The recipe revision an assignment runs, as the operator reads it."""

    selector: str
    name: str | None = None
    state: str
    revision_id: str | None = None


class FleetCacheSummary(StrictModel):
    """How many assignments of a profile are cached, missing or unknown."""

    cached: int = Field(default=0, ge=0)
    missing: int = Field(default=0, ge=0)
    unknown: int = Field(default=0, ge=0)


class FleetNodeView(StrictModel):
    """One Spark of the fleet and whether the profile assigns it."""

    selector: str
    display_name: str
    state: str


class FleetProfileAssignmentView(StrictModel):
    """Canonical read projection of one logical profile assignment."""

    selector: Alias
    display_name: Name
    recipe_selector: RecipeSelector
    recipe_id: UuidId | None = None
    spark_ids: list[NodeId] = Field(min_length=1, max_length=32)
    required_sparks: int | None = Field(default=None, ge=1, le=32)
    assigned_sparks: int = Field(ge=1, le=32)
    model: FleetAssignmentModelView
    recipe: FleetAssignmentRecipeView
    resources: CachedResourceEstimate = Field(default_factory=CachedResourceEstimate)
    # The effective choice for every option of the recipe (defaults included).
    option_choices: OptionChoices = Field(default_factory=dict)
    observed_state: str = "Not loaded"
    # Set when the loaded workload runs an older revision than the newest one;
    # informational, a reload applies it.
    recipe_update: RecipeUpdateNotice | None = None

    @model_validator(mode="after")
    def validate_spark_projection(self) -> FleetProfileAssignmentView:
        if self.spark_ids != sorted(set(self.spark_ids)):
            raise ValueError("profile assignment Spark IDs must be sorted and unique")
        if self.assigned_sparks != len(self.spark_ids):
            raise ValueError("assigned_sparks must match spark_ids")
        return self


class FleetProfileDefinition(StrictModel):
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


class SavedProfileProjectionIssue(StrictModel):
    """An observation problem; it never authorizes changing saved intent."""

    code: Literal[ProfileReasonCode.DEFINITION_UNAVAILABLE] = (
        ProfileReasonCode.DEFINITION_UNAVAILABLE
    )
    detail: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    next_action: Annotated[str, StringConstraints(min_length=1, max_length=512)]


class FleetProfileDefinitionView(StrictModel):
    id: UuidId | None
    number: int = Field(ge=1)
    revision: int = Field(ge=0)
    definition: FleetProfileDefinition | None
    projection_issue: SavedProfileProjectionIssue | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> FleetProfileDefinitionView:
        if (self.definition is None) != (self.projection_issue is not None):
            raise ValueError("unavailable saved definition requires its cause")
        if self.definition is None and self.id is None:
            raise ValueError("an uncreated profile always has a readable definition")
        if (self.id is None) != (self.revision == 0):
            raise ValueError(
                "only an uncreated profile has revision zero and no identity"
            )
        return self


class FleetProfileView(StrictModel):
    id: UuidId
    number: int = Field(ge=1)
    # Zero until the first save, matching the definition view and PUT's
    # expected_revision for an uncreated profile.
    revision: int = Field(ge=0)
    name: Name
    description: Description
    installation_policy: FleetProfileInstallationPolicy
    labels: dict[LabelName, LabelValue]
    favorite: bool
    definition: FleetProfileDefinition
    assignments: list[FleetProfileAssignmentView]
    fleet: list[FleetNodeView] = Field(default_factory=list)
    status: str = "draft"
    loaded_revision: int | None = Field(default=None, ge=1)
    cache_summary: FleetCacheSummary = Field(default_factory=FleetCacheSummary)
    warnings: list[str] = Field(default_factory=list, max_length=MAX_PROFILE_WARNINGS)
    next_actions: list[str] = Field(default_factory=list, max_length=32)
    profile_digest: Digest
    created_by: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    created_at: datetime
    updated_at: datetime


class UnavailableFleetProfileView(StrictModel):
    """Keep an authorized saved identity visible without inventing its contents."""

    id: UuidId
    number: int = Field(ge=1)
    revision: int = Field(ge=1)
    status: Literal["unavailable"] = "unavailable"
    definition: None = None
    projection_issue: SavedProfileProjectionIssue


FleetProfileReadView = FleetProfileView | UnavailableFleetProfileView


class FleetProfileList(StrictModel):
    generated_at: datetime
    profiles: list[FleetProfileReadView] = Field(max_length=128)


class FleetProfileReason(StrictModel):
    code: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    detail: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    severity: Literal["info", "warning", "error"]


class FleetProfileAssignmentPreview(StrictModel):
    assignment_id: UuidId
    recipe_revision_id: UuidId
    recipe_title: Name
    desired_state: DesiredAssignmentStateField
    current_state: FleetProfileAssignmentState
    node_ids: list[NodeId] = Field(min_length=1, max_length=32)
    option_choices: OptionChoices = Field(default_factory=dict)
    actions: list[FleetProfileAction] = Field(max_length=7)
    reasons: list[FleetProfileReason] = Field(max_length=32)

    @model_validator(mode="after")
    def validate_nodes(self) -> FleetProfileAssignmentPreview:
        if len(self.node_ids) != len(set(self.node_ids)):
            raise ValueError("assignment preview node IDs must be unique")
        return self


class FleetProfileScopePreview(StrictModel):
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


class FleetProfilePlanStep(StrictModel):
    index: int = Field(ge=0, le=1023)
    kind: FleetProfilePlanStepKind
    node_ids: list[NodeId] = Field(default_factory=list, max_length=32)
    label: Annotated[str, StringConstraints(min_length=1, max_length=240)]

    @model_validator(mode="after")
    def validate_nodes(self) -> FleetProfilePlanStep:
        if len(self.node_ids) != len(set(self.node_ids)):
            raise ValueError("plan step node IDs must be unique")
        return self


class FleetProfilePlanSummary(StrictModel):
    already_correct: int = Field(ge=0, le=64)
    placements: int = Field(ge=0, le=64)
    builds: int = Field(ge=0, le=64)
    distributions: int = Field(ge=0, le=64)
    installs: int = Field(ge=0, le=64)
    starts: int = Field(ge=0, le=64)
    stops: int = Field(ge=0, le=512)
    uninstalls: int = Field(ge=0, le=512)
    blockers: int = Field(ge=0, le=2048)


class FleetProfileAssignmentPreparation(StrictModel):
    assignment_id: UuidId
    preparation: RolloutPreparation


class FleetProfileAssignmentAssessment(StrictModel):
    assignment_id: UuidId
    assessment: RunSwitchAssessment


class FleetProfileResourceRequirement(StrictModel):
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


class FleetProfileAdmissionDecision(StrictModel):
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


class FleetProfileCompatibilityDecision(StrictModel):
    kind: Literal["engine-generation", "jit", "tuning"]
    stage: Literal["controller-prepare", "target-prepare"]
    compatibility: CompatibilityIdentity
    node_ids: list[NodeId] = Field(min_length=1, max_length=64)
    artifact_sha256: Digest | None
    ready: bool


class FleetProfilePreparationDecision(StrictModel):
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


class FleetProfileRunEffect(StrictModel):
    run_id: UuidId
    installation_id: UuidId
    alias: Alias
    # node_ids always names the full stored topology.  For a profile-owned
    # cleanup after fleet removal, this separately binds the live Stop targets
    # and missing ranks; ordinary Stops keep this field absent.
    node_ids: list[NodeId] = Field(min_length=1, max_length=32)
    action: Literal["keep", "stop"]
    profile_stop_scope: RunSwitchProfileStopScope | None = None

    @model_validator(mode="after")
    def partial_scope_matches_effect(self) -> FleetProfileRunEffect:
        if self.node_ids != sorted(set(self.node_ids)):
            raise ValueError("profile run effect node IDs must be sorted and unique")
        scope = self.profile_stop_scope
        if scope is not None and (
            self.action != "stop"
            or self.node_ids
            != sorted(node.node_id for node in scope.original_group.nodes)
        ):
            raise ValueError("profile Stop scope differs from its full run effect")
        return self


class FleetProfileInstallationEffect(StrictModel):
    installation_id: UuidId
    node_ids: list[NodeId] = Field(min_length=1, max_length=32)
    action: Literal["keep", "remove"]


class FleetProfilePendingEffect(StrictModel):
    kind: Literal["job", "profile-application"]
    id: UuidId
    node_ids: list[NodeId] = Field(min_length=1, max_length=32)


class FleetProfileAdoptedStopEffect(StrictModel):
    """Exact original cleanup, retained by a newer whole-fleet decision."""

    effect: FleetProfileRunEffect
    queue_index: int = Field(ge=0)
    operation_id: UuidId
    request_key: UuidId

    @model_validator(mode="after")
    def is_stop(self) -> FleetProfileAdoptedStopEffect:
        if self.effect.action != "stop":
            raise ValueError("adopted cleanup must be an exact Stop effect")
        return self


class FleetProfileAdoptedApplicationEffect(StrictModel):
    """An exact continuing executor authorized by the newer reviewed snapshot."""

    application_id: UuidId
    plan_digest: Digest
    workload_intent_ordinal: int = Field(ge=1)
    node_ids: list[NodeId] = Field(min_length=1, max_length=32)
    assignment_ids: list[UuidId] = Field(default_factory=list, max_length=64)
    # No cleanup adoption has the same canonical wire as before this optional
    # effect was introduced; accepted assignment-only review digests stay exact.
    stops: list[FleetProfileAdoptedStopEffect] = Field(
        default_factory=list, exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def scope_is_canonical(self) -> FleetProfileAdoptedApplicationEffect:
        if not self.assignment_ids and not self.stops:
            raise ValueError("adoption requires an assignment or exact cleanup")
        if len({stop.effect.run_id for stop in self.stops}) != len(self.stops):
            raise ValueError("adopted cleanup identities must be unique")
        if any(
            not set(stop.effect.node_ids) <= set(self.node_ids) for stop in self.stops
        ):
            raise ValueError("adopted cleanup must retain its complete topology")
        if self.node_ids != sorted(set(self.node_ids)):
            raise ValueError("adopted effect nodes must be sorted and unique")
        if self.assignment_ids != sorted(set(self.assignment_ids)):
            raise ValueError("adopted assignment IDs must be sorted and unique")
        return self


class FleetProfileEffects(StrictModel):
    """Identified live effects, including complete distributed membership."""

    runs: list[FleetProfileRunEffect]
    installations: list[FleetProfileInstallationEffect]
    superseded: list[FleetProfilePendingEffect]
    adopted: list[FleetProfileAdoptedApplicationEffect] = Field(
        default_factory=list, max_length=64
    )


class FleetProfileChildProgress(StrictModel):
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


class FleetProfileSwitchQueueItem(StrictModel):
    """One durable Run/Switch child in the profile reconciliation queue."""

    kind: FleetProfileSwitchChildKind
    id: UuidId
    profile_stop_scope: RunSwitchProfileStopScope | None = None

    @model_validator(mode="after")
    def partial_stop_is_a_stop_item(self) -> FleetProfileSwitchQueueItem:
        if self.profile_stop_scope is not None and self.kind != "stop":
            raise ValueError("profile Stop scope requires a Stop queue item")
        return self


class FleetProfileSwitchPendingChild(StrictModel):
    """An issued queue child whose exact outcome remains to be observed."""

    queue_index: int = Field(ge=0)
    operation_id: UuidId
    original_operation_id: UuidId | None = None
    kind: FleetProfileSwitchChildKind


class FleetProfileSwitchChildState(StrictModel):
    """Terminal receipt for a child already completed by the adapter."""

    queue_index: int = Field(ge=0)
    operation_id: UuidId
    original_operation_id: UuidId | None = None
    kind: FleetProfileSwitchChildKind
    state: Literal["succeeded", "failed", "cancelled"]
    result: FleetProfileSwitchChildResult | None = None


class FleetProfileAssignmentFailure(StrictModel):
    queue_index: int | None = Field(default=None, ge=0)
    assignment_id: UuidId | None = None
    operation_id: UuidId | None = None
    reason: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    terminal: bool = False


class FleetProfileSwitchAdapterResult(StrictModel):
    """Complete result of reconciling every assignment in a profile scope."""

    children: list[FleetProfileSwitchChildState] = Field(max_length=128)
    assignment_ids: list[UuidId] = Field(max_length=64)

    @model_validator(mode="after")
    def assignment_ids_are_unique(self) -> FleetProfileSwitchAdapterResult:
        if len(self.assignment_ids) != len(set(self.assignment_ids)):
            raise ValueError("profile switch assignment IDs must be unique")
        return self


class FleetProfileSwitchAdapterState(StrictModel):
    """Persisted state used to resume a profile switch after restart."""

    schema_version: Literal[2] = 2
    child_id: UuidId
    scope_node_ids: list[NodeId] = Field(max_length=32)
    assignment_ids: list[UuidId] = Field(max_length=64)
    assignments: list[FleetProfileAssignment] = Field(max_length=64)
    queue: list[FleetProfileSwitchQueueItem] = Field(max_length=128)
    position: int = Field(default=0, ge=0, le=128)
    pending_children: list[FleetProfileSwitchPendingChild] = Field(default_factory=list)
    skipped_indices: list[int] = Field(default_factory=list)
    children: list[FleetProfileSwitchChildState] = Field(
        default_factory=list, max_length=128
    )
    assignment_failures: list[FleetProfileAssignmentFailure] = Field(
        default_factory=list, max_length=128
    )
    actor: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    request_id: UuidId
    state: FleetProfileOperationState = LifecycleState.QUEUED
    child_progress: FleetProfileChildProgress | None = None
    status_reason: Annotated[str, StringConstraints(max_length=512)] | None = None
    observation_due_at: datetime | None = None
    observation_deadline_at: datetime | None = None
    pending_operation_ids: list[UuidId] = Field(default_factory=list, max_length=128)
    stop_reissue_attempt: int = Field(default=0, ge=0, le=32)
    result: FleetProfileSwitchAdapterResult | None = None

    @model_validator(mode="after")
    def identities_are_consistent(self) -> FleetProfileSwitchAdapterState:
        if self.scope_node_ids != sorted(set(self.scope_node_ids)):
            raise ValueError("profile switch scope node IDs must be sorted and unique")
        if self.assignment_ids != [assignment.id for assignment in self.assignments]:
            raise ValueError("profile switch assignments must match assignment IDs")
        indices = [
            child.queue_index for child in (*self.pending_children, *self.children)
        ]
        indices.extend(self.skipped_indices)
        if len(indices) != len(set(indices)):
            raise ValueError("queue child identities must be unique")
        operation_ids = [
            child.operation_id for child in (*self.pending_children, *self.children)
        ]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("one operation cannot own multiple queue effects")
        if any(
            child.result is not None
            and child.result.run_switch_operation_id != child.operation_id
            for child in self.children
        ):
            raise ValueError("closed queue receipt differs from its operation identity")
        if self.position > len(self.queue) or any(
            index < 0 or index >= len(self.queue) for index in indices
        ):
            raise ValueError("queue child index is outside the immutable queue")
        if any(
            failure.queue_index is not None and failure.queue_index >= len(self.queue)
            for failure in self.assignment_failures
        ):
            raise ValueError("failure index is outside the immutable queue")
        for child in (*self.pending_children, *self.children):
            if child.kind != self.queue[child.queue_index].kind:
                raise ValueError("queue child kind differs from its reviewed item")
        return self


class FleetProfileSwitchChildResult(StrictModel):
    """Profile child receipt containing the public Run/Switch result tree."""

    run_switch_operation_id: UuidId
    run_switch: RunSwitchOperationResult


# The terminal child receipt is declared above its own type so a completed
# child can carry it; resolve that forward reference once both exist.
FleetProfileSwitchChildState.model_rebuild()


class FleetProfileVerificationResult(StrictModel):
    """Small result used by profile switch adapters that verify directly."""

    verified: bool


FleetProfileChildResult = (
    FleetProfileSwitchChildResult
    | FleetProfileSwitchAdapterResult
    | FleetProfileVerificationResult
)


class FleetProfileStepResult(StrictModel):
    """Result receipt for one completed profile plan step."""

    operation_id: UuidId
    kind: Literal["switch"]
    result: FleetProfileChildResult | None = None


class FleetProfileIntendedConfiguration(StrictModel):
    """Immutable desired configuration captured when execution is admitted."""

    profile_digest: Digest
    reviewed_plan_digest: Digest
    reviewed_application_id: UuidId
    installation_policy: FleetProfileInstallationPolicy
    scope: FleetProfileScope
    assignments: list[FleetProfileAssignment] = Field(max_length=64)


class FleetProfileApplicationCancellationIntent(StrictModel):
    """Durable identity and authority for an explicit or superseding cancel."""

    request_key: UuidId
    actor: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    requested_at: datetime
    state: FleetProfileCancellationState = LifecycleState.OBSERVING
    cause: Literal["operator", "superseded"]
    successor_application_id: UuidId | None = None
    workload_intent_ordinal: int | None = Field(default=None, ge=1)
    pending_operation_ids: list[UuidId] = Field(default_factory=list, max_length=128)
    observation_due_at: datetime | None = None
    observation_deadline_at: datetime | None = None


class FleetProfileApplicationEffect(StrictModel):
    """One exact profile or child effect in the cancellation receipt."""

    effect_id: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    kind: Literal[
        "profile-step", "run", "install", "stop", "cleanup", "agent-operation"
    ]
    label: Annotated[str, StringConstraints(min_length=1, max_length=240)]
    operation_id: UuidId | None = None
    outcome: Literal["succeeded", "failed", "cancelled", "pending", "not-issued"]


class FleetProfileApplicationCancellationView(StrictModel):
    """Live projection of completed, pending, and unissued cancelled effects."""

    request_key: UuidId
    actor: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    requested_at: datetime
    state: FleetProfileCancellationState
    cause: Literal["operator", "superseded"]
    completed_effects: list[FleetProfileApplicationEffect] = Field(max_length=1024)
    pending_effects: list[FleetProfileApplicationEffect] = Field(max_length=1024)
    cancelled_effects: list[FleetProfileApplicationEffect] = Field(max_length=1024)
    owner: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    dependency: UuidId | None = None
    deadline_at: datetime | None = None


class FleetProfileEffectProgress(StrictModel):
    """Observational receipt for one immutable accepted queue effect."""

    effect_id: str
    application_id: UuidId
    plan_digest: Digest
    workload_intent_ordinal: int = Field(ge=1)
    queue_index: int = Field(ge=0)
    kind: FleetProfileSwitchChildKind
    target_id: UuidId
    node_ids: list[NodeId]
    request_key: UuidId
    operation_id: UuidId | None = None
    original_operation_id: UuidId | None = None
    state: Literal[
        "not-issued", "pending", "succeeded", "failed", "cancelled", "unknown"
    ]
    result: FleetProfileSwitchChildResult | None = None
    stop_effect: FleetProfileRunEffect | None = None


class FleetProfileApplicationProgress(StrictModel):
    """Typed progress tree persisted with every profile application."""

    effects: list[FleetProfileEffectProgress] = Field(default_factory=list)
    attempt: int = Field(default=1, ge=1)
    retry_due_at: datetime | None = None
    retry_of_application_id: UuidId | None = None
    admission_pending: bool = False
    admission_attempt: int = Field(default=0, ge=0)
    admission_retry_at: datetime | None = None
    #: When the admission began waiting for disk it asked the collector to free;
    #: the wait is bounded, then the load ends with a typed refusal.
    storage_wait_since: datetime | None = None
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
    #: The application that took over this one's work (set when it ended
    #: ``superseded`` and a successor exists), with the typed reason.
    superseded_by: UuidId | None = None
    supersede_code: FleetProfileSupersedeCode | None = None
    #: What the application is waiting for, as of its latest check.
    blockers: list[OperationBlocker] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def progress_is_consistent(self) -> FleetProfileApplicationProgress:
        if self.total_steps < self.completed_steps:
            raise ValueError("profile progress completed steps exceed total steps")
        return self


class FleetProfileApplicationResult(StrictModel):
    """Terminal result for one profile application."""

    changed: bool
    completed_steps: int = Field(ge=0, le=1024)


class FleetProfileChildOperation(StrictModel):
    """Stable child operation envelope independent of the Run service module."""

    id: UuidId
    state: FleetProfileOperationState
    progress: FleetProfileChildProgress | None = None
    status_reason: Annotated[str, StringConstraints(max_length=512)] | None = None
    result: FleetProfileChildResult | None = None
    #: A phase the child keeps retrying; empty while it makes progress.
    stalls: list[OperationBlocker] = Field(default_factory=list, max_length=16)


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

    def recovery_refused(self, application_id: str, *, session: Session) -> bool:
        """Whether a failed child must not be replayed by profile recovery."""

        ...

    def failure_signature(self, application_id: str, *, session: Session) -> str | None:
        """The failure's identity without ids, counts or times; None if not stable."""

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
    ) -> FleetProfileChildOperation | Residue:
        """Reconcile the complete desired assignment set as one child operation.

        ``assignments`` is ordered by stable assignment identity and
        ``scope_node_ids`` is the complete sorted profile boundary.  The
        implementation must plan conflicts once and preserve healthy desired
        assignments while preparing or stopping other members.  Evidence it
        cannot establish (a damaged stored plan or intent) is returned as a
        ``Residue``: the application retires, it is not refused.
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


class FleetProfileReviewedDecision(StrictModel):
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
    #: What the Controller prepares by itself, in order, when this load is
    #: accepted: the model download and the runtime image build.
    preparation_steps: list[FleetProfilePlanStep] = Field(
        default_factory=list, max_length=128
    )
    #: True when preparation is all that stands between this plan and admission.
    waits_for_preparation: bool = False
    reasons: list[FleetProfileReason] = Field(max_length=128)


class FleetProfilePreview(FleetProfileReviewedDecision):
    generated_at: datetime
    assessments: list[FleetProfileAssignmentAssessment] = Field(max_length=64)
    preparations: list[FleetProfileAssignmentPreparation] = Field(
        default_factory=list, max_length=64
    )
    plan_digest: Digest
    #: Digest of the effects an operator consents to (what stops, starts, is
    #: installed or placed and which workloads are replaced), without
    #: observations such as free capacity, readiness or timestamps. A load that
    #: names it is accepted only while the fresh plan still has the same
    #: effects. Absent on plans stored before it existed.
    effects_digest: Digest | None = None

    def reviewed_decision(self) -> FleetProfileReviewedDecision:
        return FleetProfileReviewedDecision.model_validate(
            {
                name: getattr(self, name)
                for name in FleetProfileReviewedDecision.model_fields
            }
        )


class FleetProfileLoadRequest(StrictModel):
    request_key: UuidId
    #: The ``effects_digest`` of the preview the caller showed. Omitted means
    #: "the current plan": nothing was reviewed, so nothing can be stale.
    reviewed_effects_digest: Digest | None = None


class FleetProfileApplicationCancelRequest(StrictModel):
    profile_number: int = Field(ge=1)
    request_key: UuidId


class FleetProfileApplicationProjectionIssue(StrictModel):
    """Historical state is retained while its metadata cannot be verified."""

    code: Literal[ProfileReasonCode.APPLICATION_INTENT_INVALID]
    detail: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    observation: Literal["unknown"] = "unknown"


class FleetProfileApplicationView(StrictModel):
    id: UuidId
    request_key: UuidId
    profile_id: UuidId
    profile_digest: Digest
    plan_digest: Digest
    attempt: int = Field(default=1, ge=1)
    retry_of_application_id: UuidId | None = None
    #: The application that continues this one's work once it ended ``superseded``;
    #: follow the chain to the live application.
    superseded_by: UuidId | None = None
    #: Typed reason of a ``superseded`` end.
    reason_code: FleetProfileSupersedeCode | None = None
    state: FleetProfileOperationState
    current_step: int = Field(ge=0, le=1024)
    total_steps: int = Field(ge=0, le=1024)
    current_operation_id: UuidId | None
    status_reason: Annotated[str, StringConstraints(max_length=512)] | None
    progress: FleetProfileApplicationProgress
    cancellation: FleetProfileApplicationCancellationView | None = None
    result: FleetProfileApplicationResult | None
    projection_issue: FleetProfileApplicationProjectionIssue | None = None
    #: What a queued or failed application is waiting for; empty once it runs.
    blockers: list[OperationBlocker] = Field(default_factory=list, max_length=16)
    #: When the Controller will check again; an application that will retry is
    #: reported as ``queued`` with this time, never as ``failed``.
    next_attempt_at: datetime | None = None
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
        if self.state == "succeeded" and self.projection_issue is None:
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
            self.state in {LifecycleState.FAILED, LifecycleState.NEEDS_OPERATOR}
            and not (self.status_reason or "").strip()
        ):
            raise ValueError("failed or waiting application requires a failure reason")
        if self.state == "superseded":
            if self.reason_code is None and self.projection_issue is None:
                raise ValueError("superseded application requires a reason code")
            if (
                self.reason_code == SupersedeCode.SUPERSEDED_BY_RETRY
                and self.superseded_by is None
            ):
                raise ValueError("a retry supersession names its successor")
        elif self.superseded_by is not None or self.reason_code is not None:
            raise ValueError("only a superseded application names a successor")
        return self


__all__ = [
    "FLEET_PROFILE_ENDED_STATES",
    "FleetProfileAdoptedApplicationEffect",
    "FleetProfileAdoptedStopEffect",
    "FleetProfileApplicationCancelRequest",
    "FleetProfileApplicationCancellationIntent",
    "FleetProfileApplicationCancellationView",
    "FleetProfileApplicationEffect",
    "FleetProfileApplicationProgress",
    "FleetProfileApplicationProjectionIssue",
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
    "FleetProfileEffectProgress",
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
    "FleetProfileSupersedeCode",
    "FleetProfileSwitchAdapter",
    "FleetProfileSwitchAdapterResult",
    "FleetProfileSwitchAdapterState",
    "FleetProfileSwitchChildResult",
    "FleetProfileSwitchChildState",
    "FleetProfileSwitchPendingChild",
    "FleetProfileSwitchQueueItem",
    "FleetProfileVerificationResult",
    "FleetProfileView",
]
