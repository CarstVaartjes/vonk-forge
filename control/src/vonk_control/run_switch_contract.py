"""Strict, transport-neutral contracts for high-level Run and Switch work.

The low-level recipe routes remain useful primitives, but a client selecting a
model and a Spark group must be able to review one Controller-owned outcome
plan.  These contracts deliberately contain intent and evidence only.  They
do not encode a browser gesture, a button, or a transport-specific authority.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StringConstraints, model_validator
from vonk_agent_protocol import DistributionAssignment, OperationProgress
from vonk_agent_protocol.compiled_execution_plan import MemoryKind
from vonk_agent_protocol.inventory import MemoryPool

from .lifecycle_preflight import LifecyclePreflightCheckpoint
from .model_cache_contract import ModelCacheDownloadResult
from .preparation_contract import RolloutPreparation
from .runtime_image_preparation import RuntimeImageReceipt
from .strict_json import StrictJSONModel

_UUID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_NODE_PATTERN = r"^spk_[0-9a-f]{32}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"

UuidId = Annotated[str, StringConstraints(pattern=_UUID_PATTERN)]
NodeId = Annotated[str, StringConstraints(pattern=_NODE_PATTERN)]
Digest = Annotated[str, StringConstraints(pattern=_DIGEST_PATTERN)]
PortNumber = Annotated[int, Field(ge=1, le=65535)]
Alias = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$",
    ),
]

# Each closed value set is named once here and used by the contract's own field
# annotations and by the Run/Switch operation helpers that build those fields.
# A shared alias is what keeps a helper signature from drifting away from the
# set the model will accept, so the two cannot disagree without a type error.
RunSwitchPlacementAction = Literal["install", "run", "switch"]
RunSwitchAction = Literal[RunSwitchPlacementAction, "stop", "cleanup"]
RunSwitchRetention = Literal["retain-cached", "reclaim-unreferenced"]
RunSwitchReasonSeverity = Literal["blocker", "warning", "info"]
RunSwitchReasonScope = Literal[
    "model",
    "recipe",
    "mapping",
    "group",
    "node",
    "artifact",
    "freshness",
    "conflict",
    "operation",
]
RunSwitchCapabilityEvidenceState = Literal[
    "tested", "observed", "not-tested", "unknown"
]
RunSwitchChangeEffect = Literal["none", "restart", "reprepare", "rebuild", "reinstall"]
RunSwitchCoverage = Literal["complete", "partial", "unknown"]
RunSwitchBuildEvidenceState = Literal[
    "available",
    "planned",
    "building",
    "failed",
    "missing",
    "incompatible",
    "unknown",
]
RunSwitchContainerBuildState = Literal["planned", "building", "succeeded", "failed"]
RunSwitchPhaseKind = Literal[
    "transfer",
    "verify",
    "prepare",
    "cleanup",
    "stop",
    "start",
    "uninstall",
    "final_verify",
]
RunSwitchSubphase = Literal[
    "container-build",
    "model-download",
    "runtime-image",
    "runtime-plan",
    "target-copy",
    "runtime-install",
]
RunSwitchMemberState = Literal["pending", "running", "succeeded", "failed", "unknown"]
RunSwitchProgressState = Literal[
    "queued", "running", "succeeded", "failed", "cancelled", "unknown"
]
RunSwitchOperationKind = Literal[
    "recipe.run-switch.v2",
    "recipe.stop.v2",
    "recipe.cleanup.v2",
]


class _StrictModel(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class InvocationMetadata(_StrictModel):
    """Context for audit and tracing which has no decision-making authority."""

    origin: Annotated[
        str,
        StringConstraints(
            min_length=1,
            max_length=64,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$",
        ),
    ] = "operator"
    correlation_id: UuidId | None = None
    reason: Annotated[str, StringConstraints(max_length=256)] | None = None
    context: dict[
        Annotated[str, StringConstraints(min_length=1, max_length=64)],
        Annotated[str, StringConstraints(max_length=256)],
    ] = Field(default_factory=dict, max_length=16)


class SparkGroupNode(_StrictModel):
    node_id: NodeId
    rank: int = Field(ge=0, le=31)
    role: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    endpoint_owner: bool = False


class SparkGroup(_StrictModel):
    """A complete, rank-labelled Spark group selected by the operator."""

    nodes: list[SparkGroupNode] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_group(self) -> SparkGroup:
        node_ids = [node.node_id for node in self.nodes]
        ranks = [node.rank for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("Spark group node IDs must be unique")
        if sorted(ranks) != list(range(len(ranks))):
            raise ValueError("Spark group ranks must be contiguous from zero")
        if sum(node.endpoint_owner for node in self.nodes) != 1:
            raise ValueError("Spark group must have exactly one endpoint owner")
        return self


class RunMemoryResidualRange(_StrictModel):
    """Possible remaining bytes for one exact active run reservation."""

    run_id: UuidId
    run_generation: int = Field(ge=1, le=2**63 - 1)
    reservation_kind: Literal["host-memory", "gpu-memory", "unified-memory"]
    minimum_bytes: Literal[0] = 0
    maximum_bytes: int = Field(ge=0)


class MemoryUsageUncertainty(_StrictModel):
    """Fresh aggregate capacity lacks per-run resident usage evidence."""

    source: Literal["aggregate_inventory_without_run_usage"]
    inventory_observed_at: datetime
    inventory_evidence_digest: Digest
    residual_ranges: list[RunMemoryResidualRange] = Field(max_length=128)

    @model_validator(mode="after")
    def unique_ordered_claims(self) -> MemoryUsageUncertainty:
        keys = [
            (item.run_id, item.run_generation, item.reservation_kind)
            for item in self.residual_ranges
        ]
        if keys != sorted(set(keys)):
            raise ValueError("memory uncertainty claims must be unique and ordered")
        return self


class RunSwitchPreviewRequest(_StrictModel):
    schema_version: Literal[2] = 2
    model_content_sha256: Digest
    recipe_revision_id: UuidId
    spark_group: SparkGroup
    alias: Alias
    action: RunSwitchPlacementAction = "run"
    retention: RunSwitchRetention = "retain-cached"
    invocation: InvocationMetadata = Field(default_factory=InvocationMetadata)


class RunSwitchApplyRequest(RunSwitchPreviewRequest):
    # Preview binding and idempotency are generated by the Controller when a
    # caller uses the one-step Run/Switch route.  They remain accepted for
    # advanced clients that explicitly replay a reviewed plan.
    plan_digest: Digest | None = None
    request_key: UuidId | None = None


class RunSwitchStopPreviewRequest(_StrictModel):
    schema_version: Literal[2] = 2
    run_id: UuidId
    invocation: InvocationMetadata = Field(default_factory=InvocationMetadata)


class RunSwitchStopApplyRequest(RunSwitchStopPreviewRequest):
    plan_digest: Digest | None = None
    request_key: UuidId | None = None


class RunSwitchCleanupPreviewRequest(_StrictModel):
    """Ask Run/Switch to remove one installation that is no longer desired.

    Cleanup is authorized by the installation's own uninstall assessment, so it
    never requires launch readiness: removing work must not depend on being able
    to start work.  Run/Switch still owns the sequencing, the child reference
    and the retry budget for the removal.
    """

    schema_version: Literal[2] = 2
    installation_id: UuidId
    cleanup_mode: Literal["uninstall", "reconcile"] = "uninstall"
    invocation: InvocationMetadata = Field(default_factory=InvocationMetadata)


class RunSwitchCleanupApplyRequest(RunSwitchCleanupPreviewRequest):
    plan_digest: Digest | None = None
    request_key: UuidId | None = None


class RunSwitchReconciliationTarget(_StrictModel):
    """One exact rank and its current cleanup receipt state."""

    node_id: NodeId
    rank: int = Field(ge=0, le=31)
    role: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    installed_bytes: int = Field(ge=0)
    install_operation_id: UuidId
    install_operation_payload_sha256: Digest
    compiled_spec_canonical_sha256: Digest
    state: Literal["pending", "reconciled"]
    cleanup_receipt_sha256: Digest | None = None

    @model_validator(mode="after")
    def receipt_matches_state(self) -> RunSwitchReconciliationTarget:
        if (self.state == "reconciled") != (self.cleanup_receipt_sha256 is not None):
            raise ValueError("reconciliation receipt does not match target state")
        return self


class RunSwitchReconciliationAuthority(_StrictModel):
    """Controller-owned identity and effect binding for installation repair.

    The accepted installation plan remains opaque.  This authority records its
    canonical fingerprint and binds each target to the successful original
    ``recipe.install`` operation that supplied the persisted compiled spec.
    It never claims that malformed launch metadata is executable.
    """

    schema_version: Literal[2] = 2
    installation_id: UuidId
    original_plan_digest: Digest
    recipe_revision_id: UuidId
    recipe_content_sha256: Digest
    mapping_id: UuidId
    mapping_generation: int = Field(ge=1)
    recipe_build_id: UuidId | None
    image_digest: Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
    model_content_sha256: Digest | None
    stored_plan_canonical_sha256: Digest
    targets: list[RunSwitchReconciliationTarget] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def targets_are_exact_and_ordered(self) -> RunSwitchReconciliationAuthority:
        keys = [(target.rank, target.node_id) for target in self.targets]
        node_ids = [target.node_id for target in self.targets]
        ranks = [target.rank for target in self.targets]
        if (
            len(set(node_ids)) != len(node_ids)
            or len(set(ranks)) != len(ranks)
            or sorted(ranks) != list(range(len(self.targets)))
            or keys != sorted(keys)
        ):
            raise ValueError(
                "reconciliation targets must have unique nodes and contiguous ranks"
            )
        return self


class RunSwitchReason(_StrictModel):
    code: Annotated[str, StringConstraints(min_length=1, max_length=96)]
    detail: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    severity: RunSwitchReasonSeverity
    scope: RunSwitchReasonScope
    node_ids: list[NodeId] = Field(default_factory=list, max_length=32)
    stale: bool = False


class FreshnessEvidence(_StrictModel):
    source: Annotated[str, StringConstraints(min_length=1, max_length=96)]
    state: Literal["fresh", "stale", "unknown"]
    observed_at: datetime | None = None
    age_seconds: float | None = Field(default=None, ge=0)
    maximum_age_seconds: int | None = Field(default=None, ge=1, le=86_400)
    evidence_digest: Digest | None = None


class CapabilityEvidence(_StrictModel):
    """One capability's declaration and evidence, kept separate by owner."""

    name: Annotated[str, StringConstraints(min_length=1, max_length=96)]
    declared: bool | None
    evidence: RunSwitchCapabilityEvidenceState
    support: Literal["supported", "unsupported", "unknown"]
    evidence_digest: Digest | None = None
    detail: Annotated[str, StringConstraints(max_length=256)] | None = None


