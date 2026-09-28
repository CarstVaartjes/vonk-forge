from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.gateway_key_view import GatewayKeyView





T = TypeVar("T", bound="GatewayKeyList")



@_attrs_define
class GatewayKeyList:
    """
        Attributes:
            keys (list[GatewayKeyView]):
     """

    keys: list[GatewayKeyView]





    def to_dict(self) -> dict[str, Any]:
        from ..models.gateway_key_view import GatewayKeyView # noqa: PLC0415
        keys = []
        for keys_item_data in self.keys:
            keys_item = keys_item_data.to_dict()
            keys.append(keys_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "keys": keys,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.gateway_key_view import GatewayKeyView # noqa: PLC0415
        d = dict(src_dict)
        keys = []
        _keys = d.pop("keys")
        for keys_item_data in (_keys):
            keys_item = GatewayKeyView.from_dict(keys_item_data)



            keys.append(keys_item)


        gateway_key_list = cls(
            keys=keys,
        )

        return gateway_key_list
