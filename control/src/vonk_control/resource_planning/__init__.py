"""Evidence based capacity planning for canonical recipe settings.

Public facade; patch dependencies in their implementation modules.
"""

import hashlib as hashlib
import json as json
from collections.abc import Mapping as Mapping
from collections.abc import Sequence as Sequence
from dataclasses import dataclass as dataclass
from dataclasses import field as field
from dataclasses import replace as replace
from datetime import datetime as datetime
from typing import Literal as Literal
from typing import TypeGuard as TypeGuard
from typing import get_args as get_args

from pydantic import ValidationError as ValidationError
from vonk_agent_protocol import ResourceBlockerCode as ResourceBlockerCode
from vonk_agent_protocol import ResourcePlanningCode as ResourcePlanningCode
from vonk_agent_protocol import ResourceTerm as ResourceTerm
from vonk_agent_protocol import ResourceTermProblem as ResourceTermProblem
from vonk_agent_protocol import resource_term_code as resource_term_code
from vonk_agent_protocol.inventory import MemoryPool as MemoryPool
from vonk_forge_contracts import ModelDefinition as ModelDefinition
from vonk_forge_contracts import RecipeDefinition as RecipeDefinition
from vonk_forge_contracts.recipe import RecipeDiskResources as RecipeDiskResources
from vonk_forge_contracts.recipe import RecipeMemoryResources as RecipeMemoryResources
from vonk_forge_contracts.recipe import Scalar as Scalar

from ..bounded_json import require_integer as require_integer
from ..resource_planning_contract import (
    ResourceRecipeProjection as ResourceRecipeProjection,
)
from ..run_switch_contract import (
    EffectiveSettingsSelection as EffectiveSettingsSelection,
)
from ..run_switch_contract import MemoryKind as MemoryKind
from ..run_switch_contract import RunSwitchChangeEffect as RunSwitchChangeEffect
from .capacity import _minimum_known as _minimum_known
from .capacity import (
    _resident_usage_uncertainty_detail as _resident_usage_uncertainty_detail,
)
from .capacity import plan_capacity as plan_capacity
from .demand import _resource_evidence as _resource_evidence
from .demand import _selected_model_bytes as _selected_model_bytes
from .demand import _term as _term
from .demand import resource_demand as resource_demand
from .identity import _canonical as _canonical
from .identity import _digest as _digest
from .identity import _is_digest as _is_digest
from .memory import memory_capacity_snapshot as memory_capacity_snapshot
from .memory import memory_requirement as memory_requirement
from .memory_kinds import memory_reservation_kind as memory_reservation_kind
from .memory_kinds import memory_reservation_kinds as memory_reservation_kinds
from .preflight import plan_resource_preflight as plan_resource_preflight
from .reasons import _reason as _reason
from .reasons import _same_memory_kind as _same_memory_kind
from .settings import classify_preparation_effects as classify_preparation_effects
from .settings import resolve_effective_settings as resolve_effective_settings
from .types import _CHANGE_EFFECTS as _CHANGE_EFFECTS
from .types import ENVELOPE_EXCEEDS_CAPACITY as ENVELOPE_EXCEEDS_CAPACITY
from .types import ENVELOPE_UNVERIFIED as ENVELOPE_UNVERIFIED
from .types import PLATFORM_MEMORY_FLOOR_BYTES as PLATFORM_MEMORY_FLOOR_BYTES
from .types import CapacityPlan as CapacityPlan
from .types import CapacitySnapshot as CapacitySnapshot
from .types import Effect as Effect
from .types import EffectiveResourceSettings as EffectiveResourceSettings
from .types import EvidenceState as EvidenceState
from .types import InstallationDiskRequirement as InstallationDiskRequirement
from .types import MemoryRequirement as MemoryRequirement
from .types import MemoryReservationTotals as MemoryReservationTotals
from .types import NodeCapacityPlan as NodeCapacityPlan
from .types import ParallelismSettings as ParallelismSettings
from .types import PlannedStopRelease as PlannedStopRelease
from .types import PreparationDecision as PreparationDecision
from .types import ResourceDemand as ResourceDemand
from .types import ResourceEvidence as ResourceEvidence
from .types import ResourcePreflightPlan as ResourcePreflightPlan
from .types import ResourceReason as ResourceReason
from .types import SettingsResolution as SettingsResolution
from .types import UnknownRunMemoryResidual as UnknownRunMemoryResidual
from .types import installation_disk_requirement as installation_disk_requirement
