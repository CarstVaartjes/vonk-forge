from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_child_progress_phase import check_fleet_profile_child_progress_phase
from ..models.fleet_profile_child_progress_phase import FleetProfileChildProgressPhase
from ..types import UNSET, Unset
from typing import cast
import datetime

if TYPE_CHECKING:
  from ..models.operation_progress import OperationProgress





T = TypeVar("T", bound="FleetProfileChildProgress")



@_attrs_define
class FleetProfileChildProgress:
    """ Typed progress emitted by the profile-owned Run switch adapter.

        Attributes:
            phase (FleetProfileChildProgressPhase):
            bytes_ (int | None | Unset):
            node_ids (list[str] | Unset):
            operation (None | OperationProgress | Unset):
            start_deadline (datetime.datetime | None | Unset):
            startup_budget_seconds (int | None | Unset):
            total_bytes (int | None | Unset):
     """

    phase: FleetProfileChildProgressPhase
    bytes_: int | None | Unset = UNSET
    node_ids: list[str] | Unset = UNSET
    operation: None | OperationProgress | Unset = UNSET
    start_deadline: datetime.datetime | None | Unset = UNSET
    startup_budget_seconds: int | None | Unset = UNSET
    total_bytes: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.operation_progress import OperationProgress # noqa: PLC0415
        phase: str = self.phase

        bytes_: int | None | Unset
        if isinstance(self.bytes_, Unset):
            bytes_ = UNSET
        else:
            bytes_ = self.bytes_

        node_ids: list[str] | Unset = UNSET
        if not isinstance(self.node_ids, Unset):
            node_ids = self.node_ids



        operation: dict[str, Any] | None | Unset
        if isinstance(self.operation, Unset):
            operation = UNSET
        elif isinstance(self.operation, OperationProgress):
            operation = self.operation.to_dict()
        else:
            operation = self.operation

        start_deadline: None | str | Unset
        if isinstance(self.start_deadline, Unset):
            start_deadline = UNSET
        elif isinstance(self.start_deadline, datetime.datetime):
            start_deadline = self.start_deadline.isoformat()
        else:
            start_deadline = self.start_deadline

        startup_budget_seconds: int | None | Unset
        if isinstance(self.startup_budget_seconds, Unset):
            startup_budget_seconds = UNSET
        else:
            startup_budget_seconds = self.startup_budget_seconds

        total_bytes: int | None | Unset
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
        from ..models.operation_progress import OperationProgress # noqa: PLC0415
        d = dict(src_dict)
        phase = check_fleet_profile_child_progress_phase(d.pop("phase"))




        def _parse_bytes_(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        bytes_ = _parse_bytes_(d.pop("bytes", UNSET))


        node_ids = cast(list[str], d.pop("node_ids", UNSET))


        def _parse_operation(data: object) -> None | OperationProgress | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                operation_type_0 = OperationProgress.from_dict(data)



                return operation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | OperationProgress | Unset, data)

        operation = _parse_operation(d.pop("operation", UNSET))


        def _parse_start_deadline(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                start_deadline_type_0 = datetime.datetime.fromisoformat(data)



                return start_deadline_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        start_deadline = _parse_start_deadline(d.pop("start_deadline", UNSET))


        def _parse_startup_budget_seconds(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        startup_budget_seconds = _parse_startup_budget_seconds(d.pop("startup_budget_seconds", UNSET))


        def _parse_total_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

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
