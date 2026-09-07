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
  from ..models.freshness_policy import FreshnessPolicy
  from ..models.library_recipe_summary import LibraryRecipeSummary





T = TypeVar("T", bound="LibraryRecipeList")



@_attrs_define
class LibraryRecipeList:
    """ Read-only overview of active canonical Recipe revisions.

        Attributes:
            freshness_policy (FreshnessPolicy):
            generated_at (datetime.datetime):
            next_cursor (Union[None, str]):
            recipes (list['LibraryRecipeSummary']):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    freshness_policy: 'FreshnessPolicy'
    generated_at: datetime.datetime
    next_cursor: Union[None, str]
    recipes: list['LibraryRecipeSummary']
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.freshness_policy import FreshnessPolicy
        from ..models.library_recipe_summary import LibraryRecipeSummary
        freshness_policy = self.freshness_policy.to_dict()

        generated_at = self.generated_at.isoformat()

        next_cursor: Union[None, str]
        next_cursor = self.next_cursor

        recipes = []
        for recipes_item_data in self.recipes:
            recipes_item = recipes_item_data.to_dict()
            recipes.append(recipes_item)



        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "freshness_policy": freshness_policy,
            "generated_at": generated_at,
            "next_cursor": next_cursor,
            "recipes": recipes,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.freshness_policy import FreshnessPolicy
        from ..models.library_recipe_summary import LibraryRecipeSummary
        d = dict(src_dict)
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
            recipes_item = LibraryRecipeSummary.from_dict(recipes_item_data)



            recipes.append(recipes_item)


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        library_recipe_list = cls(
            freshness_policy=freshness_policy,
            generated_at=generated_at,
            next_cursor=next_cursor,
            recipes=recipes,
            schema_version=schema_version,
        )

        return library_recipe_list
