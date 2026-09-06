from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union






T = TypeVar("T", bound="RecipeImageAvailabilityResult")



@_attrs_define
class RecipeImageAvailabilityResult:
    """
        Attributes:
            image_bytes (int):
            image_digest (str):
            oci_archive_sha256 (str):
            platform_manifest_digest (str):
            recipe_content_sha256 (str):
            source (str):
            artifact_set_sha256 (Union[None, Unset, str]):
            build_id (Union[None, Unset, str]):
            build_input_sha256 (Union[None, Unset, str]):
            local_image_config_id (Union[None, Unset, str]):
            model_child_id (Union[None, Unset, str]):
            model_digest (Union[None, Unset, str]):
            model_versions (Union[Unset, list[str]]):
            registry_manifest_digest (Union[None, Unset, str]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    image_bytes: int
    image_digest: str
    oci_archive_sha256: str
    platform_manifest_digest: str
    recipe_content_sha256: str
    source: str
    artifact_set_sha256: Union[None, Unset, str] = UNSET
    build_id: Union[None, Unset, str] = UNSET
    build_input_sha256: Union[None, Unset, str] = UNSET
    local_image_config_id: Union[None, Unset, str] = UNSET
    model_child_id: Union[None, Unset, str] = UNSET
    model_digest: Union[None, Unset, str] = UNSET
    model_versions: Union[Unset, list[str]] = UNSET
    registry_manifest_digest: Union[None, Unset, str] = UNSET
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        image_bytes = self.image_bytes

        image_digest = self.image_digest

        oci_archive_sha256 = self.oci_archive_sha256

        platform_manifest_digest = self.platform_manifest_digest

        recipe_content_sha256 = self.recipe_content_sha256

        source = self.source

        artifact_set_sha256: Union[None, Unset, str]
        if isinstance(self.artifact_set_sha256, Unset):
            artifact_set_sha256 = UNSET
        else:
            artifact_set_sha256 = self.artifact_set_sha256

        build_id: Union[None, Unset, str]
        if isinstance(self.build_id, Unset):
            build_id = UNSET
        else:
            build_id = self.build_id

        build_input_sha256: Union[None, Unset, str]
        if isinstance(self.build_input_sha256, Unset):
            build_input_sha256 = UNSET
        else:
            build_input_sha256 = self.build_input_sha256

        local_image_config_id: Union[None, Unset, str]
        if isinstance(self.local_image_config_id, Unset):
            local_image_config_id = UNSET
        else:
            local_image_config_id = self.local_image_config_id

        model_child_id: Union[None, Unset, str]
        if isinstance(self.model_child_id, Unset):
            model_child_id = UNSET
        else:
            model_child_id = self.model_child_id

        model_digest: Union[None, Unset, str]
        if isinstance(self.model_digest, Unset):
            model_digest = UNSET
        else:
            model_digest = self.model_digest

        model_versions: Union[Unset, list[str]] = UNSET
        if not isinstance(self.model_versions, Unset):
            model_versions = self.model_versions



        registry_manifest_digest: Union[None, Unset, str]
        if isinstance(self.registry_manifest_digest, Unset):
            registry_manifest_digest = UNSET
        else:
            registry_manifest_digest = self.registry_manifest_digest

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "image_bytes": image_bytes,
            "image_digest": image_digest,
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
        if model_versions is not UNSET:
            field_dict["model_versions"] = model_versions
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

        oci_archive_sha256 = d.pop("oci_archive_sha256")

        platform_manifest_digest = d.pop("platform_manifest_digest")

        recipe_content_sha256 = d.pop("recipe_content_sha256")

        source = d.pop("source")

        def _parse_artifact_set_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        artifact_set_sha256 = _parse_artifact_set_sha256(d.pop("artifact_set_sha256", UNSET))


        def _parse_build_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        build_id = _parse_build_id(d.pop("build_id", UNSET))


        def _parse_build_input_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        build_input_sha256 = _parse_build_input_sha256(d.pop("build_input_sha256", UNSET))


        def _parse_local_image_config_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        local_image_config_id = _parse_local_image_config_id(d.pop("local_image_config_id", UNSET))


        def _parse_model_child_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        model_child_id = _parse_model_child_id(d.pop("model_child_id", UNSET))


        def _parse_model_digest(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        model_digest = _parse_model_digest(d.pop("model_digest", UNSET))


        model_versions = cast(list[str], d.pop("model_versions", UNSET))


        def _parse_registry_manifest_digest(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        registry_manifest_digest = _parse_registry_manifest_digest(d.pop("registry_manifest_digest", UNSET))


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        recipe_image_availability_result = cls(
            image_bytes=image_bytes,
            image_digest=image_digest,
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
            model_versions=model_versions,
            registry_manifest_digest=registry_manifest_digest,
            schema_version=schema_version,
        )

        return recipe_image_availability_result
