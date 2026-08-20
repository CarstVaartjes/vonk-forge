from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import Literal, Union, cast
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
            nodes (list['FleetNode']):
            schema_version (Union[Literal[1], Unset]):  Default: 1.
     """

    authority_revision: str
    event_cursor: int
    generated_at: datetime.datetime
    nodes: list['FleetNode']
    schema_version: Union[Literal[1], Unset] = 1





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_node import FleetNode
        authority_revision = self.authority_revision

        event_cursor = self.event_cursor

        generated_at = self.generated_at.isoformat()

        nodes = []
        for nodes_item_data in self.nodes:
            nodes_item = nodes_item_data.to_dict()
            nodes.append(nodes_item)



        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "authority_revision": authority_revision,
            "event_cursor": event_cursor,
            "generated_at": generated_at,
            "nodes": nodes,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_node import FleetNode
        d = dict(src_dict)
        authority_revision = d.pop("authority_revision")

        event_cursor = d.pop("event_cursor")

        generated_at = isoparse(d.pop("generated_at"))




        nodes = []
        _nodes = d.pop("nodes")
        for nodes_item_data in (_nodes):
            nodes_item = FleetNode.from_dict(nodes_item_data)



            nodes.append(nodes_item)


        schema_version = cast(Union[Literal[1], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 1 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        fleet_snapshot = cls(
            authority_revision=authority_revision,
            event_cursor=event_cursor,
            generated_at=generated_at,
            nodes=nodes,
            schema_version=schema_version,
        )

        return fleet_snapshot
