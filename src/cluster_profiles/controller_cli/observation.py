"""Bounded progress observation, reconnect hints, and log following."""

from __future__ import annotations

import argparse
import shlex
import time
import urllib.parse
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from ..cli_outcome import (
    Observation,
    operation_state,
)
from ..cli_states import (
    LEGACY_PARTIAL,
)
from ..control_client import (
    ControlHTTPError,
    ControlMalformedResponse,
    ControlNotFound,
    ControlObservationUnavailable,
    ControlResponseTooLarge,
    ControlTransportError,
    ControlUnavailable,
)
from .cache_removal import _cache_operation_id
from .common import (
    ControllerClient,
    _interval_seconds,
    _profile_number,
    _quoted,
    _request_key,
    _timeout_seconds,
    _WatchCallback,
)

_TERMINAL_STATES = {
    "succeeded",
    "completed",
    "failed",
    LEGACY_PARTIAL,
    "cancelled",
    "superseded",
    "blocked",
    "rejected",
}


def _state(value: Mapping[str, object]) -> str:
    return operation_state(value)


def _watch_callback(args: argparse.Namespace) -> _WatchCallback | None:
    callback = getattr(args, "_watch_callback", None)
    return callback if isinstance(callback, _WatchCallback) else None


def _bounded_timeout(args: argparse.Namespace) -> float:
    return _timeout_seconds(str(getattr(args, "timeout_seconds", 30)))


def _bounded_interval(args: argparse.Namespace) -> float:
    return _interval_seconds(str(getattr(args, "interval_seconds", 1.0)))


def _observation_delay(
    error: BaseException, fallback: float, remaining: float
) -> float:
    """Return the delay before the next observation attempt.

    Bound waiting by the observation's remaining time. A server delay beyond
    that budget ends observation at its deadline, without polling prematurely.
    """

    retry_after = getattr(error, "retry_after_seconds", None)
    if type(retry_after) is int and retry_after >= 0:
        return max(0.01, float(min(retry_after, max(0.0, remaining))))
    return fallback


def _observation_reason(error: BaseException) -> str:
    if isinstance(error, ControlUnavailable):
        return "control API reported unavailable"
    if isinstance(error, (ControlTransportError, OSError)):
        return "control API connection was lost"
    return type(error).__name__


def _reconnect_command(args: argparse.Namespace, path: str) -> str:
    parts = path.split("/")
    if len(parts) == 6 and parts[1:3] == ["api", "profile"] and parts[4] == "requests":
        command = [
            "vonkctl",
            "--profile",
            parts[3],
            "profile",
            "progress",
            "--request-key",
            urllib.parse.unquote(parts[5]),
            "--follow",
        ]
    elif (
        len(parts) == 5
        and parts[1] == "api"
        and parts[2] in {"model", "recipe"}
        and parts[3] == "requests"
    ):
        command = [
            "vonkctl",
            parts[2],
            "progress",
            "--request-key",
            urllib.parse.unquote(parts[4]),
            "--follow",
        ]
    elif path.startswith("/api/profile/applications/"):
        command = [
            "vonkctl",
            "--profile",
            str(_profile_number(args)),
            "profile",
            "progress",
            "--application",
            urllib.parse.unquote(parts[4]),
            "--follow",
        ]
    elif len(parts) == 5 and parts[1:3] == ["api", "fleet"] and parts[4] == "loginfo":
        # Log observation has already resolved any friendly selector. Retain
        # the caller's complete scope, but reconnect by the immutable node ID.
        invocation: list[str] = list(getattr(args, "invocation", ()))
        replaced = False
        for index in range(len(invocation) - 2):
            if invocation[index : index + 2] == ["fleet", "loginfo"]:
                invocation[index + 2] = urllib.parse.unquote(parts[3])
                replaced = True
                break
        if replaced:
            command = ["vonkctl", *invocation]
        else:
            command = [
                "vonkctl",
                "fleet",
                "loginfo",
                urllib.parse.unquote(parts[3]),
            ]
            for name in ("since", "lines", "recipe", "source"):
                value = getattr(args, name, None)
                if value is not None:
                    command.extend((f"--{name}", str(value)))
            if getattr(args, "follow", False):
                command.append("--follow")
            command.extend(
                (
                    "--timeout-seconds",
                    str(_bounded_timeout(args)),
                    "--interval-seconds",
                    str(_bounded_interval(args)),
                )
            )
    elif (
        len(parts) == 5 and parts[2] in {"model", "recipe"} and parts[3] == "operations"
    ):
        command = [
            "vonkctl",
            parts[2],
            "progress",
            urllib.parse.unquote(parts[-1]),
            "--follow",
        ]
    elif path.startswith("/api/jobs/"):
        command = [
            "vonkctl",
            "fleet",
            "progress",
            urllib.parse.unquote(parts[-1]),
            "--follow",
        ]
    else:
        command = ["vonkctl", *getattr(args, "invocation", ())]
    return shlex.join(command)


