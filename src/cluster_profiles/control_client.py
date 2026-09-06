"""Bounded HTTPS client for normal control-plane administration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

from .generated_control.api.default import (
    get_job,
    get_node_statuses,
    get_published_endpoint,
    list_agents,
)
from .generated_control.client import AuthenticatedClient
from .generated_control.models.agents_response import AgentsResponse
from .generated_control.models.endpoint_response import EndpointResponse
from .generated_control.models.fleet_status_response import FleetStatusResponse
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


class ControlClientError(RuntimeError):
    pass


class ControlMalformedResponse(ControlClientError):
    pass


class ControlResponseTooLarge(ControlClientError):
    pass


class ControlTransportError(ControlClientError):
    pass


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
        retry_time: str | None = None,
        preserved: str | None = None,
        required_bytes: int | None = None,
        free_bytes: int | None = None,
        shortfall_bytes: int | None = None,
        log_excerpt: str | None = None,
        sensitive_values: tuple[str, ...] = (),
    ) -> None:
        self.status_code = status_code
        self.code = code or f"http.{status_code}"
        self.recovery = recovery
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
        super().__init__(f"control API returned HTTP {status_code}: {self.detail}")


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


def _structured_http_error_fields(problem: object) -> dict[str, object]:
    """Extract optional shared availability error metadata without exposing secrets."""
    if not isinstance(problem, Mapping):
        return {}
    code = problem.get("code", problem.get("error_code"))
    recovery = problem.get("recovery_actions", problem.get("recovery", ()))
    if isinstance(recovery, str):
        recovery = (recovery,)
    elif isinstance(recovery, list):
        recovery = tuple(item for item in recovery if isinstance(item, str))[:8]
    else:
        recovery = ()
    retry_time = problem.get("retry_time", problem.get("retry_at"))
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
    except OSError:
        raise ControlClientError(
            "control token must be a regular non-symlink file"
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
            raise ControlTransportError(
                f"control API request failed: {type(error).__name__}"
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
        self.response: httpx.Response | None = None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self._transport.handle_request(request)
        self.response = response
        if len(response.content) > _MAX_RESPONSE:
            raise ControlResponseTooLarge("control API response exceeds safety limit")
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
    ) -> None:
        error_type = _STATUS_ERRORS.get(status_code)
        if error_type is None:
            return
        detail = getattr(parsed, "detail", "control API request failed")
        if not isinstance(detail, str):
            detail = "control API request failed"
        code = getattr(parsed, "code", None)
        if not isinstance(code, str) or not code:
            code = getattr(parsed, "error_code", None)
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
        raise error_type(
            status_code,
            detail,
            _bounded_retry_after(headers.get("retry-after")),
            code=code,
            recovery=recovery,
            retry_time=retry_time,
            preserved=preserved,
            **numeric_fields,
            log_excerpt=log_excerpt,
            sensitive_values=(self._token,),
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
                )
            raise ControlMalformedResponse(
                "control API response exceeds the nesting limit"
            ) from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            if transport.response is not None:
                self._raise_http_status(
                    transport.response.status_code,
                    None,
                    transport.response.headers,
                )
            raise ControlMalformedResponse(
                "control API returned invalid JSON"
            ) from None
        except (AttributeError, KeyError, TypeError, ValueError):
            if transport.response is not None:
                self._raise_http_status(
                    transport.response.status_code,
                    None,
                    transport.response.headers,
                )
            raise ControlMalformedResponse(
                "control API response does not match the generated schema"
            ) from None
        self._raise_http_status(response.status_code, response.parsed, response.headers)
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
        if not path.startswith("/api/v1/") or ".." in path:
            raise ControlClientError("control API path is invalid")
        if query:
            path = f"{path}?{urllib.parse.urlencode(query, doseq=True)}"
        data = None
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        if extra_headers is not None:
            headers.update(extra_headers)
        if payload is not None:
            data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
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
            raise ControlClientError(
                f"control API request failed: {type(error).__name__}"
            ) from None
        if len(content) > _MAX_RESPONSE:
            raise ControlResponseTooLarge("control API response exceeds safety limit")
        if not 200 <= status < 300:
            try:
                problem = json.loads(content)
            except (UnicodeDecodeError, json.JSONDecodeError):
                problem = None
            detail = problem.get("detail") if isinstance(problem, dict) else None
            error_type = _STATUS_ERRORS.get(status, ControlHTTPError)
            fields = _structured_http_error_fields(problem)
            raise error_type(
                status,
                detail if isinstance(detail, str) else "control API request failed",
                _bounded_retry_after(response_headers.get("retry-after")),
                **fields,
                sensitive_values=(self._token,),
            )
        if status == 204 or not content:
            return {}
        try:
            decoded = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ControlClientError("control API returned invalid JSON") from None
        if not isinstance(decoded, dict):
            raise ControlClientError("control API response must be an object")
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
        if not path.startswith("/api/v1/") or ".." in path:
            raise ControlClientError("control API path is invalid")
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
                    raise ControlClientError(
                        f"control API request failed: {type(error).__name__}"
                    ) from None
        except ControlClientError:
            raise
        except OSError:
            raise ControlClientError(
                "artifact input must be a readable regular non-symlink file"
            ) from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if len(content) > _MAX_RESPONSE:
            raise ControlResponseTooLarge("control API response exceeds safety limit")
        if not 200 <= status < 300:
            try:
                problem = json.loads(content)
            except (UnicodeDecodeError, json.JSONDecodeError):
                problem = None
            detail = problem.get("detail") if isinstance(problem, dict) else None
            error_type = _STATUS_ERRORS.get(status, ControlHTTPError)
            raise error_type(
                status,
                detail if isinstance(detail, str) else "control API request failed",
                _bounded_retry_after(response_headers.get("retry-after")),
                sensitive_values=(self._token,),
            )
        try:
            decoded = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ControlClientError("control API returned invalid JSON") from None
        if not isinstance(decoded, dict):
            raise ControlClientError("control API response must be an object")
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
        if not path.startswith("/api/v1/") or ".." in path:
            raise ControlClientError("control API path is invalid")
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
                try:
                    problem = json.loads(content)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    problem = None
                detail = problem.get("detail") if isinstance(problem, dict) else None
                error_type = _STATUS_ERRORS.get(error.code, ControlHTTPError)
                fields = _structured_http_error_fields(problem)
                raise error_type(
                    error.code,
                    detail if isinstance(detail, str) else "control API request failed",
                    _bounded_retry_after(error.headers.get("retry-after")),
                    **fields,
                    sensitive_values=(self._token,),
                ) from None
            except (OSError, urllib.error.URLError) as error:
                raise ControlClientError(
                    f"control API request failed: {type(error).__name__}"
                ) from None
            with response_context as response:
                if not 200 <= response.status < 300:
                    raise ControlHTTPError(
                        response.status,
                        "control API request failed",
                        sensitive_values=(self._token,),
                    )
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

    def create_proposal(self, payload: Mapping[str, object]) -> dict[str, object]:
        return self.request("POST", "/api/v1/proposals", payload)

    def get(self, path: str) -> dict[str, object]:
        return self.request("GET", path)

    def submit_change(self, digest: str) -> dict[str, object]:
        return self.request("POST", "/api/v1/changes", {"proposal_digest": digest})

    def nodes(self) -> FleetStatusResponse:
        return self._call_generated(get_node_statuses.sync_detailed)  # type: ignore[return-value]

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

    def agents(self) -> AgentsResponse:
        return self._call_generated(list_agents.sync_detailed)  # type: ignore[return-value]
