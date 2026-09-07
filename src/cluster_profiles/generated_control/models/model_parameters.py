from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="ModelParameters")



@_attrs_define
class ModelParameters:
    """
        Attributes:
            active (Union[None, Unset, int]):
            total (Union[None, Unset, int]):
     """

    active: Union[None, Unset, int] = UNSET
    total: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        active: Union[None, Unset, int]
        if isinstance(self.active, Unset):
            active = UNSET
        else:
            active = self.active

        total: Union[None, Unset, int]
        if isinstance(self.total, Unset):
            total = UNSET
        else:
            total = self.total


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if active is not UNSET:
            field_dict["active"] = active
        if total is not UNSET:
            field_dict["total"] = total

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        def _parse_active(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        active = _parse_active(d.pop("active", UNSET))


        def _parse_total(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        total = _parse_total(d.pop("total", UNSET))


        model_parameters = cls(
            active=active,
            total=total,
        )

        return model_parameters
