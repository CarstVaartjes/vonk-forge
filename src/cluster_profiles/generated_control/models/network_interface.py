from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.network_interface_kind import check_network_interface_kind
from ..models.network_interface_kind import NetworkInterfaceKind
from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="NetworkInterface")



@_attrs_define
class NetworkInterface:
    """ One NIC as the agent reads it from sysfs.

        Attributes:
            carrier (bool):
            kind (NetworkInterfaceKind):
            name (str):
            link_speed_mbps (int | None | Unset):
     """

    carrier: bool
    kind: NetworkInterfaceKind
    name: str
    link_speed_mbps: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        carrier = self.carrier

        kind: str = self.kind

        name = self.name

        link_speed_mbps: int | None | Unset
        if isinstance(self.link_speed_mbps, Unset):
            link_speed_mbps = UNSET
        else:
            link_speed_mbps = self.link_speed_mbps


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "carrier": carrier,
            "kind": kind,
            "name": name,
        })
        if link_speed_mbps is not UNSET:
            field_dict["link_speed_mbps"] = link_speed_mbps

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        carrier = d.pop("carrier")

        kind = check_network_interface_kind(d.pop("kind"))




        name = d.pop("name")

        def _parse_link_speed_mbps(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        link_speed_mbps = _parse_link_speed_mbps(d.pop("link_speed_mbps", UNSET))


        network_interface = cls(
            carrier=carrier,
            kind=kind,
            name=name,
            link_speed_mbps=link_speed_mbps,
        )

        return network_interface
