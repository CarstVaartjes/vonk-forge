from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="ModelParameters")



@_attrs_define
class ModelParameters:
    """
        Attributes:
            active (int | None | Unset):
            total (int | None | Unset):
     """

    active: int | None | Unset = UNSET
    total: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        active: int | None | Unset
        if isinstance(self.active, Unset):
            active = UNSET
        else:
            active = self.active

        total: int | None | Unset
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
        def _parse_active(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        active = _parse_active(d.pop("active", UNSET))


        def _parse_total(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        total = _parse_total(d.pop("total", UNSET))


        model_parameters = cls(
            active=active,
            total=total,
        )

        return model_parameters
