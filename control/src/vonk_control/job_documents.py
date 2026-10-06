"""The documents a generic ``Job`` row stores, one contract per job kind.

``jobs.payload`` and ``jobs.result`` hold a different document for each ``kind``
(a recipe operation parent, a Run/Switch operation, a cache update batch, an
availability operation, an agent upgrade rollout ...).  This module defines the
documents that have no other home; the ones a feature already owns
(``RunSwitchOperationResult``, ``RecipeUpdateDocument`` ...) are bound to their
kind in :mod:`vonk_control.stored_columns`.

A kind nothing registers (the generic ``JobService`` queue, which no production
code enqueues into) keeps its document opaque: see :class:`GenericJobDocument`.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, ConfigDict, Field, JsonValue, StringConstraints
from vonk_agent_protocol import (
    OperationProgress,
    RecipeBuildCleanupRequest,
    RecipeInstallPayload,
    RecipeReconcilePayload,
    RecipeStartPayload,
    RecipeStopPayload,
    RecipeUninstallPayload,
)
from vonk_agent_protocol.contracts import RecipeBuildRequest
from vonk_agent_protocol.package_source import AgentPackageSource
from vonk_agent_protocol.recipe_jobs import RecipeJobRunRequest
from vonk_forge_contracts import RecipeDefinition

from .distribution_assignment import NodeDistributionAssignment
from .mapping_parameters import EngineArgumentValue
from .operation_blockers import OperationBlocker
from .operation_contract import AvailabilityOperationFailure
from .recipe_availability_intent import RecipeAvailabilityIntent, RecipeBuildDependency
from .recipe_build_cancellation import RecipeBuildIntent
from .recipe_image_availability_api import (
    RecipeImageAvailabilityArtifact,
    RecipeImageAvailabilityState,
)
from .recipe_lifecycle_contract import RecipeOperationCancellationResult
from .run_switch_contract import (
    RunSwitchAction,
    RunSwitchApplyRequest,
    RunSwitchCleanupApplyRequest,
    RunSwitchMemberReceipt,
    RunSwitchOperationResult,
    RunSwitchPhaseKind,
    RunSwitchPlan,
    RunSwitchProfileStopScope,
    RunSwitchReconciliationAuthority,
    RunSwitchStopApplyRequest,
)
from .runtime_image_preparation import RuntimeImageReceipt, RuntimeImageReferenceIntent
from .stored_json import ExternalPassthrough
from .strict_json import StrictJSONModel

UuidText = Annotated[
    str,
    StringConstraints(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
]
NodeText = Annotated[str, StringConstraints(min_length=1, max_length=128)]
DigestText = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

#: A kind with no registered contract.
GenericJobDocument = Annotated[
    JsonValue,
    ExternalPassthrough(
        "The generic job queue (JobService) is kind-agnostic: the handler a "
        "worker registers for a kind defines its document, and the Controller "
        "registers none, so a row of an unregistered kind keeps its document "
        "verbatim and reads as unknown."
    ),
]


class _Document(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


# ------------------------------------------------- recipe operation parents


class _PhaseOperation(_Document):
    """One agent order a parent queued, in the phase it runs."""

    operation_id: UuidText
    node_id: NodeText


class InstallPhaseOperation(_PhaseOperation):
    payload: RecipeInstallPayload


class StartPhaseOperation(_PhaseOperation):
    payload: RecipeStartPayload


class StopPhaseOperation(_PhaseOperation):
    payload: RecipeStopPayload


class UninstallPhaseOperation(_PhaseOperation):
    payload: RecipeUninstallPayload


class ReconcilePhaseOperation(_PhaseOperation):
    payload: RecipeReconcilePayload


class JobRunPhaseOperation(_PhaseOperation):
    payload: RecipeJobRunRequest


class BuildPhaseOperation(_PhaseOperation):
    payload: RecipeBuildRequest


class BuildCleanupPhaseOperation(_PhaseOperation):
    payload: RecipeBuildCleanupRequest


class RecoveryStartItem(_Document):
    node_id: NodeText
    payload: RecipeStartPayload


class DistributedRecoveryMarker(_Document):
    """What a recovery Stop remembers so the Start it interrupted can be re-issued."""

    schema_version: Literal[1]
    failed_rank: int = Field(ge=0)
    deadline: AwareDatetime
    start_phases: list[list[RecoveryStartItem]] | None = None


class _RecipeParent(_Document):
    schema_version: Literal[1]
    owner_kind: Literal["installation", "run", "recipe-build", "artifact-job"]
    owner_id: str = Field(min_length=1, max_length=128)
    plan_digest: DigestText
    workload_intent_ordinal: int | None = Field(default=None, ge=1)
    execution_mode: Literal["one-shot-jobs"] | None = None


class RecipeInstallParent(_RecipeParent):
    phases: list[list[InstallPhaseOperation]] | None = None


class RecipeStartParent(_RecipeParent):
    phases: list[list[StartPhaseOperation]] | None = None
    start_deadline: AwareDatetime | None = None
    start_anchored_at: AwareDatetime | None = None
    recovery: DistributedRecoveryMarker | None = None


class ProfilePartialStop(_Document):
    """The reachable ranks a profile Stop reaches and the ranks it cannot."""

    target_node_ids: list[NodeText] = Field(min_length=1, max_length=32)
    missing_node_ids: list[NodeText] = Field(min_length=1, max_length=31)


class RecipeStopParent(_RecipeParent):
    phases: list[list[StopPhaseOperation]] | None = None
    recovery: DistributedRecoveryMarker | None = None
    profile_partial_stop: ProfilePartialStop | None = None


class RecipeUninstallParent(_RecipeParent):
    phases: list[list[UninstallPhaseOperation]] | None = None


class RecipeReconcileParent(_RecipeParent):
    phases: list[list[ReconcilePhaseOperation]] | None = None
    reconciliation_authority: RunSwitchReconciliationAuthority | None = None


class RecipeJobRunParent(_RecipeParent):
    phases: list[list[JobRunPhaseOperation]] | None = None


class RecipeJobActivateParent(_RecipeParent):
    pass


class RecipeBuildParent(_RecipeParent):
    phases: list[list[BuildPhaseOperation]] | None = None
    build_intent: RecipeBuildIntent | None = None
    force_rebuild: bool | None = None
    prebuilt_image: str | None = Field(default=None, min_length=1, max_length=512)
    prebuilt_node_id: NodeText | None = None
    # The claim a Controller process holds on a prebuilt pull, renewed on a lease.
    prebuilt_claim_owner: str | None = Field(default=None, min_length=1, max_length=256)
    prebuilt_claim_until: str | None = Field(default=None, min_length=1, max_length=64)


class RecipeBuildCleanupParent(_RecipeParent):
    phases: list[list[BuildCleanupPhaseOperation]] | None = None
    build_cancellation: RecipeOperationCancellationResult | None = None


# --------------------------------------------------------- Run/Switch parents


class RunSwitchRunIntent(_Document):
    type: Literal["run"]
    request: RunSwitchApplyRequest


class RunSwitchStopIntent(RunSwitchStopApplyRequest):
    type: Literal["stop"]


class RunSwitchCleanupIntent(RunSwitchCleanupApplyRequest):
    type: Literal["cleanup"]


class RunSwitchProfileStopIntent(_Document):
    type: Literal["profile-stop"]
    run_id: UuidText
    profile_stop_scope: RunSwitchProfileStopScope


RunSwitchIntent = Annotated[
    RunSwitchRunIntent
    | RunSwitchStopIntent
    | RunSwitchCleanupIntent
    | RunSwitchProfileStopIntent,
    Field(discriminator="type"),
]


class RunSwitchJobPayload(_Document):
    """The reviewed plan, the request that asked for it and the live progress."""

    schema_version: Literal[2]
    operation_kind: Literal[
        "recipe.run-switch.v2", "recipe.stop.v2", "recipe.cleanup.v2"
    ]
    action: RunSwitchAction
    plan_digest: DigestText
    plan: RunSwitchPlan
    intent: RunSwitchIntent | None = None
    progress: RunSwitchOperationResult
    workload_intent_ordinal: int | None = Field(default=None, ge=1)
    retry_of: UuidText | None = None


# ---------------------------------------------- image availability operation


class RuntimeTelemetryProjection(_Document):
    engine: str
    engine_version: str | None = None
    metrics_format: str | None = None
    metrics_path: str | None = None


class RuntimeArgument(_Document):
    name: str = Field(min_length=1, max_length=64)
    value: EngineArgumentValue | None = None
    setting: str | None = None


class RuntimeEnvironmentEntry(_Document):
    name: str = Field(min_length=1, max_length=128)
    value: str | int | bool | float


class WritablePath(_Document):
    name: str
    path: str
    persistent: bool


class AvailabilityRuntime(_Document):
    """The runtime projection an availability operation prepares an image for.

    The compiled adapter fields appear once the runtime is resolved; an
    operation that only reuses a cached image carries the identity subset.
    """

    interface: str
    architecture: str
    adapter: str | None = None
    adapter_version: int | None = None
    telemetry: RuntimeTelemetryProjection | None = None
    image: str | None = None
    entrypoint: list[str] | None = None
    arguments: list[RuntimeArgument] | None = None
    environment: list[RuntimeEnvironmentEntry] | None = None
    writable_paths: list[WritablePath] | None = None
    placement_environment: dict[str, str] | None = None
    image_bytes: int | None = Field(default=None, ge=0)
    build_input_sha256: DigestText | None = None
    input_intent_sha256: DigestText | None = None
    builder_node_id: NodeText | None = None
    recipe_revision_id: str | None = Field(default=None, min_length=1, max_length=128)


class AvailabilityModelChild(_Document):
    """The model-cache child an availability operation waits on, as last seen."""

    id: UuidText
    state: RecipeImageAvailabilityState
    request_key: UuidText | None = None
    artifact_set_sha256: DigestText | None = None
    plan_digest: DigestText | None = None
    model_content_digests: list[DigestText] | None = None
    artifacts: list[RecipeImageAvailabilityArtifact] | None = None
    progress: OperationProgress | None = None
    failure: AvailabilityOperationFailure | None = None


class AvailabilityRetry(_Document):
    automatic_attempts: int = Field(ge=0)
    operator_retries: int = Field(ge=0)


class AvailabilitySupersession(_Document):
    code: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,95}$")
    recipe_revision_id: str = Field(min_length=1, max_length=128)
    superseded_at: AwareDatetime


class AvailabilityJobPayload(_Document):
    """One image-availability operation: intent, identity, progress and what it holds."""

    schema_version: Literal[2]
    kind: Literal["recipe.image.availability.v2"]
    request: RecipeAvailabilityIntent
    recipe_revision_id: str = Field(min_length=1, max_length=128)
    recipe_content_sha256: DigestText
    effective_execution_key: DigestText | None = None
    model_digest: str | None = None
    build_input_sha256: DigestText | None = None
    identity_key: DigestText | None = None
    recipe: RecipeDefinition
    runtime: AvailabilityRuntime
    force_rebuild: bool
    progress: OperationProgress
    retry: AvailabilityRetry
    claim_owner: str | None = Field(default=None, max_length=128)
    claim_until: AwareDatetime | None = None
    stage: Literal["available"] | None = None
    image_result: RuntimeImageReceipt | None = None
    image_reference_intent: RuntimeImageReferenceIntent | None = None
    model_child: AvailabilityModelChild | None = None
    failure: AvailabilityOperationFailure | None = None
    blockers: list[OperationBlocker] | None = Field(default=None, max_length=16)
    retry_after_at: AwareDatetime | None = None
    build_dependency: RecipeBuildDependency | None = None
    cancellation: RecipeOperationCancellationResult | None = None
    prebuilt_pull: bool | None = None
    removal_fence: UuidText | None = None
    removal_archives: list[DigestText] | None = None
    supersession: AvailabilitySupersession | None = None


class AvailabilityJobResult(_Document):
    """The image an availability operation produced, with its model child."""

    schema_version: Literal[2]
    recipe_content_sha256: DigestText
    model_digest: str | None = None
    build_input_sha256: DigestText | None = None
    image_digest: str = Field(min_length=1, max_length=256)
    local_image_config_id: str | None = None
    oci_archive_sha256: DigestText
    image_bytes: int = Field(ge=1)
    build_id: str | None = None
    model_child: AvailabilityModelChild | None = None


# --------------------------------------------------------- agent upgrade rollout

_UPGRADE_URL = (
    r"^https://install\.vonkforge\.ai/[A-Za-z0-9._~!$&'()*+,;=:%/-]{1,1900}"
    r"/vonk-forge-agent\.deb$"
)


class AgentUpgradePackage(_Document):
    """The signed package a rollout installs on every Spark it targets."""

    architecture: Literal["linux-arm64"]
    package_bytes: int = Field(ge=1, le=1024**3)
    package_sha256: DigestText
    package_signature: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{128}$")]
    package_url: Annotated[str, StringConstraints(pattern=_UPGRADE_URL)]
    package_version: Annotated[
        str, StringConstraints(pattern=r"^[0-9A-Za-z][0-9A-Za-z.+~-]{0,127}$")
    ]
    schema_version: Literal[1]
    target_binary_digest: DigestText
    target_build_digest: Annotated[
        str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")
    ]


class AgentUpgradeRepairManifest(_Document):
    """The repair capsule's authority, bound to one Spark and the package."""

    authority_sha256: DigestText
    kind: Literal["agent-upgrade-repair"]
    node_id: Annotated[str, StringConstraints(pattern=r"^spk_[0-9a-f]{32}$")]
    package: AgentUpgradePackage
    schema_version: Literal[2]


