from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.operation_projection_issue_field import check_operation_projection_issue_field
from ..models.operation_projection_issue_field import OperationProjectionIssueField
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="OperationProjectionIssue")



@_attrs_define
class OperationProjectionIssue:
    """ One optional fact unavailable within this response's reader allocation.

        Attributes:
            budget_bytes (int):
            field (OperationProjectionIssueField):
            observed_bytes (int):
            reason (Literal['response-budget-exceeded'] | Unset):  Default: 'response-budget-exceeded'.
     """

    budget_bytes: int
    field: OperationProjectionIssueField
    observed_bytes: int
    reason: Literal['response-budget-exceeded'] | Unset = 'response-budget-exceeded'





    def to_dict(self) -> dict[str, Any]:
        budget_bytes = self.budget_bytes

        field: str = self.field

        observed_bytes = self.observed_bytes

        reason = self.reason


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "budget_bytes": budget_bytes,
            "field": field,
            "observed_bytes": observed_bytes,
        })
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        budget_bytes = d.pop("budget_bytes")

        field = check_operation_projection_issue_field(d.pop("field"))




        observed_bytes = d.pop("observed_bytes")

        reason = cast(Literal['response-budget-exceeded'] | Unset , d.pop("reason", UNSET))
        if reason != 'response-budget-exceeded' and not isinstance(reason, Unset):
            raise ValueError(f"reason must match const 'response-budget-exceeded', got '{reason}'")

        operation_projection_issue = cls(
            budget_bytes=budget_bytes,
            field=field,
            observed_bytes=observed_bytes,
            reason=reason,
        )

        return operation_projection_issue
