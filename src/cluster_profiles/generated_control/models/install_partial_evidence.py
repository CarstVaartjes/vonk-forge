from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.install_degraded_reason import check_install_degraded_reason
from ..models.install_degraded_reason import InstallDegradedReason
from ..models.installation_state import check_installation_state
from ..models.installation_state import InstallationState
from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="InstallPartialEvidence")



@_attrs_define
class InstallPartialEvidence:
    """ Why one recipe installation group on a Spark is not complete.

        Attributes:
            affected_ranks (list[int]):
            expected_rank_count (int):
            group_state (InstallationState): The condition of a recipe installation across its Sparks.
            installation_id (str):
            present_ranks (list[int]):
            rank (int):
            rank_state (InstallationState): The condition of a recipe installation across its Sparks.
            reason (InstallDegradedReason): Why an installation is shown partial in the fleet projection.
            recipe_id (str):
            recipe_revision_id (str):
            title (str):
            installed_bytes (int | None | Unset):
            required_bytes (int | None | Unset):
     """

    affected_ranks: list[int]
    expected_rank_count: int
    group_state: InstallationState
    installation_id: str
    present_ranks: list[int]
    rank: int
    rank_state: InstallationState
    reason: InstallDegradedReason
    recipe_id: str
    recipe_revision_id: str
    title: str
    installed_bytes: int | None | Unset = UNSET
    required_bytes: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        affected_ranks = self.affected_ranks



        expected_rank_count = self.expected_rank_count

        group_state: str = self.group_state

        installation_id = self.installation_id

        present_ranks = self.present_ranks



        rank = self.rank

        rank_state: str = self.rank_state

        reason: str = self.reason

        recipe_id = self.recipe_id

        recipe_revision_id = self.recipe_revision_id

        title = self.title

        installed_bytes: int | None | Unset
        if isinstance(self.installed_bytes, Unset):
            installed_bytes = UNSET
        else:
            installed_bytes = self.installed_bytes

        required_bytes: int | None | Unset
        if isinstance(self.required_bytes, Unset):
            required_bytes = UNSET
        else:
            required_bytes = self.required_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "affected_ranks": affected_ranks,
            "expected_rank_count": expected_rank_count,
            "group_state": group_state,
            "installation_id": installation_id,
            "present_ranks": present_ranks,
            "rank": rank,
            "rank_state": rank_state,
            "reason": reason,
            "recipe_id": recipe_id,
            "recipe_revision_id": recipe_revision_id,
            "title": title,
        })
        if installed_bytes is not UNSET:
            field_dict["installed_bytes"] = installed_bytes
        if required_bytes is not UNSET:
            field_dict["required_bytes"] = required_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        affected_ranks = cast(list[int], d.pop("affected_ranks"))


        expected_rank_count = d.pop("expected_rank_count")

        group_state = check_installation_state(d.pop("group_state"))




        installation_id = d.pop("installation_id")

        present_ranks = cast(list[int], d.pop("present_ranks"))


        rank = d.pop("rank")

        rank_state = check_installation_state(d.pop("rank_state"))




        reason = check_install_degraded_reason(d.pop("reason"))




        recipe_id = d.pop("recipe_id")

        recipe_revision_id = d.pop("recipe_revision_id")

        title = d.pop("title")

        def _parse_installed_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        installed_bytes = _parse_installed_bytes(d.pop("installed_bytes", UNSET))


        def _parse_required_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        required_bytes = _parse_required_bytes(d.pop("required_bytes", UNSET))


        install_partial_evidence = cls(
            affected_ranks=affected_ranks,
            expected_rank_count=expected_rank_count,
            group_state=group_state,
            installation_id=installation_id,
            present_ranks=present_ranks,
            rank=rank,
            rank_state=rank_state,
            reason=reason,
            recipe_id=recipe_id,
            recipe_revision_id=recipe_revision_id,
            title=title,
            installed_bytes=installed_bytes,
            required_bytes=required_bytes,
        )

        return install_partial_evidence
