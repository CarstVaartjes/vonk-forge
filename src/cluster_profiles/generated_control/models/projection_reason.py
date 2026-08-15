from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.projection_reason_code import check_projection_reason_code
from ..models.projection_reason_code import ProjectionReasonCode
from ..models.projection_reason_severity import check_projection_reason_severity
from ..models.projection_reason_severity import ProjectionReasonSeverity
from typing import cast






T = TypeVar("T", bound="ProjectionReason")



@_attrs_define
class ProjectionReason:
    """
        Attributes:
            code (ProjectionReasonCode):
            detail (str):
            severity (ProjectionReasonSeverity):
     """

    code: ProjectionReasonCode
    detail: str
    severity: ProjectionReasonSeverity





    def to_dict(self) -> dict[str, Any]:
        code: str = self.code

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
        code = check_projection_reason_code(d.pop("code"))




        detail = d.pop("detail")

        severity = check_projection_reason_severity(d.pop("severity"))




        projection_reason = cls(
            code=code,
            detail=detail,
            severity=severity,
        )

        return projection_reason
