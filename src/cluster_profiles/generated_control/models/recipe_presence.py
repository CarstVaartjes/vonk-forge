from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.install_degraded_reason import check_install_degraded_reason
from ..models.install_degraded_reason import InstallDegradedReason
from ..models.recipe_presence_group_state import check_recipe_presence_group_state
from ..models.recipe_presence_group_state import RecipePresenceGroupState
from ..models.recipe_presence_rank_state import check_recipe_presence_rank_state
from ..models.recipe_presence_rank_state import RecipePresenceRankState
from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="RecipePresence")



@_attrs_define
class RecipePresence:
    """
        Attributes:
            complete (bool):
            expected_rank_count (int):
            group_state (RecipePresenceGroupState):
            installation_id (str):
            member_node_ids (list[str]):
            present_ranks (list[int]):
            rank (int):
            rank_state (RecipePresenceRankState):
            recipe_id (str):
            recipe_revision_id (str):
            role (str):
            title (str):
            topology_name (str):
            affected_ranks (list[int] | Unset):
            degraded_reason (InstallDegradedReason | None | Unset):
            installed_bytes (int | None | Unset):
            required_bytes (int | None | Unset):
     """

    complete: bool
    expected_rank_count: int
    group_state: RecipePresenceGroupState
    installation_id: str
    member_node_ids: list[str]
    present_ranks: list[int]
    rank: int
    rank_state: RecipePresenceRankState
    recipe_id: str
    recipe_revision_id: str
    role: str
    title: str
    topology_name: str
    affected_ranks: list[int] | Unset = UNSET
    degraded_reason: InstallDegradedReason | None | Unset = UNSET
    installed_bytes: int | None | Unset = UNSET
    required_bytes: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        complete = self.complete

        expected_rank_count = self.expected_rank_count

        group_state: str = self.group_state

        installation_id = self.installation_id

        member_node_ids = self.member_node_ids



        present_ranks = self.present_ranks



        rank = self.rank

        rank_state: str = self.rank_state

        recipe_id = self.recipe_id

        recipe_revision_id = self.recipe_revision_id

        role = self.role

        title = self.title

        topology_name = self.topology_name

        affected_ranks: list[int] | Unset = UNSET
        if not isinstance(self.affected_ranks, Unset):
            affected_ranks = self.affected_ranks



        degraded_reason: None | str | Unset
        if isinstance(self.degraded_reason, Unset):
            degraded_reason = UNSET
        elif isinstance(self.degraded_reason, str):
            degraded_reason = self.degraded_reason
        else:
            degraded_reason = self.degraded_reason

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
            "complete": complete,
            "expected_rank_count": expected_rank_count,
            "group_state": group_state,
            "installation_id": installation_id,
            "member_node_ids": member_node_ids,
            "present_ranks": present_ranks,
            "rank": rank,
            "rank_state": rank_state,
            "recipe_id": recipe_id,
            "recipe_revision_id": recipe_revision_id,
            "role": role,
            "title": title,
            "topology_name": topology_name,
        })
        if affected_ranks is not UNSET:
            field_dict["affected_ranks"] = affected_ranks
        if degraded_reason is not UNSET:
            field_dict["degraded_reason"] = degraded_reason
        if installed_bytes is not UNSET:
            field_dict["installed_bytes"] = installed_bytes
        if required_bytes is not UNSET:
            field_dict["required_bytes"] = required_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        complete = d.pop("complete")

        expected_rank_count = d.pop("expected_rank_count")

        group_state = check_recipe_presence_group_state(d.pop("group_state"))




        installation_id = d.pop("installation_id")

        member_node_ids = cast(list[str], d.pop("member_node_ids"))


        present_ranks = cast(list[int], d.pop("present_ranks"))


        rank = d.pop("rank")

        rank_state = check_recipe_presence_rank_state(d.pop("rank_state"))




        recipe_id = d.pop("recipe_id")

        recipe_revision_id = d.pop("recipe_revision_id")

        role = d.pop("role")

        title = d.pop("title")

        topology_name = d.pop("topology_name")

        affected_ranks = cast(list[int], d.pop("affected_ranks", UNSET))


        def _parse_degraded_reason(data: object) -> InstallDegradedReason | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                degraded_reason_type_0 = check_install_degraded_reason(data)



                return degraded_reason_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(InstallDegradedReason | None | Unset, data)

        degraded_reason = _parse_degraded_reason(d.pop("degraded_reason", UNSET))


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


        recipe_presence = cls(
            complete=complete,
            expected_rank_count=expected_rank_count,
            group_state=group_state,
            installation_id=installation_id,
            member_node_ids=member_node_ids,
            present_ranks=present_ranks,
            rank=rank,
            rank_state=rank_state,
            recipe_id=recipe_id,
            recipe_revision_id=recipe_revision_id,
            role=role,
            title=title,
            topology_name=topology_name,
            affected_ranks=affected_ranks,
            degraded_reason=degraded_reason,
            installed_bytes=installed_bytes,
            required_bytes=required_bytes,
        )

        return recipe_presence
