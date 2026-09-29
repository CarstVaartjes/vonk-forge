from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="RecipeMemoryResources")



@_attrs_define
class RecipeMemoryResources:
    """ Unified (DGX Spark) memory one role needs.

        Attributes:
            peak_bytes (int):
            reserve_bytes (int):
     """

    peak_bytes: int
    reserve_bytes: int





    def to_dict(self) -> dict[str, Any]:
        peak_bytes = self.peak_bytes

        reserve_bytes = self.reserve_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "peak_bytes": peak_bytes,
            "reserve_bytes": reserve_bytes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        peak_bytes = d.pop("peak_bytes")

        reserve_bytes = d.pop("reserve_bytes")

        recipe_memory_resources = cls(
            peak_bytes=peak_bytes,
            reserve_bytes=reserve_bytes,
        )

        return recipe_memory_resources
