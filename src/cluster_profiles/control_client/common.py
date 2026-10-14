"""Shared bounded observation policy and artifact input allocation."""

from __future__ import annotations

import random

from ..cli_states_generated import (
    DISPOSITION_FAIL,
    DISPOSITION_OBSERVE,
    DISPOSITION_SECURITY,
    DISPOSITION_WAIT,
    HTTP_STATUS_DISPOSITIONS,
)
from .errors import (
    ControlClientError,
    ControlHTTPError,
    ControlMalformedResponse,
    ControlTransportError,
)

_MAX_ARTIFACT_INPUT = 512 * 1024**2


def _status_disposition(status: int) -> str:
    return next(
        (
            verdict
            for first, last, verdict in HTTP_STATUS_DISPOSITIONS
            if first <= status <= last
        ),
        DISPOSITION_OBSERVE,
    )


def observation_disposition(error: ControlClientError) -> str:
    """The shared contract's disposition of a failed Controller request.

    The HTTP status table is the one the Controller and the agent read
    (generated into ``cli_states_generated``); a received refusal wins over a
    later body timeout or reset, and a TLS failure is a security refusal.
    """
    status = (
        error.status_code
        if isinstance(error, ControlHTTPError)
        else (error.context.http_status if error.context is not None else None)
    )
    if (
        isinstance(error, ControlTransportError)
        and error.context is not None
        and error.context.transport == "tls"
    ):
        return DISPOSITION_SECURITY
    if status is not None and status >= 400:
        return _status_disposition(status)
    if isinstance(error, ControlTransportError):
        return DISPOSITION_WAIT
    if isinstance(error, ControlMalformedResponse) and (
        status is None or 200 <= status <= 299
    ):
        return DISPOSITION_OBSERVE
    return DISPOSITION_FAIL


def observation_unknown(error: ControlClientError) -> bool:
    """Re-observe a wait or an unconfirmed answer within budget; preserve refusals."""
    return observation_disposition(error) in {DISPOSITION_WAIT, DISPOSITION_OBSERVE}


def observation_delay(
    error: ControlClientError, attempt: int, remaining: float
) -> float:
    """Bound backoff without shortening the owner's minimum retry delay."""
    server_delay = getattr(error, "retry_after_seconds", None)
    if type(server_delay) is int and server_delay >= 0:
        return min(remaining, float(server_delay))
    return min(remaining, random.uniform(0, min(3.2, 0.1 * 2 ** min(attempt, 5))))
