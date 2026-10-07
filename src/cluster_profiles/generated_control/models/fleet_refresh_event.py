from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_refresh_event_reset_reason import check_fleet_refresh_event_reset_reason
from ..models.fleet_refresh_event_reset_reason import FleetRefreshEventResetReason
from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.fleet_frame_issue import FleetFrameIssue





T = TypeVar("T", bound="FleetRefreshEvent")



@_attrs_define
class FleetRefreshEvent:
    """
        Attributes:
            event_cursor (int):
            reset_reason (FleetRefreshEventResetReason):
            issue (FleetFrameIssue | None | Unset):
     """

    event_cursor: int
    reset_reason: FleetRefreshEventResetReason
    issue: FleetFrameIssue | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_frame_issue import FleetFrameIssue # noqa: PLC0415
        event_cursor = self.event_cursor

        reset_reason: str = self.reset_reason

        issue: dict[str, Any] | None | Unset
        if isinstance(self.issue, Unset):
            issue = UNSET
        elif isinstance(self.issue, FleetFrameIssue):
            issue = self.issue.to_dict()
        else:
            issue = self.issue


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "event_cursor": event_cursor,
            "reset_reason": reset_reason,
        })
        if issue is not UNSET:
            field_dict["issue"] = issue

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_frame_issue import FleetFrameIssue # noqa: PLC0415
        d = dict(src_dict)
        event_cursor = d.pop("event_cursor")

        reset_reason = check_fleet_refresh_event_reset_reason(d.pop("reset_reason"))




        def _parse_issue(data: object) -> FleetFrameIssue | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                issue_type_0 = FleetFrameIssue.from_dict(data)



                return issue_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FleetFrameIssue | None | Unset, data)

        issue = _parse_issue(d.pop("issue", UNSET))


        fleet_refresh_event = cls(
            event_cursor=event_cursor,
            reset_reason=reset_reason,
            issue=issue,
        )

        return fleet_refresh_event
