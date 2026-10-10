"""Standard HTTP temporary-failure bodies shared by Controller and clients."""

from enum import StrEnum

from pydantic import Field

from .wire_model import WireModel


class TransientReason(StrEnum):
    CA_UNAVAILABLE = "ca_unavailable"
    CONTROLLER_STARTING = "controller_starting"
    ADMISSION_BUSY = "admission_busy"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    RATE_LIMITED = "rate_limited"
    LOCAL_STATE_UNAVAILABLE = "local_state_unavailable"
    STORAGE_UNAVAILABLE = "storage_unavailable"


class HttpTransient(WireModel):
    reason: TransientReason
    retry_after: int = Field(strict=True, ge=0)
