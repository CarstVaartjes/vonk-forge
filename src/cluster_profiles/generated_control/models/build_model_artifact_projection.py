from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="BuildModelArtifactProjection")



@_attrs_define
class BuildModelArtifactProjection:
    """
        Attributes:
            path (str):
            sha256 (str):
            size_bytes (int):
     """

    path: str
    sha256: str
    size_bytes: int





    def to_dict(self) -> dict[str, Any]:
        path = self.path

        sha256 = self.sha256

        size_bytes = self.size_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "path": path,
            "sha256": sha256,
            "size_bytes": size_bytes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        path = d.pop("path")

        sha256 = d.pop("sha256")

        size_bytes = d.pop("size_bytes")

        build_model_artifact_projection = cls(
            path=path,
            sha256=sha256,
            size_bytes=size_bytes,
        )

        return build_model_artifact_projection
