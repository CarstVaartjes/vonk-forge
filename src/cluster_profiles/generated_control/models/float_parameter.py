from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, cast
from typing import Union






T = TypeVar("T", bound="FloatParameter")



@_attrs_define
class FloatParameter:
    """
        Attributes:
            default (Union[float, int]):
            name (str):
            type_ (Literal['float']):
            allowed_values (Union[Unset, list[Union[bool, float, int, str]]]):
            maximum (Union[None, Unset, float, int]):
            minimum (Union[None, Unset, float, int]):
            pattern (Union[None, Unset, str]):
     """

    default: Union[float, int]
    name: str
    type_: Literal['float']
    allowed_values: Union[Unset, list[Union[bool, float, int, str]]] = UNSET
    maximum: Union[None, Unset, float, int] = UNSET
    minimum: Union[None, Unset, float, int] = UNSET
    pattern: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        default: Union[float, int]
        default = self.default

        name = self.name

        type_ = self.type_

        allowed_values: Union[Unset, list[Union[bool, float, int, str]]] = UNSET
        if not isinstance(self.allowed_values, Unset):
            allowed_values = []
            for allowed_values_item_data in self.allowed_values:
                allowed_values_item: Union[bool, float, int, str]
                allowed_values_item = allowed_values_item_data
                allowed_values.append(allowed_values_item)



        maximum: Union[None, Unset, float, int]
        if isinstance(self.maximum, Unset):
            maximum = UNSET
        else:
            maximum = self.maximum

        minimum: Union[None, Unset, float, int]
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
        def _parse_default(data: object) -> Union[float, int]:
            return cast(Union[float, int], data)

        default = _parse_default(d.pop("default"))


        name = d.pop("name")

        type_ = cast(Literal['float'] , d.pop("type"))
        if type_ != 'float':
            raise ValueError(f"type must match const 'float', got '{type_}'")

        allowed_values = []
        _allowed_values = d.pop("allowed_values", UNSET)
        for allowed_values_item_data in (_allowed_values or []):
            def _parse_allowed_values_item(data: object) -> Union[bool, float, int, str]:
                return cast(Union[bool, float, int, str], data)

            allowed_values_item = _parse_allowed_values_item(allowed_values_item_data)

            allowed_values.append(allowed_values_item)


        def _parse_maximum(data: object) -> Union[None, Unset, float, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float, int], data)

        maximum = _parse_maximum(d.pop("maximum", UNSET))


        def _parse_minimum(data: object) -> Union[None, Unset, float, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float, int], data)

        minimum = _parse_minimum(d.pop("minimum", UNSET))


        def _parse_pattern(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

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
