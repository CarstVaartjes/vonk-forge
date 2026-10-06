from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="CompiledTopology")



@_attrs_define
class CompiledTopology:
    """
        Attributes:
            name (str):
            node_count (int):
     """

    name: str
    node_count: int





    def to_dict(self) -> dict[str, Any]:
        name = self.name

        node_count = self.node_count


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "name": name,
            "node_count": node_count,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        name = d.pop("name")

        node_count = d.pop("node_count")

        compiled_topology = cls(
            name=name,
            node_count=node_count,
        )

        return compiled_topology
