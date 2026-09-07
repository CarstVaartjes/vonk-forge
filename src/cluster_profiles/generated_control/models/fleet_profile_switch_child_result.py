from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.run_switch_operation_result import RunSwitchOperationResult





T = TypeVar("T", bound="FleetProfileSwitchChildResult")



@_attrs_define
class FleetProfileSwitchChildResult:
    """ Profile child receipt containing the public Run/Switch result tree.

        Attributes:
            run_switch (RunSwitchOperationResult): Exact durable result tree stored in ``Job.result``.
            run_switch_operation_id (str):
     """

    run_switch: 'RunSwitchOperationResult'
    run_switch_operation_id: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_switch_operation_result import RunSwitchOperationResult
        run_switch = self.run_switch.to_dict()

        run_switch_operation_id = self.run_switch_operation_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "run_switch": run_switch,
            "run_switch_operation_id": run_switch_operation_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_switch_operation_result import RunSwitchOperationResult
        d = dict(src_dict)
        run_switch = RunSwitchOperationResult.from_dict(d.pop("run_switch"))




        run_switch_operation_id = d.pop("run_switch_operation_id")

        fleet_profile_switch_child_result = cls(
            run_switch=run_switch,
            run_switch_operation_id=run_switch_operation_id,
        )

        return fleet_profile_switch_child_result
