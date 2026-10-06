from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="ProfilePartialStop")



@_attrs_define
class ProfilePartialStop:
    """ The reachable ranks a profile Stop reaches and the ranks it cannot.

        Attributes:
            missing_node_ids (list[str]):
            target_node_ids (list[str]):
     """

    missing_node_ids: list[str]
    target_node_ids: list[str]





    def to_dict(self) -> dict[str, Any]:
        missing_node_ids = self.missing_node_ids



        target_node_ids = self.target_node_ids




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "missing_node_ids": missing_node_ids,
            "target_node_ids": target_node_ids,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        missing_node_ids = cast(list[str], d.pop("missing_node_ids"))


        target_node_ids = cast(list[str], d.pop("target_node_ids"))


        profile_partial_stop = cls(
            missing_node_ids=missing_node_ids,
            target_node_ids=target_node_ids,
        )

        return profile_partial_stop
