from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_setting_change_effect import check_recipe_setting_change_effect
from ..models.recipe_setting_change_effect import RecipeSettingChangeEffect
from typing import cast
from typing import cast, Union






T = TypeVar("T", bound="RecipeSetting")



@_attrs_define
class RecipeSetting:
    """
        Attributes:
            change_effect (RecipeSettingChangeEffect):
            value (Union[bool, float, int, str]):
     """

    change_effect: RecipeSettingChangeEffect
    value: Union[bool, float, int, str]





    def to_dict(self) -> dict[str, Any]:
        change_effect: str = self.change_effect

        value: Union[bool, float, int, str]
        value = self.value


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "change_effect": change_effect,
            "value": value,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        change_effect = check_recipe_setting_change_effect(d.pop("change_effect"))




        def _parse_value(data: object) -> Union[bool, float, int, str]:
            return cast(Union[bool, float, int, str], data)

        value = _parse_value(d.pop("value"))


        recipe_setting = cls(
            change_effect=change_effect,
            value=value,
        )

        return recipe_setting
