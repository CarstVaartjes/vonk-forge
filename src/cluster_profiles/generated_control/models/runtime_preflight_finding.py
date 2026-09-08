from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.runtime_preflight_finding_status import check_runtime_preflight_finding_status
from ..models.runtime_preflight_finding_status import RuntimePreflightFindingStatus
from typing import cast






T = TypeVar("T", bound="RuntimePreflightFinding")



@_attrs_define
class RuntimePreflightFinding:
    """
        Attributes:
            capability (str):
            code (str):
            status (RuntimePreflightFindingStatus):
     """

    capability: str
    code: str
    status: RuntimePreflightFindingStatus





    def to_dict(self) -> dict[str, Any]:
        capability = self.capability

        code = self.code

        status: str = self.status


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "capability": capability,
            "code": code,
            "status": status,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        capability = d.pop("capability")

        code = d.pop("code")

        status = check_runtime_preflight_finding_status(d.pop("status"))




        runtime_preflight_finding = cls(
            capability=capability,
            code=code,
            status=status,
        )

        return runtime_preflight_finding
