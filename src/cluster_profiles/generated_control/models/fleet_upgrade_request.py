from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="FleetUpgradeRequest")



@_attrs_define
class FleetUpgradeRequest:
    """
        Attributes:
            request_key (str):
            all_ (bool | Unset):  Default: False.
            selectors (list[str] | None | Unset):
     """

    request_key: str
    all_: bool | Unset = False
    selectors: list[str] | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        request_key = self.request_key

        all_ = self.all_

        selectors: list[str] | None | Unset
        if isinstance(self.selectors, Unset):
            selectors = UNSET
        elif isinstance(self.selectors, list):
            selectors = self.selectors


        else:
            selectors = self.selectors


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "request_key": request_key,
        })
        if all_ is not UNSET:
            field_dict["all"] = all_
        if selectors is not UNSET:
            field_dict["selectors"] = selectors

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        request_key = d.pop("request_key")

        all_ = d.pop("all", UNSET)

        def _parse_selectors(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                selectors_type_0 = cast(list[str], data)

                return selectors_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        selectors = _parse_selectors(d.pop("selectors", UNSET))


        fleet_upgrade_request = cls(
            request_key=request_key,
            all_=all_,
            selectors=selectors,
        )

        return fleet_upgrade_request
