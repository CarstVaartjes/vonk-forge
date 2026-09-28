from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="GatewayKeyRevoked")



@_attrs_define
class GatewayKeyRevoked:
    """
        Attributes:
            name (str):
            revoked (bool):
     """

    name: str
    revoked: bool





    def to_dict(self) -> dict[str, Any]:
        name = self.name

        revoked = self.revoked


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "name": name,
            "revoked": revoked,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        name = d.pop("name")

        revoked = d.pop("revoked")

        gateway_key_revoked = cls(
            name=name,
            revoked=revoked,
        )

        return gateway_key_revoked
