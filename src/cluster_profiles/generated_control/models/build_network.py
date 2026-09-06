from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.build_network_mode import BuildNetworkMode
from ..models.build_network_mode import check_build_network_mode
from typing import cast






T = TypeVar("T", bound="BuildNetwork")



@_attrs_define
class BuildNetwork:
    """
        Attributes:
            hosts (list[str]):
            mode (BuildNetworkMode):
     """

    hosts: list[str]
    mode: BuildNetworkMode





    def to_dict(self) -> dict[str, Any]:
        hosts = self.hosts



        mode: str = self.mode


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "hosts": hosts,
            "mode": mode,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        hosts = cast(list[str], d.pop("hosts"))


        mode = check_build_network_mode(d.pop("mode"))




        build_network = cls(
            hosts=hosts,
            mode=mode,
        )

        return build_network
