"""Current framed Unix-socket response from the privileged host helper."""

from typing import Annotated, Literal

from pydantic import Field

from .agent_words import HostHelperResponseStatus
from .failure_evidence import FailureLogTail
from .host_helper import Digest, Uuid4Text
from .http_failure import HttpFailureResponse
from .reason_codes.helpers import HelperErrorCode
from .wire_model import WireModel


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
    status: HostHelperResponseStatus
    exit_code: Annotated[int, Field(ge=0, le=255)] | None = None
    error_code: HelperErrorCode | None = None
    failure: HttpFailureResponse | None = None
    installation_intent_nonce: Digest | None = None
    # Set only in the reply to a RecipeRunInspectionRequest.
    process_running: bool | None = None
    # One-line exit account of a container (exit code, OOM flag, cause token).
    # Carried by a rejected run inspection and by an executed one-shot job that
    # exited unsuccessfully or timed out.
    diagnostic: Annotated[str, Field(max_length=8192)] | None = None
    # The container's own bounded output, captured before the helper removed it
    # (a failed job or an exited workload) or read while it ran (an inspection
    # that asked for logs).
    process_logs: HostHelperProcessLogs | None = None
