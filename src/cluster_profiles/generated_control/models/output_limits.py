from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="OutputLimits")



@_attrs_define
class OutputLimits:
    """
        Attributes:
            allowed_media_types (list[str]):
            max_file_bytes (int):
            max_files (int):
            max_total_bytes (int):
     """

    allowed_media_types: list[str]
    max_file_bytes: int
    max_files: int
    max_total_bytes: int





    def to_dict(self) -> dict[str, Any]:
        allowed_media_types = self.allowed_media_types



        max_file_bytes = self.max_file_bytes

        max_files = self.max_files

        max_total_bytes = self.max_total_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "allowed_media_types": allowed_media_types,
            "max_file_bytes": max_file_bytes,
            "max_files": max_files,
            "max_total_bytes": max_total_bytes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        allowed_media_types = cast(list[str], d.pop("allowed_media_types"))


        max_file_bytes = d.pop("max_file_bytes")

        max_files = d.pop("max_files")

        max_total_bytes = d.pop("max_total_bytes")

        output_limits = cls(
            allowed_media_types=allowed_media_types,
            max_file_bytes=max_file_bytes,
            max_files=max_files,
            max_total_bytes=max_total_bytes,
        )

        return output_limits
