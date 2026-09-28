from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="ModelCacheCancellationRequest")



@_attrs_define
class ModelCacheCancellationRequest:
    """ Stable identity and operator explanation for one cancellation request.

        Attributes:
            reason (str):
            request_key (str):
     """

    reason: str
    request_key: str





    def to_dict(self) -> dict[str, Any]:
        reason = self.reason

        request_key = self.request_key


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "reason": reason,
            "request_key": request_key,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        reason = d.pop("reason")

        request_key = d.pop("request_key")

        model_cache_cancellation_request = cls(
            reason=reason,
            request_key=request_key,
        )

        return model_cache_cancellation_request
