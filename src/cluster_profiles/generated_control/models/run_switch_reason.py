from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_reason_scope import check_run_switch_reason_scope
from ..models.run_switch_reason_scope import RunSwitchReasonScope
from ..models.run_switch_reason_severity import check_run_switch_reason_severity
from ..models.run_switch_reason_severity import RunSwitchReasonSeverity
from ..types import UNSET, Unset
from typing import cast
from typing import Union






T = TypeVar("T", bound="RunSwitchReason")



@_attrs_define
class RunSwitchReason:
    """
        Attributes:
            code (str):
            detail (str):
            scope (RunSwitchReasonScope):
            severity (RunSwitchReasonSeverity):
            node_ids (Union[Unset, list[str]]):
            stale (Union[Unset, bool]):  Default: False.
     """

    code: str
    detail: str
    scope: RunSwitchReasonScope
    severity: RunSwitchReasonSeverity
    node_ids: Union[Unset, list[str]] = UNSET
    stale: Union[Unset, bool] = False





    def to_dict(self) -> dict[str, Any]:
        code = self.code

        detail = self.detail

        scope: str = self.scope

        severity: str = self.severity

        node_ids: Union[Unset, list[str]] = UNSET
        if not isinstance(self.node_ids, Unset):
            node_ids = self.node_ids



        stale = self.stale


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "code": code,
            "detail": detail,
            "scope": scope,
            "severity": severity,
        })
        if node_ids is not UNSET:
            field_dict["node_ids"] = node_ids
        if stale is not UNSET:
            field_dict["stale"] = stale

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        code = d.pop("code")

        detail = d.pop("detail")

        scope = check_run_switch_reason_scope(d.pop("scope"))




        severity = check_run_switch_reason_severity(d.pop("severity"))




        node_ids = cast(list[str], d.pop("node_ids", UNSET))


        stale = d.pop("stale", UNSET)

        run_switch_reason = cls(
            code=code,
            detail=detail,
            scope=scope,
            severity=severity,
            node_ids=node_ids,
            stale=stale,
        )

        return run_switch_reason
