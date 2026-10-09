"""Fleet profile contract: assessment."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    Field,
    StringConstraints,
    model_validator,
)
from vonk_agent_protocol.inventory import MemoryPool

from ..preparation_contract import (
    CompatibilityIdentity,
    ModelArtifactIdentity,
    PreparationReason,
    RolloutPreparation,
    RuntimeImageIdentity,
)
from ..run_switch_contract import (
    ConditionalPostStopMemoryCheck,
    EffectiveSettingsSelection,
    MemoryKind,
    PortNumber,
    ResourceDemandEvidence,
    RunSwitchAssessment,
    RunSwitchReason,
    StopImpact,
)
from ..strict_json import StrictModel
from .vocabulary import (
    Alias,
    DesiredAssignmentStateField,
    Digest,
    FleetProfileAction,
    FleetProfileAssignmentState,
    FleetProfilePlanStepKind,
    Name,
    NodeId,
    OptionChoices,
    UuidId,
)


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
