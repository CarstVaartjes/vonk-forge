from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.capability_reason import CapabilityReason
from ..models.capability_reason import check_capability_reason
from ..models.controller_capability import check_controller_capability
from ..models.controller_capability import ControllerCapability
from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="CapabilityUnavailableReply")



@_attrs_define
class CapabilityUnavailableReply:
    """
        Attributes:
            capability (ControllerCapability):
            reason (CapabilityReason):
            retryable (bool | Unset):  Default: True.
     """

    capability: ControllerCapability
    reason: CapabilityReason
    retryable: bool | Unset = True





    def to_dict(self) -> dict[str, Any]:
        capability: str = self.capability

        reason: str = self.reason

        retryable = self.retryable


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "capability": capability,
            "reason": reason,
        })
        if retryable is not UNSET:
            field_dict["retryable"] = retryable

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        capability = check_controller_capability(d.pop("capability"))




        reason = check_capability_reason(d.pop("reason"))




        retryable = d.pop("retryable", UNSET)

        capability_unavailable_reply = cls(
            capability=capability,
            reason=reason,
            retryable=retryable,
        )

        return capability_unavailable_reply
