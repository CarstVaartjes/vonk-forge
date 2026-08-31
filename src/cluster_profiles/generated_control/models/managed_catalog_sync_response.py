from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.managed_catalog_sync_response_state import check_managed_catalog_sync_response_state
from ..models.managed_catalog_sync_response_state import ManagedCatalogSyncResponseState
from ..models.managed_catalog_sync_response_trigger import check_managed_catalog_sync_response_trigger
from ..models.managed_catalog_sync_response_trigger import ManagedCatalogSyncResponseTrigger
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.managed_catalog_sync_problem import ManagedCatalogSyncProblem
  from ..models.managed_catalog_withdrawn_recipe import ManagedCatalogWithdrawnRecipe
  from ..models.managed_catalog_stale_recipe import ManagedCatalogStaleRecipe





T = TypeVar("T", bound="ManagedCatalogSyncResponse")



@_attrs_define
class ManagedCatalogSyncResponse:
    """
        Attributes:
            completed_at (Union[None, str]):
            created_at (str):
            imported_count (int):
            problems (list['ManagedCatalogSyncProblem']):
            processed_count (int):
            repository (str):
            request_key (str):
            skipped_count (int):
            stale_recipes (list['ManagedCatalogStaleRecipe']):
            state (ManagedCatalogSyncResponseState):
            sync_id (str):
            total_count (int):
            trigger (ManagedCatalogSyncResponseTrigger):
            unchanged_count (int):
            updated_count (int):
            withdrawn_count (int):
            withdrawn_recipes (list['ManagedCatalogWithdrawnRecipe']):
            commit (Union[None, Unset, str]):
            expected_commit (Union[None, Unset, str]):
            schema_version (Union[Literal[1], Unset]):  Default: 1.
     """

    completed_at: Union[None, str]
    created_at: str
    imported_count: int
    problems: list['ManagedCatalogSyncProblem']
    processed_count: int
    repository: str
    request_key: str
    skipped_count: int
    stale_recipes: list['ManagedCatalogStaleRecipe']
    state: ManagedCatalogSyncResponseState
    sync_id: str
    total_count: int
    trigger: ManagedCatalogSyncResponseTrigger
    unchanged_count: int
    updated_count: int
    withdrawn_count: int
    withdrawn_recipes: list['ManagedCatalogWithdrawnRecipe']
    commit: Union[None, Unset, str] = UNSET
    expected_commit: Union[None, Unset, str] = UNSET
    schema_version: Union[Literal[1], Unset] = 1





    def to_dict(self) -> dict[str, Any]:
        from ..models.managed_catalog_sync_problem import ManagedCatalogSyncProblem
        from ..models.managed_catalog_withdrawn_recipe import ManagedCatalogWithdrawnRecipe
        from ..models.managed_catalog_stale_recipe import ManagedCatalogStaleRecipe
        completed_at: Union[None, str]
        completed_at = self.completed_at

        created_at = self.created_at

        imported_count = self.imported_count

        problems = []
        for problems_item_data in self.problems:
            problems_item = problems_item_data.to_dict()
            problems.append(problems_item)



        processed_count = self.processed_count

        repository = self.repository

        request_key = self.request_key

        skipped_count = self.skipped_count

        stale_recipes = []
        for stale_recipes_item_data in self.stale_recipes:
            stale_recipes_item = stale_recipes_item_data.to_dict()
            stale_recipes.append(stale_recipes_item)



        state: str = self.state

        sync_id = self.sync_id

        total_count = self.total_count

        trigger: str = self.trigger

        unchanged_count = self.unchanged_count

        updated_count = self.updated_count

        withdrawn_count = self.withdrawn_count

        withdrawn_recipes = []
        for withdrawn_recipes_item_data in self.withdrawn_recipes:
            withdrawn_recipes_item = withdrawn_recipes_item_data.to_dict()
            withdrawn_recipes.append(withdrawn_recipes_item)



        commit: Union[None, Unset, str]
        if isinstance(self.commit, Unset):
            commit = UNSET
        else:
            commit = self.commit

        expected_commit: Union[None, Unset, str]
        if isinstance(self.expected_commit, Unset):
            expected_commit = UNSET
        else:
            expected_commit = self.expected_commit

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "completed_at": completed_at,
            "created_at": created_at,
            "imported_count": imported_count,
            "problems": problems,
            "processed_count": processed_count,
            "repository": repository,
            "request_key": request_key,
            "skipped_count": skipped_count,
            "stale_recipes": stale_recipes,
            "state": state,
            "sync_id": sync_id,
            "total_count": total_count,
            "trigger": trigger,
            "unchanged_count": unchanged_count,
            "updated_count": updated_count,
            "withdrawn_count": withdrawn_count,
            "withdrawn_recipes": withdrawn_recipes,
        })
        if commit is not UNSET:
            field_dict["commit"] = commit
        if expected_commit is not UNSET:
            field_dict["expected_commit"] = expected_commit
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.managed_catalog_sync_problem import ManagedCatalogSyncProblem
        from ..models.managed_catalog_withdrawn_recipe import ManagedCatalogWithdrawnRecipe
        from ..models.managed_catalog_stale_recipe import ManagedCatalogStaleRecipe
        d = dict(src_dict)
        def _parse_completed_at(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        completed_at = _parse_completed_at(d.pop("completed_at"))


        created_at = d.pop("created_at")

        imported_count = d.pop("imported_count")

        problems = []
        _problems = d.pop("problems")
        for problems_item_data in (_problems):
            problems_item = ManagedCatalogSyncProblem.from_dict(problems_item_data)



            problems.append(problems_item)


        processed_count = d.pop("processed_count")

        repository = d.pop("repository")

        request_key = d.pop("request_key")

        skipped_count = d.pop("skipped_count")

        stale_recipes = []
        _stale_recipes = d.pop("stale_recipes")
        for stale_recipes_item_data in (_stale_recipes):
            stale_recipes_item = ManagedCatalogStaleRecipe.from_dict(stale_recipes_item_data)



            stale_recipes.append(stale_recipes_item)


        state = check_managed_catalog_sync_response_state(d.pop("state"))




        sync_id = d.pop("sync_id")

        total_count = d.pop("total_count")

        trigger = check_managed_catalog_sync_response_trigger(d.pop("trigger"))




        unchanged_count = d.pop("unchanged_count")

        updated_count = d.pop("updated_count")

        withdrawn_count = d.pop("withdrawn_count")

        withdrawn_recipes = []
        _withdrawn_recipes = d.pop("withdrawn_recipes")
        for withdrawn_recipes_item_data in (_withdrawn_recipes):
            withdrawn_recipes_item = ManagedCatalogWithdrawnRecipe.from_dict(withdrawn_recipes_item_data)



            withdrawn_recipes.append(withdrawn_recipes_item)


        def _parse_commit(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        commit = _parse_commit(d.pop("commit", UNSET))


        def _parse_expected_commit(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        expected_commit = _parse_expected_commit(d.pop("expected_commit", UNSET))


        schema_version = cast(Union[Literal[1], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 1 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        managed_catalog_sync_response = cls(
            completed_at=completed_at,
            created_at=created_at,
            imported_count=imported_count,
            problems=problems,
            processed_count=processed_count,
            repository=repository,
            request_key=request_key,
            skipped_count=skipped_count,
            stale_recipes=stale_recipes,
            state=state,
            sync_id=sync_id,
            total_count=total_count,
            trigger=trigger,
            unchanged_count=unchanged_count,
            updated_count=updated_count,
            withdrawn_count=withdrawn_count,
            withdrawn_recipes=withdrawn_recipes,
            commit=commit,
            expected_commit=expected_commit,
            schema_version=schema_version,
        )

        return managed_catalog_sync_response
