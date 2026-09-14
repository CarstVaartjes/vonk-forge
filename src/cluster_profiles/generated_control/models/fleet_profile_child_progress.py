from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_child_progress_phase import check_fleet_profile_child_progress_phase
from ..models.fleet_profile_child_progress_phase import FleetProfileChildProgressPhase
from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Union
import datetime

if TYPE_CHECKING:
  from ..models.operation_progress import OperationProgress





T = TypeVar("T", bound="FleetProfileChildProgress")



@_attrs_define
class FleetProfileChildProgress:
    """ Typed progress emitted by the profile-owned Run switch adapter.

        Attributes:
            phase (FleetProfileChildProgressPhase):
            bytes_ (Union[None, Unset, int]):
            node_ids (Union[Unset, list[str]]):
            operation (Union['OperationProgress', None, Unset]):
            start_deadline (Union[None, Unset, datetime.datetime]):
            startup_budget_seconds (Union[None, Unset, int]):
            total_bytes (Union[None, Unset, int]):
     """

    phase: FleetProfileChildProgressPhase
    bytes_: Union[None, Unset, int] = UNSET
    node_ids: Union[Unset, list[str]] = UNSET
    operation: Union['OperationProgress', None, Unset] = UNSET
    start_deadline: Union[None, Unset, datetime.datetime] = UNSET
    startup_budget_seconds: Union[None, Unset, int] = UNSET
    total_bytes: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.operation_progress import OperationProgress
        phase: str = self.phase

        bytes_: Union[None, Unset, int]
        if isinstance(self.bytes_, Unset):
            bytes_ = UNSET
        else:
            bytes_ = self.bytes_

        node_ids: Union[Unset, list[str]] = UNSET
        if not isinstance(self.node_ids, Unset):
            node_ids = self.node_ids



        operation: Union[None, Unset, dict[str, Any]]
        if isinstance(self.operation, Unset):
            operation = UNSET
        elif isinstance(self.operation, OperationProgress):
            operation = self.operation.to_dict()
        else:
            operation = self.operation

        start_deadline: Union[None, Unset, str]
        if isinstance(self.start_deadline, Unset):
            start_deadline = UNSET
        elif isinstance(self.start_deadline, datetime.datetime):
            start_deadline = self.start_deadline.isoformat()
        else:
            start_deadline = self.start_deadline

        startup_budget_seconds: Union[None, Unset, int]
        if isinstance(self.startup_budget_seconds, Unset):
            startup_budget_seconds = UNSET
        else:
            startup_budget_seconds = self.startup_budget_seconds

        total_bytes: Union[None, Unset, int]
        if isinstance(self.total_bytes, Unset):
            total_bytes = UNSET
        else:
            total_bytes = self.total_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "phase": phase,
        })
        if bytes_ is not UNSET:
            field_dict["bytes"] = bytes_
        if node_ids is not UNSET:
            field_dict["node_ids"] = node_ids
        if operation is not UNSET:
            field_dict["operation"] = operation
        if start_deadline is not UNSET:
            field_dict["start_deadline"] = start_deadline
        if startup_budget_seconds is not UNSET:
            field_dict["startup_budget_seconds"] = startup_budget_seconds
        if total_bytes is not UNSET:
            field_dict["total_bytes"] = total_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.operation_progress import OperationProgress
        d = dict(src_dict)
        phase = check_fleet_profile_child_progress_phase(d.pop("phase"))




        def _parse_bytes_(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        bytes_ = _parse_bytes_(d.pop("bytes", UNSET))


        node_ids = cast(list[str], d.pop("node_ids", UNSET))


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


        def _parse_start_deadline(data: object) -> Union[None, Unset, datetime.datetime]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                start_deadline_type_0 = isoparse(data)



                return start_deadline_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, Unset, datetime.datetime], data)

        start_deadline = _parse_start_deadline(d.pop("start_deadline", UNSET))


        def _parse_startup_budget_seconds(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        startup_budget_seconds = _parse_startup_budget_seconds(d.pop("startup_budget_seconds", UNSET))


        def _parse_total_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        total_bytes = _parse_total_bytes(d.pop("total_bytes", UNSET))


        fleet_profile_child_progress = cls(
            phase=phase,
            bytes_=bytes_,
            node_ids=node_ids,
            operation=operation,
            start_deadline=start_deadline,
            startup_budget_seconds=startup_budget_seconds,
            total_bytes=total_bytes,
        )

        return fleet_profile_child_progress
