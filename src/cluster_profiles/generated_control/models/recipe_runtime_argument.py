from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="RecipeRuntimeArgument")



@_attrs_define
class RecipeRuntimeArgument:
    """
        Attributes:
            name (str):
            setting (None | str | Unset):
            value (Any | None | Unset): A literal process value; null is reserved for the setting-bound placeholder.
     """

    name: str
    setting: None | str | Unset = UNSET
    value: Any | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        name = self.name

        setting: None | str | Unset
        if isinstance(self.setting, Unset):
            setting = UNSET
        else:
            setting = self.setting

        value: Any | None | Unset
        if isinstance(self.value, Unset):
            value = UNSET
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
        d = dict(src_dict)
        name = d.pop("name")

        def _parse_setting(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        setting = _parse_setting(d.pop("setting", UNSET))


        def _parse_value(data: object) -> Any | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Any | None | Unset, data)

        value = _parse_value(d.pop("value", UNSET))


        recipe_runtime_argument = cls(
            name=name,
            setting=setting,
            value=value,
        )

        return recipe_runtime_argument
