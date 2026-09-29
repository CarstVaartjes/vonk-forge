from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.operation_blocker_severity import check_operation_blocker_severity
from ..models.operation_blocker_severity import OperationBlockerSeverity
from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="OperationBlocker")



@_attrs_define
class OperationBlocker:
    """ One reason an operation is waiting or blocked, with the Sparks it concerns.

        Attributes:
            code (str):
            detail (str):
            severity (OperationBlockerSeverity):
            node_ids (list[str] | Unset):
     """

    code: str
    detail: str
    severity: OperationBlockerSeverity
    node_ids: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        code = self.code

        detail = self.detail

        severity: str = self.severity

        node_ids: list[str] | Unset = UNSET
        if not isinstance(self.node_ids, Unset):
            node_ids = self.node_ids




        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
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

        severity = check_operation_blocker_severity(d.pop("severity"))




        node_ids = cast(list[str], d.pop("node_ids", UNSET))


        operation_blocker = cls(
            code=code,
            detail=detail,
            severity=severity,
            node_ids=node_ids,
        )


        operation_blocker.additional_properties = d
        return operation_blocker

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
