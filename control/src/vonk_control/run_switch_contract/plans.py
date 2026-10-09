"""Run switch contract: plans."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    Field,
    StringConstraints,
    model_validator,
)

from ..preparation_contract import RolloutPreparation
from ..run_switch_identity_contract import UuidId
from ..strict_json import StrictModel
from .evidence import (
    ArtifactStorageImpact,
    ConditionalPostStopMemoryCheck,
    EffectiveSettingsSelection,
    FreshnessEvidence,
    MappingSelection,
    RunSwitchBuildEvidence,
    RunSwitchPhase,
    RunSwitchReason,
    RuntimeImageStorageImpact,
    SparkFit,
    StopImpact,
)
from .requests import (
    InvocationMetadata,
    RunSwitchProfileStopScope,
    RunSwitchReconciliationAuthority,
    SparkGroup,
)
from .vocabulary import Alias, Digest, RunSwitchAction


class RunSwitchAssessment(StrictModel):
    """Planner-owned admission and observations shared by operator reviews."""

    alias: Alias | None
    freshness: list[FreshnessEvidence] = Field(default_factory=list, max_length=128)
    fit_current: SparkFit
    fit_after_stop: SparkFit | None
    post_stop_memory_check: ConditionalPostStopMemoryCheck | None = None
    effective_settings: EffectiveSettingsSelection | None = None
    preparation: RolloutPreparation | None = None
    stops: list[StopImpact] = Field(max_length=128)
    allowed: bool
    blockers: list[RunSwitchReason] = Field(max_length=128)
    warnings: list[RunSwitchReason] = Field(max_length=128)
    stop_before_prepare: bool = False
    stop_before_transfer: bool = False

    @model_validator(mode="after")
    def admission_matches_reasons(self) -> RunSwitchAssessment:
        if self.allowed != (not self.blockers):
            raise ValueError("admission verdict must agree with its named blockers")
        named = {
            (reason.code, reason.detail, tuple(sorted(reason.node_ids)))
            for reason in self.blockers
        }
        if self.preparation is not None and any(
            reason.severity == "blocker"
            and (reason.code, reason.detail, tuple(sorted(reason.node_ids)))
            not in named
            for reason in self.preparation.reasons
        ):
            raise ValueError("preparation blockers must be named by admission")
        freshness_by_node = {
            item.source.removeprefix("spark:").removesuffix(":inventory"): item
            for item in self.freshness
            if item.source.startswith("spark:") and item.source.endswith(":inventory")
        }
        for fit in (self.fit_current, self.fit_after_stop):
            if fit is None:
                continue
            for node in fit.nodes:
                uncertainty = node.memory_usage_uncertainty
                if uncertainty is None:
                    continue
                sample = freshness_by_node.get(node.node_id)
                if (
                    sample is None
                    or sample.observed_at != uncertainty.inventory_observed_at
                    or sample.evidence_digest != uncertainty.inventory_evidence_digest
                ):
                    raise ValueError(
                        "memory usage uncertainty must bind the node inventory sample"
                    )
        if self.post_stop_memory_check is not None:
            expected_stops = sorted(stop.run_id for stop in self.stops)
            if not expected_stops or (
                self.post_stop_memory_check.stop_run_ids != expected_stops
            ):
                raise ValueError(
                    "conditional memory check must bind the exact reviewed stops"
                )
            if self.fit_after_stop is not None:
                raise ValueError(
                    "conditional memory check cannot claim measured after-stop capacity"
                )
            if any(
                node.memory_required_bytes is None
                or node.memory_kind is None
                or node.memory_pool is None
                or node.memory_floor_bytes is None
                or node.memory_capacity_bytes is None
                or node.memory_available_bytes is None
                or node.resource_demand is None
                or node.memory_capacity_bytes
                < node.memory_required_bytes + node.memory_floor_bytes
                for node in self.fit_current.nodes
            ):
                raise ValueError(
                    "conditional memory check requires known feasible demand and capacity"
                )
            fit_nodes = {node.node_id for node in self.fit_current.nodes}
            if any(not set(stop.node_ids) <= fit_nodes for stop in self.stops):
                raise ValueError(
                    "conditional memory stops must stay within the reviewed target scope"
                )
        return self


class RunSwitchPlan(RunSwitchAssessment):
    schema_version: Literal[2] = 2
    generated_at: datetime
    action: RunSwitchAction
    model_content_sha256: Digest | None
    recipe_revision_id: UuidId | None
    recipe_content_sha256: Digest | None
    run_id: UuidId | None
    spark_group: SparkGroup
    # Present only when FleetProfile has reviewed a multi-Spark cleanup after
    # one or more ranks left the live fleet. The rest of the plan and its Job
    # targets cover reachable Sparks only.
    profile_stop_scope: RunSwitchProfileStopScope | None = None
    mapping: MappingSelection | None
    installation_id: UuidId | None
    installation_state: (
        Annotated[str, StringConstraints(min_length=1, max_length=24)] | None
    )
    # A scoped cleanup either removes installed bytes or abandons a persisted
    # plan that never reached a node.  The assessment owns the decision; the
    # phase executor reads it here instead of re-deriving it from state.
    cleanup_disposition: Literal["uninstall", "abandon"] = "uninstall"
    cleanup_mode: Literal["uninstall", "reconcile"] = "uninstall"
    reconciliation_authority: RunSwitchReconciliationAuthority | None = None
    recipe_build_id: UuidId | None
    image_digest: (
        Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")] | None
    )
    start_plan_digest: Digest | None
    # ``fit`` is the current admission view retained as a compact client
    # affordance; the two named views above make stop-before-prepare decisions
    # explicit for reviewers and profile callers.
    fit: SparkFit
    storage: ArtifactStorageImpact
    runtime_storage: RuntimeImageStorageImpact
    build: RunSwitchBuildEvidence
    conflicts: list[RunSwitchReason] = Field(max_length=128)
    reclaimed_bytes: int = Field(ge=0)
    phases: list[RunSwitchPhase] = Field(min_length=1, max_length=16)
    invocation: InvocationMetadata
    plan_digest: Digest

    @model_validator(mode="after")
    def cleanup_authority_matches_effect(self) -> RunSwitchPlan:
        scope = self.profile_stop_scope
        if scope is not None:
            target_ids = scope.target_node_ids
            if (
                self.action != "stop"
                or self.run_id is None
                or self.spark_group != scope.original_group
                or len(self.stops) != 1
                or self.stops[0].run_id != self.run_id
                or self.stops[0].node_ids != target_ids
                or self.mapping is None
                or [node.model_dump(mode="json") for node in self.mapping.nodes]
                != [node.model_dump(mode="json") for node in scope.original_group.nodes]
            ):
                raise ValueError(
                    "profile partial Stop plan does not match its reviewed scope"
                )
        if self.cleanup_mode == "uninstall":
            if self.reconciliation_authority is not None:
                raise ValueError(
                    "ordinary cleanup cannot carry reconciliation authority"
                )
            return self
        authority = self.reconciliation_authority
        if self.action != "cleanup" or self.installation_id is None:
            raise ValueError("reconciliation authority requires installation cleanup")
        if self.cleanup_disposition == "abandon":
            # A plan that never reached a node has no effect to reconcile:
            # reconciling it discards the record, with no receipt authority.
            if authority is not None:
                raise ValueError("an abandoned installation has no reconciliation")
            return self
        if self.allowed and authority is None:
            raise ValueError("allowed reconciliation requires exact authority")
        if authority is None:
            return self
        if (
            authority.installation_id != self.installation_id
            or authority.recipe_revision_id != self.recipe_revision_id
            or authority.recipe_content_sha256 != self.recipe_content_sha256
            or authority.mapping_id
            != (self.mapping.mapping_id if self.mapping else None)
            or authority.mapping_generation
            != (self.mapping.mapping_generation if self.mapping else None)
            or authority.image_digest != self.image_digest
            or authority.model_content_sha256 != self.model_content_sha256
        ):
            raise ValueError("reconciliation authority differs from cleanup identity")
        reviewed_targets = [
            (node.node_id, node.rank, node.role) for node in self.spark_group.nodes
        ]
        authority_targets = [
            (node.node_id, node.rank, node.role) for node in authority.targets
        ]
        if authority_targets != reviewed_targets:
            raise ValueError("reconciliation authority differs from target membership")
        return self

    def assessment(self) -> RunSwitchAssessment:
        return RunSwitchAssessment.model_validate(
            {name: getattr(self, name) for name in RunSwitchAssessment.model_fields}
        )
