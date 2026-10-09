"""Shared HTTP failure families, carried in the x-vonk-outcome response header.

The body remains the endpoint's typed diagnostic document. This envelope is
independent of that document so middleware, streaming and upload endpoints all
publish the same decision before any response bytes are consumed.
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from .wire_model import WireModel, typed_tag


class HttpFailureFamily(StrEnum):
    TRANSIENT = "transient"
    REFUSAL = "refusal"


class TransientReason(StrEnum):
    CA_UNAVAILABLE = "ca_unavailable"
    CONTROLLER_STARTING = "controller_starting"
    ADMISSION_BUSY = "admission_busy"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    RATE_LIMITED = "rate_limited"
    PEER_RESPONSE_UNAVAILABLE = "peer_response_unavailable"
    LOCAL_STATE_UNAVAILABLE = "local_state_unavailable"


class HttpRefusalReason(StrEnum):
    AUTHENTICATION_REQUIRED = "authentication_required"
    AUTHORITY_DENIED = "authority_denied"
    UNKNOWN_IDENTITY = "unknown_identity"
    REVOKED_IDENTITY = "revoked_identity"
    INVALID_SIGNATURE = "invalid_signature"
    INVALID_DIGEST = "invalid_digest"
    TAMPERED_TOKEN = "tampered_token"
    EXPIRED_CREDENTIAL = "expired_credential"


class RenewalWindow(WireModel):
    """Offsets from issuance, in seconds; a client picks once per credential."""

    start_seconds: int = Field(strict=True, ge=0, le=2**32 - 1)
    end_seconds: int = Field(strict=True, ge=0, le=2**32 - 1)


class HttpTransient(WireModel):
    family: Literal[HttpFailureFamily.TRANSIENT] = typed_tag()
    reason: TransientReason
    retry_after: int = Field(strict=True, ge=0, le=3600)
    resolution_window: int = Field(strict=True, ge=1, le=3600)
    suggested_window: RenewalWindow | None = None


class HttpRefusal(WireModel):
    family: Literal[HttpFailureFamily.REFUSAL] = typed_tag()
    reason: HttpRefusalReason


HttpFailure = Annotated[HttpTransient | HttpRefusal, Field(discriminator="family")]


class HttpFailureResponse(WireModel):
    failure: HttpFailure


class RenewalHealth(WireModel):
    """Disposable status bound to the exact certificate content, never admission."""

    certificate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    failed: bool
