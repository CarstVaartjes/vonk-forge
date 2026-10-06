from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.stored_install_node_plan import StoredInstallNodePlan
  from ..models.stored_installation_plan_compiled_execution_plans import StoredInstallationPlanCompiledExecutionPlans





T = TypeVar("T", bound="StoredInstallationPlan")



@_attrs_define
class StoredInstallationPlan:
    """
        Attributes:
            allowed (bool):
            compiled_execution_plans (StoredInstallationPlanCompiledExecutionPlans):
            image_digest (str):
            mapping_generation (int):
            mapping_id (str):
            nodes (list[StoredInstallNodePlan]):
            plan_digest (str):
            recipe_build_id (None | str):
            recipe_content_sha256 (str):
            recipe_revision_id (str):
            schema_version (Literal[1]):
     """

    allowed: bool
    compiled_execution_plans: StoredInstallationPlanCompiledExecutionPlans
    image_digest: str
    mapping_generation: int
    mapping_id: str
    nodes: list[StoredInstallNodePlan]
    plan_digest: str
    recipe_build_id: None | str
    recipe_content_sha256: str
    recipe_revision_id: str
    schema_version: Literal[1]





    def to_dict(self) -> dict[str, Any]:
        from ..models.stored_install_node_plan import StoredInstallNodePlan # noqa: PLC0415
        from ..models.stored_installation_plan_compiled_execution_plans import StoredInstallationPlanCompiledExecutionPlans # noqa: PLC0415
        allowed = self.allowed

        compiled_execution_plans = self.compiled_execution_plans.to_dict()

        image_digest = self.image_digest

        mapping_generation = self.mapping_generation

        mapping_id = self.mapping_id

        nodes = []
        for nodes_item_data in self.nodes:
            nodes_item = nodes_item_data.to_dict()
            nodes.append(nodes_item)



        plan_digest = self.plan_digest

        recipe_build_id: None | str
        recipe_build_id = self.recipe_build_id

        recipe_content_sha256 = self.recipe_content_sha256

        recipe_revision_id = self.recipe_revision_id

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "allowed": allowed,
            "compiled_execution_plans": compiled_execution_plans,
            "image_digest": image_digest,
            "mapping_generation": mapping_generation,
            "mapping_id": mapping_id,
            "nodes": nodes,
            "plan_digest": plan_digest,
            "recipe_build_id": recipe_build_id,
            "recipe_content_sha256": recipe_content_sha256,
            "recipe_revision_id": recipe_revision_id,
            "schema_version": schema_version,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.stored_install_node_plan import StoredInstallNodePlan # noqa: PLC0415
        from ..models.stored_installation_plan_compiled_execution_plans import StoredInstallationPlanCompiledExecutionPlans # noqa: PLC0415
        d = dict(src_dict)
        allowed = d.pop("allowed")

        compiled_execution_plans = StoredInstallationPlanCompiledExecutionPlans.from_dict(d.pop("compiled_execution_plans"))




        image_digest = d.pop("image_digest")

        mapping_generation = d.pop("mapping_generation")

        mapping_id = d.pop("mapping_id")

        nodes = []
        _nodes = d.pop("nodes")
        for nodes_item_data in (_nodes):
            nodes_item = StoredInstallNodePlan.from_dict(nodes_item_data)



            nodes.append(nodes_item)


        plan_digest = d.pop("plan_digest")

        def _parse_recipe_build_id(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        recipe_build_id = _parse_recipe_build_id(d.pop("recipe_build_id"))


        recipe_content_sha256 = d.pop("recipe_content_sha256")

        recipe_revision_id = d.pop("recipe_revision_id")

        schema_version = cast(Literal[1] , d.pop("schema_version"))
        if schema_version != 1:
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        stored_installation_plan = cls(
            allowed=allowed,
            compiled_execution_plans=compiled_execution_plans,
            image_digest=image_digest,
            mapping_generation=mapping_generation,
            mapping_id=mapping_id,
            nodes=nodes,
            plan_digest=plan_digest,
            recipe_build_id=recipe_build_id,
            recipe_content_sha256=recipe_content_sha256,
            recipe_revision_id=recipe_revision_id,
            schema_version=schema_version,
        )

        return stored_installation_plan
