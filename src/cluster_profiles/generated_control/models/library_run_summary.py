from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.library_run_summary_route_state import check_library_run_summary_route_state
from ..models.library_run_summary_route_state import LibraryRunSummaryRouteState
from ..models.library_run_summary_state import check_library_run_summary_state
from ..models.library_run_summary_state import LibraryRunSummaryState
from typing import cast






T = TypeVar("T", bound="LibraryRunSummary")



@_attrs_define
class LibraryRunSummary:
    """
        Attributes:
            expected_rank_count (int):
            healthy (bool):
            healthy_rank_count (int):
            installation_id (str):
            recipe_revision_id (str):
            route_state (LibraryRunSummaryRouteState):
            run_id (str):
            state (LibraryRunSummaryState):
     """

    expected_rank_count: int
    healthy: bool
    healthy_rank_count: int
    installation_id: str
    recipe_revision_id: str
    route_state: LibraryRunSummaryRouteState
    run_id: str
    state: LibraryRunSummaryState





    def to_dict(self) -> dict[str, Any]:
        expected_rank_count = self.expected_rank_count

        healthy = self.healthy

        healthy_rank_count = self.healthy_rank_count

        installation_id = self.installation_id

        recipe_revision_id = self.recipe_revision_id

        route_state: str = self.route_state

        run_id = self.run_id

        state: str = self.state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "expected_rank_count": expected_rank_count,
            "healthy": healthy,
            "healthy_rank_count": healthy_rank_count,
            "installation_id": installation_id,
            "recipe_revision_id": recipe_revision_id,
            "route_state": route_state,
            "run_id": run_id,
            "state": state,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        expected_rank_count = d.pop("expected_rank_count")

        healthy = d.pop("healthy")

        healthy_rank_count = d.pop("healthy_rank_count")

        installation_id = d.pop("installation_id")

        recipe_revision_id = d.pop("recipe_revision_id")

        route_state = check_library_run_summary_route_state(d.pop("route_state"))




        run_id = d.pop("run_id")

        state = check_library_run_summary_state(d.pop("state"))




        library_run_summary = cls(
            expected_rank_count=expected_rank_count,
            healthy=healthy,
            healthy_rank_count=healthy_rank_count,
            installation_id=installation_id,
            recipe_revision_id=recipe_revision_id,
            route_state=route_state,
            run_id=run_id,
            state=state,
        )

        return library_run_summary
