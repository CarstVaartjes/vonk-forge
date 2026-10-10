"""Shared bounded observation policy and artifact input allocation."""

from __future__ import annotations

import random

from .errors import (
    ControlClientError,
    ControlHTTPError,
    ControlTransportError,
)

_MAX_ARTIFACT_INPUT = 512 * 1024**2


def observation_unknown(error: ControlClientError) -> bool:
    """Retry known temporary HTTP/transport failures, never an unknown answer."""
    status = (
        error.status_code
        if isinstance(error, ControlHTTPError)
        else (error.context.http_status if error.context is not None else None)
    )
    if isinstance(error, ControlTransportError):
        if error.context is not None and error.context.transport == "tls":
            return False
        # A received refusal wins over a later body timeout/reset.
        return status is None or not (400 <= status <= 499 and status != 429)
    return status is not None and (status == 429 or 500 <= status <= 599)


def observation_delay(
    error: ControlClientError, attempt: int, remaining: float
) -> float:
    """Bound backoff without shortening the owner's minimum retry delay."""
    server_delay = getattr(error, "retry_after_seconds", None)
    if type(server_delay) is int and server_delay >= 0:
        return min(remaining, float(server_delay))
    return min(remaining, random.uniform(0, min(3.2, 0.1 * 2 ** min(attempt, 5))))
