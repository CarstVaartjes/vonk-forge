"""Digest-bound orchestration for local recipe installation and execution."""

from collections.abc import Callable as Callable
from collections.abc import Collection as Collection
from collections.abc import Mapping as Mapping
from collections.abc import Sequence as Sequence
from dataclasses import dataclass as dataclass
from datetime import UTC as UTC
from datetime import datetime as datetime
from datetime import timedelta as timedelta
from typing import Literal as Literal
from typing import NoReturn as NoReturn
from typing import Protocol as Protocol

from pydantic import TypeAdapter as TypeAdapter
from sqlalchemy import func as func
from sqlalchemy import or_ as or_
from sqlalchemy import select as select
from sqlalchemy.exc import DBAPIError as DBAPIError
from sqlalchemy.exc import IntegrityError as IntegrityError
from sqlalchemy.exc import OperationalError as OperationalError
from sqlalchemy.orm import Session as Session
from sqlalchemy.orm import object_session as object_session
from sqlalchemy.orm import sessionmaker as sessionmaker
from vonk_agent_protocol import AgentFailureKind as AgentFailureKind
from vonk_agent_protocol import AgentFailureResult as AgentFailureResult
from vonk_agent_protocol import AgentInstallResult as AgentInstallResult
from vonk_agent_protocol import InstallAdmissionCode as InstallAdmissionCode
from vonk_agent_protocol import InstallationNodeState as InstallationNodeState
from vonk_agent_protocol import InstallationState as InstallationState
from vonk_agent_protocol import InvalidRequestError as InvalidRequestError
from vonk_agent_protocol import InvalidRequestReason as InvalidRequestReason
from vonk_agent_protocol import LifecycleState as LifecycleState
from vonk_agent_protocol import OperationProgress as OperationProgress
from vonk_agent_protocol import ProjectionCode as ProjectionCode
from vonk_agent_protocol import RecipeBuildCleanupEvidence as RecipeBuildCleanupEvidence
from vonk_agent_protocol import RecipeBuildCleanupRequest as RecipeBuildCleanupRequest
from vonk_agent_protocol import RecipeBuildCode as RecipeBuildCode
from vonk_agent_protocol import RecipeBuildEvidence as RecipeBuildEvidence
from vonk_agent_protocol import RecipeBuildRequest as RecipeBuildRequest
from vonk_agent_protocol import RecipeInstallPayload as RecipeInstallPayload
from vonk_agent_protocol import RecipeJobRunRequest as RecipeJobRunRequest
from vonk_agent_protocol import RecipeOperationCode as RecipeOperationCode
from vonk_agent_protocol import RecipeReconcilePayload as RecipeReconcilePayload
from vonk_agent_protocol import RecipeStartPayload as RecipeStartPayload
from vonk_agent_protocol import RecipeStartResult as RecipeStartResult
from vonk_agent_protocol import RecipeStopPayload as RecipeStopPayload
from vonk_agent_protocol import RecipeUninstallPayload as RecipeUninstallPayload
from vonk_agent_protocol import ReconcileCode as ReconcileCode
from vonk_agent_protocol import ReservationState as ReservationState
from vonk_agent_protocol import RouteState as RouteState
from vonk_agent_protocol import RunState as RunState
from vonk_agent_protocol import SecurityRefusalError as SecurityRefusalError
from vonk_agent_protocol import UnknownOutcomeError as UnknownOutcomeError
from vonk_agent_protocol import WaitReason as WaitReason
from vonk_agent_protocol import canonical_message as canonical_message
from vonk_agent_protocol import (
    validate_result_for_operation as validate_result_for_operation,
)
from vonk_agent_protocol.compiled_execution_plan import (
    CompiledExecutionPlan as WireCompiledExecutionPlan,
)
from vonk_forge_contracts import RecipeDefinition as RecipeDefinition
from vonk_forge_contracts import read_recipe as read_recipe

