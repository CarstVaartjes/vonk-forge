from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="FleetProfileLoadReview")



@_attrs_define
class FleetProfileLoadReview:
    """
        Attributes:
            effects_digest (str):
     """

    effects_digest: str





    def to_dict(self) -> dict[str, Any]:
        effects_digest = self.effects_digest


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "effects_digest": effects_digest,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        effects_digest = d.pop("effects_digest")

        fleet_profile_load_review = cls(
            effects_digest=effects_digest,
        )

        return fleet_profile_load_review
