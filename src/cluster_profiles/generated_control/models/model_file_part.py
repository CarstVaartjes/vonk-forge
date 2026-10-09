from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="ModelFilePart")



@_attrs_define
class ModelFilePart:
    """ One published piece of a file the source can only host split.

    A part is a transport detail: it exists only at the source (for example a
    Hugging Face repository that caps files at 50 GB publishes
    ``model.safetensors.part00``). It is never installed.

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

        model_file_part = cls(
            path=path,
            sha256=sha256,
            size_bytes=size_bytes,
        )

        return model_file_part
