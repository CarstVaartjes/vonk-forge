from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.visual_recipe_parameter_change_effect import check_visual_recipe_parameter_change_effect
from ..models.visual_recipe_parameter_change_effect import VisualRecipeParameterChangeEffect
from ..models.visual_recipe_parameter_type import check_visual_recipe_parameter_type
from ..models.visual_recipe_parameter_type import VisualRecipeParameterType
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="VisualRecipeParameter")



@_attrs_define
class VisualRecipeParameter:
    """
        Attributes:
            change_effect (VisualRecipeParameterChangeEffect):
            default (Union[None, bool, int, str]):
            description (str):
            name (str):
            type_ (VisualRecipeParameterType):
            allowed_values (Union[Unset, list[Union[None, bool, int, str]]]):
            maximum (Union[None, Unset, int]):
            minimum (Union[None, Unset, int]):
            pattern (Union[None, Unset, str]):
     """

    change_effect: VisualRecipeParameterChangeEffect
    default: Union[None, bool, int, str]
    description: str
    name: str
    type_: VisualRecipeParameterType
    allowed_values: Union[Unset, list[Union[None, bool, int, str]]] = UNSET
    maximum: Union[None, Unset, int] = UNSET
    minimum: Union[None, Unset, int] = UNSET
    pattern: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        change_effect: str = self.change_effect

        default: Union[None, bool, int, str]
        default = self.default

        description = self.description

        name = self.name

        type_: str = self.type_

        allowed_values: Union[Unset, list[Union[None, bool, int, str]]] = UNSET
        if not isinstance(self.allowed_values, Unset):
            allowed_values = []
            for allowed_values_item_data in self.allowed_values:
                allowed_values_item: Union[None, bool, int, str]
                allowed_values_item = allowed_values_item_data
                allowed_values.append(allowed_values_item)



        maximum: Union[None, Unset, int]
        if isinstance(self.maximum, Unset):
            maximum = UNSET
        else:
            maximum = self.maximum

        minimum: Union[None, Unset, int]
        if isinstance(self.minimum, Unset):
            minimum = UNSET
        else:
            minimum = self.minimum

        pattern: Union[None, Unset, str]
        if isinstance(self.pattern, Unset):
            pattern = UNSET
        else:
            pattern = self.pattern


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "change_effect": change_effect,
            "default": default,
            "description": description,
            "name": name,
            "type": type_,
        })
        if allowed_values is not UNSET:
            field_dict["allowed_values"] = allowed_values
        if maximum is not UNSET:
            field_dict["maximum"] = maximum
        if minimum is not UNSET:
            field_dict["minimum"] = minimum
        if pattern is not UNSET:
            field_dict["pattern"] = pattern

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        change_effect = check_visual_recipe_parameter_change_effect(d.pop("change_effect"))




        def _parse_default(data: object) -> Union[None, bool, int, str]:
            if data is None:
                return data
            return cast(Union[None, bool, int, str], data)

        default = _parse_default(d.pop("default"))


        description = d.pop("description")

        name = d.pop("name")

        type_ = check_visual_recipe_parameter_type(d.pop("type"))




        allowed_values = []
        _allowed_values = d.pop("allowed_values", UNSET)
        for allowed_values_item_data in (_allowed_values or []):
            def _parse_allowed_values_item(data: object) -> Union[None, bool, int, str]:
                if data is None:
                    return data
                return cast(Union[None, bool, int, str], data)

            allowed_values_item = _parse_allowed_values_item(allowed_values_item_data)

            allowed_values.append(allowed_values_item)


        def _parse_maximum(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        maximum = _parse_maximum(d.pop("maximum", UNSET))


        def _parse_minimum(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        minimum = _parse_minimum(d.pop("minimum", UNSET))


        def _parse_pattern(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        pattern = _parse_pattern(d.pop("pattern", UNSET))


        visual_recipe_parameter = cls(
            change_effect=change_effect,
            default=default,
            description=description,
            name=name,
            type_=type_,
            allowed_values=allowed_values,
            maximum=maximum,
            minimum=minimum,
            pattern=pattern,
        )

        return visual_recipe_parameter
