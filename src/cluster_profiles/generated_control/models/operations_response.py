from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.operation_detail_response import OperationDetailResponse





T = TypeVar("T", bound="OperationsResponse")



@_attrs_define
class OperationsResponse:
    """
        Attributes:
            operations (list[OperationDetailResponse] | None):
            total (int | None):
            continuation_unavailable (bool | Unset):  Default: False.
            next_cursor (None | str | Unset):
            projection_issue (None | str | Unset):
     """

    operations: list[OperationDetailResponse] | None
    total: int | None
    continuation_unavailable: bool | Unset = False
    next_cursor: None | str | Unset = UNSET
    projection_issue: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.operation_detail_response import OperationDetailResponse # noqa: PLC0415
        operations: list[dict[str, Any]] | None
        if isinstance(self.operations, list):
            operations = []
            for operations_type_0_item_data in self.operations:
                operations_type_0_item = operations_type_0_item_data.to_dict()
                operations.append(operations_type_0_item)


        else:
            operations = self.operations

        total: int | None
        total = self.total

        continuation_unavailable = self.continuation_unavailable

        next_cursor: None | str | Unset
        if isinstance(self.next_cursor, Unset):
            next_cursor = UNSET
        else:
            next_cursor = self.next_cursor

        projection_issue: None | str | Unset
        if isinstance(self.projection_issue, Unset):
            projection_issue = UNSET
        else:
            projection_issue = self.projection_issue


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "operations": operations,
            "total": total,
        })
        if continuation_unavailable is not UNSET:
            field_dict["continuation_unavailable"] = continuation_unavailable
        if next_cursor is not UNSET:
            field_dict["next_cursor"] = next_cursor
        if projection_issue is not UNSET:
            field_dict["projection_issue"] = projection_issue

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.operation_detail_response import OperationDetailResponse # noqa: PLC0415
        d = dict(src_dict)
        def _parse_operations(data: object) -> list[OperationDetailResponse] | None:
            if data is None:
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                operations_type_0 = []
                _operations_type_0 = data
                for operations_type_0_item_data in (_operations_type_0):
                    operations_type_0_item = OperationDetailResponse.from_dict(operations_type_0_item_data)



                    operations_type_0.append(operations_type_0_item)

                return operations_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[OperationDetailResponse] | None, data)

        operations = _parse_operations(d.pop("operations"))


        def _parse_total(data: object) -> int | None:
            if data is None:
                return data
            return cast(int | None, data)

        total = _parse_total(d.pop("total"))


        continuation_unavailable = d.pop("continuation_unavailable", UNSET)

        def _parse_next_cursor(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        next_cursor = _parse_next_cursor(d.pop("next_cursor", UNSET))


        def _parse_projection_issue(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        projection_issue = _parse_projection_issue(d.pop("projection_issue", UNSET))


        operations_response = cls(
            operations=operations,
            total=total,
            continuation_unavailable=continuation_unavailable,
            next_cursor=next_cursor,
            projection_issue=projection_issue,
        )

        return operations_response
