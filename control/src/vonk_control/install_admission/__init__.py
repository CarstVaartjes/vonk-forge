"""Role-aware disk admission for one mapping generation and exact OCI build.

Public facade; patch dependencies in their implementation modules.
"""

import hashlib as hashlib
import json as json
from collections.abc import Callable as Callable
from collections.abc import Mapping as Mapping
from collections.abc import Sequence as Sequence
from contextlib import nullcontext as nullcontext
from dataclasses import asdict as asdict
from dataclasses import dataclass as dataclass
from datetime import datetime as datetime

from sqlalchemy import select as select
from sqlalchemy.exc import OperationalError as OperationalError
from sqlalchemy.orm import Session as Session
from sqlalchemy.orm import sessionmaker as sessionmaker
from vonk_agent_protocol import InstallAdmissionCode as InstallAdmissionCode
from vonk_agent_protocol import InstallationNodeState as InstallationNodeState
from vonk_agent_protocol import InstallationState as InstallationState
from vonk_agent_protocol import InvalidRequestError as InvalidRequestError
from vonk_agent_protocol import InvalidRequestReason as InvalidRequestReason
from vonk_agent_protocol import ModelFileState as ModelFileState
from vonk_agent_protocol import ReservationState as ReservationState
from vonk_agent_protocol import RuntimePreflightCode as RuntimePreflightCode
from vonk_agent_protocol import UnknownOutcomeError as UnknownOutcomeError
from vonk_agent_protocol import WaitReason as WaitReason

from ..admission_locking import AdmissionLockBusy as AdmissionLockBusy
from ..admission_locking import AdmissionRowLock as AdmissionRowLock
from ..admission_locking import acquire_admission_keys as acquire_admission_keys
from ..admission_locking import admission_attempts as admission_attempts
from ..admission_locking import is_admission_contention as is_admission_contention
from ..admission_locking import lock_admission_rows as lock_admission_rows
from ..admission_locking import node_admission_key as node_admission_key
from ..categorized_errors import BookkeepingUnknown as BookkeepingUnknown
from ..categorized_errors import InvalidType as InvalidType
from ..categorized_errors import InvalidValue as InvalidValue
from ..categorized_errors import MissingRecord as MissingRecord
from ..cluster_mappings import (
    validate_mapping_parameters as validate_mapping_parameters,
)
from ..content_identity import same_image as same_image
from ..content_identity import same_model_object as same_model_object
from ..disk_reservations import describe_disk_charges as describe_disk_charges
from ..disk_reservations import models_stored_on_node as models_stored_on_node
from ..disk_reservations import outstanding_disk_charges as outstanding_disk_charges
from ..disk_reservations import (
    outstanding_disk_reservation_bytes as outstanding_disk_reservation_bytes,
)
from ..inventory_repository import InventoryRepository as InventoryRepository
from ..inventory_repository import InventorySnapshotView as InventorySnapshotView
from ..legal_admission import territorial_admission as territorial_admission
from ..models import AgentNode as AgentNode
from ..models import CatalogDocumentRevision as CatalogDocumentRevision
from ..models import ClusterMapping as ClusterMapping
from ..models import ClusterMappingNode as ClusterMappingNode
from ..models import InstallationNode as InstallationNode
from ..models import NodeArtifact as NodeArtifact
from ..models import NodeInventorySnapshot as NodeInventorySnapshot
from ..models import RecipeBuild as RecipeBuild
from ..models import RecipeInstallation as RecipeInstallation
from ..models import ResourceReservation as ResourceReservation
from ..profile_capacity import inherited_profile_disk as inherited_profile_disk
from ..recipe_execution_contract import (
    RecipeExecutionContractError as RecipeExecutionContractError,
)
from ..recipe_execution_contract import StoredInstallationPlan as StoredInstallationPlan
from ..recipe_execution_contract import (
    installation_plan_document as installation_plan_document,
)
from ..recipe_execution_contract import (
    parse_stored_installation_plan as parse_stored_installation_plan,
)
from ..recipe_runtime_specs import RecipeRuntimeSpecError as RecipeRuntimeSpecError
from ..recipe_runtime_specs import recipe_topology as recipe_topology
from ..recipe_runtime_specs import resolve_recipe_entities as resolve_recipe_entities
from ..resource_planning import (
    installation_disk_requirement as installation_disk_requirement,
)
from ..runtime_preflight import admission_blockers as admission_blockers
from ..runtime_preflight import latest_result as latest_result
from ..runtime_preflight import recipe_requirements as recipe_requirements
from ..runtime_preflight import request_digest as request_digest
from ..topology import Placement as Placement
from ..topology import TopologyError as TopologyError
from ..topology import validate_topology as validate_topology
from .contracts import (
    _REFRESHABLE_PREFLIGHT_BLOCKERS as _REFRESHABLE_PREFLIGHT_BLOCKERS,
)
from .contracts import _RETRYABLE_INSTALL_BLOCKERS as _RETRYABLE_INSTALL_BLOCKERS
from .contracts import AGENT_UPGRADE_REQUIRED_DETAIL as AGENT_UPGRADE_REQUIRED_DETAIL
from .contracts import IMAGE_PULL_CAPABILITY as IMAGE_PULL_CAPABILITY
from .contracts import UNSETTLED_PLAN_PREFIX as UNSETTLED_PLAN_PREFIX
from .contracts import AdmissionReason as AdmissionReason
from .contracts import InstallAdmissionBusy as InstallAdmissionBusy
from .contracts import InstallEvidenceChanged as InstallEvidenceChanged
from .contracts import InstallNodePlan as InstallNodePlan
from .contracts import InstallPlan as InstallPlan
from .contracts import InstallPlanConflict as InstallPlanConflict
from .contracts import InstallPlanStale as InstallPlanStale
from .contracts import InstallPreflightExpired as InstallPreflightExpired
from .contracts import StoredInstallIdentityDamaged as StoredInstallIdentityDamaged
from .contracts import WireCompiledExecutionPlan as WireCompiledExecutionPlan
from .contracts import _node_document as _node_document
from .identity import _INSTALL_NODE_DIGEST_FIELDS as _INSTALL_NODE_DIGEST_FIELDS
from .identity import _compiled_build_matches as _compiled_build_matches
from .identity import _installation_plan_digest as _installation_plan_digest
from .identity import _installation_plan_identity as _installation_plan_identity
from .identity import _is_nonnegative_json_int as _is_nonnegative_json_int
from .identity import _node_digest_document as _node_digest_document
from .identity import _primary_model_sha256 as _primary_model_sha256
from .identity import (
    installation_plan_digest_from_stored_document as installation_plan_digest_from_stored_document,
)
from .service import InstallAdmissionService as InstallAdmissionService
from .validation import _active_recipe_revision as _active_recipe_revision
from .validation import require_admissible as require_admissible
from .validation import require_same_execution as require_same_execution
