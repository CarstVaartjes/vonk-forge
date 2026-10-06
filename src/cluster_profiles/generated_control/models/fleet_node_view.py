from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="FleetNodeView")



@_attrs_define
class FleetNodeView:
    """ One Spark of the fleet and whether the profile assigns it.

        Attributes:
            display_name (str):
            selector (str):
            state (str):
     """

    display_name: str
    selector: str
    state: str





    def to_dict(self) -> dict[str, Any]:
        display_name = self.display_name

        selector = self.selector

        state = self.state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "display_name": display_name,
            "selector": selector,
            "state": state,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        display_name = d.pop("display_name")

        selector = d.pop("selector")

        state = d.pop("state")

        fleet_node_view = cls(
            display_name=display_name,
            selector=selector,
            state=state,
        )

        return fleet_node_view
