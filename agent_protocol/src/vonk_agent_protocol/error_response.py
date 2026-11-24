"""The bounded error envelope every Controller route can answer with."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import ConfigDict, Field

from vonk_agent_protocol.wire_model import StrictJSONModel


class _ErrorModel(StrictJSONModel):
    """Exact structure and scalar types for the error envelope; not itself a message."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ErrorContextResponse(_ErrorModel):
    """Safe context shared by public errors and generated clients."""

    operation: str = Field(min_length=1, max_length=160)
    endpoint: str | None = Field(
        default=None,
        pattern=r"^/[^?#\x00\r\n]{0,511}$",
        max_length=512,
    )
    http_status: int | None = Field(default=None, ge=100, le=599)
    code: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,95}$")
    request_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    source: Literal["remote_rejection", "transport", "local_io", "protocol", "unknown"]
    decision: Literal["retry", "defer", "exit"]
    retryable: bool = False


class BoundedErrorResponse(_ErrorModel):
    detail: str = Field(min_length=1, max_length=256)
    context: ErrorContextResponse | None = None


class RequestValidationIssue(_ErrorModel):
    """A structural input error without the submitted input or validator context."""

    type: str
    loc: list[str | int]
    msg: str


class RequestValidationProblem(BoundedErrorResponse):
    issues: list[RequestValidationIssue]
    candidates: (
        list[
            Annotated[
                str,
                Field(
                    pattern=r"^(?:[0-9a-f]{64}|spk_[0-9a-f]{32}|[a-z0-9][a-z0-9._-]{0,62}/[a-z0-9][a-z0-9._-]{0,62})$",
                    max_length=127,
                ),
            ]
        ]
        | None
    ) = None
