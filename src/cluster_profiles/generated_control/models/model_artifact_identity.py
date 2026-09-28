from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="ModelArtifactIdentity")



@_attrs_define
class ModelArtifactIdentity:
    """ Exact model set, independent of transfer progress and verification time.

        Attributes:
            artifact_count (int):
            artifact_set_bytes (int):
            artifact_set_sha256 (str):
            model_content_sha256 (str):
            dependency_model_content_sha256 (list[str] | Unset):
            recipe_revision_sha256 (None | str | Unset):
     """

    artifact_count: int
    artifact_set_bytes: int
    artifact_set_sha256: str
    model_content_sha256: str
    dependency_model_content_sha256: list[str] | Unset = UNSET
    recipe_revision_sha256: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        artifact_count = self.artifact_count

        artifact_set_bytes = self.artifact_set_bytes

        artifact_set_sha256 = self.artifact_set_sha256

        model_content_sha256 = self.model_content_sha256

        dependency_model_content_sha256: list[str] | Unset = UNSET
        if not isinstance(self.dependency_model_content_sha256, Unset):
            dependency_model_content_sha256 = self.dependency_model_content_sha256



        recipe_revision_sha256: None | str | Unset
        if isinstance(self.recipe_revision_sha256, Unset):
            recipe_revision_sha256 = UNSET
        else:
            recipe_revision_sha256 = self.recipe_revision_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifact_count": artifact_count,
            "artifact_set_bytes": artifact_set_bytes,
            "artifact_set_sha256": artifact_set_sha256,
            "model_content_sha256": model_content_sha256,
        })
        if dependency_model_content_sha256 is not UNSET:
            field_dict["dependency_model_content_sha256"] = dependency_model_content_sha256
        if recipe_revision_sha256 is not UNSET:
            field_dict["recipe_revision_sha256"] = recipe_revision_sha256

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        artifact_count = d.pop("artifact_count")

        artifact_set_bytes = d.pop("artifact_set_bytes")

        artifact_set_sha256 = d.pop("artifact_set_sha256")

        model_content_sha256 = d.pop("model_content_sha256")

        dependency_model_content_sha256 = cast(list[str], d.pop("dependency_model_content_sha256", UNSET))


        def _parse_recipe_revision_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        recipe_revision_sha256 = _parse_recipe_revision_sha256(d.pop("recipe_revision_sha256", UNSET))


        model_artifact_identity = cls(
            artifact_count=artifact_count,
            artifact_set_bytes=artifact_set_bytes,
            artifact_set_sha256=artifact_set_sha256,
            model_content_sha256=model_content_sha256,
            dependency_model_content_sha256=dependency_model_content_sha256,
            recipe_revision_sha256=recipe_revision_sha256,
        )

        return model_artifact_identity
