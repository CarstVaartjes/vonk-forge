from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="ImageDistributionPreviewInput")



@_attrs_define
class ImageDistributionPreviewInput:
    """
        Attributes:
            mapping_generation (int):
            mapping_id (str):
            recipe_build_id (str):
     """

    mapping_generation: int
    mapping_id: str
    recipe_build_id: str





    def to_dict(self) -> dict[str, Any]:
        mapping_generation = self.mapping_generation

        mapping_id = self.mapping_id

        recipe_build_id = self.recipe_build_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "mapping_generation": mapping_generation,
            "mapping_id": mapping_id,
            "recipe_build_id": recipe_build_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        mapping_generation = d.pop("mapping_generation")

        mapping_id = d.pop("mapping_id")

        recipe_build_id = d.pop("recipe_build_id")

        image_distribution_preview_input = cls(
            mapping_generation=mapping_generation,
            mapping_id=mapping_id,
            recipe_build_id=recipe_build_id,
        )

        return image_distribution_preview_input
