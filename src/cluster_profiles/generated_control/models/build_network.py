from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="BuildNetwork")



@_attrs_define
class BuildNetwork:
    """
        Attributes:
            hosts (list[str]):
     """

    hosts: list[str]





    def to_dict(self) -> dict[str, Any]:
        hosts = self.hosts




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "hosts": hosts,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        hosts = cast(list[str], d.pop("hosts"))


        build_network = cls(
            hosts=hosts,
        )

        return build_network
