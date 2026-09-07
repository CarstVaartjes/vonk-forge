from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="ArtifactOutputFile")



@_attrs_define
class ArtifactOutputFile:
    """
        Attributes:
            media_type (str):
            name (str):
            sha256 (str):
            size_bytes (int):
     """

    media_type: str
    name: str
    sha256: str
    size_bytes: int





    def to_dict(self) -> dict[str, Any]:
        media_type = self.media_type

        name = self.name

        sha256 = self.sha256

        size_bytes = self.size_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "media_type": media_type,
            "name": name,
            "sha256": sha256,
            "size_bytes": size_bytes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        media_type = d.pop("media_type")

        name = d.pop("name")

        sha256 = d.pop("sha256")

        size_bytes = d.pop("size_bytes")

        artifact_output_file = cls(
            media_type=media_type,
            name=name,
            sha256=sha256,
            size_bytes=size_bytes,
        )

        return artifact_output_file
