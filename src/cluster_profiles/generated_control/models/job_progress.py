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
  from ..models.operation_progress import OperationProgress





T = TypeVar("T", bound="JobProgress")



@_attrs_define
class JobProgress:
    """
        Attributes:
            completed (int):
            failed (int):
            running (int):
            total (int):
            operation (Union['OperationProgress', None, Unset]):
     """

    completed: int
    failed: int
    running: int
    total: int
    operation: Union['OperationProgress', None, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.operation_progress import OperationProgress
        completed = self.completed

        failed = self.failed

        running = self.running

        total = self.total

        operation: Union[None, Unset, dict[str, Any]]
        if isinstance(self.operation, Unset):
            operation = UNSET
        elif isinstance(self.operation, OperationProgress):
            operation = self.operation.to_dict()
        else:
            operation = self.operation


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "completed": completed,
            "failed": failed,
            "running": running,
            "total": total,
        })
        if operation is not UNSET:
            field_dict["operation"] = operation

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.operation_progress import OperationProgress
        d = dict(src_dict)
        completed = d.pop("completed")

        failed = d.pop("failed")

        running = d.pop("running")

        total = d.pop("total")

        def _parse_operation(data: object) -> Union['OperationProgress', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                operation_type_0 = OperationProgress.from_dict(data)



                return operation_type_0
            except: # noqa: E722
                pass
            return cast(Union['OperationProgress', None, Unset], data)

        operation = _parse_operation(d.pop("operation", UNSET))


        job_progress = cls(
            completed=completed,
            failed=failed,
            running=running,
            total=total,
            operation=operation,
        )

        return job_progress
