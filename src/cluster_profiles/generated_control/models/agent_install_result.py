from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="AgentInstallResult")



@_attrs_define
class AgentInstallResult:
    """
        Attributes:
            installed_bytes (int):
     """

    installed_bytes: int





    def to_dict(self) -> dict[str, Any]:
        installed_bytes = self.installed_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "installed_bytes": installed_bytes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        installed_bytes = d.pop("installed_bytes")

        agent_install_result = cls(
            installed_bytes=installed_bytes,
        )

        return agent_install_result
