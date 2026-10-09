"""Closed reason, blocker, warning and attention codes shared by Python, Rust and TypeScript.

Every code the Controller shows an operator as *why* something is waiting, refused,
blocked or degraded is a member of one of these enums, grouped by the domain that
raises it.  ``scripts/export-agent-wire-schema`` publishes them through
:class:`ReasonCodeVocabulary`, ``scripts/generate-agent-wire`` turns that into Rust
types and ``scripts/generate-control-clients`` into OpenAPI and TypeScript, so no
consumer spells a code by hand.  The vocabulary ratchet
(``control/tests/vocabulary_literals.py``) fails a string literal that equals a
member and a string constant in any code position of the Controller.

The values are the spellings already stored in rows and shown by the API: this
module adds no new word, it only closes the set.  The exceptions are the agent's
runtime preflight finding codes (:class:`RuntimePreflightFindingCode`), which an
agent used to write as bare free text and which now carry one domain prefix so they
cannot collide with another domain's word; the old spellings are adopted on read
(:func:`adopt_preflight_finding_code`).  A code is a ``StrEnum``, so it
compares, hashes and serialises as its word.
"""

from .admission import AdmissionCode as AdmissionCode
from .admission import CertificateCode as CertificateCode
from .admission import ControllerErrorCode as ControllerErrorCode
from .admission import InstallAdmissionCode as InstallAdmissionCode
from .admission import InstallDegradedReason as InstallDegradedReason
from .admission import UninstallPlanCode as UninstallPlanCode
from .catalog import CatalogCode as CatalogCode
from .catalog import CatalogSyncCode as CatalogSyncCode
from .catalog import LibraryAssessmentCode as LibraryAssessmentCode
from .catalog import LibraryProjectionCode as LibraryProjectionCode
from .evidence import AgentEvidenceCode as AgentEvidenceCode
from .evidence import ArtifactLifecycleCode as ArtifactLifecycleCode
from .evidence import NodeOfflineReason as NodeOfflineReason
from .evidence import OperationFailureCode as OperationFailureCode
from .evidence import RunDegradedReason as RunDegradedReason
from .fleet import ClusterMappingCode as ClusterMappingCode
from .fleet import DistributionCode as DistributionCode
from .fleet import TopologyCode as TopologyCode
from .helpers import HelperErrorCode as HelperErrorCode
from .helpers import HelperOperationCode as HelperOperationCode
from .images import ImageStoreCode as ImageStoreCode
from .images import PrebuiltImageCode as PrebuiltImageCode
from .model_cache import CacheReferenceReason as CacheReferenceReason
from .model_cache import ModelCacheBlockerCode as ModelCacheBlockerCode
from .model_cache import ModelCacheCode as ModelCacheCode
from .profiles import ProfileReasonCode as ProfileReasonCode
from .profiles import ProjectionCode as ProjectionCode
from .recipes import RecipeBuildCode as RecipeBuildCode
from .recipes import RecipeImageCode as RecipeImageCode
from .recipes import RecipeOperationCode as RecipeOperationCode
from .recipes import RecipePackageCode as RecipePackageCode
from .recipes import RecipeUpdateCode as RecipeUpdateCode
from .recipes import ReconcileCode as ReconcileCode
from .resources import ResourcePlanningCode as ResourcePlanningCode
from .resources import ResourceTerm as ResourceTerm
from .resources import ResourceTermProblem as ResourceTermProblem
from .resources import StorageDemandCode as StorageDemandCode
from .runs import RunSwitchCode as RunSwitchCode
from .runs import StopPlanCode as StopPlanCode
from .runs import SupersedeCode as SupersedeCode
from .runtime import RuntimeImageCode as RuntimeImageCode
from .runtime import RuntimePreflightCode as RuntimePreflightCode
from .runtime import RuntimePreflightFindingCode as RuntimePreflightFindingCode
from .sources import SourceBundleCode as SourceBundleCode
from .sources import SourcePolicyCode as SourcePolicyCode
from .vocabulary import _FINDING_PREFIX as _FINDING_PREFIX
from .vocabulary import _RUN_SWITCH_WRAPPED as _RUN_SWITCH_WRAPPED
from .vocabulary import REASON_CODE_ENUMS as REASON_CODE_ENUMS
from .vocabulary import RETIRED_CODE_SPELLINGS as RETIRED_CODE_SPELLINGS
from .vocabulary import RETIRED_FINDING_CODE_SPELLINGS as RETIRED_FINDING_CODE_SPELLINGS
from .vocabulary import ReasonCodeVocabulary as ReasonCodeVocabulary
from .vocabulary import __all__ as __all__
from .vocabulary import _index as _index
from .vocabulary import adopt_preflight_finding_code as adopt_preflight_finding_code
from .vocabulary import adopt_reason_code as adopt_reason_code
from .vocabulary import reason_code_of as reason_code_of
from .vocabulary import resource_term_code as resource_term_code
from .vocabulary import run_switch_code as run_switch_code
