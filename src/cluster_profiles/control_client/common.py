"""Shared bounded observation policy and artifact input allocation."""

from __future__ import annotations

from .errors import (
    ControlClientError,
    ControlForbidden,
    ControlHTTPError,
    ControlMalformedResponse,
    ControlTransportError,
    ControlUnauthorized,
)

_MAX_ARTIFACT_INPUT = 512 * 1024**2


def observation_unknown(error: ControlClientError) -> bool:
    """Only verified owner answers and authentication end observation early."""
    if isinstance(error, (ControlUnauthorized, ControlForbidden)):
        return False
    if (
        isinstance(error, ControlTransportError)
        and error.context is not None
        and error.context.transport == "tls"
    ):
        return False
    if isinstance(error, ControlHTTPError):
        # Absent acceptance can become visible after a lost submission reply.
        # Re-observe the same key within this budget before returning the
        # owner's answer to the exact-request reconciliation caller.
        return error.status_code not in (401, 403)
    return isinstance(error, (ControlMalformedResponse, ControlTransportError))


def observation_delay(
    error: ControlClientError, attempt: int, remaining: float
) -> float:
    """Bound backoff without shortening the owner's minimum retry delay."""
    server_delay = getattr(error, "retry_after_seconds", None)
    if type(server_delay) is int and server_delay >= 0:
        return remaining if server_delay >= remaining else max(0.01, server_delay)
    return min(remaining, 0.1 * 2 ** min(attempt, 5))
