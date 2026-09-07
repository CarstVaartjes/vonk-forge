from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.run_switch_target_transfer_result_assignments import RunSwitchTargetTransferResultAssignments





T = TypeVar("T", bound="RunSwitchTargetTransferResult")



@_attrs_define
class RunSwitchTargetTransferResult:
    """
        Attributes:
            assignments (RunSwitchTargetTransferResultAssignments):
            phase (Literal['transfer']):
            subphase (Literal['target-copy']):
            cached_nodes (Union[Unset, list[str]]):
     """

    assignments: 'RunSwitchTargetTransferResultAssignments'
    phase: Literal['transfer']
    subphase: Literal['target-copy']
    cached_nodes: Union[Unset, list[str]] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_switch_target_transfer_result_assignments import RunSwitchTargetTransferResultAssignments
        assignments = self.assignments.to_dict()

        phase = self.phase

        subphase = self.subphase

        cached_nodes: Union[Unset, list[str]] = UNSET
        if not isinstance(self.cached_nodes, Unset):
            cached_nodes = self.cached_nodes




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "assignments": assignments,
            "phase": phase,
            "subphase": subphase,
        })
        if cached_nodes is not UNSET:
            field_dict["cached_nodes"] = cached_nodes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_switch_target_transfer_result_assignments import RunSwitchTargetTransferResultAssignments
        d = dict(src_dict)
        assignments = RunSwitchTargetTransferResultAssignments.from_dict(d.pop("assignments"))




        phase = cast(Literal['transfer'] , d.pop("phase"))
        if phase != 'transfer':
            raise ValueError(f"phase must match const 'transfer', got '{phase}'")

        subphase = cast(Literal['target-copy'] , d.pop("subphase"))
        if subphase != 'target-copy':
            raise ValueError(f"subphase must match const 'target-copy', got '{subphase}'")

        cached_nodes = cast(list[str], d.pop("cached_nodes", UNSET))


        run_switch_target_transfer_result = cls(
            assignments=assignments,
            phase=phase,
            subphase=subphase,
            cached_nodes=cached_nodes,
        )

        return run_switch_target_transfer_result
