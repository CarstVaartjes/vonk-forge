from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="LibraryModelSizes")



@_attrs_define
class LibraryModelSizes:
    """
        Attributes:
            download_bytes (int):
            installed_bytes (int):
     """

    download_bytes: int
    installed_bytes: int





    def to_dict(self) -> dict[str, Any]:
        download_bytes = self.download_bytes

        installed_bytes = self.installed_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "download_bytes": download_bytes,
            "installed_bytes": installed_bytes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        download_bytes = d.pop("download_bytes")

        installed_bytes = d.pop("installed_bytes")

        library_model_sizes = cls(
            download_bytes=download_bytes,
            installed_bytes=installed_bytes,
        )

        return library_model_sizes
