from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import Literal, cast
from typing import Union
import datetime

if TYPE_CHECKING:
  from ..models.job_payload import JobPayload





T = TypeVar("T", bound="JobChange")



@_attrs_define
class JobChange:
    """
        Attributes:
            entity_id (str):
            entity_kind (Literal['job']):
            fields (JobPayload):
            occurred_at (datetime.datetime):
            node_id (Union[Unset, None]):
     """

    entity_id: str
    entity_kind: Literal['job']
    fields: 'JobPayload'
    occurred_at: datetime.datetime
    node_id: Union[Unset, None] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.job_payload import JobPayload
        entity_id = self.entity_id

        entity_kind = self.entity_kind

        fields = self.fields.to_dict()

        occurred_at = self.occurred_at.isoformat()

        node_id = self.node_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "entity_id": entity_id,
            "entity_kind": entity_kind,
            "fields": fields,
            "occurred_at": occurred_at,
        })
        if node_id is not UNSET:
            field_dict["node_id"] = node_id

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.job_payload import JobPayload
        d = dict(src_dict)
        entity_id = d.pop("entity_id")

        entity_kind = cast(Literal['job'] , d.pop("entity_kind"))
        if entity_kind != 'job':
            raise ValueError(f"entity_kind must match const 'job', got '{entity_kind}'")

        fields = JobPayload.from_dict(d.pop("fields"))




        occurred_at = isoparse(d.pop("occurred_at"))




        node_id = d.pop("node_id", UNSET)

        job_change = cls(
            entity_id=entity_id,
            entity_kind=entity_kind,
            fields=fields,
            occurred_at=occurred_at,
            node_id=node_id,
        )

        return job_change