def _poll_path(
    client: ControllerClient,
    path: str,
    initial: dict[str, object],
    args: argparse.Namespace,
    *,
    query: Mapping[str, object] | None = None,
    terminal: Callable[[Mapping[str, object]], bool] | None = None,
    validate: Callable[[Mapping[str, object]], object] | None = None,
    deadline: float | None = None,
    fetch_initial: bool = False,
    fetch: Callable[[float], object] | None = None,
    attempts: int | None = None,
    publish: bool = True,
) -> dict[str, object]:
    """Observe a bounded durable snapshot, retaining the last truthful value.

    A temporary loss of the Controller must not discard the observation.  The
    last confirmed snapshot remains evidence and polling continues to the
    bounded deadline, including a temporarily missing durable projection.
    Authorization errors remain immediate errors. Unreadable peer data is unknown.
    The durable operation's own outcome is
    never rewritten by an observation failure.
    """

    callback = _watch_callback(args)
    is_terminal = terminal or (lambda observed: _state(observed) in _TERMINAL_STATES)
    current = initial
    if fetch_initial and not publish:
        callback = None
    if validate is not None and not fetch_initial:
        try:
            validate(current)
        except (
            ControlMalformedResponse,
            ControlObservationUnavailable,
            ControlResponseTooLarge,
        ):
            current = {}
            fetch_initial = True
    started = time.monotonic()
    timeout = _bounded_timeout(args)
    observation = Observation(
        path,
        _reconnect_command(args, path),
        current,
        timeout,
        started,
        started,
        datetime.now(UTC),
    )
    args.observation = observation
    deadline = started + timeout if deadline is None else deadline
    interval = _bounded_interval(args)
    wait_before_request = not fetch_initial
    remaining_attempts = attempts
    while True:
        if callback is not None and publish:
            try:
                callback(current)
            except BrokenPipeError:
                observation.status = "interrupted"
                return current
        if not fetch_initial and is_terminal(current):
            observation.status = "complete"
            return current
        if time.monotonic() >= deadline or remaining_attempts == 0:
            observation.status = "timed_out"
            return current
        if wait_before_request:
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
        wait_before_request = True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            observation.status = "timed_out"
            return current
        try:
            if remaining_attempts is not None:
                remaining_attempts -= 1
            candidate = (
                fetch(remaining)
                if fetch is not None
                else client.request("GET", path, query=query, timeout_seconds=remaining)
            )
            if not isinstance(candidate, dict):
                raise ControlMalformedResponse("observation candidate is unreadable")
            if validate is not None:
                validate(candidate)
        except (
            ControlNotFound,
            ControlObservationUnavailable,
            ControlResponseTooLarge,
            ControlMalformedResponse,
            ControlUnavailable,
            ControlTransportError,
            OSError,
        ) as error:
            observation.error = _observation_reason(error)
            interval = _observation_delay(error, interval, deadline - time.monotonic())
            continue
        except ControlHTTPError as error:
            if error.status_code in {400, 401, 403, 422}:
                # Owner authentication/authorization and malformed query answers
                # remain strict. A conflicting or rate-limited read is unknown.
                raise
            observation.error = _observation_reason(error)
            interval = _observation_delay(error, interval, deadline - time.monotonic())
            continue
        current = candidate
        fetch_initial = False
        observation.update(current)
        interval = _bounded_interval(args)


