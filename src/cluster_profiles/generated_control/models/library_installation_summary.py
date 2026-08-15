from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.library_installation_summary_state import check_library_installation_summary_state
from ..models.library_installation_summary_state import LibraryInstallationSummaryState
from typing import cast






T = TypeVar("T", bound="LibraryInstallationSummary")



@_attrs_define
class LibraryInstallationSummary:
    """
        Attributes:
            complete (bool):
            expected_rank_count (int):
            installation_id (str):
            installed_rank_count (int):
            recipe_revision_id (str):
            state (LibraryInstallationSummaryState):
     """

    complete: bool
    expected_rank_count: int
    installation_id: str
    installed_rank_count: int
    recipe_revision_id: str
    state: LibraryInstallationSummaryState





    def to_dict(self) -> dict[str, Any]:
        complete = self.complete

        expected_rank_count = self.expected_rank_count

        installation_id = self.installation_id

        installed_rank_count = self.installed_rank_count

        recipe_revision_id = self.recipe_revision_id

        state: str = self.state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "complete": complete,
            "expected_rank_count": expected_rank_count,
            "installation_id": installation_id,
            "installed_rank_count": installed_rank_count,
            "recipe_revision_id": recipe_revision_id,
            "state": state,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        complete = d.pop("complete")

        expected_rank_count = d.pop("expected_rank_count")

        installation_id = d.pop("installation_id")

        installed_rank_count = d.pop("installed_rank_count")

        recipe_revision_id = d.pop("recipe_revision_id")

        state = check_library_installation_summary_state(d.pop("state"))




        library_installation_summary = cls(
            complete=complete,
            expected_rank_count=expected_rank_count,
            installation_id=installation_id,
            installed_rank_count=installed_rank_count,
            recipe_revision_id=recipe_revision_id,
            state=state,
        )

        return library_installation_summary
