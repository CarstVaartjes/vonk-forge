"""Reason-code discovery, adoption and wire vocabulary carrier."""

from __future__ import annotations

from collections.abc import Mapping
from functools import cache

from ..wire_model import WireEnum, WireModel
from .admission import (
    AdmissionCode,
    CertificateCode,
    ControllerErrorCode,
    InstallAdmissionCode,
    InstallDegradedReason,
    UninstallPlanCode,
)
from .catalog import (
    CatalogCode,
    CatalogSyncCode,
    LibraryAssessmentCode,
    LibraryProjectionCode,
)
from .evidence import (
    AgentEvidenceCode,
    ArtifactLifecycleCode,
    NodeOfflineReason,
    OperationFailureCode,
    RunDegradedReason,
)
from .fleet import ClusterMappingCode, DistributionCode, TopologyCode
from .helpers import HelperErrorCode, HelperOperationCode
from .images import ImageStoreCode, PrebuiltImageCode
from .model_cache import CacheReferenceReason, ModelCacheBlockerCode, ModelCacheCode
from .profiles import ProfileReasonCode, ProjectionCode
from .recipes import (
    RecipeBuildCode,
    RecipeImageCode,
    RecipeOperationCode,
    RecipePackageCode,
    RecipeUpdateCode,
    ReconcileCode,
)
from .resources import (
    ResourcePlanningCode,
    ResourceTerm,
    ResourceTermProblem,
    StorageDemandCode,
)
from .runs import RunSwitchCode, StopPlanCode, SupersedeCode
from .runtime import RuntimeImageCode, RuntimePreflightCode, RuntimePreflightFindingCode
from .sources import SourceBundleCode, SourcePolicyCode

#: Every domain enum, in the order the wire schema lists them.
REASON_CODE_ENUMS: tuple[type[WireEnum], ...] = (
    AdmissionCode,
    AgentEvidenceCode,
    ArtifactLifecycleCode,
    CacheReferenceReason,
    CertificateCode,
    CatalogCode,
    CatalogSyncCode,
    ClusterMappingCode,
    ControllerErrorCode,
    DistributionCode,
    HelperErrorCode,
    HelperOperationCode,
    ImageStoreCode,
    InstallAdmissionCode,
    InstallDegradedReason,
    LibraryAssessmentCode,
    LibraryProjectionCode,
    ModelCacheBlockerCode,
    ModelCacheCode,
    NodeOfflineReason,
    OperationFailureCode,
    PrebuiltImageCode,
    ProfileReasonCode,
    ProjectionCode,
    RecipeBuildCode,
    RecipeImageCode,
    RecipeOperationCode,
    RecipePackageCode,
    RecipeUpdateCode,
    ReconcileCode,
    ResourcePlanningCode,
    ResourceTerm,
    ResourceTermProblem,
    RunDegradedReason,
    RunSwitchCode,
    RuntimeImageCode,
    RuntimePreflightCode,
    RuntimePreflightFindingCode,
    SourceBundleCode,
    SourcePolicyCode,
    StopPlanCode,
    StorageDemandCode,
    SupersedeCode,
    TopologyCode,
    UninstallPlanCode,
)


@cache
def _index() -> Mapping[str, WireEnum]:
    index: dict[str, WireEnum] = {}
    for enum in REASON_CODE_ENUMS:
        for member in enum:
            index.setdefault(member.value, member)
    return index


def reason_code_of(word: str) -> WireEnum | None:
    """The contract member that spells ``word``, or ``None`` for a word no domain owns.

    A stored or relayed code that this release does not know is never a failure:
    the reader keeps the word as text and shows it, so a newer writer's code
    survives an older reader.
    """

    return _index().get(adopt_reason_code(word))


