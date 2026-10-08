"""Idempotent submission and unknown-outcome reconciliation."""

from __future__ import annotations

import argparse
import math
import sys
import time
from collections.abc import Callable, Mapping

from ..cli_outcome import (
    Submission,
)
from ..control_client import (
    ControlClientError,
    ControlHTTPError,
    ControlMalformedResponse,
    ControlNotFound,
    ControlObservationUnavailable,
    ControlResponseTooLarge,
    ControlTransportError,
    ControlUnavailable,
)
from ..error_reporting import ErrorContext, protocol_context, transport_context
from .common import ControllerClient


def _known_http_refusal_status(error: ControlClientError) -> int | None:
    status = (
        error.status_code
        if isinstance(error, ControlHTTPError)
        else (error.context.http_status if error.context else None)
    )
    return status if status is not None and 400 <= status <= 499 else None


def _submit_idempotent_request(
    client: ControllerClient,
    args: argparse.Namespace,
    *,
    key: str,
    path: str,
    lookup: str,
    body: dict[str, object],
    noun: str,
    action: str,
    validate: Callable[[Mapping[str, object]], str],
    lookup_validate: Callable[[Mapping[str, object]], str] | None = None,
    reconnect: str,
) -> dict[str, object]:
    """Bounded submission, exact-request reconciliation, and one identical replay."""
    normal_timeout = client.request_timeout_seconds
    if not math.isfinite(normal_timeout) or normal_timeout <= 0:
        raise ValueError("request timeout must be finite and positive")
    submission = Submission(key, path, lookup, 3 * normal_timeout, action=action)
    args.submission = submission
    deadline = time.monotonic() + submission.timeout_seconds

    def end_unknown(*, interrupted: bool = False) -> None:
        from .observation import _poll_path

        _poll_path(
            client, lookup, {}, args, deadline=time.monotonic(), fetch_initial=True
        )
        args.observation.reconnect_command = reconnect
        if interrupted:
            args.observation.status = "interrupted"

    if not (args.global_json or getattr(args, "json", False)):
        try:
            print(
                f"Request key: {key}\nReconnect: {reconnect}",
                file=sys.stderr,
                flush=True,
            )
        except BrokenPipeError:
            end_unknown(interrupted=True)
            return {}

    def request(
        method: str,
        target: str,
        stage: str,
        *,
        receipt_validator: Callable[[Mapping[str, object]], str] | None = None,
    ) -> dict[str, object]:
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ControlTransportError(
                    f"{action} submission deadline reached",
                    context=ErrorContext(
                        operation=f"{noun}.{action}.{stage}",
                        endpoint=target,
                        code="control.submission_timeout",
                        source="transport",
                        transport="timeout",
                        decision="exit",
                    ),
                )
            result = client.request(
                method,
                target,
                body if method == "POST" else None,
                timeout_seconds=min(normal_timeout, remaining),
            )
            operation_id = (receipt_validator or validate)(result)
        except BrokenPipeError:
            raise
        except (ControlClientError, OSError) as error:
            context = error.context if isinstance(error, ControlClientError) else None
            if context is None:
                context = (
                    transport_context(
                        operation=f"{noun}.{action}.{stage}",
                        endpoint=target,
                        error=error,
                    )
                    if isinstance(error, (ControlTransportError, OSError))
                    else protocol_context(
                        operation=f"{noun}.{action}.{stage}", endpoint=target
                    )
                )
            submission.failures.append({"stage": stage, **context.as_dict()})
            raise
        submission.acceptance = "accepted"
        submission.operation_id = operation_id
        return result

    retry_error: ControlHTTPError | ControlTransportError | None = None
    retry_not_before = 0.0
    submission.acceptance = "unknown"
    try:
        return request("POST", path, "submit")
    except BrokenPipeError:
        end_unknown(interrupted=True)
        return {}
    except OSError:
        may_replay = True
    except ControlClientError as error:
        status = _known_http_refusal_status(error)
        if status is not None:
            submission.acceptance = "refused"
            raise
        if isinstance(
            error,
            (
                ControlMalformedResponse,
                ControlResponseTooLarge,
                ControlObservationUnavailable,
            ),
        ):
            # Diagnose by read only. An invalid receipt never licenses a replay.
            may_replay = False
        elif isinstance(error, ControlTransportError) or (
            isinstance(error, ControlHTTPError) and 500 <= error.status_code <= 599
        ):
            may_replay = True
            if (
                isinstance(error, (ControlHTTPError, ControlTransportError))
                and error.retry_after_seconds is not None
            ):
                retry_error = error
                now = time.monotonic()
                # A delay beyond this invocation's budget needs no timer and
                # must not overflow when represented as floating-point time.
                retry_not_before = (
                    deadline
                    if error.retry_after_seconds >= deadline - now
                    else now + error.retry_after_seconds
                )
        else:
            raise

    if lookup == path:
        if not may_replay:
            end_unknown()
            return {}
    else:
        from .observation import _poll_path

        def reconcile(_remaining: float) -> object:
            return request("GET", lookup, "lookup", receipt_validator=lookup_validate)

        try:
            return request("GET", lookup, "lookup", receipt_validator=lookup_validate)
        except ControlNotFound:
            if may_replay:
                pass
            else:
                return _poll_path(
                    client,
                    lookup,
                    {},
                    args,
                    deadline=deadline,
                    fetch_initial=True,
                    fetch=reconcile,
                    attempts=3,
                    terminal=lambda _: True,
                )
        except (
            ControlMalformedResponse,
            ControlResponseTooLarge,
            ControlObservationUnavailable,
            ControlTransportError,
            ControlUnavailable,
            OSError,
        ):
            return _poll_path(
                client,
                lookup,
                {},
                args,
                deadline=deadline,
                fetch_initial=True,
                fetch=reconcile,
                attempts=3,
                terminal=lambda _: True,
            )
    if retry_error is not None:
        now = time.monotonic()
        delay = max(0.0, retry_not_before - now)
        if delay >= deadline - now:
            end_unknown()
            return {}
        if delay:
            time.sleep(delay)
    try:
        return request("POST", path, "replay")
    except BrokenPipeError:
        end_unknown(interrupted=True)
        return {}
    except (
        ControlMalformedResponse,
        ControlResponseTooLarge,
        ControlObservationUnavailable,
        ControlTransportError,
        ControlUnavailable,
        OSError,
    ):
        if lookup == path:
            end_unknown()
            return {}
        return _poll_path(
            client,
            lookup,
            {},
            args,
            deadline=deadline,
            fetch_initial=True,
            fetch=reconcile,
            attempts=3,
            terminal=lambda _: True,
        )