def _follow_cache_operation(
    client: ControllerClient,
    noun: str,
    operation_id: str,
    result: dict[str, object],
    args: argparse.Namespace,
) -> dict[str, object]:
    def same_operation(observed: Mapping[str, object]) -> None:
        if _cache_operation_id(noun, observed) != operation_id:
            raise ControlMalformedResponse(
                "cache observation identifies another operation"
            )

    return _poll_path(
        client,
        f"/api/{noun}/operations/{_quoted(operation_id)}",
        result,
        args,
        validate=same_operation,
        terminal=lambda observed: (
            _state(observed) in _TERMINAL_STATES and observed.get("residue") is None
        ),
    )


def _cache_progress(
    client: ControllerClient,
    noun: str,
    args: argparse.Namespace,
    factory: Callable[[], str],
) -> dict[str, object]:
    deadline = time.monotonic() + _bounded_timeout(args)
    key = getattr(args, "request_key", None)
    if key is not None:
        key = _request_key(args, factory)
        path = f"/api/{noun}/requests/{_quoted(key)}"
    else:
        path = f"/api/{noun}/operations/{_quoted(args.operation_id)}"

    def identity(observed: object) -> None:
        if not isinstance(observed, Mapping):
            raise ControlMalformedResponse("cache observation is unreadable")
        operation_id = _cache_operation_id(noun, observed)
        if key is not None:
            key_field = (
                "request_id"
                if noun == "recipe" and "kind" in observed
                else "request_key"
            )
            if observed.get(key_field) != key:
                raise ControlMalformedResponse(
                    "cache lookup identifies another request"
                )
        elif operation_id != args.operation_id:
            raise ControlMalformedResponse("cache lookup identifies another operation")

    result = _poll_path(
        client,
        path,
        {},
        args,
        fetch_initial=True,
        terminal=lambda _: True,
        validate=identity,
        deadline=deadline,
    )
    if not args.follow or args.observation.status != "complete":
        return result
    operation_id = _cache_operation_id(noun, result)

    def identity_for_operation(observed: object) -> None:
        if (
            not isinstance(observed, Mapping)
            or _cache_operation_id(noun, observed) != operation_id
        ):
            raise ControlMalformedResponse(
                "cache observation identifies another operation"
            )

    return _poll_path(
        client,
        f"/api/{noun}/operations/{_quoted(operation_id)}",
        result,
        args,
        deadline=deadline,
        validate=lambda observed: identity_for_operation(observed),
        terminal=lambda observed: (
            _state(observed) in _TERMINAL_STATES and observed.get("residue") is None
        ),
    )


def _follow_mutation(
    client: ControllerClient,
    noun: str,
    result: dict[str, object],
    args: argparse.Namespace,
) -> dict[str, object]:
    """Follow a model/recipe mutation through its noun-owned operation view."""
    if getattr(getattr(args, "observation", None), "status", None) in {
        "timed_out",
        "interrupted",
    }:
        return result
    operation_id = _cache_operation_id(noun, result)
    if getattr(args, "detach", False):
        return result
    return _follow_cache_operation(client, noun, operation_id, result, args)


def _watch_resource(
    client: ControllerClient,
    path: str,
    result: dict[str, object],
    args: argparse.Namespace,
    *,
    query: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if not getattr(args, "watch", False):
        return result
    return _poll_path(client, path, result, args, query=query)


def _log_follow_complete(observed: Mapping[str, object]) -> bool:
    return (
        observed.get("follow") is False
        or observed.get("complete") is True
        or observed.get("closed") is True
        or _state(observed) in _TERMINAL_STATES
    )


def _follow_loginfo(
    client: ControllerClient,
    path: str,
    result: dict[str, object],
    args: argparse.Namespace,
    query: Mapping[str, object],
    node_id: str,
) -> dict[str, object]:
    def same_node(observed: Mapping[str, object]) -> None:
        if observed.get("node_id") != node_id:
            raise ControlMalformedResponse(
                "fleet log observation identifies another Spark"
            )

    return _poll_path(
        client,
        path,
        result,
        args,
        query=query,
        terminal=_log_follow_complete
        if getattr(args, "follow", False)
        else lambda _: True,
        validate=same_node,
    )
