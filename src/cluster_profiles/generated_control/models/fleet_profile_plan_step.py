from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_plan_step_kind import check_fleet_profile_plan_step_kind
from ..models.fleet_profile_plan_step_kind import FleetProfilePlanStepKind
from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="FleetProfilePlanStep")



@_attrs_define
class FleetProfilePlanStep:
    """
        Attributes:
            index (int):
            kind (FleetProfilePlanStepKind):
            label (str):
            node_ids (list[str] | Unset):
     """

    index: int
    kind: FleetProfilePlanStepKind
    label: str
    node_ids: list[str] | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        index = self.index

        kind: str = self.kind

        label = self.label

        node_ids: list[str] | Unset = UNSET
        if not isinstance(self.node_ids, Unset):
            node_ids = self.node_ids




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "index": index,
            "kind": kind,
            "label": label,
        })
        if node_ids is not UNSET:
            field_dict["node_ids"] = node_ids

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        index = d.pop("index")

        kind = check_fleet_profile_plan_step_kind(d.pop("kind"))




        label = d.pop("label")

        node_ids = cast(list[str], d.pop("node_ids", UNSET))


        fleet_profile_plan_step = cls(
            index=index,
            kind=kind,
            label=label,
            node_ids=node_ids,
        )

        return fleet_profile_plan_step
