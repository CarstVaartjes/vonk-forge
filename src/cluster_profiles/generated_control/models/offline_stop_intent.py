from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="OfflineStopIntent")



@_attrs_define
class OfflineStopIntent:
    """ Exact Stop orders retained for reconciliation after node contact returns.

        Attributes:
            node_ids (list[str]):
            code (Literal['node.offline'] | Unset):  Default: 'node.offline'.
     """

    node_ids: list[str]
    code: Literal['node.offline'] | Unset = 'node.offline'
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        node_ids = self.node_ids



        code = self.code


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "node_ids": node_ids,
        })
        if code is not UNSET:
            field_dict["code"] = code

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        node_ids = cast(list[str], d.pop("node_ids"))


        code = cast(Literal['node.offline'] | Unset , d.pop("code", UNSET))
        if code != 'node.offline' and not isinstance(code, Unset):
            raise ValueError(f"code must match const 'node.offline', got '{code}'")

        offline_stop_intent = cls(
            node_ids=node_ids,
            code=code,
        )


        offline_stop_intent.additional_properties = d
        return offline_stop_intent

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
