from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.audit_event_response import AuditEventResponse





T = TypeVar("T", bound="AuditResponse")



@_attrs_define
class AuditResponse:
    """
        Attributes:
            events (list['AuditEventResponse']):
     """

    events: list['AuditEventResponse']





    def to_dict(self) -> dict[str, Any]:
        from ..models.audit_event_response import AuditEventResponse
        events = []
        for events_item_data in self.events:
            events_item = events_item_data.to_dict()
            events.append(events_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "events": events,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.audit_event_response import AuditEventResponse
        d = dict(src_dict)
        events = []
        _events = d.pop("events")
        for events_item_data in (_events):
            events_item = AuditEventResponse.from_dict(events_item_data)



            events.append(events_item)


        audit_response = cls(
            events=events,
        )

        return audit_response
