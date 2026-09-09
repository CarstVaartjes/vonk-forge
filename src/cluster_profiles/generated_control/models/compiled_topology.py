from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.compiled_topology_mode import check_compiled_topology_mode
from ..models.compiled_topology_mode import CompiledTopologyMode
from typing import cast






T = TypeVar("T", bound="CompiledTopology")



@_attrs_define
class CompiledTopology:
    """
        Attributes:
            backend (str):
            mode (CompiledTopologyMode):
            name (str):
            node_count (int):
            rank (int):
            role (str):
            world_size (int):
     """

    backend: str
    mode: CompiledTopologyMode
    name: str
    node_count: int
    rank: int
    role: str
    world_size: int





    def to_dict(self) -> dict[str, Any]:
        backend = self.backend

        mode: str = self.mode

        name = self.name

        node_count = self.node_count

        rank = self.rank

        role = self.role

        world_size = self.world_size


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "backend": backend,
            "mode": mode,
            "name": name,
            "node_count": node_count,
            "rank": rank,
            "role": role,
            "world_size": world_size,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        backend = d.pop("backend")

        mode = check_compiled_topology_mode(d.pop("mode"))




        name = d.pop("name")

        node_count = d.pop("node_count")

        rank = d.pop("rank")

        role = d.pop("role")

        world_size = d.pop("world_size")

        compiled_topology = cls(
            backend=backend,
            mode=mode,
            name=name,
            node_count=node_count,
            rank=rank,
            role=role,
            world_size=world_size,
        )

        return compiled_topology
