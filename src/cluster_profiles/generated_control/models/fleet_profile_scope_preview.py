from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Union






T = TypeVar("T", bound="FleetProfileScopePreview")



@_attrs_define
class FleetProfileScopePreview:
    """
        Attributes:
            node_ids (list[str]):
            idle_node_ids (Union[Unset, list[str]]):
     """

    node_ids: list[str]
    idle_node_ids: Union[Unset, list[str]] = UNSET





    def to_dict(self) -> dict[str, Any]:
        node_ids = self.node_ids



        idle_node_ids: Union[Unset, list[str]] = UNSET
        if not isinstance(self.idle_node_ids, Unset):
            idle_node_ids = self.idle_node_ids




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_ids": node_ids,
        })
        if idle_node_ids is not UNSET:
            field_dict["idle_node_ids"] = idle_node_ids

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        node_ids = cast(list[str], d.pop("node_ids"))


        idle_node_ids = cast(list[str], d.pop("idle_node_ids", UNSET))


        fleet_profile_scope_preview = cls(
            node_ids=node_ids,
            idle_node_ids=idle_node_ids,
        )

        return fleet_profile_scope_preview
