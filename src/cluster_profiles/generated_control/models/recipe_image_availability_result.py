from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="RecipeImageAvailabilityResult")



@_attrs_define
class RecipeImageAvailabilityResult:
    """
        Attributes:
            image_bytes (int):
            image_digest (str):
            model_content_digests (list[str]):
            oci_archive_sha256 (str):
            platform_manifest_digest (str):
            recipe_content_sha256 (str):
            source (str):
            artifact_set_sha256 (None | str | Unset):
            build_id (None | str | Unset):
            build_input_sha256 (None | str | Unset):
            local_image_config_id (None | str | Unset):
            model_child_id (None | str | Unset):
            model_digest (None | str | Unset):
            registry_manifest_digest (None | str | Unset):
            schema_version (Literal[2] | Unset):  Default: 2.
     """

    image_bytes: int
    image_digest: str
    model_content_digests: list[str]
    oci_archive_sha256: str
    platform_manifest_digest: str
    recipe_content_sha256: str
    source: str
    artifact_set_sha256: None | str | Unset = UNSET
    build_id: None | str | Unset = UNSET
    build_input_sha256: None | str | Unset = UNSET
    local_image_config_id: None | str | Unset = UNSET
    model_child_id: None | str | Unset = UNSET
    model_digest: None | str | Unset = UNSET
    registry_manifest_digest: None | str | Unset = UNSET
    schema_version: Literal[2] | Unset = 2





    def to_dict(self) -> dict[str, Any]:
        image_bytes = self.image_bytes

        image_digest = self.image_digest

        model_content_digests = self.model_content_digests



        oci_archive_sha256 = self.oci_archive_sha256

        platform_manifest_digest = self.platform_manifest_digest

        recipe_content_sha256 = self.recipe_content_sha256

        source = self.source

        artifact_set_sha256: None | str | Unset
        if isinstance(self.artifact_set_sha256, Unset):
            artifact_set_sha256 = UNSET
        else:
            artifact_set_sha256 = self.artifact_set_sha256

        build_id: None | str | Unset
        if isinstance(self.build_id, Unset):
            build_id = UNSET
        else:
            build_id = self.build_id

        build_input_sha256: None | str | Unset
        if isinstance(self.build_input_sha256, Unset):
            build_input_sha256 = UNSET
        else:
            build_input_sha256 = self.build_input_sha256

        local_image_config_id: None | str | Unset
        if isinstance(self.local_image_config_id, Unset):
            local_image_config_id = UNSET
        else:
            local_image_config_id = self.local_image_config_id

        model_child_id: None | str | Unset
        if isinstance(self.model_child_id, Unset):
            model_child_id = UNSET
        else:
            model_child_id = self.model_child_id

        model_digest: None | str | Unset
        if isinstance(self.model_digest, Unset):
            model_digest = UNSET
        else:
            model_digest = self.model_digest

        registry_manifest_digest: None | str | Unset
        if isinstance(self.registry_manifest_digest, Unset):
            registry_manifest_digest = UNSET
        else:
            registry_manifest_digest = self.registry_manifest_digest

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "image_bytes": image_bytes,
            "image_digest": image_digest,
            "model_content_digests": model_content_digests,
            "oci_archive_sha256": oci_archive_sha256,
            "platform_manifest_digest": platform_manifest_digest,
            "recipe_content_sha256": recipe_content_sha256,
            "source": source,
        })
        if artifact_set_sha256 is not UNSET:
            field_dict["artifact_set_sha256"] = artifact_set_sha256
        if build_id is not UNSET:
            field_dict["build_id"] = build_id
        if build_input_sha256 is not UNSET:
            field_dict["build_input_sha256"] = build_input_sha256
        if local_image_config_id is not UNSET:
            field_dict["local_image_config_id"] = local_image_config_id
        if model_child_id is not UNSET:
            field_dict["model_child_id"] = model_child_id
        if model_digest is not UNSET:
            field_dict["model_digest"] = model_digest
        if registry_manifest_digest is not UNSET:
            field_dict["registry_manifest_digest"] = registry_manifest_digest
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        image_bytes = d.pop("image_bytes")

        image_digest = d.pop("image_digest")

        model_content_digests = cast(list[str], d.pop("model_content_digests"))


        oci_archive_sha256 = d.pop("oci_archive_sha256")

        platform_manifest_digest = d.pop("platform_manifest_digest")

        recipe_content_sha256 = d.pop("recipe_content_sha256")

        source = d.pop("source")

        def _parse_artifact_set_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        artifact_set_sha256 = _parse_artifact_set_sha256(d.pop("artifact_set_sha256", UNSET))


        def _parse_build_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        build_id = _parse_build_id(d.pop("build_id", UNSET))


        def _parse_build_input_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        build_input_sha256 = _parse_build_input_sha256(d.pop("build_input_sha256", UNSET))


        def _parse_local_image_config_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        local_image_config_id = _parse_local_image_config_id(d.pop("local_image_config_id", UNSET))


        def _parse_model_child_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        model_child_id = _parse_model_child_id(d.pop("model_child_id", UNSET))


        def _parse_model_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        model_digest = _parse_model_digest(d.pop("model_digest", UNSET))


        def _parse_registry_manifest_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        registry_manifest_digest = _parse_registry_manifest_digest(d.pop("registry_manifest_digest", UNSET))


        schema_version = cast(Literal[2] | Unset , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        recipe_image_availability_result = cls(
            image_bytes=image_bytes,
            image_digest=image_digest,
            model_content_digests=model_content_digests,
            oci_archive_sha256=oci_archive_sha256,
            platform_manifest_digest=platform_manifest_digest,
            recipe_content_sha256=recipe_content_sha256,
            source=source,
            artifact_set_sha256=artifact_set_sha256,
            build_id=build_id,
            build_input_sha256=build_input_sha256,
            local_image_config_id=local_image_config_id,
            model_child_id=model_child_id,
            model_digest=model_digest,
            registry_manifest_digest=registry_manifest_digest,
            schema_version=schema_version,
        )

        return recipe_image_availability_result
