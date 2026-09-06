from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union






T = TypeVar("T", bound="ModelCacheDownloadRequest")



@_attrs_define
class ModelCacheDownloadRequest:
    """
        Attributes:
            plan_digest (str):
            request_key (str):
            artifact_set_sha256 (Union[None, Unset, str]):
            model_version_sha256 (Union[None, Unset, str]):
            recipe_revision_id (Union[None, Unset, str]):
            recipe_revision_sha256 (Union[None, Unset, str]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
            source_policy (Union[Literal['nas-first'], Unset]):  Default: 'nas-first'.
     """

    plan_digest: str
    request_key: str
    artifact_set_sha256: Union[None, Unset, str] = UNSET
    model_version_sha256: Union[None, Unset, str] = UNSET
    recipe_revision_id: Union[None, Unset, str] = UNSET
    recipe_revision_sha256: Union[None, Unset, str] = UNSET
    schema_version: Union[Literal[2], Unset] = 2
    source_policy: Union[Literal['nas-first'], Unset] = 'nas-first'





    def to_dict(self) -> dict[str, Any]:
        plan_digest = self.plan_digest

        request_key = self.request_key

        artifact_set_sha256: Union[None, Unset, str]
        if isinstance(self.artifact_set_sha256, Unset):
            artifact_set_sha256 = UNSET
        else:
            artifact_set_sha256 = self.artifact_set_sha256

        model_version_sha256: Union[None, Unset, str]
        if isinstance(self.model_version_sha256, Unset):
            model_version_sha256 = UNSET
        else:
            model_version_sha256 = self.model_version_sha256

        recipe_revision_id: Union[None, Unset, str]
        if isinstance(self.recipe_revision_id, Unset):
            recipe_revision_id = UNSET
        else:
            recipe_revision_id = self.recipe_revision_id

        recipe_revision_sha256: Union[None, Unset, str]
        if isinstance(self.recipe_revision_sha256, Unset):
            recipe_revision_sha256 = UNSET
        else:
            recipe_revision_sha256 = self.recipe_revision_sha256

        schema_version = self.schema_version

        source_policy = self.source_policy


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "plan_digest": plan_digest,
            "request_key": request_key,
        })
        if artifact_set_sha256 is not UNSET:
            field_dict["artifact_set_sha256"] = artifact_set_sha256
        if model_version_sha256 is not UNSET:
            field_dict["model_version_sha256"] = model_version_sha256
        if recipe_revision_id is not UNSET:
            field_dict["recipe_revision_id"] = recipe_revision_id
        if recipe_revision_sha256 is not UNSET:
            field_dict["recipe_revision_sha256"] = recipe_revision_sha256
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if source_policy is not UNSET:
            field_dict["source_policy"] = source_policy

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        plan_digest = d.pop("plan_digest")

        request_key = d.pop("request_key")

        def _parse_artifact_set_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        artifact_set_sha256 = _parse_artifact_set_sha256(d.pop("artifact_set_sha256", UNSET))


        def _parse_model_version_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        model_version_sha256 = _parse_model_version_sha256(d.pop("model_version_sha256", UNSET))


        def _parse_recipe_revision_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        recipe_revision_id = _parse_recipe_revision_id(d.pop("recipe_revision_id", UNSET))


        def _parse_recipe_revision_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        recipe_revision_sha256 = _parse_recipe_revision_sha256(d.pop("recipe_revision_sha256", UNSET))


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        source_policy = cast(Union[Literal['nas-first'], Unset] , d.pop("source_policy", UNSET))
        if source_policy != 'nas-first' and not isinstance(source_policy, Unset):
            raise ValueError(f"source_policy must match const 'nas-first', got '{source_policy}'")

        model_cache_download_request = cls(
            plan_digest=plan_digest,
            request_key=request_key,
            artifact_set_sha256=artifact_set_sha256,
            model_version_sha256=model_version_sha256,
            recipe_revision_id=recipe_revision_id,
            recipe_revision_sha256=recipe_revision_sha256,
            schema_version=schema_version,
            source_policy=source_policy,
        )

        return model_cache_download_request
