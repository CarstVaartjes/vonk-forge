from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.agent_upgrade_identity_response import AgentUpgradeIdentityResponse





T = TypeVar("T", bound="AgentUpgradeTargetDiagnosticsResponse")



@_attrs_define
class AgentUpgradeTargetDiagnosticsResponse:
    """
        Attributes:
            attempts (int):
            node_id (str):
            observed_identity (AgentUpgradeIdentityResponse):
            retry_queued (bool):
            state (str):
            target_proven (bool):
            raw_reason (Union[None, Unset, str]):
            retry_not_before (Union[None, Unset, str]):
     """

    attempts: int
    node_id: str
    observed_identity: 'AgentUpgradeIdentityResponse'
    retry_queued: bool
    state: str
    target_proven: bool
    raw_reason: Union[None, Unset, str] = UNSET
    retry_not_before: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.agent_upgrade_identity_response import AgentUpgradeIdentityResponse
        attempts = self.attempts

        node_id = self.node_id

        observed_identity = self.observed_identity.to_dict()

        retry_queued = self.retry_queued

        state = self.state

        target_proven = self.target_proven

        raw_reason: Union[None, Unset, str]
        if isinstance(self.raw_reason, Unset):
            raw_reason = UNSET
        else:
            raw_reason = self.raw_reason

        retry_not_before: Union[None, Unset, str]
        if isinstance(self.retry_not_before, Unset):
            retry_not_before = UNSET
        else:
            retry_not_before = self.retry_not_before


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "attempts": attempts,
            "node_id": node_id,
            "observed_identity": observed_identity,
            "retry_queued": retry_queued,
            "state": state,
            "target_proven": target_proven,
        })
        if raw_reason is not UNSET:
            field_dict["raw_reason"] = raw_reason
        if retry_not_before is not UNSET:
            field_dict["retry_not_before"] = retry_not_before

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_upgrade_identity_response import AgentUpgradeIdentityResponse
        d = dict(src_dict)
        attempts = d.pop("attempts")

        node_id = d.pop("node_id")

        observed_identity = AgentUpgradeIdentityResponse.from_dict(d.pop("observed_identity"))




        retry_queued = d.pop("retry_queued")

        state = d.pop("state")

        target_proven = d.pop("target_proven")

        def _parse_raw_reason(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        raw_reason = _parse_raw_reason(d.pop("raw_reason", UNSET))


        def _parse_retry_not_before(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        retry_not_before = _parse_retry_not_before(d.pop("retry_not_before", UNSET))


        agent_upgrade_target_diagnostics_response = cls(
            attempts=attempts,
            node_id=node_id,
            observed_identity=observed_identity,
            retry_queued=retry_queued,
            state=state,
            target_proven=target_proven,
            raw_reason=raw_reason,
            retry_not_before=retry_not_before,
        )

        return agent_upgrade_target_diagnostics_response
