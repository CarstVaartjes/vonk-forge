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
  from ..models.install_plan_response_compiled_execution_plans import InstallPlanResponseCompiledExecutionPlans
  from ..models.install_node_plan_response import InstallNodePlanResponse





T = TypeVar("T", bound="InstallPlanResponse")



@_attrs_define
class InstallPlanResponse:
    """
        Attributes:
            allowed (bool):
            image_digest (str):
            mapping_generation (int):
            mapping_id (str):
            nodes (list['InstallNodePlanResponse']):
            plan_digest (str):
            recipe_build_id (Union[None, str]):
            recipe_content_sha256 (str):
            recipe_revision_id (str):
            compiled_execution_plans (Union[Unset, InstallPlanResponseCompiledExecutionPlans]):
     """

    allowed: bool
    image_digest: str
    mapping_generation: int
    mapping_id: str
    nodes: list['InstallNodePlanResponse']
    plan_digest: str
    recipe_build_id: Union[None, str]
    recipe_content_sha256: str
    recipe_revision_id: str
    compiled_execution_plans: Union[Unset, 'InstallPlanResponseCompiledExecutionPlans'] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.install_plan_response_compiled_execution_plans import InstallPlanResponseCompiledExecutionPlans
        from ..models.install_node_plan_response import InstallNodePlanResponse
        allowed = self.allowed

        image_digest = self.image_digest

        mapping_generation = self.mapping_generation

        mapping_id = self.mapping_id

        nodes = []
        for nodes_item_data in self.nodes:
            nodes_item = nodes_item_data.to_dict()
            nodes.append(nodes_item)



        plan_digest = self.plan_digest

        recipe_build_id: Union[None, str]
        recipe_build_id = self.recipe_build_id

        recipe_content_sha256 = self.recipe_content_sha256

        recipe_revision_id = self.recipe_revision_id

        compiled_execution_plans: Union[Unset, dict[str, Any]] = UNSET
        if not isinstance(self.compiled_execution_plans, Unset):
            compiled_execution_plans = self.compiled_execution_plans.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "allowed": allowed,
            "image_digest": image_digest,
            "mapping_generation": mapping_generation,
            "mapping_id": mapping_id,
            "nodes": nodes,
            "plan_digest": plan_digest,
            "recipe_build_id": recipe_build_id,
            "recipe_content_sha256": recipe_content_sha256,
            "recipe_revision_id": recipe_revision_id,
        })
        if compiled_execution_plans is not UNSET:
            field_dict["compiled_execution_plans"] = compiled_execution_plans

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.install_plan_response_compiled_execution_plans import InstallPlanResponseCompiledExecutionPlans
        from ..models.install_node_plan_response import InstallNodePlanResponse
        d = dict(src_dict)
        allowed = d.pop("allowed")

        image_digest = d.pop("image_digest")

        mapping_generation = d.pop("mapping_generation")

        mapping_id = d.pop("mapping_id")

        nodes = []
        _nodes = d.pop("nodes")
        for nodes_item_data in (_nodes):
            nodes_item = InstallNodePlanResponse.from_dict(nodes_item_data)



            nodes.append(nodes_item)


        plan_digest = d.pop("plan_digest")

        def _parse_recipe_build_id(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        recipe_build_id = _parse_recipe_build_id(d.pop("recipe_build_id"))


        recipe_content_sha256 = d.pop("recipe_content_sha256")

        recipe_revision_id = d.pop("recipe_revision_id")

        _compiled_execution_plans = d.pop("compiled_execution_plans", UNSET)
        compiled_execution_plans: Union[Unset, InstallPlanResponseCompiledExecutionPlans]
        if isinstance(_compiled_execution_plans,  Unset):
            compiled_execution_plans = UNSET
        else:
            compiled_execution_plans = InstallPlanResponseCompiledExecutionPlans.from_dict(_compiled_execution_plans)




        install_plan_response = cls(
            allowed=allowed,
            image_digest=image_digest,
            mapping_generation=mapping_generation,
            mapping_id=mapping_id,
            nodes=nodes,
            plan_digest=plan_digest,
            recipe_build_id=recipe_build_id,
            recipe_content_sha256=recipe_content_sha256,
            recipe_revision_id=recipe_revision_id,
            compiled_execution_plans=compiled_execution_plans,
        )

        return install_plan_response
