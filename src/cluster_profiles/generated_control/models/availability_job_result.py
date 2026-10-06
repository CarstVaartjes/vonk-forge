from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.availability_model_child import AvailabilityModelChild





T = TypeVar("T", bound="AvailabilityJobResult")



@_attrs_define
class AvailabilityJobResult:
    """ The image an availability operation produced, with its model child.

        Attributes:
            image_bytes (int):
            image_digest (str):
            oci_archive_sha256 (str):
            recipe_content_sha256 (str):
            schema_version (Literal[2]):
            build_id (None | str | Unset):
            build_input_sha256 (None | str | Unset):
            local_image_config_id (None | str | Unset):
            model_child (AvailabilityModelChild | None | Unset):
            model_digest (None | str | Unset):
     """

    image_bytes: int
    image_digest: str
    oci_archive_sha256: str
    recipe_content_sha256: str
    schema_version: Literal[2]
    build_id: None | str | Unset = UNSET
    build_input_sha256: None | str | Unset = UNSET
    local_image_config_id: None | str | Unset = UNSET
    model_child: AvailabilityModelChild | None | Unset = UNSET
    model_digest: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.availability_model_child import AvailabilityModelChild # noqa: PLC0415
        image_bytes = self.image_bytes

        image_digest = self.image_digest

        oci_archive_sha256 = self.oci_archive_sha256

        recipe_content_sha256 = self.recipe_content_sha256

        schema_version = self.schema_version

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

        model_child: dict[str, Any] | None | Unset
        if isinstance(self.model_child, Unset):
            model_child = UNSET
        elif isinstance(self.model_child, AvailabilityModelChild):
            model_child = self.model_child.to_dict()
        else:
            model_child = self.model_child

        model_digest: None | str | Unset
        if isinstance(self.model_digest, Unset):
            model_digest = UNSET
        else:
            model_digest = self.model_digest


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "image_bytes": image_bytes,
            "image_digest": image_digest,
            "oci_archive_sha256": oci_archive_sha256,
            "recipe_content_sha256": recipe_content_sha256,
            "schema_version": schema_version,
        })
        if build_id is not UNSET:
            field_dict["build_id"] = build_id
        if build_input_sha256 is not UNSET:
            field_dict["build_input_sha256"] = build_input_sha256
        if local_image_config_id is not UNSET:
            field_dict["local_image_config_id"] = local_image_config_id
        if model_child is not UNSET:
            field_dict["model_child"] = model_child
        if model_digest is not UNSET:
            field_dict["model_digest"] = model_digest

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.availability_model_child import AvailabilityModelChild # noqa: PLC0415
        d = dict(src_dict)
        image_bytes = d.pop("image_bytes")

        image_digest = d.pop("image_digest")

        oci_archive_sha256 = d.pop("oci_archive_sha256")

        recipe_content_sha256 = d.pop("recipe_content_sha256")

        schema_version = cast(Literal[2] , d.pop("schema_version"))
        if schema_version != 2:
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

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


        def _parse_model_child(data: object) -> AvailabilityModelChild | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                model_child_type_0 = AvailabilityModelChild.from_dict(data)



                return model_child_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AvailabilityModelChild | None | Unset, data)

        model_child = _parse_model_child(d.pop("model_child", UNSET))


        def _parse_model_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        model_digest = _parse_model_digest(d.pop("model_digest", UNSET))


        availability_job_result = cls(
            image_bytes=image_bytes,
            image_digest=image_digest,
            oci_archive_sha256=oci_archive_sha256,
            recipe_content_sha256=recipe_content_sha256,
            schema_version=schema_version,
            build_id=build_id,
            build_input_sha256=build_input_sha256,
            local_image_config_id=local_image_config_id,
            model_child=model_child,
            model_digest=model_digest,
        )

        return availability_job_result
