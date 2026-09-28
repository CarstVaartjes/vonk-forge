from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
import datetime

if TYPE_CHECKING:
  from ..models.freshness_policy import FreshnessPolicy
  from ..models.library_facet_values import LibraryFacetValues
  from ..models.library_filter_values import LibraryFilterValues
  from ..models.library_recipe_projection import LibraryRecipeProjection
  from ..models.library_release import LibraryRelease





T = TypeVar("T", bound="RecipeLibraryResponse")



@_attrs_define
class RecipeLibraryResponse:
    """
        Attributes:
            facets (LibraryFacetValues):
            freshness_policy (FreshnessPolicy):
            generated_at (datetime.datetime):
            next_cursor (None | str):
            recipes (list[LibraryRecipeProjection]):
            filters (LibraryFilterValues | Unset): The filters that produced a Library page, echoed to the client.

                This is a typed echo rather than a free-form map so the request and the
                response describe the same vocabulary. Every field is optional, so a page
                that applied no filter stays valid without inventing values.
            library (LibraryRelease | None | Unset):
     """

    facets: LibraryFacetValues
    freshness_policy: FreshnessPolicy
    generated_at: datetime.datetime
    next_cursor: None | str
    recipes: list[LibraryRecipeProjection]
    filters: LibraryFilterValues | Unset = UNSET
    library: LibraryRelease | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.freshness_policy import FreshnessPolicy # noqa: PLC0415
        from ..models.library_facet_values import LibraryFacetValues # noqa: PLC0415
        from ..models.library_filter_values import LibraryFilterValues # noqa: PLC0415
        from ..models.library_recipe_projection import LibraryRecipeProjection # noqa: PLC0415
        from ..models.library_release import LibraryRelease # noqa: PLC0415
        facets = self.facets.to_dict()

        freshness_policy = self.freshness_policy.to_dict()

        generated_at = self.generated_at.isoformat()

        next_cursor: None | str
        next_cursor = self.next_cursor

        recipes = []
        for recipes_item_data in self.recipes:
            recipes_item = recipes_item_data.to_dict()
            recipes.append(recipes_item)



        filters: dict[str, Any] | Unset = UNSET
        if not isinstance(self.filters, Unset):
            filters = self.filters.to_dict()

        library: dict[str, Any] | None | Unset
        if isinstance(self.library, Unset):
            library = UNSET
        elif isinstance(self.library, LibraryRelease):
            library = self.library.to_dict()
        else:
            library = self.library


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
        if library is not UNSET:
            field_dict["library"] = library

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.freshness_policy import FreshnessPolicy # noqa: PLC0415
        from ..models.library_facet_values import LibraryFacetValues # noqa: PLC0415
        from ..models.library_filter_values import LibraryFilterValues # noqa: PLC0415
        from ..models.library_recipe_projection import LibraryRecipeProjection # noqa: PLC0415
        from ..models.library_release import LibraryRelease # noqa: PLC0415
        d = dict(src_dict)
        facets = LibraryFacetValues.from_dict(d.pop("facets"))




        freshness_policy = FreshnessPolicy.from_dict(d.pop("freshness_policy"))




        generated_at = datetime.datetime.fromisoformat(d.pop("generated_at"))




        def _parse_next_cursor(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        next_cursor = _parse_next_cursor(d.pop("next_cursor"))


        recipes = []
        _recipes = d.pop("recipes")
        for recipes_item_data in (_recipes):
            recipes_item = LibraryRecipeProjection.from_dict(recipes_item_data)



            recipes.append(recipes_item)


        _filters = d.pop("filters", UNSET)
        filters: LibraryFilterValues | Unset
        if isinstance(_filters,  Unset):
            filters = UNSET
        else:
            filters = LibraryFilterValues.from_dict(_filters)




        def _parse_library(data: object) -> LibraryRelease | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                library_type_0 = LibraryRelease.from_dict(data)



                return library_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(LibraryRelease | None | Unset, data)

        library = _parse_library(d.pop("library", UNSET))


        recipe_library_response = cls(
            facets=facets,
            freshness_policy=freshness_policy,
            generated_at=generated_at,
            next_cursor=next_cursor,
            recipes=recipes,
            filters=filters,
            library=library,
        )

        return recipe_library_response