#: Spellings an older Controller stored that a later one spells differently.  A
#: reader adopts them (:func:`adopt_reason_code`); nothing writes them any more.
RETIRED_CODE_SPELLINGS: Mapping[str, WireEnum] = {
    "run-switch.installation_preparation_unavailable": (
        RunSwitchCode.INSTALLATION_PREPARATION_UNAVAILABLE
    ),
    "run-switch.stop_plan_unavailable": RunSwitchCode.STOP_PLAN_UNAVAILABLE,
}


def adopt_reason_code(stored: str) -> str:
    """The current spelling of a stored code (the word itself when it is current)."""

    adopted = RETIRED_CODE_SPELLINGS.get(stored)
    return stored if adopted is None else str(adopted.value)


#: The prefix every :class:`RuntimePreflightFindingCode` word carries.  An agent
#: that predates the enum sent the bare word (``available``,
#: ``helper_operation_io``) and, for the podman and probe diagnostics, a kebab-case
#: spelling (``proc-mount-denied``).
_FINDING_PREFIX = "preflight_finding."

#: The words an older agent reported as free text, each read as the member that
#: names it.  This is the one legacy adapter for a finding code; nothing writes
#: these spellings any more.  It is scoped to the finding code (not folded into
#: :data:`RETIRED_CODE_SPELLINGS`) because a bare legacy word such as
#: ``helper_grant_invalid`` is also a member of another domain.
RETIRED_FINDING_CODE_SPELLINGS: Mapping[str, RuntimePreflightFindingCode] = {
    **{
        member.value.removeprefix(_FINDING_PREFIX): member
        for member in RuntimePreflightFindingCode
    },
    **{
        word: RuntimePreflightFindingCode(_FINDING_PREFIX + word.replace("-", "_"))
        for word in (
            "build-step-failed",
            "capabilities-not-zero",
            "memory-limit-exceeded",
            "mount-namespace-unavailable",
            "no-new-privileges-unavailable",
            "nonzero-without-output",
            "oci-runtime-unavailable",
            "patch-rejected",
            "permission-denied",
            "proc-mount-denied",
            "proc-unavailable",
            "storage-driver-failure",
            "subordinate-id-mapping-unavailable",
            "systemd-scope-failure",
            "temporary-directory-unavailable",
            "temporary-storage-exhausted",
            "unclassified-podman-build-failure",
            "user-namespace-denied",
            "user-service-manager-unavailable",
        )
    },
}


def adopt_preflight_finding_code(stored: str) -> RuntimePreflightFindingCode:
    """The member that names a finding code read from an agent.

    A current spelling and a retired free-text spelling both map to their member.
    A word this release does not know (a newer agent's, or free text no member
    spells) reads as ``UNCLASSIFIED``: the finding is kept and shown rather than
    refusing the whole preflight result.
    """

    try:
        return RuntimePreflightFindingCode(stored)
    except ValueError:
        return RETIRED_FINDING_CODE_SPELLINGS.get(
            stored, RuntimePreflightFindingCode.UNCLASSIFIED
        )


def run_switch_code(inner: str) -> RunSwitchCode:
    """The Run/Switch code that wraps a code of another domain.

    Run/Switch shows a resource, reconcile, stop or uninstall code as its own
    (``run-switch.`` plus the inner code).  Every inner code of those domains is a
    member; a word no domain owns maps to ``REASON_UNCLASSIFIED`` and the detail
    text carries the cause.
    """

    return _RUN_SWITCH_WRAPPED.get(str(inner), RunSwitchCode.REASON_UNCLASSIFIED)


_RUN_SWITCH_WRAPPED: Mapping[str, RunSwitchCode] = {
    member.value.removeprefix("run-switch."): member for member in RunSwitchCode
}


def resource_term_code(
    term: ResourceTerm, problem: ResourceTermProblem
) -> ResourcePlanningCode:
    """The resource-planning code for one problem with one effective setting."""

    return ResourcePlanningCode(f"resource.{term.value}_{problem.value}")


