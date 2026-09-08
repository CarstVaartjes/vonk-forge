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
  from ..models.model_reference import ModelReference
  from ..models.model_cache_upstream_revision import ModelCacheUpstreamRevision





T = TypeVar("T", bound="ModelCacheUpdateResponse")



@_attrs_define
class ModelCacheUpdateResponse:
    """
        Attributes:
            artifact_set_sha256 (str):
            latest_model_content_sha256 (Union[None, str]):
            latest_recipe_revision_sha256 (Union[None, str]):
            model_content_sha256 (Union[None, str]):
            model_update_available (bool):
            recipe_revision_sha256 (Union[None, str]):
            recipe_update_available (bool):
            model_update_ambiguous (Union[Unset, bool]):  Default: False.
            model_update_candidates (Union[Unset, list['ModelReference']]):
            model_update_from (Union['ModelReference', None, Unset]):
            model_update_to (Union['ModelReference', None, Unset]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
            updated_at (Union[None, Unset, str]):
            upstream_revisions (Union[Unset, list['ModelCacheUpstreamRevision']]):
     """

    artifact_set_sha256: str
    latest_model_content_sha256: Union[None, str]
    latest_recipe_revision_sha256: Union[None, str]
    model_content_sha256: Union[None, str]
    model_update_available: bool
    recipe_revision_sha256: Union[None, str]
    recipe_update_available: bool
    model_update_ambiguous: Union[Unset, bool] = False
    model_update_candidates: Union[Unset, list['ModelReference']] = UNSET
    model_update_from: Union['ModelReference', None, Unset] = UNSET
    model_update_to: Union['ModelReference', None, Unset] = UNSET
    schema_version: Union[Literal[2], Unset] = 2
    updated_at: Union[None, Unset, str] = UNSET
    upstream_revisions: Union[Unset, list['ModelCacheUpstreamRevision']] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.model_reference import ModelReference
        from ..models.model_cache_upstream_revision import ModelCacheUpstreamRevision
        artifact_set_sha256 = self.artifact_set_sha256

        latest_model_content_sha256: Union[None, str]
        latest_model_content_sha256 = self.latest_model_content_sha256

        latest_recipe_revision_sha256: Union[None, str]
        latest_recipe_revision_sha256 = self.latest_recipe_revision_sha256

        model_content_sha256: Union[None, str]
        model_content_sha256 = self.model_content_sha256

        model_update_available = self.model_update_available

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
        elif isinstance(self.model_update_from, ModelReference):
            model_update_from = self.model_update_from.to_dict()
        else:
            model_update_from = self.model_update_from

        model_update_to: Union[None, Unset, dict[str, Any]]
        if isinstance(self.model_update_to, Unset):
            model_update_to = UNSET
        elif isinstance(self.model_update_to, ModelReference):
            model_update_to = self.model_update_to.to_dict()
        else:
            model_update_to = self.model_update_to

        schema_version = self.schema_version

        updated_at: Union[None, Unset, str]
        if isinstance(self.updated_at, Unset):
            updated_at = UNSET
        else:
            updated_at = self.updated_at

        upstream_revisions: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.upstream_revisions, Unset):
            upstream_revisions = []
            for upstream_revisions_item_data in self.upstream_revisions:
                upstream_revisions_item = upstream_revisions_item_data.to_dict()
                upstream_revisions.append(upstream_revisions_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifact_set_sha256": artifact_set_sha256,
            "latest_model_content_sha256": latest_model_content_sha256,
            "latest_recipe_revision_sha256": latest_recipe_revision_sha256,
            "model_content_sha256": model_content_sha256,
            "model_update_available": model_update_available,
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
        if upstream_revisions is not UNSET:
            field_dict["upstream_revisions"] = upstream_revisions

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.model_reference import ModelReference
        from ..models.model_cache_upstream_revision import ModelCacheUpstreamRevision
        d = dict(src_dict)
        artifact_set_sha256 = d.pop("artifact_set_sha256")

        def _parse_latest_model_content_sha256(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        latest_model_content_sha256 = _parse_latest_model_content_sha256(d.pop("latest_model_content_sha256"))


        def _parse_latest_recipe_revision_sha256(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        latest_recipe_revision_sha256 = _parse_latest_recipe_revision_sha256(d.pop("latest_recipe_revision_sha256"))


        def _parse_model_content_sha256(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        model_content_sha256 = _parse_model_content_sha256(d.pop("model_content_sha256"))


        model_update_available = d.pop("model_update_available")

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
            model_update_candidates_item = ModelReference.from_dict(model_update_candidates_item_data)



            model_update_candidates.append(model_update_candidates_item)


        def _parse_model_update_from(data: object) -> Union['ModelReference', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                model_update_from_type_0 = ModelReference.from_dict(data)



                return model_update_from_type_0
            except: # noqa: E722
                pass
            return cast(Union['ModelReference', None, Unset], data)

        model_update_from = _parse_model_update_from(d.pop("model_update_from", UNSET))


        def _parse_model_update_to(data: object) -> Union['ModelReference', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                model_update_to_type_0 = ModelReference.from_dict(data)



                return model_update_to_type_0
            except: # noqa: E722
                pass
            return cast(Union['ModelReference', None, Unset], data)

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


        upstream_revisions = []
        _upstream_revisions = d.pop("upstream_revisions", UNSET)
        for upstream_revisions_item_data in (_upstream_revisions or []):
            upstream_revisions_item = ModelCacheUpstreamRevision.from_dict(upstream_revisions_item_data)



            upstream_revisions.append(upstream_revisions_item)


        model_cache_update_response = cls(
            artifact_set_sha256=artifact_set_sha256,
            latest_model_content_sha256=latest_model_content_sha256,
            latest_recipe_revision_sha256=latest_recipe_revision_sha256,
            model_content_sha256=model_content_sha256,
            model_update_available=model_update_available,
            recipe_revision_sha256=recipe_revision_sha256,
            recipe_update_available=recipe_update_available,
            model_update_ambiguous=model_update_ambiguous,
            model_update_candidates=model_update_candidates,
            model_update_from=model_update_from,
            model_update_to=model_update_to,
            schema_version=schema_version,
            updated_at=updated_at,
            upstream_revisions=upstream_revisions,
        )

        return model_cache_update_response
