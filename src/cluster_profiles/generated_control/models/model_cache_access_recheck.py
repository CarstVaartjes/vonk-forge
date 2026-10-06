from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="ModelCacheAccessRecheck")



@_attrs_define
class ModelCacheAccessRecheck:
    """
        Attributes:
            authorized (bool):
            checked_at (str):
            request_key (str):
     """

    authorized: bool
    checked_at: str
    request_key: str





    def to_dict(self) -> dict[str, Any]:
        authorized = self.authorized

        checked_at = self.checked_at

        request_key = self.request_key


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "authorized": authorized,
            "checked_at": checked_at,
            "request_key": request_key,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        authorized = d.pop("authorized")

        checked_at = d.pop("checked_at")

        request_key = d.pop("request_key")

        model_cache_access_recheck = cls(
            authorized=authorized,
            checked_at=checked_at,
            request_key=request_key,
        )

        return model_cache_access_recheck
