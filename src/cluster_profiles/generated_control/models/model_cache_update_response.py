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

if TYPE_CHECKING:
  from ..models.model_cache_update_response_model_update_to_type_0 import ModelCacheUpdateResponseModelUpdateToType0
  from ..models.model_cache_update_response_model_update_from_type_0 import ModelCacheUpdateResponseModelUpdateFromType0
  from ..models.model_cache_update_response_model_update_candidates_item import ModelCacheUpdateResponseModelUpdateCandidatesItem





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
            model_update_ambiguous (Union[Unset, bool]):  Default: False.
            model_update_candidates (Union[Unset, list['ModelCacheUpdateResponseModelUpdateCandidatesItem']]):
            model_update_from (Union['ModelCacheUpdateResponseModelUpdateFromType0', None, Unset]):
            model_update_to (Union['ModelCacheUpdateResponseModelUpdateToType0', None, Unset]):
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
    model_update_ambiguous: Union[Unset, bool] = False
    model_update_candidates: Union[Unset, list['ModelCacheUpdateResponseModelUpdateCandidatesItem']] = UNSET
    model_update_from: Union['ModelCacheUpdateResponseModelUpdateFromType0', None, Unset] = UNSET
    model_update_to: Union['ModelCacheUpdateResponseModelUpdateToType0', None, Unset] = UNSET
    schema_version: Union[Literal[2], Unset] = 2
    updated_at: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.model_cache_update_response_model_update_to_type_0 import ModelCacheUpdateResponseModelUpdateToType0
        from ..models.model_cache_update_response_model_update_from_type_0 import ModelCacheUpdateResponseModelUpdateFromType0
        from ..models.model_cache_update_response_model_update_candidates_item import ModelCacheUpdateResponseModelUpdateCandidatesItem
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

        model_update_ambiguous = self.model_update_ambiguous

        model_update_candidates: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.model_update_candidates, Unset):
            model_update_candidates = []
            for model_update_candidates_item_data in self.model_update_candidates:
                model_update_candidates_item = model_update_candidates_item_data.to_dict()
                model_update_candidates.append(model_update_candidates_item)



        model_update_from: Union[None, Unset, dict[str, Any]]
        if isinstance(self.model_update_from, Unset):
            model_update_from = UNSET
        elif isinstance(self.model_update_from, ModelCacheUpdateResponseModelUpdateFromType0):
            model_update_from = self.model_update_from.to_dict()
        else:
            model_update_from = self.model_update_from

        model_update_to: Union[None, Unset, dict[str, Any]]
        if isinstance(self.model_update_to, Unset):
            model_update_to = UNSET
        elif isinstance(self.model_update_to, ModelCacheUpdateResponseModelUpdateToType0):
            model_update_to = self.model_update_to.to_dict()
        else:
            model_update_to = self.model_update_to

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
        if model_update_ambiguous is not UNSET:
            field_dict["model_update_ambiguous"] = model_update_ambiguous
        if model_update_candidates is not UNSET:
            field_dict["model_update_candidates"] = model_update_candidates
        if model_update_from is not UNSET:
            field_dict["model_update_from"] = model_update_from
        if model_update_to is not UNSET:
            field_dict["model_update_to"] = model_update_to
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if updated_at is not UNSET:
            field_dict["updated_at"] = updated_at

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.model_cache_update_response_model_update_to_type_0 import ModelCacheUpdateResponseModelUpdateToType0
        from ..models.model_cache_update_response_model_update_from_type_0 import ModelCacheUpdateResponseModelUpdateFromType0
        from ..models.model_cache_update_response_model_update_candidates_item import ModelCacheUpdateResponseModelUpdateCandidatesItem
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

        model_update_ambiguous = d.pop("model_update_ambiguous", UNSET)

        model_update_candidates = []
        _model_update_candidates = d.pop("model_update_candidates", UNSET)
        for model_update_candidates_item_data in (_model_update_candidates or []):
            model_update_candidates_item = ModelCacheUpdateResponseModelUpdateCandidatesItem.from_dict(model_update_candidates_item_data)



            model_update_candidates.append(model_update_candidates_item)


        def _parse_model_update_from(data: object) -> Union['ModelCacheUpdateResponseModelUpdateFromType0', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                model_update_from_type_0 = ModelCacheUpdateResponseModelUpdateFromType0.from_dict(data)



                return model_update_from_type_0
            except: # noqa: E722
                pass
            return cast(Union['ModelCacheUpdateResponseModelUpdateFromType0', None, Unset], data)

        model_update_from = _parse_model_update_from(d.pop("model_update_from", UNSET))


        def _parse_model_update_to(data: object) -> Union['ModelCacheUpdateResponseModelUpdateToType0', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                model_update_to_type_0 = ModelCacheUpdateResponseModelUpdateToType0.from_dict(data)



                return model_update_to_type_0
            except: # noqa: E722
                pass
            return cast(Union['ModelCacheUpdateResponseModelUpdateToType0', None, Unset], data)

        model_update_to = _parse_model_update_to(d.pop("model_update_to", UNSET))


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
            model_update_ambiguous=model_update_ambiguous,
            model_update_candidates=model_update_candidates,
            model_update_from=model_update_from,
            model_update_to=model_update_to,
            schema_version=schema_version,
            updated_at=updated_at,
        )

        return model_cache_update_response
