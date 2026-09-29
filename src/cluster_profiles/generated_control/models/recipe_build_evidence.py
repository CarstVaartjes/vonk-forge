from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="RecipeBuildEvidence")



@_attrs_define
class RecipeBuildEvidence:
    """
        Attributes:
            image_bytes (int):
            image_digest (str):
            oci_layout_sha256 (str):
     """

    image_bytes: int
    image_digest: str
    oci_layout_sha256: str





    def to_dict(self) -> dict[str, Any]:
        image_bytes = self.image_bytes

        image_digest = self.image_digest

        oci_layout_sha256 = self.oci_layout_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "image_bytes": image_bytes,
            "image_digest": image_digest,
            "oci_layout_sha256": oci_layout_sha256,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        image_bytes = d.pop("image_bytes")

        image_digest = d.pop("image_digest")

        oci_layout_sha256 = d.pop("oci_layout_sha256")

        recipe_build_evidence = cls(
            image_bytes=image_bytes,
            image_digest=image_digest,
            oci_layout_sha256=oci_layout_sha256,
        )

        return recipe_build_evidence