from .. import agent_operation_states as agent_operation_states
from .. import artifact_job_states as artifact_job_states
from .. import job_states as job_states
from ..admission_locking import AdmissionLockBusy as AdmissionLockBusy
from ..admission_locking import AdmissionRowLock as AdmissionRowLock
from ..admission_locking import acquire_admission_keys as acquire_admission_keys
from ..admission_locking import admission_attempts as admission_attempts
from ..admission_locking import admission_wait_exhausted as admission_wait_exhausted
from ..admission_locking import is_admission_contention as is_admission_contention
from ..admission_locking import job_request_key as job_request_key
from ..admission_locking import lock_admission_rows as lock_admission_rows
from ..admission_locking import node_admission_key as node_admission_key
from ..agent_jobs import AgentJobService as AgentJobService
from ..agent_jobs import _JsonFlagIsTrue as _JsonFlagIsTrue
from ..agent_jobs import (
    release_owned_reservations_in_session as release_owned_reservations_in_session,
)
from ..agent_jobs import (
    superseded_cancellation_deadline as superseded_cancellation_deadline,
)
from ..artifact_job_evidence import (
    ArtifactJobResultEvidence as ArtifactJobResultEvidence,
)
from ..categorized_errors import InvalidValue as InvalidValue
from ..categorized_errors import MissingRecord as MissingRecord
from ..cluster_mappings import ClusterMappingPlan as ClusterMappingPlan
from ..cluster_mappings import ClusterMappingService as ClusterMappingService
from ..compiled_execution_plan import (
    MAX_COMPILED_EXECUTION_PLAN_BYTES as MAX_COMPILED_EXECUTION_PLAN_BYTES,
)
from ..compiled_execution_plan import (
    CompiledExecutionPlanError as CompiledExecutionPlanError,
)
from ..compiled_execution_plan import (
    validate_compiled_launch_payload as validate_compiled_launch_payload,
)
from ..distributed_lifecycle import (
    DistributedLifecycleError as DistributedLifecycleError,
)
from ..distributed_lifecycle import (
    DistributedRecoveryInvalid as DistributedRecoveryInvalid,
)
from ..distributed_recovery import (
    enforce_recovery_deadline as enforce_recovery_deadline,
)
from ..distributed_recovery import recovery_start_plan as recovery_start_plan
from ..distributed_recovery import run_node_reports_absent as run_node_reports_absent
from ..distributed_recovery import (
    settle_absent_run_in_session as settle_absent_run_in_session,
)
from ..install_admission import InstallAdmissionBusy as InstallAdmissionBusy
from ..install_admission import InstallAdmissionService as InstallAdmissionService
from ..install_admission import InstallPlan as InstallPlan
from ..install_admission import InstallPlanConflict as InstallPlanConflict
from ..install_admission import require_admissible as require_install_admissible
from ..install_admission import require_same_execution as require_same_install_execution
from ..job_documents import DistributedRecoveryMarker as DistributedRecoveryMarker
from ..job_documents import ProfilePartialStop as ProfilePartialStop
from ..job_documents import RecipeBuildCleanupParent as RecipeBuildCleanupParent
from ..job_documents import RecipeBuildParent as RecipeBuildParent
from ..job_documents import RecipeInstallParent as RecipeInstallParent
from ..job_documents import RecipeJobActivateParent as RecipeJobActivateParent
from ..job_documents import RecipeJobRunParent as RecipeJobRunParent
from ..job_documents import RecipeReconcileParent as RecipeReconcileParent
from ..job_documents import RecipeStartParent as RecipeStartParent
from ..job_documents import RecipeStopParent as RecipeStopParent
from ..job_documents import RecipeUninstallParent as RecipeUninstallParent
from ..job_documents import RunSwitchJobPayload as RunSwitchJobPayload
from ..job_documents import ServiceRunStopReview as ServiceRunStopReview
from ..lifecycle import CancelRequested as CancelRequested
from ..lifecycle import Effect as Effect
from ..lifecycle import Outcome as Outcome
from ..lifecycle import Reported as Reported
from ..lifecycle.agent_operation import AgentOperationAdapter as AgentOperationAdapter
from ..lifecycle.agent_operation import retry_scheduled as retry_scheduled
from ..lifecycle.artifact_job import ArtifactJobAdapter as ArtifactJobAdapter
from ..lifecycle.evidence import BookkeepingReason as BookkeepingReason
from ..lifecycle.evidence import Damaged as Damaged
from ..lifecycle.evidence import Residue as Residue
from ..lifecycle.evidence import read_or_rebuild as read_or_rebuild
from ..lifecycle.evidence import retire_as_unknown as retire_as_unknown
from ..lifecycle.recipe_operation import (
    RecipeOperationAdapter as RecipeOperationAdapter,
)
from ..logging import redact_text as redact_text
from ..mapping_parameters import MappingParameters as MappingParameters
from ..models import (
    STOPPABLE_NOT_RUNNING_RUN_STATES as STOPPABLE_NOT_RUNNING_RUN_STATES,
)
from ..models import STOPPABLE_RUN_STATES as STOPPABLE_RUN_STATES
from ..models import AgentNode as AgentNode
from ..models import AgentOperation as AgentOperation
from ..models import AgentOperationAttempt as AgentOperationAttempt
from ..models import AgentPresence as AgentPresence
from ..models import ArtifactJob as ArtifactJob
from ..models import CatalogDocumentRevision as CatalogDocumentRevision
from ..models import ClusterMapping as ClusterMapping
from ..models import ClusterMappingNode as ClusterMappingNode
from ..models import FleetProfileApplication as FleetProfileApplication
from ..models import InstallationNode as InstallationNode
from ..models import Job as Job
from ..models import RecipeBuild as RecipeBuild
from ..models import RecipeInstallation as RecipeInstallation
from ..models import RecipeRun as RecipeRun
from ..models import ResourceReservation as ResourceReservation
from ..models import RunNode as RunNode
from ..offline_stops import deferred_stop_nodes as deferred_stop_nodes
from ..prebuilt_images import policy_prebuilt_reference as policy_prebuilt_reference
from ..prebuilt_images import prebuilt_reference as prebuilt_reference
from ..profile_stop_authority import JobRunStopScope as JobRunStopScope
from ..profile_stop_authority import (
    ProfileJobRunStopAuthorization as ProfileJobRunStopAuthorization,
)
from ..profile_stop_authority import ProfileJobRunStopJob as ProfileJobRunStopJob
from ..profile_stop_authority import ProfileJobRunStopTarget as ProfileJobRunStopTarget
from ..profile_stop_authority import (
    ProfileStopAuthorityError as ProfileStopAuthorityError,
)
from ..profile_stop_authority import ProfileStopOwnerBinding as ProfileStopOwnerBinding
from ..profile_stop_authority import (
    validate_jobrun_stop_source as validate_jobrun_stop_source,
)
from ..profile_stop_authority import (
    validate_profile_jobrun_stop_target as validate_profile_jobrun_stop_target,
)
from ..profile_stop_authority import (
    validate_profile_stop_owner as validate_profile_stop_owner,
)
from ..profile_stop_authority import (
    validate_run_jobrun_stop_target as validate_run_jobrun_stop_target,
)
from ..recipe_action_plans import StopNodeImpact as StopNodeImpact
from ..recipe_action_plans import StopPlan as StopPlan
from ..recipe_action_plans import UninstallActiveRun as UninstallActiveRun
from ..recipe_action_plans import UninstallNodeImpact as UninstallNodeImpact
from ..recipe_action_plans import UninstallPlan as UninstallPlan
from ..recipe_action_plans import stop_plan as stop_plan
from ..recipe_action_plans import uninstall_plan as uninstall_plan
from ..recipe_build_cancellation import BuildConsumerError as BuildConsumerError
from ..recipe_build_cancellation import RecipeBuildIntent as RecipeBuildIntent
from ..recipe_build_cancellation import build_cancellation as build_cancellation
from ..recipe_build_cancellation import (
    current_build_consumers as current_build_consumers,
)
from ..recipe_build_cancellation import read_build_intent as read_build_intent
from ..recipe_build_cancellation import (
    request_build_cancellation as request_build_cancellation,
)
from ..recipe_builds import RecipeBuildAdmissionBusy as RecipeBuildAdmissionBusy
from ..recipe_builds import RecipeBuildPlan as RecipeBuildPlan
from ..recipe_builds import RecipeBuildService as RecipeBuildService
from ..recipe_execution_contract import (
    RecipeExecutionContractError as RecipeExecutionContractError,
)
from ..recipe_execution_contract import StoredInstallationPlan as StoredInstallationPlan
from ..recipe_execution_contract import StoredRunPlan as StoredRunPlan
from ..recipe_execution_contract import build_plan_document as build_plan_document
from ..recipe_execution_contract import (
    parse_stored_build_plan as parse_stored_build_plan,
)
from ..recipe_execution_contract import (
    parse_stored_build_policy as parse_stored_build_policy,
)
from ..recipe_execution_contract import (
    parse_stored_installation_plan as parse_stored_installation_plan,
)
from ..recipe_execution_contract import parse_stored_run_plan as parse_stored_run_plan
from ..recipe_execution_contract import run_endpoint_document as run_endpoint_document
from ..recipe_execution_contract import run_plan_document as run_plan_document
from ..recipe_lifecycle_contract import (
    LifecycleCodeFailureResult as LifecycleCodeFailureResult,
)
from ..recipe_lifecycle_contract import LifecycleNodeResult as LifecycleNodeResult
from ..recipe_lifecycle_contract import RecipeLifecycleResult as RecipeLifecycleResult
from ..recipe_lifecycle_contract import (
    RecipeOperationCancellationResult as RecipeOperationCancellationResult,
)
from ..recipe_lifecycle_contract import (
    RecipeOperationProgressResult as RecipeOperationProgressResult,
)
from ..recipe_lifecycle_contract import RecipeOperationResult as RecipeOperationResult
from ..recipe_lifecycle_contract import (
    parse_recipe_lifecycle_result as parse_recipe_lifecycle_result,
)
from ..recipe_lifecycle_contract import (
    validate_recipe_lifecycle_terminal as validate_recipe_lifecycle_terminal,
)
from ..recipe_progress import _cancel_requested as _cancel_requested
from ..recipe_progress import (
    _canonical_distributed_readiness as _canonical_distributed_readiness,
)
from ..recipe_progress import _catalog_recipe as _catalog_recipe
from ..recipe_progress import _current_phase_index as _current_phase_index
from ..recipe_progress import _lower_hex_digest as _lower_hex_digest
from ..recipe_progress import _parent_execution_mode as _parent_execution_mode
from ..recipe_progress import _parent_force_rebuild as _parent_force_rebuild
from ..recipe_progress import _parent_identity as _parent_identity
from ..recipe_progress import _parent_intent as _parent_intent
from ..recipe_progress import _parent_reconciliation as _parent_reconciliation
from ..recipe_progress import _parent_recovery as _parent_recovery
from ..recipe_progress import _parse_recipe_parent as _parse_recipe_parent
from ..recipe_progress import _primary_model_identity as _primary_model_identity
from ..recipe_progress import (
    _project_recipe_operation_progress as _project_recipe_operation_progress,
)
from ..recipe_progress import _recipe_model_identities as _recipe_model_identities
from ..recipe_progress import _recorded_parent as _recorded_parent
from ..recipe_progress import _role_phases as _role_phases
from ..recipe_progress import _start_deadline_failure as _start_deadline_failure
from ..recipe_progress import _stored_phases as _stored_phases
from ..recipe_progress import _topology_order as _topology_order
from ..recipe_progress import (
    current_recipe_progress_attribution as current_recipe_progress_attribution,
)
from ..recipe_routes import RecipeRouteError as RecipeRouteError
from ..recipe_routes import RecipeRouteNotReady as RecipeRouteNotReady
from ..recipe_routes import RecipeRouteService as RecipeRouteService
from ..recipe_routes import publication_is_temporary as publication_is_temporary
from ..recipe_routes import (
    route_health_recovery_pending as route_health_recovery_pending,
)
from ..recipe_routes import (
    route_publication_transaction as route_publication_transaction,
)
from ..recipe_runtime_specs import recipe_topology as recipe_topology
from ..recipe_start_payloads import RecipeStartPayloadError as RecipeStartPayloadError
from ..recipe_start_payloads import RecipeStartPlacement as RecipeStartPlacement
from ..recipe_start_payloads import (
    build_recipe_start_payload as build_recipe_start_payload,
)
from ..recipe_start_payloads import (
    validate_distributed_start_timeout_seconds as validate_distributed_start_timeout_seconds,
)
from ..recipe_stop_payloads import RecipeStopAuthorityError as RecipeStopAuthorityError
from ..recipe_stop_payloads import (
    durable_run_stop_payloads as durable_run_stop_payloads,
)
from ..recipe_stop_payloads import (
    stop_payload_from_job_run as stop_payload_from_job_run,
)
from ..recovery_policy import FailureKind as FailureKind
from ..reservation_owners import run_has_live_operation as run_has_live_operation
from ..run_admission import RunAdmissionBusy as RunAdmissionBusy
from ..run_admission import RunAdmissionService as RunAdmissionService
from ..run_admission import RunNodePlan as RunNodePlan
from ..run_admission import RunPlan as RunPlan
from ..run_admission import require_admissible as require_run_admissible
from ..run_admission import require_same_execution as require_same_run_execution
from ..run_switch_contract import (
    RunSwitchReconciliationAuthority as RunSwitchReconciliationAuthority,
)
from ..run_switch_contract import (
    RunSwitchReconciliationTarget as RunSwitchReconciliationTarget,
)
from ..source_policy import SourcePolicyReport as SourcePolicyReport
from ..storage_demands import StorageDemands as StorageDemands
from ..storage_demands import spark_scope as spark_scope
from ..strict_json import read_stored_model as read_stored_model
from ..strict_json import serialize_json_value as serialize_json_value
from .constants import _BLOCKER_REASON_CHARS as _BLOCKER_REASON_CHARS
from .constants import (
    _INITIAL_OBSERVATION_GRACE_SECONDS as _INITIAL_OBSERVATION_GRACE_SECONDS,
)
from .constants import _MAX_ACTION_NODES as _MAX_ACTION_NODES
from .constants import _MAX_ACTIVE_RUNS as _MAX_ACTIVE_RUNS
from .constants import _MEMORY_RESERVATION_KINDS as _MEMORY_RESERVATION_KINDS
from .constants import _STOP_WITHDRAWAL_ATTEMPTS as _STOP_WITHDRAWAL_ATTEMPTS
from .constants import _WORKLOAD_INTENT_KINDS as _WORKLOAD_INTENT_KINDS
from .constants import _bounded_blocker_reason as _bounded_blocker_reason
from .errors import (
    RecipeArtifactJobCancellationPending as RecipeArtifactJobCancellationPending,
)
from .errors import RecipeBuildOwnershipBusy as RecipeBuildOwnershipBusy
from .errors import RecipeInstallPreflightExpired as RecipeInstallPreflightExpired
from .errors import RecipeOperationConflict as RecipeOperationConflict
from .errors import RecipeReconciliationBlocked as RecipeReconciliationBlocked
from .errors import RecipeRequestInvalid as RecipeRequestInvalid
from .errors import RecipeRetryLater as RecipeRetryLater
from .errors import RecipeStopAuthorityRefused as RecipeStopAuthorityRefused
from .errors import _RouteNotWithdrawn as _RouteNotWithdrawn
from .errors import _ServiceStopReplay as _ServiceStopReplay
from .intent import _active_owned_workload_jobs as _active_owned_workload_jobs
from .intent import _bound_workload_intent as _bound_workload_intent
from .intent import _cancel_reason as _cancel_reason
from .intent import _intent_is_current as _intent_is_current
from .intent import _job_workload_intent as _job_workload_intent
from .intent import _profile_effect_scope as _profile_effect_scope
from .intent import _run_start_intent as _run_start_intent
from .intent import _shared_workload_intent as _shared_workload_intent
from .intent import _unissued_workload_children as _unissued_workload_children
from .intent import _workload_owner_scope as _workload_owner_scope
from .interfaces import _TERMINAL_JOB_STATES as _TERMINAL_JOB_STATES
from .interfaces import AgentJobQueue as AgentJobQueue
from .interfaces import IssuedWorkloadReconciliation as IssuedWorkloadReconciliation
from .interfaces import RecipeOperationView as RecipeOperationView
from .interfaces import RecipeRunRankStatus as RecipeRunRankStatus
from .interfaces import RecipeRunRecoveryOwner as RecipeRunRecoveryOwner
from .interfaces import RecipeRunStatus as RecipeRunStatus
from .interfaces import new_recipe_job as new_recipe_job
from .observation_helpers import _RECIPE_PARENT_READERS as _RECIPE_PARENT_READERS
from .observation_helpers import RecipeParent as RecipeParent
from .observation_helpers import RecipeWirePayload as RecipeWirePayload
from .observation_helpers import _active_recipe_revision as _active_recipe_revision
from .observation_helpers import _aware as _aware
from .observation_helpers import _PhaseGroups as _PhaseGroups
from .observation_helpers import _start_endpoint as _start_endpoint
from .observation_helpers import (
    prepare_exact_recipe_run_observation_nodes as prepare_exact_recipe_run_observation_nodes,
)
from .observation_helpers import record_build_evidence as record_build_evidence
from .rank_authority import _RECIPE_WIRE_PAYLOAD_MODELS as _RECIPE_WIRE_PAYLOAD_MODELS
from .rank_authority import _installation_accepted_ranks as _installation_accepted_ranks
from .rank_authority import _plan_ranks as _plan_ranks
from .rank_authority import _profile_jobrun_parent as _profile_jobrun_parent
from .rank_authority import _ranks_from_mapping as _ranks_from_mapping
from .rank_authority import _run_accepted_ranks as _run_accepted_ranks
from .rank_authority import _run_is_one_shot as _run_is_one_shot
from .rank_authority import _run_observes_per_generation as _run_observes_per_generation
from .rank_authority import _uninstall_recipe as _uninstall_recipe
from .rank_authority import _UninstallRecipe as _UninstallRecipe
from .results import _RANK_FAILED as _RANK_FAILED
from .results import _AcceptedRanks as _AcceptedRanks
from .results import _evidence_is_acceptable as _evidence_is_acceptable
from .results import _node_result as _node_result
from .results import _recorded_result as _recorded_result
from .results import _recorded_result_document as _recorded_result_document
from .results import _unproven_evidence as _unproven_evidence
from .results import _validated_result as _validated_result
from .service import RecipeOperationService as RecipeOperationService

