"""Bounded HTTPS client for normal control-plane administration."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any

import httpx
from jsonschema import Draft202012Validator, FormatChecker, validators
from jsonschema.exceptions import SchemaError

from .error_reporting import (
    ErrorContext,
    decision_for,
    local_io_context,
    protocol_context,
    safe_code,
    safe_endpoint,
    safe_request_id,
    transport_context,
)
from .generated_control.api.default import (
    get_fleet_status,
    get_job,
    get_published_endpoint,
)
from .generated_control.client import AuthenticatedClient
from .generated_control.models.endpoint_response import EndpointResponse
from .generated_control.models.fleet_snapshot import FleetSnapshot
from .generated_control.models.job_detail_response import JobDetailResponse
from .generated_control.types import Response as GeneratedResponse

_MAX_RESPONSE = 1_048_576
_MAX_ARTIFACT_INPUT = 512 * 1024**2
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


class ControlTransportError(ControlClientError):
    def __init__(self, message: str = "control API request failed", *, context: ErrorContext | None = None) -> None:
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
        self.log_excerpt = _sanitize_remote_text(log_excerpt, "", sensitive_values=sensitive_values) if log_excerpt else None
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


def _bounded_retry_after(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        seconds = int(value)
    except ValueError:
        return None
    if seconds < 0:
        return None
    return max(1, min(30, seconds))


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


@lru_cache(maxsize=1)
def _control_openapi() -> dict[str, object]:
    try:
        raw = files("cluster_profiles.schemas").joinpath(
            "control-openapi.json"
        ).read_text()
        schema = json.loads(raw)
        Draft202012Validator.check_schema(schema)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, SchemaError):
        raise ControlClientError("bundled control API schema is invalid") from None
    if not isinstance(schema, dict):
        raise ControlClientError("bundled control API schema must be an object")
    return schema


@lru_cache(maxsize=1)
def _control_validator() -> Draft202012Validator:
    return _ControlValidator(_control_openapi(), format_checker=FormatChecker())


def _path_pattern(template: str) -> re.Pattern[str]:
    escaped = re.escape(template)
    return re.compile(r"^" + re.sub(r"\\\{[^}]+\\\}", r"[^/]+", escaped) + r"$")


@lru_cache(maxsize=256)
def _operation(path: str, method: str) -> dict[str, object]:
    schema = _control_openapi()
    paths = schema.get("paths")
    if not isinstance(paths, dict):
        raise ControlClientError("bundled control API schema has no paths")
    candidates = sorted(
        paths.items(), key=lambda entry: (entry[0].count("{"), -len(entry[0]))
    )
    for template, item in candidates:
        if not isinstance(template, str) or not isinstance(item, dict):
            continue
        if _path_pattern(template).match(path):
            operation = item.get(method.lower())
            if isinstance(operation, dict):
                return operation
    raise ControlClientError("control API route is not in the bundled schema")


def _validate_schema(value: object, schema: object, *, message: str) -> None:
    if not isinstance(schema, dict):
        return
    # Keep the generated document as the reference root while validating an
    # operation-local schema, so component $refs resolve exactly as emitted.
    operation_schema = {
        "components": _control_openapi().get("components", {}),
        "allOf": [schema],
    }
    error = next(
        _control_validator().evolve(schema=operation_schema).iter_errors(value),
        None,
    )
    if error is not None:
        raise ControlClientError(message) from None


def _request_contract(
    path: str, method: str, payload: Mapping[str, object] | None
) -> None:
    operation = _operation(path, method)
    request_body = operation.get("requestBody")
    if not isinstance(request_body, dict):
        if payload is not None:
            raise ControlClientError(
                "control API request has no OpenAPI request body"
            )
        return
    if payload is None:
        if request_body.get("required") is True:
            raise ControlClientError(
                "control API request is missing its OpenAPI request body"
            )
        return
    content = request_body.get("content")
    if not isinstance(content, dict):
        raise ControlClientError("control API request body media types are invalid")
    media = content.get("application/json")
    if not isinstance(media, dict):
        raise ControlClientError(
            "control API request body does not accept application/json"
        )
    _validate_schema(
        payload,
        media.get("schema"),
        message="control API request does not match the OpenAPI schema",
    )


def _request_media_contract(path: str, method: str, media_type: str) -> None:
    operation = _operation(path, method)
    request_body = operation.get("requestBody")
    if not isinstance(request_body, dict):
        raise ControlClientError("control API request has no OpenAPI request body")
    content = request_body.get("content")
    if not isinstance(content, dict) or media_type not in content:
        raise ControlClientError(
            "control API request content type is not documented"
        )


def _response_definition(
    path: str, method: str, status: int
) -> dict[str, object]:
    operation = _operation(path, method)
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        raise ControlMalformedResponse("control API returned an undocumented status")
    response = responses.get(str(status), responses.get("default"))
    if not isinstance(response, dict):
        raise ControlMalformedResponse("control API returned an undocumented status")
    return response


def _response_contract(path: str, method: str, status: int, decoded: object) -> None:
    response = _response_definition(path, method, status)
    content = response.get("content")
    if not isinstance(content, dict):
        return
    media = content.get("application/json")
    if isinstance(media, dict):
        try:
            _validate_schema(
                decoded,
                media.get("schema"),
                message="control API response does not match the OpenAPI schema",
            )
        except ControlClientError as error:
            raise ControlMalformedResponse(str(error)) from None


def _response_media_contract(
    path: str, method: str, status: int, media_type: str, *, has_content: bool
) -> None:
    if not has_content:
        return
    response = _response_definition(path, method, status)
    content = response.get("content")
    if not isinstance(content, dict) or media_type not in content:
        raise ControlMalformedResponse(
            "control API response content type is not documented"
        )


def _validate_generated_request(request: httpx.Request) -> None:
    parsed_url = urllib.parse.urlsplit(str(request.url))
    route_path = parsed_url.path
    request_media_type = request.headers.get("content-type", "").split(";", 1)[0]
    if request.content:
        if request_media_type.strip().lower() == "application/json":
            try:
                payload = json.loads(request.content)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ControlClientError(
                    "control API request contains invalid JSON"
                ) from None
            if not isinstance(payload, Mapping):
                raise ControlClientError(
                    "control API request must be a JSON object"
                )
            _request_contract(route_path, request.method, payload)
        else:
            _request_media_contract(
                route_path, request.method, request_media_type.strip().lower()
            )
    else:
        _request_contract(route_path, request.method, None)


def _validate_generated_response(
    request: httpx.Request, response: httpx.Response
) -> None:
    parsed_url = urllib.parse.urlsplit(str(request.url))
    route_path = parsed_url.path
    response_media_type = response.headers.get("content-type", "").split(";", 1)[0]
    _response_media_contract(
        route_path,
        request.method,
        response.status_code,
        response_media_type.strip().lower(),
        has_content=bool(response.content),
    )
    if response.status_code == 204 or not response.content:
        _response_contract(route_path, request.method, response.status_code, {})
    elif response_media_type.strip().lower() == "application/json":
        try:
            decoded = json.loads(response.content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ControlMalformedResponse("control API returned invalid JSON") from None
        _response_contract(route_path, request.method, response.status_code, decoded)
    else:
        _response_definition(route_path, request.method, response.status_code)


def _structured_http_error_fields(problem: object) -> dict[str, object]:
    """Extract optional shared availability error metadata without exposing secrets."""
    if not isinstance(problem, Mapping):
        return {}
    code = problem.get("code", problem.get("error_code"))
    context = problem.get("context")
    if (not isinstance(code, str) or not code) and isinstance(context, Mapping):
        code = context.get("code")
    recovery = problem.get("recovery_actions", problem.get("recovery", ()))
    if isinstance(recovery, str):
        recovery = (recovery,)
    elif isinstance(recovery, list):
        recovery = tuple(item for item in recovery if isinstance(item, str))[:8]
    else:
        recovery = ()
    retry_time = problem.get("retry_time", problem.get("retry_at"))
    retry_after_seconds = problem.get("retry_after_seconds")
    retry_after_seconds = retry_after_seconds if type(retry_after_seconds) is int and retry_after_seconds >= 0 else None
    retryable = problem.get("retryable") is True
    preserved = problem.get("preserved")
    numeric_fields = {
        key: problem.get(key) if type(problem.get(key)) is int and problem.get(key) >= 0 else None
        for key in ("required_bytes", "free_bytes", "shortfall_bytes")
    }
    log_excerpt = problem.get("log_excerpt")
    return {
        "code": code if isinstance(code, str) and code else None,
        "recovery": recovery,
        "retry_time": retry_time if isinstance(retry_time, str) else None,
        "retry_after_seconds": retry_after_seconds,
        "retryable": retryable,
        "preserved": preserved if isinstance(preserved, str) else None,
        **numeric_fields,
        "log_excerpt": log_excerpt if isinstance(log_excerpt, str) else None,
    }


def _safe_job_observation(
    job: JobDetailResponse | None,
    *,
    sensitive_values: tuple[str, ...] = (),
) -> JobDetailResponse | None:
    if job is None:
        return None
    safe_job = JobDetailResponse.from_dict(job.to_dict())
    if safe_job.status_reason is not None:
        safe_job.status_reason = _sanitize_remote_text(
            safe_job.status_reason,
            "job observation has no safe reason",
            sensitive_values=sensitive_values,
        )
    return safe_job


def _read_token_file(token_file: Path) -> str:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOINHERIT", 0)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise ControlClientError("control token file cannot be opened safely")
    flags |= no_follow

    descriptor = -1
    try:
        descriptor = os.open(token_file, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ControlClientError("control token must be a regular non-symlink file")
        getuid = getattr(os, "getuid", None)
        if getuid is not None and metadata.st_uid != getuid():
            raise ControlClientError("control token file owner is invalid")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ControlClientError("control token file permissions are too broad")

        content = bytearray()
        while len(content) <= _MAX_TOKEN:
            chunk = os.read(descriptor, _MAX_TOKEN + 1 - len(content))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > _MAX_TOKEN:
            raise ControlClientError("control token file is invalid")
        try:
            token = bytes(content).decode().strip()
        except UnicodeDecodeError:
            raise ControlClientError("control token file is invalid") from None
    except ControlClientError:
        raise
    except OSError as error:
        context = local_io_context(
            operation="read control token", path=token_file, error=error
        )
        raise ControlClientError(
            context.render("control token must be a regular non-symlink file"),
            context=context,
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if (
        not token
        or len(token) > _MAX_TOKEN
        or any(character.isspace() for character in token)
    ):
        raise ControlClientError("control token file is invalid")
    return token


class _OpenerTransport(httpx.BaseTransport):
    def __init__(self, opener: Callable[..., object], timeout: float) -> None:
        self._opener = opener
        self._timeout = timeout

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        outgoing = urllib.request.Request(
            str(request.url),
            data=request.content or None,
            headers=dict(request.headers),
            method=request.method,
        )
        try:
            response_context = self._opener(outgoing, timeout=self._timeout)
        except urllib.error.HTTPError as error:
            response_context = error
        except (OSError, urllib.error.URLError) as error:
            context = transport_context(
                operation=f"{request.method} {safe_endpoint(str(request.url)) or '/'}",
                endpoint=str(request.url),
                error=error,
            )
            raise ControlTransportError(
                context.render("control API request failed"), context=context
            ) from None
        with response_context as response:  # type: ignore[attr-defined]
            content = response.read(_MAX_RESPONSE + 1)  # type: ignore[attr-defined]
            if len(content) > _MAX_RESPONSE:
                raise ControlResponseTooLarge(
                    "control API response exceeds safety limit"
                )
            response_headers = httpx.Headers(response.headers.items())  # type: ignore[attr-defined]
            return httpx.Response(
                response.status,  # type: ignore[attr-defined]
                content=content,
                headers=response_headers,
                request=request,
            )


class _RecordingTransport(httpx.BaseTransport):
    def __init__(self, transport: httpx.BaseTransport) -> None:
        self._transport = transport
        self.request: httpx.Request | None = None
        self.response: httpx.Response | None = None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        _validate_generated_request(request)
        response = self._transport.handle_request(request)
        self.response = response
        if len(response.content) > _MAX_RESPONSE:
            raise ControlResponseTooLarge("control API response exceeds safety limit")
        _validate_generated_response(request, response)
        return response


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _redirect_denied_opener() -> Callable[..., object]:
    return urllib.request.build_opener(_RejectRedirectHandler()).open


class ControlClient:
    def __init__(
        self,
        base_url: str,
        token_file: Path,
        *,
        opener: Callable[..., object] | None = None,
        timeout_seconds: float = 15,
        artifact_transfer_timeout_seconds: float = 3_600,
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ControlClientError(
                "control URL must be an HTTPS origin without credentials"
            )
        token = _read_token_file(token_file)
        if not 1 <= artifact_transfer_timeout_seconds <= 3_600:
            raise ControlClientError(
                "artifact transfer timeout must be between 1 and 3600 seconds"
            )
        self._base = base_url.rstrip("/")
        self._token = token
        self._opener = opener if opener is not None else _redirect_denied_opener()
        self._transport = _OpenerTransport(self._opener, timeout_seconds)
        self._timeout = timeout_seconds
        self._artifact_transfer_timeout = artifact_transfer_timeout_seconds

    def _generated_client(
        self,
        transport: httpx.BaseTransport,
        headers: Mapping[str, str] | None = None,
    ) -> AuthenticatedClient:
        return AuthenticatedClient(
            base_url=self._base,
            token=self._token,
            headers={"Accept": "application/json", **dict(headers or {})},
            timeout=httpx.Timeout(self._timeout),
            verify_ssl=True,
            follow_redirects=False,
            httpx_args={"transport": transport},
        )

    def _raise_http_status(
        self,
        status_code: int,
        parsed: object,
        headers: Mapping[str, str],
        *,
        operation: str = "control.http",
        endpoint: str | None = None,
    ) -> None:
        if status_code < 400:
            return
        error_type = _STATUS_ERRORS.get(status_code, ControlHTTPError)
        detail = getattr(parsed, "detail", "control API request failed")
        if not isinstance(detail, str):
            detail = "control API request failed"
        context = getattr(parsed, "context", None)
        code = getattr(parsed, "code", None)
        if not isinstance(code, str) or not code:
            code = getattr(parsed, "error_code", None)
        if (not isinstance(code, str) or not code) and context is not None:
            code = getattr(context, "code", None)
        if not isinstance(code, str) or not code:
            code = headers.get("x-vonk-error-code")
        if not isinstance(code, str) or not code:
            code = None
        recovery_value = getattr(parsed, "recovery_actions", getattr(parsed, "recovery", ()))
        if isinstance(recovery_value, str):
            recovery = (recovery_value,)
        elif isinstance(recovery_value, (list, tuple)):
            recovery = tuple(item for item in recovery_value if isinstance(item, str))[:8]
        else:
            recovery = ()
        retry_time = getattr(parsed, "retry_time", None)
        if not isinstance(retry_time, str):
            retry_time = getattr(parsed, "retry_at", None)
        if not isinstance(retry_time, str):
            retry_time = None
        preserved = getattr(parsed, "preserved", None)
        if not isinstance(preserved, str):
            preserved = None
        numeric_fields: dict[str, int | None] = {}
        for key in ("required_bytes", "free_bytes", "shortfall_bytes"):
            value = getattr(parsed, key, None)
            numeric_fields[key] = value if type(value) is int and value >= 0 else None
        log_excerpt = getattr(parsed, "log_excerpt", None)
        if not isinstance(log_excerpt, str):
            log_excerpt = None
        retryable = getattr(parsed, "retryable", False) is True
        retry_after = _bounded_retry_after(headers.get("retry-after"))
        if retry_after is None:
            parsed_retry_after = getattr(parsed, "retry_after_seconds", None)
            if type(parsed_retry_after) is int and parsed_retry_after >= 0:
                retry_after = parsed_retry_after
        raise error_type(
            status_code,
            detail,
            retry_after,
            code=code,
            recovery=recovery,
            retryable=retryable,
            retry_time=retry_time,
            preserved=preserved,
            **numeric_fields,
            log_excerpt=log_excerpt,
            sensitive_values=(self._token,),
            operation=operation,
            endpoint=endpoint,
            request_id=headers.get("x-request-id")
            or getattr(context, "request_id", None),
        )

    def _call_generated(
        self,
        operation: Callable[..., GeneratedResponse[Any]],
        *args: object,
        headers: Mapping[str, str] | None = None,
        **kwargs: object,
    ) -> object:
        transport = _RecordingTransport(self._transport)
        try:
            with self._generated_client(transport, headers) as client:
                response = operation(*args, client=client, **kwargs)
        except RecursionError:
            if transport.response is not None:
                self._raise_http_status(
                    transport.response.status_code,
                    None,
                    transport.response.headers,
                    operation=f"{transport.request.method} {safe_endpoint(str(transport.request.url))}" if transport.request else "control.http",
                    endpoint=str(transport.request.url) if transport.request else None,
                )
            raise ControlMalformedResponse(
                "control API response exceeds the nesting limit",
                context=protocol_context(
                    operation=f"{transport.request.method} {safe_endpoint(str(transport.request.url))}" if transport.request else "control.http",
                    endpoint=str(transport.request.url) if transport.request else None,
                ),
            ) from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            if transport.response is not None:
                self._raise_http_status(
                    transport.response.status_code,
                    None,
                    transport.response.headers,
                    operation=f"{transport.request.method} {safe_endpoint(str(transport.request.url))}" if transport.request else "control.http",
                    endpoint=str(transport.request.url) if transport.request else None,
                )
            raise ControlMalformedResponse(
                "control API returned invalid JSON",
                context=protocol_context(
                    operation=f"{transport.request.method} {safe_endpoint(str(transport.request.url))}" if transport.request else "control.http",
                    endpoint=str(transport.request.url) if transport.request else None,
                ),
            ) from None
        except (AttributeError, KeyError, TypeError, ValueError):
            if transport.response is not None:
                self._raise_http_status(
                    transport.response.status_code,
                    None,
                    transport.response.headers,
                    operation=f"{transport.request.method} {safe_endpoint(str(transport.request.url))}" if transport.request else "control.http",
                    endpoint=str(transport.request.url) if transport.request else None,
                )
            raise ControlMalformedResponse(
                "control API response does not match the generated schema",
                context=protocol_context(
                    operation=f"{transport.request.method} {safe_endpoint(str(transport.request.url))}" if transport.request else "control.http",
                    endpoint=str(transport.request.url) if transport.request else None,
                ),
            ) from None
        request = transport.request
        self._raise_http_status(
            response.status_code,
            response.parsed,
            response.headers,
            operation=f"{request.method} {safe_endpoint(str(request.url))}" if request else "control.http",
            endpoint=str(request.url) if request else None,
        )
        if 200 <= response.status_code < 300 and response.parsed is not None:
            media_type = response.headers.get("content-type", "").split(";", 1)[0]
            if media_type.strip().lower() != "application/json":
                raise ControlMalformedResponse(
                    "control API returned an invalid content type"
                )
            return response.parsed
        raise ControlClientError(f"control API returned HTTP {response.status_code}")

    @classmethod
    def from_environment(cls) -> ControlClient:
        import os

        url = os.environ.get("VONK_CONTROL_URL", "")
        token = os.environ.get("VONK_CONTROL_TOKEN_FILE", "")
        if not url or not token:
            raise ControlClientError(
                "VONK_CONTROL_URL and VONK_CONTROL_TOKEN_FILE are required"
            )
        return cls(url, Path(token))

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        extra_headers: Mapping[str, str] | None = None,
        query: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if not path.startswith("/api/") or ".." in path:
            raise ControlClientError("control API path is invalid")
        route_path = path
        if query:
            path = f"{path}?{urllib.parse.urlencode(query, doseq=True)}"
        _request_contract(route_path, method, payload)
        data = None
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        if extra_headers is not None:
            headers.update(extra_headers)
        if payload is not None:
            data = json.dumps(
                payload, sort_keys=True, separators=(",", ":")
            ).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self._base + path, data=data, headers=headers, method=method
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                content = response.read(_MAX_RESPONSE + 1)
                status = response.status
                response_headers = response.headers
        except urllib.error.HTTPError as error:
            content = error.read(_MAX_RESPONSE + 1)
            status = error.code
            response_headers = error.headers
        except (OSError, urllib.error.URLError) as error:
            context = transport_context(
                operation=f"{method} {route_path}", endpoint=route_path, error=error
            )
            raise ControlTransportError(
                context.render("control API request failed"), context=context
            ) from None
        if len(content) > _MAX_RESPONSE:
            raise ControlResponseTooLarge("control API response exceeds safety limit")
        if not 200 <= status < 300:
            error_media_type = response_headers.get("content-type", "").split(";", 1)[0]
            _response_media_contract(
                route_path,
                method,
                status,
                error_media_type.strip().lower(),
                has_content=bool(content),
            )
            try:
                problem = json.loads(content)
            except (UnicodeDecodeError, json.JSONDecodeError):
                if error_media_type.strip().lower() == "application/json":
                    raise ControlMalformedResponse(
                        "control API returned invalid JSON error"
                    ) from None
                problem = None
            if error_media_type.strip().lower() == "application/json":
                if not isinstance(problem, dict):
                    raise ControlMalformedResponse(
                        "control API error does not match the OpenAPI schema"
                    )
                _response_contract(route_path, method, status, problem)
            detail = problem.get("detail") if isinstance(problem, dict) else None
            problem_context = problem.get("context") if isinstance(problem, Mapping) else None
            error_type = _STATUS_ERRORS.get(status, ControlHTTPError)
            fields = _structured_http_error_fields(problem)
            if fields.get("code") is None:
                fields["code"] = response_headers.get("x-vonk-error-code")
            retry_after = _bounded_retry_after(response_headers.get("retry-after"))
            body_retry_after = fields.pop("retry_after_seconds", None)
            if retry_after is None and type(body_retry_after) is int:
                retry_after = body_retry_after
            raise error_type(
                status,
                detail if isinstance(detail, str) else "control API request failed",
                retry_after,
                **fields,
                sensitive_values=(self._token,),
                operation=f"{method} {route_path}",
                endpoint=route_path,
                request_id=response_headers.get("x-request-id")
                or (
                    problem_context.get("request_id")
                    if isinstance(problem_context, Mapping)
                    else None
                ),
            )
        if status == 204 or not content:
            _response_contract(route_path, method, status, {})
            return {}
        response_media_type = response_headers.get("content-type", "").split(";", 1)[0]
        _response_media_contract(
            route_path,
            method,
            status,
            response_media_type.strip().lower(),
            has_content=True,
        )
        try:
            decoded = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ControlClientError("control API returned invalid JSON") from None
        if not isinstance(decoded, dict):
            raise ControlClientError("control API response must be an object")
        _response_contract(route_path, method, status, decoded)
        return decoded

    def upload_file(
        self,
        path: str,
        source: Path,
        *,
        media_type: str,
        expected_sha256: str,
        expected_size: int,
    ) -> dict[str, object]:
        """Stream one previously declared input after rechecking its identity."""
        if not path.startswith("/api/") or ".." in path:
            raise ControlClientError("control API path is invalid")
        _request_media_contract(path, "PUT", "application/octet-stream")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ControlClientError("artifact input SHA-256 is invalid")
        if not 0 <= expected_size <= _MAX_ARTIFACT_INPUT:
            raise ControlClientError("artifact input size is invalid")
        flags = (
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOINHERIT", 0)
        )
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise ControlClientError("artifact input cannot be opened safely")
        descriptor = -1
        try:
            descriptor = os.open(source, flags | no_follow)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != expected_size:
                raise ControlClientError("artifact input changed before upload")
            digest = hashlib.sha256()
            observed = 0
            while observed <= _MAX_ARTIFACT_INPUT:
                chunk = os.read(
                    descriptor,
                    min(1024**2, _MAX_ARTIFACT_INPUT + 1 - observed),
                )
                if not chunk:
                    break
                observed += len(chunk)
                digest.update(chunk)
            if observed != expected_size or digest.hexdigest() != expected_sha256:
                raise ControlClientError("artifact input changed before upload")
            os.lseek(descriptor, 0, os.SEEK_SET)
            headers = {
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "Content-Type": media_type,
                "Content-Length": str(expected_size),
                "X-Content-SHA256": expected_sha256,
            }
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                descriptor = -1
                request = urllib.request.Request(
                    self._base + path, data=stream, headers=headers, method="PUT"
                )
                try:
                    with self._opener(
                        request, timeout=self._artifact_transfer_timeout
                    ) as response:
                        content = response.read(_MAX_RESPONSE + 1)
                        status = response.status
                        response_headers = response.headers
                except urllib.error.HTTPError as error:
                    content = error.read(_MAX_RESPONSE + 1)
                    status = error.code
                    response_headers = error.headers
                except (OSError, urllib.error.URLError) as error:
                    context = transport_context(
                        operation=f"PUT {path}", endpoint=path, error=error
                    )
                    raise ControlTransportError(
                        context.render("control API request failed"), context=context
                    ) from None
        except ControlClientError:
            raise
        except OSError as error:
            context = local_io_context(
                operation="read artifact input", path=source, error=error
            )
            raise ControlClientError(
                context.render("artifact input must be a readable regular non-symlink file"),
                context=context,
            ) from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if len(content) > _MAX_RESPONSE:
            raise ControlResponseTooLarge("control API response exceeds safety limit")
        if not 200 <= status < 300:
            error_media_type = response_headers.get("content-type", "").split(";", 1)[0]
            _response_media_contract(
                path,
                "PUT",
                status,
                error_media_type.strip().lower(),
                has_content=bool(content),
            )
            try:
                problem = json.loads(content)
            except (UnicodeDecodeError, json.JSONDecodeError):
                if error_media_type.strip().lower() == "application/json":
                    raise ControlMalformedResponse(
                        "control API returned invalid JSON error"
                    ) from None
                problem = None
            if error_media_type.strip().lower() == "application/json":
                if not isinstance(problem, dict):
                    raise ControlMalformedResponse(
                        "control API error does not match the OpenAPI schema"
                    )
                _response_contract(path, "PUT", status, problem)
            detail = problem.get("detail") if isinstance(problem, dict) else None
            error_type = _STATUS_ERRORS.get(status, ControlHTTPError)
            fields = _structured_http_error_fields(problem)
            if fields.get("code") is None:
                fields["code"] = response_headers.get("x-vonk-error-code")
            body_retry_after = fields.pop("retry_after_seconds", None)
            retry_after = _bounded_retry_after(response_headers.get("retry-after"))
            if retry_after is None and type(body_retry_after) is int:
                retry_after = body_retry_after
            raise error_type(
                status,
                detail if isinstance(detail, str) else "control API request failed",
                retry_after,
                **fields,
                sensitive_values=(self._token,),
                operation=f"PUT {path}",
                endpoint=path,
                request_id=response_headers.get("x-request-id"),
            )
        try:
            decoded = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ControlClientError("control API returned invalid JSON") from None
        if not isinstance(decoded, dict):
            raise ControlClientError("control API response must be an object")
        response_media_type = response_headers.get("content-type", "").split(";", 1)[0]
        _response_media_contract(
            path,
            "PUT",
            status,
            response_media_type.strip().lower(),
            has_content=True,
        )
        _response_contract(path, "PUT", status, decoded)
        return decoded

    def download_file(
        self,
        path: str,
        destination: Path,
        *,
        media_type: str,
        expected_sha256: str,
        expected_size: int,
        overwrite: bool,
    ) -> dict[str, object]:
        """Stream, verify, and atomically publish one result file."""
        if not path.startswith("/api/") or ".." in path:
            raise ControlClientError("control API path is invalid")
        _operation(path, "GET")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ControlClientError("artifact output SHA-256 is invalid")
        if not 0 <= expected_size <= _MAX_ARTIFACT_OUTPUT:
            raise ControlClientError("artifact output size is invalid")
        parent = destination.parent
        if parent.is_symlink() or not parent.is_dir():
            raise ControlClientError("artifact output directory is invalid")
        if destination.exists() and not overwrite:
            raise ControlClientError(
                f"artifact output already exists: {destination.name}"
            )
        request = urllib.request.Request(
            self._base + path,
            headers={"Authorization": f"Bearer {self._token}", "Accept": "*/*"},
            method="GET",
        )
        temporary = Path()
        descriptor = -1
        try:
            try:
                response_context = self._opener(
                    request, timeout=self._artifact_transfer_timeout
                )
            except urllib.error.HTTPError as error:
                content = error.read(_MAX_RESPONSE + 1)
                if len(content) > _MAX_RESPONSE:
                    raise ControlResponseTooLarge(
                        "control API response exceeds safety limit"
                    )
                error_media_type = error.headers.get("content-type", "").split(";", 1)[0]
                _response_media_contract(
                    path,
                    "GET",
                    error.code,
                    error_media_type.strip().lower(),
                    has_content=bool(content),
                )
                try:
                    problem = json.loads(content)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    if error_media_type.strip().lower() == "application/json":
                        raise ControlMalformedResponse(
                            "control API returned invalid JSON error"
                        ) from None
                    problem = None
                if error_media_type.strip().lower() == "application/json":
                    if not isinstance(problem, dict):
                        raise ControlMalformedResponse(
                            "control API error does not match the OpenAPI schema"
                        )
                    _response_contract(path, "GET", error.code, problem)
                detail = problem.get("detail") if isinstance(problem, dict) else None
                error_type = _STATUS_ERRORS.get(error.code, ControlHTTPError)
                fields = _structured_http_error_fields(problem)
                if fields.get("code") is None:
                    fields["code"] = error.headers.get("x-vonk-error-code")
                retry_after = _bounded_retry_after(error.headers.get("retry-after"))
                body_retry_after = fields.pop("retry_after_seconds", None)
                if retry_after is None and type(body_retry_after) is int:
                    retry_after = body_retry_after
                raise error_type(
                    error.code,
                    detail if isinstance(detail, str) else "control API request failed",
                    retry_after,
                    **fields,
                    sensitive_values=(self._token,),
                    operation=f"GET {path}",
                    endpoint=path,
                    request_id=error.headers.get("x-request-id"),
                ) from None
            except (OSError, urllib.error.URLError) as error:
                context = transport_context(
                    operation=f"GET {path}", endpoint=path, error=error
                )
                raise ControlTransportError(
                    context.render("control API request failed"), context=context
                ) from None
            with response_context as response:
                if not 200 <= response.status < 300:
                    response_media_type = response.headers.get("content-type", "").split(
                        ";", 1
                    )[0]
                    _response_media_contract(
                        path,
                        "GET",
                        response.status,
                        response_media_type.strip().lower(),
                        has_content=True,
                    )
                    raise ControlHTTPError(
                        response.status,
                        "control API request failed",
                        sensitive_values=(self._token,),
                        operation=f"GET {path}",
                        endpoint=path,
                        request_id=response.headers.get("x-request-id"),
                    )
                _response_definition(path, "GET", response.status)
                response_type = response.headers.get("content-type", "").split(";", 1)[
                    0
                ]
                if response_type.strip().lower() != media_type:
                    raise ControlMalformedResponse(
                        "artifact output content type does not match its manifest"
                    )
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError:
                        raise ControlMalformedResponse(
                            "artifact output content length is invalid"
                        ) from None
                    if declared_length != expected_size:
                        raise ControlMalformedResponse(
                            "artifact output content length does not match its manifest"
                        )
                response_digest = response.headers.get("x-content-sha256")
                if response_digest != expected_sha256:
                    raise ControlMalformedResponse(
                        "artifact output digest header does not match its manifest"
                    )
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{destination.name}.", suffix=".download", dir=parent
                )
                temporary = Path(temporary_name)
                os.fchmod(descriptor, 0o600)
                digest = hashlib.sha256()
                observed = 0
                with os.fdopen(descriptor, "wb", closefd=True) as output:
                    descriptor = -1
                    while observed <= expected_size:
                        chunk = response.read(
                            min(1024**2, expected_size + 1 - observed)
                        )
                        if not chunk:
                            break
                        observed += len(chunk)
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if observed != expected_size or digest.hexdigest() != expected_sha256:
                    raise ControlMalformedResponse(
                        "artifact output content does not match its manifest"
                    )
            if overwrite:
                os.replace(temporary, destination)
            else:
                os.link(temporary, destination, follow_symlinks=False)
                temporary.unlink()
            temporary = Path()
        except FileExistsError:
            raise ControlClientError(
                f"artifact output already exists: {destination.name}"
            ) from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary != Path():
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
        return {
            "destination": str(destination),
            "media_type": media_type,
            "size_bytes": expected_size,
            "sha256": expected_sha256,
        }

    def get(self, path: str) -> dict[str, object]:
        return self.request("GET", path)

    def fleet(self) -> FleetSnapshot:
        return self._call_generated(get_fleet_status.sync_detailed)  # type: ignore[return-value]

    def job(self, job_id: str) -> JobDetailResponse:
        return self._call_generated(get_job.sync_detailed, job_id)  # type: ignore[return-value]

    def wait_job(
        self, job_id: str, timeout: float, interval: float
    ) -> JobDetailResponse:
        deadline = time.monotonic() + timeout
        result: JobDetailResponse | None = None
        while True:
            try:
                result = self.job(job_id)
            except (ControlTransportError, ControlUnavailable) as error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ControlTimeout(
                        job_id, result, sensitive_values=(self._token,)
                    ) from error
                delay = getattr(error, "retry_after_seconds", None)
                if delay is None:
                    delay = interval
                if delay >= remaining:
                    time.sleep(remaining)
                    raise ControlTimeout(
                        job_id, result, sensitive_values=(self._token,)
                    ) from error
                time.sleep(delay)
                continue
            if result.state == "succeeded":
                return result
            if result.state in {"expired", "failed"}:
                raise JobFailed(result, sensitive_values=(self._token,))
            if result.state == "waiting-for-operator":
                raise JobWaitingForOperator(result, sensitive_values=(self._token,))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ControlTimeout(job_id, result, sensitive_values=(self._token,))
            if interval >= remaining:
                time.sleep(remaining)
                raise ControlTimeout(job_id, result, sensitive_values=(self._token,))
            time.sleep(interval)

    def endpoint(self, alias: str) -> EndpointResponse:
        return self._call_generated(get_published_endpoint.sync_detailed, alias)  # type: ignore[return-value]
