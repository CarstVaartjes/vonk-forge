from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.managed_catalog_sync_result_state import check_managed_catalog_sync_result_state
from ..models.managed_catalog_sync_result_state import ManagedCatalogSyncResultState
from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.managed_catalog_stale_recipe import ManagedCatalogStaleRecipe
  from ..models.managed_catalog_sync_problem import ManagedCatalogSyncProblem
  from ..models.managed_catalog_withdrawn_recipe import ManagedCatalogWithdrawnRecipe





T = TypeVar("T", bound="ManagedCatalogSyncResult")



@_attrs_define
class ManagedCatalogSyncResult:
    """
        Attributes:
            imported_count (int):
            problems (list[ManagedCatalogSyncProblem]):
            schema_version (Literal[1]):
            skipped_count (int):
            stale_recipes (list[ManagedCatalogStaleRecipe]):
            state (ManagedCatalogSyncResultState):
            unchanged_count (int):
            updated_count (int):
            withdrawn_count (int):
            withdrawn_recipes (list[ManagedCatalogWithdrawnRecipe]):
     """

    imported_count: int
    problems: list[ManagedCatalogSyncProblem]
    schema_version: Literal[1]
    skipped_count: int
    stale_recipes: list[ManagedCatalogStaleRecipe]
    state: ManagedCatalogSyncResultState
    unchanged_count: int
    updated_count: int
    withdrawn_count: int
    withdrawn_recipes: list[ManagedCatalogWithdrawnRecipe]





    def to_dict(self) -> dict[str, Any]:
        from ..models.managed_catalog_stale_recipe import ManagedCatalogStaleRecipe # noqa: PLC0415
        from ..models.managed_catalog_sync_problem import ManagedCatalogSyncProblem # noqa: PLC0415
        from ..models.managed_catalog_withdrawn_recipe import ManagedCatalogWithdrawnRecipe # noqa: PLC0415
        imported_count = self.imported_count

        problems = []
        for problems_item_data in self.problems:
            problems_item = problems_item_data.to_dict()
            problems.append(problems_item)



        schema_version = self.schema_version

        skipped_count = self.skipped_count

        stale_recipes = []
        for stale_recipes_item_data in self.stale_recipes:
            stale_recipes_item = stale_recipes_item_data.to_dict()
            stale_recipes.append(stale_recipes_item)



        state: str = self.state

        unchanged_count = self.unchanged_count

        updated_count = self.updated_count

        withdrawn_count = self.withdrawn_count

        withdrawn_recipes = []
        for withdrawn_recipes_item_data in self.withdrawn_recipes:
            withdrawn_recipes_item = withdrawn_recipes_item_data.to_dict()
            withdrawn_recipes.append(withdrawn_recipes_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "imported_count": imported_count,
            "problems": problems,
            "schema_version": schema_version,
            "skipped_count": skipped_count,
            "stale_recipes": stale_recipes,
            "state": state,
            "unchanged_count": unchanged_count,
            "updated_count": updated_count,
            "withdrawn_count": withdrawn_count,
            "withdrawn_recipes": withdrawn_recipes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.managed_catalog_stale_recipe import ManagedCatalogStaleRecipe # noqa: PLC0415
        from ..models.managed_catalog_sync_problem import ManagedCatalogSyncProblem # noqa: PLC0415
        from ..models.managed_catalog_withdrawn_recipe import ManagedCatalogWithdrawnRecipe # noqa: PLC0415
        d = dict(src_dict)
        imported_count = d.pop("imported_count")

        problems = []
        _problems = d.pop("problems")
        for problems_item_data in (_problems):
            problems_item = ManagedCatalogSyncProblem.from_dict(problems_item_data)



            problems.append(problems_item)


        schema_version = cast(Literal[1] , d.pop("schema_version"))
        if schema_version != 1:
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        skipped_count = d.pop("skipped_count")

        stale_recipes = []
        _stale_recipes = d.pop("stale_recipes")
        for stale_recipes_item_data in (_stale_recipes):
            stale_recipes_item = ManagedCatalogStaleRecipe.from_dict(stale_recipes_item_data)



            stale_recipes.append(stale_recipes_item)


        state = check_managed_catalog_sync_result_state(d.pop("state"))




        unchanged_count = d.pop("unchanged_count")

        updated_count = d.pop("updated_count")

        withdrawn_count = d.pop("withdrawn_count")

        withdrawn_recipes = []
        _withdrawn_recipes = d.pop("withdrawn_recipes")
        for withdrawn_recipes_item_data in (_withdrawn_recipes):
            withdrawn_recipes_item = ManagedCatalogWithdrawnRecipe.from_dict(withdrawn_recipes_item_data)



            withdrawn_recipes.append(withdrawn_recipes_item)


        managed_catalog_sync_result = cls(
            imported_count=imported_count,
            problems=problems,
            schema_version=schema_version,
            skipped_count=skipped_count,
            stale_recipes=stale_recipes,
            state=state,
            unchanged_count=unchanged_count,
            updated_count=updated_count,
            withdrawn_count=withdrawn_count,
            withdrawn_recipes=withdrawn_recipes,
        )

        return managed_catalog_sync_result
