"""Which contract owns the document in every JSON column of ``models.py``.

``bind`` is the only place a JSON column gets its contract; a column with no
binding fails ``control/tests/test_json_column_contracts.py``.  A table that
stores several document families binds one contract per value of its
``kind`` column.  The mechanism (reading, writing, the write guard and the
structural walker) is :mod:`vonk_control.stored_json`.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field
from vonk_agent_protocol import CacheReferenceReason, OperationProgress
from vonk_agent_protocol.contracts import (
    PAYLOAD_MODELS,
    AgentResultPayload,
    RecipeBuildRequest,
)
from vonk_agent_protocol.distribution import DistributionObject
from vonk_agent_protocol.inventory import Capability, NetworkInterface
from vonk_agent_protocol.job_inputs import RecipeJobInputManifest
from vonk_agent_protocol.recipe_jobs import RecipeJobRunResult
from vonk_agent_protocol.route_activation import ActivationMarker
from vonk_agent_protocol.runtime_preflight import RuntimePreflightRequest
from vonk_agent_protocol.source_bundles import SourceBundleManifest
from vonk_forge_contracts import ModelDefinition, RecipeDefinition

from .agent_upgrade_contract import (
    AgentUpgradeRolloutPayload,
    AgentUpgradeRolloutResult,
)
from .artifact_job_evidence import ArtifactJobResultEvidence
from .ca_issuance_contract import CertificateIssuanceBinding
from .catalog_revision_contract import (
    ModelRevisionProjection,
    RecipeRevisionProjection,
    UnprojectedRevision,
)
from .catalog_sync_contract import ManagedCatalogSyncResult
from .compiled_artifact_contract import (
    ArtifactOutputLimits,
    CompiledArtifactContract,
    ParameterScalar,
)
from .fleet_event_contract import (
    AgentOperationPayload,
    InstallationNodePayload,
    JobPayload,
    NodeProfilePayload,
    NodeTelemetryPayload,
    RecipeInstallationPayload,
    RecipeRunPayload,
    RunNodePayload,
)
from .fleet_profile_contract import (
    FleetProfileApplicationProgress,
    FleetProfileApplicationResult,
    FleetProfileAssignmentInput,
    FleetProfilePreview,
    LabelName,
    LabelValue,
)
from .job_documents import (
    AvailabilityJobPayload,
    AvailabilityJobResult,
    DistributionJobPayload,
    EmptyJobResult,
    GenericJobDocument,
    RecipeBuildCleanupParent,
    RecipeBuildParent,
    RecipeInstallParent,
    RecipeJobActivateParent,
    RecipeJobRunParent,
    RecipeReconcileParent,
    RecipeStartParent,
    RecipeStopParent,
    RecipeUninstallParent,
    RunSwitchJobPayload,
)
from .mapping_parameters import MappingParameters
from .model_cache_contract import (
    CacheManifest,
    ModelCacheDownloadPayload,
    ModelCacheOperationProgress,
    ModelCacheRemovalPayload,
    ModelCacheRepairPayload,
)
from .profile_stop_authority import ProfileJobRunStopJob
from .recipe_execution_contract import (
    StoredBuildPolicyReport,
    StoredInstallationPlan,
    StoredRunEndpoint,
    StoredRunPlan,
)
from .recipe_image_removal_contract import (
    RecipeCacheRemovalOwner,
    RecipeCacheRemovalResult,
)
from .recipe_lifecycle_contract import (
    RecipeOperationActivatedResult,
    RecipeOperationCancellationResult,
    RecipeOperationProgressResult,
    RecipeOperationResult,
    RecipeOperationStoppedResult,
)
from .recipe_update_contract import RecipeUpdateDocument, RecipeUpdateFailure
from .run_switch_contract import (
    RunSwitchDistributionChildResult,
    RunSwitchOperationResult,
)
from .run_switch_journal_contract import (
    RunSwitchJournalRepairEndEvidence,
    RunSwitchJournalRepairEvidence,
    RunSwitchJournalRepairPendingState,
)
from .stored_documents import RouteClaimMarker
from .stored_json import bind

ProfileLabels = Annotated[dict[LabelName, LabelValue], Field(max_length=16)]

bind("jobs", "targets", list[str])
bind("agent_enrollments", "provider_request", CertificateIssuanceBinding, nullable=True)
bind(
    "agent_certificate_rotations",
    "provider_request",
    CertificateIssuanceBinding,
    nullable=True,
)
bind("agent_node_profiles", "labels", ProfileLabels)
bind("fleet_profiles", "labels", ProfileLabels)
bind("node_inventory_snapshots", "capabilities", list[Capability])
bind(
    "node_inventory_snapshots",
    "network_interfaces",
    list[NetworkInterface],
    nullable=True,
)
bind("model_cache_sets", "protected_reasons", list[CacheReferenceReason])
bind("run_nodes", "endpoint", StoredRunEndpoint, nullable=True)
bind(
    "route_publications",
    "activation_marker",
    ActivationMarker | RouteClaimMarker,
    nullable=True,
)

bind("fleet_profiles", "assignments", list[FleetProfileAssignmentInput])
bind("fleet_profile_applications", "plan", FleetProfilePreview)
bind("fleet_profile_applications", "progress", FleetProfileApplicationProgress)
bind(
    "fleet_profile_applications",
    "result",
    FleetProfileApplicationResult,
    nullable=True,
)

bind(
    "catalog_document_revisions",
    "document",
    {"model": ModelDefinition, "recipe": RecipeDefinition},
    discriminator="kind",
)
bind(
    "catalog_document_revisions",
    "projected",
    {
        "model": ModelRevisionProjection | UnprojectedRevision,
        "recipe": RecipeRevisionProjection | UnprojectedRevision,
    },
    discriminator="kind",
)
bind("recipe_library_sync_runs", "result", ManagedCatalogSyncResult)
bind("recipe_source_bundles", "manifest", SourceBundleManifest)
bind("model_cache_sets", "manifest", CacheManifest)
bind(
    "model_cache_operations",
    "payload",
    {
        "download": ModelCacheDownloadPayload,
        "repair": ModelCacheRepairPayload,
        "remove": ModelCacheRemovalPayload,
    },
    discriminator="kind",
)
bind(
    "model_cache_operations",
    "progress",
    ModelCacheOperationProgress,
)

bind("recipe_builds", "policy_report", StoredBuildPolicyReport)
bind("recipe_builds", "plan", RecipeBuildRequest)
bind("cluster_mappings", "parameters", MappingParameters)
bind("recipe_installations", "plan", StoredInstallationPlan)
bind("recipe_runs", "plan", StoredRunPlan)

bind("artifact_jobs", "parameters", dict[str, ParameterScalar])
bind("artifact_jobs", "output_limits", ArtifactOutputLimits)
bind("artifact_jobs", "compiled_contract", CompiledArtifactContract)
bind("artifact_jobs", "input_manifest", RecipeJobInputManifest)
bind("artifact_jobs", "result_evidence", ArtifactJobResultEvidence, nullable=True)

bind("artifact_distribution_assignments", "objects", list[DistributionObject])
bind(
    "fleet_stream_events",
    "payload",
    {
        "node-profile": NodeProfilePayload,
        "node-telemetry-latest": NodeTelemetryPayload,
        "recipe-installation": RecipeInstallationPayload,
        "installation-node": InstallationNodePayload,
        "recipe-run": RecipeRunPayload,
        "run-node": RunNodePayload,
        "job": JobPayload,
        "agent-operation": AgentOperationPayload,
    },
    discriminator="entity_kind",
)

bind(
    "jobs",
    "payload",
    {
        "recipe.install": RecipeInstallParent,
        "recipe.start": RecipeStartParent,
        "recipe.stop": RecipeStopParent | ProfileJobRunStopJob,
        "recipe.uninstall": RecipeUninstallParent,
        "recipe.reconcile": RecipeReconcileParent,
        "recipe.job.run.v1": RecipeJobRunParent,
        "recipe.job.activate.v1": RecipeJobActivateParent,
        "recipe.build.v1": RecipeBuildParent,
        "recipe.build.cleanup.v1": RecipeBuildCleanupParent,
        "recipe.run-switch.v2": RunSwitchJobPayload,
        "recipe.stop.v2": RunSwitchJobPayload,
        "recipe.cleanup.v2": RunSwitchJobPayload,
        "recipe.image.availability.v2": AvailabilityJobPayload,
        "agent-upgrade": AgentUpgradeRolloutPayload,
        "artifact-distribution": DistributionJobPayload,
        "recipe.cache.update.v2": RecipeUpdateDocument,
        "recipe.cache.remove.v2": RecipeCacheRemovalOwner,
        "runtime.preflight.v1": RuntimePreflightRequest,
        None: GenericJobDocument,
    },
    discriminator="kind",
)

_LIFECYCLE_RESULT = (
    RecipeOperationResult
    | RecipeOperationProgressResult
    | RecipeOperationCancellationResult
)
bind(
    "jobs",
    "result",
    {
        "recipe.install": _LIFECYCLE_RESULT,
        "recipe.start": _LIFECYCLE_RESULT,
        "recipe.stop": RecipeOperationStoppedResult | _LIFECYCLE_RESULT,
        "recipe.uninstall": _LIFECYCLE_RESULT,
        "recipe.reconcile": _LIFECYCLE_RESULT,
        "recipe.build.v1": _LIFECYCLE_RESULT,
        "recipe.build.cleanup.v1": _LIFECYCLE_RESULT,
        "recipe.job.run.v1": RecipeJobRunResult | RecipeOperationCancellationResult,
        "recipe.job.activate.v1": RecipeOperationActivatedResult,
        "recipe.run-switch.v2": RunSwitchOperationResult,
        "recipe.stop.v2": RunSwitchOperationResult,
        "recipe.cleanup.v2": RunSwitchOperationResult,
        "recipe.cache.update.v2": RecipeUpdateFailure,
        "recipe.cache.remove.v2": RecipeCacheRemovalResult,
        "recipe.image.availability.v2": AvailabilityJobResult,
        "agent-upgrade": AgentUpgradeRolloutResult,
        "artifact-distribution": RunSwitchDistributionChildResult,
        "runtime.preflight.v1": EmptyJobResult,
        None: GenericJobDocument,
    },
    nullable=True,
    discriminator="kind",
)

bind(
    "agent_operations",
    "payload",
    {kind.value: model for kind, model in PAYLOAD_MODELS.items()},
    discriminator="kind",
)
bind("agent_operation_attempts", "progress", OperationProgress, nullable=True)
bind("agent_operation_attempts", "result", AgentResultPayload, nullable=True)

bind(
    "run_switch_journal_repairs",
    "evidence",
    RunSwitchJournalRepairEvidence | RunSwitchJournalRepairEndEvidence,
)

bind(
    "run_switch_journal_repair_pending", "progress", RunSwitchJournalRepairPendingState
)
