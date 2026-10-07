from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_frame_issue_reason_code import check_fleet_frame_issue_reason_code
from ..models.fleet_frame_issue_reason_code import FleetFrameIssueReasonCode
from typing import cast






T = TypeVar("T", bound="FleetFrameIssue")



@_attrs_define
class FleetFrameIssue:
    """
        Attributes:
            budget_bytes (int):
            observed_bytes_at_least (int | None):
            reason_code (FleetFrameIssueReasonCode):
     """

    budget_bytes: int
    observed_bytes_at_least: int | None
    reason_code: FleetFrameIssueReasonCode





    def to_dict(self) -> dict[str, Any]:
        budget_bytes = self.budget_bytes

        observed_bytes_at_least: int | None
        observed_bytes_at_least = self.observed_bytes_at_least

        reason_code: str = self.reason_code


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "budget_bytes": budget_bytes,
            "observed_bytes_at_least": observed_bytes_at_least,
            "reason_code": reason_code,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        budget_bytes = d.pop("budget_bytes")

        def _parse_observed_bytes_at_least(data: object) -> int | None:
            if data is None:
                return data
            return cast(int | None, data)

        observed_bytes_at_least = _parse_observed_bytes_at_least(d.pop("observed_bytes_at_least"))


        reason_code = check_fleet_frame_issue_reason_code(d.pop("reason_code"))




        fleet_frame_issue = cls(
            budget_bytes=budget_bytes,
            observed_bytes_at_least=observed_bytes_at_least,
            reason_code=reason_code,
        )

        return fleet_frame_issue
