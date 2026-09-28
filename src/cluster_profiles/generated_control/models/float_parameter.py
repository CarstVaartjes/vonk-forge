from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="FloatParameter")



@_attrs_define
class FloatParameter:
    """
        Attributes:
            default (float | int):
            name (str):
            type_ (Literal['float']):
            allowed_values (list[bool | float | int | str] | Unset):
            maximum (float | int | None | Unset):
            minimum (float | int | None | Unset):
            pattern (None | str | Unset):
     """

    default: float | int
    name: str
    type_: Literal['float']
    allowed_values: list[bool | float | int | str] | Unset = UNSET
    maximum: float | int | None | Unset = UNSET
    minimum: float | int | None | Unset = UNSET
    pattern: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        default: float | int
        default = self.default

        name = self.name

        type_ = self.type_

        allowed_values: list[bool | float | int | str] | Unset = UNSET
        if not isinstance(self.allowed_values, Unset):
            allowed_values = []
            for allowed_values_item_data in self.allowed_values:
                allowed_values_item: bool | float | int | str
                allowed_values_item = allowed_values_item_data
                allowed_values.append(allowed_values_item)



        maximum: float | int | None | Unset
        if isinstance(self.maximum, Unset):
            maximum = UNSET
        else:
            maximum = self.maximum

        minimum: float | int | None | Unset
        if isinstance(self.minimum, Unset):
            minimum = UNSET
        else:
            minimum = self.minimum

        pattern: None | str | Unset
        if isinstance(self.pattern, Unset):
            pattern = UNSET
        else:
            pattern = self.pattern


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "default": default,
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
        def _parse_default(data: object) -> float | int:
            return cast(float | int, data)

        default = _parse_default(d.pop("default"))


        name = d.pop("name")

        type_ = cast(Literal['float'] , d.pop("type"))
        if type_ != 'float':
            raise ValueError(f"type must match const 'float', got '{type_}'")

        _allowed_values = d.pop("allowed_values", UNSET)
        allowed_values: list[bool | float | int | str] | Unset = UNSET
        if _allowed_values is not UNSET:
            allowed_values = []
            for allowed_values_item_data in _allowed_values:
                def _parse_allowed_values_item(data: object) -> bool | float | int | str:
                    return cast(bool | float | int | str, data)

                allowed_values_item = _parse_allowed_values_item(allowed_values_item_data)

                allowed_values.append(allowed_values_item)


        def _parse_maximum(data: object) -> float | int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | int | None | Unset, data)

        maximum = _parse_maximum(d.pop("maximum", UNSET))


        def _parse_minimum(data: object) -> float | int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | int | None | Unset, data)

        minimum = _parse_minimum(d.pop("minimum", UNSET))


        def _parse_pattern(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        pattern = _parse_pattern(d.pop("pattern", UNSET))


        float_parameter = cls(
            default=default,
            name=name,
            type_=type_,
            allowed_values=allowed_values,
            maximum=maximum,
            minimum=minimum,
            pattern=pattern,
        )

        return float_parameter
