from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.route_state import check_route_state
from ..models.route_state import RouteState
from ..models.run_state import check_run_state
from ..models.run_state import RunState
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="UnavailableRunPresence")



@_attrs_define
class UnavailableRunPresence:
    """
        Attributes:
            healthy (None):
            projection_issue (str):
            run_id (str):
            alias (None | str | Unset):
            degraded_reason (None | Unset):
            expected_rank_count (int | None | Unset):
            group_state (Literal['unavailable'] | Unset):  Default: 'unavailable'.
            installation_id (None | str | Unset):
            member_node_ids (list[str] | None | Unset):
            option_choices (None | Unset):
            present_ranks (list[int] | None | Unset):
            rank (int | None | Unset):
            rank_age_seconds (None | Unset):
            rank_fresh (None | Unset):
            rank_state (None | RunState | Unset):
            recipe_id (None | str | Unset):
            recipe_revision_id (None | str | Unset):
            recipe_update (None | Unset):
            role (None | str | Unset):
            route_reason (None | Unset):
            route_state (None | RouteState | Unset):
            run_state (None | RunState | Unset):
            title (None | str | Unset):
     """

    healthy: None
    projection_issue: str
    run_id: str
    alias: None | str | Unset = UNSET
    degraded_reason: None | Unset = UNSET
    expected_rank_count: int | None | Unset = UNSET
    group_state: Literal['unavailable'] | Unset = 'unavailable'
    installation_id: None | str | Unset = UNSET
    member_node_ids: list[str] | None | Unset = UNSET
    option_choices: None | Unset = UNSET
    present_ranks: list[int] | None | Unset = UNSET
    rank: int | None | Unset = UNSET
    rank_age_seconds: None | Unset = UNSET
    rank_fresh: None | Unset = UNSET
    rank_state: None | RunState | Unset = UNSET
    recipe_id: None | str | Unset = UNSET
    recipe_revision_id: None | str | Unset = UNSET
    recipe_update: None | Unset = UNSET
    role: None | str | Unset = UNSET
    route_reason: None | Unset = UNSET
    route_state: None | RouteState | Unset = UNSET
    run_state: None | RunState | Unset = UNSET
    title: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        healthy = self.healthy

        projection_issue = self.projection_issue

        run_id = self.run_id

        alias: None | str | Unset
        if isinstance(self.alias, Unset):
            alias = UNSET
        else:
            alias = self.alias

        degraded_reason = self.degraded_reason

        expected_rank_count: int | None | Unset
        if isinstance(self.expected_rank_count, Unset):
            expected_rank_count = UNSET
        else:
            expected_rank_count = self.expected_rank_count

        group_state = self.group_state

        installation_id: None | str | Unset
        if isinstance(self.installation_id, Unset):
            installation_id = UNSET
        else:
            installation_id = self.installation_id

        member_node_ids: list[str] | None | Unset
        if isinstance(self.member_node_ids, Unset):
            member_node_ids = UNSET
        elif isinstance(self.member_node_ids, list):
            member_node_ids = self.member_node_ids


        else:
            member_node_ids = self.member_node_ids

        option_choices = self.option_choices

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

        rank_age_seconds = self.rank_age_seconds

        rank_fresh = self.rank_fresh

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

        recipe_update = self.recipe_update

        role: None | str | Unset
        if isinstance(self.role, Unset):
            role = UNSET
        else:
            role = self.role

        route_reason = self.route_reason

        route_state: None | str | Unset
        if isinstance(self.route_state, Unset):
            route_state = UNSET
        elif isinstance(self.route_state, str):
            route_state = self.route_state
        else:
            route_state = self.route_state

        run_state: None | str | Unset
        if isinstance(self.run_state, Unset):
            run_state = UNSET
        elif isinstance(self.run_state, str):
            run_state = self.run_state
        else:
            run_state = self.run_state

        title: None | str | Unset
        if isinstance(self.title, Unset):
            title = UNSET
        else:
            title = self.title


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "healthy": healthy,
            "projection_issue": projection_issue,
            "run_id": run_id,
        })
        if alias is not UNSET:
            field_dict["alias"] = alias
        if degraded_reason is not UNSET:
            field_dict["degraded_reason"] = degraded_reason
        if expected_rank_count is not UNSET:
            field_dict["expected_rank_count"] = expected_rank_count
        if group_state is not UNSET:
            field_dict["group_state"] = group_state
        if installation_id is not UNSET:
            field_dict["installation_id"] = installation_id
        if member_node_ids is not UNSET:
            field_dict["member_node_ids"] = member_node_ids
        if option_choices is not UNSET:
            field_dict["option_choices"] = option_choices
        if present_ranks is not UNSET:
            field_dict["present_ranks"] = present_ranks
        if rank is not UNSET:
            field_dict["rank"] = rank
        if rank_age_seconds is not UNSET:
            field_dict["rank_age_seconds"] = rank_age_seconds
        if rank_fresh is not UNSET:
            field_dict["rank_fresh"] = rank_fresh
        if rank_state is not UNSET:
            field_dict["rank_state"] = rank_state
        if recipe_id is not UNSET:
            field_dict["recipe_id"] = recipe_id
        if recipe_revision_id is not UNSET:
            field_dict["recipe_revision_id"] = recipe_revision_id
        if recipe_update is not UNSET:
            field_dict["recipe_update"] = recipe_update
        if role is not UNSET:
            field_dict["role"] = role
        if route_reason is not UNSET:
            field_dict["route_reason"] = route_reason
        if route_state is not UNSET:
            field_dict["route_state"] = route_state
        if run_state is not UNSET:
            field_dict["run_state"] = run_state
        if title is not UNSET:
            field_dict["title"] = title

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        healthy = d.pop("healthy")

        projection_issue = d.pop("projection_issue")

        run_id = d.pop("run_id")

        def _parse_alias(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        alias = _parse_alias(d.pop("alias", UNSET))


        degraded_reason = d.pop("degraded_reason", UNSET)

        def _parse_expected_rank_count(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        expected_rank_count = _parse_expected_rank_count(d.pop("expected_rank_count", UNSET))


        group_state = cast(Literal['unavailable'] | Unset , d.pop("group_state", UNSET))
        if group_state != 'unavailable' and not isinstance(group_state, Unset):
            raise ValueError(f"group_state must match const 'unavailable', got '{group_state}'")

        def _parse_installation_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        installation_id = _parse_installation_id(d.pop("installation_id", UNSET))


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


        option_choices = d.pop("option_choices", UNSET)

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


        rank_age_seconds = d.pop("rank_age_seconds", UNSET)

        rank_fresh = d.pop("rank_fresh", UNSET)

        def _parse_rank_state(data: object) -> None | RunState | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                rank_state_type_0 = check_run_state(data)



                return rank_state_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RunState | Unset, data)

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


        recipe_update = d.pop("recipe_update", UNSET)

        def _parse_role(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        role = _parse_role(d.pop("role", UNSET))


        route_reason = d.pop("route_reason", UNSET)

        def _parse_route_state(data: object) -> None | RouteState | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                route_state_type_0 = check_route_state(data)



                return route_state_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RouteState | Unset, data)

        route_state = _parse_route_state(d.pop("route_state", UNSET))


        def _parse_run_state(data: object) -> None | RunState | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                run_state_type_0 = check_run_state(data)



                return run_state_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RunState | Unset, data)

        run_state = _parse_run_state(d.pop("run_state", UNSET))


        def _parse_title(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        title = _parse_title(d.pop("title", UNSET))


        unavailable_run_presence = cls(
            healthy=healthy,
            projection_issue=projection_issue,
            run_id=run_id,
            alias=alias,
            degraded_reason=degraded_reason,
            expected_rank_count=expected_rank_count,
            group_state=group_state,
            installation_id=installation_id,
            member_node_ids=member_node_ids,
            option_choices=option_choices,
            present_ranks=present_ranks,
            rank=rank,
            rank_age_seconds=rank_age_seconds,
            rank_fresh=rank_fresh,
            rank_state=rank_state,
            recipe_id=recipe_id,
            recipe_revision_id=recipe_revision_id,
            recipe_update=recipe_update,
            role=role,
            route_reason=route_reason,
            route_state=route_state,
            run_state=run_state,
            title=title,
        )

        return unavailable_run_presence
