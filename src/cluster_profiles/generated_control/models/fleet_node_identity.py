from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="FleetNodeIdentity")



@_attrs_define
class FleetNodeIdentity:
    """
        Attributes:
            display_name (str):
            hostname (str):
            id (str):
            management_address (Union[None, Unset, str]):
     """

    display_name: str
    hostname: str
    id: str
    management_address: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        display_name = self.display_name

        hostname = self.hostname

        id = self.id

        management_address: Union[None, Unset, str]
        if isinstance(self.management_address, Unset):
            management_address = UNSET
        else:
            management_address = self.management_address


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "display_name": display_name,
            "hostname": hostname,
            "id": id,
        })
        if management_address is not UNSET:
            field_dict["management_address"] = management_address

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        display_name = d.pop("display_name")

        hostname = d.pop("hostname")

        id = d.pop("id")

        def _parse_management_address(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        management_address = _parse_management_address(d.pop("management_address", UNSET))


        fleet_node_identity = cls(
            display_name=display_name,
            hostname=hostname,
            id=id,
            management_address=management_address,
        )

        return fleet_node_identity
