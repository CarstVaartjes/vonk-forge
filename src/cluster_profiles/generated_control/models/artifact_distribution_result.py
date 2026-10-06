from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="ArtifactDistributionResult")



@_attrs_define
class ArtifactDistributionResult:
    """
        Attributes:
            downloaded_bytes (int):
     """

    downloaded_bytes: int





    def to_dict(self) -> dict[str, Any]:
        downloaded_bytes = self.downloaded_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "downloaded_bytes": downloaded_bytes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        downloaded_bytes = d.pop("downloaded_bytes")

        artifact_distribution_result = cls(
            downloaded_bytes=downloaded_bytes,
        )

        return artifact_distribution_result
