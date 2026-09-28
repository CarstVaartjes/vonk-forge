from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="RecipeDiskResources")



@_attrs_define
class RecipeDiskResources:
    """
        Attributes:
            artifact_bytes (int):
            image_bytes (int):
            safety_margin_bytes (int):
            working_bytes (int):
     """

    artifact_bytes: int
    image_bytes: int
    safety_margin_bytes: int
    working_bytes: int





    def to_dict(self) -> dict[str, Any]:
        artifact_bytes = self.artifact_bytes

        image_bytes = self.image_bytes

        safety_margin_bytes = self.safety_margin_bytes

        working_bytes = self.working_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifact_bytes": artifact_bytes,
            "image_bytes": image_bytes,
            "safety_margin_bytes": safety_margin_bytes,
            "working_bytes": working_bytes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        artifact_bytes = d.pop("artifact_bytes")

        image_bytes = d.pop("image_bytes")

        safety_margin_bytes = d.pop("safety_margin_bytes")

        working_bytes = d.pop("working_bytes")

        recipe_disk_resources = cls(
            artifact_bytes=artifact_bytes,
            image_bytes=image_bytes,
            safety_margin_bytes=safety_margin_bytes,
            working_bytes=working_bytes,
        )

        return recipe_disk_resources