class ResourceDemandEvidence(_StrictModel):
    """The evidence terms used for one selected rank's memory fit."""

    weights_bytes: int | None = Field(default=None, ge=0)
    runtime_overhead_bytes: int | None = Field(default=None, ge=0)
    context_bytes: int | None = Field(default=None, ge=0)
    concurrency_bytes: int | None = Field(default=None, ge=0)
    batch_bytes: int | None = Field(default=None, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    evidence_state: Literal["declared", "measured", "fresh", "stale", "unknown"]
    evidence_digest: Digest | None = None


class EffectiveParallelism(_StrictModel):
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


class EffectiveSettingsSelection(_StrictModel):
    """Canonical effective settings bound into the Run/Switch plan digest."""

    kind: Literal["generation", "embedding", "job"]
    context_tokens: int | None = Field(default=None, ge=1)
    concurrency: int | None = Field(default=None, ge=1)
    max_batch_tokens: int | None = Field(default=None, ge=1)
    parallelism: EffectiveParallelism
    knobs: dict[str, object] = Field(default_factory=dict, max_length=64)
    change_effects: dict[str, RunSwitchChangeEffect] = Field(max_length=64)
    identity_sha256: Digest


class SparkFitNode(_StrictModel):
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


class SparkFit(_StrictModel):
    allowed: bool
    nodes: list[SparkFitNode] = Field(min_length=1, max_length=32)
    blockers: list[RunSwitchReason] = Field(default_factory=list, max_length=64)
    warnings: list[RunSwitchReason] = Field(default_factory=list, max_length=64)


class ArtifactStorageImpact(_StrictModel):
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
    reclaimable_bytes: int = Field(default=0, ge=0)
    reclaimed_bytes: int = Field(default=0, ge=0)
    nas_coverage: RunSwitchCoverage
    spark_coverage: RunSwitchCoverage
    retention: RunSwitchRetention
    running_coverage: RunSwitchCoverage = "unknown"
    artifact_digests: list[Digest] = Field(default_factory=list, max_length=256)
    reclaimable_digests: list[Digest] = Field(default_factory=list, max_length=256)


class BuildSourceEvidence(_StrictModel):
    state: Literal["available", "missing", "unknown"]
    source_bundle_sha256: Digest | None = None
    detail: Annotated[str, StringConstraints(max_length=256)] | None = None


class BuildCompatibilityEvidence(_StrictModel):
    expected_architecture: Annotated[
        str, StringConstraints(min_length=1, max_length=64)
    ]
    observed_architecture: Annotated[str, StringConstraints(max_length=64)] | None = (
        None
    )
    state: Literal["compatible", "incompatible", "unknown"]
    evidence_digest: Digest | None = None
    detail: Annotated[str, StringConstraints(max_length=256)] | None = None


class RuntimeImageStorageImpact(_StrictModel):
    build_id: UuidId | None
    preparation_required: bool
    registry_manifest_digest: (
        Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")] | None
    ) = None
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
    nas_coverage: RunSwitchCoverage
    spark_coverage: RunSwitchCoverage
    running_coverage: RunSwitchCoverage = "unknown"
    reclaimable_bytes: int = Field(default=0, ge=0)
    reclaimable_digests: list[Digest] = Field(default_factory=list, max_length=256)


class RunSwitchBuildEvidence(_StrictModel):
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


class MappingSelection(_StrictModel):
    mapping_id: UuidId | None
    mapping_generation: int | None = Field(default=None, ge=1)
    topology_name: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    parameters: dict[str, object] = Field(default_factory=dict, max_length=128)
    placement_digest: Digest
    action: Literal["reuse", "create"]
    nodes: list[SparkGroupNode] = Field(min_length=1, max_length=32)


class StopImpact(_StrictModel):
    run_id: UuidId
    run_plan_digest: Digest
    alias: Alias
    state: Annotated[str, StringConstraints(min_length=1, max_length=24)]
    node_ids: list[NodeId] = Field(min_length=1, max_length=32)
    reserved_bytes: int = Field(ge=0)
    plan_digest: Digest


class ConditionalPostStopMemoryCheck(_StrictModel):
    """Fresh inventory and ordinary memory admission required after stops."""

    stop_run_ids: list[UuidId] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def ordered_unique_stops(self) -> ConditionalPostStopMemoryCheck:
        if self.stop_run_ids != sorted(set(self.stop_run_ids)):
            raise ValueError(
                "post-stop memory check identities must be unique and ordered"
            )
        return self


class RunSwitchPhase(_StrictModel):
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


class RunSwitchAssessment(_StrictModel):
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
    model_capabilities: list[CapabilityEvidence] = Field(max_length=128)
    recipe_capabilities: list[CapabilityEvidence] = Field(max_length=128)
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
        if self.cleanup_mode == "uninstall":
            if self.reconciliation_authority is not None:
                raise ValueError(
                    "ordinary cleanup cannot carry reconciliation authority"
                )
            return self
        authority = self.reconciliation_authority
        if self.action != "cleanup" or self.installation_id is None:
            raise ValueError("reconciliation authority requires installation cleanup")
        if self.cleanup_disposition != "uninstall":
            raise ValueError("reconciliation cannot abandon an installation")
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


class RunSwitchMemberProgress(_StrictModel):
    node_id: NodeId
    phase: RunSwitchPhaseKind | None = None
    state: RunSwitchMemberState
    completed_bytes: int = Field(default=0, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    error: Annotated[str, StringConstraints(max_length=256)] | None = None


class RunSwitchProgress(_StrictModel):
    operation: OperationProgress | None = None
    startup_budget_seconds: int | None = Field(default=None, ge=1)
    start_deadline: datetime | None = None
    phase_index: int = Field(ge=0, le=31)
    phase_count: int = Field(ge=1, le=32)
    phase: RunSwitchPhaseKind | None
    state: RunSwitchProgressState
    completed_bytes: int = Field(default=0, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    total_bytes_known: bool
    subphase: RunSwitchSubphase | None = None
    members: list[RunSwitchMemberProgress] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def total_bytes_state_is_consistent(self) -> RunSwitchProgress:
        if self.total_bytes_known != (self.total_bytes is not None):
            raise ValueError("progress total byte knowledge is inconsistent")
        if self.total_bytes is not None and self.completed_bytes > self.total_bytes:
            raise ValueError("progress completed bytes exceed total bytes")
        return self


class ArtifactVerificationEvidence(_StrictModel):
    """One node's immutable artifact handoff evidence."""

    node_id: NodeId
    verified: bool | None = None
    verified_digests: list[Digest] = Field(default_factory=list, max_length=256)
    downloaded_bytes: int | None = Field(default=None, ge=0)
    copied_bytes: int | None = Field(default=None, ge=0)
    verified_image_digest: (
        Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")] | None
    ) = None
    imported_image_digest: (
        Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")] | None
    ) = None
    verified_oci_layout_sha256: Digest | None = None
    error: Annotated[str, StringConstraints(max_length=512)] | None = None
    reason: Annotated[str, StringConstraints(max_length=512)] | None = None
    uncertain: bool = False


class RunSwitchMemberReceipt(_StrictModel):
    """Durable member projection emitted by a child distribution operation."""

    node_id: NodeId
    phase: RunSwitchPhaseKind | None = None
    state: RunSwitchMemberState
    completed_bytes: int = Field(default=0, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    error: Annotated[str, StringConstraints(max_length=512)] | None = None
    cached: bool = False


class RunSwitchRankReceipt(_StrictModel):
    node_id: NodeId
    rank: int = Field(ge=0, le=31)
    role: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    state: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    fresh: bool | None = None


class RunSwitchChildProgress(_StrictModel):
    """Progress nested in a durable child receipt."""

    operation: OperationProgress | None = None

    phase: (
        Literal[
            "transfer",
            "verify",
            "prepare",
            "cleanup",
            "stop",
            "start",
            "final_verify",
            "container-build",
            "model-download",
            "runtime-image",
            "runtime-plan",
            "target-copy",
            "runtime-install",
        ]
        | None
    ) = None
    completed_bytes: int = Field(default=0, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    total_bytes_known: bool = False
    members: list[RunSwitchMemberReceipt] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def total_bytes_state_is_consistent(self) -> RunSwitchChildProgress:
        if self.total_bytes_known != (self.total_bytes is not None):
            raise ValueError("child progress total byte knowledge is inconsistent")
        if self.total_bytes is not None and self.completed_bytes > self.total_bytes:
            raise ValueError("child progress completed bytes exceed total bytes")
        return self


class ArtifactVerificationResult(_StrictModel):
    """Canonical evidence returned by a completed artifact verify phase."""

    skipped: bool = False
    verified: Literal[True]
    verified_digests: list[Digest] = Field(max_length=256)
    verified_build_id: UuidId | None
    verified_image_digest: Annotated[
        str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")
    ]
    verified_registry_manifest_digest: (
        Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")] | None
    ) = None
    verified_oci_layout_sha256: Digest
    cached_nodes: list[NodeId] = Field(default_factory=list, max_length=32)
    cached_target_totals: dict[NodeId, int] = Field(default_factory=dict)
    evidence: list[ArtifactVerificationEvidence] = Field(default_factory=list)


class _RunSwitchPhaseBase(_StrictModel):
    # `subphase` is declared by each concrete result rather than here. A default
    # on this base makes every subclass that narrows the field to its own
    # Literal an override that drops the default, which pyright reports nine
    # times; a base field without a default instead makes it required on the
    # four results that do not narrow it, changing the committed OpenAPI. Each
    # result declaring the field it actually publishes keeps both quiet without
    # touching a published schema.
    phase: RunSwitchPhaseKind


class RunSwitchContainerBuildResult(_RunSwitchPhaseBase):
    phase: Literal["prepare"]
    subphase: Literal["container-build"]
    build_id: UuidId
    build_input_sha256: Digest
    state: RunSwitchContainerBuildState
    image_digest: (
        Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")] | None
    ) = None
    oci_layout_sha256: Digest | None = None
    image_bytes: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def succeeded_build_has_receipt(self) -> RunSwitchContainerBuildResult:
        if self.state == "succeeded" and (
            self.image_digest is None
            or self.oci_layout_sha256 is None
            or self.image_bytes is None
        ):
            raise ValueError("succeeded build phase requires complete image receipt")
        return self


class RunSwitchRuntimeImageResult(_RunSwitchPhaseBase):
    phase: Literal["prepare"]
    subphase: Literal["runtime-image"]
    runtime_image: RuntimeImageReceipt
    effective_execution_key: Digest | None = None
    image_digest: Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
    oci_layout_sha256: Digest
    image_bytes: int = Field(ge=1)
    build_id: UuidId | None = None

    @model_validator(mode="after")
    def outer_identity_matches_receipt(self) -> RunSwitchRuntimeImageResult:
        if (
            self.runtime_image.image_digest != self.image_digest
            or self.runtime_image.oci_archive_sha256 != self.oci_layout_sha256
            or self.runtime_image.image_bytes != self.image_bytes
        ):
            raise ValueError(
                "runtime image phase identity differs from canonical receipt"
            )
        if self.build_id is not None and self.runtime_image.build_id != self.build_id:
            raise ValueError("runtime image phase build identity differs from receipt")
        return self


class RunSwitchModelDownloadResult(ModelCacheDownloadResult):
    phase: Literal["transfer"]
    subphase: Literal["model-download"]
    skipped: Literal[True] = True
    downloaded_bytes: int = Field(ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    progress: RunSwitchChildProgress
    reason: Annotated[str, StringConstraints(max_length=512)] | None = None
    evidence: ModelCacheDownloadResult | None = None

    @model_validator(mode="after")
    def nested_identity_matches_result(self) -> RunSwitchModelDownloadResult:
        if (
            self.evidence is not None
            and self.evidence.artifact_set_sha256 != self.artifact_set_sha256
        ):
            raise ValueError("model download evidence artifact set differs from result")
        return self


class RunSwitchModelDownloadPendingResult(_RunSwitchPhaseBase):
    phase: Literal["transfer"]
    subphase: Literal["model-download"]
    schema_version: Literal[2] = 2
    artifact_set_sha256: Digest
    downloaded_bytes: int = Field(ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    progress: RunSwitchChildProgress
    reason: Annotated[str, StringConstraints(max_length=512)] | None = None


class RunSwitchTargetTransferResult(_RunSwitchPhaseBase):
    phase: Literal["transfer"]
    subphase: Literal["target-copy"]
    cached_nodes: list[NodeId] = Field(default_factory=list, max_length=32)
    assignments: dict[NodeId, DistributionAssignment] = Field(min_length=1)


class RunSwitchTargetTransferEvidenceResult(_RunSwitchPhaseBase):
    phase: Literal["transfer"]
    subphase: Literal["target-copy"]
    node_id: NodeId
    verified: Literal[True]
    verified_digests: list[Digest] = Field(min_length=1, max_length=256)
    verified_image_digest: Annotated[
        str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")
    ]
    imported_image_digest: Annotated[
        str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")
    ]
    verified_oci_layout_sha256: Digest
    downloaded_bytes: int | None = Field(default=None, ge=0)
    copied_bytes: int | None = Field(default=None, ge=0)


class RunSwitchCachedTransferResult(_RunSwitchPhaseBase):
    phase: Literal["transfer"]
    subphase: Literal["target-copy"]
    skipped: Literal[True]
    verified: Literal[False]
    verified_digests: list[Digest] = Field(max_length=256)
    verified_build_id: UuidId | None
    verified_image_digest: Annotated[
        str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")
    ]
    verified_oci_layout_sha256: Digest
    cached_nodes: list[NodeId] = Field(min_length=1, max_length=32)
    cached_target_totals: dict[NodeId, int]


class RunSwitchVerifyResult(ArtifactVerificationResult):
    phase: Literal["verify"]
    subphase: Literal["target-copy"]


class RunSwitchCleanupResult(_RunSwitchPhaseBase):
    phase: Literal["cleanup"]
    subphase: RunSwitchSubphase | None = None
    scope: Literal["spark-local"]
    reclaimed_bytes: int = Field(ge=0)
    protected_referenced_bytes: int = Field(default=0, ge=0)
    reclaimed_digests: list[Digest] = Field(default_factory=list, max_length=256)
    protected_digests: list[Digest] = Field(default_factory=list, max_length=256)
    nas_evicted: bool


class RunSwitchRuntimePlanResult(_RunSwitchPhaseBase):
    phase: Literal["prepare"]
    subphase: Literal["runtime-plan"]
    installation_id: UuidId
    mapping_id: UuidId
    install_plan_digest: Digest
    model_artifact_set_sha256: Digest | None = None
    model_artifact_set_bytes: int | None = Field(default=None, ge=0)
    compiled_plan_persisted: Literal[True]


class RunSwitchPreparedResult(_RunSwitchPhaseBase):
    phase: Literal["prepare"]
    subphase: Literal["runtime-plan"]
    prepared: Literal[True]


class RunSwitchRuntimeInstallResult(_RunSwitchPhaseBase):
    phase: Literal["prepare"]
    subphase: Literal["runtime-install"]
    installation_id: UuidId


class RunSwitchStopResult(_RunSwitchPhaseBase):
    phase: Literal["stop"]
    subphase: RunSwitchSubphase | None = None
    run_id: UuidId


class RunSwitchStartResult(_RunSwitchPhaseBase):
    phase: Literal["start"]
    subphase: RunSwitchSubphase | None = None
    run_id: UuidId


class RunSwitchUninstallResult(_RunSwitchPhaseBase):
    """The removal of one installation that is no longer desired."""

    phase: Literal["uninstall"]
    subphase: RunSwitchSubphase | None = None
    installation_id: UuidId
    # ``abandoned`` records a persisted plan that never reached a node, so the
    # operator sees why the record was disposed of without node work.
    disposition: Literal["uninstalled", "abandoned"] = "uninstalled"
    reason: Annotated[str, StringConstraints(max_length=512)] | None = None


class RunSwitchCleanupVerifyResult(_RunSwitchPhaseBase):
    """Observed removal of the installation, derived from durable state."""

    phase: Literal["final_verify"]
    subphase: RunSwitchSubphase | None = None
    final_verified: bool
    installation_id: UuidId
    removed: bool
    active_runs: int = Field(default=0, ge=0)
    installation_state: (
        Annotated[str, StringConstraints(min_length=1, max_length=24)] | None
    ) = None


class RunSwitchFinalVerifyResult(_RunSwitchPhaseBase):
    phase: Literal["final_verify"]
    subphase: RunSwitchSubphase | None = None
    final_verified: bool
    run_id: UuidId
    state: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    route_state: Annotated[str, StringConstraints(max_length=64)]
    healthy: bool
    ranks: list[RunSwitchRankReceipt] = Field(max_length=32)


class RunSwitchInstallationVerifyResult(_RunSwitchPhaseBase):
    """Exact installed membership observed without a serving workload."""

    phase: Literal["final_verify"]
    subphase: RunSwitchSubphase | None = None
    final_verified: bool
    installation_id: UuidId
    installation_state: Annotated[str, StringConstraints(min_length=1, max_length=24)]
    active_runs: int = Field(ge=0)
    unwithdrawn_routes: int = Field(ge=0)
    ranks: list[RunSwitchRankReceipt] = Field(min_length=1, max_length=32)


class RunSwitchDistributionChildResult(_StrictModel):
    """Durable projection of one target-copy child operation.

    A child Job has a different persisted shape from a parent phase receipt:
    it owns member progress and per-node handoff evidence.  Keeping that
    projection separate prevents a progress snapshot from being accepted as
    a completed phase result.
    """

    phase: Literal["transfer"]
    subphase: Literal["target-copy"]
    progress: RunSwitchChildProgress
    members: list[RunSwitchMemberReceipt] = Field(min_length=1, max_length=32)
    evidence: list[ArtifactVerificationEvidence] = Field(max_length=32)
    reason: Annotated[str, StringConstraints(max_length=512)] | None = None


RunSwitchPhaseResult = (
    RunSwitchContainerBuildResult
    | RunSwitchRuntimeImageResult
    | RunSwitchModelDownloadResult
    | RunSwitchModelDownloadPendingResult
    | RunSwitchTargetTransferResult
    | RunSwitchCachedTransferResult
    | RunSwitchTargetTransferEvidenceResult
    | RunSwitchVerifyResult
    | RunSwitchCleanupResult
    | RunSwitchRuntimePlanResult
    | RunSwitchPreparedResult
    | RunSwitchRuntimeInstallResult
    | RunSwitchStopResult
    | RunSwitchStartResult
    | RunSwitchUninstallResult
    | RunSwitchFinalVerifyResult
    | RunSwitchCleanupVerifyResult
    | RunSwitchInstallationVerifyResult
)


class RunSwitchCancellation(_StrictModel):
    request_key: UuidId
    actor: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    reason: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    requested_at: datetime


class RunSwitchRuntimeImageReferenceIntent(_StrictModel):
    """Exact image bytes provisionally protected by a current RunSwitch job."""

    schema_version: Literal[2] = 2
    owner_kind: Literal["run-switch-job"]
    operation_id: UuidId
    request_key: UuidId
    actor: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    plan_digest: Digest
    phase_index: int = Field(ge=0, le=31)
    item_index: int = Field(ge=0, le=31)
    workload_intent_ordinal: int = Field(ge=1)
    recipe_revision_id: UuidId
    profile_application_id: UuidId | None = None
    execution_keys: list[Digest] = Field(min_length=1, max_length=32)
    source: Literal["published", "controller-build"]
    registry_manifest_digest: (
        Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")] | None
    ) = None
    image_digest: Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
    archive_sha256: Digest
    image_bytes: int = Field(strict=True, ge=1, le=16 * 1024**4)
    build_id: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = (
        None
    )
    build_input_sha256: Digest | None = None

    @model_validator(mode="after")
    def reference_identity_is_consistent(
        self,
    ) -> RunSwitchRuntimeImageReferenceIntent:
        if self.execution_keys != sorted(set(self.execution_keys)):
            raise ValueError("RunSwitch runtime execution keys are not canonical")
        if self.source == "published" and (
            self.registry_manifest_digest is None
            or self.build_id is not None
            or self.build_input_sha256 is not None
        ):
            raise ValueError("published RunSwitch image reference is inconsistent")
        if self.source == "controller-build" and (
            self.registry_manifest_digest is not None or self.build_id is None
        ):
            raise ValueError("built RunSwitch image reference is inconsistent")
        return self


class RunSwitchOperationResult(_StrictModel):
    """Exact durable result tree stored in ``Job.result``."""

    phase_index: int = Field(default=0, ge=0, le=31)
    workload_intent_ordinal: int | None = Field(default=None, ge=1)
    profile_application_id: UuidId | None = None
    item_index: int = Field(default=0, ge=0, le=31)
    phase: RunSwitchPhaseKind | None = None
    subphase: RunSwitchSubphase | None = None
    completed_phases: list[RunSwitchPhaseKind] = Field(
        default_factory=list, max_length=16
    )
    child_operation_id: UuidId | None = None
    runtime_image_reference_intent: RunSwitchRuntimeImageReferenceIntent | None = None
    phase_results: list[RunSwitchPhaseResult] = Field(default_factory=list)
    operation_phase_index: int | None = Field(default=None, ge=0, le=31)
    preflight: LifecyclePreflightCheckpoint | None = None
    cancellation: RunSwitchCancellation | None = None
    operation: OperationProgress | None = None
    completed_bytes: int = Field(default=0, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    total_bytes_known: bool = False
    members: list[RunSwitchMemberReceipt] = Field(default_factory=list, max_length=32)
    retryable: bool = False
    failure_code: (
        Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.:-]{0,95}$")] | None
    ) = None
    retry_attempt: int | None = Field(default=None, ge=2)
    retry_reason: Annotated[str, StringConstraints(max_length=512)] | None = None
    observation_due_at: datetime | None = None
    observation_deadline_at: datetime | None = None
    startup_budget_seconds: int | None = Field(default=None, ge=1)
    start_deadline: datetime | None = None
    failed_phase: RunSwitchPhaseKind | None = None
    final_verify_started_at: float | None = Field(default=None, ge=0)
    final_observation: RunSwitchPhaseResult | None = None

    @model_validator(mode="after")
    def total_bytes_state_is_consistent(self) -> RunSwitchOperationResult:
        if self.total_bytes_known != (self.total_bytes is not None):
            raise ValueError("operation total byte knowledge is inconsistent")
        if self.total_bytes is not None and self.completed_bytes > self.total_bytes:
            raise ValueError("operation completed bytes exceed total bytes")
        return self


class RunSwitchOperation(_StrictModel):
    schema_version: Literal[2] = 2
    operation_id: UuidId
    kind: RunSwitchOperationKind
    action: RunSwitchAction
    state: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    plan_digest: Digest
    request_key: UuidId
    cleanup_mode: Literal["uninstall", "reconcile"] | None = None
    installation_id: UuidId | None = None
    node_ids: list[NodeId] = Field(min_length=1, max_length=32)
    current_phase: RunSwitchPhaseKind | None = None
    completed_phases: list[RunSwitchPhaseKind] = Field(max_length=16)
    progress: RunSwitchProgress
    status_reason: Annotated[str, StringConstraints(max_length=512)] | None = None
    result: RunSwitchOperationResult | None = None

    @model_validator(mode="after")
    def terminal_evidence_is_consistent(self) -> RunSwitchOperation:
        if self.action == "cleanup":
            if self.cleanup_mode is None or self.installation_id is None:
                raise ValueError("cleanup operation requires its reviewed identity")
        elif self.cleanup_mode is not None or self.installation_id is not None:
            raise ValueError("non-cleanup operation cannot carry cleanup identity")
        if self.state == "succeeded":
            if self.result is None or not self.result.completed_phases:
                raise ValueError(
                    "succeeded run-switch requires completed phase evidence"
                )
            if self.status_reason is not None or self.result.failed_phase is not None:
                raise ValueError("succeeded run-switch cannot retain failure evidence")
            if self.result.retryable or self.result.child_operation_id is not None:
                raise ValueError(
                    "succeeded run-switch cannot retain pending recovery or child work"
                )
            if self.result.failure_code is not None:
                raise ValueError("succeeded run-switch cannot retain failure evidence")
        if self.state == "failed" and not (self.status_reason or "").strip():
            raise ValueError("failed run-switch requires a status reason")
        return self


class RunSwitchCancelRequest(_StrictModel):
    schema_version: Literal[2] = 2
    request_key: UuidId
    reason: Annotated[str, StringConstraints(min_length=1, max_length=512)]


class RunSwitchRetryRequest(_StrictModel):
    schema_version: Literal[2] = 2
    request_key: UuidId


__all__ = [
    "Alias",
    "ArtifactStorageImpact",
    "ArtifactVerificationEvidence",
    "BuildCompatibilityEvidence",
    "BuildSourceEvidence",
    "CapabilityEvidence",
    "Digest",
    "FreshnessEvidence",
    "InvocationMetadata",
    "MappingSelection",
    "RunSwitchAction",
    "RunSwitchApplyRequest",
    "RunSwitchAssessment",
    "RunSwitchBuildEvidence",
    "RunSwitchBuildEvidenceState",
    "RunSwitchCachedTransferResult",
    "RunSwitchCapabilityEvidenceState",
    "RunSwitchChangeEffect",
    "RunSwitchCleanupApplyRequest",
    "RunSwitchCleanupPreviewRequest",
    "RunSwitchCleanupResult",
    "RunSwitchContainerBuildResult",
    "RunSwitchContainerBuildState",
    "RunSwitchCoverage",
    "RunSwitchDistributionChildResult",
    "RunSwitchFinalVerifyResult",
    "RunSwitchInstallationVerifyResult",
    "RunSwitchMemberProgress",
    "RunSwitchMemberState",
    "RunSwitchModelDownloadPendingResult",
    "RunSwitchModelDownloadResult",
    "RunSwitchOperation",
    "RunSwitchOperationKind",
    "RunSwitchOperationResult",
    "RunSwitchPhase",
    "RunSwitchPhaseKind",
    "RunSwitchPhaseResult",
    "RunSwitchPlacementAction",
    "RunSwitchPlan",
    "RunSwitchPreparedResult",
    "RunSwitchPreviewRequest",
    "RunSwitchProgress",
    "RunSwitchProgressState",
    "RunSwitchReason",
    "RunSwitchReasonScope",
    "RunSwitchReasonSeverity",
    "RunSwitchReconciliationAuthority",
    "RunSwitchReconciliationTarget",
    "RunSwitchRetention",
    "RunSwitchRetryRequest",
    "RunSwitchRuntimeImageReferenceIntent",
    "RunSwitchRuntimeImageResult",
    "RunSwitchRuntimeInstallResult",
    "RunSwitchRuntimePlanResult",
    "RunSwitchStartResult",
    "RunSwitchStopApplyRequest",
    "RunSwitchStopPreviewRequest",
    "RunSwitchStopResult",
    "RunSwitchSubphase",
    "RunSwitchTargetTransferEvidenceResult",
    "RunSwitchTargetTransferResult",
    "RunSwitchVerifyResult",
    "RuntimeImageStorageImpact",
    "SparkFit",
    "SparkFitNode",
    "SparkGroup",
    "SparkGroupNode",
    "StopImpact",
    "UuidId",
]
