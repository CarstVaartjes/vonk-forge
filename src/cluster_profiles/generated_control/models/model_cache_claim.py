from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="ModelCacheClaim")



@_attrs_define
class ModelCacheClaim:
    """
        Attributes:
            expires_at (str):
            owner (str):
     """

    expires_at: str
    owner: str





    def to_dict(self) -> dict[str, Any]:
        expires_at = self.expires_at

        owner = self.owner


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "expires_at": expires_at,
            "owner": owner,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        expires_at = d.pop("expires_at")

        owner = d.pop("owner")

        model_cache_claim = cls(
            expires_at=expires_at,
            owner=owner,
        )

        return model_cache_claim
