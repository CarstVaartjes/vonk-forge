from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.admission_code import AdmissionCode
from ..models.admission_code import check_admission_code
from ..models.agent_evidence_code import AgentEvidenceCode
from ..models.agent_evidence_code import check_agent_evidence_code
from ..models.artifact_lifecycle_code import ArtifactLifecycleCode
from ..models.artifact_lifecycle_code import check_artifact_lifecycle_code
from ..models.cache_reference_reason import CacheReferenceReason
from ..models.cache_reference_reason import check_cache_reference_reason
from ..models.catalog_code import CatalogCode
from ..models.catalog_code import check_catalog_code
from ..models.catalog_sync_code import CatalogSyncCode
from ..models.catalog_sync_code import check_catalog_sync_code
from ..models.cluster_mapping_code import check_cluster_mapping_code
from ..models.cluster_mapping_code import ClusterMappingCode
from ..models.controller_error_code import check_controller_error_code
from ..models.controller_error_code import ControllerErrorCode
from ..models.distribution_code import check_distribution_code
from ..models.distribution_code import DistributionCode
from ..models.helper_error_code import check_helper_error_code
from ..models.helper_error_code import HelperErrorCode
from ..models.image_store_code import check_image_store_code
from ..models.image_store_code import ImageStoreCode
from ..models.install_admission_code import check_install_admission_code
from ..models.install_admission_code import InstallAdmissionCode
from ..models.install_degraded_reason import check_install_degraded_reason
from ..models.install_degraded_reason import InstallDegradedReason
from ..models.library_assessment_code import check_library_assessment_code
from ..models.library_assessment_code import LibraryAssessmentCode
from ..models.library_projection_code import check_library_projection_code
from ..models.library_projection_code import LibraryProjectionCode
from ..models.model_cache_blocker_code import check_model_cache_blocker_code
from ..models.model_cache_blocker_code import ModelCacheBlockerCode
from ..models.model_cache_code import check_model_cache_code
from ..models.model_cache_code import ModelCacheCode
from ..models.node_offline_reason import check_node_offline_reason
from ..models.node_offline_reason import NodeOfflineReason
from ..models.operation_failure_code import check_operation_failure_code
from ..models.operation_failure_code import OperationFailureCode
from ..models.prebuilt_image_code import check_prebuilt_image_code
from ..models.prebuilt_image_code import PrebuiltImageCode
from ..models.profile_reason_code import check_profile_reason_code
from ..models.profile_reason_code import ProfileReasonCode
from ..models.projection_code import check_projection_code
from ..models.projection_code import ProjectionCode
from ..models.recipe_build_code import check_recipe_build_code
from ..models.recipe_build_code import RecipeBuildCode
from ..models.recipe_image_code import check_recipe_image_code
from ..models.recipe_image_code import RecipeImageCode
from ..models.recipe_operation_code import check_recipe_operation_code
from ..models.recipe_operation_code import RecipeOperationCode
from ..models.recipe_package_code import check_recipe_package_code
from ..models.recipe_package_code import RecipePackageCode
from ..models.recipe_update_code import check_recipe_update_code
from ..models.recipe_update_code import RecipeUpdateCode
from ..models.reconcile_code import check_reconcile_code
from ..models.reconcile_code import ReconcileCode
from ..models.resource_planning_code import check_resource_planning_code
from ..models.resource_planning_code import ResourcePlanningCode
from ..models.resource_term import check_resource_term
from ..models.resource_term import ResourceTerm
from ..models.resource_term_problem import check_resource_term_problem
from ..models.resource_term_problem import ResourceTermProblem
from ..models.run_degraded_reason import check_run_degraded_reason
from ..models.run_degraded_reason import RunDegradedReason
from ..models.run_switch_code import check_run_switch_code
from ..models.run_switch_code import RunSwitchCode
from ..models.runtime_image_code import check_runtime_image_code
from ..models.runtime_image_code import RuntimeImageCode
from ..models.runtime_preflight_code import check_runtime_preflight_code
from ..models.runtime_preflight_code import RuntimePreflightCode
from ..models.runtime_preflight_finding_code import check_runtime_preflight_finding_code
from ..models.runtime_preflight_finding_code import RuntimePreflightFindingCode
from ..models.source_bundle_code import check_source_bundle_code
from ..models.source_bundle_code import SourceBundleCode
from ..models.source_policy_code import check_source_policy_code
from ..models.source_policy_code import SourcePolicyCode
from ..models.stop_plan_code import check_stop_plan_code
from ..models.stop_plan_code import StopPlanCode
from ..models.storage_demand_code import check_storage_demand_code
from ..models.storage_demand_code import StorageDemandCode
from ..models.supersede_code import check_supersede_code
from ..models.supersede_code import SupersedeCode
from ..models.topology_code import check_topology_code
from ..models.topology_code import TopologyCode
from ..models.uninstall_plan_code import check_uninstall_plan_code
from ..models.uninstall_plan_code import UninstallPlanCode
from typing import cast






