from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_reason_severity import check_fleet_profile_reason_severity
from ..models.fleet_profile_reason_severity import FleetProfileReasonSeverity
from typing import cast






T = TypeVar("T", bound="FleetProfileReason")



@_attrs_define
class FleetProfileReason:
    """
        Attributes:
            code (str):
            detail (str):
            severity (FleetProfileReasonSeverity):
     """

    code: str
    detail: str
    severity: FleetProfileReasonSeverity





    def to_dict(self) -> dict[str, Any]:
        code = self.code

        detail = self.detail

        severity: str = self.severity


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "code": code,
            "detail": detail,
            "severity": severity,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        code = d.pop("code")

        detail = d.pop("detail")

        severity = check_fleet_profile_reason_severity(d.pop("severity"))




        fleet_profile_reason = cls(
            code=code,
            detail=detail,
            severity=severity,
        )

        return fleet_profile_reason
