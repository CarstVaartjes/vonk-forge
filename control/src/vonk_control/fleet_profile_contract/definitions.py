"""Fleet profile contract: definitions."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    Field,
    StringConstraints,
    model_validator,
)
from vonk_agent_protocol import (
    DesiredAssignmentState,
    ProfileReasonCode,
)

from ..integer_domains import MAX_DATABASE_INTEGER
from ..model_cache_contract import CachedResourceEstimate
from ..recipe_update_notice import RecipeUpdateNotice
from ..run_switch_contract import (
    SparkGroupNode,
)
from ..strict_json import StrictModel
from .vocabulary import (
    MAX_PROFILE_WARNINGS,
    Alias,
    Description,
    DesiredAssignmentStateField,
    Digest,
    FleetProfileInstallationPolicy,
    LabelName,
    LabelValue,
    Name,
    NodeId,
    OptionChoices,
    RecipeSelector,
    UuidId,
)


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
    expected_revision: int = Field(le=MAX_DATABASE_INTEGER, default=0, ge=0)


class SavedProfileProjectionIssue(StrictModel):
    """An observation problem; it never authorizes changing saved intent."""

    code: Literal[ProfileReasonCode.DEFINITION_UNAVAILABLE] = (
        ProfileReasonCode.DEFINITION_UNAVAILABLE
    )
    detail: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    next_action: Annotated[str, StringConstraints(min_length=1, max_length=512)]


class FleetProfileDefinitionView(StrictModel):
    id: UuidId | None
    number: int = Field(le=MAX_DATABASE_INTEGER, ge=1)
    revision: int = Field(le=MAX_DATABASE_INTEGER, ge=0)
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
    number: int = Field(le=MAX_DATABASE_INTEGER, ge=1)
    # Zero until the first save, matching the definition view and PUT's
    # expected_revision for an uncreated profile.
    revision: int = Field(le=MAX_DATABASE_INTEGER, ge=0)
    name: Name
    description: Description
    installation_policy: FleetProfileInstallationPolicy
    labels: dict[LabelName, LabelValue]
    favorite: bool
    definition: FleetProfileDefinition
    assignments: list[FleetProfileAssignmentView]
    fleet: list[FleetNodeView] = Field(default_factory=list)
    status: str = "draft"
    loaded_revision: int | None = Field(le=MAX_DATABASE_INTEGER, default=None, ge=1)
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
    number: int = Field(le=MAX_DATABASE_INTEGER, ge=1)
    revision: int = Field(le=MAX_DATABASE_INTEGER, ge=1)
    status: Literal["unavailable"] = "unavailable"
    definition: None = None
    projection_issue: SavedProfileProjectionIssue


FleetProfileReadView = FleetProfileView | UnavailableFleetProfileView


class FleetProfileList(StrictModel):
    generated_at: datetime
    profiles: list[FleetProfileReadView] = Field(max_length=128)
