from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast
from typing import Union






T = TypeVar("T", bound="FleetProfilePlanStep")



@_attrs_define
class FleetProfilePlanStep:
    """
        Attributes:
            index (int):
            kind (Literal['switch']):
            label (str):
            node_ids (Union[Unset, list[str]]):
     """

    index: int
    kind: Literal['switch']
    label: str
    node_ids: Union[Unset, list[str]] = UNSET





    def to_dict(self) -> dict[str, Any]:
        index = self.index

        kind = self.kind

        label = self.label

        node_ids: Union[Unset, list[str]] = UNSET
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

        kind = cast(Literal['switch'] , d.pop("kind"))
        if kind != 'switch':
            raise ValueError(f"kind must match const 'switch', got '{kind}'")

        label = d.pop("label")

        node_ids = cast(list[str], d.pop("node_ids", UNSET))


        fleet_profile_plan_step = cls(
            index=index,
            kind=kind,
            label=label,
            node_ids=node_ids,
        )

        return fleet_profile_plan_step
