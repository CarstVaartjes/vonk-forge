from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.source_bundle_file import SourceBundleFile





T = TypeVar("T", bound="SourceBundleManifest")



@_attrs_define
class SourceBundleManifest:
    """
        Attributes:
            files (list[SourceBundleFile]):
            schema_version (Literal[1]):
            sha256 (str):
            total_bytes (int):
     """

    files: list[SourceBundleFile]
    schema_version: Literal[1]
    sha256: str
    total_bytes: int





    def to_dict(self) -> dict[str, Any]:
        from ..models.source_bundle_file import SourceBundleFile # noqa: PLC0415
        files = []
        for files_item_data in self.files:
            files_item = files_item_data.to_dict()
            files.append(files_item)



        schema_version = self.schema_version

        sha256 = self.sha256

        total_bytes = self.total_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "files": files,
            "schema_version": schema_version,
            "sha256": sha256,
            "total_bytes": total_bytes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.source_bundle_file import SourceBundleFile # noqa: PLC0415
        d = dict(src_dict)
        files = []
        _files = d.pop("files")
        for files_item_data in (_files):
            files_item = SourceBundleFile.from_dict(files_item_data)



            files.append(files_item)


        schema_version = cast(Literal[1] , d.pop("schema_version"))
        if schema_version != 1:
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        sha256 = d.pop("sha256")

        total_bytes = d.pop("total_bytes")

        source_bundle_manifest = cls(
            files=files,
            schema_version=schema_version,
            sha256=sha256,
            total_bytes=total_bytes,
        )

        return source_bundle_manifest
