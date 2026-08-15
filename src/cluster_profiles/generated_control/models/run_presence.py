from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

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
from typing import cast, Union
from typing import Union






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
            degraded_reason (Union[None, RunPresenceDegradedReasonType0, Unset]):
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
    degraded_reason: Union[None, RunPresenceDegradedReasonType0, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
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

        degraded_reason: Union[None, Unset, str]
        if isinstance(self.degraded_reason, Unset):
            degraded_reason = UNSET
        elif isinstance(self.degraded_reason, str):
            degraded_reason = self.degraded_reason
        else:
            degraded_reason = self.degraded_reason


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

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
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

        def _parse_degraded_reason(data: object) -> Union[None, RunPresenceDegradedReasonType0, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                degraded_reason_type_0 = check_run_presence_degraded_reason_type_0(data)



                return degraded_reason_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, RunPresenceDegradedReasonType0, Unset], data)

        degraded_reason = _parse_degraded_reason(d.pop("degraded_reason", UNSET))


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
        )

        return run_presence
