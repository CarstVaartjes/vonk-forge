from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="CompiledIdentity")



@_attrs_define
class CompiledIdentity:
    """
        Attributes:
            model_artifact_set_sha256 (str):
            recipe_revision_sha256 (str):
     """

    model_artifact_set_sha256: str
    recipe_revision_sha256: str





    def to_dict(self) -> dict[str, Any]:
        model_artifact_set_sha256 = self.model_artifact_set_sha256

        recipe_revision_sha256 = self.recipe_revision_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "model_artifact_set_sha256": model_artifact_set_sha256,
            "recipe_revision_sha256": recipe_revision_sha256,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        model_artifact_set_sha256 = d.pop("model_artifact_set_sha256")

        recipe_revision_sha256 = d.pop("recipe_revision_sha256")

        compiled_identity = cls(
            model_artifact_set_sha256=model_artifact_set_sha256,
            recipe_revision_sha256=recipe_revision_sha256,
        )

        return compiled_identity
