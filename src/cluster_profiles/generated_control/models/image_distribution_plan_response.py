from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="ImageDistributionPlanResponse")



@_attrs_define
class ImageDistributionPlanResponse:
    """
        Attributes:
            image_digest (str):
            mapping_generation (int):
            mapping_id (str):
            node_ids (list[str]):
            plan_digest (str):
            recipe_build_id (str):
     """

    image_digest: str
    mapping_generation: int
    mapping_id: str
    node_ids: list[str]
    plan_digest: str
    recipe_build_id: str





    def to_dict(self) -> dict[str, Any]:
        image_digest = self.image_digest

        mapping_generation = self.mapping_generation

        mapping_id = self.mapping_id

        node_ids = self.node_ids



        plan_digest = self.plan_digest

        recipe_build_id = self.recipe_build_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "image_digest": image_digest,
            "mapping_generation": mapping_generation,
            "mapping_id": mapping_id,
            "node_ids": node_ids,
            "plan_digest": plan_digest,
            "recipe_build_id": recipe_build_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        image_digest = d.pop("image_digest")

        mapping_generation = d.pop("mapping_generation")

        mapping_id = d.pop("mapping_id")

        node_ids = cast(list[str], d.pop("node_ids"))


        plan_digest = d.pop("plan_digest")

        recipe_build_id = d.pop("recipe_build_id")

        image_distribution_plan_response = cls(
            image_digest=image_digest,
            mapping_generation=mapping_generation,
            mapping_id=mapping_id,
            node_ids=node_ids,
            plan_digest=plan_digest,
            recipe_build_id=recipe_build_id,
        )

        return image_distribution_plan_response
