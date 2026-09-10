""" Contains all the data models used in inputs/outputs """

from .agent_deployment_evidence import AgentDeploymentEvidence
from .agent_deployment_evidence_connectivity import AgentDeploymentEvidenceConnectivity
from .agent_failure_result import AgentFailureResult
from .agent_operation import AgentOperation
from .agent_operation_change import AgentOperationChange
from .agent_operation_payload import AgentOperationPayload
from .agent_repair_manifest_request import AgentRepairManifestRequest
from .agent_summary import AgentSummary
from .agent_upgrade_apply_request import AgentUpgradeApplyRequest
from .agent_upgrade_apply_request_strategy import AgentUpgradeApplyRequestStrategy
from .agent_upgrade_apply_response import AgentUpgradeApplyResponse
from .agent_upgrade_diagnostics_response import AgentUpgradeDiagnosticsResponse
from .agent_upgrade_identity_response import AgentUpgradeIdentityResponse
from .agent_upgrade_package_request import AgentUpgradePackageRequest
from .agent_upgrade_preview_request import AgentUpgradePreviewRequest
from .agent_upgrade_preview_request_strategy import AgentUpgradePreviewRequestStrategy
from .agent_upgrade_preview_response import AgentUpgradePreviewResponse
from .agent_upgrade_preview_response_strategy import AgentUpgradePreviewResponseStrategy
from .agent_upgrade_target_diagnostics_response import AgentUpgradeTargetDiagnosticsResponse
from .agents_response import AgentsResponse
from .artifact_file_declaration import ArtifactFileDeclaration
from .artifact_input_contract import ArtifactInputContract
from .artifact_job_capabilities_response import ArtifactJobCapabilitiesResponse
from .artifact_job_create import ArtifactJobCreate
from .artifact_job_create_parameters import ArtifactJobCreateParameters
from .artifact_job_list_response import ArtifactJobListResponse
from .artifact_job_response import ArtifactJobResponse
from .artifact_job_response_interface import ArtifactJobResponseInterface
from .artifact_job_response_state import ArtifactJobResponseState
from .artifact_job_result_evidence import ArtifactJobResultEvidence
from .artifact_job_storage_capabilities import ArtifactJobStorageCapabilities
from .artifact_job_transport_capabilities import ArtifactJobTransportCapabilities
from .artifact_output_contract import ArtifactOutputContract
from .artifact_output_file import ArtifactOutputFile
from .artifact_output_limits import ArtifactOutputLimits
from .artifact_slot_contract import ArtifactSlotContract
from .artifact_verification_evidence import ArtifactVerificationEvidence
from .audit_event_response import AuditEventResponse
from .audit_response import AuditResponse
from .availability_operation_failure import AvailabilityOperationFailure
from .availability_recovery_action import AvailabilityRecoveryAction
from .boolean_parameter import BooleanParameter
from .bounded_error_response import BoundedErrorResponse
from .build_argument import BuildArgument
from .build_context import BuildContext
from .build_network import BuildNetwork
from .build_network_mode import BuildNetworkMode
from .build_patch import BuildPatch
from .cancel_request import CancelRequest
from .capacity_reservations import CapacityReservations
from .catalog_problem import CatalogProblem
from .compatibility_identity import CompatibilityIdentity
from .compatibility_preparation import CompatibilityPreparation
from .compatibility_preparation_kind import CompatibilityPreparationKind
from .compatibility_preparation_stage import CompatibilityPreparationStage
from .compatibility_preparation_state import CompatibilityPreparationState
from .compiled_artifact_contract import CompiledArtifactContract
from .compiled_artifact_contract_engine_type_0 import CompiledArtifactContractEngineType0
from .compiled_artifact_contract_interface import CompiledArtifactContractInterface
from .controller_asset_state import ControllerAssetState
from .controller_asset_state_source import ControllerAssetStateSource
from .controller_asset_state_state import ControllerAssetStateState
from .deployment_model_identity import DeploymentModelIdentity
from .deployment_provenance import DeploymentProvenance
from .distribution_assignment import DistributionAssignment
from .distribution_object import DistributionObject
from .distribution_object_kind import DistributionObjectKind
from .endpoint_response import EndpointResponse
from .enrollment_grant_response import EnrollmentGrantResponse
from .enrollment_grant_response_installer_url import EnrollmentGrantResponseInstallerUrl
from .enrollment_grant_response_purpose import EnrollmentGrantResponsePurpose
from .enrollment_list_response import EnrollmentListResponse
from .enrollment_summary import EnrollmentSummary
from .enum_parameter import EnumParameter
from .error_context_response import ErrorContextResponse
from .error_context_response_decision import ErrorContextResponseDecision
from .error_context_response_source import ErrorContextResponseSource
from .evidence_age import EvidenceAge
from .evidence_age_freshness import EvidenceAgeFreshness
from .evidence_context import EvidenceContext
from .evidence_context_source import EvidenceContextSource
from .failure_diagnostics import FailureDiagnostics
from .failure_diagnostics_category import FailureDiagnosticsCategory
from .failure_evidence_bundle import FailureEvidenceBundle
from .failure_log_tail import FailureLogTail
from .failure_property import FailureProperty
from .fleet_action_response import FleetActionResponse
from .fleet_action_response_action import FleetActionResponseAction
from .fleet_change_event import FleetChangeEvent
from .fleet_enroll_request import FleetEnrollRequest
from .fleet_log_entry import FleetLogEntry
from .fleet_log_entry_level import FleetLogEntryLevel
from .fleet_log_entry_source import FleetLogEntrySource
from .fleet_log_response import FleetLogResponse
from .fleet_node import FleetNode
from .fleet_node_detail_response import FleetNodeDetailResponse
from .fleet_node_detail_response_labels import FleetNodeDetailResponseLabels
from .fleet_node_identity import FleetNodeIdentity
from .fleet_node_labels import FleetNodeLabels
from .fleet_profile_application_progress import FleetProfileApplicationProgress
from .fleet_profile_application_progress_assignments import FleetProfileApplicationProgressAssignments
from .fleet_profile_application_progress_child_source_type_0 import FleetProfileApplicationProgressChildSourceType0
from .fleet_profile_application_progress_step_results import FleetProfileApplicationProgressStepResults
from .fleet_profile_application_result import FleetProfileApplicationResult
from .fleet_profile_application_view import FleetProfileApplicationView
from .fleet_profile_application_view_state import FleetProfileApplicationViewState
from .fleet_profile_assignment import FleetProfileAssignment
from .fleet_profile_assignment_context import FleetProfileAssignmentContext
from .fleet_profile_assignment_desired_state import FleetProfileAssignmentDesiredState
from .fleet_profile_assignment_input import FleetProfileAssignmentInput
from .fleet_profile_assignment_input_desired_state import FleetProfileAssignmentInputDesiredState
from .fleet_profile_assignment_preparation import FleetProfileAssignmentPreparation
from .fleet_profile_assignment_preview import FleetProfileAssignmentPreview
from .fleet_profile_assignment_preview_actions_item import FleetProfileAssignmentPreviewActionsItem
from .fleet_profile_assignment_preview_current_state import FleetProfileAssignmentPreviewCurrentState
from .fleet_profile_assignment_preview_desired_state import FleetProfileAssignmentPreviewDesiredState
from .fleet_profile_assignment_view import FleetProfileAssignmentView
from .fleet_profile_assignment_view_model import FleetProfileAssignmentViewModel
from .fleet_profile_assignment_view_recipe import FleetProfileAssignmentViewRecipe
from .fleet_profile_assignment_view_resources import FleetProfileAssignmentViewResources
from .fleet_profile_child_progress import FleetProfileChildProgress
from .fleet_profile_child_progress_phase import FleetProfileChildProgressPhase
from .fleet_profile_input import FleetProfileInput
from .fleet_profile_input_installation_policy import FleetProfileInputInstallationPolicy
from .fleet_profile_input_labels import FleetProfileInputLabels
from .fleet_profile_intended_configuration import FleetProfileIntendedConfiguration
from .fleet_profile_intended_configuration_installation_policy import FleetProfileIntendedConfigurationInstallationPolicy
from .fleet_profile_list import FleetProfileList
from .fleet_profile_load_request import FleetProfileLoadRequest
from .fleet_profile_node import FleetProfileNode
from .fleet_profile_plan_step import FleetProfilePlanStep
from .fleet_profile_plan_step_kind import FleetProfilePlanStepKind
from .fleet_profile_plan_summary import FleetProfilePlanSummary
from .fleet_profile_preview import FleetProfilePreview
from .fleet_profile_reason import FleetProfileReason
from .fleet_profile_reason_severity import FleetProfileReasonSeverity
from .fleet_profile_scope import FleetProfileScope
from .fleet_profile_scope_preview import FleetProfileScopePreview
from .fleet_profile_step_result import FleetProfileStepResult
from .fleet_profile_switch_adapter_result import FleetProfileSwitchAdapterResult
from .fleet_profile_switch_adapter_state import FleetProfileSwitchAdapterState
from .fleet_profile_switch_adapter_state_active_kind_type_0 import FleetProfileSwitchAdapterStateActiveKindType0
from .fleet_profile_switch_adapter_state_state import FleetProfileSwitchAdapterStateState
from .fleet_profile_switch_child_result import FleetProfileSwitchChildResult
from .fleet_profile_switch_child_state import FleetProfileSwitchChildState
from .fleet_profile_switch_child_state_kind import FleetProfileSwitchChildStateKind
from .fleet_profile_switch_child_state_state import FleetProfileSwitchChildStateState
from .fleet_profile_switch_queue_item import FleetProfileSwitchQueueItem
from .fleet_profile_switch_queue_item_kind import FleetProfileSwitchQueueItemKind
from .fleet_profile_verification_result import FleetProfileVerificationResult
from .fleet_profile_view import FleetProfileView
from .fleet_profile_view_cache_summary import FleetProfileViewCacheSummary
from .fleet_profile_view_fleet_item import FleetProfileViewFleetItem
from .fleet_profile_view_installation_policy import FleetProfileViewInstallationPolicy
from .fleet_profile_view_labels import FleetProfileViewLabels
from .fleet_rename_request import FleetRenameRequest
from .fleet_snapshot import FleetSnapshot
from .fleet_snapshot_event import FleetSnapshotEvent
from .fleet_telemetry_event import FleetTelemetryEvent
from .fleet_upgrade_request import FleetUpgradeRequest
from .fleet_upgrade_request_strategy import FleetUpgradeRequestStrategy
from .float_parameter import FloatParameter
from .freshness_policy import FreshnessPolicy
from .get_fleet_log_info_source_type_0 import GetFleetLogInfoSourceType0
from .get_fleet_metrics_history_resolution import GetFleetMetricsHistoryResolution
from .grant_request import GrantRequest
from .grant_request_purpose import GrantRequestPurpose
from .identity_history_item import IdentityHistoryItem
from .identity_history_response import IdentityHistoryResponse
from .installation_node_change import InstallationNodeChange
from .installation_node_payload import InstallationNodePayload
from .integer_parameter import IntegerParameter
from .inventory_state import InventoryState
from .inventory_state_freshness import InventoryStateFreshness
from .job_change import JobChange
from .job_detail_response import JobDetailResponse
from .job_logs_response import JobLogsResponse
from .job_operation_response import JobOperationResponse
from .job_payload import JobPayload
from .job_progress import JobProgress
from .job_resume_response import JobResumeResponse
from .job_summary import JobSummary
from .jobs_response import JobsResponse
from .library_facet_values import LibraryFacetValues
from .library_local_progress import LibraryLocalProgress
from .library_local_progress_state import LibraryLocalProgressState
from .library_local_state import LibraryLocalState
from .library_local_state_controller import LibraryLocalStateController
from .library_model_identity import LibraryModelIdentity
from .library_model_projection import LibraryModelProjection
from .library_recipe_identity import LibraryRecipeIdentity
from .library_recipe_model import LibraryRecipeModel
from .library_recipe_projection import LibraryRecipeProjection
from .library_resource_projection import LibraryResourceProjection
from .lifecycle_preflight_checkpoint import LifecyclePreflightCheckpoint
from .lifecycle_preflight_checkpoint_attempts import LifecyclePreflightCheckpointAttempts
from .lifecycle_preflight_checkpoint_receipts import LifecyclePreflightCheckpointReceipts
from .list_model_library_sort import ListModelLibrarySort
from .list_recipe_library_sort import ListRecipeLibrarySort
from .managed_catalog_stale_recipe import ManagedCatalogStaleRecipe
from .managed_catalog_sync_problem import ManagedCatalogSyncProblem
from .managed_catalog_sync_request import ManagedCatalogSyncRequest
from .managed_catalog_sync_response import ManagedCatalogSyncResponse
from .managed_catalog_sync_response_state import ManagedCatalogSyncResponseState
from .managed_catalog_sync_response_trigger import ManagedCatalogSyncResponseTrigger
from .managed_catalog_withdrawn_recipe import ManagedCatalogWithdrawnRecipe
from .model_access import ModelAccess
from .model_access_authentication import ModelAccessAuthentication
from .model_access_visibility import ModelAccessVisibility
from .model_artifact_preparation import ModelArtifactPreparation
from .model_artifact_preparation_completeness import ModelArtifactPreparationCompleteness
from .model_cache_download_result import ModelCacheDownloadResult
from .model_cache_operator_request import ModelCacheOperatorRequest
from .model_cache_operator_response import ModelCacheOperatorResponse
from .model_cache_operator_response_action import ModelCacheOperatorResponseAction
from .model_cache_operator_response_state import ModelCacheOperatorResponseState
from .model_cache_removal_result import ModelCacheRemovalResult
from .model_capabilities import ModelCapabilities
from .model_capability_fact import ModelCapabilityFact
from .model_capability_fact_capability import ModelCapabilityFactCapability
from .model_capability_fact_evidence_status import ModelCapabilityFactEvidenceStatus
from .model_capability_fact_support import ModelCapabilityFactSupport
from .model_capability_provenance import ModelCapabilityProvenance
from .model_definition import ModelDefinition
from .model_definition_modalities_item import ModelDefinitionModalitiesItem
from .model_detail_response import ModelDetailResponse
from .model_family import ModelFamily
from .model_file import ModelFile
from .model_format import ModelFormat
from .model_format_container import ModelFormatContainer
from .model_identity import ModelIdentity
from .model_library_response import ModelLibraryResponse
from .model_library_response_filters import ModelLibraryResponseFilters
from .model_license import ModelLicense
from .model_limits import ModelLimits
from .model_lineage import ModelLineage
from .model_lineage_relation import ModelLineageRelation
from .model_lineage_source import ModelLineageSource
from .model_metadata import ModelMetadata
from .model_parameters import ModelParameters
from .model_provenance import ModelProvenance
from .model_record import ModelRecord
from .model_reference import ModelReference
from .model_source import ModelSource
from .model_territorial_restrictions import ModelTerritorialRestrictions
from .node_connection import NodeConnection
from .node_connection_agent_state import NodeConnectionAgentState
from .node_connection_certificate_state import NodeConnectionCertificateState
from .node_connection_offline_reason_type_0 import NodeConnectionOfflineReasonType0
from .node_connection_online_state import NodeConnectionOnlineState
from .node_profile_change import NodeProfileChange
from .node_profile_payload import NodeProfilePayload
from .operation_checkpoint import OperationCheckpoint
from .operation_detail_response import OperationDetailResponse
from .operation_evidence_download import OperationEvidenceDownload
from .operation_evidence_provenance import OperationEvidenceProvenance
from .operation_failure_evidence import OperationFailureEvidence
from .operation_member_progress import OperationMemberProgress
from .operation_member_progress_activity_type_0 import OperationMemberProgressActivityType0
from .operation_progress import OperationProgress
from .operation_progress_activity_type_0 import OperationProgressActivityType0
from .operation_recovery import OperationRecovery
from .operation_recovery_action import OperationRecoveryAction
from .operations_response import OperationsResponse
from .output_limits import OutputLimits
from .package_activation_receipt import PackageActivationReceipt
from .package_activation_receipt_phase import PackageActivationReceiptPhase
from .physical_acceptance_evidence import PhysicalAcceptanceEvidence
from .physical_acceptance_evidence_state import PhysicalAcceptanceEvidenceState
from .platform_boundary import PlatformBoundary
from .platform_boundary_boundary import PlatformBoundaryBoundary
from .platform_boundary_state import PlatformBoundaryState
from .preparation_reason import PreparationReason
from .preparation_reason_severity import PreparationReasonSeverity
from .projection_reason import ProjectionReason
from .projection_reason_code import ProjectionReasonCode
from .projection_reason_severity import ProjectionReasonSeverity
from .rank_provenance import RankProvenance
from .rank_provenance_identity_agreement import RankProvenanceIdentityAgreement
from .recipe_benchmark import RecipeBenchmark
from .recipe_benchmark_configuration import RecipeBenchmarkConfiguration
from .recipe_build_definition import RecipeBuildDefinition
from .recipe_build_execution import RecipeBuildExecution
from .recipe_definition import RecipeDefinition
from .recipe_detail_response import RecipeDetailResponse
from .recipe_disk_resources import RecipeDiskResources
from .recipe_embedding_settings import RecipeEmbeddingSettings
from .recipe_embedding_settings_knobs import RecipeEmbeddingSettingsKnobs
from .recipe_fabric import RecipeFabric
from .recipe_fabric_connectivity import RecipeFabricConnectivity
from .recipe_failure_policy import RecipeFailurePolicy
from .recipe_failure_policy_rank_loss import RecipeFailurePolicyRankLoss
from .recipe_failure_policy_recovery import RecipeFailurePolicyRecovery
from .recipe_generation_settings import RecipeGenerationSettings
from .recipe_generation_settings_knobs import RecipeGenerationSettingsKnobs
from .recipe_http_serving_request import RecipeHttpServingRequest
from .recipe_http_serving_request_body_type_0 import RecipeHttpServingRequestBodyType0
from .recipe_http_serving_request_method import RecipeHttpServingRequestMethod
from .recipe_identity import RecipeIdentity
from .recipe_image import RecipeImage
from .recipe_image_availability_action import RecipeImageAvailabilityAction
from .recipe_image_availability_artifact import RecipeImageAvailabilityArtifact
from .recipe_image_availability_child import RecipeImageAvailabilityChild
from .recipe_image_availability_child_kind import RecipeImageAvailabilityChildKind
from .recipe_image_availability_child_state import RecipeImageAvailabilityChildState
from .recipe_image_availability_response import RecipeImageAvailabilityResponse
from .recipe_image_availability_response_state import RecipeImageAvailabilityResponseState
from .recipe_image_availability_result import RecipeImageAvailabilityResult
from .recipe_image_execution import RecipeImageExecution
from .recipe_input_slot import RecipeInputSlot
from .recipe_installation_change import RecipeInstallationChange
from .recipe_installation_payload import RecipeInstallationPayload
from .recipe_integer_setting import RecipeIntegerSetting
from .recipe_integer_setting_change_effect import RecipeIntegerSettingChangeEffect
from .recipe_job_input import RecipeJobInput
from .recipe_job_interface import RecipeJobInterface
from .recipe_job_interface_adapter import RecipeJobInterfaceAdapter
from .recipe_job_output import RecipeJobOutput
from .recipe_job_serving_request import RecipeJobServingRequest
from .recipe_job_serving_request_input_slots import RecipeJobServingRequestInputSlots
from .recipe_job_settings import RecipeJobSettings
from .recipe_job_settings_knobs import RecipeJobSettingsKnobs
from .recipe_library_evidence import RecipeLibraryEvidence
from .recipe_library_response import RecipeLibraryResponse
from .recipe_library_response_filters import RecipeLibraryResponseFilters
from .recipe_lifecycle import RecipeLifecycle
from .recipe_memory_resources import RecipeMemoryResources
from .recipe_memory_resources_kind import RecipeMemoryResourcesKind
from .recipe_metadata import RecipeMetadata
from .recipe_metadata_alignment_type_0 import RecipeMetadataAlignmentType0
from .recipe_model_file import RecipeModelFile
from .recipe_model_selection import RecipeModelSelection
from .recipe_mount import RecipeMount
from .recipe_open_ai_interface import RecipeOpenAIInterface
from .recipe_operator_request import RecipeOperatorRequest
from .recipe_operator_response import RecipeOperatorResponse
from .recipe_operator_response_state import RecipeOperatorResponseState
from .recipe_output_slot import RecipeOutputSlot
from .recipe_parallelism import RecipeParallelism
from .recipe_presence import RecipePresence
from .recipe_presence_degraded_reason_type_0 import RecipePresenceDegradedReasonType0
from .recipe_presence_group_state import RecipePresenceGroupState
from .recipe_presence_rank_state import RecipePresenceRankState
from .recipe_provenance import RecipeProvenance
from .recipe_provenance_source_kind import RecipeProvenanceSourceKind
from .recipe_release import RecipeRelease
from .recipe_release_change import RecipeReleaseChange
from .recipe_release_change_kind import RecipeReleaseChangeKind
from .recipe_release_history_entry import RecipeReleaseHistoryEntry
from .recipe_release_history_entry_upgrade_effect import RecipeReleaseHistoryEntryUpgradeEffect
from .recipe_role_resources import RecipeRoleResources
from .recipe_run_change import RecipeRunChange
from .recipe_run_payload import RecipeRunPayload
from .recipe_runtime import RecipeRuntime
from .recipe_runtime_argument import RecipeRuntimeArgument
from .recipe_runtime_environment import RecipeRuntimeEnvironment
from .recipe_serving_validation import RecipeServingValidation
from .recipe_serving_validation_interface import RecipeServingValidationInterface
from .recipe_setting import RecipeSetting
from .recipe_setting_change_effect import RecipeSettingChangeEffect
from .recipe_topology import RecipeTopology
from .recipe_topology_mode import RecipeTopologyMode
from .recipe_topology_role import RecipeTopologyRole
from .recipe_update_request import RecipeUpdateRequest
from .recipe_update_response import RecipeUpdateResponse
from .recipe_validation import RecipeValidation
from .recipe_validation_check import RecipeValidationCheck
from .recipe_validation_check_assertions_item import RecipeValidationCheckAssertionsItem
from .recipe_validation_check_kind import RecipeValidationCheckKind
from .request_validation_issue import RequestValidationIssue
from .request_validation_problem import RequestValidationProblem
from .rollout_preparation import RolloutPreparation
from .run_node_change import RunNodeChange
from .run_node_payload import RunNodePayload
from .run_presence import RunPresence
from .run_presence_degraded_reason_type_0 import RunPresenceDegradedReasonType0
from .run_presence_group_state import RunPresenceGroupState
from .run_presence_rank_state import RunPresenceRankState
from .run_presence_route_state import RunPresenceRouteState
from .run_presence_run_state import RunPresenceRunState
from .run_switch_cached_transfer_result import RunSwitchCachedTransferResult
from .run_switch_cached_transfer_result_cached_target_totals import RunSwitchCachedTransferResultCachedTargetTotals
from .run_switch_cancellation import RunSwitchCancellation
from .run_switch_child_progress import RunSwitchChildProgress
from .run_switch_child_progress_phase_type_0 import RunSwitchChildProgressPhaseType0
from .run_switch_cleanup_result import RunSwitchCleanupResult
from .run_switch_cleanup_result_subphase_type_0 import RunSwitchCleanupResultSubphaseType0
from .run_switch_container_build_result import RunSwitchContainerBuildResult
from .run_switch_container_build_result_state import RunSwitchContainerBuildResultState
from .run_switch_final_verify_result import RunSwitchFinalVerifyResult
from .run_switch_final_verify_result_subphase_type_0 import RunSwitchFinalVerifyResultSubphaseType0
from .run_switch_member_receipt import RunSwitchMemberReceipt
from .run_switch_member_receipt_phase_type_0 import RunSwitchMemberReceiptPhaseType0
from .run_switch_member_receipt_state import RunSwitchMemberReceiptState
from .run_switch_model_download_pending_result import RunSwitchModelDownloadPendingResult
from .run_switch_model_download_result import RunSwitchModelDownloadResult
from .run_switch_operation_result import RunSwitchOperationResult
from .run_switch_operation_result_completed_phases_item import RunSwitchOperationResultCompletedPhasesItem
from .run_switch_operation_result_failed_phase_type_0 import RunSwitchOperationResultFailedPhaseType0
from .run_switch_operation_result_phase_type_0 import RunSwitchOperationResultPhaseType0
from .run_switch_operation_result_subphase_type_0 import RunSwitchOperationResultSubphaseType0
from .run_switch_prepared_result import RunSwitchPreparedResult
from .run_switch_rank_receipt import RunSwitchRankReceipt
from .run_switch_runtime_image_result import RunSwitchRuntimeImageResult
from .run_switch_runtime_install_result import RunSwitchRuntimeInstallResult
from .run_switch_runtime_plan_result import RunSwitchRuntimePlanResult
from .run_switch_start_result import RunSwitchStartResult
from .run_switch_start_result_subphase_type_0 import RunSwitchStartResultSubphaseType0
from .run_switch_stop_result import RunSwitchStopResult
from .run_switch_stop_result_subphase_type_0 import RunSwitchStopResultSubphaseType0
from .run_switch_target_transfer_evidence_result import RunSwitchTargetTransferEvidenceResult
from .run_switch_target_transfer_result import RunSwitchTargetTransferResult
from .run_switch_target_transfer_result_assignments import RunSwitchTargetTransferResultAssignments
from .run_switch_verify_result import RunSwitchVerifyResult
from .run_switch_verify_result_cached_target_totals import RunSwitchVerifyResultCachedTargetTotals
from .runtime_image_preparation import RuntimeImagePreparation
from .runtime_image_receipt import RuntimeImageReceipt
from .runtime_image_receipt_source import RuntimeImageReceiptSource
from .runtime_preflight_finding import RuntimePreflightFinding
from .runtime_preflight_finding_status import RuntimePreflightFindingStatus
from .runtime_preflight_result import RuntimePreflightResult
from .source_bundle_response import SourceBundleResponse
from .string_parameter import StringParameter
from .target_asset_state import TargetAssetState
from .target_asset_state_state import TargetAssetStateState
from .telemetry_capabilities_response import TelemetryCapabilitiesResponse
from .telemetry_capabilities_response_freshness import TelemetryCapabilitiesResponseFreshness
from .telemetry_capability import TelemetryCapability
from .telemetry_capability_measurement_kind import TelemetryCapabilityMeasurementKind
from .telemetry_capability_scope import TelemetryCapabilityScope
from .telemetry_current_response import TelemetryCurrentResponse
from .telemetry_current_response_freshness import TelemetryCurrentResponseFreshness
from .telemetry_details import TelemetryDetails
from .telemetry_history_metadata import TelemetryHistoryMetadata
from .telemetry_history_metadata_actual_resolution import TelemetryHistoryMetadataActualResolution
from .telemetry_history_metadata_requested_resolution import TelemetryHistoryMetadataRequestedResolution
from .telemetry_history_response import TelemetryHistoryResponse
from .telemetry_history_response_resolution import TelemetryHistoryResponseResolution
from .telemetry_metric_summary import TelemetryMetricSummary
from .telemetry_metrics import TelemetryMetrics
from .telemetry_point import TelemetryPoint
from .telemetry_provenance import TelemetryProvenance
from .telemetry_rollup_point import TelemetryRollupPoint
from .telemetry_rollup_point_metrics import TelemetryRollupPointMetrics
from .telemetry_rollup_point_resolution import TelemetryRollupPointResolution
from .telemetry_runtime import TelemetryRuntime
from .telemetry_runtime_readiness import TelemetryRuntimeReadiness
from .telemetry_series import TelemetrySeries
from .telemetry_series_freshness import TelemetrySeriesFreshness
from .telemetry_series_measurement_kind import TelemetrySeriesMeasurementKind
from .telemetry_series_scope import TelemetrySeriesScope
from .telemetry_series_support_status import TelemetrySeriesSupportStatus
from .telemetry_state import TelemetryState
from .telemetry_state_freshness import TelemetryStateFreshness
from .telemetry_workload import TelemetryWorkload
from .telemetry_workload_state import TelemetryWorkloadState
from .telemetry_workloads_response import TelemetryWorkloadsResponse
from .telemetry_workloads_response_freshness import TelemetryWorkloadsResponseFreshness
from .workload_provenance import WorkloadProvenance
from .workload_provenance_mapping_agreement import WorkloadProvenanceMappingAgreement
from .workload_provenance_rank_agreement import WorkloadProvenanceRankAgreement

