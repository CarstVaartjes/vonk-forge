from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.availability_recovery_action import AvailabilityRecoveryAction
from ..models.availability_recovery_action import check_availability_recovery_action
from typing import cast






T = TypeVar("T", bound="RecipeImageAvailabilityAction")



@_attrs_define
class RecipeImageAvailabilityAction:
    """
        Attributes:
            key (AvailabilityRecoveryAction):
     """

    key: AvailabilityRecoveryAction





    def to_dict(self) -> dict[str, Any]:
        key: str = self.key


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "key": key,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        key = check_availability_recovery_action(d.pop("key"))




        recipe_image_availability_action = cls(
            key=key,
        )

        return recipe_image_availability_action
