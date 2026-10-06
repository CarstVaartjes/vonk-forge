from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="RecipeBuildEnvironmentArgument")



@_attrs_define
class RecipeBuildEnvironmentArgument:
    """
        Attributes:
            name (str):
            value (bool | int | str):
     """

    name: str
    value: bool | int | str





    def to_dict(self) -> dict[str, Any]:
        name = self.name

        value: bool | int | str
        value = self.value


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "name": name,
            "value": value,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        name = d.pop("name")

        def _parse_value(data: object) -> bool | int | str:
            return cast(bool | int | str, data)

        value = _parse_value(d.pop("value"))


        recipe_build_environment_argument = cls(
            name=name,
            value=value,
        )

        return recipe_build_environment_argument