class AgentUpgradeRequestIntent(_Document):
    """Which Sparks the operator asked for: all of them, or an explicit list."""

    all: bool
    selectors: list[Annotated[str, StringConstraints(min_length=1)]] | None = Field(
        default=None, max_length=64
    )


class AgentUpgradeRolloutPayload(_Document):
    """An upgrade rollout: the package, the order, and each Spark's rollback source."""

    node_order: list[NodeText] = Field(min_length=1, max_length=64)
    package: AgentUpgradePackage
    request_intent: AgentUpgradeRequestIntent
    sources: dict[NodeText, AgentPackageSource]
    repair_manifest: AgentUpgradeRepairManifest | None = None


class AgentUpgradeRolloutResult(_Document):
    """What a rollout skipped, and the newer rollout that replaced it."""

    skipped: dict[NodeText, str] | None = None
    superseded_by: UuidText | None = None


# --------------------------------------------------------- artifact distribution


class DistributionTransferProgress(_Document):
    phase: RunSwitchPhaseKind
    completed_bytes: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    total_bytes_known: bool
    members: list[RunSwitchMemberReceipt]


class DistributionJobPayload(_Document):
    """One target-copy child: the plan it serves and what each Spark must receive."""

    plan_digest: DigestText
    workload_intent_ordinal: int | None = Field(default=None, ge=1)
    phase: RunSwitchPhaseKind
    progress: DistributionTransferProgress
    cached_nodes: list[NodeText]
    target_order: list[NodeText]
    target_totals: dict[NodeText, int]
    assignments: dict[NodeText, NodeDistributionAssignment]


class EmptyJobResult(_Document):
    """A kind that records its outcome elsewhere stores no result document."""
