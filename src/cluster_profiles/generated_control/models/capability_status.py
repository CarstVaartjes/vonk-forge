from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.capability_availability import CapabilityAvailability
from ..models.capability_availability import check_capability_availability
from ..models.capability_reason import CapabilityReason
from ..models.capability_reason import check_capability_reason
from ..models.controller_capability import check_controller_capability
from ..models.controller_capability import ControllerCapability
from ..types import UNSET, Unset
from typing import cast
import datetime






T = TypeVar("T", bound="CapabilityStatus")



@_attrs_define
class CapabilityStatus:
    """
        Attributes:
            availability (CapabilityAvailability):
            capability (ControllerCapability):
            next_attempt_at (datetime.datetime | None | Unset):
            reason (CapabilityReason | None | Unset):
     """

    availability: CapabilityAvailability
    capability: ControllerCapability
    next_attempt_at: datetime.datetime | None | Unset = UNSET
    reason: CapabilityReason | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        availability: str = self.availability

        capability: str = self.capability

        next_attempt_at: None | str | Unset
        if isinstance(self.next_attempt_at, Unset):
            next_attempt_at = UNSET
        elif isinstance(self.next_attempt_at, datetime.datetime):
            next_attempt_at = self.next_attempt_at.isoformat()
        else:
            next_attempt_at = self.next_attempt_at

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        elif isinstance(self.reason, str):
            reason = self.reason
        else:
            reason = self.reason


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "availability": availability,
            "capability": capability,
        })
        if next_attempt_at is not UNSET:
            field_dict["next_attempt_at"] = next_attempt_at
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        availability = check_capability_availability(d.pop("availability"))




        capability = check_controller_capability(d.pop("capability"))




        def _parse_next_attempt_at(data: object) -> datetime.datetime | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                next_attempt_at_type_0 = datetime.datetime.fromisoformat(data)



                return next_attempt_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        next_attempt_at = _parse_next_attempt_at(d.pop("next_attempt_at", UNSET))


        def _parse_reason(data: object) -> CapabilityReason | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                reason_type_0 = check_capability_reason(data)



                return reason_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(CapabilityReason | None | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))


        capability_status = cls(
            availability=availability,
            capability=capability,
            next_attempt_at=next_attempt_at,
            reason=reason,
        )

        return capability_status
