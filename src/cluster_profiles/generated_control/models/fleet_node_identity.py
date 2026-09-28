from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="FleetNodeIdentity")



@_attrs_define
class FleetNodeIdentity:
    """
        Attributes:
            display_name (str):
            hostname (str):
            id (str):
            ip_address (None | str | Unset):
     """

    display_name: str
    hostname: str
    id: str
    ip_address: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        display_name = self.display_name

        hostname = self.hostname

        id = self.id

        ip_address: None | str | Unset
        if isinstance(self.ip_address, Unset):
            ip_address = UNSET
        else:
            ip_address = self.ip_address


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "display_name": display_name,
            "hostname": hostname,
            "id": id,
        })
        if ip_address is not UNSET:
            field_dict["ip_address"] = ip_address

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        display_name = d.pop("display_name")

        hostname = d.pop("hostname")

        id = d.pop("id")

        def _parse_ip_address(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        ip_address = _parse_ip_address(d.pop("ip_address", UNSET))


        fleet_node_identity = cls(
            display_name=display_name,
            hostname=hostname,
            id=id,
            ip_address=ip_address,
        )

        return fleet_node_identity
