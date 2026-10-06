from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast
import datetime

if TYPE_CHECKING:
  from ..models.recovery_start_item import RecoveryStartItem





T = TypeVar("T", bound="DistributedRecoveryMarker")



@_attrs_define
class DistributedRecoveryMarker:
    """ What a recovery Stop remembers so the Start it interrupted can be re-issued.

        Attributes:
            deadline (datetime.datetime):
            failed_rank (int):
            schema_version (Literal[1]):
            start_phases (list[list[RecoveryStartItem]] | None | Unset):
     """

    deadline: datetime.datetime
    failed_rank: int
    schema_version: Literal[1]
    start_phases: list[list[RecoveryStartItem]] | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.recovery_start_item import RecoveryStartItem # noqa: PLC0415
        deadline = self.deadline.isoformat()

        failed_rank = self.failed_rank

        schema_version = self.schema_version

        start_phases: list[list[dict[str, Any]]] | None | Unset
        if isinstance(self.start_phases, Unset):
            start_phases = UNSET
        elif isinstance(self.start_phases, list):
            start_phases = []
            for start_phases_type_0_item_data in self.start_phases:
                start_phases_type_0_item = []
                for start_phases_type_0_item_item_data in start_phases_type_0_item_data:
                    start_phases_type_0_item_item = start_phases_type_0_item_item_data.to_dict()
                    start_phases_type_0_item.append(start_phases_type_0_item_item)


                start_phases.append(start_phases_type_0_item)


        else:
            start_phases = self.start_phases


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "deadline": deadline,
            "failed_rank": failed_rank,
            "schema_version": schema_version,
        })
        if start_phases is not UNSET:
            field_dict["start_phases"] = start_phases

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recovery_start_item import RecoveryStartItem # noqa: PLC0415
        d = dict(src_dict)
        deadline = datetime.datetime.fromisoformat(d.pop("deadline"))




        failed_rank = d.pop("failed_rank")

        schema_version = cast(Literal[1] , d.pop("schema_version"))
        if schema_version != 1:
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        def _parse_start_phases(data: object) -> list[list[RecoveryStartItem]] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                start_phases_type_0 = []
                _start_phases_type_0 = data
                for start_phases_type_0_item_data in (_start_phases_type_0):
                    start_phases_type_0_item = []
                    _start_phases_type_0_item = start_phases_type_0_item_data
                    for start_phases_type_0_item_item_data in (_start_phases_type_0_item):
                        start_phases_type_0_item_item = RecoveryStartItem.from_dict(start_phases_type_0_item_item_data)



                        start_phases_type_0_item.append(start_phases_type_0_item_item)

                    start_phases_type_0.append(start_phases_type_0_item)

                return start_phases_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[list[RecoveryStartItem]] | None | Unset, data)

        start_phases = _parse_start_phases(d.pop("start_phases", UNSET))


        distributed_recovery_marker = cls(
            deadline=deadline,
            failed_rank=failed_rank,
            schema_version=schema_version,
            start_phases=start_phases,
        )

        return distributed_recovery_marker
