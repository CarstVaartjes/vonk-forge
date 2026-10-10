from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.transient_reason import check_transient_reason
from ..models.transient_reason import TransientReason
from typing import cast






T = TypeVar("T", bound="HttpTransient")



@_attrs_define
class HttpTransient:
    """
        Attributes:
            reason (TransientReason):
            retry_after (int):
     """

    reason: TransientReason
    retry_after: int





    def to_dict(self) -> dict[str, Any]:
        reason: str = self.reason

        retry_after = self.retry_after


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "reason": reason,
            "retry_after": retry_after,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        reason = check_transient_reason(d.pop("reason"))




        retry_after = d.pop("retry_after")

        http_transient = cls(
            reason=reason,
            retry_after=retry_after,
        )

        return http_transient
