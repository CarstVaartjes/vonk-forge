"""Current framed Unix-socket response from the privileged host helper."""

from typing import Annotated, Literal

from pydantic import Field

from .host_helper import Digest, SignedRecipeRunObservationReceipt, Uuid4Text
from .wire_model import WireModel


class HostHelperResponse(WireModel):
    """Shared response shape; consumers also enforce their grant/action context."""

    schema_version: Literal[1]
    request_id: Uuid4Text | None
    status: Literal[
        "rejected",
        "package-installed",
        "package-activation-confirmed",
        "unit-restarted",
        "reboot-scheduled",
        "container-runtime-request-executed",
        "container-runtime-stop-uncertain",
    ]
    evidence_sha256: Digest | None
    exit_code: Annotated[int, Field(ge=0, le=255)] | None = None
    error_code: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")] | None = None
    observation_receipt: SignedRecipeRunObservationReceipt | None = None
    diagnostic: Annotated[str, Field(max_length=8192)] | None = None
