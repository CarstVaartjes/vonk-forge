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
from vonk_agent_protocol import CacheReferenceReason, RecipeBuildRequest
from vonk_agent_protocol.distribution import DistributionObject
from vonk_agent_protocol.inventory import Capability, NetworkInterface
from vonk_agent_protocol.job_inputs import RecipeJobInputManifest
from vonk_agent_protocol.route_activation import ActivationMarker
from vonk_agent_protocol.source_bundles import SourceBundleManifest
from vonk_forge_contracts import ModelDefinition, RecipeDefinition

from .artifact_jobs import ArtifactJobResultEvidence
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
from .mapping_parameters import MappingParameters
from .model_cache_contract import (
    CacheManifest,
    ModelCacheDownloadPayload,
    ModelCacheOperationProgress,
    ModelCacheRemovalPayload,
    ModelCacheRepairPayload,
)
from .recipe_execution_contract import (
    StoredBuildPolicyReport,
    StoredInstallationPlan,
    StoredRunEndpoint,
    StoredRunPlan,
)
from .stored_documents import RouteClaimMarker
from .stored_json import bind

ProfileLabels = Annotated[dict[LabelName, LabelValue], Field(max_length=16)]

bind("jobs", "targets", list[str])
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
