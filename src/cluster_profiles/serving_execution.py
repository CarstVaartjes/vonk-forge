"""Shared execution and evaluation of canonical recipe serving checks.

The recipe producer owns the request and assertion declarations.  This module
only performs the bounded transport work and evaluates observations against
those declarations; it does not invent a request or launch a runtime.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .cli_states import ENDED_STATES, SUCCEEDED
from .control_transport import open_https


class ServingExecutionError(ValueError):
    """A declared serving check could not be executed or evaluated."""


class ServingObservationUnknown(ServingExecutionError):
    """An unreadable peer observation, retried only by the bounded executor."""


MAX_HTTP_RESPONSE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class HttpObservation:
    status: int
    headers: Mapping[str, str]
    body: bytes


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ServingObservationUnknown(f"{label} projection is unavailable")
    return value


def _json_body(observation: HttpObservation) -> Mapping[str, object]:
    try:
        value = json.loads(observation.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ServingObservationUnknown("serving response is not valid JSON") from error
    if not isinstance(value, Mapping):
        raise ServingObservationUnknown("serving response projection is malformed")
    return value


def _request_body(check: Mapping[str, object]) -> Mapping[str, object]:
    request = _mapping(check.get("request"), "serving check request")
    body = request.get("body")
    return _mapping(body, "serving request body")


def _substitute_alias(value: object, model_alias: str | None) -> object:
    if (
        model_alias is not None
        and isinstance(value, str)
        and value in {"$ALIAS", "$MODEL"}
    ):
        return model_alias
    if isinstance(value, Mapping):
        return {
            str(key): _substitute_alias(item, model_alias)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_substitute_alias(item, model_alias) for item in value]
    return value


def _choices(response: Mapping[str, object]) -> list[Mapping[str, object]]:
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ServingObservationUnknown("serving response must contain one choice")
    if not isinstance(choices[0], Mapping):
        raise ServingObservationUnknown(
            "serving response choice projection is malformed"
        )
    return [choices[0]]


def _assert_output_cap(
    response: Mapping[str, object], check: Mapping[str, object], field: str
) -> None:
    body = _request_body(check)
    limit = body.get("max_tokens")
    if type(limit) is not int or limit < 1:
        raise ServingObservationUnknown(f"{field} request projection is unavailable")
    usage = response.get("usage")
    tokens = usage.get("completion_tokens") if isinstance(usage, Mapping) else None
    if type(tokens) is not int or tokens < 0:
        raise ServingObservationUnknown("serving token usage projection is unavailable")
    if tokens > limit:
        raise ServingExecutionError("serving response exceeds declared output cap")


def evaluate_http_response(
    observation: HttpObservation,
    check: Mapping[str, object],
) -> dict[str, object]:
    """Evaluate one real HTTP observation against one canonical check."""

    if observation.status < 200 or observation.status >= 300:
        raise ServingExecutionError(
            f"serving endpoint returned HTTP {observation.status}"
        )
    kind = check.get("kind")
    assertions = check.get("assertions")
    if not isinstance(assertions, Sequence) or isinstance(
        assertions, (str, bytes, bytearray)
    ):
        raise ServingObservationUnknown("serving check assertions are unavailable")
    assertions = list(assertions)
    response = _json_body(observation)
    if kind == "openai.health":
        if "endpoint.healthy" not in assertions:
            raise ServingObservationUnknown("health check projection is unavailable")
        return {"response_shape": "health", "status": observation.status}
    if kind == "openai.embedding":
        data = response.get("data")
        if "embedding.nonempty" in assertions and (
            not isinstance(data, list)
            or not data
            or any(
                not isinstance(item, Mapping)
                or not isinstance(item.get("embedding"), list)
                or not item["embedding"]
                for item in data
            )
        ):
            raise ServingExecutionError("embedding response is empty")
        return {
            "response_shape": "list.embedding",
            "embeddings": len(data) if isinstance(data, list) else 0,
        }
    if kind == "openai.completion":
        choice = _choices(response)[0]
        text = choice.get("text")
        if "completion.nonempty" in assertions and (
            not isinstance(text, str) or not text.strip()
        ):
            raise ServingExecutionError("completion response is empty")
        if "completion.output-cap" in assertions:
            _assert_output_cap(response, check, "completion.output-cap")
        return {"response_shape": "text_completion", "choices": 1}
    choice = _choices(response)[0]
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise ServingObservationUnknown(
            "serving response message projection is malformed"
        )
    content = message.get("content")
    has_content = isinstance(content, str) and bool(content.strip())
    tool_calls = message.get("tool_calls")
    has_tools = isinstance(tool_calls, list) and bool(tool_calls)
    if "chat.nonempty" in assertions and not has_content and not has_tools:
        raise ServingExecutionError("chat response contains no non-empty content")
    if "tools.called" in assertions and not has_tools:
        raise ServingExecutionError("chat response did not satisfy tools.called")
    if "chat.output-cap" in assertions:
        _assert_output_cap(response, check, "chat.output-cap")
    observed = ["text"] if has_content else []
    if has_tools:
        observed.append("tools")
    return {
        "response_shape": "chat.completion",
        "choices": 1,
        "observed_features": observed,
    }


def evaluate_job_result(
    result: Mapping[str, object], check: Mapping[str, object]
) -> dict[str, object]:
    """Evaluate a Controller artifact-job result without interpreting model bytes."""

    if not result:
        raise ServingObservationUnknown("job result projection is unavailable")
    assertions = check.get("assertions")
    if not isinstance(assertions, Sequence) or isinstance(
        assertions, (str, bytes, bytearray)
    ):
        raise ServingObservationUnknown("job check assertions are unavailable")
    if "artifact.output" in assertions:
        output_slot = _mapping(check.get("request"), "job request").get("output_slot")
        outputs = result.get("outputs")
        if not isinstance(outputs, list) or not outputs:
            raise ServingObservationUnknown("job output projection is unavailable")
        if output_slot is not None and not any(
            isinstance(item, Mapping) and item.get("slot") == output_slot
            for item in outputs
        ):
            raise ServingObservationUnknown("job output slot projection is unavailable")
    if "inference.completed" in assertions:
        state = result.get("state", result.get("status"))
        if state not in ENDED_STATES:
            raise ServingObservationUnknown("job completion projection is unavailable")
        if state != SUCCEEDED:
            raise ServingExecutionError(
                "job result does not show a successful completion"
            )
    declared_outputs = result.get("outputs")
    return {
        "response_shape": "job.result",
        "result_fields": sorted(str(key) for key in result),
        "output_count": len(declared_outputs)
        if isinstance(declared_outputs, list)
        else 0,
    }


def execute_http_check(
    base_url: str,
    check: Mapping[str, object],
    *,
    timeout_seconds: float = 30.0,
    model_alias: str | None = None,
    opener: Callable[..., Any] = open_https,
) -> dict[str, object]:
    """Issue the declared request and evaluate its actual response."""

    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ServingExecutionError("serving timeout must be finite and positive")
    deadline = time.monotonic() + timeout_seconds
    request = _mapping(check.get("request"), "serving check request")
    method = request.get("method")
    path = request.get("path")
    if method not in {"GET", "POST"} or not isinstance(path, str):
        raise ServingExecutionError("serving HTTP request is invalid")
    payload = _substitute_alias(request.get("body"), model_alias)
    data = (
        json.dumps(payload, separators=(",", ":")).encode()
        if payload is not None
        else None
    )
    http_request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        method=str(method),
        headers={"Content-Type": "application/json"} if data is not None else {},
    )

    def read_bounded(response: Any) -> bytes:
        raw_length = response.headers.get("Content-Length")
        try:
            declared_length = int(raw_length) if raw_length is not None else None
        except (TypeError, ValueError) as error:
            raise ServingObservationUnknown(
                "serving response Content-Length is invalid"
            ) from error
        if declared_length is not None and declared_length > MAX_HTTP_RESPONSE_BYTES:
            raise ServingExecutionError(
                "serving response exceeds the maximum body size"
            )
        body = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
        if len(body) > MAX_HTTP_RESPONSE_BYTES:
            raise ServingExecutionError(
                "serving response exceeds the maximum body size"
            )
        return body

    last_unknown = None
    while time.monotonic() < deadline:
        try:
            try:
                with opener(
                    http_request, timeout=max(0.001, deadline - time.monotonic())
                ) as response:
                    observation = HttpObservation(
                        int(response.status),
                        dict(response.headers.items()),
                        read_bounded(response),
                    )
            except urllib.error.HTTPError as error:
                with error:
                    observation = HttpObservation(
                        int(error.code),
                        dict(error.headers.items()),
                        read_bounded(error),
                    )
            if observation.status in (401, 403):
                raise ServingExecutionError(
                    "serving endpoint denied authentication or authorization"
                )
            if not 200 <= observation.status < 300:
                raise ServingObservationUnknown(
                    "serving endpoint observation is unavailable"
                )
            return evaluate_http_response(observation, check)
        except (ServingObservationUnknown, OSError, urllib.error.URLError) as error:
            last_unknown = error
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.5, remaining))
    raise ServingObservationUnknown(
        "serving observation deadline elapsed"
    ) from last_unknown


__all__ = [
    "MAX_HTTP_RESPONSE_BYTES",
    "HttpObservation",
    "ServingExecutionError",
    "evaluate_http_response",
    "evaluate_job_result",
    "execute_http_check",
]