class ReasonCodeVocabulary(WireModel):
    """Carrier that publishes every reason-code enum into the wire schema.

    The model is never sent: it exists so the schema exporter, the Rust
    generator and the OpenAPI/TypeScript generators emit each closed word set
    from this one module.
    """

    admission_code: AdmissionCode
    agent_evidence_code: AgentEvidenceCode
    artifact_lifecycle_code: ArtifactLifecycleCode
    cache_reference_reason: CacheReferenceReason
    certificate_code: CertificateCode
    catalog_code: CatalogCode
    catalog_sync_code: CatalogSyncCode
    cluster_mapping_code: ClusterMappingCode
    controller_error_code: ControllerErrorCode
    distribution_code: DistributionCode
    helper_operation_code: HelperOperationCode
    helper_error_code: HelperErrorCode
    image_store_code: ImageStoreCode
    install_admission_code: InstallAdmissionCode
    install_degraded_reason: InstallDegradedReason
    library_assessment_code: LibraryAssessmentCode
    library_projection_code: LibraryProjectionCode
    model_cache_blocker_code: ModelCacheBlockerCode
    model_cache_code: ModelCacheCode
    node_offline_reason: NodeOfflineReason
    operation_failure_code: OperationFailureCode
    prebuilt_image_code: PrebuiltImageCode
    profile_reason_code: ProfileReasonCode
    projection_code: ProjectionCode
    recipe_build_code: RecipeBuildCode
    recipe_image_code: RecipeImageCode
    recipe_operation_code: RecipeOperationCode
    recipe_package_code: RecipePackageCode
    recipe_update_code: RecipeUpdateCode
    reconcile_code: ReconcileCode
    resource_planning_code: ResourcePlanningCode
    resource_term: ResourceTerm
    resource_term_problem: ResourceTermProblem
    run_degraded_reason: RunDegradedReason
    run_switch_code: RunSwitchCode
    runtime_image_code: RuntimeImageCode
    runtime_preflight_code: RuntimePreflightCode
    runtime_preflight_finding_code: RuntimePreflightFindingCode
    source_bundle_code: SourceBundleCode
    source_policy_code: SourcePolicyCode
    stop_plan_code: StopPlanCode
    storage_demand_code: StorageDemandCode
    supersede_code: SupersedeCode
    topology_code: TopologyCode
    uninstall_plan_code: UninstallPlanCode


__all__ = [
    "REASON_CODE_ENUMS",
    "RETIRED_CODE_SPELLINGS",
    "RETIRED_FINDING_CODE_SPELLINGS",
    "AdmissionCode",
    "AgentEvidenceCode",
    "ArtifactLifecycleCode",
    "CacheReferenceReason",
    "CatalogCode",
    "CatalogSyncCode",
    "CertificateCode",
    "ClusterMappingCode",
    "ControllerErrorCode",
    "DistributionCode",
    "HelperErrorCode",
    "HelperOperationCode",
    "ImageStoreCode",
    "InstallAdmissionCode",
    "InstallDegradedReason",
    "LibraryAssessmentCode",
    "LibraryProjectionCode",
    "ModelCacheBlockerCode",
    "ModelCacheCode",
    "NodeOfflineReason",
    "OperationFailureCode",
    "PrebuiltImageCode",
    "ProfileReasonCode",
    "ProjectionCode",
    "ReasonCodeVocabulary",
    "RecipeBuildCode",
    "RecipeImageCode",
    "RecipeOperationCode",
    "RecipePackageCode",
    "RecipeUpdateCode",
    "ReconcileCode",
    "ResourcePlanningCode",
    "ResourceTerm",
    "ResourceTermProblem",
    "RunDegradedReason",
    "RunSwitchCode",
    "RuntimeImageCode",
    "RuntimePreflightCode",
    "RuntimePreflightFindingCode",
    "SourceBundleCode",
    "SourcePolicyCode",
    "StopPlanCode",
    "StorageDemandCode",
    "SupersedeCode",
    "TopologyCode",
    "UninstallPlanCode",
    "adopt_preflight_finding_code",
    "adopt_reason_code",
    "reason_code_of",
    "resource_term_code",
    "run_switch_code",
]
