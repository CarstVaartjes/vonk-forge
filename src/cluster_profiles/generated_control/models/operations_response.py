from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.operation_detail_response import OperationDetailResponse





T = TypeVar("T", bound="OperationsResponse")



@_attrs_define
class OperationsResponse:
    """
        Attributes:
            operations (list[OperationDetailResponse]):
            total (int):
            next_cursor (None | str | Unset):
            schema_version (Literal[2] | Unset):  Default: 2.
     """

    operations: list[OperationDetailResponse]
    total: int
    next_cursor: None | str | Unset = UNSET
    schema_version: Literal[2] | Unset = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.operation_detail_response import OperationDetailResponse # noqa: PLC0415
        operations = []
        for operations_item_data in self.operations:
            operations_item = operations_item_data.to_dict()
            operations.append(operations_item)



        total = self.total

        next_cursor: None | str | Unset
        if isinstance(self.next_cursor, Unset):
            next_cursor = UNSET
        else:
            next_cursor = self.next_cursor

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "operations": operations,
            "total": total,
        })
        if next_cursor is not UNSET:
            field_dict["next_cursor"] = next_cursor
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.operation_detail_response import OperationDetailResponse # noqa: PLC0415
        d = dict(src_dict)
        operations = []
        _operations = d.pop("operations")
        for operations_item_data in (_operations):
            operations_item = OperationDetailResponse.from_dict(operations_item_data)



            operations.append(operations_item)


        total = d.pop("total")

        def _parse_next_cursor(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        next_cursor = _parse_next_cursor(d.pop("next_cursor", UNSET))


        schema_version = cast(Literal[2] | Unset , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        operations_response = cls(
            operations=operations,
            total=total,
            next_cursor=next_cursor,
            schema_version=schema_version,
        )

        return operations_response
