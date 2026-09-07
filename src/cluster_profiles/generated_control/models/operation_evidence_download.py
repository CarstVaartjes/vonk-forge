from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="OperationEvidenceDownload")



@_attrs_define
class OperationEvidenceDownload:
    """
        Attributes:
            href (str):
            media_type (str):
            sha256 (str):
            size_bytes (int):
     """

    href: str
    media_type: str
    sha256: str
    size_bytes: int





    def to_dict(self) -> dict[str, Any]:
        href = self.href

        media_type = self.media_type

        sha256 = self.sha256

        size_bytes = self.size_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "href": href,
            "media_type": media_type,
            "sha256": sha256,
            "size_bytes": size_bytes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        href = d.pop("href")

        media_type = d.pop("media_type")

        sha256 = d.pop("sha256")

        size_bytes = d.pop("size_bytes")

        operation_evidence_download = cls(
            href=href,
            media_type=media_type,
            sha256=sha256,
            size_bytes=size_bytes,
        )

        return operation_evidence_download
