from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="AgentUpgradeApplyResponse")



@_attrs_define
class AgentUpgradeApplyResponse:
    """
        Attributes:
            id (str):
            state (str):
     """

    id: str
    state: str





    def to_dict(self) -> dict[str, Any]:
        id = self.id

        state = self.state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "id": id,
            "state": state,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        state = d.pop("state")

        agent_upgrade_apply_response = cls(
            id=id,
            state=state,
        )

        return agent_upgrade_apply_response
