from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.cache_manifest_artifact import CacheManifestArtifact
  from ..models.model_reference import ModelReference





T = TypeVar("T", bound="CacheManifest")



@_attrs_define
class CacheManifest:
    """
        Attributes:
            artifacts (list[CacheManifestArtifact]):
            model_content_digests (list[str]):
            model_content_sha256 (None | str):
            model_definition_ref (ModelReference | None):
            recipe_revision_sha256 (None | str):
            schema_version (Literal[2]):
            source_policy (Literal['nas-first']):
     """

    artifacts: list[CacheManifestArtifact]
    model_content_digests: list[str]
    model_content_sha256: None | str
    model_definition_ref: ModelReference | None
    recipe_revision_sha256: None | str
    schema_version: Literal[2]
    source_policy: Literal['nas-first']





    def to_dict(self) -> dict[str, Any]:
        from ..models.cache_manifest_artifact import CacheManifestArtifact # noqa: PLC0415
        from ..models.model_reference import ModelReference # noqa: PLC0415
        artifacts = []
        for artifacts_item_data in self.artifacts:
            artifacts_item = artifacts_item_data.to_dict()
            artifacts.append(artifacts_item)



        model_content_digests = self.model_content_digests



        model_content_sha256: None | str
        model_content_sha256 = self.model_content_sha256

        model_definition_ref: dict[str, Any] | None
        if isinstance(self.model_definition_ref, ModelReference):
            model_definition_ref = self.model_definition_ref.to_dict()
        else:
            model_definition_ref = self.model_definition_ref

        recipe_revision_sha256: None | str
        recipe_revision_sha256 = self.recipe_revision_sha256

        schema_version = self.schema_version

        source_policy = self.source_policy


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifacts": artifacts,
            "model_content_digests": model_content_digests,
            "model_content_sha256": model_content_sha256,
            "model_definition_ref": model_definition_ref,
            "recipe_revision_sha256": recipe_revision_sha256,
            "schema_version": schema_version,
            "source_policy": source_policy,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.cache_manifest_artifact import CacheManifestArtifact # noqa: PLC0415
        from ..models.model_reference import ModelReference # noqa: PLC0415
        d = dict(src_dict)
        artifacts = []
        _artifacts = d.pop("artifacts")
        for artifacts_item_data in (_artifacts):
            artifacts_item = CacheManifestArtifact.from_dict(artifacts_item_data)



            artifacts.append(artifacts_item)


        model_content_digests = cast(list[str], d.pop("model_content_digests"))


        def _parse_model_content_sha256(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        model_content_sha256 = _parse_model_content_sha256(d.pop("model_content_sha256"))


        def _parse_model_definition_ref(data: object) -> ModelReference | None:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                model_definition_ref_type_0 = ModelReference.from_dict(data)



                return model_definition_ref_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ModelReference | None, data)

        model_definition_ref = _parse_model_definition_ref(d.pop("model_definition_ref"))


        def _parse_recipe_revision_sha256(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        recipe_revision_sha256 = _parse_recipe_revision_sha256(d.pop("recipe_revision_sha256"))


        schema_version = cast(Literal[2] , d.pop("schema_version"))
        if schema_version != 2:
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        source_policy = cast(Literal['nas-first'] , d.pop("source_policy"))
        if source_policy != 'nas-first':
            raise ValueError(f"source_policy must match const 'nas-first', got '{source_policy}'")

        cache_manifest = cls(
            artifacts=artifacts,
            model_content_digests=model_content_digests,
            model_content_sha256=model_content_sha256,
            model_definition_ref=model_definition_ref,
            recipe_revision_sha256=recipe_revision_sha256,
            schema_version=schema_version,
            source_policy=source_policy,
        )

        return cache_manifest
