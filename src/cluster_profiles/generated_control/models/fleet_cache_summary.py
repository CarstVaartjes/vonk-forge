from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset






T = TypeVar("T", bound="FleetCacheSummary")



@_attrs_define
class FleetCacheSummary:
    """ How many assignments of a profile are cached, missing or unknown.

        Attributes:
            cached (int | Unset):  Default: 0.
            missing (int | Unset):  Default: 0.
            unknown (int | Unset):  Default: 0.
     """

    cached: int | Unset = 0
    missing: int | Unset = 0
    unknown: int | Unset = 0





    def to_dict(self) -> dict[str, Any]:
        cached = self.cached

        missing = self.missing

        unknown = self.unknown


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if cached is not UNSET:
            field_dict["cached"] = cached
        if missing is not UNSET:
            field_dict["missing"] = missing
        if unknown is not UNSET:
            field_dict["unknown"] = unknown

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        cached = d.pop("cached", UNSET)

        missing = d.pop("missing", UNSET)

        unknown = d.pop("unknown", UNSET)

        fleet_cache_summary = cls(
            cached=cached,
            missing=missing,
            unknown=unknown,
        )

        return fleet_cache_summary
