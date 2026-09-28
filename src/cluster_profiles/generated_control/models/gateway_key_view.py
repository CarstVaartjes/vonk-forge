from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="GatewayKeyView")



@_attrs_define
class GatewayKeyView:
    """ One gateway client key without its secret. Empty `models` means all.

        Attributes:
            models (list[str]):
            name (str):
            created_at (None | str | Unset):
            expires_at (None | str | Unset):
            last_used_at (None | str | Unset):
     """

    models: list[str]
    name: str
    created_at: None | str | Unset = UNSET
    expires_at: None | str | Unset = UNSET
    last_used_at: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        models = self.models



        name = self.name

        created_at: None | str | Unset
        if isinstance(self.created_at, Unset):
            created_at = UNSET
        else:
            created_at = self.created_at

        expires_at: None | str | Unset
        if isinstance(self.expires_at, Unset):
            expires_at = UNSET
        else:
            expires_at = self.expires_at

        last_used_at: None | str | Unset
        if isinstance(self.last_used_at, Unset):
            last_used_at = UNSET
        else:
            last_used_at = self.last_used_at


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "models": models,
            "name": name,
        })
        if created_at is not UNSET:
            field_dict["created_at"] = created_at
        if expires_at is not UNSET:
            field_dict["expires_at"] = expires_at
        if last_used_at is not UNSET:
            field_dict["last_used_at"] = last_used_at

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        models = cast(list[str], d.pop("models"))


        name = d.pop("name")

        def _parse_created_at(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        created_at = _parse_created_at(d.pop("created_at", UNSET))


        def _parse_expires_at(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        expires_at = _parse_expires_at(d.pop("expires_at", UNSET))


        def _parse_last_used_at(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        last_used_at = _parse_last_used_at(d.pop("last_used_at", UNSET))


        gateway_key_view = cls(
            models=models,
            name=name,
            created_at=created_at,
            expires_at=expires_at,
            last_used_at=last_used_at,
        )

        return gateway_key_view
