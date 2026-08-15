from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_presence_degraded_reason_type_0 import check_recipe_presence_degraded_reason_type_0
from ..models.recipe_presence_degraded_reason_type_0 import RecipePresenceDegradedReasonType0
from ..models.recipe_presence_group_state import check_recipe_presence_group_state
from ..models.recipe_presence_group_state import RecipePresenceGroupState
from ..models.recipe_presence_rank_state import check_recipe_presence_rank_state
from ..models.recipe_presence_rank_state import RecipePresenceRankState
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






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
            profile_name (str):
            rank (int):
            rank_state (RecipePresenceRankState):
            recipe_id (str):
            recipe_revision_id (str):
            role (str):
            title (str):
            degraded_reason (Union[None, RecipePresenceDegradedReasonType0, Unset]):
     """

    complete: bool
    expected_rank_count: int
    group_state: RecipePresenceGroupState
    installation_id: str
    member_node_ids: list[str]
    present_ranks: list[int]
    profile_name: str
    rank: int
    rank_state: RecipePresenceRankState
    recipe_id: str
    recipe_revision_id: str
    role: str
    title: str
    degraded_reason: Union[None, RecipePresenceDegradedReasonType0, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        complete = self.complete

        expected_rank_count = self.expected_rank_count

        group_state: str = self.group_state

        installation_id = self.installation_id

        member_node_ids = self.member_node_ids



        present_ranks = self.present_ranks



        profile_name = self.profile_name

        rank = self.rank

        rank_state: str = self.rank_state

        recipe_id = self.recipe_id

        recipe_revision_id = self.recipe_revision_id

        role = self.role

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
            "complete": complete,
            "expected_rank_count": expected_rank_count,
            "group_state": group_state,
            "installation_id": installation_id,
            "member_node_ids": member_node_ids,
            "present_ranks": present_ranks,
            "profile_name": profile_name,
            "rank": rank,
            "rank_state": rank_state,
            "recipe_id": recipe_id,
            "recipe_revision_id": recipe_revision_id,
            "role": role,
            "title": title,
        })
        if degraded_reason is not UNSET:
            field_dict["degraded_reason"] = degraded_reason

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


        profile_name = d.pop("profile_name")

        rank = d.pop("rank")

        rank_state = check_recipe_presence_rank_state(d.pop("rank_state"))




        recipe_id = d.pop("recipe_id")

        recipe_revision_id = d.pop("recipe_revision_id")

        role = d.pop("role")

        title = d.pop("title")

        def _parse_degraded_reason(data: object) -> Union[None, RecipePresenceDegradedReasonType0, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                degraded_reason_type_0 = check_recipe_presence_degraded_reason_type_0(data)



                return degraded_reason_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, RecipePresenceDegradedReasonType0, Unset], data)

        degraded_reason = _parse_degraded_reason(d.pop("degraded_reason", UNSET))


        recipe_presence = cls(
            complete=complete,
            expected_rank_count=expected_rank_count,
            group_state=group_state,
            installation_id=installation_id,
            member_node_ids=member_node_ids,
            present_ranks=present_ranks,
            profile_name=profile_name,
            rank=rank,
            rank_state=rank_state,
            recipe_id=recipe_id,
            recipe_revision_id=recipe_revision_id,
            role=role,
            title=title,
            degraded_reason=degraded_reason,
        )

        return recipe_presence
