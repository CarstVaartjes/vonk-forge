"""Strict public contracts for saved Fleet profiles and their applications.

Public facade; patch dependencies in their implementation modules.
"""

from dataclasses import dataclass as dataclass
from datetime import datetime as datetime
from typing import TYPE_CHECKING as TYPE_CHECKING
from typing import Annotated as Annotated
from typing import Literal as Literal
from typing import Protocol as Protocol
from uuid import NAMESPACE_URL as NAMESPACE_URL
from uuid import uuid5 as uuid5

from pydantic import BeforeValidator as BeforeValidator
from pydantic import Field as Field
from pydantic import StringConstraints as StringConstraints
from pydantic import model_validator as model_validator
from vonk_agent_protocol import DesiredAssignmentState as DesiredAssignmentState
from vonk_agent_protocol import EndpointState as EndpointState
from vonk_agent_protocol import LifecycleState as LifecycleState
from vonk_agent_protocol import LifecycleSubject as LifecycleSubject
from vonk_agent_protocol import ObservedAssignmentState as ObservedAssignmentState
from vonk_agent_protocol import OperationProgress as OperationProgress
from vonk_agent_protocol import ProfileReasonCode as ProfileReasonCode
from vonk_agent_protocol import SupersedeCode as SupersedeCode
from vonk_agent_protocol import machine_adopter as machine_adopter
from vonk_agent_protocol import state_adopter as state_adopter
from vonk_agent_protocol.agent_words import ProfileAction as ProfileAction
from vonk_agent_protocol.agent_words import ProfileChildPhase as ProfileChildPhase
from vonk_agent_protocol.agent_words import ProfileEffectState as ProfileEffectState
from vonk_agent_protocol.agent_words import (
    ProfileInstallationPolicy as ProfileInstallationPolicy,
)
from vonk_agent_protocol.agent_words import ProfileOperationKind as ProfileOperationKind
from vonk_agent_protocol.agent_words import ProfileReportedPhase as ProfileReportedPhase
from vonk_agent_protocol.agent_words import (
    ProfileSwitchChildKind as ProfileSwitchChildKind,
)
from vonk_agent_protocol.inventory import MemoryPool as MemoryPool

