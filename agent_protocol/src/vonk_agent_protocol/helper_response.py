"""Current framed Unix-socket response from the privileged host helper."""

from typing import Annotated, Literal

from pydantic import Field

from .failure_evidence import FailureLogTail
from .host_helper import Digest, SignedRecipeRunObservationReceipt, Uuid4Text
from .wire_model import ErrorCode, WireModel


class HostHelperProcessLogs(WireModel):
    """The exact failed container's own output, retained per stream.

    The helper is the only party that ever holds the container's log, so it owns
    the retention bound and reports it per stream.  Keeping the two streams
    apart matters more than the bound: a merged document can only ever show one
    tail, and whichever stream is written first is always the one discarded.
    """

    stdout: FailureLogTail
    stderr: FailureLogTail


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
    error_code: ErrorCode | None = None
    observation_receipt: SignedRecipeRunObservationReceipt | None = None
    diagnostic: Annotated[str, Field(max_length=8192)] | None = None
    process_logs: HostHelperProcessLogs | None = None
