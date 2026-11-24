"""Availability of independently recoverable Controller capabilities."""

from datetime import datetime
from enum import StrEnum

from vonk_agent_protocol.capability_reply import (
    CapabilityReason,
    ControllerCapability,
)
from vonk_control.strict_json import StrictModel


class CapabilityAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class CapabilityStatus(StrictModel):
    capability: ControllerCapability
    availability: CapabilityAvailability
    reason: CapabilityReason | None = None
    next_attempt_at: datetime | None = None
