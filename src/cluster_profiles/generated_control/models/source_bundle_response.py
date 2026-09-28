from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="SourceBundleResponse")



@_attrs_define
class SourceBundleResponse:
    """
        Attributes:
            archive_bytes (int):
            file_count (int):
            files (list[str]):
            sha256 (str):
            total_bytes (int):
     """

    archive_bytes: int
    file_count: int
    files: list[str]
    sha256: str
    total_bytes: int





    def to_dict(self) -> dict[str, Any]:
        archive_bytes = self.archive_bytes

        file_count = self.file_count

        files = self.files



        sha256 = self.sha256

        total_bytes = self.total_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "archive_bytes": archive_bytes,
            "file_count": file_count,
            "files": files,
            "sha256": sha256,
            "total_bytes": total_bytes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        archive_bytes = d.pop("archive_bytes")

        file_count = d.pop("file_count")

        files = cast(list[str], d.pop("files"))


        sha256 = d.pop("sha256")

        total_bytes = d.pop("total_bytes")

        source_bundle_response = cls(
            archive_bytes=archive_bytes,
            file_count=file_count,
            files=files,
            sha256=sha256,
            total_bytes=total_bytes,
        )

        return source_bundle_response
