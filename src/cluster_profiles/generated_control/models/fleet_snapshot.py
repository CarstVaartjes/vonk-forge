from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
import datetime

if TYPE_CHECKING:
  from ..models.fleet_node import FleetNode





T = TypeVar("T", bound="FleetSnapshot")



@_attrs_define
class FleetSnapshot:
    """
        Attributes:
            authority_revision (str):
            event_cursor (int):
            generated_at (datetime.datetime):
            nodes (list[FleetNode]):
     """

    authority_revision: str
    event_cursor: int
    generated_at: datetime.datetime
    nodes: list[FleetNode]





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_node import FleetNode # noqa: PLC0415
        authority_revision = self.authority_revision

        event_cursor = self.event_cursor

        generated_at = self.generated_at.isoformat()

        nodes = []
        for nodes_item_data in self.nodes:
            nodes_item = nodes_item_data.to_dict()
            nodes.append(nodes_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "authority_revision": authority_revision,
            "event_cursor": event_cursor,
            "generated_at": generated_at,
            "nodes": nodes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_node import FleetNode # noqa: PLC0415
        d = dict(src_dict)
        authority_revision = d.pop("authority_revision")

        event_cursor = d.pop("event_cursor")

        generated_at = datetime.datetime.fromisoformat(d.pop("generated_at"))




        nodes = []
        _nodes = d.pop("nodes")
        for nodes_item_data in (_nodes):
            nodes_item = FleetNode.from_dict(nodes_item_data)



            nodes.append(nodes_item)


        fleet_snapshot = cls(
            authority_revision=authority_revision,
            event_cursor=event_cursor,
            generated_at=generated_at,
            nodes=nodes,
        )

        return fleet_snapshot
