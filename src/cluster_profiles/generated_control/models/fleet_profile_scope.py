from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="FleetProfileScope")



@_attrs_define
class FleetProfileScope:
    """ The complete set of Sparks reconciled by a profile.

    Scope is deliberately independent from assignments.  A member with no
    assignment is an intentional idle outcome when the profile is applied.

        Attributes:
            node_ids (list[str]):
     """

    node_ids: list[str]





    def to_dict(self) -> dict[str, Any]:
        node_ids = self.node_ids




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_ids": node_ids,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        node_ids = cast(list[str], d.pop("node_ids"))


        fleet_profile_scope = cls(
            node_ids=node_ids,
        )

        return fleet_profile_scope
