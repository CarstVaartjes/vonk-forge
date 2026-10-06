from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.source_bundle_file_mode import check_source_bundle_file_mode
from ..models.source_bundle_file_mode import SourceBundleFileMode
from typing import cast






T = TypeVar("T", bound="SourceBundleFile")



@_attrs_define
class SourceBundleFile:
    """
        Attributes:
            mode (SourceBundleFileMode):
            path (str):
            sha256 (str):
            size (int):
     """

    mode: SourceBundleFileMode
    path: str
    sha256: str
    size: int





    def to_dict(self) -> dict[str, Any]:
        mode: int = self.mode

        path = self.path

        sha256 = self.sha256

        size = self.size


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "mode": mode,
            "path": path,
            "sha256": sha256,
            "size": size,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        mode = check_source_bundle_file_mode(d.pop("mode"))




        path = d.pop("path")

        sha256 = d.pop("sha256")

        size = d.pop("size")

        source_bundle_file = cls(
            mode=mode,
            path=path,
            sha256=sha256,
            size=size,
        )

        return source_bundle_file
