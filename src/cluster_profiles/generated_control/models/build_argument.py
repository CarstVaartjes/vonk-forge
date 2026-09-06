from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast, Union






T = TypeVar("T", bound="BuildArgument")



@_attrs_define
class BuildArgument:
    """
        Attributes:
            name (str):
            value (Union[bool, float, int, str]):
     """

    name: str
    value: Union[bool, float, int, str]





    def to_dict(self) -> dict[str, Any]:
        name = self.name

        value: Union[bool, float, int, str]
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

        def _parse_value(data: object) -> Union[bool, float, int, str]:
            return cast(Union[bool, float, int, str], data)

        value = _parse_value(d.pop("value"))


        build_argument = cls(
            name=name,
            value=value,
        )

        return build_argument
