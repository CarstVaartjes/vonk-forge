from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="UninstallNodeImpactResponse")



@_attrs_define
class UninstallNodeImpactResponse:
    """
        Attributes:
            node_id (str):
            rank (int):
            role (str):
            state (str):
            installed_bytes (Union[None, Unset, int]):
     """

    node_id: str
    rank: int
    role: str
    state: str
    installed_bytes: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        node_id = self.node_id

        rank = self.rank

        role = self.role

        state = self.state

        installed_bytes: Union[None, Unset, int]
        if isinstance(self.installed_bytes, Unset):
            installed_bytes = UNSET
        else:
            installed_bytes = self.installed_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_id": node_id,
            "rank": rank,
            "role": role,
            "state": state,
        })
        if installed_bytes is not UNSET:
            field_dict["installed_bytes"] = installed_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        node_id = d.pop("node_id")

        rank = d.pop("rank")

        role = d.pop("role")

        state = d.pop("state")

        def _parse_installed_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        installed_bytes = _parse_installed_bytes(d.pop("installed_bytes", UNSET))


        uninstall_node_impact_response = cls(
            node_id=node_id,
            rank=rank,
            role=role,
            state=state,
            installed_bytes=installed_bytes,
        )

        return uninstall_node_impact_response
