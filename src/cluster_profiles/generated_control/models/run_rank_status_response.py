from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from dateutil.parser import isoparse
from typing import cast
import datetime






T = TypeVar("T", bound="RunRankStatusResponse")



@_attrs_define
class RunRankStatusResponse:
    """
        Attributes:
            age_seconds (float):
            fresh (bool):
            node_id (str):
            observed_at (datetime.datetime):
            rank (int):
            role (str):
            state (str):
     """

    age_seconds: float
    fresh: bool
    node_id: str
    observed_at: datetime.datetime
    rank: int
    role: str
    state: str





    def to_dict(self) -> dict[str, Any]:
        age_seconds = self.age_seconds

        fresh = self.fresh

        node_id = self.node_id

        observed_at = self.observed_at.isoformat()

        rank = self.rank

        role = self.role

        state = self.state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "age_seconds": age_seconds,
            "fresh": fresh,
            "node_id": node_id,
            "observed_at": observed_at,
            "rank": rank,
            "role": role,
            "state": state,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        age_seconds = d.pop("age_seconds")

        fresh = d.pop("fresh")

        node_id = d.pop("node_id")

        observed_at = isoparse(d.pop("observed_at"))




        rank = d.pop("rank")

        role = d.pop("role")

        state = d.pop("state")

        run_rank_status_response = cls(
            age_seconds=age_seconds,
            fresh=fresh,
            node_id=node_id,
            observed_at=observed_at,
            rank=rank,
            role=role,
            state=state,
        )

        return run_rank_status_response
