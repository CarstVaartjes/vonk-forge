from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast, Union






T = TypeVar("T", bound="CompiledPlacement")



@_attrs_define
class CompiledPlacement:
    """
        Attributes:
            endpoint_address (Union[None, str]):
            local_address (Union[None, str]):
            master_address (Union[None, str]):
            master_port (Union[None, int]):
            port (Union[None, int]):
            rank (int):
            reserved_memory_bytes (int):
            role (str):
            world_size (int):
     """

    endpoint_address: Union[None, str]
    local_address: Union[None, str]
    master_address: Union[None, str]
    master_port: Union[None, int]
    port: Union[None, int]
    rank: int
    reserved_memory_bytes: int
    role: str
    world_size: int





    def to_dict(self) -> dict[str, Any]:
        endpoint_address: Union[None, str]
        endpoint_address = self.endpoint_address

        local_address: Union[None, str]
        local_address = self.local_address

        master_address: Union[None, str]
        master_address = self.master_address

        master_port: Union[None, int]
        master_port = self.master_port

        port: Union[None, int]
        port = self.port

        rank = self.rank

        reserved_memory_bytes = self.reserved_memory_bytes

        role = self.role

        world_size = self.world_size


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "endpoint_address": endpoint_address,
            "local_address": local_address,
            "master_address": master_address,
            "master_port": master_port,
            "port": port,
            "rank": rank,
            "reserved_memory_bytes": reserved_memory_bytes,
            "role": role,
            "world_size": world_size,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        def _parse_endpoint_address(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        endpoint_address = _parse_endpoint_address(d.pop("endpoint_address"))


        def _parse_local_address(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        local_address = _parse_local_address(d.pop("local_address"))


        def _parse_master_address(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        master_address = _parse_master_address(d.pop("master_address"))


        def _parse_master_port(data: object) -> Union[None, int]:
            if data is None:
                return data
            return cast(Union[None, int], data)

        master_port = _parse_master_port(d.pop("master_port"))


        def _parse_port(data: object) -> Union[None, int]:
            if data is None:
                return data
            return cast(Union[None, int], data)

        port = _parse_port(d.pop("port"))


        rank = d.pop("rank")

        reserved_memory_bytes = d.pop("reserved_memory_bytes")

        role = d.pop("role")

        world_size = d.pop("world_size")

        compiled_placement = cls(
            endpoint_address=endpoint_address,
            local_address=local_address,
            master_address=master_address,
            master_port=master_port,
            port=port,
            rank=rank,
            reserved_memory_bytes=reserved_memory_bytes,
            role=role,
            world_size=world_size,
        )

        return compiled_placement