from ..endpoint_contract import EndpointResponse as EndpointResponse
from ..integer_domains import MAX_DATABASE_INTEGER as MAX_DATABASE_INTEGER
from ..model_cache_contract import CachedResourceEstimate as CachedResourceEstimate
from ..operation_blockers import OperationBlocker as OperationBlocker
from ..preparation_contract import CompatibilityIdentity as CompatibilityIdentity
from ..preparation_contract import ModelArtifactIdentity as ModelArtifactIdentity
from ..preparation_contract import PreparationReason as PreparationReason
from ..preparation_contract import RolloutPreparation as RolloutPreparation
from ..preparation_contract import RuntimeImageIdentity as RuntimeImageIdentity
from ..recipe_update_notice import RecipeUpdateNotice as RecipeUpdateNotice
from ..run_switch_contract import (
    ConditionalPostStopMemoryCheck as ConditionalPostStopMemoryCheck,
)
from ..run_switch_contract import (
    EffectiveSettingsSelection as EffectiveSettingsSelection,
)
from ..run_switch_contract import MemoryKind as MemoryKind
from ..run_switch_contract import PortNumber as PortNumber
from ..run_switch_contract import ResourceDemandEvidence as ResourceDemandEvidence
from ..run_switch_contract import RunSwitchAssessment as RunSwitchAssessment
from ..run_switch_contract import RunSwitchOperationResult as RunSwitchOperationResult
from ..run_switch_contract import RunSwitchProfileStopScope as RunSwitchProfileStopScope
from ..run_switch_contract import RunSwitchProgress as RunSwitchProgress
from ..run_switch_contract import RunSwitchReason as RunSwitchReason
from ..run_switch_contract import SparkGroupNode as SparkGroupNode
from ..run_switch_contract import StopImpact as StopImpact
from ..strict_json import StrictModel as StrictModel
from .adapter import FleetProfileSwitchAdapter as FleetProfileSwitchAdapter
from .applications import (
    FleetProfileApplicationCancellationIntent as FleetProfileApplicationCancellationIntent,
)
from .applications import (
    FleetProfileApplicationCancellationView as FleetProfileApplicationCancellationView,
)
from .applications import FleetProfileApplicationEffect as FleetProfileApplicationEffect
from .applications import (
    FleetProfileApplicationProgress as FleetProfileApplicationProgress,
)
from .applications import FleetProfileApplicationResult as FleetProfileApplicationResult
from .applications import FleetProfileChildOperation as FleetProfileChildOperation
from .applications import FleetProfileEffectProgress as FleetProfileEffectProgress
from .applications import FleetProfileEffectState as FleetProfileEffectState
from .applications import (
    FleetProfileIntendedConfiguration as FleetProfileIntendedConfiguration,
)
from .assessment import FleetProfileAdmissionDecision as FleetProfileAdmissionDecision
from .assessment import (
    FleetProfileAssignmentAssessment as FleetProfileAssignmentAssessment,
)
from .assessment import (
    FleetProfileAssignmentPreparation as FleetProfileAssignmentPreparation,
)
from .assessment import FleetProfileAssignmentPreview as FleetProfileAssignmentPreview
from .assessment import (
    FleetProfileCompatibilityDecision as FleetProfileCompatibilityDecision,
)
from .assessment import FleetProfilePlanStep as FleetProfilePlanStep
from .assessment import FleetProfilePlanSummary as FleetProfilePlanSummary
from .assessment import (
    FleetProfilePreparationDecision as FleetProfilePreparationDecision,
)
from .assessment import FleetProfileReason as FleetProfileReason
from .assessment import (
    FleetProfileResourceRequirement as FleetProfileResourceRequirement,
)
from .assessment import FleetProfileScopePreview as FleetProfileScopePreview
from .definitions import FleetAssignmentModelView as FleetAssignmentModelView
from .definitions import FleetAssignmentRecipeView as FleetAssignmentRecipeView
from .definitions import FleetCacheSummary as FleetCacheSummary
from .definitions import FleetNodeView as FleetNodeView
from .definitions import FleetProfileAssignment as FleetProfileAssignment
from .definitions import FleetProfileAssignmentInput as FleetProfileAssignmentInput
from .definitions import FleetProfileAssignmentView as FleetProfileAssignmentView
from .definitions import FleetProfileDefinition as FleetProfileDefinition
from .definitions import FleetProfileDefinitionView as FleetProfileDefinitionView
from .definitions import FleetProfileInput as FleetProfileInput
from .definitions import FleetProfileList as FleetProfileList
from .definitions import FleetProfileNode as FleetProfileNode
from .definitions import FleetProfileReadView as FleetProfileReadView
from .definitions import FleetProfileScope as FleetProfileScope
from .definitions import FleetProfileView as FleetProfileView
from .definitions import SavedProfileProjectionIssue as SavedProfileProjectionIssue
from .definitions import StoredFleetProfileAssignment as StoredFleetProfileAssignment
from .definitions import UnavailableFleetProfileView as UnavailableFleetProfileView
from .effects import (
    FleetProfileAdoptedApplicationEffect as FleetProfileAdoptedApplicationEffect,
)
from .effects import FleetProfileAdoptedStopEffect as FleetProfileAdoptedStopEffect
from .effects import FleetProfileChildProgress as FleetProfileChildProgress
from .effects import FleetProfileEffects as FleetProfileEffects
from .effects import FleetProfileInstallationEffect as FleetProfileInstallationEffect
from .effects import FleetProfilePendingEffect as FleetProfilePendingEffect
from .effects import FleetProfileRunEffect as FleetProfileRunEffect
from .endpoints import (
    FleetProfileEndpointAssignmentIntent as FleetProfileEndpointAssignmentIntent,
)
from .endpoints import (
    FleetProfileEndpointAssignmentView as FleetProfileEndpointAssignmentView,
)
from .endpoints import FleetProfileEndpointIntent as FleetProfileEndpointIntent
from .endpoints import (
    FleetProfileEndpointProjectionIssue as FleetProfileEndpointProjectionIssue,
)
from .endpoints import FleetProfileEndpointsView as FleetProfileEndpointsView
from .review import (
    FleetProfileApplicationCancelRequest as FleetProfileApplicationCancelRequest,
)
from .review import (
    FleetProfileApplicationProjectionIssue as FleetProfileApplicationProjectionIssue,
)
from .review import FleetProfileApplicationView as FleetProfileApplicationView
from .review import FleetProfileLoadRequest as FleetProfileLoadRequest
from .review import FleetProfileLoadReview as FleetProfileLoadReview
from .review import FleetProfilePreview as FleetProfilePreview
from .review import FleetProfileReviewedDecision as FleetProfileReviewedDecision
from .switch_state import FleetProfileAssignmentFailure as FleetProfileAssignmentFailure
from .switch_state import FleetProfileChildResult as FleetProfileChildResult
from .switch_state import FleetProfileStepResult as FleetProfileStepResult
from .switch_state import (
    FleetProfileSwitchAdapterResult as FleetProfileSwitchAdapterResult,
)
from .switch_state import (
    FleetProfileSwitchAdapterState as FleetProfileSwitchAdapterState,
)
from .switch_state import FleetProfileSwitchChildKind as FleetProfileSwitchChildKind
from .switch_state import FleetProfileSwitchChildResult as FleetProfileSwitchChildResult
from .switch_state import FleetProfileSwitchChildState as FleetProfileSwitchChildState
from .switch_state import (
    FleetProfileSwitchPendingChild as FleetProfileSwitchPendingChild,
)
from .switch_state import FleetProfileSwitchQueueItem as FleetProfileSwitchQueueItem
from .switch_state import (
    FleetProfileVerificationResult as FleetProfileVerificationResult,
)
from .vocabulary import _DIGEST_PATTERN as _DIGEST_PATTERN
from .vocabulary import _NODE_PATTERN as _NODE_PATTERN
from .vocabulary import _UUID_PATTERN as _UUID_PATTERN
from .vocabulary import FLEET_PROFILE_ENDED_STATES as FLEET_PROFILE_ENDED_STATES
from .vocabulary import MAX_PROFILE_WARNINGS as MAX_PROFILE_WARNINGS
from .vocabulary import Alias as Alias
from .vocabulary import Description as Description
from .vocabulary import DesiredAssignmentStateField as DesiredAssignmentStateField
from .vocabulary import Digest as Digest
from .vocabulary import FleetProfileAction as FleetProfileAction
from .vocabulary import FleetProfileAssignmentState as FleetProfileAssignmentState
from .vocabulary import FleetProfileCancellationState as FleetProfileCancellationState
from .vocabulary import FleetProfileChildPhase as FleetProfileChildPhase
from .vocabulary import FleetProfileEndpointState as FleetProfileEndpointState
from .vocabulary import FleetProfileInstallationPolicy as FleetProfileInstallationPolicy
from .vocabulary import FleetProfileOperationKind as FleetProfileOperationKind
from .vocabulary import FleetProfileOperationState as FleetProfileOperationState
from .vocabulary import FleetProfilePlanStepKind as FleetProfilePlanStepKind
from .vocabulary import FleetProfileSupersedeCode as FleetProfileSupersedeCode
from .vocabulary import LabelName as LabelName
from .vocabulary import LabelValue as LabelValue
from .vocabulary import Name as Name
from .vocabulary import NodeId as NodeId
from .vocabulary import OptionChoices as OptionChoices
from .vocabulary import OptionSlug as OptionSlug
from .vocabulary import RecipeSelector as RecipeSelector
from .vocabulary import UuidId as UuidId
from .vocabulary import (
    profile_switch_child_request_key as profile_switch_child_request_key,
)

