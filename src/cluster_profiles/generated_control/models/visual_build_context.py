from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="VisualBuildContext")



@_attrs_define
class VisualBuildContext:
    """
        Attributes:
            expected_bytes (int):
            media_type (str):
            sha256 (str):
     """

    expected_bytes: int
    media_type: str
    sha256: str





    def to_dict(self) -> dict[str, Any]:
        expected_bytes = self.expected_bytes

        media_type = self.media_type

        sha256 = self.sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "expected_bytes": expected_bytes,
            "media_type": media_type,
            "sha256": sha256,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        expected_bytes = d.pop("expected_bytes")

        media_type = d.pop("media_type")

        sha256 = d.pop("sha256")

        visual_build_context = cls(
            expected_bytes=expected_bytes,
            media_type=media_type,
            sha256=sha256,
        )

        return visual_build_context
