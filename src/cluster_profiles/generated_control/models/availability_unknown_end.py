from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.bookkeeping_reason import BookkeepingReason
from ..models.bookkeeping_reason import check_bookkeeping_reason
from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="AvailabilityUnknownEnd")



@_attrs_define
class AvailabilityUnknownEnd:
    """ An ended owner whose execution intent could not be re-derived.

    Contains no lease, dependency or publication authority. It is evidence of
    ending, never an alternate executable preparation payload.

        Attributes:
            residue (BookkeepingReason): Why bookkeeping was treated as unknown instead of refused.
            claim_owner (None | Unset):
            claim_until (None | Unset):
     """

    residue: BookkeepingReason
    claim_owner: None | Unset = UNSET
    claim_until: None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        residue: str = self.residue

        claim_owner = self.claim_owner

        claim_until = self.claim_until


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "residue": residue,
        })
        if claim_owner is not UNSET:
            field_dict["claim_owner"] = claim_owner
        if claim_until is not UNSET:
            field_dict["claim_until"] = claim_until

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        residue = check_bookkeeping_reason(d.pop("residue"))




        claim_owner = d.pop("claim_owner", UNSET)

        claim_until = d.pop("claim_until", UNSET)

        availability_unknown_end = cls(
            residue=residue,
            claim_owner=claim_owner,
            claim_until=claim_until,
        )

        return availability_unknown_end
