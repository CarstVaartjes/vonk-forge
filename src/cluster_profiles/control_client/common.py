"""Shared bounded observation policy and artifact input allocation."""

from __future__ import annotations

import json
import random
from collections.abc import Mapping
from email.message import Message

from ..cli_states_generated import HTTP_REFUSAL, HTTP_TRANSIENT
from .errors import (
    ControlClientError,
    ControlHTTPError,
    ControlMalformedResponse,
    ControlTransportError,
)
from .schema import validate_control_document


def http_outcome(
    headers: Mapping[str, str] | Message[str, str],
) -> tuple[str | None, int | None]:
    failure_family = None
    typed_retry_after = None
    encoded_outcome = headers.get("x-vonk-outcome")
    if encoded_outcome is not None and len(encoded_outcome) <= 4096:
        try:
            document = validate_control_document(
                "HttpFailureResponse", json.loads(encoded_outcome)
            )
            failure = document["failure"]
            if isinstance(failure, dict):
                family = failure["family"]
                failure_family = family if isinstance(family, str) else None
                retry_after = failure.get("retry_after")
                typed_retry_after = retry_after if type(retry_after) is int else None
        except (ControlClientError, ValueError, KeyError, TypeError, RecursionError):
            # An unreadable peer answer is an observation miss.
            failure_family = None
    return failure_family, typed_retry_after


_MAX_ARTIFACT_INPUT = 512 * 1024**2


def observation_unknown(error: ControlClientError) -> bool:
    """Only verified owner answers and authentication end observation early."""
    if (
        isinstance(error, ControlTransportError)
        and error.context is not None
        and error.context.transport == "tls"
    ):
        return False
    if isinstance(error, ControlHTTPError):
        if error.candidates:
            # Selector ambiguity is a complete answer, not missing observation.
            return False
        # Absent acceptance can become visible after a lost submission reply.
        # Re-observe the same key within this budget before returning the
        # owner's answer to the exact-request reconciliation caller.
        if error.failure_family == HTTP_REFUSAL:
            return False
        return error.failure_family in (None, HTTP_TRANSIENT)
    return isinstance(error, (ControlMalformedResponse, ControlTransportError))


def observation_delay(
    error: ControlClientError, attempt: int, remaining: float
) -> float:
    """Bound backoff without shortening the owner's minimum retry delay."""
    server_delay = getattr(error, "retry_after_seconds", None)
    minimum = server_delay if type(server_delay) is int and server_delay >= 0 else 0
    return min(
        remaining, minimum + random.uniform(0, min(3.2, 0.1 * 2 ** min(attempt, 5)))
    )
