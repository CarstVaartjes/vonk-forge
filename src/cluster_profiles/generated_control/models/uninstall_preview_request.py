from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="UninstallPreviewRequest")



@_attrs_define
class UninstallPreviewRequest:
    """
        Attributes:
            installation_id (str):
     """

    installation_id: str





    def to_dict(self) -> dict[str, Any]:
        installation_id = self.installation_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "installation_id": installation_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        installation_id = d.pop("installation_id")

        uninstall_preview_request = cls(
            installation_id=installation_id,
        )

        return uninstall_preview_request
