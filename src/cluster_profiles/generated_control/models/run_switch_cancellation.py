from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from dateutil.parser import isoparse
from typing import cast
import datetime






T = TypeVar("T", bound="RunSwitchCancellation")



@_attrs_define
class RunSwitchCancellation:
    """
        Attributes:
            actor (str):
            reason (str):
            request_key (str):
            requested_at (datetime.datetime):
     """

    actor: str
    reason: str
    request_key: str
    requested_at: datetime.datetime





    def to_dict(self) -> dict[str, Any]:
        actor = self.actor

        reason = self.reason

        request_key = self.request_key

        requested_at = self.requested_at.isoformat()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "actor": actor,
            "reason": reason,
            "request_key": request_key,
            "requested_at": requested_at,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        actor = d.pop("actor")

        reason = d.pop("reason")

        request_key = d.pop("request_key")

        requested_at = isoparse(d.pop("requested_at"))




        run_switch_cancellation = cls(
            actor=actor,
            reason=reason,
            request_key=request_key,
            requested_at=requested_at,
        )

        return run_switch_cancellation