__all__ = [
    "MAX_COMPILED_EXECUTION_PLAN_BYTES",
    "STOPPABLE_NOT_RUNNING_RUN_STATES",
    "STOPPABLE_RUN_STATES",
    "UTC",
    "_BLOCKER_REASON_CHARS",
    "_INITIAL_OBSERVATION_GRACE_SECONDS",
    "_MAX_ACTION_NODES",
    "_MAX_ACTIVE_RUNS",
    "_MEMORY_RESERVATION_KINDS",
    "_RANK_FAILED",
    "_RECIPE_PARENT_READERS",
    "_RECIPE_WIRE_PAYLOAD_MODELS",
    "_STOP_WITHDRAWAL_ATTEMPTS",
    "_TERMINAL_JOB_STATES",
    "_WORKLOAD_INTENT_KINDS",
    "AdmissionLockBusy",
    "AdmissionRowLock",
    "AgentFailureKind",
    "AgentFailureResult",
    "AgentInstallResult",
    "AgentJobQueue",
    "AgentJobService",
    "AgentNode",
    "AgentOperation",
    "AgentOperationAdapter",
    "AgentOperationAttempt",
    "AgentPresence",
    "ArtifactJob",
    "ArtifactJobAdapter",
    "ArtifactJobResultEvidence",
    "BookkeepingReason",
    "BuildConsumerError",
    "Callable",
    "CancelRequested",
    "CatalogDocumentRevision",
    "ClusterMapping",
    "ClusterMappingNode",
    "ClusterMappingPlan",
    "ClusterMappingService",
    "Collection",
    "CompiledExecutionPlanError",
    "DBAPIError",
    "Damaged",
    "DistributedLifecycleError",
    "DistributedRecoveryInvalid",
    "DistributedRecoveryMarker",
    "Effect",
    "FailureKind",
    "FleetProfileApplication",
    "InstallAdmissionBusy",
    "InstallAdmissionCode",
    "InstallAdmissionService",
    "InstallPlan",
    "InstallPlanConflict",
    "InstallationNode",
    "InstallationNodeState",
    "InstallationState",
    "IntegrityError",
    "InvalidRequestError",
    "InvalidRequestReason",
    "InvalidValue",
    "IssuedWorkloadReconciliation",
    "Job",
    "JobRunStopScope",
    "LifecycleCodeFailureResult",
    "LifecycleNodeResult",
    "LifecycleState",
    "Literal",
    "Mapping",
    "MappingParameters",
    "MissingRecord",
    "NoReturn",
    "OperationProgress",
    "OperationalError",
    "Outcome",
    "ProfileJobRunStopAuthorization",
    "ProfileJobRunStopJob",
    "ProfileJobRunStopTarget",
    "ProfilePartialStop",
    "ProfileStopAuthorityError",
    "ProfileStopOwnerBinding",
    "ProjectionCode",
    "Protocol",
    "RecipeArtifactJobCancellationPending",
    "RecipeBuild",
    "RecipeBuildAdmissionBusy",
    "RecipeBuildCleanupEvidence",
    "RecipeBuildCleanupParent",
    "RecipeBuildCleanupRequest",
    "RecipeBuildCode",
    "RecipeBuildEvidence",
    "RecipeBuildIntent",
    "RecipeBuildOwnershipBusy",
    "RecipeBuildParent",
    "RecipeBuildPlan",
    "RecipeBuildRequest",
    "RecipeBuildService",
    "RecipeDefinition",
    "RecipeExecutionContractError",
    "RecipeInstallParent",
    "RecipeInstallPayload",
    "RecipeInstallPreflightExpired",
    "RecipeInstallation",
    "RecipeJobActivateParent",
    "RecipeJobRunParent",
    "RecipeJobRunRequest",
    "RecipeLifecycleResult",
    "RecipeOperationAdapter",
    "RecipeOperationCancellationResult",
    "RecipeOperationCode",
    "RecipeOperationConflict",
    "RecipeOperationProgressResult",
    "RecipeOperationResult",
    "RecipeOperationService",
    "RecipeOperationView",
    "RecipeParent",
    "RecipeReconcileParent",
    "RecipeReconcilePayload",
    "RecipeReconciliationBlocked",
    "RecipeRequestInvalid",
    "RecipeRetryLater",
    "RecipeRouteError",
    "RecipeRouteNotReady",
    "RecipeRouteService",
    "RecipeRun",
    "RecipeRunRankStatus",
    "RecipeRunRecoveryOwner",
    "RecipeRunStatus",
    "RecipeStartParent",
    "RecipeStartPayload",
    "RecipeStartPayloadError",
    "RecipeStartPlacement",
    "RecipeStartResult",
    "RecipeStopAuthorityError",
    "RecipeStopAuthorityRefused",
    "RecipeStopParent",
    "RecipeStopPayload",
    "RecipeUninstallParent",
    "RecipeUninstallPayload",
    "RecipeWirePayload",
    "ReconcileCode",
    "Reported",
    "ReservationState",
    "Residue",
    "ResourceReservation",
    "RouteState",
    "RunAdmissionBusy",
    "RunAdmissionService",
    "RunNode",
    "RunNodePlan",
    "RunPlan",
    "RunState",
    "RunSwitchJobPayload",
    "RunSwitchReconciliationAuthority",
    "RunSwitchReconciliationTarget",
    "SecurityRefusalError",
    "Sequence",
    "ServiceRunStopReview",
    "Session",
    "SourcePolicyReport",
    "StopNodeImpact",
    "StopPlan",
    "StorageDemands",
    "StoredInstallationPlan",
    "StoredRunPlan",
    "TypeAdapter",
    "UninstallActiveRun",
    "UninstallNodeImpact",
    "UninstallPlan",
    "UnknownOutcomeError",
    "WaitReason",
    "WireCompiledExecutionPlan",
    "_AcceptedRanks",
    "_JsonFlagIsTrue",
    "_PhaseGroups",
    "_RouteNotWithdrawn",
    "_ServiceStopReplay",
    "_UninstallRecipe",
    "__all__",
    "_active_owned_workload_jobs",
    "_active_recipe_revision",
    "_aware",
    "_bound_workload_intent",
    "_bounded_blocker_reason",
    "_cancel_reason",
    "_cancel_requested",
    "_canonical_distributed_readiness",
    "_catalog_recipe",
    "_current_phase_index",
    "_evidence_is_acceptable",
    "_installation_accepted_ranks",
    "_intent_is_current",
    "_job_workload_intent",
    "_lower_hex_digest",
    "_node_result",
    "_parent_execution_mode",
    "_parent_force_rebuild",
    "_parent_identity",
    "_parent_intent",
    "_parent_reconciliation",
    "_parent_recovery",
    "_parse_recipe_parent",
    "_plan_ranks",
    "_primary_model_identity",
    "_profile_effect_scope",
    "_profile_jobrun_parent",
    "_project_recipe_operation_progress",
    "_ranks_from_mapping",
    "_recipe_model_identities",
    "_recorded_parent",
    "_recorded_result",
    "_recorded_result_document",
    "_role_phases",
    "_run_accepted_ranks",
    "_run_is_one_shot",
    "_run_observes_per_generation",
    "_run_start_intent",
    "_shared_workload_intent",
    "_start_deadline_failure",
    "_start_endpoint",
    "_stored_phases",
    "_topology_order",
    "_uninstall_recipe",
    "_unissued_workload_children",
    "_unproven_evidence",
    "_validated_result",
    "_workload_owner_scope",
    "acquire_admission_keys",
    "admission_attempts",
    "admission_wait_exhausted",
    "agent_operation_states",
    "artifact_job_states",
    "build_cancellation",
    "build_plan_document",
    "build_recipe_start_payload",
    "canonical_message",
    "current_build_consumers",
    "current_recipe_progress_attribution",
    "dataclass",
    "datetime",
    "deferred_stop_nodes",
    "durable_run_stop_payloads",
    "enforce_recovery_deadline",
    "func",
    "is_admission_contention",
    "job_request_key",
    "job_states",
    "lock_admission_rows",
    "new_recipe_job",
    "node_admission_key",
    "object_session",
    "or_",
    "parse_recipe_lifecycle_result",
    "parse_stored_build_plan",
    "parse_stored_build_policy",
    "parse_stored_installation_plan",
    "parse_stored_run_plan",
    "policy_prebuilt_reference",
    "prebuilt_reference",
    "prepare_exact_recipe_run_observation_nodes",
    "publication_is_temporary",
    "read_build_intent",
    "read_or_rebuild",
    "read_recipe",
    "read_stored_model",
    "recipe_topology",
    "record_build_evidence",
    "recovery_start_plan",
    "redact_text",
    "release_owned_reservations_in_session",
    "request_build_cancellation",
    "require_install_admissible",
    "require_run_admissible",
    "require_same_install_execution",
    "require_same_run_execution",
    "retire_as_unknown",
    "retry_scheduled",
    "route_health_recovery_pending",
    "route_publication_transaction",
    "run_endpoint_document",
    "run_has_live_operation",
    "run_node_reports_absent",
    "run_plan_document",
    "select",
    "serialize_json_value",
    "sessionmaker",
    "settle_absent_run_in_session",
    "spark_scope",
    "stop_payload_from_job_run",
    "stop_plan",
    "superseded_cancellation_deadline",
    "timedelta",
    "uninstall_plan",
    "validate_compiled_launch_payload",
    "validate_distributed_start_timeout_seconds",
    "validate_jobrun_stop_source",
    "validate_profile_jobrun_stop_target",
    "validate_profile_stop_owner",
    "validate_recipe_lifecycle_terminal",
    "validate_result_for_operation",
    "validate_run_jobrun_stop_target",
]
