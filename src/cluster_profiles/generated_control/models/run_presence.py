from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_presence_degraded_reason_type_0 import check_run_presence_degraded_reason_type_0
from ..models.run_presence_degraded_reason_type_0 import RunPresenceDegradedReasonType0
from ..models.run_presence_group_state import check_run_presence_group_state
from ..models.run_presence_group_state import RunPresenceGroupState
from ..models.run_presence_rank_state import check_run_presence_rank_state
from ..models.run_presence_rank_state import RunPresenceRankState
from ..models.run_presence_route_state import check_run_presence_route_state
from ..models.run_presence_route_state import RunPresenceRouteState
from ..models.run_presence_run_state import check_run_presence_run_state
from ..models.run_presence_run_state import RunPresenceRunState
from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_update_notice import RecipeUpdateNotice
  from ..models.run_presence_option_choices import RunPresenceOptionChoices





T = TypeVar("T", bound="RunPresence")



@_attrs_define
class RunPresence:
    """
        Attributes:
            alias (str):
            expected_rank_count (int):
            group_state (RunPresenceGroupState):
            healthy (bool):
            installation_id (str):
            member_node_ids (list[str]):
            present_ranks (list[int]):
            rank (int):
            rank_age_seconds (float):
            rank_fresh (bool):
            rank_state (RunPresenceRankState):
            recipe_id (str):
            recipe_revision_id (str):
            role (str):
            route_state (RunPresenceRouteState):
            run_id (str):
            run_state (RunPresenceRunState):
            title (str):
            degraded_reason (None | RunPresenceDegradedReasonType0 | Unset):
            option_choices (RunPresenceOptionChoices | Unset):
            recipe_update (None | RecipeUpdateNotice | Unset):
            route_reason (None | str | Unset):
     """

    alias: str
    expected_rank_count: int
    group_state: RunPresenceGroupState
    healthy: bool
    installation_id: str
    member_node_ids: list[str]
    present_ranks: list[int]
    rank: int
    rank_age_seconds: float
    rank_fresh: bool
    rank_state: RunPresenceRankState
    recipe_id: str
    recipe_revision_id: str
    role: str
    route_state: RunPresenceRouteState
    run_id: str
    run_state: RunPresenceRunState
    title: str
    degraded_reason: None | RunPresenceDegradedReasonType0 | Unset = UNSET
    option_choices: RunPresenceOptionChoices | Unset = UNSET
    recipe_update: None | RecipeUpdateNotice | Unset = UNSET
    route_reason: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_update_notice import RecipeUpdateNotice # noqa: PLC0415
        from ..models.run_presence_option_choices import RunPresenceOptionChoices # noqa: PLC0415
        alias = self.alias

        expected_rank_count = self.expected_rank_count

        group_state: str = self.group_state

        healthy = self.healthy

        installation_id = self.installation_id

        member_node_ids = self.member_node_ids



        present_ranks = self.present_ranks



        rank = self.rank

        rank_age_seconds = self.rank_age_seconds

        rank_fresh = self.rank_fresh

        rank_state: str = self.rank_state

        recipe_id = self.recipe_id

        recipe_revision_id = self.recipe_revision_id

        role = self.role

        route_state: str = self.route_state

        run_id = self.run_id

        run_state: str = self.run_state

        title = self.title

        degraded_reason: None | str | Unset
        if isinstance(self.degraded_reason, Unset):
            degraded_reason = UNSET
        elif isinstance(self.degraded_reason, str):
            degraded_reason = self.degraded_reason
        else:
            degraded_reason = self.degraded_reason

        option_choices: dict[str, Any] | Unset = UNSET
        if not isinstance(self.option_choices, Unset):
            option_choices = self.option_choices.to_dict()

        recipe_update: dict[str, Any] | None | Unset
        if isinstance(self.recipe_update, Unset):
            recipe_update = UNSET
        elif isinstance(self.recipe_update, RecipeUpdateNotice):
            recipe_update = self.recipe_update.to_dict()
        else:
            recipe_update = self.recipe_update

        route_reason: None | str | Unset
        if isinstance(self.route_reason, Unset):
            route_reason = UNSET
        else:
            route_reason = self.route_reason


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "alias": alias,
            "expected_rank_count": expected_rank_count,
            "group_state": group_state,
            "healthy": healthy,
            "installation_id": installation_id,
            "member_node_ids": member_node_ids,
            "present_ranks": present_ranks,
            "rank": rank,
            "rank_age_seconds": rank_age_seconds,
            "rank_fresh": rank_fresh,
            "rank_state": rank_state,
            "recipe_id": recipe_id,
            "recipe_revision_id": recipe_revision_id,
            "role": role,
            "route_state": route_state,
            "run_id": run_id,
            "run_state": run_state,
            "title": title,
        })
        if degraded_reason is not UNSET:
            field_dict["degraded_reason"] = degraded_reason
        if option_choices is not UNSET:
            field_dict["option_choices"] = option_choices
        if recipe_update is not UNSET:
            field_dict["recipe_update"] = recipe_update
        if route_reason is not UNSET:
            field_dict["route_reason"] = route_reason

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_update_notice import RecipeUpdateNotice # noqa: PLC0415
        from ..models.run_presence_option_choices import RunPresenceOptionChoices # noqa: PLC0415
        d = dict(src_dict)
        alias = d.pop("alias")

        expected_rank_count = d.pop("expected_rank_count")

        group_state = check_run_presence_group_state(d.pop("group_state"))




        healthy = d.pop("healthy")

        installation_id = d.pop("installation_id")

        member_node_ids = cast(list[str], d.pop("member_node_ids"))


        present_ranks = cast(list[int], d.pop("present_ranks"))


        rank = d.pop("rank")

        rank_age_seconds = d.pop("rank_age_seconds")

        rank_fresh = d.pop("rank_fresh")

        rank_state = check_run_presence_rank_state(d.pop("rank_state"))




        recipe_id = d.pop("recipe_id")

        recipe_revision_id = d.pop("recipe_revision_id")

        role = d.pop("role")

        route_state = check_run_presence_route_state(d.pop("route_state"))




        run_id = d.pop("run_id")

        run_state = check_run_presence_run_state(d.pop("run_state"))




        title = d.pop("title")

        def _parse_degraded_reason(data: object) -> None | RunPresenceDegradedReasonType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                degraded_reason_type_0 = check_run_presence_degraded_reason_type_0(data)



                return degraded_reason_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RunPresenceDegradedReasonType0 | Unset, data)

        degraded_reason = _parse_degraded_reason(d.pop("degraded_reason", UNSET))


        _option_choices = d.pop("option_choices", UNSET)
        option_choices: RunPresenceOptionChoices | Unset
        if isinstance(_option_choices,  Unset):
            option_choices = UNSET
        else:
            option_choices = RunPresenceOptionChoices.from_dict(_option_choices)




        def _parse_recipe_update(data: object) -> None | RecipeUpdateNotice | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                recipe_update_type_0 = RecipeUpdateNotice.from_dict(data)



                return recipe_update_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RecipeUpdateNotice | Unset, data)

        recipe_update = _parse_recipe_update(d.pop("recipe_update", UNSET))


        def _parse_route_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        route_reason = _parse_route_reason(d.pop("route_reason", UNSET))


        run_presence = cls(
            alias=alias,
            expected_rank_count=expected_rank_count,
            group_state=group_state,
            healthy=healthy,
            installation_id=installation_id,
            member_node_ids=member_node_ids,
            present_ranks=present_ranks,
            rank=rank,
            rank_age_seconds=rank_age_seconds,
            rank_fresh=rank_fresh,
            rank_state=rank_state,
            recipe_id=recipe_id,
            recipe_revision_id=recipe_revision_id,
            role=role,
            route_state=route_state,
            run_id=run_id,
            run_state=run_state,
            title=title,
            degraded_reason=degraded_reason,
            option_choices=option_choices,
            recipe_update=recipe_update,
            route_reason=route_reason,
        )

        return run_presence
