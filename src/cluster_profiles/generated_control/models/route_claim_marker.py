from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="RouteClaimMarker")



@_attrs_define
class RouteClaimMarker:
    """ The marker of the one route publication claim row.

    ``route_publications.activation_marker`` holds an activation marker for a
    published or maintenance generation, and this ordinal for the claim row that
    orders concurrent publication attempts.

        Attributes:
            claim_ordinal (int):
     """

    claim_ordinal: int





    def to_dict(self) -> dict[str, Any]:
        claim_ordinal = self.claim_ordinal


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "claim_ordinal": claim_ordinal,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        claim_ordinal = d.pop("claim_ordinal")

        route_claim_marker = cls(
            claim_ordinal=claim_ordinal,
        )

        return route_claim_marker
