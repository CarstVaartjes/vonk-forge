"""Safe typed transport failures and remote error sanitization."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from email.message import Message
from typing import TYPE_CHECKING, Protocol, Self, TypedDict

from jsonschema import Draft202012Validator, validators

from ..error_reporting import (
    ErrorContext,
    decision_for,
    safe_code,
    safe_endpoint,
    safe_request_id,
)

if TYPE_CHECKING:
    # Importing any generated model runs the generated package initializer,
    # which imports all of its models; that alone was most of every vonkctl
    # invocation's start-up time. Load them where a request needs them.
    from ..generated_control.models.job_detail_response import JobDetailResponse


_MAX_ARTIFACT_OUTPUT = 1024**3


_MAX_TOKEN = 8192


_MAX_REMOTE_TEXT = 256


_PEM_BLOCK = re.compile(
    r"-----BEGIN ([A-Z0-9][A-Z0-9 -]{0,63})-----.*?"
    r"-----END \1-----",
    re.DOTALL,
)


_AUTHORIZATION = re.compile(r"(?i)(authorization\s*:\s*)(?:bearer|basic)\s+[^\s,;]+")


_BEARER = re.compile(r"(?i)\b(?:bearer|basic)\s+[^\s,;]+")


_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization|api[_-]?key|cert[_-]?pem|chain[_-]?pem|"
    r"client[_-]?certificate(?:[_-]?pem)?|"
    r"certificate(?:[_-]?(?:body|chain|data|pem))?|credential|password|"
    r"private[_-]?key|secret|token|x509)\b(\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)


_URL_CREDENTIALS = re.compile(r"(?i)(https?://)[^/@\s]+@")


_CONTROL_TYPE_CHECKER = Draft202012Validator.TYPE_CHECKER.redefine(
    "integer", lambda _checker, value: type(value) is int
).redefine(
    "number",
    lambda _checker, value: type(value) in (int, float) and math.isfinite(value),
)


_ControlValidator = validators.extend(
    Draft202012Validator, type_checker=_CONTROL_TYPE_CHECKER
)


class _OpenedResponse(Protocol):
    """The bounded surface used from the HTTPS transport or an injected peer."""

    @property
    def status(self) -> int: ...

    @property
    def headers(self) -> Message: ...

    def read(self, amount: int, /) -> bytes: ...

    def __enter__(self) -> Self: ...

    def __exit__(self, *args: object) -> None: ...


class _HTTPErrorFields(TypedDict, total=False):
    """Optional availability metadata accepted by ``ControlHTTPError``."""

    code: str | None
    recovery: tuple[str, ...]
    retryable: bool
    retry_time: str | None
    preserved: str | None
    required_bytes: int | None
    free_bytes: int | None
    shortfall_bytes: int | None
    log_excerpt: str | None
    candidates: tuple[str, ...]


def _nonnegative_integer(value: object) -> int | None:
    """Return *value* as a non-negative JSON integer, or ``None``.

    ``bool`` is rejected so a JSON ``true`` never becomes a byte count, and
    strings or floats are not coerced into one.
    """

    if type(value) is not int or value < 0:
        return None
    return value


class ControlClientError(RuntimeError):
    def __init__(self, message: str, *, context: ErrorContext | None = None) -> None:
        self.context = context
        super().__init__(message)

    @property
    def operation(self) -> str | None:
        return self.context.operation if self.context else None

    @property
    def endpoint(self) -> str | None:
        return self.context.endpoint if self.context else None

    @property
    def request_id(self) -> str | None:
        return self.context.request_id if self.context else None


class ControlMalformedResponse(ControlClientError):
    pass


class ControlResponseTooLarge(ControlClientError):
    pass


class ControlObservationUnavailable(ControlClientError):
    def __init__(
        self, reason_code: str, detail: str, *, context: ErrorContext | None = None
    ) -> None:
        self.reason_code = reason_code
        super().__init__(f"{reason_code}: {detail}", context=context)


class ControlTransportError(ControlClientError):
    def __init__(
        self,
        message: str = "control API request failed",
        *,
        context: ErrorContext | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message, context=context)


class ControlTimeout(ControlClientError):
    def __init__(
        self,
        job_id: str,
        job: JobDetailResponse | None,
        *,
        sensitive_values: tuple[str, ...] = (),
    ) -> None:
        self.job_id = job_id
        self.job = _safe_job_observation(job, sensitive_values=sensitive_values)
        super().__init__(f"timed out waiting for control job {job_id}")


class JobTerminalError(ControlClientError):
    def __init__(
        self, job: JobDetailResponse, *, sensitive_values: tuple[str, ...] = ()
    ) -> None:
        if job.status_reason is not None:
            job.status_reason = _sanitize_remote_text(
                job.status_reason,
                "job failed without a safe reason",
                sensitive_values=sensitive_values,
            )
        self.job = job
        self.reason = job.status_reason
        super().__init__(
            f"control job {job.id} entered {job.state}: "
            f"{job.status_reason or 'no reason provided'}"
        )


class JobFailed(JobTerminalError):
    pass


class JobWaitingForOperator(JobTerminalError):
    pass


class ControlHTTPError(ControlClientError):
    def __init__(
        self,
        status_code: int,
        detail: str,
        retry_after_seconds: int | None = None,
        *,
        code: str | None = None,
        recovery: tuple[str, ...] = (),
        retryable: bool = False,
        retry_time: str | None = None,
        preserved: str | None = None,
        required_bytes: int | None = None,
        free_bytes: int | None = None,
        shortfall_bytes: int | None = None,
        log_excerpt: str | None = None,
        candidates: tuple[str, ...] = (),
        sensitive_values: tuple[str, ...] = (),
        operation: str | None = None,
        endpoint: str | None = None,
        request_id: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = safe_code(code, f"http.{status_code}")
        self.recovery = recovery
        self.retryable = retryable
        self.retry_time = retry_time
        self.preserved = preserved
        self.required_bytes = required_bytes
        self.free_bytes = free_bytes
        self.shortfall_bytes = shortfall_bytes
        self.candidates = tuple(
            _sanitize_remote_text(value, "", sensitive_values=sensitive_values)
            for value in candidates
        )
        self.log_excerpt = (
            _sanitize_remote_text(log_excerpt, "", sensitive_values=sensitive_values)
            if log_excerpt
            else None
        )
        self.detail = _sanitize_remote_text(
            detail,
            "control API request failed",
            sensitive_values=sensitive_values,
        )
        self.retry_after_seconds = retry_after_seconds
        source = "remote_rejection"
        retry_decision = decision_for(
            retryable=retryable,
            retry_after_seconds=retry_after_seconds,
            source=source,
        )
        context = ErrorContext(
            operation=operation or "control.http",
            endpoint=safe_endpoint(endpoint),
            code=safe_code(code, f"http.{status_code}"),
            source=source,
            decision=retry_decision,
            retryable=retryable,
            http_status=status_code,
            request_id=safe_request_id(request_id),
        )
        super().__init__(context.render(self.detail), context=context)


class ControlUnauthorized(ControlHTTPError):
    pass


class ControlForbidden(ControlHTTPError):
    pass


class ControlNotFound(ControlHTTPError):
    pass


class ControlConflict(ControlHTTPError):
    pass


class ControlUnavailable(ControlHTTPError):
    pass


_STATUS_ERRORS: dict[int, type[ControlHTTPError]] = {
    401: ControlUnauthorized,
    403: ControlForbidden,
    404: ControlNotFound,
    409: ControlConflict,
    503: ControlUnavailable,
}


def _retry_after_seconds(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        seconds = int(value)
    except ValueError:
        return None
    if seconds < 0:
        return None
    # The caller bounds its own waiting budget; shortening a server delay
    # would authorize a retry before the dependency is willing to accept it.
    return max(1, seconds)


def _sanitize_remote_text(
    value: object,
    fallback: str,
    *,
    sensitive_values: tuple[str, ...] = (),
) -> str:
    if not isinstance(value, str) or not value:
        return fallback
    text = value.replace("\x00", "")
    for sensitive_value in sorted(set(sensitive_values), key=len, reverse=True):
        if sensitive_value:
            text = text.replace(sensitive_value, "<redacted>")
    text = _PEM_BLOCK.sub("<redacted pem>", text)
    if "-----BEGIN " in text:
        text = text.split("-----BEGIN ", 1)[0] + "<redacted pem>"
    text = _AUTHORIZATION.sub(r"\1<redacted>", text)
    text = _BEARER.sub("<redacted>", text)
    text = _SENSITIVE_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>", text
    )
    text = _URL_CREDENTIALS.sub(r"\1<redacted>@", text)
    text = "".join(
        character for character in text if character in "\n\t" or ord(character) >= 32
    ).strip()
    if not text:
        text = fallback
    marker = "...<truncated>"
    if len(text) > _MAX_REMOTE_TEXT:
        text = text[: _MAX_REMOTE_TEXT - len(marker)] + marker
    return text


def _structured_http_error_fields(
    problem: object,
) -> tuple[_HTTPErrorFields, int | None]:
    """Extract optional shared availability error metadata without exposing secrets."""
    if not isinstance(problem, Mapping):
        return {}, None
    code = problem.get("code", problem.get("error_code"))
    context = problem.get("context")
    if (not isinstance(code, str) or not code) and isinstance(context, Mapping):
        code = context.get("code")
    raw_recovery = problem.get("recovery_actions", problem.get("recovery", ()))
    if isinstance(raw_recovery, str):
        recovery: tuple[str, ...] = (raw_recovery,)
    elif isinstance(raw_recovery, list):
        recovery = tuple(item for item in raw_recovery if isinstance(item, str))[:8]
    else:
        recovery = ()
    retry_time = problem.get("retry_time", problem.get("retry_at"))
    retry_after_seconds = _nonnegative_integer(problem.get("retry_after_seconds"))
    retryable = problem.get("retryable") is True
    preserved = problem.get("preserved")
    log_excerpt = problem.get("log_excerpt")
    candidates = problem.get("candidates")
    fields: _HTTPErrorFields = {
        "code": code if isinstance(code, str) and code else None,
        "recovery": recovery,
        "retry_time": retry_time if isinstance(retry_time, str) else None,
        "retryable": retryable,
        "preserved": preserved if isinstance(preserved, str) else None,
        "required_bytes": _nonnegative_integer(problem.get("required_bytes")),
        "free_bytes": _nonnegative_integer(problem.get("free_bytes")),
        "shortfall_bytes": _nonnegative_integer(problem.get("shortfall_bytes")),
        "log_excerpt": log_excerpt if isinstance(log_excerpt, str) else None,
        "candidates": tuple(candidates)
        if isinstance(candidates, list)
        and all(isinstance(value, str) for value in candidates)
        else (),
    }
    return fields, retry_after_seconds


def _safe_job_observation(
    job: JobDetailResponse | None,
    *,
    sensitive_values: tuple[str, ...] = (),
) -> JobDetailResponse | None:
    if job is None:
        return None
    from ..generated_control.models.job_detail_response import JobDetailResponse

    safe_job = JobDetailResponse.from_dict(job.to_dict())
    if safe_job.status_reason is not None:
        safe_job.status_reason = _sanitize_remote_text(
            safe_job.status_reason,
            "job observation has no safe reason",
            sensitive_values=sensitive_values,
        )
    return safe_job


__all__ = ["_OpenedResponse"]
