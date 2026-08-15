from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.node_connection_agent_state import check_node_connection_agent_state
from ..models.node_connection_agent_state import NodeConnectionAgentState
from ..models.node_connection_certificate_state import check_node_connection_certificate_state
from ..models.node_connection_certificate_state import NodeConnectionCertificateState
from ..models.node_connection_offline_reason_type_0 import check_node_connection_offline_reason_type_0
from ..models.node_connection_offline_reason_type_0 import NodeConnectionOfflineReasonType0
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
            agent_state (NodeConnectionAgentState):
            certificate_state (NodeConnectionCertificateState):
            last_seen_age_seconds (Union[None, float]):
            last_seen_at (Union[None, datetime.datetime]):
            offline_reason (Union[NodeConnectionOfflineReasonType0, None]):
            online_state (NodeConnectionOnlineState):
     """

    agent_state: NodeConnectionAgentState
    certificate_state: NodeConnectionCertificateState
    last_seen_age_seconds: Union[None, float]
    last_seen_at: Union[None, datetime.datetime]
    offline_reason: Union[NodeConnectionOfflineReasonType0, None]
    online_state: NodeConnectionOnlineState





    def to_dict(self) -> dict[str, Any]:
        agent_state: str = self.agent_state

        certificate_state: str = self.certificate_state

        last_seen_age_seconds: Union[None, float]
        last_seen_age_seconds = self.last_seen_age_seconds

        last_seen_at: Union[None, str]
        if isinstance(self.last_seen_at, datetime.datetime):
            last_seen_at = self.last_seen_at.isoformat()
        else:
            last_seen_at = self.last_seen_at

        offline_reason: Union[None, str]
        if isinstance(self.offline_reason, str):
            offline_reason = self.offline_reason
        else:
            offline_reason = self.offline_reason

        online_state: str = self.online_state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "agent_state": agent_state,
            "certificate_state": certificate_state,
            "last_seen_age_seconds": last_seen_age_seconds,
            "last_seen_at": last_seen_at,
            "offline_reason": offline_reason,
            "online_state": online_state,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        agent_state = check_node_connection_agent_state(d.pop("agent_state"))




        certificate_state = check_node_connection_certificate_state(d.pop("certificate_state"))




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


        def _parse_offline_reason(data: object) -> Union[NodeConnectionOfflineReasonType0, None]:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                offline_reason_type_0 = check_node_connection_offline_reason_type_0(data)



                return offline_reason_type_0
            except: # noqa: E722
                pass
            return cast(Union[NodeConnectionOfflineReasonType0, None], data)

        offline_reason = _parse_offline_reason(d.pop("offline_reason"))


        online_state = check_node_connection_online_state(d.pop("online_state"))




        node_connection = cls(
            agent_state=agent_state,
            certificate_state=certificate_state,
            last_seen_age_seconds=last_seen_age_seconds,
            last_seen_at=last_seen_at,
            offline_reason=offline_reason,
            online_state=online_state,
        )

        return node_connection
