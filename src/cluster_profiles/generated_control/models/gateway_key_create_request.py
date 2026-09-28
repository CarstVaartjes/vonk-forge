from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="GatewayKeyCreateRequest")



@_attrs_define
class GatewayKeyCreateRequest:
    """
        Attributes:
            name (str):
            expires (None | str | Unset):
            models (list[str] | Unset):
     """

    name: str
    expires: None | str | Unset = UNSET
    models: list[str] | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        name = self.name

        expires: None | str | Unset
        if isinstance(self.expires, Unset):
            expires = UNSET
        else:
            expires = self.expires

        models: list[str] | Unset = UNSET
        if not isinstance(self.models, Unset):
            models = self.models




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "name": name,
        })
        if expires is not UNSET:
            field_dict["expires"] = expires
        if models is not UNSET:
            field_dict["models"] = models

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        name = d.pop("name")

        def _parse_expires(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        expires = _parse_expires(d.pop("expires", UNSET))


        models = cast(list[str], d.pop("models", UNSET))


        gateway_key_create_request = cls(
            name=name,
            expires=expires,
            models=models,
        )

        return gateway_key_create_request
