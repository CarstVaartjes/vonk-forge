from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.plan_reason import PlanReason
  from ..models.uninstall_node_impact_response import UninstallNodeImpactResponse
  from ..models.uninstall_plan_response_recipe_content import UninstallPlanResponseRecipeContent
  from ..models.uninstall_active_run_response import UninstallActiveRunResponse
  from ..models.uninstall_model_impact_response import UninstallModelImpactResponse
  from ..models.uninstall_consequences_response import UninstallConsequencesResponse





T = TypeVar("T", bound="UninstallPlanResponse")



@_attrs_define
class UninstallPlanResponse:
    """
        Attributes:
            active_run_count (int):
            active_runs (list['UninstallActiveRunResponse']):
            active_runs_truncated (bool):
            allowed (bool):
            blockers (list['PlanReason']):
            consequences (UninstallConsequencesResponse):
            installation_authority_digest (str):
            installation_id (str):
            installation_state (str):
            model_impact (UninstallModelImpactResponse):
            nodes (list['UninstallNodeImpactResponse']):
            original_plan_digest (str):
            plan_digest (str):
            recipe_content (UninstallPlanResponseRecipeContent):
            recipe_content_sha256 (str):
            recipe_id (str):
            recipe_revision_id (str):
            warnings (list['PlanReason']):
            bytes_removed (Union[None, Unset, int]):
     """

    active_run_count: int
    active_runs: list['UninstallActiveRunResponse']
    active_runs_truncated: bool
    allowed: bool
    blockers: list['PlanReason']
    consequences: 'UninstallConsequencesResponse'
    installation_authority_digest: str
    installation_id: str
    installation_state: str
    model_impact: 'UninstallModelImpactResponse'
    nodes: list['UninstallNodeImpactResponse']
    original_plan_digest: str
    plan_digest: str
    recipe_content: 'UninstallPlanResponseRecipeContent'
    recipe_content_sha256: str
    recipe_id: str
    recipe_revision_id: str
    warnings: list['PlanReason']
    bytes_removed: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.plan_reason import PlanReason
        from ..models.uninstall_node_impact_response import UninstallNodeImpactResponse
        from ..models.uninstall_plan_response_recipe_content import UninstallPlanResponseRecipeContent
        from ..models.uninstall_active_run_response import UninstallActiveRunResponse
        from ..models.uninstall_model_impact_response import UninstallModelImpactResponse
        from ..models.uninstall_consequences_response import UninstallConsequencesResponse
        active_run_count = self.active_run_count

        active_runs = []
        for active_runs_item_data in self.active_runs:
            active_runs_item = active_runs_item_data.to_dict()
            active_runs.append(active_runs_item)



        active_runs_truncated = self.active_runs_truncated

        allowed = self.allowed

        blockers = []
        for blockers_item_data in self.blockers:
            blockers_item = blockers_item_data.to_dict()
            blockers.append(blockers_item)



        consequences = self.consequences.to_dict()

        installation_authority_digest = self.installation_authority_digest

        installation_id = self.installation_id

        installation_state = self.installation_state

        model_impact = self.model_impact.to_dict()

        nodes = []
        for nodes_item_data in self.nodes:
            nodes_item = nodes_item_data.to_dict()
            nodes.append(nodes_item)



        original_plan_digest = self.original_plan_digest

        plan_digest = self.plan_digest

        recipe_content = self.recipe_content.to_dict()

        recipe_content_sha256 = self.recipe_content_sha256

        recipe_id = self.recipe_id

        recipe_revision_id = self.recipe_revision_id

        warnings = []
        for warnings_item_data in self.warnings:
            warnings_item = warnings_item_data.to_dict()
            warnings.append(warnings_item)



        bytes_removed: Union[None, Unset, int]
        if isinstance(self.bytes_removed, Unset):
            bytes_removed = UNSET
        else:
            bytes_removed = self.bytes_removed


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "active_run_count": active_run_count,
            "active_runs": active_runs,
            "active_runs_truncated": active_runs_truncated,
            "allowed": allowed,
            "blockers": blockers,
            "consequences": consequences,
            "installation_authority_digest": installation_authority_digest,
            "installation_id": installation_id,
            "installation_state": installation_state,
            "model_impact": model_impact,
            "nodes": nodes,
            "original_plan_digest": original_plan_digest,
            "plan_digest": plan_digest,
            "recipe_content": recipe_content,
            "recipe_content_sha256": recipe_content_sha256,
            "recipe_id": recipe_id,
            "recipe_revision_id": recipe_revision_id,
            "warnings": warnings,
        })
        if bytes_removed is not UNSET:
            field_dict["bytes_removed"] = bytes_removed

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.plan_reason import PlanReason
        from ..models.uninstall_node_impact_response import UninstallNodeImpactResponse
        from ..models.uninstall_plan_response_recipe_content import UninstallPlanResponseRecipeContent
        from ..models.uninstall_active_run_response import UninstallActiveRunResponse
        from ..models.uninstall_model_impact_response import UninstallModelImpactResponse
        from ..models.uninstall_consequences_response import UninstallConsequencesResponse
        d = dict(src_dict)
        active_run_count = d.pop("active_run_count")

        active_runs = []
        _active_runs = d.pop("active_runs")
        for active_runs_item_data in (_active_runs):
            active_runs_item = UninstallActiveRunResponse.from_dict(active_runs_item_data)



            active_runs.append(active_runs_item)


        active_runs_truncated = d.pop("active_runs_truncated")

        allowed = d.pop("allowed")

        blockers = []
        _blockers = d.pop("blockers")
        for blockers_item_data in (_blockers):
            blockers_item = PlanReason.from_dict(blockers_item_data)



            blockers.append(blockers_item)


        consequences = UninstallConsequencesResponse.from_dict(d.pop("consequences"))




        installation_authority_digest = d.pop("installation_authority_digest")

        installation_id = d.pop("installation_id")

        installation_state = d.pop("installation_state")

        model_impact = UninstallModelImpactResponse.from_dict(d.pop("model_impact"))




        nodes = []
        _nodes = d.pop("nodes")
        for nodes_item_data in (_nodes):
            nodes_item = UninstallNodeImpactResponse.from_dict(nodes_item_data)



            nodes.append(nodes_item)


        original_plan_digest = d.pop("original_plan_digest")

        plan_digest = d.pop("plan_digest")

        recipe_content = UninstallPlanResponseRecipeContent.from_dict(d.pop("recipe_content"))




        recipe_content_sha256 = d.pop("recipe_content_sha256")

        recipe_id = d.pop("recipe_id")

        recipe_revision_id = d.pop("recipe_revision_id")

        warnings = []
        _warnings = d.pop("warnings")
        for warnings_item_data in (_warnings):
            warnings_item = PlanReason.from_dict(warnings_item_data)



            warnings.append(warnings_item)


        def _parse_bytes_removed(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        bytes_removed = _parse_bytes_removed(d.pop("bytes_removed", UNSET))


        uninstall_plan_response = cls(
            active_run_count=active_run_count,
            active_runs=active_runs,
            active_runs_truncated=active_runs_truncated,
            allowed=allowed,
            blockers=blockers,
            consequences=consequences,
            installation_authority_digest=installation_authority_digest,
            installation_id=installation_id,
            installation_state=installation_state,
            model_impact=model_impact,
            nodes=nodes,
            original_plan_digest=original_plan_digest,
            plan_digest=plan_digest,
            recipe_content=recipe_content,
            recipe_content_sha256=recipe_content_sha256,
            recipe_id=recipe_id,
            recipe_revision_id=recipe_revision_id,
            warnings=warnings,
            bytes_removed=bytes_removed,
        )

        return uninstall_plan_response
