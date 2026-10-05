from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.projection_reason_code import check_projection_reason_code
from ..models.projection_reason_code import ProjectionReasonCode
from ..models.projection_reason_severity import check_projection_reason_severity
from ..models.projection_reason_severity import ProjectionReasonSeverity
from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.install_partial_evidence import InstallPartialEvidence





T = TypeVar("T", bound="ProjectionReason")



@_attrs_define
class ProjectionReason:
    """
        Attributes:
            code (ProjectionReasonCode):
            detail (str):
            severity (ProjectionReasonSeverity):
            install_partial (InstallPartialEvidence | None | Unset):
     """

    code: ProjectionReasonCode
    detail: str
    severity: ProjectionReasonSeverity
    install_partial: InstallPartialEvidence | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.install_partial_evidence import InstallPartialEvidence # noqa: PLC0415
        code: str = self.code

        detail = self.detail

        severity: str = self.severity

        install_partial: dict[str, Any] | None | Unset
        if isinstance(self.install_partial, Unset):
            install_partial = UNSET
        elif isinstance(self.install_partial, InstallPartialEvidence):
            install_partial = self.install_partial.to_dict()
        else:
            install_partial = self.install_partial


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "code": code,
            "detail": detail,
            "severity": severity,
        })
        if install_partial is not UNSET:
            field_dict["install_partial"] = install_partial

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.install_partial_evidence import InstallPartialEvidence # noqa: PLC0415
        d = dict(src_dict)
        code = check_projection_reason_code(d.pop("code"))




        detail = d.pop("detail")

        severity = check_projection_reason_severity(d.pop("severity"))




        def _parse_install_partial(data: object) -> InstallPartialEvidence | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                install_partial_type_0 = InstallPartialEvidence.from_dict(data)



                return install_partial_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(InstallPartialEvidence | None | Unset, data)

        install_partial = _parse_install_partial(d.pop("install_partial", UNSET))


        projection_reason = cls(
            code=code,
            detail=detail,
            severity=severity,
            install_partial=install_partial,
        )

        return projection_reason
