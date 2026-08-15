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
  from ..models.library_model import LibraryModel





T = TypeVar("T", bound="LibrarySnapshot")



@_attrs_define
class LibrarySnapshot:
    """
        Attributes:
            freshness_policy (FreshnessPolicy):
            generated_at (datetime.datetime):
            models (list['LibraryModel']):
            next_cursor (Union[None, str]):
            unlinked_recipes (list['LibraryRecipeSummary']):
            schema_version (Union[Literal[1], Unset]):  Default: 1.
     """

    freshness_policy: 'FreshnessPolicy'
    generated_at: datetime.datetime
    models: list['LibraryModel']
    next_cursor: Union[None, str]
    unlinked_recipes: list['LibraryRecipeSummary']
    schema_version: Union[Literal[1], Unset] = 1





    def to_dict(self) -> dict[str, Any]:
        from ..models.freshness_policy import FreshnessPolicy
        from ..models.library_recipe_summary import LibraryRecipeSummary
        from ..models.library_model import LibraryModel
        freshness_policy = self.freshness_policy.to_dict()

        generated_at = self.generated_at.isoformat()

        models = []
        for models_item_data in self.models:
            models_item = models_item_data.to_dict()
            models.append(models_item)



        next_cursor: Union[None, str]
        next_cursor = self.next_cursor

        unlinked_recipes = []
        for unlinked_recipes_item_data in self.unlinked_recipes:
            unlinked_recipes_item = unlinked_recipes_item_data.to_dict()
            unlinked_recipes.append(unlinked_recipes_item)



        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "freshness_policy": freshness_policy,
            "generated_at": generated_at,
            "models": models,
            "next_cursor": next_cursor,
            "unlinked_recipes": unlinked_recipes,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.freshness_policy import FreshnessPolicy
        from ..models.library_recipe_summary import LibraryRecipeSummary
        from ..models.library_model import LibraryModel
        d = dict(src_dict)
        freshness_policy = FreshnessPolicy.from_dict(d.pop("freshness_policy"))




        generated_at = isoparse(d.pop("generated_at"))




        models = []
        _models = d.pop("models")
        for models_item_data in (_models):
            models_item = LibraryModel.from_dict(models_item_data)



            models.append(models_item)


        def _parse_next_cursor(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        next_cursor = _parse_next_cursor(d.pop("next_cursor"))


        unlinked_recipes = []
        _unlinked_recipes = d.pop("unlinked_recipes")
        for unlinked_recipes_item_data in (_unlinked_recipes):
            unlinked_recipes_item = LibraryRecipeSummary.from_dict(unlinked_recipes_item_data)



            unlinked_recipes.append(unlinked_recipes_item)


        schema_version = cast(Union[Literal[1], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 1 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        library_snapshot = cls(
            freshness_policy=freshness_policy,
            generated_at=generated_at,
            models=models,
            next_cursor=next_cursor,
            unlinked_recipes=unlinked_recipes,
            schema_version=schema_version,
        )

        return library_snapshot
