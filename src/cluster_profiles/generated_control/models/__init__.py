""" Contains all the data models used in inputs/outputs """

from .agent_repair_manifest_request import AgentRepairManifestRequest
from .agent_summary import AgentSummary
from .agent_upgrade_apply_request import AgentUpgradeApplyRequest
from .agent_upgrade_apply_request_strategy import AgentUpgradeApplyRequestStrategy
from .agent_upgrade_diagnostics_response import AgentUpgradeDiagnosticsResponse
from .agent_upgrade_identity_response import AgentUpgradeIdentityResponse
from .agent_upgrade_package_request import AgentUpgradePackageRequest
from .agent_upgrade_preview_request import AgentUpgradePreviewRequest
from .agent_upgrade_preview_request_strategy import AgentUpgradePreviewRequestStrategy
from .agent_upgrade_target_diagnostics_response import AgentUpgradeTargetDiagnosticsResponse
from .agents_response import AgentsResponse
from .apply_agent_upgrade_response_apply_agent_upgrade_api_v1_agents_upgrades_post import ApplyAgentUpgradeResponseApplyAgentUpgradeApiV1AgentsUpgradesPost
from .apply_request import ApplyRequest
from .artifact_file_declaration import ArtifactFileDeclaration
from .artifact_job_create import ArtifactJobCreate
from .artifact_job_create_parameters import ArtifactJobCreateParameters
from .bounded_error_response import BoundedErrorResponse
from .build_plan_response import BuildPlanResponse
from .build_preview_input import BuildPreviewInput
from .build_preview_request import BuildPreviewRequest
from .build_preview_target import BuildPreviewTarget
from .build_request import BuildRequest
from .cancel_artifact_job_response_cancelartifactjob import CancelArtifactJobResponseCancelartifactjob
from .cancel_request import CancelRequest
from .capacity_reservations import CapacityReservations
from .catalog_entity_list_response import CatalogEntityListResponse
from .catalog_entity_revision_response import CatalogEntityRevisionResponse
from .catalog_entity_revision_response_document import CatalogEntityRevisionResponseDocument
from .catalog_entity_revision_response_kind import CatalogEntityRevisionResponseKind
from .catalog_entity_revision_response_lifecycle import CatalogEntityRevisionResponseLifecycle
from .catalog_problem import CatalogProblem
from .change_request import ChangeRequest
from .create_artifact_job_response_createartifactjob import CreateArtifactJobResponseCreateartifactjob
from .create_catalog_entity_request import CreateCatalogEntityRequest
from .create_catalog_entity_request_document import CreateCatalogEntityRequestDocument
from .create_recipe_request import CreateRecipeRequest
from .create_recipe_request_document import CreateRecipeRequestDocument
from .endpoint_response import EndpointResponse
from .enrollment_grant_response import EnrollmentGrantResponse
from .enrollment_grant_response_installer_url import EnrollmentGrantResponseInstallerUrl
from .enrollment_grant_response_purpose import EnrollmentGrantResponsePurpose
from .enrollment_list_response import EnrollmentListResponse
from .enrollment_summary import EnrollmentSummary
from .finalize_artifact_job_response_finalizeartifactjob import FinalizeArtifactJobResponseFinalizeartifactjob
from .fleet_node import FleetNode
from .fleet_node_identity import FleetNodeIdentity
from .fleet_node_labels import FleetNodeLabels
from .fleet_profile_application_view import FleetProfileApplicationView
from .fleet_profile_application_view_progress import FleetProfileApplicationViewProgress
from .fleet_profile_application_view_result_type_0 import FleetProfileApplicationViewResultType0
from .fleet_profile_application_view_state import FleetProfileApplicationViewState
from .fleet_profile_apply_request import FleetProfileApplyRequest
from .fleet_profile_assignment import FleetProfileAssignment
from .fleet_profile_assignment_desired_state import FleetProfileAssignmentDesiredState
from .fleet_profile_assignment_input import FleetProfileAssignmentInput
from .fleet_profile_assignment_input_desired_state import FleetProfileAssignmentInputDesiredState
from .fleet_profile_assignment_preview import FleetProfileAssignmentPreview
from .fleet_profile_assignment_preview_actions_item import FleetProfileAssignmentPreviewActionsItem
from .fleet_profile_assignment_preview_current_state import FleetProfileAssignmentPreviewCurrentState
from .fleet_profile_assignment_preview_desired_state import FleetProfileAssignmentPreviewDesiredState
from .fleet_profile_capture_input import FleetProfileCaptureInput
from .fleet_profile_capture_input_installation_policy import FleetProfileCaptureInputInstallationPolicy
from .fleet_profile_capture_input_labels import FleetProfileCaptureInputLabels
from .fleet_profile_duplicate_input import FleetProfileDuplicateInput
from .fleet_profile_input import FleetProfileInput
from .fleet_profile_input_installation_policy import FleetProfileInputInstallationPolicy
from .fleet_profile_input_labels import FleetProfileInputLabels
from .fleet_profile_list import FleetProfileList
from .fleet_profile_node import FleetProfileNode
from .fleet_profile_plan_step import FleetProfilePlanStep
from .fleet_profile_plan_step_kind import FleetProfilePlanStepKind
from .fleet_profile_plan_summary import FleetProfilePlanSummary
from .fleet_profile_prepare_preview_request import FleetProfilePreparePreviewRequest
from .fleet_profile_prepare_request import FleetProfilePrepareRequest
from .fleet_profile_preview import FleetProfilePreview
from .fleet_profile_preview_request import FleetProfilePreviewRequest
from .fleet_profile_reason import FleetProfileReason
from .fleet_profile_reason_severity import FleetProfileReasonSeverity
from .fleet_profile_scope import FleetProfileScope
from .fleet_profile_scope_preview import FleetProfileScopePreview
from .fleet_profile_status_view import FleetProfileStatusView
from .fleet_profile_status_view_state import FleetProfileStatusViewState
from .fleet_profile_view import FleetProfileView
from .fleet_profile_view_installation_policy import FleetProfileViewInstallationPolicy
from .fleet_profile_view_labels import FleetProfileViewLabels
from .fleet_snapshot import FleetSnapshot
from .fleet_status_response import FleetStatusResponse
from .fork_recipe_request import ForkRecipeRequest
from .freshness_policy import FreshnessPolicy
from .get_agent_upgrade_candidate_response_current_agent_upgrade_api_v1_agents_upgrades_candidate_get import GetAgentUpgradeCandidateResponseCurrentAgentUpgradeApiV1AgentsUpgradesCandidateGet
from .get_artifact_job_capabilities_response_getartifactjobcapabilities import GetArtifactJobCapabilitiesResponseGetartifactjobcapabilities
from .get_artifact_job_result_response_getartifactjobresult import GetArtifactJobResultResponseGetartifactjobresult
from .get_artifact_job_status_response_getartifactjobstatus import GetArtifactJobStatusResponseGetartifactjobstatus
from .get_authority_response_authority_view_api_v1_authority_get import GetAuthorityResponseAuthorityViewApiV1AuthorityGet
from .get_node_telemetry_history_resolution import GetNodeTelemetryHistoryResolution
from .global_import_preview_request import GlobalImportPreviewRequest
from .global_import_request import GlobalImportRequest
from .global_revision_response import GlobalRevisionResponse
from .global_revision_response_document import GlobalRevisionResponseDocument
from .grant_request import GrantRequest
from .grant_request_purpose import GrantRequestPurpose
from .http_validation_error import HTTPValidationError
from .image_distribution_plan_response import ImageDistributionPlanResponse
from .image_distribution_preview_input import ImageDistributionPreviewInput
from .image_distribution_preview_request import ImageDistributionPreviewRequest
from .image_distribution_preview_target import ImageDistributionPreviewTarget
from .image_distribution_request import ImageDistributionRequest
from .install_node_plan_response import InstallNodePlanResponse
from .install_plan_response import InstallPlanResponse
from .install_preview_input import InstallPreviewInput
from .install_preview_request import InstallPreviewRequest
from .install_preview_target import InstallPreviewTarget
from .install_request import InstallRequest
from .inventory_state import InventoryState
from .inventory_state_freshness import InventoryStateFreshness
from .job_detail_response import JobDetailResponse
from .job_logs_response import JobLogsResponse
from .job_operation_progress import JobOperationProgress
from .job_operation_response import JobOperationResponse
from .job_progress import JobProgress
from .job_resume_response import JobResumeResponse
from .job_summary import JobSummary
from .jobs_response import JobsResponse
from .library_installation_summary import LibraryInstallationSummary
from .library_installation_summary_state import LibraryInstallationSummaryState
from .library_model import LibraryModel
from .library_placement_application import LibraryPlacementApplication
from .library_placement_application_desired_state import LibraryPlacementApplicationDesiredState
from .library_placement_application_progress import LibraryPlacementApplicationProgress
from .library_placement_application_state import LibraryPlacementApplicationState
from .library_placement_apply_request import LibraryPlacementApplyRequest
from .library_placement_apply_request_desired_state import LibraryPlacementApplyRequestDesiredState
from .library_placement_apply_request_invocation import LibraryPlacementApplyRequestInvocation
from .library_placement_locations import LibraryPlacementLocations
from .library_placement_node import LibraryPlacementNode
from .library_placement_preview import LibraryPlacementPreview
from .library_placement_preview_desired_state import LibraryPlacementPreviewDesiredState
from .library_placement_preview_invocation import LibraryPlacementPreviewInvocation
from .library_placement_preview_request import LibraryPlacementPreviewRequest
from .library_placement_preview_request_desired_state import LibraryPlacementPreviewRequestDesiredState
from .library_placement_preview_request_invocation import LibraryPlacementPreviewRequestInvocation
from .library_placement_reason import LibraryPlacementReason
from .library_placement_reason_severity import LibraryPlacementReasonSeverity
from .library_placement_step import LibraryPlacementStep
from .library_placement_step_kind import LibraryPlacementStepKind
from .library_projection_reason import LibraryProjectionReason
from .library_projection_reason_severity import LibraryProjectionReasonSeverity
from .library_recipe_detail import LibraryRecipeDetail
from .library_recipe_identity import LibraryRecipeIdentity
from .library_recipe_identity_source_kind import LibraryRecipeIdentitySourceKind
from .library_recipe_summary import LibraryRecipeSummary
from .library_recipe_summary_source_kind import LibraryRecipeSummarySourceKind
from .library_run_summary import LibraryRunSummary
from .library_run_summary_route_state import LibraryRunSummaryRouteState
from .library_run_summary_state import LibraryRunSummaryState
from .library_snapshot import LibrarySnapshot
from .list_artifact_jobs_for_run_response_listartifactjobsforrun import ListArtifactJobsForRunResponseListartifactjobsforrun
from .list_audit_events_response_audit_view_api_v1_audit_get import ListAuditEventsResponseAuditViewApiV1AuditGet
from .list_identity_history_response_listidentityhistory import ListIdentityHistoryResponseListidentityhistory
from .managed_catalog_stale_recipe import ManagedCatalogStaleRecipe
from .managed_catalog_sync_problem import ManagedCatalogSyncProblem
from .managed_catalog_sync_request import ManagedCatalogSyncRequest
from .managed_catalog_sync_response import ManagedCatalogSyncResponse
from .managed_catalog_sync_response_state import ManagedCatalogSyncResponseState
from .managed_catalog_sync_response_trigger import ManagedCatalogSyncResponseTrigger
from .managed_catalog_withdrawn_recipe import ManagedCatalogWithdrawnRecipe
from .mapping_node_plan_response import MappingNodePlanResponse
from .mapping_plan_response import MappingPlanResponse
from .mapping_plan_response_parameters import MappingPlanResponseParameters
from .mapping_preview_input import MappingPreviewInput
from .mapping_preview_input_parameters import MappingPreviewInputParameters
from .mapping_preview_request import MappingPreviewRequest
from .mapping_preview_request_parameters import MappingPreviewRequestParameters
from .mapping_preview_target import MappingPreviewTarget
from .mapping_request import MappingRequest
from .mapping_request_parameters import MappingRequestParameters
from .mapping_response import MappingResponse
from .model_deletion_installation_impact_response import ModelDeletionInstallationImpactResponse
from .model_deletion_node_impact_response import ModelDeletionNodeImpactResponse
from .model_deletion_plan_response import ModelDeletionPlanResponse
from .model_deletion_preview_request import ModelDeletionPreviewRequest
from .model_version_identity import ModelVersionIdentity
from .node_connection import NodeConnection
from .node_connection_agent_state import NodeConnectionAgentState
from .node_connection_certificate_state import NodeConnectionCertificateState
from .node_connection_offline_reason_type_0 import NodeConnectionOfflineReasonType0
from .node_connection_online_state import NodeConnectionOnlineState
from .node_profile_update_request import NodeProfileUpdateRequest
from .node_status import NodeStatus
from .node_status_labels import NodeStatusLabels
from .operation_response import OperationResponse
from .operation_response_result_type_0 import OperationResponseResultType0
from .operational_build import OperationalBuild
from .operational_build_state import OperationalBuildState
from .operational_installation import OperationalInstallation
from .operational_installation_state import OperationalInstallationState
from .operational_mapping import OperationalMapping
from .operational_mapping_node import OperationalMappingNode
from .operational_mapping_state import OperationalMappingState
from .operational_run import OperationalRun
from .operational_run_route_state import OperationalRunRouteState
from .operational_run_state import OperationalRunState
from .operational_state import OperationalState
from .output_limits import OutputLimits
from .placement_evidence_counts import PlacementEvidenceCounts
from .placement_evidence_counts_truncated_collections_item import PlacementEvidenceCountsTruncatedCollectionsItem
from .placement_limits import PlacementLimits
from .placement_node import PlacementNode
from .placement_node_memory_kind import PlacementNodeMemoryKind
from .placement_recommendation import PlacementRecommendation
from .placement_recommendation_install_state import PlacementRecommendationInstallState
from .placement_recommendation_load_state import PlacementRecommendationLoadState
from .placement_score import PlacementScore
from .plan_reason import PlanReason
from .preview_agent_upgrade_response_preview_agent_upgrade_api_v1_agents_upgrades_preview_post import PreviewAgentUpgradeResponsePreviewAgentUpgradeApiV1AgentsUpgradesPreviewPost
from .preview_proposal_response_proposal_preview_api_v1_proposals_post import PreviewProposalResponseProposalPreviewApiV1ProposalsPost
from .preview_request import PreviewRequest
from .projection_reason import ProjectionReason
from .projection_reason_code import ProjectionReasonCode
from .projection_reason_severity import ProjectionReasonSeverity
from .proposal_change_request import ProposalChangeRequest
from .proposal_change_request_document import ProposalChangeRequestDocument
from .proposal_request import ProposalRequest
from .public_import_request import PublicImportRequest
from .public_recipe_artifact_identity import PublicRecipeArtifactIdentity
from .public_recipe_change import PublicRecipeChange
from .public_recipe_change_kind import PublicRecipeChangeKind
from .public_recipe_disk_requirements import PublicRecipeDiskRequirements
from .public_recipe_fabric import PublicRecipeFabric
from .public_recipe_fabric_connectivity import PublicRecipeFabricConnectivity
from .public_recipe_list_item import PublicRecipeListItem
from .public_recipe_list_item_alignment import PublicRecipeListItemAlignment
from .public_recipe_list_item_capabilities_item import PublicRecipeListItemCapabilitiesItem
from .public_recipe_list_item_execution_readiness import PublicRecipeListItemExecutionReadiness
from .public_recipe_list_item_execution_readiness_basis import PublicRecipeListItemExecutionReadinessBasis
from .public_recipe_list_item_qualification import PublicRecipeListItemQualification
from .public_recipe_list_item_qualification_basis import PublicRecipeListItemQualificationBasis
from .public_recipe_list_response import PublicRecipeListResponse
from .public_recipe_local_state import PublicRecipeLocalState
from .public_recipe_local_state_status import PublicRecipeLocalStateStatus
from .public_recipe_preview_response import PublicRecipePreviewResponse
from .public_recipe_preview_response_alignment import PublicRecipePreviewResponseAlignment
from .public_recipe_preview_response_capabilities_item import PublicRecipePreviewResponseCapabilitiesItem
from .public_recipe_preview_response_execution_readiness import PublicRecipePreviewResponseExecutionReadiness
from .public_recipe_preview_response_execution_readiness_basis import PublicRecipePreviewResponseExecutionReadinessBasis
from .public_recipe_preview_response_qualification import PublicRecipePreviewResponseQualification
from .public_recipe_preview_response_qualification_basis import PublicRecipePreviewResponseQualificationBasis
from .public_recipe_preview_response_source import PublicRecipePreviewResponseSource
from .public_recipe_release import PublicRecipeRelease
from .public_recipe_release_upgrade_effect import PublicRecipeReleaseUpgradeEffect
from .public_recipe_topology_role import PublicRecipeTopologyRole
from .publication_export_request import PublicationExportRequest
from .recipe_disk_requirements import RecipeDiskRequirements
from .recipe_fabric import RecipeFabric
from .recipe_fabric_connectivity import RecipeFabricConnectivity
from .recipe_library_import_request import RecipeLibraryImportRequest
from .recipe_library_import_request_document import RecipeLibraryImportRequestDocument
from .recipe_list_response import RecipeListResponse
from .recipe_memory_requirements import RecipeMemoryRequirements
from .recipe_memory_requirements_kind import RecipeMemoryRequirementsKind
from .recipe_parallelism import RecipeParallelism
from .recipe_presence import RecipePresence
from .recipe_presence_degraded_reason_type_0 import RecipePresenceDegradedReasonType0
from .recipe_presence_group_state import RecipePresenceGroupState
from .recipe_presence_rank_state import RecipePresenceRankState
from .recipe_revision_response import RecipeRevisionResponse
from .recipe_revision_response_document import RecipeRevisionResponseDocument
from .recipe_revision_response_lifecycle import RecipeRevisionResponseLifecycle
from .recipe_revision_response_origin import RecipeRevisionResponseOrigin
from .recipe_revision_summary import RecipeRevisionSummary
from .recipe_revision_summary_lifecycle import RecipeRevisionSummaryLifecycle
from .recipe_role import RecipeRole
from .recipe_summary_response import RecipeSummaryResponse
from .recipe_summary_response_lifecycle import RecipeSummaryResponseLifecycle
from .recipe_summary_response_origin import RecipeSummaryResponseOrigin
from .recipe_topology import RecipeTopology
from .rejected_node import RejectedNode
from .request_key import RequestKey
from .resolve_catalog_entity_request import ResolveCatalogEntityRequest
from .resolve_import_request import ResolveImportRequest
from .resolve_import_request_overlays import ResolveImportRequestOverlays
from .resolve_recipe_request import ResolveRecipeRequest
from .revise_catalog_entity_request import ReviseCatalogEntityRequest
from .revise_catalog_entity_request_document import ReviseCatalogEntityRequestDocument
from .run_node_plan_response import RunNodePlanResponse
from .run_plan_response import RunPlanResponse
from .run_presence import RunPresence
from .run_presence_degraded_reason_type_0 import RunPresenceDegradedReasonType0
from .run_presence_group_state import RunPresenceGroupState
from .run_presence_rank_state import RunPresenceRankState
from .run_presence_route_state import RunPresenceRouteState
from .run_presence_run_state import RunPresenceRunState
from .run_preview_input import RunPreviewInput
from .run_preview_request import RunPreviewRequest
from .run_preview_target import RunPreviewTarget
from .run_rank_status_response import RunRankStatusResponse
from .run_request import RunRequest
from .run_status_response import RunStatusResponse
from .source_bundle_response import SourceBundleResponse
from .source_check_request import SourceCheckRequest
from .source_policy_finding_response import SourcePolicyFindingResponse
from .source_policy_response import SourcePolicyResponse
from .stop_node_impact_response import StopNodeImpactResponse
from .stop_plan_response import StopPlanResponse
from .stop_preview_request import StopPreviewRequest
from .stop_request import StopRequest
from .submit_artifact_job_response_submitartifactjob import SubmitArtifactJobResponseSubmitartifactjob
from .submit_change_response_submit_change_api_v1_changes_post import SubmitChangeResponseSubmitChangeApiV1ChangesPost
from .telemetry_details import TelemetryDetails
from .telemetry_history_response import TelemetryHistoryResponse
from .telemetry_history_response_resolution import TelemetryHistoryResponseResolution
from .telemetry_metric_summary import TelemetryMetricSummary
from .telemetry_point import TelemetryPoint
from .telemetry_rollup_point import TelemetryRollupPoint
from .telemetry_rollup_point_metrics import TelemetryRollupPointMetrics
from .telemetry_rollup_point_resolution import TelemetryRollupPointResolution
from .telemetry_state import TelemetryState
from .telemetry_state_freshness import TelemetryStateFreshness
from .test_report_request import TestReportRequest
from .test_report_request_report import TestReportRequestReport
from .topology_placement import TopologyPlacement
from .uninstall_active_run_response import UninstallActiveRunResponse
from .uninstall_consequences_response import UninstallConsequencesResponse
from .uninstall_model_impact_response import UninstallModelImpactResponse
from .uninstall_node_impact_response import UninstallNodeImpactResponse
from .uninstall_plan_response import UninstallPlanResponse
from .uninstall_plan_response_recipe_content import UninstallPlanResponseRecipeContent
from .uninstall_preview_request import UninstallPreviewRequest
from .uninstall_request import UninstallRequest
from .update_recipe_draft_request import UpdateRecipeDraftRequest
from .update_recipe_draft_request_document import UpdateRecipeDraftRequestDocument
from .validation_error import ValidationError
from .visual_artifact import VisualArtifact
from .visual_build import VisualBuild
from .visual_build_additional_context import VisualBuildAdditionalContext
from .visual_build_context import VisualBuildContext
from .visual_build_option_value import VisualBuildOptionValue
from .visual_build_options import VisualBuildOptions
from .visual_build_options_format import VisualBuildOptionsFormat
from .visual_build_options_layer_compression import VisualBuildOptionsLayerCompression
from .visual_build_options_squash import VisualBuildOptionsSquash
from .visual_catalog_identity import VisualCatalogIdentity
from .visual_catalog_identity_kind import VisualCatalogIdentityKind
from .visual_execution import VisualExecution
from .visual_identity import VisualIdentity
from .visual_input_slot import VisualInputSlot
from .visual_interface import VisualInterface
from .visual_interface_input import VisualInterfaceInput
from .visual_interface_output import VisualInterfaceOutput
from .visual_metadata import VisualMetadata
from .visual_model_license import VisualModelLicense
from .visual_output_slot import VisualOutputSlot
from .visual_provenance import VisualProvenance
from .visual_provenance_source_kind import VisualProvenanceSourceKind
from .visual_recipe_document import VisualRecipeDocument
from .visual_recipe_parameter import VisualRecipeParameter
from .visual_recipe_parameter_change_effect import VisualRecipeParameterChangeEffect
from .visual_recipe_parameter_type import VisualRecipeParameterType
from .visual_runtime import VisualRuntime
from .visual_territorial_restrictions import VisualTerritorialRestrictions
from .visual_validation import VisualValidation

