"""Which contract owns the document in every JSON column of ``models.py``.

``bind`` is the only place a JSON column gets its contract; a column with no
binding fails ``control/tests/test_json_column_contracts.py``.  A table that
stores several document families binds one contract per value of its
``kind`` column.  The mechanism (reading, writing, the write guard and the
structural walker) is :mod:`vonk_control.stored.json_values`.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field
from vonk_forge_contracts import (
    CatalogVocabulary,
    ModelDefinition,
    ModelDisplay,
    RecipeDefinition,
    RecipeDisplay,
)

from vonk_agent_protocol.agent_words import (
    ProfileChildJobKind,
    RunSwitchReasonScopeName,
)
from vonk_agent_protocol.build_import import RecipeBuildRequest
from vonk_agent_protocol.contracts import PAYLOAD_MODELS, AgentResultPayload
from vonk_agent_protocol.distribution import DistributionObject
from vonk_agent_protocol.failure_evidence import FailureDiagnostics
from vonk_agent_protocol.gpu_clock import GpuClockObservation
from vonk_agent_protocol.inventory import (
    Capability,
    HostSettings,
    InstallationBytes,
    InstallationId,
    ModelStoreObject,
    NetworkInterface,
    RunScopeId,
)
from vonk_agent_protocol.job_inputs import RecipeJobInputManifest
from vonk_agent_protocol.reason_codes.model_cache import CacheReferenceReason
from vonk_agent_protocol.recipe_jobs import RecipeJobRunResult
from vonk_agent_protocol.route_activation import ActivationMarker
from vonk_agent_protocol.runtime_preflight import RuntimePreflightRequest
from vonk_agent_protocol.source_bundles import SourceBundleManifest
from vonk_agent_protocol.telemetry import HostResourceSample
from vonk_agent_protocol.wire_model import OperationProgress
from vonk_control.agent_upgrades.contract import (
    AgentUpgradeRolloutPayload,
    AgentUpgradeRolloutResult,
)
from vonk_control.artifact_jobs.evidence import ArtifactJobResultEvidence
from vonk_control.ca_issuance_contract import CertificateIssuanceBinding
from vonk_control.catalog.revision_contract import (
    ModelRevisionProjection,
    RecipeRevisionProjection,
    UnprojectedRevision,
)
from vonk_control.catalog.sync_contract import ManagedCatalogSyncResult
from vonk_control.compiled_artifact_contract import (
    ArtifactOutputLimits,
    CompiledArtifactContract,
    ParameterScalar,
)
from vonk_control.coordinator_contract import CoordinatorFailureEvidence
from vonk_control.fleet_event_contract import (
    AgentOperationPayload,
    InstallationNodePayload,
    JobPayload,
    NodeProfilePayload,
    NodeTelemetryPayload,
    RecipeInstallationPayload,
    RecipeRunPayload,
    RunNodePayload,
)
from vonk_control.fleet_profile_contract.applications import (
    FleetProfileApplicationProgress,
    FleetProfileApplicationResult,
)
from vonk_control.fleet_profile_contract.definitions import FleetProfileAssignmentInput
from vonk_control.fleet_profile_contract.review import FleetProfilePreview
from vonk_control.fleet_profile_contract.vocabulary import LabelName, LabelValue
from vonk_control.fleet_profiles.stop_contract import ProfileJobRunStopJob
from vonk_control.inventory.items_contract import AgentInventoryItems
from vonk_control.jobs.documents import (
    AvailabilityJobPayload,
    AvailabilityJobResult,
    AvailabilityUnknownEnd,
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
from vonk_control.mapping_parameters import MappingParameters
from vonk_control.model_cache.contract import (
    CacheManifest,
    ModelCacheDownloadPayload,
    ModelCacheOperationProgress,
    ModelCacheRemovalPayload,
    ModelCacheRepairPayload,
)
from vonk_control.operator_receipt_contract import ReceiptOwner
from vonk_control.recipe_image_availability.removal_contract import (
    RecipeCacheRemovalOwner,
    RecipeCacheRemovalResult,
)
from vonk_control.recipe_operations.execution_contract import (
    StoredBuildPolicyReport,
    StoredInstallationPlan,
    StoredRunEndpoint,
    StoredRunPlan,
)
from vonk_control.recipe_operations.lifecycle_contract import (
    JOB_ACTIVATION,
    RecipeOperationActivatedResult,
    RecipeOperationCancellationResult,
    RecipeOperationProgressResult,
    RecipeOperationResult,
    RecipeOperationStoppedResult,
)
from vonk_control.recipe_update_batches.contract import (
    RecipeUpdateDocument,
    RecipeUpdateFailure,
)
from vonk_control.repeated_evidence_contract import RepeatedEvidence
from vonk_control.run_switch_contract.identity import RunSwitchCancellation
from vonk_control.run_switch_contract.journal import (
    JournalRepairPurpose,
    RunSwitchJournalRepairEndEvidence,
    RunSwitchJournalRepairEvidence,
    RunSwitchJournalRepairPendingState,
)
from vonk_control.run_switch_contract.operations import RunSwitchOperationResult
from vonk_control.run_switch_contract.phase_results import (
    RunSwitchDistributionChildResult,
)
from vonk_control.stored.documents import RouteClaimMarker
from vonk_control.stored.json_values import bind
from vonk_control.waits.contract import WaitEvidence

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
bind("node_inventory_snapshots", "capabilities", list[Capability], nullable=True)
bind(
    "node_inventory_snapshots",
    "network_interfaces",
    list[NetworkInterface],
    nullable=True,
)
bind("node_inventory_snapshots", "model_objects", list[ModelStoreObject], nullable=True)
bind("node_inventory_snapshots", "host_settings", HostSettings, nullable=True)
bind("node_telemetry_samples", "gpu_clock", GpuClockObservation, nullable=True)
bind("model_cache_sets", "protected_reasons", list[CacheReferenceReason])
bind("run_nodes", "endpoint", StoredRunEndpoint, nullable=True)
bind("run_nodes", "observation_failure_diagnostics", FailureDiagnostics, nullable=True)
bind("run_nodes", "last_exit_diagnostics", FailureDiagnostics, nullable=True)
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
    "catalog_documents",
    "display",
    {
        RunSwitchReasonScopeName.MODEL: ModelDisplay,
        ReceiptOwner.RECIPE: RecipeDisplay,
    },
    nullable=True,
    discriminator="kind",
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
bind("recipe_library_sync_runs", "vocabulary", CatalogVocabulary, nullable=True)
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
        JOB_ACTIVATION: RecipeJobActivateParent,
        "recipe.build.v1": RecipeBuildParent,
        "recipe.build.cleanup.v1": RecipeBuildCleanupParent,
        ProfileChildJobKind.RUN_SWITCH.value: RunSwitchJobPayload,
        ProfileChildJobKind.STOP.value: RunSwitchJobPayload,
        ProfileChildJobKind.CLEANUP.value: RunSwitchJobPayload,
        "recipe.image.availability.v2": AvailabilityJobPayload | AvailabilityUnknownEnd,
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
        JOB_ACTIVATION: RecipeOperationActivatedResult,
        ProfileChildJobKind.RUN_SWITCH.value: RunSwitchOperationResult,
        ProfileChildJobKind.STOP.value: RunSwitchOperationResult,
        ProfileChildJobKind.CLEANUP.value: RunSwitchOperationResult,
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

bind(
    "agent_issued_certificate_revocations",
    "provider_request",
    CertificateIssuanceBinding,
    nullable=True,
)

bind(
    "agent_certificates", "provider_request", CertificateIssuanceBinding, nullable=True
)


bind(
    "run_switch_journal_repair_pending",
    JournalRepairPurpose.CANCELLATION.value,
    RunSwitchCancellation,
    nullable=True,
)


bind("local_certificate_issuance", "binding", CertificateIssuanceBinding)

# Every wait uses one contract; next_check is projected from the owner's SQL clock.

for _table in (
    "coordinator_failures",
    "recipe_installations",
    "artifact_lifecycle_gates",
    "agent_operations",
    "jobs",
    "artifact_jobs",
    "model_cache_operations",
    "fleet_profile_applications",
    "fleet_profile_selection",
    "recipe_runs",
    "route_publication_owner",
    "recipe_library_sync_runs",
    "agent_certificates",
    "agent_issued_certificate_revocations",
):
    bind(_table, "wait_evidence", WaitEvidence, nullable=True)


bind("coordinator_failures", "failure", CoordinatorFailureEvidence)


bind("node_inventory_snapshots", "agent_items", AgentInventoryItems, nullable=True)

bind("node_inventory_snapshots", "installations", list[InstallationId], nullable=True)
bind("node_inventory_snapshots", "runs", list[RunScopeId], nullable=True)
bind(
    "node_inventory_snapshots",
    "installation_bytes",
    list[InstallationBytes],
    nullable=True,
)


bind(
    "agent_node_contacts",
    "contact_repeated_evidence",
    list[RepeatedEvidence],
    nullable=True,
)
bind("agent_operations", "repeated_evidence", list[RepeatedEvidence], nullable=True)
bind(
    "fleet_profile_applications",
    "repeated_evidence",
    list[RepeatedEvidence],
    nullable=True,
)

bind("node_inventory_snapshots", "host_resources", HostResourceSample, nullable=True)
