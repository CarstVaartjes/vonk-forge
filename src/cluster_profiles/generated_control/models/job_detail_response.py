from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.job_progress import JobProgress
  from ..models.job_operation_response import JobOperationResponse





T = TypeVar("T", bound="JobDetailResponse")



@_attrs_define
class JobDetailResponse:
    """
        Attributes:
            authority_revision (str):
            current_attempt (int):
            id (str):
            kind (str):
            operation_total (int):
            operations (list['JobOperationResponse']):
            progress (JobProgress):
            state (str):
            target_total (int):
            targets (list[str]):
            operation_next_cursor (Union[None, Unset, str]):
            reconciliation_id (Union[None, Unset, str]):
            status_reason (Union[None, Unset, str]):
            target_next_cursor (Union[None, Unset, str]):
     """

    authority_revision: str
    current_attempt: int
    id: str
    kind: str
    operation_total: int
    operations: list['JobOperationResponse']
    progress: 'JobProgress'
    state: str
    target_total: int
    targets: list[str]
    operation_next_cursor: Union[None, Unset, str] = UNSET
    reconciliation_id: Union[None, Unset, str] = UNSET
    status_reason: Union[None, Unset, str] = UNSET
    target_next_cursor: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.job_progress import JobProgress
        from ..models.job_operation_response import JobOperationResponse
        authority_revision = self.authority_revision

        current_attempt = self.current_attempt

        id = self.id

        kind = self.kind

        operation_total = self.operation_total

        operations = []
        for operations_item_data in self.operations:
            operations_item = operations_item_data.to_dict()
            operations.append(operations_item)



        progress = self.progress.to_dict()

        state = self.state

        target_total = self.target_total

        targets = self.targets



        operation_next_cursor: Union[None, Unset, str]
        if isinstance(self.operation_next_cursor, Unset):
            operation_next_cursor = UNSET
        else:
            operation_next_cursor = self.operation_next_cursor

        reconciliation_id: Union[None, Unset, str]
        if isinstance(self.reconciliation_id, Unset):
            reconciliation_id = UNSET
        else:
            reconciliation_id = self.reconciliation_id

        status_reason: Union[None, Unset, str]
        if isinstance(self.status_reason, Unset):
            status_reason = UNSET
        else:
            status_reason = self.status_reason

        target_next_cursor: Union[None, Unset, str]
        if isinstance(self.target_next_cursor, Unset):
            target_next_cursor = UNSET
        else:
            target_next_cursor = self.target_next_cursor


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "authority_revision": authority_revision,
            "current_attempt": current_attempt,
            "id": id,
            "kind": kind,
            "operation_total": operation_total,
            "operations": operations,
            "progress": progress,
            "state": state,
            "target_total": target_total,
            "targets": targets,
        })
        if operation_next_cursor is not UNSET:
            field_dict["operation_next_cursor"] = operation_next_cursor
        if reconciliation_id is not UNSET:
            field_dict["reconciliation_id"] = reconciliation_id
        if status_reason is not UNSET:
            field_dict["status_reason"] = status_reason
        if target_next_cursor is not UNSET:
            field_dict["target_next_cursor"] = target_next_cursor

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.job_progress import JobProgress
        from ..models.job_operation_response import JobOperationResponse
        d = dict(src_dict)
        authority_revision = d.pop("authority_revision")

        current_attempt = d.pop("current_attempt")

        id = d.pop("id")

        kind = d.pop("kind")

        operation_total = d.pop("operation_total")

        operations = []
        _operations = d.pop("operations")
        for operations_item_data in (_operations):
            operations_item = JobOperationResponse.from_dict(operations_item_data)



            operations.append(operations_item)


        progress = JobProgress.from_dict(d.pop("progress"))




        state = d.pop("state")

        target_total = d.pop("target_total")

        targets = cast(list[str], d.pop("targets"))


        def _parse_operation_next_cursor(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        operation_next_cursor = _parse_operation_next_cursor(d.pop("operation_next_cursor", UNSET))


        def _parse_reconciliation_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        reconciliation_id = _parse_reconciliation_id(d.pop("reconciliation_id", UNSET))


        def _parse_status_reason(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        status_reason = _parse_status_reason(d.pop("status_reason", UNSET))


        def _parse_target_next_cursor(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        target_next_cursor = _parse_target_next_cursor(d.pop("target_next_cursor", UNSET))


        job_detail_response = cls(
            authority_revision=authority_revision,
            current_attempt=current_attempt,
            id=id,
            kind=kind,
            operation_total=operation_total,
            operations=operations,
            progress=progress,
            state=state,
            target_total=target_total,
            targets=targets,
            operation_next_cursor=operation_next_cursor,
            reconciliation_id=reconciliation_id,
            status_reason=status_reason,
            target_next_cursor=target_next_cursor,
        )

        return job_detail_response