__all__ = (
    "AgentRepairManifestRequest",
    "AgentsResponse",
    "AgentSummary",
    "AgentUpgradeApplyRequest",
    "AgentUpgradeApplyRequestStrategy",
    "AgentUpgradeDiagnosticsResponse",
    "AgentUpgradeIdentityResponse",
    "AgentUpgradePackageRequest",
    "AgentUpgradePreviewRequest",
    "AgentUpgradePreviewRequestStrategy",
    "AgentUpgradeTargetDiagnosticsResponse",
    "ApplyAgentUpgradeResponseApplyAgentUpgradeApiV1AgentsUpgradesPost",
    "ApplyRequest",
    "ArtifactFileDeclaration",
    "ArtifactJobCreate",
    "ArtifactJobCreateParameters",
    "BoundedErrorResponse",
    "BuildPlanResponse",
    "BuildPreviewInput",
    "BuildPreviewRequest",
    "BuildPreviewTarget",
    "BuildRequest",
    "CancelArtifactJobResponseCancelartifactjob",
    "CancelRequest",
    "CapacityReservations",
    "CatalogEntityListResponse",
    "CatalogEntityRevisionResponse",
    "CatalogEntityRevisionResponseDocument",
    "CatalogEntityRevisionResponseKind",
    "CatalogEntityRevisionResponseLifecycle",
    "CatalogProblem",
    "ChangeRequest",
    "CreateArtifactJobResponseCreateartifactjob",
    "CreateCatalogEntityRequest",
    "CreateCatalogEntityRequestDocument",
    "CreateRecipeRequest",
    "CreateRecipeRequestDocument",
    "EndpointResponse",
    "EnrollmentGrantResponse",
    "EnrollmentGrantResponseInstallerUrl",
    "EnrollmentGrantResponsePurpose",
    "EnrollmentListResponse",
    "EnrollmentSummary",
    "FinalizeArtifactJobResponseFinalizeartifactjob",
    "FleetNode",
    "FleetNodeIdentity",
    "FleetNodeLabels",
    "FleetProfileApplicationView",
    "FleetProfileApplicationViewProgress",
    "FleetProfileApplicationViewResultType0",
    "FleetProfileApplicationViewState",
    "FleetProfileApplyRequest",
    "FleetProfileAssignment",
    "FleetProfileAssignmentDesiredState",
    "FleetProfileAssignmentInput",
    "FleetProfileAssignmentInputDesiredState",
    "FleetProfileAssignmentPreview",
    "FleetProfileAssignmentPreviewActionsItem",
    "FleetProfileAssignmentPreviewCurrentState",
    "FleetProfileAssignmentPreviewDesiredState",
    "FleetProfileCaptureInput",
    "FleetProfileCaptureInputInstallationPolicy",
    "FleetProfileCaptureInputLabels",
    "FleetProfileDuplicateInput",
    "FleetProfileInput",
    "FleetProfileInputInstallationPolicy",
    "FleetProfileInputLabels",
    "FleetProfileList",
    "FleetProfileNode",
    "FleetProfilePlanStep",
    "FleetProfilePlanStepKind",
    "FleetProfilePlanSummary",
    "FleetProfilePreparePreviewRequest",
    "FleetProfilePrepareRequest",
    "FleetProfilePreview",
    "FleetProfilePreviewRequest",
    "FleetProfileReason",
    "FleetProfileReasonSeverity",
    "FleetProfileScope",
    "FleetProfileScopePreview",
    "FleetProfileStatusView",
    "FleetProfileStatusViewState",
    "FleetProfileView",
    "FleetProfileViewInstallationPolicy",
    "FleetProfileViewLabels",
    "FleetSnapshot",
    "FleetStatusResponse",
    "ForkRecipeRequest",
    "FreshnessPolicy",
    "GetAgentUpgradeCandidateResponseCurrentAgentUpgradeApiV1AgentsUpgradesCandidateGet",
    "GetArtifactJobCapabilitiesResponseGetartifactjobcapabilities",
    "GetArtifactJobResultResponseGetartifactjobresult",
    "GetArtifactJobStatusResponseGetartifactjobstatus",
    "GetAuthorityResponseAuthorityViewApiV1AuthorityGet",
    "GetNodeTelemetryHistoryResolution",
    "GlobalImportPreviewRequest",
    "GlobalImportRequest",
    "GlobalRevisionResponse",
    "GlobalRevisionResponseDocument",
    "GrantRequest",
    "GrantRequestPurpose",
    "HTTPValidationError",
    "ImageDistributionPlanResponse",
    "ImageDistributionPreviewInput",
    "ImageDistributionPreviewRequest",
    "ImageDistributionPreviewTarget",
    "ImageDistributionRequest",
    "InstallNodePlanResponse",
    "InstallPlanResponse",
    "InstallPreviewInput",
    "InstallPreviewRequest",
    "InstallPreviewTarget",
    "InstallRequest",
    "InventoryState",
    "InventoryStateFreshness",
    "JobDetailResponse",
    "JobLogsResponse",
    "JobOperationProgress",
    "JobOperationResponse",
    "JobProgress",
    "JobResumeResponse",
    "JobsResponse",
    "JobSummary",
    "LibraryInstallationSummary",
    "LibraryInstallationSummaryState",
    "LibraryModel",
    "LibraryPlacementApplication",
    "LibraryPlacementApplicationDesiredState",
    "LibraryPlacementApplicationProgress",
    "LibraryPlacementApplicationState",
    "LibraryPlacementApplyRequest",
    "LibraryPlacementApplyRequestDesiredState",
    "LibraryPlacementApplyRequestInvocation",
    "LibraryPlacementLocations",
    "LibraryPlacementNode",
    "LibraryPlacementPreview",
    "LibraryPlacementPreviewDesiredState",
    "LibraryPlacementPreviewInvocation",
    "LibraryPlacementPreviewRequest",
    "LibraryPlacementPreviewRequestDesiredState",
    "LibraryPlacementPreviewRequestInvocation",
    "LibraryPlacementReason",
    "LibraryPlacementReasonSeverity",
    "LibraryPlacementStep",
    "LibraryPlacementStepKind",
    "LibraryProjectionReason",
    "LibraryProjectionReasonSeverity",
    "LibraryRecipeDetail",
    "LibraryRecipeIdentity",
    "LibraryRecipeIdentitySourceKind",
    "LibraryRecipeSummary",
    "LibraryRecipeSummarySourceKind",
    "LibraryRunSummary",
    "LibraryRunSummaryRouteState",
    "LibraryRunSummaryState",
    "LibrarySnapshot",
    "ListArtifactJobsForRunResponseListartifactjobsforrun",
    "ListAuditEventsResponseAuditViewApiV1AuditGet",
    "ListIdentityHistoryResponseListidentityhistory",
    "ManagedCatalogStaleRecipe",
    "ManagedCatalogSyncProblem",
    "ManagedCatalogSyncRequest",
    "ManagedCatalogSyncResponse",
    "ManagedCatalogSyncResponseState",
    "ManagedCatalogSyncResponseTrigger",
    "ManagedCatalogWithdrawnRecipe",
    "MappingNodePlanResponse",
    "MappingPlanResponse",
    "MappingPlanResponseParameters",
    "MappingPreviewInput",
    "MappingPreviewInputParameters",
    "MappingPreviewRequest",
    "MappingPreviewRequestParameters",
    "MappingPreviewTarget",
    "MappingRequest",
    "MappingRequestParameters",
    "MappingResponse",
    "ModelDeletionInstallationImpactResponse",
    "ModelDeletionNodeImpactResponse",
    "ModelDeletionPlanResponse",
    "ModelDeletionPreviewRequest",
    "ModelVersionIdentity",
    "NodeConnection",
    "NodeConnectionAgentState",
    "NodeConnectionCertificateState",
    "NodeConnectionOfflineReasonType0",
    "NodeConnectionOnlineState",
    "NodeProfileUpdateRequest",
    "NodeStatus",
    "NodeStatusLabels",
    "OperationalBuild",
    "OperationalBuildState",
    "OperationalInstallation",
    "OperationalInstallationState",
    "OperationalMapping",
    "OperationalMappingNode",
    "OperationalMappingState",
    "OperationalRun",
    "OperationalRunRouteState",
    "OperationalRunState",
    "OperationalState",
    "OperationResponse",
    "OperationResponseResultType0",
    "OutputLimits",
    "PlacementEvidenceCounts",
    "PlacementEvidenceCountsTruncatedCollectionsItem",
    "PlacementLimits",
    "PlacementNode",
    "PlacementNodeMemoryKind",
    "PlacementRecommendation",
    "PlacementRecommendationInstallState",
    "PlacementRecommendationLoadState",
    "PlacementScore",
    "PlanReason",
    "PreviewAgentUpgradeResponsePreviewAgentUpgradeApiV1AgentsUpgradesPreviewPost",
    "PreviewProposalResponseProposalPreviewApiV1ProposalsPost",
    "PreviewRequest",
    "ProjectionReason",
    "ProjectionReasonCode",
    "ProjectionReasonSeverity",
    "ProposalChangeRequest",
    "ProposalChangeRequestDocument",
    "ProposalRequest",
    "PublicationExportRequest",
    "PublicImportRequest",
    "PublicRecipeArtifactIdentity",
    "PublicRecipeChange",
    "PublicRecipeChangeKind",
    "PublicRecipeDiskRequirements",
    "PublicRecipeFabric",
    "PublicRecipeFabricConnectivity",
    "PublicRecipeListItem",
    "PublicRecipeListItemAlignment",
    "PublicRecipeListItemCapabilitiesItem",
    "PublicRecipeListItemExecutionReadiness",
    "PublicRecipeListItemExecutionReadinessBasis",
    "PublicRecipeListItemQualification",
    "PublicRecipeListItemQualificationBasis",
    "PublicRecipeListResponse",
    "PublicRecipeLocalState",
    "PublicRecipeLocalStateStatus",
    "PublicRecipePreviewResponse",
    "PublicRecipePreviewResponseAlignment",
    "PublicRecipePreviewResponseCapabilitiesItem",
    "PublicRecipePreviewResponseExecutionReadiness",
    "PublicRecipePreviewResponseExecutionReadinessBasis",
    "PublicRecipePreviewResponseQualification",
    "PublicRecipePreviewResponseQualificationBasis",
    "PublicRecipePreviewResponseSource",
    "PublicRecipeRelease",
    "PublicRecipeReleaseUpgradeEffect",
    "PublicRecipeTopologyRole",
    "RecipeDiskRequirements",
    "RecipeFabric",
    "RecipeFabricConnectivity",
    "RecipeLibraryImportRequest",
    "RecipeLibraryImportRequestDocument",
    "RecipeListResponse",
    "RecipeMemoryRequirements",
    "RecipeMemoryRequirementsKind",
    "RecipeParallelism",
    "RecipePresence",
    "RecipePresenceDegradedReasonType0",
    "RecipePresenceGroupState",
    "RecipePresenceRankState",
    "RecipeRevisionResponse",
    "RecipeRevisionResponseDocument",
    "RecipeRevisionResponseLifecycle",
    "RecipeRevisionResponseOrigin",
    "RecipeRevisionSummary",
    "RecipeRevisionSummaryLifecycle",
    "RecipeRole",
    "RecipeSummaryResponse",
    "RecipeSummaryResponseLifecycle",
    "RecipeSummaryResponseOrigin",
    "RecipeTopology",
    "RejectedNode",
    "RequestKey",
    "ResolveCatalogEntityRequest",
    "ResolveImportRequest",
    "ResolveImportRequestOverlays",
    "ResolveRecipeRequest",
    "ReviseCatalogEntityRequest",
    "ReviseCatalogEntityRequestDocument",
    "RunNodePlanResponse",
    "RunPlanResponse",
    "RunPresence",
    "RunPresenceDegradedReasonType0",
    "RunPresenceGroupState",
    "RunPresenceRankState",
    "RunPresenceRouteState",
    "RunPresenceRunState",
    "RunPreviewInput",
    "RunPreviewRequest",
    "RunPreviewTarget",
    "RunRankStatusResponse",
    "RunRequest",
    "RunStatusResponse",
    "SourceBundleResponse",
    "SourceCheckRequest",
    "SourcePolicyFindingResponse",
    "SourcePolicyResponse",
    "StopNodeImpactResponse",
    "StopPlanResponse",
    "StopPreviewRequest",
    "StopRequest",
    "SubmitArtifactJobResponseSubmitartifactjob",
    "SubmitChangeResponseSubmitChangeApiV1ChangesPost",
    "TelemetryDetails",
    "TelemetryHistoryResponse",
    "TelemetryHistoryResponseResolution",
    "TelemetryMetricSummary",
    "TelemetryPoint",
    "TelemetryRollupPoint",
    "TelemetryRollupPointMetrics",
    "TelemetryRollupPointResolution",
    "TelemetryState",
    "TelemetryStateFreshness",
    "TestReportRequest",
    "TestReportRequestReport",
    "TopologyPlacement",
    "UninstallActiveRunResponse",
    "UninstallConsequencesResponse",
    "UninstallModelImpactResponse",
    "UninstallNodeImpactResponse",
    "UninstallPlanResponse",
    "UninstallPlanResponseRecipeContent",
    "UninstallPreviewRequest",
    "UninstallRequest",
    "UpdateRecipeDraftRequest",
    "UpdateRecipeDraftRequestDocument",
    "ValidationError",
    "VisualArtifact",
    "VisualBuild",
    "VisualBuildAdditionalContext",
    "VisualBuildContext",
    "VisualBuildOptions",
    "VisualBuildOptionsFormat",
    "VisualBuildOptionsLayerCompression",
    "VisualBuildOptionsSquash",
    "VisualBuildOptionValue",
    "VisualCatalogIdentity",
    "VisualCatalogIdentityKind",
    "VisualExecution",
    "VisualIdentity",
    "VisualInputSlot",
    "VisualInterface",
    "VisualInterfaceInput",
    "VisualInterfaceOutput",
    "VisualMetadata",
    "VisualModelLicense",
    "VisualOutputSlot",
    "VisualProvenance",
    "VisualProvenanceSourceKind",
    "VisualRecipeDocument",
    "VisualRecipeParameter",
    "VisualRecipeParameterChangeEffect",
    "VisualRecipeParameterType",
    "VisualRuntime",
    "VisualTerritorialRestrictions",
    "VisualValidation",
)
