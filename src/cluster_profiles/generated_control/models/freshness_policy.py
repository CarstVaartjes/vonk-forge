from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import Union






T = TypeVar("T", bound="FreshnessPolicy")



@_attrs_define
class FreshnessPolicy:
    """
        Attributes:
            inventory_fresh_seconds (Union[Unset, int]):  Default: 300.
            telemetry_delayed_seconds (Union[Unset, int]):  Default: 20.
            telemetry_live_seconds (Union[Unset, int]):  Default: 6.
     """

    inventory_fresh_seconds: Union[Unset, int] = 300
    telemetry_delayed_seconds: Union[Unset, int] = 20
    telemetry_live_seconds: Union[Unset, int] = 6





    def to_dict(self) -> dict[str, Any]:
        inventory_fresh_seconds = self.inventory_fresh_seconds

        telemetry_delayed_seconds = self.telemetry_delayed_seconds

        telemetry_live_seconds = self.telemetry_live_seconds


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if inventory_fresh_seconds is not UNSET:
            field_dict["inventory_fresh_seconds"] = inventory_fresh_seconds
        if telemetry_delayed_seconds is not UNSET:
            field_dict["telemetry_delayed_seconds"] = telemetry_delayed_seconds
        if telemetry_live_seconds is not UNSET:
            field_dict["telemetry_live_seconds"] = telemetry_live_seconds

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        inventory_fresh_seconds = d.pop("inventory_fresh_seconds", UNSET)

        telemetry_delayed_seconds = d.pop("telemetry_delayed_seconds", UNSET)

        telemetry_live_seconds = d.pop("telemetry_live_seconds", UNSET)

        freshness_policy = cls(
            inventory_fresh_seconds=inventory_fresh_seconds,
            telemetry_delayed_seconds=telemetry_delayed_seconds,
            telemetry_live_seconds=telemetry_live_seconds,
        )

        return freshness_policy