__all__ = [
    "FLEET_PROFILE_ENDED_STATES",
    "FleetProfileAdoptedApplicationEffect",
    "FleetProfileAdoptedStopEffect",
    "FleetProfileApplicationCancelRequest",
    "FleetProfileApplicationCancellationIntent",
    "FleetProfileApplicationCancellationView",
    "FleetProfileApplicationEffect",
    "FleetProfileApplicationProgress",
    "FleetProfileApplicationProjectionIssue",
    "FleetProfileApplicationResult",
    "FleetProfileApplicationView",
    "FleetProfileAssignment",
    "FleetProfileAssignmentInput",
    "FleetProfileAssignmentPreparation",
    "FleetProfileAssignmentPreview",
    "FleetProfileAssignmentView",
    "FleetProfileChildOperation",
    "FleetProfileChildProgress",
    "FleetProfileChildResult",
    "FleetProfileCompatibilityDecision",
    "FleetProfileEffectProgress",
    "FleetProfileEffects",
    "FleetProfileInput",
    "FleetProfileInstallationEffect",
    "FleetProfileIntendedConfiguration",
    "FleetProfileList",
    "FleetProfileLoadRequest",
    "FleetProfileNode",
    "FleetProfilePendingEffect",
    "FleetProfilePlanStep",
    "FleetProfilePlanSummary",
    "FleetProfilePreparationDecision",
    "FleetProfilePreview",
    "FleetProfileReason",
    "FleetProfileReviewedDecision",
    "FleetProfileRunEffect",
    "FleetProfileScope",
    "FleetProfileScopePreview",
    "FleetProfileStepResult",
    "FleetProfileSupersedeCode",
    "FleetProfileSwitchAdapter",
    "FleetProfileSwitchAdapterResult",
    "FleetProfileSwitchAdapterState",
    "FleetProfileSwitchChildResult",
    "FleetProfileSwitchChildState",
    "FleetProfileSwitchPendingChild",
    "FleetProfileSwitchQueueItem",
    "FleetProfileVerificationResult",
    "FleetProfileView",
]
