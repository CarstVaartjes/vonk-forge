"""Strict, transport-neutral contracts for high-level Run and Switch work.

Public facade; patch dependencies in their implementation modules.
"""

from datetime import datetime as datetime
from typing import Annotated as Annotated
from typing import Literal as Literal

from pydantic import BeforeValidator as BeforeValidator
from pydantic import Field as Field
from pydantic import StringConstraints as StringConstraints
from pydantic import model_validator as model_validator
from vonk_agent_protocol import DistributionCode as DistributionCode
from vonk_agent_protocol import LifecycleState as LifecycleState
from vonk_agent_protocol import LifecycleSubject as LifecycleSubject
from vonk_agent_protocol import OperationProgress as OperationProgress
from vonk_agent_protocol import WaitReason as WaitReason
from vonk_agent_protocol import state_adopter as state_adopter
from vonk_agent_protocol.agent_words import ProfileChildPhase as ProfileChildPhase
from vonk_agent_protocol.compiled_execution_plan import MemoryKind as MemoryKind
from vonk_agent_protocol.inventory import MemoryPool as MemoryPool
from vonk_forge_contracts.recipe import Scalar as Scalar

from ..distribution_assignment import (
    NodeDistributionAssignment as NodeDistributionAssignment,
)
from ..integer_domains import MAX_DATABASE_INTEGER as MAX_DATABASE_INTEGER
from ..lifecycle_preflight import (
    LifecyclePreflightCheckpoint as LifecyclePreflightCheckpoint,
)
from ..mapping_parameters import MappingParameters as MappingParameters
from ..model_cache_contract import ModelCacheDownloadResult as ModelCacheDownloadResult
from ..operation_blockers import OperationBlocker as OperationBlocker
from ..preparation_contract import RolloutPreparation as RolloutPreparation
from ..run_switch_identity_contract import NodeId as NodeId
from ..run_switch_identity_contract import (
    RunSwitchCancellation as RunSwitchCancellation,
)
from ..run_switch_identity_contract import UuidId as UuidId
from ..runtime_image_preparation import RuntimeImageReceipt as RuntimeImageReceipt
from ..strict_json import StrictModel as StrictModel
from .evidence import ArtifactStorageImpact as ArtifactStorageImpact
from .evidence import BuildCompatibilityEvidence as BuildCompatibilityEvidence
from .evidence import BuildSourceEvidence as BuildSourceEvidence
from .evidence import ConditionalPostStopMemoryCheck as ConditionalPostStopMemoryCheck
from .evidence import EffectiveParallelism as EffectiveParallelism
from .evidence import EffectiveSettingsSelection as EffectiveSettingsSelection
from .evidence import FreshnessEvidence as FreshnessEvidence
from .evidence import MappingSelection as MappingSelection
from .evidence import ResourceDemandEvidence as ResourceDemandEvidence
from .evidence import RunSwitchBuildEvidence as RunSwitchBuildEvidence
from .evidence import RunSwitchPhase as RunSwitchPhase
from .evidence import RunSwitchReason as RunSwitchReason
from .evidence import RuntimeImageStorageImpact as RuntimeImageStorageImpact
from .evidence import SparkFit as SparkFit
from .evidence import SparkFitNode as SparkFitNode
from .evidence import StopImpact as StopImpact
from .operations import RunSwitchCancelRequest as RunSwitchCancelRequest
from .operations import RunSwitchOperation as RunSwitchOperation
from .operations import RunSwitchOperationResult as RunSwitchOperationResult
from .operations import RunSwitchRetryRequest as RunSwitchRetryRequest
from .operations import (
    RunSwitchRuntimeImageReferenceIntent as RunSwitchRuntimeImageReferenceIntent,
)
from .phase_results import ArtifactVerificationResult as ArtifactVerificationResult
from .phase_results import (
    RunSwitchCachedTransferResult as RunSwitchCachedTransferResult,
)
from .phase_results import RunSwitchCleanupResult as RunSwitchCleanupResult
from .phase_results import RunSwitchCleanupVerifyResult as RunSwitchCleanupVerifyResult
from .phase_results import (
    RunSwitchContainerBuildResult as RunSwitchContainerBuildResult,
)
from .phase_results import (
    RunSwitchDistributionChildResult as RunSwitchDistributionChildResult,
)
from .phase_results import (
    RunSwitchDistributionEndedResult as RunSwitchDistributionEndedResult,
)
from .phase_results import RunSwitchFinalVerifyResult as RunSwitchFinalVerifyResult
from .phase_results import (
    RunSwitchInstallationVerifyResult as RunSwitchInstallationVerifyResult,
)
from .phase_results import (
    RunSwitchModelDownloadPendingResult as RunSwitchModelDownloadPendingResult,
)
from .phase_results import RunSwitchModelDownloadResult as RunSwitchModelDownloadResult
from .phase_results import RunSwitchPhaseResult as RunSwitchPhaseResult
from .phase_results import RunSwitchPreparedResult as RunSwitchPreparedResult
from .phase_results import RunSwitchRuntimeImageResult as RunSwitchRuntimeImageResult
from .phase_results import (
    RunSwitchRuntimeInstallResult as RunSwitchRuntimeInstallResult,
)
from .phase_results import RunSwitchRuntimePlanResult as RunSwitchRuntimePlanResult
from .phase_results import RunSwitchStartResult as RunSwitchStartResult
from .phase_results import RunSwitchStopResult as RunSwitchStopResult
from .phase_results import (
    RunSwitchTargetTransferEvidenceResult as RunSwitchTargetTransferEvidenceResult,
)
from .phase_results import (
    RunSwitchTargetTransferResult as RunSwitchTargetTransferResult,
)
from .phase_results import RunSwitchUninstallResult as RunSwitchUninstallResult
from .phase_results import RunSwitchVerifyResult as RunSwitchVerifyResult
from .phase_results import _RunSwitchPhaseBase as _RunSwitchPhaseBase
from .plans import RunSwitchAssessment as RunSwitchAssessment
from .plans import RunSwitchPlan as RunSwitchPlan
from .progress import ArtifactVerificationEvidence as ArtifactVerificationEvidence
from .progress import RunSwitchChildProgress as RunSwitchChildProgress
from .progress import RunSwitchMemberProgress as RunSwitchMemberProgress
from .progress import RunSwitchMemberReceipt as RunSwitchMemberReceipt
from .progress import RunSwitchProgress as RunSwitchProgress
from .progress import RunSwitchRankReceipt as RunSwitchRankReceipt
from .progress import _FailureText as _FailureText
from .requests import InstallationReconcileRequest as InstallationReconcileRequest
from .requests import InvocationMetadata as InvocationMetadata
from .requests import MemoryUsageUncertainty as MemoryUsageUncertainty
from .requests import RunMemoryResidualRange as RunMemoryResidualRange
from .requests import RunSwitchApplyRequest as RunSwitchApplyRequest
from .requests import RunSwitchCleanupApplyRequest as RunSwitchCleanupApplyRequest
from .requests import RunSwitchCleanupPreviewRequest as RunSwitchCleanupPreviewRequest
from .requests import RunSwitchPreviewRequest as RunSwitchPreviewRequest
from .requests import RunSwitchProfileStopScope as RunSwitchProfileStopScope
from .requests import (
    RunSwitchReconciliationAuthority as RunSwitchReconciliationAuthority,
)
from .requests import RunSwitchReconciliationTarget as RunSwitchReconciliationTarget
from .requests import RunSwitchStopApplyRequest as RunSwitchStopApplyRequest
from .requests import RunSwitchStopPreviewRequest as RunSwitchStopPreviewRequest
from .requests import SparkGroup as SparkGroup
from .requests import SparkGroupNode as SparkGroupNode
from .vocabulary import _DIGEST_PATTERN as _DIGEST_PATTERN
from .vocabulary import Alias as Alias
from .vocabulary import Digest as Digest
from .vocabulary import PortNumber as PortNumber
from .vocabulary import RunSwitchAction as RunSwitchAction
from .vocabulary import RunSwitchBuildEvidenceState as RunSwitchBuildEvidenceState
from .vocabulary import RunSwitchChangeEffect as RunSwitchChangeEffect
from .vocabulary import RunSwitchContainerBuildState as RunSwitchContainerBuildState
from .vocabulary import RunSwitchCoverage as RunSwitchCoverage
from .vocabulary import RunSwitchMemberState as RunSwitchMemberState
from .vocabulary import RunSwitchOperationKind as RunSwitchOperationKind
from .vocabulary import RunSwitchPhaseKind as RunSwitchPhaseKind
from .vocabulary import RunSwitchPlacementAction as RunSwitchPlacementAction
from .vocabulary import RunSwitchProgressState as RunSwitchProgressState
from .vocabulary import RunSwitchReasonScope as RunSwitchReasonScope
from .vocabulary import RunSwitchReasonSeverity as RunSwitchReasonSeverity
from .vocabulary import RunSwitchRetention as RunSwitchRetention
from .vocabulary import RunSwitchSubphase as RunSwitchSubphase

__all__ = [
    "Alias",
    "ArtifactStorageImpact",
    "ArtifactVerificationEvidence",
    "BuildCompatibilityEvidence",
    "BuildSourceEvidence",
    "Digest",
    "FreshnessEvidence",
    "InstallationReconcileRequest",
    "InvocationMetadata",
    "MappingSelection",
    "RunSwitchAction",
    "RunSwitchApplyRequest",
    "RunSwitchAssessment",
    "RunSwitchBuildEvidence",
    "RunSwitchBuildEvidenceState",
    "RunSwitchCachedTransferResult",
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
