from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.wait_reason import check_wait_reason
from ..models.wait_reason import WaitReason
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="UnknownError")



@_attrs_define
class UnknownError:
    """ Anything else: observed and reconciled, never parked.

        Attributes:
            category (Literal['unknown']):
            reason (WaitReason): Typed reason codes for an effect that cannot be confirmed (the *unknown* kind).

                Each code names the one fact the executor could not establish.  The free
                text of a report is for people; the Controller decides on this code.
     """

    category: Literal['unknown']
    reason: WaitReason





    def to_dict(self) -> dict[str, Any]:
        category = self.category

        reason: str = self.reason


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "category": category,
            "reason": reason,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        category = cast(Literal['unknown'] , d.pop("category"))
        if category != 'unknown':
            raise ValueError(f"category must match const 'unknown', got '{category}'")

        reason = check_wait_reason(d.pop("reason"))




        unknown_error = cls(
            category=category,
            reason=reason,
        )

        return unknown_error
