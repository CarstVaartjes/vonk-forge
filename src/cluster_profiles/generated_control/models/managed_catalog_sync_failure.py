from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="ManagedCatalogSyncFailure")



@_attrs_define
class ManagedCatalogSyncFailure:
    """
        Attributes:
            code (str):
            detail (str):
            occurred_at (str):
     """

    code: str
    detail: str
    occurred_at: str





    def to_dict(self) -> dict[str, Any]:
        code = self.code

        detail = self.detail

        occurred_at = self.occurred_at


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "code": code,
            "detail": detail,
            "occurred_at": occurred_at,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        code = d.pop("code")

        detail = d.pop("detail")

        occurred_at = d.pop("occurred_at")

        managed_catalog_sync_failure = cls(
            code=code,
            detail=detail,
            occurred_at=occurred_at,
        )

        return managed_catalog_sync_failure
