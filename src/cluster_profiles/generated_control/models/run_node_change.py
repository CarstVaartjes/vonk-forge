from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from dateutil.parser import isoparse
from typing import cast
from typing import Literal, cast
import datetime

if TYPE_CHECKING:
  from ..models.run_node_payload import RunNodePayload





T = TypeVar("T", bound="RunNodeChange")



@_attrs_define
class RunNodeChange:
    """
        Attributes:
            entity_id (str):
            entity_kind (Literal['run-node']):
            fields (RunNodePayload):
            node_id (str):
            occurred_at (datetime.datetime):
     """

    entity_id: str
    entity_kind: Literal['run-node']
    fields: 'RunNodePayload'
    node_id: str
    occurred_at: datetime.datetime





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_node_payload import RunNodePayload
        entity_id = self.entity_id

        entity_kind = self.entity_kind

        fields = self.fields.to_dict()

        node_id = self.node_id

        occurred_at = self.occurred_at.isoformat()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "entity_id": entity_id,
            "entity_kind": entity_kind,
            "fields": fields,
            "node_id": node_id,
            "occurred_at": occurred_at,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_node_payload import RunNodePayload
        d = dict(src_dict)
        entity_id = d.pop("entity_id")

        entity_kind = cast(Literal['run-node'] , d.pop("entity_kind"))
        if entity_kind != 'run-node':
            raise ValueError(f"entity_kind must match const 'run-node', got '{entity_kind}'")

        fields = RunNodePayload.from_dict(d.pop("fields"))




        node_id = d.pop("node_id")

        occurred_at = isoparse(d.pop("occurred_at"))




        run_node_change = cls(
            entity_id=entity_id,
            entity_kind=entity_kind,
            fields=fields,
            node_id=node_id,
            occurred_at=occurred_at,
        )

        return run_node_change
