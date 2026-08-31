from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.library_placement_reason_severity import check_library_placement_reason_severity
from ..models.library_placement_reason_severity import LibraryPlacementReasonSeverity
from ..types import UNSET, Unset
from typing import cast
from typing import Union






T = TypeVar("T", bound="LibraryPlacementReason")



@_attrs_define
class LibraryPlacementReason:
    """
        Attributes:
            code (str):
            detail (str):
            severity (LibraryPlacementReasonSeverity):
            node_ids (Union[Unset, list[str]]):
     """

    code: str
    detail: str
    severity: LibraryPlacementReasonSeverity
    node_ids: Union[Unset, list[str]] = UNSET





    def to_dict(self) -> dict[str, Any]:
        code = self.code

        detail = self.detail

        severity: str = self.severity

        node_ids: Union[Unset, list[str]] = UNSET
        if not isinstance(self.node_ids, Unset):
            node_ids = self.node_ids




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "code": code,
            "detail": detail,
            "severity": severity,
        })
        if node_ids is not UNSET:
            field_dict["node_ids"] = node_ids

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        code = d.pop("code")

        detail = d.pop("detail")

        severity = check_library_placement_reason_severity(d.pop("severity"))




        node_ids = cast(list[str], d.pop("node_ids", UNSET))


        library_placement_reason = cls(
            code=code,
            detail=detail,
            severity=severity,
            node_ids=node_ids,
        )

        return library_placement_reason
