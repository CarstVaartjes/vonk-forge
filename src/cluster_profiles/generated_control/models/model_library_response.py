from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
import datetime

if TYPE_CHECKING:
  from ..models.library_facet_values import LibraryFacetValues
  from ..models.freshness_policy import FreshnessPolicy
  from ..models.library_model_projection import LibraryModelProjection
  from ..models.model_library_response_filters import ModelLibraryResponseFilters





T = TypeVar("T", bound="ModelLibraryResponse")



@_attrs_define
class ModelLibraryResponse:
    """
        Attributes:
            facets (LibraryFacetValues):
            filters (ModelLibraryResponseFilters):
            freshness_policy (FreshnessPolicy):
            generated_at (datetime.datetime):
            models (list['LibraryModelProjection']):
            next_cursor (Union[None, str]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    facets: 'LibraryFacetValues'
    filters: 'ModelLibraryResponseFilters'
    freshness_policy: 'FreshnessPolicy'
    generated_at: datetime.datetime
    models: list['LibraryModelProjection']
    next_cursor: Union[None, str]
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.library_facet_values import LibraryFacetValues
        from ..models.freshness_policy import FreshnessPolicy
        from ..models.library_model_projection import LibraryModelProjection
        from ..models.model_library_response_filters import ModelLibraryResponseFilters
        facets = self.facets.to_dict()

        filters = self.filters.to_dict()

        freshness_policy = self.freshness_policy.to_dict()

        generated_at = self.generated_at.isoformat()

        models = []
        for models_item_data in self.models:
            models_item = models_item_data.to_dict()
            models.append(models_item)



        next_cursor: Union[None, str]
        next_cursor = self.next_cursor

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "facets": facets,
            "filters": filters,
            "freshness_policy": freshness_policy,
            "generated_at": generated_at,
            "models": models,
            "next_cursor": next_cursor,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.library_facet_values import LibraryFacetValues
        from ..models.freshness_policy import FreshnessPolicy
        from ..models.library_model_projection import LibraryModelProjection
        from ..models.model_library_response_filters import ModelLibraryResponseFilters
        d = dict(src_dict)
        facets = LibraryFacetValues.from_dict(d.pop("facets"))




        filters = ModelLibraryResponseFilters.from_dict(d.pop("filters"))




        freshness_policy = FreshnessPolicy.from_dict(d.pop("freshness_policy"))




        generated_at = isoparse(d.pop("generated_at"))




        models = []
        _models = d.pop("models")
        for models_item_data in (_models):
            models_item = LibraryModelProjection.from_dict(models_item_data)



            models.append(models_item)


        def _parse_next_cursor(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        next_cursor = _parse_next_cursor(d.pop("next_cursor"))


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        model_library_response = cls(
            facets=facets,
            filters=filters,
            freshness_policy=freshness_policy,
            generated_at=generated_at,
            models=models,
            next_cursor=next_cursor,
            schema_version=schema_version,
        )

        return model_library_response
