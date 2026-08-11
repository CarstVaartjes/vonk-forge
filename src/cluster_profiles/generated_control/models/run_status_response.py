from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.run_rank_status_response import RunRankStatusResponse





T = TypeVar("T", bound="RunStatusResponse")



@_attrs_define
class RunStatusResponse:
    """
        Attributes:
            alias (str):
            healthy (bool):
            id (str):
            ranks (list['RunRankStatusResponse']):
            route_state (str):
            state (str):
     """

    alias: str
    healthy: bool
    id: str
    ranks: list['RunRankStatusResponse']
    route_state: str
    state: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_rank_status_response import RunRankStatusResponse
        alias = self.alias

        healthy = self.healthy

        id = self.id

        ranks = []
        for ranks_item_data in self.ranks:
            ranks_item = ranks_item_data.to_dict()
            ranks.append(ranks_item)



        route_state = self.route_state

        state = self.state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "alias": alias,
            "healthy": healthy,
            "id": id,
            "ranks": ranks,
            "route_state": route_state,
            "state": state,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_rank_status_response import RunRankStatusResponse
        d = dict(src_dict)
        alias = d.pop("alias")

        healthy = d.pop("healthy")

        id = d.pop("id")

        ranks = []
        _ranks = d.pop("ranks")
        for ranks_item_data in (_ranks):
            ranks_item = RunRankStatusResponse.from_dict(ranks_item_data)



            ranks.append(ranks_item)


        route_state = d.pop("route_state")

        state = d.pop("state")

        run_status_response = cls(
            alias=alias,
            healthy=healthy,
            id=id,
            ranks=ranks,
            route_state=route_state,
            state=state,
        )

        return run_status_response
