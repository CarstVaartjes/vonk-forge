from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="LibraryPlacementNode")



@_attrs_define
class LibraryPlacementNode:
    """
        Attributes:
            endpoint_owner (bool):
            node_id (str):
            rank (int):
            role (str):
            disk_free_after_bytes (Union[None, Unset, int]):
            disk_free_bytes (Union[None, Unset, int]):
            disk_required_bytes (Union[None, Unset, int]):
            memory_available_bytes (Union[None, Unset, int]):
            memory_free_after_bytes (Union[None, Unset, int]):
            memory_required_bytes (Union[None, Unset, int]):
     """

    endpoint_owner: bool
    node_id: str
    rank: int
    role: str
    disk_free_after_bytes: Union[None, Unset, int] = UNSET
    disk_free_bytes: Union[None, Unset, int] = UNSET
    disk_required_bytes: Union[None, Unset, int] = UNSET
    memory_available_bytes: Union[None, Unset, int] = UNSET
    memory_free_after_bytes: Union[None, Unset, int] = UNSET
    memory_required_bytes: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        endpoint_owner = self.endpoint_owner

        node_id = self.node_id

        rank = self.rank

        role = self.role

        disk_free_after_bytes: Union[None, Unset, int]
        if isinstance(self.disk_free_after_bytes, Unset):
            disk_free_after_bytes = UNSET
        else:
            disk_free_after_bytes = self.disk_free_after_bytes

        disk_free_bytes: Union[None, Unset, int]
        if isinstance(self.disk_free_bytes, Unset):
            disk_free_bytes = UNSET
        else:
            disk_free_bytes = self.disk_free_bytes

        disk_required_bytes: Union[None, Unset, int]
        if isinstance(self.disk_required_bytes, Unset):
            disk_required_bytes = UNSET
        else:
            disk_required_bytes = self.disk_required_bytes

        memory_available_bytes: Union[None, Unset, int]
        if isinstance(self.memory_available_bytes, Unset):
            memory_available_bytes = UNSET
        else:
            memory_available_bytes = self.memory_available_bytes

        memory_free_after_bytes: Union[None, Unset, int]
        if isinstance(self.memory_free_after_bytes, Unset):
            memory_free_after_bytes = UNSET
        else:
            memory_free_after_bytes = self.memory_free_after_bytes

        memory_required_bytes: Union[None, Unset, int]
        if isinstance(self.memory_required_bytes, Unset):
            memory_required_bytes = UNSET
        else:
            memory_required_bytes = self.memory_required_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "endpoint_owner": endpoint_owner,
            "node_id": node_id,
            "rank": rank,
            "role": role,
        })
        if disk_free_after_bytes is not UNSET:
            field_dict["disk_free_after_bytes"] = disk_free_after_bytes
        if disk_free_bytes is not UNSET:
            field_dict["disk_free_bytes"] = disk_free_bytes
        if disk_required_bytes is not UNSET:
            field_dict["disk_required_bytes"] = disk_required_bytes
        if memory_available_bytes is not UNSET:
            field_dict["memory_available_bytes"] = memory_available_bytes
        if memory_free_after_bytes is not UNSET:
            field_dict["memory_free_after_bytes"] = memory_free_after_bytes
        if memory_required_bytes is not UNSET:
            field_dict["memory_required_bytes"] = memory_required_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        endpoint_owner = d.pop("endpoint_owner")

        node_id = d.pop("node_id")

        rank = d.pop("rank")

        role = d.pop("role")

        def _parse_disk_free_after_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        disk_free_after_bytes = _parse_disk_free_after_bytes(d.pop("disk_free_after_bytes", UNSET))


        def _parse_disk_free_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        disk_free_bytes = _parse_disk_free_bytes(d.pop("disk_free_bytes", UNSET))


        def _parse_disk_required_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        disk_required_bytes = _parse_disk_required_bytes(d.pop("disk_required_bytes", UNSET))


        def _parse_memory_available_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        memory_available_bytes = _parse_memory_available_bytes(d.pop("memory_available_bytes", UNSET))


        def _parse_memory_free_after_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        memory_free_after_bytes = _parse_memory_free_after_bytes(d.pop("memory_free_after_bytes", UNSET))


        def _parse_memory_required_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        memory_required_bytes = _parse_memory_required_bytes(d.pop("memory_required_bytes", UNSET))


        library_placement_node = cls(
            endpoint_owner=endpoint_owner,
            node_id=node_id,
            rank=rank,
            role=role,
            disk_free_after_bytes=disk_free_after_bytes,
            disk_free_bytes=disk_free_bytes,
            disk_required_bytes=disk_required_bytes,
            memory_available_bytes=memory_available_bytes,
            memory_free_after_bytes=memory_free_after_bytes,
            memory_required_bytes=memory_required_bytes,
        )

        return library_placement_node
