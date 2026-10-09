"""Run switch contract: evidence."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    Field,
    StringConstraints,
    model_validator,
)
from vonk_agent_protocol.compiled_execution_plan import MemoryKind
from vonk_agent_protocol.inventory import MemoryPool
from vonk_forge_contracts.recipe import Scalar

from ..integer_domains import MAX_DATABASE_INTEGER
from ..mapping_parameters import MappingParameters
from ..run_switch_identity_contract import NodeId, UuidId
from ..strict_json import StrictModel
from .requests import MemoryUsageUncertainty, SparkGroupNode
from .vocabulary import (
    Alias,
    Digest,
    PortNumber,
    RunSwitchBuildEvidenceState,
    RunSwitchChangeEffect,
    RunSwitchCoverage,
    RunSwitchPhaseKind,
    RunSwitchReasonScope,
    RunSwitchReasonSeverity,
    RunSwitchRetention,
    RunSwitchSubphase,
)


class RunSwitchReason(StrictModel):
    code: Annotated[str, StringConstraints(min_length=1, max_length=96)]
    detail: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    severity: RunSwitchReasonSeverity
    scope: RunSwitchReasonScope
    node_ids: list[NodeId] = Field(default_factory=list, max_length=32)
    stale: bool = False


class FreshnessEvidence(StrictModel):
    source: Annotated[str, StringConstraints(min_length=1, max_length=96)]
    state: Literal["fresh", "stale", "unknown"]
    observed_at: datetime | None = None
    age_seconds: float | None = Field(default=None, ge=0)
    maximum_age_seconds: int | None = Field(default=None, ge=1, le=86_400)
    evidence_digest: Digest | None = None


class ResourceDemandEvidence(StrictModel):
    """The evidence terms used for one selected rank's memory fit."""

    weights_bytes: int | None = Field(default=None, ge=0)
    runtime_overhead_bytes: int | None = Field(default=None, ge=0)
    context_bytes: int | None = Field(default=None, ge=0)
    concurrency_bytes: int | None = Field(default=None, ge=0)
    batch_bytes: int | None = Field(default=None, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    evidence_state: Literal["declared", "measured", "fresh", "stale", "unknown"]
    evidence_digest: Digest | None = None


class EffectiveParallelism(StrictModel):
    """Derived from topology; never an editable settings field."""

    world_size: int = Field(ge=1, le=31)
    tensor: int = Field(ge=1, le=31)
    pipeline: int = Field(ge=1, le=31)
    data: int = Field(ge=1, le=31)
    backend: Annotated[str, StringConstraints(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def product_matches_world_size(self) -> EffectiveParallelism:
        if self.world_size != self.tensor * self.pipeline * self.data:
            raise ValueError("topology parallelism product must equal world_size")
        return self


class EffectiveSettingsSelection(StrictModel):
    """Canonical effective settings bound into the Run/Switch plan digest."""

    kind: Literal["generation", "embedding", "job"]
    context_tokens: int | None = Field(default=None, ge=1)
    concurrency: int | None = Field(default=None, ge=1)
    max_batch_tokens: int | None = Field(default=None, ge=1)
    parallelism: EffectiveParallelism
    knobs: dict[str, Scalar] = Field(default_factory=dict, max_length=64)
    change_effects: dict[str, RunSwitchChangeEffect] = Field(max_length=64)
    identity_sha256: Digest


class SparkFitNode(StrictModel):
    node_id: NodeId
    rank: int = Field(ge=0, le=31)
    role: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    allowed: bool
    ports_required: list[PortNumber]
    disk_required_bytes: int | None = Field(default=None, ge=0)
    disk_free_bytes: int | None = Field(default=None, ge=0)
    disk_free_after_bytes: int | None = None
    memory_required_bytes: int | None = Field(default=None, ge=0)
    memory_kind: MemoryKind | None = None
    memory_pool: MemoryPool | None = None
    memory_floor_bytes: int | None = Field(default=None, ge=0)
    memory_capacity_bytes: int | None = Field(default=None, ge=0)
    memory_available_bytes: int | None = Field(default=None, ge=0)
    memory_free_after_bytes: int | None = None
    memory_usage_uncertainty: MemoryUsageUncertainty | None = None
    resource_demand: ResourceDemandEvidence | None = None
    blockers: list[RunSwitchReason] = Field(default_factory=list, max_length=32)
    warnings: list[RunSwitchReason] = Field(default_factory=list, max_length=32)


class SparkFit(StrictModel):
    allowed: bool
    nodes: list[SparkFitNode] = Field(min_length=1, max_length=32)
    blockers: list[RunSwitchReason] = Field(default_factory=list, max_length=64)
    warnings: list[RunSwitchReason] = Field(default_factory=list, max_length=64)


class ArtifactStorageImpact(StrictModel):
    """Byte impact with unknown values preserved as unknown, never guessed."""

    # The model manifest is known before a source build produces an image.
    # Bind it independently of the combined rollout preparation receipt.
    artifact_set_sha256: Digest | None = None
    artifact_set_bytes: int | None = Field(default=None, ge=1)
    required_bytes: int | None = Field(default=None, ge=0)
    reused_bytes: int = Field(default=0, ge=0)
    copied_bytes: int = Field(default=0, ge=0)
    missing_nas_bytes: int | None = Field(default=None, ge=0)
    missing_spark_bytes: int | None = Field(default=None, ge=0)
    # Per-node evidence behind ``missing_spark_bytes`` (a SUM over targets).
    missing_spark_bytes_by_node: dict[str, Annotated[int, Field(ge=0)]] | None = None
    reclaimable_bytes: int = Field(default=0, ge=0)
    reclaimed_bytes: int = Field(default=0, ge=0)
    nas_coverage: RunSwitchCoverage
    spark_coverage: RunSwitchCoverage
    retention: RunSwitchRetention
    running_coverage: RunSwitchCoverage = "unknown"
    artifact_digests: list[Digest] = Field(default_factory=list, max_length=256)
    reclaimable_digests: list[Digest] = Field(default_factory=list, max_length=256)


class BuildSourceEvidence(StrictModel):
    state: Literal["available", "missing", "unknown"]
    source_bundle_sha256: Digest | None = None
    detail: Annotated[str, StringConstraints(max_length=256)] | None = None


class BuildCompatibilityEvidence(StrictModel):
    expected_architecture: Annotated[
        str, StringConstraints(min_length=1, max_length=64)
    ]
    observed_architecture: Annotated[str, StringConstraints(max_length=64)] | None = (
        None
    )
    state: Literal["compatible", "incompatible", "unknown"]
    evidence_digest: Digest | None = None
    detail: Annotated[str, StringConstraints(max_length=256)] | None = None


class RuntimeImageStorageImpact(StrictModel):
    build_id: UuidId | None
    preparation_required: bool
    image_digest: (
        Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")] | None
    )
    oci_layout_sha256: Digest | None = None
    image_bytes: int | None = Field(default=None, ge=0)
    required_bytes: int | None = Field(default=None, ge=0)
    reused_bytes: int = Field(default=0, ge=0)
    copied_bytes: int = Field(default=0, ge=0)
    missing_nas_bytes: int | None = Field(default=None, ge=0)
    missing_spark_bytes: int | None = Field(default=None, ge=0)
    missing_image_distribution_bytes: int | None = Field(default=None, ge=0)
    # Per-node evidence behind ``missing_image_distribution_bytes``.
    missing_image_distribution_bytes_by_node: (
        dict[str, Annotated[int, Field(ge=0)]] | None
    ) = None
    nas_coverage: RunSwitchCoverage
    spark_coverage: RunSwitchCoverage
    running_coverage: RunSwitchCoverage = "unknown"
    reclaimable_bytes: int = Field(default=0, ge=0)
    reclaimable_digests: list[Digest] = Field(default_factory=list, max_length=256)


class RunSwitchBuildEvidence(StrictModel):
    state: RunSwitchBuildEvidenceState
    build_id: UuidId | None
    # A pending build is still bound to an immutable source/build input and a
    # deterministically selected Controller builder.  The OCI output digest
    # is filled only after the durable build child records its receipt.
    build_input_sha256: Digest | None = None
    builder_node_id: NodeId | None = None
    image_digest: (
        Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")] | None
    )
    image_bytes: int | None = Field(default=None, ge=0)
    oci_layout_sha256: Digest | None = None
    source: BuildSourceEvidence
    compatibility: BuildCompatibilityEvidence
    runtime: RuntimeImageStorageImpact
    detail: Annotated[str, StringConstraints(max_length=512)] | None = None


class MappingSelection(StrictModel):
    mapping_id: UuidId | None
    mapping_generation: int | None = Field(le=MAX_DATABASE_INTEGER, default=None, ge=1)
    topology_name: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    parameters: MappingParameters = Field(default_factory=dict, max_length=128)
    # The effective recipe-option choices this mapping runs with (also inside
    # ``parameters``); empty for a recipe without options.
    option_choices: dict[str, str] = Field(default_factory=dict, max_length=16)
    placement_digest: Digest
    action: Literal["reuse", "create"]
    nodes: list[SparkGroupNode] = Field(min_length=1, max_length=32)


class StopImpact(StrictModel):
    run_id: UuidId
    run_plan_digest: Digest
    alias: Alias
    state: Annotated[str, StringConstraints(min_length=1, max_length=24)]
    node_ids: list[NodeId] = Field(min_length=1, max_length=32)
    reserved_bytes: int = Field(ge=0)
    plan_digest: Digest


class ConditionalPostStopMemoryCheck(StrictModel):
    """Fresh inventory and ordinary memory admission required after stops."""

    stop_run_ids: list[UuidId] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def ordered_unique_stops(self) -> ConditionalPostStopMemoryCheck:
        if self.stop_run_ids != sorted(set(self.stop_run_ids)):
            raise ValueError(
                "post-stop memory check identities must be unique and ordered"
            )
        return self


class RunSwitchPhase(StrictModel):
    index: int = Field(ge=0, le=31)
    kind: RunSwitchPhaseKind
    # ``kind`` stays in the shared lifecycle vocabulary.  This typed purpose
    # distinguishes Controller-side OCI preparation from target installation
    # while keeping Activity's generic phase enum stable.
    subphase: RunSwitchSubphase | None = None
    state: Literal["planned", "retained", "skipped", "blocked"]
    node_ids: list[NodeId] = Field(default_factory=list, max_length=32)
    operation_digest: Digest | None = None
    detail: Annotated[str, StringConstraints(min_length=1, max_length=256)]