__all__ = (
    "AgentDeploymentEvidence",
    "AgentDeploymentEvidenceConnectivity",
    "AgentFailureResult",
    "AgentOperation",
    "AgentOperationChange",
    "AgentOperationPayload",
    "AgentRepairManifestRequest",
    "AgentsResponse",
    "AgentSummary",
    "AgentUpgradeApplyRequest",
    "AgentUpgradeApplyRequestStrategy",
    "AgentUpgradeApplyResponse",
    "AgentUpgradeDiagnosticsResponse",
    "AgentUpgradeIdentityResponse",
    "AgentUpgradePackageRequest",
    "AgentUpgradePreviewRequest",
    "AgentUpgradePreviewRequestStrategy",
    "AgentUpgradePreviewResponse",
    "AgentUpgradePreviewResponseStrategy",
    "AgentUpgradeTargetDiagnosticsResponse",
    "ArtifactFileDeclaration",
    "ArtifactInputContract",
    "ArtifactJobCapabilitiesResponse",
    "ArtifactJobCreate",
    "ArtifactJobCreateParameters",
    "ArtifactJobListResponse",
    "ArtifactJobResponse",
    "ArtifactJobResponseInterface",
    "ArtifactJobResponseState",
    "ArtifactJobResultEvidence",
    "ArtifactJobStorageCapabilities",
    "ArtifactJobTransportCapabilities",
    "ArtifactOutputContract",
    "ArtifactOutputFile",
    "ArtifactOutputLimits",
    "ArtifactSlotContract",
    "ArtifactVerificationEvidence",
    "AuditEventResponse",
    "AuditResponse",
    "AvailabilityOperationFailure",
    "AvailabilityRecoveryAction",
    "BooleanParameter",
    "BoundedErrorResponse",
    "BuildArgument",
    "BuildContext",
    "BuildNetwork",
    "BuildNetworkMode",
    "BuildPatch",
    "CancelRequest",
    "CapacityReservations",
    "CatalogProblem",
    "CompatibilityIdentity",
    "CompatibilityPreparation",
    "CompatibilityPreparationKind",
    "CompatibilityPreparationStage",
    "CompatibilityPreparationState",
    "CompiledArtifactContract",
    "CompiledArtifactContractEngineType0",
    "CompiledArtifactContractInterface",
    "ControllerAssetState",
    "ControllerAssetStateSource",
    "ControllerAssetStateState",
    "DeploymentModelIdentity",
    "DeploymentProvenance",
    "DistributionAssignment",
    "DistributionObject",
    "DistributionObjectKind",
    "EndpointResponse",
    "EnrollmentGrantResponse",
    "EnrollmentGrantResponseInstallerUrl",
    "EnrollmentGrantResponsePurpose",
    "EnrollmentListResponse",
    "EnrollmentSummary",
    "EnumParameter",
    "ErrorContextResponse",
    "ErrorContextResponseDecision",
    "ErrorContextResponseSource",
    "EvidenceAge",
    "EvidenceAgeFreshness",
    "EvidenceContext",
    "EvidenceContextSource",
    "FailureDiagnostics",
    "FailureDiagnosticsCategory",
    "FailureEvidenceBundle",
    "FailureLogTail",
    "FailureProperty",
    "FleetActionResponse",
    "FleetActionResponseAction",
    "FleetChangeEvent",
    "FleetEnrollRequest",
    "FleetLogEntry",
    "FleetLogEntryLevel",
    "FleetLogEntrySource",
    "FleetLogResponse",
    "FleetNode",
    "FleetNodeDetailResponse",
    "FleetNodeDetailResponseLabels",
    "FleetNodeIdentity",
    "FleetNodeLabels",
    "FleetProfileApplicationProgress",
    "FleetProfileApplicationProgressAssignments",
    "FleetProfileApplicationProgressChildSourceType0",
    "FleetProfileApplicationProgressStepResults",
    "FleetProfileApplicationResult",
    "FleetProfileApplicationView",
    "FleetProfileApplicationViewState",
    "FleetProfileAssignment",
    "FleetProfileAssignmentContext",
    "FleetProfileAssignmentDesiredState",
    "FleetProfileAssignmentInput",
    "FleetProfileAssignmentInputDesiredState",
    "FleetProfileAssignmentPreparation",
    "FleetProfileAssignmentPreview",
    "FleetProfileAssignmentPreviewActionsItem",
    "FleetProfileAssignmentPreviewCurrentState",
    "FleetProfileAssignmentPreviewDesiredState",
    "FleetProfileAssignmentView",
    "FleetProfileAssignmentViewModel",
    "FleetProfileAssignmentViewRecipe",
    "FleetProfileAssignmentViewResources",
    "FleetProfileChildProgress",
    "FleetProfileChildProgressPhase",
    "FleetProfileInput",
    "FleetProfileInputInstallationPolicy",
    "FleetProfileInputLabels",
    "FleetProfileIntendedConfiguration",
    "FleetProfileIntendedConfigurationInstallationPolicy",
    "FleetProfileList",
    "FleetProfileLoadRequest",
    "FleetProfileNode",
    "FleetProfilePlanStep",
    "FleetProfilePlanStepKind",
    "FleetProfilePlanSummary",
    "FleetProfilePreview",
    "FleetProfileReason",
    "FleetProfileReasonSeverity",
    "FleetProfileScope",
    "FleetProfileScopePreview",
    "FleetProfileStepResult",
    "FleetProfileSwitchAdapterResult",
    "FleetProfileSwitchAdapterState",
    "FleetProfileSwitchAdapterStateActiveKindType0",
    "FleetProfileSwitchAdapterStateState",
    "FleetProfileSwitchChildResult",
    "FleetProfileSwitchChildState",
    "FleetProfileSwitchChildStateKind",
    "FleetProfileSwitchChildStateState",
    "FleetProfileSwitchQueueItem",
    "FleetProfileSwitchQueueItemKind",
    "FleetProfileVerificationResult",
    "FleetProfileView",
    "FleetProfileViewCacheSummary",
    "FleetProfileViewFleetItem",
    "FleetProfileViewInstallationPolicy",
    "FleetProfileViewLabels",
    "FleetRenameRequest",
    "FleetSnapshot",
    "FleetSnapshotEvent",
    "FleetTelemetryEvent",
    "FleetUpgradeRequest",
    "FleetUpgradeRequestStrategy",
    "FloatParameter",
    "FreshnessPolicy",
    "GetFleetLogInfoSourceType0",
    "GetFleetMetricsHistoryResolution",
    "GrantRequest",
    "GrantRequestPurpose",
    "IdentityHistoryItem",
    "IdentityHistoryResponse",
    "InstallationNodeChange",
    "InstallationNodePayload",
    "IntegerParameter",
    "InventoryState",
    "InventoryStateFreshness",
    "JobChange",
    "JobDetailResponse",
    "JobLogsResponse",
    "JobOperationResponse",
    "JobPayload",
    "JobProgress",
    "JobResumeResponse",
    "JobsResponse",
    "JobSummary",
    "LibraryFacetValues",
    "LibraryLocalProgress",
    "LibraryLocalProgressState",
    "LibraryLocalState",
    "LibraryLocalStateController",
    "LibraryModelIdentity",
    "LibraryModelProjection",
    "LibraryRecipeIdentity",
    "LibraryRecipeModel",
    "LibraryRecipeProjection",
    "LibraryResourceProjection",
    "LifecyclePreflightCheckpoint",
    "LifecyclePreflightCheckpointAttempts",
    "LifecyclePreflightCheckpointReceipts",
    "ListModelLibrarySort",
    "ListRecipeLibrarySort",
    "ManagedCatalogStaleRecipe",
    "ManagedCatalogSyncProblem",
    "ManagedCatalogSyncRequest",
    "ManagedCatalogSyncResponse",
    "ManagedCatalogSyncResponseState",
    "ManagedCatalogSyncResponseTrigger",
    "ManagedCatalogWithdrawnRecipe",
    "ModelAccess",
    "ModelAccessAuthentication",
    "ModelAccessVisibility",
    "ModelArtifactPreparation",
    "ModelArtifactPreparationCompleteness",
    "ModelCacheDownloadResult",
    "ModelCacheOperatorRequest",
    "ModelCacheOperatorResponse",
    "ModelCacheOperatorResponseAction",
    "ModelCacheOperatorResponseState",
    "ModelCacheRemovalResult",
    "ModelCapabilities",
    "ModelCapabilityFact",
    "ModelCapabilityFactCapability",
    "ModelCapabilityFactEvidenceStatus",
    "ModelCapabilityFactSupport",
    "ModelCapabilityProvenance",
    "ModelDefinition",
    "ModelDefinitionModalitiesItem",
    "ModelDetailResponse",
    "ModelFamily",
    "ModelFile",
    "ModelFormat",
    "ModelFormatContainer",
    "ModelIdentity",
    "ModelLibraryResponse",
    "ModelLibraryResponseFilters",
    "ModelLicense",
    "ModelLimits",
    "ModelLineage",
    "ModelLineageRelation",
    "ModelLineageSource",
    "ModelMetadata",
    "ModelParameters",
    "ModelProvenance",
    "ModelRecord",
    "ModelReference",
    "ModelSource",
    "ModelTerritorialRestrictions",
    "NodeConnection",
    "NodeConnectionAgentState",
    "NodeConnectionCertificateState",
    "NodeConnectionOfflineReasonType0",
    "NodeConnectionOnlineState",
    "NodeProfileChange",
    "NodeProfilePayload",
    "OperationCheckpoint",
    "OperationDetailResponse",
    "OperationEvidenceDownload",
    "OperationEvidenceProvenance",
    "OperationFailureEvidence",
    "OperationMemberProgress",
    "OperationMemberProgressActivityType0",
    "OperationProgress",
    "OperationProgressActivityType0",
    "OperationRecovery",
    "OperationRecoveryAction",
    "OperationsResponse",
    "OutputLimits",
    "PackageActivationReceipt",
    "PackageActivationReceiptPhase",
    "PhysicalAcceptanceEvidence",
    "PhysicalAcceptanceEvidenceState",
    "PlatformBoundary",
    "PlatformBoundaryBoundary",
    "PlatformBoundaryState",
    "PreparationReason",
    "PreparationReasonSeverity",
    "ProjectionReason",
    "ProjectionReasonCode",
    "ProjectionReasonSeverity",
    "RankProvenance",
    "RankProvenanceIdentityAgreement",
    "RecipeBenchmark",
    "RecipeBenchmarkConfiguration",
    "RecipeBuildDefinition",
    "RecipeBuildExecution",
    "RecipeDefinition",
    "RecipeDetailResponse",
    "RecipeDiskResources",
    "RecipeEmbeddingSettings",
    "RecipeEmbeddingSettingsKnobs",
    "RecipeFabric",
    "RecipeFabricConnectivity",
    "RecipeFailurePolicy",
    "RecipeFailurePolicyRankLoss",
    "RecipeFailurePolicyRecovery",
    "RecipeGenerationSettings",
    "RecipeGenerationSettingsKnobs",
    "RecipeHttpServingRequest",
    "RecipeHttpServingRequestBodyType0",
    "RecipeHttpServingRequestMethod",
    "RecipeIdentity",
    "RecipeImage",
    "RecipeImageAvailabilityAction",
    "RecipeImageAvailabilityArtifact",
    "RecipeImageAvailabilityChild",
    "RecipeImageAvailabilityChildKind",
    "RecipeImageAvailabilityChildState",
    "RecipeImageAvailabilityResponse",
    "RecipeImageAvailabilityResponseState",
    "RecipeImageAvailabilityResult",
    "RecipeImageExecution",
    "RecipeInputSlot",
    "RecipeInstallationChange",
    "RecipeInstallationPayload",
    "RecipeIntegerSetting",
    "RecipeIntegerSettingChangeEffect",
    "RecipeJobInput",
    "RecipeJobInterface",
    "RecipeJobInterfaceAdapter",
    "RecipeJobOutput",
    "RecipeJobServingRequest",
    "RecipeJobServingRequestInputSlots",
    "RecipeJobSettings",
    "RecipeJobSettingsKnobs",
    "RecipeLibraryEvidence",
    "RecipeLibraryResponse",
    "RecipeLibraryResponseFilters",
    "RecipeLifecycle",
    "RecipeMemoryResources",
    "RecipeMemoryResourcesKind",
    "RecipeMetadata",
    "RecipeMetadataAlignmentType0",
    "RecipeModelFile",
    "RecipeModelSelection",
    "RecipeMount",
    "RecipeOpenAIInterface",
    "RecipeOperatorRequest",
    "RecipeOperatorResponse",
    "RecipeOperatorResponseState",
    "RecipeOutputSlot",
    "RecipeParallelism",
    "RecipePresence",
    "RecipePresenceDegradedReasonType0",
    "RecipePresenceGroupState",
    "RecipePresenceRankState",
    "RecipeProvenance",
    "RecipeProvenanceSourceKind",
    "RecipeRelease",
    "RecipeReleaseChange",
    "RecipeReleaseChangeKind",
    "RecipeReleaseHistoryEntry",
    "RecipeReleaseHistoryEntryUpgradeEffect",
    "RecipeRoleResources",
    "RecipeRunChange",
    "RecipeRunPayload",
    "RecipeRuntime",
    "RecipeRuntimeArgument",
    "RecipeRuntimeEnvironment",
    "RecipeServingValidation",
    "RecipeServingValidationInterface",
    "RecipeSetting",
    "RecipeSettingChangeEffect",
    "RecipeTopology",
    "RecipeTopologyMode",
    "RecipeTopologyRole",
    "RecipeUpdateRequest",
    "RecipeUpdateResponse",
    "RecipeValidation",
    "RecipeValidationCheck",
    "RecipeValidationCheckAssertionsItem",
    "RecipeValidationCheckKind",
    "RequestValidationIssue",
    "RequestValidationProblem",
    "RolloutPreparation",
    "RunNodeChange",
    "RunNodePayload",
    "RunPresence",
    "RunPresenceDegradedReasonType0",
    "RunPresenceGroupState",
    "RunPresenceRankState",
    "RunPresenceRouteState",
    "RunPresenceRunState",
    "RunSwitchCachedTransferResult",
    "RunSwitchCachedTransferResultCachedTargetTotals",
    "RunSwitchCancellation",
    "RunSwitchChildProgress",
    "RunSwitchChildProgressPhaseType0",
    "RunSwitchCleanupResult",
    "RunSwitchCleanupResultSubphaseType0",
    "RunSwitchContainerBuildResult",
    "RunSwitchContainerBuildResultState",
    "RunSwitchFinalVerifyResult",
    "RunSwitchFinalVerifyResultSubphaseType0",
    "RunSwitchMemberReceipt",
    "RunSwitchMemberReceiptPhaseType0",
    "RunSwitchMemberReceiptState",
    "RunSwitchModelDownloadPendingResult",
    "RunSwitchModelDownloadResult",
    "RunSwitchOperationResult",
    "RunSwitchOperationResultCompletedPhasesItem",
    "RunSwitchOperationResultFailedPhaseType0",
    "RunSwitchOperationResultPhaseType0",
    "RunSwitchOperationResultSubphaseType0",
    "RunSwitchPreparedResult",
    "RunSwitchRankReceipt",
    "RunSwitchRuntimeImageResult",
    "RunSwitchRuntimeInstallResult",
    "RunSwitchRuntimePlanResult",
    "RunSwitchStartResult",
    "RunSwitchStartResultSubphaseType0",
    "RunSwitchStopResult",
    "RunSwitchStopResultSubphaseType0",
    "RunSwitchTargetTransferEvidenceResult",
    "RunSwitchTargetTransferResult",
    "RunSwitchTargetTransferResultAssignments",
    "RunSwitchVerifyResult",
    "RunSwitchVerifyResultCachedTargetTotals",
    "RuntimeImagePreparation",
    "RuntimeImageReceipt",
    "RuntimeImageReceiptSource",
    "RuntimePreflightFinding",
    "RuntimePreflightFindingStatus",
    "RuntimePreflightResult",
    "SourceBundleResponse",
    "StringParameter",
    "TargetAssetState",
    "TargetAssetStateState",
    "TelemetryCapabilitiesResponse",
    "TelemetryCapabilitiesResponseFreshness",
    "TelemetryCapability",
    "TelemetryCapabilityMeasurementKind",
    "TelemetryCapabilityScope",
    "TelemetryCurrentResponse",
    "TelemetryCurrentResponseFreshness",
    "TelemetryDetails",
    "TelemetryHistoryMetadata",
    "TelemetryHistoryMetadataActualResolution",
    "TelemetryHistoryMetadataRequestedResolution",
    "TelemetryHistoryResponse",
    "TelemetryHistoryResponseResolution",
    "TelemetryMetrics",
    "TelemetryMetricSummary",
    "TelemetryPoint",
    "TelemetryProvenance",
    "TelemetryRollupPoint",
    "TelemetryRollupPointMetrics",
    "TelemetryRollupPointResolution",
    "TelemetryRuntime",
    "TelemetryRuntimeReadiness",
    "TelemetrySeries",
    "TelemetrySeriesFreshness",
    "TelemetrySeriesMeasurementKind",
    "TelemetrySeriesScope",
    "TelemetrySeriesSupportStatus",
    "TelemetryState",
    "TelemetryStateFreshness",
    "TelemetryWorkload",
    "TelemetryWorkloadsResponse",
    "TelemetryWorkloadsResponseFreshness",
    "TelemetryWorkloadState",
    "WorkloadProvenance",
    "WorkloadProvenanceMappingAgreement",
    "WorkloadProvenanceRankAgreement",
)