T = TypeVar("T", bound="ReasonCodeVocabulary")



@_attrs_define
class ReasonCodeVocabulary:
    """ Carrier that publishes every reason-code enum into the wire schema.

    The model is never sent: it exists so the schema exporter, the Rust
    generator and the OpenAPI/TypeScript generators emit each closed word set
    from this one module.

        Attributes:
            admission_code (AdmissionCode): The shared admission lock refused because capacity is held by another admission.
            agent_evidence_code (AgentEvidenceCode): Optional agent evidence that was dropped so the mandatory report is
                kept.

                An agent report carries a mandatory core (identity, lease, capacity, outcome)
                and optional evidence (NICs, the NAS route, fabric details, readings,
                progress, diagnostics). Invalid optional evidence is never a reason to refuse
                the core: the evidence is dropped and one of these words names what was lost,
                on the agent that dropped it and on the Controller that received it.
            artifact_lifecycle_code (ArtifactLifecycleCode): Why an artifact (model file, image archive, blob) cannot be
                removed, referenced or changed right now.
            cache_reference_reason (CacheReferenceReason): What keeps a cached artifact from being removed.
            catalog_code (CatalogCode): Refusals of the recipe catalog and the recipe library documents.
            catalog_sync_code (CatalogSyncCode): Catalog synchronisation refusals and per-item problems.
            cluster_mapping_code (ClusterMappingCode): Refusals of a cluster mapping (recipe-to-Spark assignment) request.
            controller_error_code (ControllerErrorCode): Generic Controller request and fleet-operation problem codes.
            distribution_code (DistributionCode): Why a distribution assignment object cannot be served to a Spark.
            helper_error_code (HelperErrorCode): Every code the privileged helper, or the agent speaking about it, names as
                an error.

                The helper builds its rejection from a member; the agent reads a reply code
                through the enum, so a word outside it is a malformed rejection, never a code
                the agent invents or forwards.  ``runtime_helper_*`` is the spelling of an
                agent-side cause in failure evidence.
            image_store_code (ImageStoreCode): Refusals and damage found by the Controller OCI image store.
            install_admission_code (InstallAdmissionCode): Why an installation is not admitted (or is waiting) on a Spark.
            install_degraded_reason (InstallDegradedReason): Why an installation is shown partial in the fleet projection.
            library_assessment_code (LibraryAssessmentCode): Why a library entry is not assessed runnable on the current
                fleet.
            library_projection_code (LibraryProjectionCode): Reasons the library projection truncates what it lists.
            model_cache_blocker_code (ModelCacheBlockerCode): Why a model download plan is blocked or waiting.
            model_cache_code (ModelCacheCode): Model cache resolution, storage, removal and operation problems.
            node_offline_reason (NodeOfflineReason): Why a node is shown offline in the fleet projection.
            operation_failure_code (OperationFailureCode): Error codes of a stored operation failure evidence record.
            prebuilt_image_code (PrebuiltImageCode): Why a prebuilt runtime image is, or is not, used.
            profile_reason_code (ProfileReasonCode): Reasons a fleet profile cannot be applied or is waiting.
            projection_code (ProjectionCode): Warnings and attention items of the fleet and library projections.
            recipe_build_code (RecipeBuildCode): Recipe image build planning and recording problems.
            recipe_image_code (RecipeImageCode): Runtime image availability, preparation and cache-removal problems.
            recipe_operation_code (RecipeOperationCode): Recipe operation conflicts.
            recipe_package_code (RecipePackageCode): Recipe package download and verification problems.
            recipe_update_code (RecipeUpdateCode): Recipe update batch problems.
            reconcile_code (ReconcileCode): Why an installation reconcile is blocked.
            resource_planning_code (ResourcePlanningCode): Resource planning (memory, disk, parallelism) refusals and
                unknowns.
            resource_term (ResourceTerm): The effective settings whose capacity cost the resource planner derives.
            resource_term_problem (ResourceTermProblem): What is wrong with the evidence for one resource term.
            run_degraded_reason (RunDegradedReason): Why a run is shown degraded in the fleet projection.
            run_switch_code (RunSwitchCode): Run/Switch phase blockers, waits and failure codes.
            runtime_image_code (RuntimeImageCode): Runtime image preparation, receipt and registry problems.
            runtime_preflight_code (RuntimePreflightCode): Runtime preflight blockers.
            runtime_preflight_finding_code (RuntimePreflightFindingCode): Why one runtime preflight capability passed,
                failed or stayed unknown, as the agent reports it.

                The agent builds every finding from a member; the Controller reads a code an
                older agent sent as free text through :func:`adopt_preflight_finding_code`.
            source_bundle_code (SourceBundleCode): Recipe source bundle validation problems.
            source_policy_code (SourcePolicyCode): Findings of the build source policy (Dockerfile and Compose rules).
            stop_plan_code (StopPlanCode): Why a stop plan is stale or cannot be taken.
            storage_demand_code (StorageDemandCode): Storage demand outcomes of an admission.
            supersede_code (SupersedeCode): Why a fleet profile application was superseded by newer intent.
            topology_code (TopologyCode): Topology planning refusals.
            uninstall_plan_code (UninstallPlanCode): Why an uninstall plan is blocked or incomplete.
     """

    admission_code: AdmissionCode
    agent_evidence_code: AgentEvidenceCode
    artifact_lifecycle_code: ArtifactLifecycleCode
    cache_reference_reason: CacheReferenceReason
    catalog_code: CatalogCode
    catalog_sync_code: CatalogSyncCode
    cluster_mapping_code: ClusterMappingCode
    controller_error_code: ControllerErrorCode
    distribution_code: DistributionCode
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





    def to_dict(self) -> dict[str, Any]:
        admission_code: str = self.admission_code

        agent_evidence_code: str = self.agent_evidence_code

        artifact_lifecycle_code: str = self.artifact_lifecycle_code

        cache_reference_reason: str = self.cache_reference_reason

        catalog_code: str = self.catalog_code

        catalog_sync_code: str = self.catalog_sync_code

        cluster_mapping_code: str = self.cluster_mapping_code

        controller_error_code: str = self.controller_error_code

        distribution_code: str = self.distribution_code

        helper_error_code: str = self.helper_error_code

        image_store_code: str = self.image_store_code

        install_admission_code: str = self.install_admission_code

        install_degraded_reason: str = self.install_degraded_reason

        library_assessment_code: str = self.library_assessment_code

        library_projection_code: str = self.library_projection_code

        model_cache_blocker_code: str = self.model_cache_blocker_code

        model_cache_code: str = self.model_cache_code

        node_offline_reason: str = self.node_offline_reason

        operation_failure_code: str = self.operation_failure_code

        prebuilt_image_code: str = self.prebuilt_image_code

        profile_reason_code: str = self.profile_reason_code

        projection_code: str = self.projection_code

        recipe_build_code: str = self.recipe_build_code

        recipe_image_code: str = self.recipe_image_code

        recipe_operation_code: str = self.recipe_operation_code

        recipe_package_code: str = self.recipe_package_code

        recipe_update_code: str = self.recipe_update_code

        reconcile_code: str = self.reconcile_code

        resource_planning_code: str = self.resource_planning_code

        resource_term: str = self.resource_term

        resource_term_problem: str = self.resource_term_problem

        run_degraded_reason: str = self.run_degraded_reason

        run_switch_code: str = self.run_switch_code

        runtime_image_code: str = self.runtime_image_code

        runtime_preflight_code: str = self.runtime_preflight_code

        runtime_preflight_finding_code: str = self.runtime_preflight_finding_code

        source_bundle_code: str = self.source_bundle_code

        source_policy_code: str = self.source_policy_code

        stop_plan_code: str = self.stop_plan_code

        storage_demand_code: str = self.storage_demand_code

        supersede_code: str = self.supersede_code

        topology_code: str = self.topology_code

        uninstall_plan_code: str = self.uninstall_plan_code


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "admission_code": admission_code,
            "agent_evidence_code": agent_evidence_code,
            "artifact_lifecycle_code": artifact_lifecycle_code,
            "cache_reference_reason": cache_reference_reason,
            "catalog_code": catalog_code,
            "catalog_sync_code": catalog_sync_code,
            "cluster_mapping_code": cluster_mapping_code,
            "controller_error_code": controller_error_code,
            "distribution_code": distribution_code,
            "helper_error_code": helper_error_code,
            "image_store_code": image_store_code,
            "install_admission_code": install_admission_code,
            "install_degraded_reason": install_degraded_reason,
            "library_assessment_code": library_assessment_code,
            "library_projection_code": library_projection_code,
            "model_cache_blocker_code": model_cache_blocker_code,
            "model_cache_code": model_cache_code,
            "node_offline_reason": node_offline_reason,
            "operation_failure_code": operation_failure_code,
            "prebuilt_image_code": prebuilt_image_code,
            "profile_reason_code": profile_reason_code,
            "projection_code": projection_code,
            "recipe_build_code": recipe_build_code,
            "recipe_image_code": recipe_image_code,
            "recipe_operation_code": recipe_operation_code,
            "recipe_package_code": recipe_package_code,
            "recipe_update_code": recipe_update_code,
            "reconcile_code": reconcile_code,
            "resource_planning_code": resource_planning_code,
            "resource_term": resource_term,
            "resource_term_problem": resource_term_problem,
            "run_degraded_reason": run_degraded_reason,
            "run_switch_code": run_switch_code,
            "runtime_image_code": runtime_image_code,
            "runtime_preflight_code": runtime_preflight_code,
            "runtime_preflight_finding_code": runtime_preflight_finding_code,
            "source_bundle_code": source_bundle_code,
            "source_policy_code": source_policy_code,
            "stop_plan_code": stop_plan_code,
            "storage_demand_code": storage_demand_code,
            "supersede_code": supersede_code,
            "topology_code": topology_code,
            "uninstall_plan_code": uninstall_plan_code,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        admission_code = check_admission_code(d.pop("admission_code"))




        agent_evidence_code = check_agent_evidence_code(d.pop("agent_evidence_code"))




        artifact_lifecycle_code = check_artifact_lifecycle_code(d.pop("artifact_lifecycle_code"))




        cache_reference_reason = check_cache_reference_reason(d.pop("cache_reference_reason"))




        catalog_code = check_catalog_code(d.pop("catalog_code"))




        catalog_sync_code = check_catalog_sync_code(d.pop("catalog_sync_code"))




        cluster_mapping_code = check_cluster_mapping_code(d.pop("cluster_mapping_code"))




        controller_error_code = check_controller_error_code(d.pop("controller_error_code"))




        distribution_code = check_distribution_code(d.pop("distribution_code"))




        helper_error_code = check_helper_error_code(d.pop("helper_error_code"))




        image_store_code = check_image_store_code(d.pop("image_store_code"))




        install_admission_code = check_install_admission_code(d.pop("install_admission_code"))




        install_degraded_reason = check_install_degraded_reason(d.pop("install_degraded_reason"))




        library_assessment_code = check_library_assessment_code(d.pop("library_assessment_code"))




        library_projection_code = check_library_projection_code(d.pop("library_projection_code"))




        model_cache_blocker_code = check_model_cache_blocker_code(d.pop("model_cache_blocker_code"))




        model_cache_code = check_model_cache_code(d.pop("model_cache_code"))




        node_offline_reason = check_node_offline_reason(d.pop("node_offline_reason"))




        operation_failure_code = check_operation_failure_code(d.pop("operation_failure_code"))




        prebuilt_image_code = check_prebuilt_image_code(d.pop("prebuilt_image_code"))




        profile_reason_code = check_profile_reason_code(d.pop("profile_reason_code"))




        projection_code = check_projection_code(d.pop("projection_code"))




        recipe_build_code = check_recipe_build_code(d.pop("recipe_build_code"))




        recipe_image_code = check_recipe_image_code(d.pop("recipe_image_code"))




        recipe_operation_code = check_recipe_operation_code(d.pop("recipe_operation_code"))




        recipe_package_code = check_recipe_package_code(d.pop("recipe_package_code"))




        recipe_update_code = check_recipe_update_code(d.pop("recipe_update_code"))




        reconcile_code = check_reconcile_code(d.pop("reconcile_code"))




        resource_planning_code = check_resource_planning_code(d.pop("resource_planning_code"))




        resource_term = check_resource_term(d.pop("resource_term"))




        resource_term_problem = check_resource_term_problem(d.pop("resource_term_problem"))




        run_degraded_reason = check_run_degraded_reason(d.pop("run_degraded_reason"))




        run_switch_code = check_run_switch_code(d.pop("run_switch_code"))




        runtime_image_code = check_runtime_image_code(d.pop("runtime_image_code"))




        runtime_preflight_code = check_runtime_preflight_code(d.pop("runtime_preflight_code"))




        runtime_preflight_finding_code = check_runtime_preflight_finding_code(d.pop("runtime_preflight_finding_code"))




        source_bundle_code = check_source_bundle_code(d.pop("source_bundle_code"))




        source_policy_code = check_source_policy_code(d.pop("source_policy_code"))




        stop_plan_code = check_stop_plan_code(d.pop("stop_plan_code"))




        storage_demand_code = check_storage_demand_code(d.pop("storage_demand_code"))




        supersede_code = check_supersede_code(d.pop("supersede_code"))




        topology_code = check_topology_code(d.pop("topology_code"))




        uninstall_plan_code = check_uninstall_plan_code(d.pop("uninstall_plan_code"))




        reason_code_vocabulary = cls(
            admission_code=admission_code,
            agent_evidence_code=agent_evidence_code,
            artifact_lifecycle_code=artifact_lifecycle_code,
            cache_reference_reason=cache_reference_reason,
            catalog_code=catalog_code,
            catalog_sync_code=catalog_sync_code,
            cluster_mapping_code=cluster_mapping_code,
            controller_error_code=controller_error_code,
            distribution_code=distribution_code,
            helper_error_code=helper_error_code,
            image_store_code=image_store_code,
            install_admission_code=install_admission_code,
            install_degraded_reason=install_degraded_reason,
            library_assessment_code=library_assessment_code,
            library_projection_code=library_projection_code,
            model_cache_blocker_code=model_cache_blocker_code,
            model_cache_code=model_cache_code,
            node_offline_reason=node_offline_reason,
            operation_failure_code=operation_failure_code,
            prebuilt_image_code=prebuilt_image_code,
            profile_reason_code=profile_reason_code,
            projection_code=projection_code,
            recipe_build_code=recipe_build_code,
            recipe_image_code=recipe_image_code,
            recipe_operation_code=recipe_operation_code,
            recipe_package_code=recipe_package_code,
            recipe_update_code=recipe_update_code,
            reconcile_code=reconcile_code,
            resource_planning_code=resource_planning_code,
            resource_term=resource_term,
            resource_term_problem=resource_term_problem,
            run_degraded_reason=run_degraded_reason,
            run_switch_code=run_switch_code,
            runtime_image_code=runtime_image_code,
            runtime_preflight_code=runtime_preflight_code,
            runtime_preflight_finding_code=runtime_preflight_finding_code,
            source_bundle_code=source_bundle_code,
            source_policy_code=source_policy_code,
            stop_plan_code=stop_plan_code,
            storage_demand_code=storage_demand_code,
            supersede_code=supersede_code,
            topology_code=topology_code,
            uninstall_plan_code=uninstall_plan_code,
        )

        return reason_code_vocabulary
