from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="RunSwitchRankReceipt")



@_attrs_define
class RunSwitchRankReceipt:
    """
        Attributes:
            node_id (str):
            rank (int):
            role (str):
            state (str):
            fresh (Union[None, Unset, bool]):
     """

    node_id: str
    rank: int
    role: str
    state: str
    fresh: Union[None, Unset, bool] = UNSET





    def to_dict(self) -> dict[str, Any]:
        node_id = self.node_id

        rank = self.rank

        role = self.role

        state = self.state

        fresh: Union[None, Unset, bool]
        if isinstance(self.fresh, Unset):
            fresh = UNSET
        else:
            fresh = self.fresh


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_id": node_id,
            "rank": rank,
            "role": role,
            "state": state,
        })
        if fresh is not UNSET:
            field_dict["fresh"] = fresh

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        node_id = d.pop("node_id")

        rank = d.pop("rank")

        role = d.pop("role")

        state = d.pop("state")

        def _parse_fresh(data: object) -> Union[None, Unset, bool]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, bool], data)

        fresh = _parse_fresh(d.pop("fresh", UNSET))


        run_switch_rank_receipt = cls(
            node_id=node_id,
            rank=rank,
            role=role,
            state=state,
            fresh=fresh,
        )

        return run_switch_rank_receipt
