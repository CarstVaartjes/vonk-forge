from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
import datetime

if TYPE_CHECKING:
  from ..models.fleet_node import FleetNode
  from ..models.prometheus_attention import PrometheusAttention





T = TypeVar("T", bound="FleetSnapshot")



@_attrs_define
class FleetSnapshot:
    """
        Attributes:
            authority_revision (str):
            event_cursor (int):
            generated_at (datetime.datetime):
            nodes (list[FleetNode]):
            attention (list[PrometheusAttention] | None | Unset):
            attention_unavailable (bool | None | Unset):
     """

    authority_revision: str
    event_cursor: int
    generated_at: datetime.datetime
    nodes: list[FleetNode]
    attention: list[PrometheusAttention] | None | Unset = UNSET
    attention_unavailable: bool | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_node import FleetNode # noqa: PLC0415
        from ..models.prometheus_attention import PrometheusAttention # noqa: PLC0415
        authority_revision = self.authority_revision

        event_cursor = self.event_cursor

        generated_at = self.generated_at.isoformat()

        nodes = []
        for nodes_item_data in self.nodes:
            nodes_item = nodes_item_data.to_dict()
            nodes.append(nodes_item)



        attention: list[dict[str, Any]] | None | Unset
        if isinstance(self.attention, Unset):
            attention = UNSET
        elif isinstance(self.attention, list):
            attention = []
            for attention_type_0_item_data in self.attention:
                attention_type_0_item = attention_type_0_item_data.to_dict()
                attention.append(attention_type_0_item)


        else:
            attention = self.attention

        attention_unavailable: bool | None | Unset
        if isinstance(self.attention_unavailable, Unset):
            attention_unavailable = UNSET
        else:
            attention_unavailable = self.attention_unavailable


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "authority_revision": authority_revision,
            "event_cursor": event_cursor,
            "generated_at": generated_at,
            "nodes": nodes,
        })
        if attention is not UNSET:
            field_dict["attention"] = attention
        if attention_unavailable is not UNSET:
            field_dict["attention_unavailable"] = attention_unavailable

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_node import FleetNode # noqa: PLC0415
        from ..models.prometheus_attention import PrometheusAttention # noqa: PLC0415
        d = dict(src_dict)
        authority_revision = d.pop("authority_revision")

        event_cursor = d.pop("event_cursor")

        generated_at = datetime.datetime.fromisoformat(d.pop("generated_at"))




        nodes = []
        _nodes = d.pop("nodes")
        for nodes_item_data in (_nodes):
            nodes_item = FleetNode.from_dict(nodes_item_data)



            nodes.append(nodes_item)


        def _parse_attention(data: object) -> list[PrometheusAttention] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                attention_type_0 = []
                _attention_type_0 = data
                for attention_type_0_item_data in (_attention_type_0):
                    attention_type_0_item = PrometheusAttention.from_dict(attention_type_0_item_data)



                    attention_type_0.append(attention_type_0_item)

                return attention_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[PrometheusAttention] | None | Unset, data)

        attention = _parse_attention(d.pop("attention", UNSET))


        def _parse_attention_unavailable(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        attention_unavailable = _parse_attention_unavailable(d.pop("attention_unavailable", UNSET))


        fleet_snapshot = cls(
            authority_revision=authority_revision,
            event_cursor=event_cursor,
            generated_at=generated_at,
            nodes=nodes,
            attention=attention,
            attention_unavailable=attention_unavailable,
        )

        return fleet_snapshot
