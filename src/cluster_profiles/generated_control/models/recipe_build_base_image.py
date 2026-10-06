from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="RecipeBuildBaseImage")



@_attrs_define
class RecipeBuildBaseImage:
    """
        Attributes:
            manifest_digest (str):
            reference (str):
     """

    manifest_digest: str
    reference: str





    def to_dict(self) -> dict[str, Any]:
        manifest_digest = self.manifest_digest

        reference = self.reference


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "manifest_digest": manifest_digest,
            "reference": reference,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        manifest_digest = d.pop("manifest_digest")

        reference = d.pop("reference")

        recipe_build_base_image = cls(
            manifest_digest=manifest_digest,
            reference=reference,
        )

        return recipe_build_base_image
