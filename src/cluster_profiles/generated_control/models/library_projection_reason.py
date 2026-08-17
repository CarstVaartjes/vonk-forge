from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.library_projection_reason_severity import check_library_projection_reason_severity
from ..models.library_projection_reason_severity import LibraryProjectionReasonSeverity
from typing import cast






T = TypeVar("T", bound="LibraryProjectionReason")



@_attrs_define
class LibraryProjectionReason:
    """
        Attributes:
            code (str):
            detail (str):
            severity (LibraryProjectionReasonSeverity):
     """

    code: str
    detail: str
    severity: LibraryProjectionReasonSeverity





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

        severity = check_library_projection_reason_severity(d.pop("severity"))




        library_projection_reason = cls(
            code=code,
            detail=detail,
            severity=severity,
        )

        return library_projection_reason
