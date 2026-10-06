from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.installation_state import check_installation_state
from ..models.installation_state import InstallationState
from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="UnavailableRecipePresence")



@_attrs_define
class UnavailableRecipePresence:
    """ Known membership whose stored group evidence cannot be projected.

        Attributes:
            complete (None):
            installation_id (str):
            projection_issue (str):
            affected_ranks (None | Unset):
            degraded_reason (None | Unset):
            expected_rank_count (int | None | Unset):
            group_state (InstallationState | None | Unset):
            installed_bytes (int | None | Unset):
            member_node_ids (list[str] | None | Unset):
            present_ranks (list[int] | None | Unset):
            rank (int | None | Unset):
            rank_state (InstallationState | None | Unset):
            recipe_id (None | str | Unset):
            recipe_revision_id (None | str | Unset):
            required_bytes (None | Unset):
            role (None | str | Unset):
            title (None | str | Unset):
            topology_name (None | str | Unset):
     """

    complete: None
    installation_id: str
    projection_issue: str
    affected_ranks: None | Unset = UNSET
    degraded_reason: None | Unset = UNSET
    expected_rank_count: int | None | Unset = UNSET
    group_state: InstallationState | None | Unset = UNSET
    installed_bytes: int | None | Unset = UNSET
    member_node_ids: list[str] | None | Unset = UNSET
    present_ranks: list[int] | None | Unset = UNSET
    rank: int | None | Unset = UNSET
    rank_state: InstallationState | None | Unset = UNSET
    recipe_id: None | str | Unset = UNSET
    recipe_revision_id: None | str | Unset = UNSET
    required_bytes: None | Unset = UNSET
    role: None | str | Unset = UNSET
    title: None | str | Unset = UNSET
    topology_name: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        complete = self.complete

        installation_id = self.installation_id

        projection_issue = self.projection_issue

        affected_ranks = self.affected_ranks

        degraded_reason = self.degraded_reason

        expected_rank_count: int | None | Unset
        if isinstance(self.expected_rank_count, Unset):
            expected_rank_count = UNSET
        else:
            expected_rank_count = self.expected_rank_count

        group_state: None | str | Unset
        if isinstance(self.group_state, Unset):
            group_state = UNSET
        elif isinstance(self.group_state, str):
            group_state = self.group_state
        else:
            group_state = self.group_state

        installed_bytes: int | None | Unset
        if isinstance(self.installed_bytes, Unset):
            installed_bytes = UNSET
        else:
            installed_bytes = self.installed_bytes

        member_node_ids: list[str] | None | Unset
        if isinstance(self.member_node_ids, Unset):
            member_node_ids = UNSET
        elif isinstance(self.member_node_ids, list):
            member_node_ids = self.member_node_ids


        else:
            member_node_ids = self.member_node_ids

        present_ranks: list[int] | None | Unset
        if isinstance(self.present_ranks, Unset):
            present_ranks = UNSET
        elif isinstance(self.present_ranks, list):
            present_ranks = self.present_ranks


        else:
            present_ranks = self.present_ranks

        rank: int | None | Unset
        if isinstance(self.rank, Unset):
            rank = UNSET
        else:
            rank = self.rank

        rank_state: None | str | Unset
        if isinstance(self.rank_state, Unset):
            rank_state = UNSET
        elif isinstance(self.rank_state, str):
            rank_state = self.rank_state
        else:
            rank_state = self.rank_state

        recipe_id: None | str | Unset
        if isinstance(self.recipe_id, Unset):
            recipe_id = UNSET
        else:
            recipe_id = self.recipe_id

        recipe_revision_id: None | str | Unset
        if isinstance(self.recipe_revision_id, Unset):
            recipe_revision_id = UNSET
        else:
            recipe_revision_id = self.recipe_revision_id

        required_bytes = self.required_bytes

        role: None | str | Unset
        if isinstance(self.role, Unset):
            role = UNSET
        else:
            role = self.role

        title: None | str | Unset
        if isinstance(self.title, Unset):
            title = UNSET
        else:
            title = self.title

        topology_name: None | str | Unset
        if isinstance(self.topology_name, Unset):
            topology_name = UNSET
        else:
            topology_name = self.topology_name


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "complete": complete,
            "installation_id": installation_id,
            "projection_issue": projection_issue,
        })
        if affected_ranks is not UNSET:
            field_dict["affected_ranks"] = affected_ranks
        if degraded_reason is not UNSET:
            field_dict["degraded_reason"] = degraded_reason
        if expected_rank_count is not UNSET:
            field_dict["expected_rank_count"] = expected_rank_count
        if group_state is not UNSET:
            field_dict["group_state"] = group_state
        if installed_bytes is not UNSET:
            field_dict["installed_bytes"] = installed_bytes
        if member_node_ids is not UNSET:
            field_dict["member_node_ids"] = member_node_ids
        if present_ranks is not UNSET:
            field_dict["present_ranks"] = present_ranks
        if rank is not UNSET:
            field_dict["rank"] = rank
        if rank_state is not UNSET:
            field_dict["rank_state"] = rank_state
        if recipe_id is not UNSET:
            field_dict["recipe_id"] = recipe_id
        if recipe_revision_id is not UNSET:
            field_dict["recipe_revision_id"] = recipe_revision_id
        if required_bytes is not UNSET:
            field_dict["required_bytes"] = required_bytes
        if role is not UNSET:
            field_dict["role"] = role
        if title is not UNSET:
            field_dict["title"] = title
        if topology_name is not UNSET:
            field_dict["topology_name"] = topology_name

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        complete = d.pop("complete")

        installation_id = d.pop("installation_id")

        projection_issue = d.pop("projection_issue")

        affected_ranks = d.pop("affected_ranks", UNSET)

        degraded_reason = d.pop("degraded_reason", UNSET)

        def _parse_expected_rank_count(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        expected_rank_count = _parse_expected_rank_count(d.pop("expected_rank_count", UNSET))


        def _parse_group_state(data: object) -> InstallationState | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                group_state_type_0 = check_installation_state(data)



                return group_state_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(InstallationState | None | Unset, data)

        group_state = _parse_group_state(d.pop("group_state", UNSET))


        def _parse_installed_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        installed_bytes = _parse_installed_bytes(d.pop("installed_bytes", UNSET))


        def _parse_member_node_ids(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                member_node_ids_type_0 = cast(list[str], data)

                return member_node_ids_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        member_node_ids = _parse_member_node_ids(d.pop("member_node_ids", UNSET))


        def _parse_present_ranks(data: object) -> list[int] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                present_ranks_type_0 = cast(list[int], data)

                return present_ranks_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[int] | None | Unset, data)

        present_ranks = _parse_present_ranks(d.pop("present_ranks", UNSET))


        def _parse_rank(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        rank = _parse_rank(d.pop("rank", UNSET))


        def _parse_rank_state(data: object) -> InstallationState | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                rank_state_type_0 = check_installation_state(data)



                return rank_state_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(InstallationState | None | Unset, data)

        rank_state = _parse_rank_state(d.pop("rank_state", UNSET))


        def _parse_recipe_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        recipe_id = _parse_recipe_id(d.pop("recipe_id", UNSET))


        def _parse_recipe_revision_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        recipe_revision_id = _parse_recipe_revision_id(d.pop("recipe_revision_id", UNSET))


        required_bytes = d.pop("required_bytes", UNSET)

        def _parse_role(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        role = _parse_role(d.pop("role", UNSET))


        def _parse_title(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        title = _parse_title(d.pop("title", UNSET))


        def _parse_topology_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        topology_name = _parse_topology_name(d.pop("topology_name", UNSET))


        unavailable_recipe_presence = cls(
            complete=complete,
            installation_id=installation_id,
            projection_issue=projection_issue,
            affected_ranks=affected_ranks,
            degraded_reason=degraded_reason,
            expected_rank_count=expected_rank_count,
            group_state=group_state,
            installed_bytes=installed_bytes,
            member_node_ids=member_node_ids,
            present_ranks=present_ranks,
            rank=rank,
            rank_state=rank_state,
            recipe_id=recipe_id,
            recipe_revision_id=recipe_revision_id,
            required_bytes=required_bytes,
            role=role,
            title=title,
            topology_name=topology_name,
        )

        return unavailable_recipe_presence
