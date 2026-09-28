from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="EnumParameter")



@_attrs_define
class EnumParameter:
    """
        Attributes:
            allowed_values (list[bool | float | int | str]):
            default (bool | float | int | str):
            name (str):
            type_ (Literal['enum']):
            maximum (None | Unset):
            minimum (None | Unset):
            pattern (None | str | Unset):
     """

    allowed_values: list[bool | float | int | str]
    default: bool | float | int | str
    name: str
    type_: Literal['enum']
    maximum: None | Unset = UNSET
    minimum: None | Unset = UNSET
    pattern: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        allowed_values = []
        for allowed_values_item_data in self.allowed_values:
            allowed_values_item: bool | float | int | str
            allowed_values_item = allowed_values_item_data
            allowed_values.append(allowed_values_item)



        default: bool | float | int | str
        default = self.default

        name = self.name

        type_ = self.type_

        maximum = self.maximum

        minimum = self.minimum

        pattern: None | str | Unset
        if isinstance(self.pattern, Unset):
            pattern = UNSET
        else:
            pattern = self.pattern


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "allowed_values": allowed_values,
            "default": default,
            "name": name,
            "type": type_,
        })
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
        allowed_values = []
        _allowed_values = d.pop("allowed_values")
        for allowed_values_item_data in (_allowed_values):
            def _parse_allowed_values_item(data: object) -> bool | float | int | str:
                return cast(bool | float | int | str, data)

            allowed_values_item = _parse_allowed_values_item(allowed_values_item_data)

            allowed_values.append(allowed_values_item)


        def _parse_default(data: object) -> bool | float | int | str:
            return cast(bool | float | int | str, data)

        default = _parse_default(d.pop("default"))


        name = d.pop("name")

        type_ = cast(Literal['enum'] , d.pop("type"))
        if type_ != 'enum':
            raise ValueError(f"type must match const 'enum', got '{type_}'")

        maximum = d.pop("maximum", UNSET)

        minimum = d.pop("minimum", UNSET)

        def _parse_pattern(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        pattern = _parse_pattern(d.pop("pattern", UNSET))


        enum_parameter = cls(
            allowed_values=allowed_values,
            default=default,
            name=name,
            type_=type_,
            maximum=maximum,
            minimum=minimum,
            pattern=pattern,
        )

        return enum_parameter
