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
from typing import Union
import datetime

if TYPE_CHECKING:
  from ..models.library_filter_values import LibraryFilterValues
  from ..models.library_facet_values import LibraryFacetValues
  from ..models.freshness_policy import FreshnessPolicy
  from ..models.library_recipe_projection import LibraryRecipeProjection





T = TypeVar("T", bound="RecipeLibraryResponse")



@_attrs_define
class RecipeLibraryResponse:
    """
        Attributes:
            facets (LibraryFacetValues):
            freshness_policy (FreshnessPolicy):
            generated_at (datetime.datetime):
            next_cursor (Union[None, str]):
            recipes (list['LibraryRecipeProjection']):
            filters (Union[Unset, LibraryFilterValues]): The filters that produced a Library page, echoed to the client.

                This is a typed echo rather than a free-form map so the request and the
                response describe the same vocabulary. Every field is optional, so a page
                that applied no filter stays valid without inventing values.
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    facets: 'LibraryFacetValues'
    freshness_policy: 'FreshnessPolicy'
    generated_at: datetime.datetime
    next_cursor: Union[None, str]
    recipes: list['LibraryRecipeProjection']
    filters: Union[Unset, 'LibraryFilterValues'] = UNSET
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.library_filter_values import LibraryFilterValues
        from ..models.library_facet_values import LibraryFacetValues
        from ..models.freshness_policy import FreshnessPolicy
        from ..models.library_recipe_projection import LibraryRecipeProjection
        facets = self.facets.to_dict()

        freshness_policy = self.freshness_policy.to_dict()

        generated_at = self.generated_at.isoformat()

        next_cursor: Union[None, str]
        next_cursor = self.next_cursor

        recipes = []
        for recipes_item_data in self.recipes:
            recipes_item = recipes_item_data.to_dict()
            recipes.append(recipes_item)



        filters: Union[Unset, dict[str, Any]] = UNSET
        if not isinstance(self.filters, Unset):
            filters = self.filters.to_dict()

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "facets": facets,
            "freshness_policy": freshness_policy,
            "generated_at": generated_at,
            "next_cursor": next_cursor,
            "recipes": recipes,
        })
        if filters is not UNSET:
            field_dict["filters"] = filters
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.library_filter_values import LibraryFilterValues
        from ..models.library_facet_values import LibraryFacetValues
        from ..models.freshness_policy import FreshnessPolicy
        from ..models.library_recipe_projection import LibraryRecipeProjection
        d = dict(src_dict)
        facets = LibraryFacetValues.from_dict(d.pop("facets"))




        freshness_policy = FreshnessPolicy.from_dict(d.pop("freshness_policy"))




        generated_at = isoparse(d.pop("generated_at"))




        def _parse_next_cursor(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        next_cursor = _parse_next_cursor(d.pop("next_cursor"))


        recipes = []
        _recipes = d.pop("recipes")
        for recipes_item_data in (_recipes):
            recipes_item = LibraryRecipeProjection.from_dict(recipes_item_data)



            recipes.append(recipes_item)


        _filters = d.pop("filters", UNSET)
        filters: Union[Unset, LibraryFilterValues]
        if isinstance(_filters,  Unset):
            filters = UNSET
        else:
            filters = LibraryFilterValues.from_dict(_filters)




        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        recipe_library_response = cls(
            facets=facets,
            freshness_policy=freshness_policy,
            generated_at=generated_at,
            next_cursor=next_cursor,
            recipes=recipes,
            filters=filters,
            schema_version=schema_version,
        )

        return recipe_library_response
