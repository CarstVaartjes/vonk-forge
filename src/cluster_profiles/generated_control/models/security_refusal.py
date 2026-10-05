from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.security_refusal_reason import check_security_refusal_reason
from ..models.security_refusal_reason import SecurityRefusalReason
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="SecurityRefusal")



@_attrs_define
class SecurityRefusal:
    """ A refused request at a security boundary; it fails closed.

        Attributes:
            category (Literal['security-refusal']):
            reason (SecurityRefusalReason): Closed reason codes of a security refusal: a real security boundary.

                Authentication and authorization, identity and certificate expiry, node
                revocation, enrollment, signed package metadata, host-helper authority,
                tombstone fencing, credential denial and the digest-bound destructive-effect
                checks of Run/Switch.  ``failure_classification`` derives its code set from
                this enum, so the Controller and the contract cannot disagree.
     """

    category: Literal['security-refusal']
    reason: SecurityRefusalReason





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
        category = cast(Literal['security-refusal'] , d.pop("category"))
        if category != 'security-refusal':
            raise ValueError(f"category must match const 'security-refusal', got '{category}'")

        reason = check_security_refusal_reason(d.pop("reason"))




        security_refusal = cls(
            category=category,
            reason=reason,
        )

        return security_refusal
