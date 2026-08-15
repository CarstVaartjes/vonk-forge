from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="StopNodeImpactResponse")



@_attrs_define
class StopNodeImpactResponse:
    """
        Attributes:
            active_memory_reservation_bytes (int):
            node_id (str):
            rank (int):
            reserved_memory_bytes (int):
            role (str):
            state (str):
     """

    active_memory_reservation_bytes: int
    node_id: str
    rank: int
    reserved_memory_bytes: int
    role: str
    state: str





    def to_dict(self) -> dict[str, Any]:
        active_memory_reservation_bytes = self.active_memory_reservation_bytes

        node_id = self.node_id

        rank = self.rank

        reserved_memory_bytes = self.reserved_memory_bytes

        role = self.role

        state = self.state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "active_memory_reservation_bytes": active_memory_reservation_bytes,
            "node_id": node_id,
            "rank": rank,
            "reserved_memory_bytes": reserved_memory_bytes,
            "role": role,
            "state": state,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        active_memory_reservation_bytes = d.pop("active_memory_reservation_bytes")

        node_id = d.pop("node_id")

        rank = d.pop("rank")

        reserved_memory_bytes = d.pop("reserved_memory_bytes")

        role = d.pop("role")

        state = d.pop("state")

        stop_node_impact_response = cls(
            active_memory_reservation_bytes=active_memory_reservation_bytes,
            node_id=node_id,
            rank=rank,
            reserved_memory_bytes=reserved_memory_bytes,
            role=role,
            state=state,
        )

        return stop_node_impact_response
