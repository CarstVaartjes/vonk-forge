from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, Union, cast

if TYPE_CHECKING:
  from ..models.recipe_image_availability_response import RecipeImageAvailabilityResponse





T = TypeVar("T", bound="RecipeUpdateResponse")



@_attrs_define
class RecipeUpdateResponse:
    """
        Attributes:
            updates (list['RecipeImageAvailabilityResponse']):
            action (Union[Literal['update'], Unset]):  Default: 'update'.
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    updates: list['RecipeImageAvailabilityResponse']
    action: Union[Literal['update'], Unset] = 'update'
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_image_availability_response import RecipeImageAvailabilityResponse
        updates = []
        for updates_item_data in self.updates:
            updates_item = updates_item_data.to_dict()
            updates.append(updates_item)



        action = self.action

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "updates": updates,
        })
        if action is not UNSET:
            field_dict["action"] = action
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_image_availability_response import RecipeImageAvailabilityResponse
        d = dict(src_dict)
        updates = []
        _updates = d.pop("updates")
        for updates_item_data in (_updates):
            updates_item = RecipeImageAvailabilityResponse.from_dict(updates_item_data)



            updates.append(updates_item)


        action = cast(Union[Literal['update'], Unset] , d.pop("action", UNSET))
        if action != 'update' and not isinstance(action, Unset):
            raise ValueError(f"action must match const 'update', got '{action}'")

        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        recipe_update_response = cls(
            updates=updates,
            action=action,
            schema_version=schema_version,
        )

        return recipe_update_response
