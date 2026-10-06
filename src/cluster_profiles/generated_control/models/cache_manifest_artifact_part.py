from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="CacheManifestArtifactPart")



@_attrs_define
class CacheManifestArtifactPart:
    """ One source-published piece of a split artifact (joined in list order).

        Attributes:
            download_bytes (int):
            path (str):
            sha256 (str):
            source (str):
     """

    download_bytes: int
    path: str
    sha256: str
    source: str





    def to_dict(self) -> dict[str, Any]:
        download_bytes = self.download_bytes

        path = self.path

        sha256 = self.sha256

        source = self.source


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "download_bytes": download_bytes,
            "path": path,
            "sha256": sha256,
            "source": source,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        download_bytes = d.pop("download_bytes")

        path = d.pop("path")

        sha256 = d.pop("sha256")

        source = d.pop("source")

        cache_manifest_artifact_part = cls(
            download_bytes=download_bytes,
            path=path,
            sha256=sha256,
            source=source,
        )

        return cache_manifest_artifact_part
