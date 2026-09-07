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
  from ..models.runtime_argument_value import RuntimeArgumentValue





T = TypeVar("T", bound="RecipeRuntimeArgument")



@_attrs_define
class RecipeRuntimeArgument:
    """
        Attributes:
            name (str):
            setting (Union[None, Unset, str]):
            value (Union['RuntimeArgumentValue', None, Unset]): A literal process value; null is reserved for the setting-
                bound placeholder.
     """

    name: str
    setting: Union[None, Unset, str] = UNSET
    value: Union['RuntimeArgumentValue', None, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.runtime_argument_value import RuntimeArgumentValue
        name = self.name

        setting: Union[None, Unset, str]
        if isinstance(self.setting, Unset):
            setting = UNSET
        else:
            setting = self.setting

        value: Union[None, Unset, dict[str, Any]]
        if isinstance(self.value, Unset):
            value = UNSET
        elif isinstance(self.value, RuntimeArgumentValue):
            value = self.value.to_dict()
        else:
            value = self.value


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "name": name,
        })
        if setting is not UNSET:
            field_dict["setting"] = setting
        if value is not UNSET:
            field_dict["value"] = value

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.runtime_argument_value import RuntimeArgumentValue
        d = dict(src_dict)
        name = d.pop("name")

        def _parse_setting(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        setting = _parse_setting(d.pop("setting", UNSET))


        def _parse_value(data: object) -> Union['RuntimeArgumentValue', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                value_type_0 = RuntimeArgumentValue.from_dict(data)



                return value_type_0
            except: # noqa: E722
                pass
            return cast(Union['RuntimeArgumentValue', None, Unset], data)

        value = _parse_value(d.pop("value", UNSET))


        recipe_runtime_argument = cls(
            name=name,
            setting=setting,
            value=value,
        )

        return recipe_runtime_argument
