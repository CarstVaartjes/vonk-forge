from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union






T = TypeVar("T", bound="ModelCacheUpdateResponse")



@_attrs_define
class ModelCacheUpdateResponse:
    """
        Attributes:
            artifact_set_sha256 (str):
            latest_model_version_sha256 (Union[None, str]):
            latest_recipe_revision_sha256 (Union[None, str]):
            model_update_available (bool):
            model_version_sha256 (Union[None, str]):
            recipe_revision_sha256 (Union[None, str]):
            recipe_update_available (bool):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
            updated_at (Union[None, Unset, str]):
     """

    artifact_set_sha256: str
    latest_model_version_sha256: Union[None, str]
    latest_recipe_revision_sha256: Union[None, str]
    model_update_available: bool
    model_version_sha256: Union[None, str]
    recipe_revision_sha256: Union[None, str]
    recipe_update_available: bool
    schema_version: Union[Literal[2], Unset] = 2
    updated_at: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        artifact_set_sha256 = self.artifact_set_sha256

        latest_model_version_sha256: Union[None, str]
        latest_model_version_sha256 = self.latest_model_version_sha256

        latest_recipe_revision_sha256: Union[None, str]
        latest_recipe_revision_sha256 = self.latest_recipe_revision_sha256

        model_update_available = self.model_update_available

        model_version_sha256: Union[None, str]
        model_version_sha256 = self.model_version_sha256

        recipe_revision_sha256: Union[None, str]
        recipe_revision_sha256 = self.recipe_revision_sha256

        recipe_update_available = self.recipe_update_available

        schema_version = self.schema_version

        updated_at: Union[None, Unset, str]
        if isinstance(self.updated_at, Unset):
            updated_at = UNSET
        else:
            updated_at = self.updated_at


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifact_set_sha256": artifact_set_sha256,
            "latest_model_version_sha256": latest_model_version_sha256,
            "latest_recipe_revision_sha256": latest_recipe_revision_sha256,
            "model_update_available": model_update_available,
            "model_version_sha256": model_version_sha256,
            "recipe_revision_sha256": recipe_revision_sha256,
            "recipe_update_available": recipe_update_available,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if updated_at is not UNSET:
            field_dict["updated_at"] = updated_at

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        artifact_set_sha256 = d.pop("artifact_set_sha256")

        def _parse_latest_model_version_sha256(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        latest_model_version_sha256 = _parse_latest_model_version_sha256(d.pop("latest_model_version_sha256"))


        def _parse_latest_recipe_revision_sha256(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        latest_recipe_revision_sha256 = _parse_latest_recipe_revision_sha256(d.pop("latest_recipe_revision_sha256"))


        model_update_available = d.pop("model_update_available")

        def _parse_model_version_sha256(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        model_version_sha256 = _parse_model_version_sha256(d.pop("model_version_sha256"))


        def _parse_recipe_revision_sha256(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        recipe_revision_sha256 = _parse_recipe_revision_sha256(d.pop("recipe_revision_sha256"))


        recipe_update_available = d.pop("recipe_update_available")

        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        def _parse_updated_at(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        updated_at = _parse_updated_at(d.pop("updated_at", UNSET))


        model_cache_update_response = cls(
            artifact_set_sha256=artifact_set_sha256,
            latest_model_version_sha256=latest_model_version_sha256,
            latest_recipe_revision_sha256=latest_recipe_revision_sha256,
            model_update_available=model_update_available,
            model_version_sha256=model_version_sha256,
            recipe_revision_sha256=recipe_revision_sha256,
            recipe_update_available=recipe_update_available,
            schema_version=schema_version,
            updated_at=updated_at,
        )

        return model_cache_update_response
