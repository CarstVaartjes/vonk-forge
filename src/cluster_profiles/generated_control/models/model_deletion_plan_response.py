from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.model_deletion_installation_impact_response import ModelDeletionInstallationImpactResponse
  from ..models.uninstall_active_run_response import UninstallActiveRunResponse
  from ..models.plan_reason import PlanReason
  from ..models.model_deletion_node_impact_response import ModelDeletionNodeImpactResponse





T = TypeVar("T", bound="ModelDeletionPlanResponse")



@_attrs_define
class ModelDeletionPlanResponse:
    """
        Attributes:
            active_run_count (int):
            active_runs (list['UninstallActiveRunResponse']):
            allowed (bool):
            blockers (list['PlanReason']):
            bytes_removed (int):
            installations (list['ModelDeletionInstallationImpactResponse']):
            model_title (str):
            model_version_sha256 (str):
            nodes (list['ModelDeletionNodeImpactResponse']):
            plan_digest (str):
            shared_cache_policy (str):
            warnings (list['PlanReason']):
     """

    active_run_count: int
    active_runs: list['UninstallActiveRunResponse']
    allowed: bool
    blockers: list['PlanReason']
    bytes_removed: int
    installations: list['ModelDeletionInstallationImpactResponse']
    model_title: str
    model_version_sha256: str
    nodes: list['ModelDeletionNodeImpactResponse']
    plan_digest: str
    shared_cache_policy: str
    warnings: list['PlanReason']





    def to_dict(self) -> dict[str, Any]:
        from ..models.model_deletion_installation_impact_response import ModelDeletionInstallationImpactResponse
        from ..models.uninstall_active_run_response import UninstallActiveRunResponse
        from ..models.plan_reason import PlanReason
        from ..models.model_deletion_node_impact_response import ModelDeletionNodeImpactResponse
        active_run_count = self.active_run_count

        active_runs = []
        for active_runs_item_data in self.active_runs:
            active_runs_item = active_runs_item_data.to_dict()
            active_runs.append(active_runs_item)



        allowed = self.allowed

        blockers = []
        for blockers_item_data in self.blockers:
            blockers_item = blockers_item_data.to_dict()
            blockers.append(blockers_item)



        bytes_removed = self.bytes_removed

        installations = []
        for installations_item_data in self.installations:
            installations_item = installations_item_data.to_dict()
            installations.append(installations_item)



        model_title = self.model_title

        model_version_sha256 = self.model_version_sha256

        nodes = []
        for nodes_item_data in self.nodes:
            nodes_item = nodes_item_data.to_dict()
            nodes.append(nodes_item)



        plan_digest = self.plan_digest

        shared_cache_policy = self.shared_cache_policy

        warnings = []
        for warnings_item_data in self.warnings:
            warnings_item = warnings_item_data.to_dict()
            warnings.append(warnings_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "active_run_count": active_run_count,
            "active_runs": active_runs,
            "allowed": allowed,
            "blockers": blockers,
            "bytes_removed": bytes_removed,
            "installations": installations,
            "model_title": model_title,
            "model_version_sha256": model_version_sha256,
            "nodes": nodes,
            "plan_digest": plan_digest,
            "shared_cache_policy": shared_cache_policy,
            "warnings": warnings,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.model_deletion_installation_impact_response import ModelDeletionInstallationImpactResponse
        from ..models.uninstall_active_run_response import UninstallActiveRunResponse
        from ..models.plan_reason import PlanReason
        from ..models.model_deletion_node_impact_response import ModelDeletionNodeImpactResponse
        d = dict(src_dict)
        active_run_count = d.pop("active_run_count")

        active_runs = []
        _active_runs = d.pop("active_runs")
        for active_runs_item_data in (_active_runs):
            active_runs_item = UninstallActiveRunResponse.from_dict(active_runs_item_data)



            active_runs.append(active_runs_item)


        allowed = d.pop("allowed")

        blockers = []
        _blockers = d.pop("blockers")
        for blockers_item_data in (_blockers):
            blockers_item = PlanReason.from_dict(blockers_item_data)



            blockers.append(blockers_item)


        bytes_removed = d.pop("bytes_removed")

        installations = []
        _installations = d.pop("installations")
        for installations_item_data in (_installations):
            installations_item = ModelDeletionInstallationImpactResponse.from_dict(installations_item_data)



            installations.append(installations_item)


        model_title = d.pop("model_title")

        model_version_sha256 = d.pop("model_version_sha256")

        nodes = []
        _nodes = d.pop("nodes")
        for nodes_item_data in (_nodes):
            nodes_item = ModelDeletionNodeImpactResponse.from_dict(nodes_item_data)



            nodes.append(nodes_item)


        plan_digest = d.pop("plan_digest")

        shared_cache_policy = d.pop("shared_cache_policy")

        warnings = []
        _warnings = d.pop("warnings")
        for warnings_item_data in (_warnings):
            warnings_item = PlanReason.from_dict(warnings_item_data)



            warnings.append(warnings_item)


        model_deletion_plan_response = cls(
            active_run_count=active_run_count,
            active_runs=active_runs,
            allowed=allowed,
            blockers=blockers,
            bytes_removed=bytes_removed,
            installations=installations,
            model_title=model_title,
            model_version_sha256=model_version_sha256,
            nodes=nodes,
            plan_digest=plan_digest,
            shared_cache_policy=shared_cache_policy,
            warnings=warnings,
        )

        return model_deletion_plan_response
