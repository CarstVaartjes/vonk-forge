from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import Union






T = TypeVar("T", bound="RecipeImageAvailabilityStart")



@_attrs_define
class RecipeImageAvailabilityStart:
    """
        Attributes:
            recipe_revision_id (str):
            request_key (str):
            force (Union[Unset, bool]):  Default: False.
     """

    recipe_revision_id: str
    request_key: str
    force: Union[Unset, bool] = False





    def to_dict(self) -> dict[str, Any]:
        recipe_revision_id = self.recipe_revision_id

        request_key = self.request_key

        force = self.force


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "recipe_revision_id": recipe_revision_id,
            "request_key": request_key,
        })
        if force is not UNSET:
            field_dict["force"] = force

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        recipe_revision_id = d.pop("recipe_revision_id")

        request_key = d.pop("request_key")

        force = d.pop("force", UNSET)

        recipe_image_availability_start = cls(
            recipe_revision_id=recipe_revision_id,
            request_key=request_key,
            force=force,
        )

        return recipe_image_availability_start
