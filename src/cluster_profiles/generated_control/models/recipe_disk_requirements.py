from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="RecipeDiskRequirements")



@_attrs_define
class RecipeDiskRequirements:
    """
        Attributes:
            artifact_bytes (int):
            cache_bytes (int):
            image_bytes (int):
            rollback_bytes (int):
            safety_margin_bytes (int):
            staging_bytes (int):
     """

    artifact_bytes: int
    cache_bytes: int
    image_bytes: int
    rollback_bytes: int
    safety_margin_bytes: int
    staging_bytes: int





    def to_dict(self) -> dict[str, Any]:
        artifact_bytes = self.artifact_bytes

        cache_bytes = self.cache_bytes

        image_bytes = self.image_bytes

        rollback_bytes = self.rollback_bytes

        safety_margin_bytes = self.safety_margin_bytes

        staging_bytes = self.staging_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifact_bytes": artifact_bytes,
            "cache_bytes": cache_bytes,
            "image_bytes": image_bytes,
            "rollback_bytes": rollback_bytes,
            "safety_margin_bytes": safety_margin_bytes,
            "staging_bytes": staging_bytes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        artifact_bytes = d.pop("artifact_bytes")

        cache_bytes = d.pop("cache_bytes")

        image_bytes = d.pop("image_bytes")

        rollback_bytes = d.pop("rollback_bytes")

        safety_margin_bytes = d.pop("safety_margin_bytes")

        staging_bytes = d.pop("staging_bytes")

        recipe_disk_requirements = cls(
            artifact_bytes=artifact_bytes,
            cache_bytes=cache_bytes,
            image_bytes=image_bytes,
            rollback_bytes=rollback_bytes,
            safety_margin_bytes=safety_margin_bytes,
            staging_bytes=staging_bytes,
        )

        return recipe_disk_requirements
