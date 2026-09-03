"""Bounded typed contract and deterministic display helpers for Library reads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

_UUID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_NODE_PATTERN = r"^spk_[0-9a-f]{32}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_IMAGE_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_MAX_PAGE_RECIPES = 100
_MAX_OPERATIONAL_ROWS = 512
_MAX_OPERATIONAL_MEMBERS = 16_384
_MAX_AGENT_ROWS = 500
_MAX_CANDIDATE_NODES = 32
_MAX_EXAMINED_GROUPS = 512
_MAX_RECOMMENDATIONS = 16
_MAX_REJECTED_NODES = 32
_MAX_REJECTED_GROUPS = 16
_MAX_NODE_ARTIFACTS_PER_NODE = 512
_MAX_PROJECTED_CAPABILITIES = 64
_MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807

UuidId = Annotated[str, StringConstraints(pattern=_UUID_PATTERN)]
NodeId = Annotated[str, StringConstraints(pattern=_NODE_PATTERN)]
Digest = Annotated[str, StringConstraints(pattern=_DIGEST_PATTERN)]
ImageDigest = Annotated[str, StringConstraints(pattern=_IMAGE_DIGEST_PATTERN)]
Text32 = Annotated[str, StringConstraints(min_length=1, max_length=32)]
Text64 = Annotated[str, StringConstraints(min_length=1, max_length=64)]
Text80 = Annotated[str, StringConstraints(min_length=1, max_length=80)]
Text128 = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Text200 = Annotated[str, StringConstraints(min_length=1, max_length=200)]
Text256 = Annotated[str, StringConstraints(min_length=1, max_length=256)]
Text512 = Annotated[str, StringConstraints(min_length=1, max_length=512)]
Scalar = str | int | bool
DisplayScalar = (
    Annotated[str, StringConstraints(max_length=512)]
    | Annotated[int, Field(ge=-_MAX_SIGNED_BIGINT, le=_MAX_SIGNED_BIGINT)]
    | bool
    | None
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ProjectionReason(_StrictModel):
    model_config = ConfigDict(title="LibraryProjectionReason")

    code: Text80
    detail: Text256
    severity: Literal["info", "warning", "error"]


class FreshnessPolicy(_StrictModel):
    inventory_fresh_seconds: int = Field(default=300, ge=1, le=3_600)
    telemetry_live_seconds: int = Field(default=6, ge=1, le=60)
    telemetry_delayed_seconds: int = Field(default=20, ge=1, le=300)


class RecipeRevisionSummary(_StrictModel):
    id: UuidId
    revision_number: int = Field(ge=1, le=2_147_483_647)
    lifecycle: Literal["draft", "blocked", "resolved", "deprecated"]
    schema_version: Literal[1] = 1
    content_sha256: Digest | None
    created_at: datetime


class LibraryRecipeIdentity(_StrictModel):
    recipe_id: UuidId
    slug: Text128
    title: Text200
    description: Annotated[str, StringConstraints(max_length=4_096)]
    source_kind: Literal["local", "workload_run", "global", "recipe_library"]


class LibraryInstallationSummary(_StrictModel):
    installation_id: UuidId
    recipe_revision_id: UuidId
    state: Literal["planned", "installing", "installed", "partial", "failed"]
    installed_rank_count: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    expected_rank_count: int = Field(ge=1, le=_MAX_SIGNED_BIGINT)
    complete: bool


class LibraryRunSummary(_StrictModel):
    run_id: UuidId
    installation_id: UuidId
    recipe_revision_id: UuidId
    state: Literal["planned", "starting", "running", "stopping"]
    route_state: Literal["withdrawn", "pending", "published", "failed"]
    healthy_rank_count: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    expected_rank_count: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    healthy: bool


class LibraryRecipeSummary(LibraryRecipeIdentity):
    selected_revision: RecipeRevisionSummary | None
    capabilities: list[Text64] = Field(max_length=64)
    topology_name: Text64 | None
    installations: list[LibraryInstallationSummary] = Field(max_length=64)
    installation_total_count: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    installation_returned_count: int = Field(ge=0, le=64)
    installations_truncated: bool
    runs: list[LibraryRunSummary] = Field(max_length=64)
    run_total_count: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    run_returned_count: int = Field(ge=0, le=64)
    runs_truncated: bool
    reasons: list[ProjectionReason] = Field(max_length=16)


class ModelVersionIdentity(_StrictModel):
    kind: Literal["model-version"]
    publisher: Text128
    slug: Text128
    content_sha256: Digest


class LibraryModel(_StrictModel):
    model: ModelVersionIdentity
    page_local: Literal[True] = True
    recipes: list[LibraryRecipeSummary] = Field(min_length=1, max_length=100)


class LibrarySnapshot(_StrictModel):
    schema_version: Literal[1] = 1
    generated_at: datetime
    models: list[LibraryModel] = Field(max_length=100)
    unlinked_recipes: list[LibraryRecipeSummary] = Field(max_length=100)
    next_cursor: Annotated[str, StringConstraints(max_length=1024)] | None
    freshness_policy: FreshnessPolicy


class VisualIdentity(_StrictModel):
    publisher: Text128
    slug: Text128


class VisualMetadata(_StrictModel):
    title: Text200
    description: Text512
    tags: list[Text64] = Field(max_length=64)


class VisualCatalogIdentity(_StrictModel):
    kind: Literal[
        "model-version",
        "execution-harness",
        "runtime-distribution",
        "patch-bundle",
    ]
    publisher: Text128
    slug: Text128
    content_sha256: Digest


class VisualExecution(_StrictModel):
    harness: VisualCatalogIdentity
    patch_bundle: VisualCatalogIdentity | None


class VisualBuildContext(_StrictModel):
    sha256: Digest
    expected_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    media_type: Text128


class VisualBuildOptionValue(_StrictModel):
    name: Text128
    value: str = Field(max_length=1024)


class VisualBuildAdditionalContext(_StrictModel):
    name: Text64
    path: Text256


class VisualBuildOptions(_StrictModel):
    additional_contexts: list[VisualBuildAdditionalContext] = Field(max_length=16)
    annotations: list[VisualBuildOptionValue] = Field(max_length=64)
    environment: list[VisualBuildOptionValue] = Field(max_length=64)
    format: Literal["oci", "docker"]
    identity_label: bool
    ignorefile: Text256 | None
    jobs: int = Field(ge=1, le=32)
    labels: list[VisualBuildOptionValue] = Field(max_length=64)
    layer_compression: Literal["disabled", "gzip"]
    layer_labels: list[VisualBuildOptionValue] = Field(max_length=64)
    layers: bool
    no_hostname: bool
    no_hosts: bool
    omit_history: bool
    os_features: list[Text64] = Field(max_length=32)
    os_version: Text64 | None
    shm_bytes: int = Field(ge=65_536, le=68_719_476_736)
    skip_unused_stages: bool
    squash: Literal["none", "new", "all"]
    timestamp: int | None = Field(default=None, ge=0, le=4_102_444_800)
    unset_environment: list[Text128] = Field(max_length=64)
    unset_labels: list[Text128] = Field(max_length=64)


class VisualBuild(_StrictModel):
    context: VisualBuildContext
    dockerfile: Text256
    target: Text64 | None
    platform: Text64
    network_mode: Text32
    network_hosts: list[Text256] = Field(max_length=64)
    capabilities: list[Text32] = Field(max_length=12)
    options: VisualBuildOptions
    cpu_cores: int = Field(ge=1, le=256)
    download_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    temporary_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    memory_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    processes: int = Field(ge=1, le=65_535)
    timeout_seconds: int = Field(ge=1, le=86_400)


class VisualArtifact(_StrictModel):
    id: Text64
    kind: Text64
    repository: Text256
    revision: Text128
    include_paths: list[Text512] = Field(max_length=256)
    download_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    installed_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    roles: list[Text64] = Field(max_length=64)


class VisualRuntime(_StrictModel):
    distribution: VisualCatalogIdentity
    entrypoint: list[Text256] = Field(max_length=64)
    lifecycle_pre_start_count: int = Field(ge=0, le=64)
    lifecycle_post_stop_count: int = Field(ge=0, le=64)
    stop_timeout_seconds: int = Field(ge=1, le=86_400)


class VisualRecipeParameter(_StrictModel):
    name: Text64
    description: Text512
    type: Literal["string", "integer", "boolean", "enum"]
    default: DisplayScalar
    minimum: int | None = Field(
        default=None,
        ge=-_MAX_SIGNED_BIGINT,
        le=_MAX_SIGNED_BIGINT,
    )
    maximum: int | None = Field(
        default=None,
        ge=-_MAX_SIGNED_BIGINT,
        le=_MAX_SIGNED_BIGINT,
    )
    allowed_values: list[DisplayScalar] = Field(default_factory=list, max_length=128)
    pattern: Annotated[str, StringConstraints(max_length=256)] | None = None
    change_effect: Literal["rebuild", "reinstall", "restart"]


class VisualInputSlot(_StrictModel):
    id: Annotated[
        str,
        StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,31}$"),
    ]
    label: Text64
    description: Text256
    media_types: list[Text128] = Field(min_length=1, max_length=16)
    extensions: list[
        Annotated[
            str,
            StringConstraints(pattern=r"^\.[a-z0-9][a-z0-9._-]{0,15}$"),
        ]
    ] = Field(max_length=16)
    min_files: int = Field(ge=0, le=32)
    max_files: int = Field(ge=1, le=32)
    max_file_bytes: int = Field(ge=1, le=512 * 1024**2)
    max_total_bytes: int = Field(ge=1, le=1024**3)


class VisualInterfaceInput(_StrictModel):
    path: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    required: bool
    media_types: list[Text128] = Field(min_length=1, max_length=16)
    max_bytes: int = Field(ge=1, le=_MAX_SIGNED_BIGINT)
    min_files: int = Field(ge=0, le=32)
    max_files: int = Field(ge=1, le=32)
    slots: list[VisualInputSlot] = Field(default_factory=list, max_length=32)


class VisualOutputSlot(_StrictModel):
    id: Annotated[
        str,
        StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,31}$"),
    ]
    label: Text64
    description: Text256
    media_types: list[Text128] = Field(min_length=1, max_length=16)
    extensions: list[
        Annotated[
            str,
            StringConstraints(pattern=r"^\.[a-z0-9][a-z0-9._-]{0,15}$"),
        ]
    ] = Field(min_length=1, max_length=16)
    min_files: int = Field(ge=0, le=32)
    max_files: int = Field(ge=1, le=32)
    max_file_bytes: int = Field(ge=1, le=1024**3)
    max_total_bytes: int = Field(ge=1, le=2 * 1024**3)


class VisualInterfaceOutput(_StrictModel):
    path: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    allowed_media_types: list[Text128] = Field(max_length=16)
    max_total_bytes: int | None = Field(default=None, ge=1, le=2 * 1024**3)
    slots: list[VisualOutputSlot] = Field(default_factory=list, max_length=32)


class VisualInterface(_StrictModel):
    adapter: Text64
    port: int | None = Field(default=None, ge=1, le=65_535)
    model_aliases: list[Text128] = Field(default_factory=list, max_length=64)
    health_path: (
        Annotated[str, StringConstraints(min_length=1, max_length=512)] | None
    ) = None
    path: Annotated[str, StringConstraints(min_length=1, max_length=512)] | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=3_600)
    input: VisualInterfaceInput | None = None
    output: VisualInterfaceOutput | None = None


class VisualTerritorialRestrictions(_StrictModel):
    denied_jurisdictions: list[
        Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]
    ] = Field(min_length=1, max_length=32)
    notice: Annotated[str, StringConstraints(min_length=1, max_length=1_000)]


class VisualModelLicense(_StrictModel):
    territorial_restrictions: VisualTerritorialRestrictions | None = None


class VisualValidation(_StrictModel):
    checks: list[Text80] = Field(min_length=1, max_length=64)
    benchmark_count: int = Field(ge=0, le=32)


class VisualProvenance(_StrictModel):
    source_kind: Literal["local", "workload_run", "global", "fork"]
    source_reference: Annotated[str, StringConstraints(max_length=2_048)] | None
    attribution: list[Text512] = Field(max_length=32)


class VisualRecipeDocument(_StrictModel):
    schema_version: Literal[1]
    identity: VisualIdentity
    metadata: VisualMetadata
    model: VisualCatalogIdentity
    model_license: VisualModelLicense | None
    execution: VisualExecution
    build: VisualBuild
    parameters: list[VisualRecipeParameter] = Field(max_length=128)
    artifacts: list[VisualArtifact] = Field(max_length=128)
    runtime: VisualRuntime
    interfaces: list[VisualInterface] = Field(max_length=64)
    validation: VisualValidation
    provenance: VisualProvenance


class RecipeDiskRequirements(_StrictModel):
    image_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    artifact_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    staging_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    cache_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    rollback_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    safety_margin_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)


class RecipeMemoryRequirements(_StrictModel):
    kind: Literal["unified", "host", "accelerator"]
    startup_peak_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    steady_state_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    runtime_growth_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    system_reserve_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)


class RecipeRole(_StrictModel):
    name: Text64
    count: int = Field(ge=1, le=_MAX_SIGNED_BIGINT)
    endpoint_owner: bool
    artifacts: list[Text64] = Field(max_length=128)
    disk: RecipeDiskRequirements
    memory: RecipeMemoryRequirements


class RecipeFabric(_StrictModel):
    connectivity: Literal["none", "connected", "full_mesh", "switch"]
    minimum_bandwidth_mbps: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)


class RecipeParallelism(_StrictModel):
    tensor: int = Field(ge=1, le=_MAX_SIGNED_BIGINT)
    pipeline: int = Field(ge=1, le=_MAX_SIGNED_BIGINT)
    data: int = Field(ge=1, le=_MAX_SIGNED_BIGINT)
    backend: Text64


class RecipeTopology(_StrictModel):
    name: Text64
    mode: Text64
    node_count: int = Field(ge=1, le=_MAX_SIGNED_BIGINT)
    parallelism: RecipeParallelism
    roles: list[RecipeRole] = Field(min_length=1, max_length=32)
    fabric: RecipeFabric
    start_order: list[Text64] = Field(max_length=32)
    stop_order: list[Text64] = Field(max_length=32)


class OperationalBuild(_StrictModel):
    recipe_build_id: UuidId
    recipe_revision_id: UuidId
    state: Literal["planned", "building", "succeeded", "failed"]
    image_digest: ImageDigest | None
    image_bytes: int | None = Field(default=None, ge=1, le=_MAX_SIGNED_BIGINT)


class OperationalMappingNode(_StrictModel):
    node_id: NodeId
    rank: int = Field(ge=0, le=_MAX_CANDIDATE_NODES - 1)
    role: Text64
    endpoint_owner: bool


class OperationalMapping(_StrictModel):
    mapping_id: UuidId
    recipe_revision_id: UuidId
    topology_name: Text64
    generation: int = Field(ge=1, le=2_147_483_647)
    state: Literal["planned", "ready", "stale"]
    nodes: list[OperationalMappingNode] = Field(max_length=32)


class OperationalInstallation(_StrictModel):
    installation_id: UuidId
    recipe_revision_id: UuidId
    mapping_id: UuidId
    recipe_build_id: UuidId
    state: Literal[
        "planned", "installing", "installed", "partial", "failed", "uninstalled"
    ]
    node_ids: list[NodeId] = Field(max_length=32)


class OperationalRun(_StrictModel):
    run_id: UuidId
    installation_id: UuidId
    mapping_id: UuidId
    recipe_revision_id: UuidId
    state: Literal[
        "planned", "starting", "running", "stopping", "stopped", "failed", "lost"
    ]
    route_state: Literal["withdrawn", "pending", "published", "failed"]
    node_ids: list[NodeId] = Field(max_length=32)


class OperationalState(_StrictModel):
    builds: list[OperationalBuild] = Field(max_length=_MAX_OPERATIONAL_ROWS)
    mappings: list[OperationalMapping] = Field(max_length=_MAX_OPERATIONAL_ROWS)
    installations: list[OperationalInstallation] = Field(
        max_length=_MAX_OPERATIONAL_ROWS
    )
    runs: list[OperationalRun] = Field(max_length=_MAX_OPERATIONAL_ROWS)


class MappingPreviewInput(_StrictModel):
    recipe_revision_id: UuidId
    node_ids: list[NodeId] = Field(min_length=1, max_length=32)
    parameters: dict[Text64, Scalar] = Field(max_length=128)


class BuildPreviewInput(_StrictModel):
    recipe_revision_id: UuidId
    builder_node_id: NodeId


class ImageDistributionPreviewInput(_StrictModel):
    recipe_build_id: UuidId
    mapping_id: UuidId
    mapping_generation: int = Field(ge=1, le=2_147_483_647)


class InstallPreviewInput(_StrictModel):
    mapping_id: UuidId
    recipe_build_id: UuidId


class RunPreviewInput(_StrictModel):
    installation_id: UuidId


class MappingPreviewTarget(_StrictModel):
    kind: Literal["mapping"] = "mapping"
    input: MappingPreviewInput


class BuildPreviewTarget(_StrictModel):
    kind: Literal["build"] = "build"
    input: BuildPreviewInput


class ImageDistributionPreviewTarget(_StrictModel):
    kind: Literal["image_distribution"] = "image_distribution"
    input: ImageDistributionPreviewInput


class InstallPreviewTarget(_StrictModel):
    kind: Literal["install"] = "install"
    input: InstallPreviewInput


class RunPreviewTarget(_StrictModel):
    kind: Literal["run"] = "run"
    input: RunPreviewInput


PreviewTarget = Annotated[
    BuildPreviewTarget
    | MappingPreviewTarget
    | ImageDistributionPreviewTarget
    | InstallPreviewTarget
    | RunPreviewTarget,
    Field(discriminator="kind"),
]


class PlacementLimits(_StrictModel):
    candidate_node_limit: Literal[32] = _MAX_CANDIDATE_NODES
    examined_group_limit: Literal[512] = _MAX_EXAMINED_GROUPS
    recommendation_limit: Literal[16] = _MAX_RECOMMENDATIONS
    rejected_node_evidence_limit: Literal[32] = _MAX_REJECTED_NODES
    rejected_group_evidence_limit: Literal[16] = _MAX_REJECTED_GROUPS
    artifact_evidence_per_node_limit: Literal[512] = _MAX_NODE_ARTIFACTS_PER_NODE
    operational_row_evidence_limit: Literal[512] = _MAX_OPERATIONAL_ROWS
    operational_member_evidence_limit: Literal[16384] = _MAX_OPERATIONAL_MEMBERS


EvidenceCollection = Literal[
    "builds",
    "mappings",
    "mapping_members",
    "installations",
    "installation_members",
    "runs",
    "run_members",
]


class PlacementEvidenceCounts(_StrictModel):
    builds: int = Field(ge=0, le=_MAX_OPERATIONAL_ROWS + 1)
    mappings: int = Field(ge=0, le=_MAX_OPERATIONAL_ROWS + 1)
    mapping_members: int = Field(ge=0, le=_MAX_OPERATIONAL_MEMBERS + 1)
    installations: int = Field(ge=0, le=_MAX_OPERATIONAL_ROWS + 1)
    installation_members: int = Field(ge=0, le=_MAX_OPERATIONAL_MEMBERS + 1)
    runs: int = Field(ge=0, le=_MAX_OPERATIONAL_ROWS + 1)
    run_members: int = Field(ge=0, le=_MAX_OPERATIONAL_MEMBERS + 1)
    truncated_collections: list[EvidenceCollection] = Field(max_length=7)


class PlacementNode(_StrictModel):
    node_id: NodeId
    rank: int = Field(ge=0, le=_MAX_CANDIDATE_NODES - 1)
    role: Text64
    endpoint_owner: bool
    inventory_observed_at: datetime
    telemetry_observed_at: datetime
    inventory_age_seconds: float = Field(ge=0, le=float(_MAX_SIGNED_BIGINT))
    telemetry_age_seconds: float = Field(ge=0, le=float(_MAX_SIGNED_BIGINT))
    disk_free_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    disk_reserved_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    disk_required_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    disk_free_after_bytes: int = Field(ge=-_MAX_SIGNED_BIGINT, le=_MAX_SIGNED_BIGINT)
    memory_kind: Literal["unified", "host", "accelerator"]
    memory_available_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    memory_reserved_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    memory_required_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    memory_free_after_bytes: int = Field(ge=-_MAX_SIGNED_BIGINT, le=_MAX_SIGNED_BIGINT)
    artifact_reuse_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    fabric_address: Annotated[str, StringConstraints(max_length=45)] | None
    fabric_bandwidth_mbps: int | None = Field(default=None, ge=1, le=_MAX_SIGNED_BIGINT)


class PlacementScore(_StrictModel):
    exact_install_complete: bool
    exact_install_partial: bool
    active_run_count: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    artifact_reuse_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    minimum_disk_headroom_bytes: int = Field(
        ge=-_MAX_SIGNED_BIGINT, le=_MAX_SIGNED_BIGINT
    )
    minimum_memory_headroom_bytes: int = Field(
        ge=-_MAX_SIGNED_BIGINT, le=_MAX_SIGNED_BIGINT
    )
    maximum_telemetry_age_seconds: float = Field(ge=0, le=float(_MAX_SIGNED_BIGINT))


class PlacementRecommendation(_StrictModel):
    recipe_revision_id: UuidId
    topology_name: Text64
    node_ids: list[NodeId] = Field(min_length=1, max_length=32)
    nodes: list[PlacementNode] = Field(min_length=1, max_length=32)
    group_complete: Literal[True] = True
    eligible: bool
    ranking_scope: Literal["bounded-advisory"] = "bounded-advisory"
    score: PlacementScore
    install_state: Literal["complete", "partial", "not_present", "unknown"]
    load_state: Literal["loaded", "not_loaded", "unknown"]
    mapping_id: UuidId | None
    recipe_build_id: UuidId | None
    installation_ids: list[UuidId] = Field(max_length=16)
    run_ids: list[UuidId] = Field(max_length=16)
    preview_targets: list[PreviewTarget] = Field(max_length=5)
    reasons: list[ProjectionReason] = Field(max_length=64)


class RejectedNode(_StrictModel):
    node_id: NodeId
    reasons: list[ProjectionReason] = Field(min_length=1, max_length=16)


class TopologyPlacement(_StrictModel):
    topology_name: Text64
    node_count: int = Field(ge=1, le=_MAX_SIGNED_BIGINT)
    candidate_node_ids: list[NodeId] = Field(max_length=32)
    recommendations: list[PlacementRecommendation] = Field(max_length=16)
    rejected_nodes: list[RejectedNode] = Field(max_length=32)
    rejected_groups: list[PlacementRecommendation] = Field(max_length=16)
    evaluated_group_count: int = Field(ge=0, le=512)
    search_complete: bool
    rejected_evidence_truncated: bool
    limits: PlacementLimits
    evidence_counts: PlacementEvidenceCounts
    reasons: list[ProjectionReason] = Field(max_length=16)


class LibraryRecipeDetail(_StrictModel):
    schema_version: Literal[1] = 1
    generated_at: datetime
    recipe: LibraryRecipeIdentity
    selected_revision: RecipeRevisionSummary | None
    visual_recipe: VisualRecipeDocument | None
    topology: RecipeTopology | None
    operational_state: OperationalState
    placement: list[TopologyPlacement] = Field(max_length=1)
    reasons: list[ProjectionReason] = Field(max_length=16)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _bounded_detail(value: object) -> str:
    detail = " ".join(str(value).split())
    return (detail or "Projection evidence is unavailable.")[:256]


def _bounded_text(value: object, maximum_length: int) -> str:
    """Return deterministic response copy without rejecting valid stored text."""

    return str(value)[:maximum_length]


def _saturating_nonnegative(value: object) -> int:
    """Project a schema-valid nonnegative integer into signed-bigint DTO space."""

    return min(max(int(value), 0), _MAX_SIGNED_BIGINT)


def _saturating_nonnegative_sum(*values: object) -> int:
    """Add nonnegative inputs without allowing a bounded DTO overflow."""

    total = 0
    for value in values:
        total = min(
            _MAX_SIGNED_BIGINT,
            total + _saturating_nonnegative(value),
        )
    return total


def _saturating_headroom(available: object, *required: object) -> int:
    """Subtract bounded capacity evidence and clamp to signed-bigint DTO space."""

    value = _saturating_nonnegative(available) - sum(
        _saturating_nonnegative(item) for item in required
    )
    return max(-_MAX_SIGNED_BIGINT, min(value, _MAX_SIGNED_BIGINT))


def _numeric_truncation_count(value: object) -> int:
    """Count integers outside the public signed-bigint display bound."""

    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return int(not -_MAX_SIGNED_BIGINT <= value <= _MAX_SIGNED_BIGINT)
    if isinstance(value, Mapping):
        return sum(_numeric_truncation_count(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return sum(_numeric_truncation_count(item) for item in value)
    return 0


def _display_scalar_truncation_count(value: object) -> int:
    if isinstance(value, str):
        return int(len(value) > 512)
    if isinstance(value, Mapping):
        return sum(_display_scalar_truncation_count(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return sum(_display_scalar_truncation_count(item) for item in value)
    return 0


def _bounded_display_scalar(value: Scalar | None) -> DisplayScalar:
    """Bound display-only scalars without feeding them back into actions."""

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value[:512]
    return max(-_MAX_SIGNED_BIGINT, min(value, _MAX_SIGNED_BIGINT))


def _reason(code: str, detail: str, severity: str = "warning") -> ProjectionReason:
    return ProjectionReason(
        code=code, detail=_bounded_detail(detail), severity=severity
    )


def _bounded_reasons(
    reasons: Sequence[ProjectionReason], maximum: int
) -> list[ProjectionReason]:
    """Dedupe, severity-sort, and cap evidence with an observable marker."""

    severity_order = {"error": 0, "warning": 1, "info": 2}
    unique = {
        (reason.severity, reason.code, reason.detail): reason for reason in reasons
    }
    ordered = sorted(
        unique.values(),
        key=lambda reason: (
            severity_order[reason.severity],
            reason.code,
            reason.detail,
        ),
    )
    if len(ordered) <= maximum:
        return ordered
    marker = _reason(
        "projection.reasons_truncated",
        f"Projection produced {len(ordered)} distinct reasons; returning {maximum}, including this truncation marker.",
        "warning",
    )
    available = maximum - 1
    representatives: dict[tuple[str, str], ProjectionReason] = {}
    for reason in ordered:
        representatives.setdefault((reason.severity, reason.code), reason)
    selected = list(representatives.values())[:available]
    selected_keys = {
        (reason.severity, reason.code, reason.detail) for reason in selected
    }
    for reason in ordered:
        key = (reason.severity, reason.code, reason.detail)
        if len(selected) == available:
            break
        if key not in selected_keys:
            selected.append(reason)
            selected_keys.add(key)
    selected.append(marker)
    return sorted(
        selected,
        key=lambda reason: (
            severity_order[reason.severity],
            reason.code,
            reason.detail,
        ),
    )
