from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.http_refusal_reason import check_http_refusal_reason
from ..models.http_refusal_reason import HttpRefusalReason
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="HttpRefusal")



@_attrs_define
class HttpRefusal:
    """
        Attributes:
            family (Literal['refusal']):
            reason (HttpRefusalReason):
     """

    family: Literal['refusal']
    reason: HttpRefusalReason





    def to_dict(self) -> dict[str, Any]:
        family = self.family

        reason: str = self.reason


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "family": family,
            "reason": reason,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        family = cast(Literal['refusal'] , d.pop("family"))
        if family != 'refusal':
            raise ValueError(f"family must match const 'refusal', got '{family}'")

        reason = check_http_refusal_reason(d.pop("reason"))




        http_refusal = cls(
            family=family,
            reason=reason,
        )

        return http_refusal
