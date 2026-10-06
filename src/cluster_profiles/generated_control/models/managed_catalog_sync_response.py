from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.catalog_sync_state import CatalogSyncState
from ..models.catalog_sync_state import check_catalog_sync_state
from ..models.managed_catalog_sync_response_trigger import check_managed_catalog_sync_response_trigger
from ..models.managed_catalog_sync_response_trigger import ManagedCatalogSyncResponseTrigger
from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.managed_catalog_stale_recipe import ManagedCatalogStaleRecipe
  from ..models.managed_catalog_sync_failure import ManagedCatalogSyncFailure
  from ..models.managed_catalog_sync_problem import ManagedCatalogSyncProblem
  from ..models.managed_catalog_withdrawn_recipe import ManagedCatalogWithdrawnRecipe





T = TypeVar("T", bound="ManagedCatalogSyncResponse")



@_attrs_define
class ManagedCatalogSyncResponse:
    """
        Attributes:
            completed_at (None | str):
            created_at (str):
            imported_count (int):
            problems (list[ManagedCatalogSyncProblem]):
            processed_count (int):
            repository (str):
            request_key (str):
            skipped_count (int):
            stale_recipes (list[ManagedCatalogStaleRecipe]):
            state (CatalogSyncState): The outcome of a catalog synchronization, as the catalog shows it.
            sync_id (str):
            total_count (int):
            trigger (ManagedCatalogSyncResponseTrigger):
            unchanged_count (int):
            updated_count (int):
            withdrawn_count (int):
            withdrawn_recipes (list[ManagedCatalogWithdrawnRecipe]):
            commit (None | str | Unset):
            expected_commit (None | str | Unset):
            last_error (ManagedCatalogSyncFailure | None | Unset):
            library_updated_at (None | str | Unset):
            library_version (None | str | Unset):
     """

    completed_at: None | str
    created_at: str
    imported_count: int
    problems: list[ManagedCatalogSyncProblem]
    processed_count: int
    repository: str
    request_key: str
    skipped_count: int
    stale_recipes: list[ManagedCatalogStaleRecipe]
    state: CatalogSyncState
    sync_id: str
    total_count: int
    trigger: ManagedCatalogSyncResponseTrigger
    unchanged_count: int
    updated_count: int
    withdrawn_count: int
    withdrawn_recipes: list[ManagedCatalogWithdrawnRecipe]
    commit: None | str | Unset = UNSET
    expected_commit: None | str | Unset = UNSET
    last_error: ManagedCatalogSyncFailure | None | Unset = UNSET
    library_updated_at: None | str | Unset = UNSET
    library_version: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.managed_catalog_stale_recipe import ManagedCatalogStaleRecipe # noqa: PLC0415
        from ..models.managed_catalog_sync_failure import ManagedCatalogSyncFailure # noqa: PLC0415
        from ..models.managed_catalog_sync_problem import ManagedCatalogSyncProblem # noqa: PLC0415
        from ..models.managed_catalog_withdrawn_recipe import ManagedCatalogWithdrawnRecipe # noqa: PLC0415
        completed_at: None | str
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



        commit: None | str | Unset
        if isinstance(self.commit, Unset):
            commit = UNSET
        else:
            commit = self.commit

        expected_commit: None | str | Unset
        if isinstance(self.expected_commit, Unset):
            expected_commit = UNSET
        else:
            expected_commit = self.expected_commit

        last_error: dict[str, Any] | None | Unset
        if isinstance(self.last_error, Unset):
            last_error = UNSET
        elif isinstance(self.last_error, ManagedCatalogSyncFailure):
            last_error = self.last_error.to_dict()
        else:
            last_error = self.last_error

        library_updated_at: None | str | Unset
        if isinstance(self.library_updated_at, Unset):
            library_updated_at = UNSET
        else:
            library_updated_at = self.library_updated_at

        library_version: None | str | Unset
        if isinstance(self.library_version, Unset):
            library_version = UNSET
        else:
            library_version = self.library_version


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
        if last_error is not UNSET:
            field_dict["last_error"] = last_error
        if library_updated_at is not UNSET:
            field_dict["library_updated_at"] = library_updated_at
        if library_version is not UNSET:
            field_dict["library_version"] = library_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.managed_catalog_stale_recipe import ManagedCatalogStaleRecipe # noqa: PLC0415
        from ..models.managed_catalog_sync_failure import ManagedCatalogSyncFailure # noqa: PLC0415
        from ..models.managed_catalog_sync_problem import ManagedCatalogSyncProblem # noqa: PLC0415
        from ..models.managed_catalog_withdrawn_recipe import ManagedCatalogWithdrawnRecipe # noqa: PLC0415
        d = dict(src_dict)
        def _parse_completed_at(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

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


        state = check_catalog_sync_state(d.pop("state"))




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


        def _parse_commit(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        commit = _parse_commit(d.pop("commit", UNSET))


        def _parse_expected_commit(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        expected_commit = _parse_expected_commit(d.pop("expected_commit", UNSET))


        def _parse_last_error(data: object) -> ManagedCatalogSyncFailure | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                last_error_type_0 = ManagedCatalogSyncFailure.from_dict(data)



                return last_error_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ManagedCatalogSyncFailure | None | Unset, data)

        last_error = _parse_last_error(d.pop("last_error", UNSET))


        def _parse_library_updated_at(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        library_updated_at = _parse_library_updated_at(d.pop("library_updated_at", UNSET))


        def _parse_library_version(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        library_version = _parse_library_version(d.pop("library_version", UNSET))


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
            last_error=last_error,
            library_updated_at=library_updated_at,
            library_version=library_version,
        )

        return managed_catalog_sync_response
