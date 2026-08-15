from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.node_connection_online_state import check_node_connection_online_state
from ..models.node_connection_online_state import NodeConnectionOnlineState
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
import datetime






T = TypeVar("T", bound="NodeConnection")



@_attrs_define
class NodeConnection:
    """
        Attributes:
            agent_state (str):
            last_seen_age_seconds (Union[None, float]):
            last_seen_at (Union[None, datetime.datetime]):
            online_state (NodeConnectionOnlineState):
     """

    agent_state: str
    last_seen_age_seconds: Union[None, float]
    last_seen_at: Union[None, datetime.datetime]
    online_state: NodeConnectionOnlineState





    def to_dict(self) -> dict[str, Any]:
        agent_state = self.agent_state

        last_seen_age_seconds: Union[None, float]
        last_seen_age_seconds = self.last_seen_age_seconds

        last_seen_at: Union[None, str]
        if isinstance(self.last_seen_at, datetime.datetime):
            last_seen_at = self.last_seen_at.isoformat()
        else:
            last_seen_at = self.last_seen_at

        online_state: str = self.online_state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "agent_state": agent_state,
            "last_seen_age_seconds": last_seen_age_seconds,
            "last_seen_at": last_seen_at,
            "online_state": online_state,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        agent_state = d.pop("agent_state")

        def _parse_last_seen_age_seconds(data: object) -> Union[None, float]:
            if data is None:
                return data
            return cast(Union[None, float], data)

        last_seen_age_seconds = _parse_last_seen_age_seconds(d.pop("last_seen_age_seconds"))


        def _parse_last_seen_at(data: object) -> Union[None, datetime.datetime]:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                last_seen_at_type_0 = isoparse(data)



                return last_seen_at_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, datetime.datetime], data)

        last_seen_at = _parse_last_seen_at(d.pop("last_seen_at"))


        online_state = check_node_connection_online_state(d.pop("online_state"))




        node_connection = cls(
            agent_state=agent_state,
            last_seen_age_seconds=last_seen_age_seconds,
            last_seen_at=last_seen_at,
            online_state=online_state,
        )

        return node_connection
