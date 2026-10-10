"""Bounded HTTPS requests, observation transfer, and generated API calls."""

from __future__ import annotations

import binascii
import http.client
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import replace
from email.message import Message
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Never, Self

import httpx2

from ..cli_states import FAILED_JOB_STATES, OPERATOR_WAIT_STATES, SUCCEEDED
from ..control_limits import MAX_CONTROL_DOCUMENT_BYTES
from ..control_transport import open_https
from ..error_reporting import (
    protocol_context,
    safe_endpoint,
    safe_request_id,
    transport_context,
)
from ..observation_transfer_reader import (
    ObservationTransferInvalid,
    ObservationTransferUnavailable,
    receive_observation,
)

if TYPE_CHECKING:
    # Importing any generated model runs the generated package initializer,
    # which imports all of its models; that alone was most of every vonkctl
    # invocation's start-up time. Load them where a request needs them.
    from ..generated_control.client import AuthenticatedClient
    from ..generated_control.models.fleet_profile_endpoints_view import (
        FleetProfileEndpointsView,
    )
    from ..generated_control.models.fleet_snapshot import FleetSnapshot
    from ..generated_control.models.job_detail_response import JobDetailResponse
    from ..generated_control.types import Response as GeneratedResponse


from .common import observation_delay, observation_unknown
from .errors import (
    _STATUS_ERRORS,
    ControlClientError,
    ControlHTTPError,
    ControlMalformedResponse,
    ControlObservationUnavailable,
    ControlResponseTooLarge,
    ControlTimeout,
    ControlTransportError,
    JobFailed,
    JobWaitingForOperator,
    _OpenedResponse,
    _retry_after_seconds,
    _structured_http_error_fields,
)
from .schema import (
    _request_contract,
    _response_contract,
    _response_definition,
    _response_media_contract,
    _validate_schema,
    validate_control_document,
)
from .transport import (
    _observation_payload,
    _OpenerTransport,
    _read_control_response,
    _read_token_file,
    _RecordingTransport,
)


class ControlClient:
    def __init__(
        self,
        base_url: str,
        token_file: Path,
        *,
        opener: Callable[..., _OpenedResponse] | None = None,
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
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ControlClientError("request timeout must be finite and positive")
        if not 1 <= artifact_transfer_timeout_seconds <= 3_600:
            raise ControlClientError(
                "artifact transfer timeout must be between 1 and 3600 seconds"
            )
        self._base = base_url.rstrip("/")
        self._token = token
        self._opener = opener if opener is not None else open_https
        self._transport = _OpenerTransport(self._opener, timeout_seconds)
        self._timeout = timeout_seconds
        self._artifact_transfer_timeout = artifact_transfer_timeout_seconds

    @property
    def request_timeout_seconds(self) -> float:
        """The per-request budget used to bound multi-call CLI submissions."""

        return self._timeout

    def _generated_client(
        self,
        transport: httpx2.BaseTransport,
        headers: Mapping[str, str] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> AuthenticatedClient:
        from ..generated_control.client import AuthenticatedClient

        return AuthenticatedClient(
            base_url=self._base,
            token=self._token,
            headers={"Accept": "application/json", **dict(headers or {})},
            timeout=httpx2.Timeout(timeout_seconds or self._timeout),
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
        code = getattr(parsed, "reason", getattr(parsed, "code", None))
        if not isinstance(code, str) or not code:
            code = getattr(parsed, "error_code", None)
        if (not isinstance(code, str) or not code) and context is not None:
            code = getattr(context, "code", None)
        if not isinstance(code, str) or not code:
            code = headers.get("x-vonk-error-code")
        if not isinstance(code, str) or not code:
            code = None
        recovery_value = getattr(
            parsed, "recovery_actions", getattr(parsed, "recovery", ())
        )
        if isinstance(recovery_value, str):
            recovery = (recovery_value,)
        elif isinstance(recovery_value, (list, tuple)):
            recovery = tuple(item for item in recovery_value if isinstance(item, str))[
                :8
            ]
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
        retryable = status_code == 429 or 500 <= status_code <= 599
        candidates = getattr(parsed, "candidates", None)
        retry_after = _retry_after_seconds(headers.get("retry-after"))
        if retry_after is None:
            parsed_retry_after = getattr(
                parsed, "retry_after", getattr(parsed, "retry_after_seconds", None)
            )
            if type(parsed_retry_after) is int and parsed_retry_after >= 0:
                retry_after = parsed_retry_after
        raise error_type(
            status_code,
            detail,
            retry_after,
            code=code,
            recovery=recovery,
            candidates=tuple(candidates) if isinstance(candidates, list) else (),
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
        timeout_seconds: float | None = None,
        **kwargs: object,
    ) -> object:
        timeout = self._request_timeout(timeout_seconds)
        deadline = time.monotonic() + timeout
        attempt = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ControlTransportError("control observation deadline elapsed")
            transport = _RecordingTransport(
                _OpenerTransport(self._opener, remaining, deadline=deadline)
            )
            try:
                result = self._call_generated_once(
                    operation,
                    *args,
                    headers=headers,
                    timeout_seconds=remaining,
                    recording_transport=transport,
                    **kwargs,
                )
                if time.monotonic() >= deadline:
                    raise ControlTransportError("control observation deadline elapsed")
                return result
            except ControlClientError as error:
                if (
                    isinstance(error, ControlMalformedResponse)
                    and transport.response is not None
                ):
                    error.retry_after_seconds = _retry_after_seconds(
                        transport.response.headers.get("retry-after")
                    )
                # Generated functions declare their method through _get_kwargs;
                # the actual request is recorded below, rather than inferred
                # from an endpoint's name. Only read-only calls can be retried.
                if (
                    transport.request is None
                    or (
                        transport.request.method != "GET"
                        and transport.request_validated
                    )
                    or not observation_unknown(error)
                ):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(observation_delay(error, attempt, remaining))
                attempt += 1
                if time.monotonic() >= deadline:
                    raise

    def _request_timeout(self, requested: float | None) -> float:
        if requested is not None and (not math.isfinite(requested) or requested <= 0):
            raise ControlClientError("request timeout must be finite and positive")
        return self._timeout if requested is None else min(self._timeout, requested)

    def _call_generated_once(
        self,
        operation: Callable[..., GeneratedResponse[Any]],
        *args: object,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        recording_transport: _RecordingTransport | None = None,
        **kwargs: object,
    ) -> object:
        timeout = min(self._timeout, timeout_seconds or self._timeout)
        transport = recording_transport or _RecordingTransport(
            _OpenerTransport(self._opener, timeout)
        )
        try:
            with self._generated_client(
                transport, headers, timeout_seconds=timeout
            ) as client:
                response = operation(*args, client=client, **kwargs)
        except (
            RecursionError,
            UnicodeDecodeError,
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
        ):
            request = transport.request
            received = transport.response
            raise ControlMalformedResponse(
                "control API response does not match the generated schema",
                context=replace(
                    protocol_context(
                        operation=f"{request.method} {safe_endpoint(str(request.url))}"
                        if request
                        else "control.http",
                        endpoint=str(request.url) if request else None,
                    ),
                    http_status=received.status_code if received is not None else None,
                    request_id=safe_request_id(received.headers.get("x-request-id"))
                    if received is not None
                    else None,
                ),
            ) from None
        request = transport.request
        self._raise_http_status(
            response.status_code,
            response.parsed,
            response.headers,
            operation=f"{request.method} {safe_endpoint(str(request.url))}"
            if request
            else "control.http",
            endpoint=str(request.url) if request else None,
        )
        if 200 <= response.status_code < 300 and response.parsed is not None:
            media_type = response.headers.get("content-type", "").split(";", 1)[0]
            if media_type.strip().lower() != "application/json":
                raise ControlMalformedResponse(
                    "control API returned an invalid content type"
                )
            return response.parsed
        raise ControlMalformedResponse("control API returned no canonical receipt")

    @classmethod
    def from_environment(cls) -> Self:

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
        timeout_seconds: float | None = None,
        retry: bool = True,
    ) -> dict[str, object]:
        timeout = self._request_timeout(timeout_seconds)
        if not path.startswith("/api/") or ".." in path:
            raise ControlClientError("control API path is invalid")
        route_path = path
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
        operation = f"{method} {route_path}"[:160]
        deadline = time.monotonic() + timeout
        attempt = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ControlTransportError("control observation deadline elapsed")
            status = None
            response_headers = Message()
            dispatched = False
            try:
                _request_contract(route_path, method, payload)
                observation_payload = _observation_payload(route_path, method)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ControlTransportError("control observation deadline elapsed")
                dispatched = True
                if observation_payload is not None:
                    result = self._read_observation(
                        request,
                        remaining,
                        route_path,
                        observation_payload,
                        validate_payload=partial(
                            validate_control_document, observation_payload
                        ),
                    )
                else:
                    status, content, response_headers = _read_control_response(
                        self._opener, request, remaining
                    )
                    result = self._request_response(
                        method, route_path, status, content, response_headers
                    )
                if time.monotonic() >= deadline:
                    raise ControlTransportError("control observation deadline elapsed")
                return result
            except ControlClientError as error:
                if (
                    isinstance(error, ControlMalformedResponse)
                    and error.context is None
                ):
                    error.context = replace(
                        protocol_context(operation=operation, endpoint=route_path),
                        http_status=status,
                        request_id=safe_request_id(
                            response_headers.get("x-request-id")
                        ),
                    )
                    error.retry_after_seconds = _retry_after_seconds(
                        response_headers.get("retry-after")
                    )
                if (
                    not retry
                    or (method != "GET" and dispatched)
                    or not observation_unknown(error)
                ):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(observation_delay(error, attempt, remaining))
                attempt += 1
                if time.monotonic() >= deadline:
                    raise

    def _read_observation[Receipt](
        self,
        request: urllib.request.Request,
        timeout: float,
        path: str,
        payload: str,
        *,
        validate_payload: Callable[[object], Receipt],
    ) -> Receipt:
        """Verify one frozen whole observation before returning any domain fact.

        Only the declared streaming routes bypass the whole-document allocation.
        Every record is bounded before retention; bytes are spooled, with no
        invented aggregate size cap or claim that the final model has bounded RAM.
        """
        deadline = time.monotonic() + timeout
        media_type = "application/x-vonk-observation+ndjson"
        request.add_header("Accept", media_type)
        status: int | None = None
        request_id: str | None = None
        received_retry_after: int | None = None
        try:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("observation attempt deadline elapsed")
                response = self._opener(request, timeout=remaining)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                status = (
                    response.code
                    if isinstance(response, urllib.error.HTTPError)
                    else response.status
                )
                request_id = safe_request_id(response.headers.get("x-request-id"))
                received_retry_after = _retry_after_seconds(
                    response.headers.get("retry-after")
                )
                if status in (401, 403):
                    raise _STATUS_ERRORS[status](
                        status,
                        "control API authorization denied",
                        received_retry_after,
                        code=response.headers.get("x-vonk-error-code"),
                        operation=f"GET {path}",
                        endpoint=path,
                        request_id=request_id,
                    )
                if not 200 <= status < 300:
                    self._raise_http_error(
                        "GET",
                        path,
                        status,
                        response.read(MAX_CONTROL_DOCUMENT_BYTES + 1),
                        response.headers,
                    )
                actual_media = (
                    response.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                _response_media_contract(
                    path, "GET", status, actual_media, has_content=True
                )
                if actual_media != media_type:
                    raise ValueError("observation transfer content type differs")
                content = _response_definition(path, "GET", status).get("content")
                if not isinstance(content, dict) or not isinstance(
                    content.get(media_type), dict
                ):
                    raise TypeError("observation transfer schema is unavailable")
                record_media = content[media_type]
                if not isinstance(record_media, dict):
                    raise TypeError("observation transfer schema is unavailable")
                record_schema = record_media.get("schema")

                def schema_failure(
                    error: ControlClientError,
                ) -> ControlMalformedResponse:
                    # Only the owning canonical validators reach this boundary.
                    # Their bounded reasons name schema rules, not payload values.
                    return ControlMalformedResponse(
                        f"Complete observation unavailable: {error}",
                        context=replace(
                            protocol_context(operation=f"GET {path}", endpoint=path),
                            http_status=status,
                            request_id=request_id,
                        ),
                    )

                def validate_record(record: object) -> None:
                    try:
                        _validate_schema(
                            record,
                            record_schema,
                            message="observation record violates canonical schema",
                        )
                    except ControlClientError as error:
                        raise schema_failure(error) from None

                def decode_payload(document: object) -> Receipt:
                    try:
                        return validate_payload(document)
                    except ControlClientError as error:
                        raise schema_failure(error) from None

                return receive_observation(
                    response,
                    resource="fleet" if payload == "FleetSnapshot" else "platform",
                    record_max_bytes=MAX_CONTROL_DOCUMENT_BYTES,
                    deadline=deadline,
                    validate_record=validate_record,
                    validate_payload=decode_payload,
                )
        except ObservationTransferUnavailable as error:
            raise ControlObservationUnavailable(
                error.reason_code,
                error.detail,
                context=replace(
                    protocol_context(operation=f"GET {path}", endpoint=path),
                    http_status=status,
                    request_id=request_id,
                ),
            ) from None
        except ObservationTransferInvalid as error:
            raise ControlMalformedResponse(
                f"Complete observation unavailable: {error}",
                retry_after_seconds=received_retry_after,
                context=replace(
                    protocol_context(operation=f"GET {path}", endpoint=path),
                    http_status=status,
                    request_id=request_id,
                ),
            ) from None
        except (ControlHTTPError, ControlObservationUnavailable):
            raise
        except (ControlMalformedResponse, ControlResponseTooLarge) as error:
            # The bounded HTTP error parser and canonical validators already
            # classified this failure. Preserve that cause and attach only the
            # status/correlation evidence received before body validation.
            error.retry_after_seconds = received_retry_after
            if error.context is None:
                error.context = replace(
                    protocol_context(operation=f"GET {path}", endpoint=path),
                    http_status=status,
                    request_id=request_id,
                )
            raise
        except (OSError, urllib.error.URLError, http.client.HTTPException) as error:
            context = replace(
                transport_context(operation=f"GET {path}", endpoint=path, error=error),
                http_status=status,
                request_id=request_id,
            )
            raise ControlTransportError(
                context.render("observation transfer failed"),
                context=context,
                retry_after_seconds=received_retry_after,
            ) from None
        except (
            ValueError,
            TypeError,
            RecursionError,
            binascii.Error,
            ControlClientError,
        ):
            context = replace(
                protocol_context(operation=f"GET {path}", endpoint=path),
                http_status=status,
                request_id=request_id,
            )
            raise ControlMalformedResponse(
                "Complete observation unavailable: transfer or canonical payload validation failed",
                retry_after_seconds=received_retry_after,
                context=context,
            ) from None

    def _raise_http_error(
        self,
        method: str,
        route_path: str,
        status: int,
        content: bytes,
        response_headers: Message,
    ) -> Never:
        """Parse the bounded non-success body and always raise its owning error."""

        if len(content) > MAX_CONTROL_DOCUMENT_BYTES:
            raise ControlResponseTooLarge("control API response exceeds safety limit")
        try:
            problem = json.loads(content)
        except (RecursionError, UnicodeDecodeError, json.JSONDecodeError):
            problem = None
        detail = problem.get("detail") if isinstance(problem, dict) else None
        problem_context = (
            problem.get("context") if isinstance(problem, Mapping) else None
        )
        error_type = _STATUS_ERRORS.get(status, ControlHTTPError)
        fields, body_retry_after = _structured_http_error_fields(problem)
        if fields.get("code") is None:
            fields["code"] = response_headers.get("x-vonk-error-code")
        retry_after = _retry_after_seconds(response_headers.get("retry-after"))
        if retry_after is None and type(body_retry_after) is int:
            retry_after = body_retry_after
        raise error_type(
            status,
            detail if isinstance(detail, str) else "control API request failed",
            retry_after,
            **fields,
            sensitive_values=(self._token,),
            operation=f"{method} {route_path}"[:160],
            endpoint=route_path,
            request_id=response_headers.get("x-request-id")
            or (
                problem_context.get("request_id")
                if isinstance(problem_context, Mapping)
                else None
            ),
        )

    def _request_response(
        self,
        method: str,
        route_path: str,
        status: int,
        content: bytes,
        response_headers: Message,
    ) -> dict[str, object]:
        """Interpret the bounded body while the caller retains HTTP evidence."""

        if not 200 <= status < 300:
            self._raise_http_error(
                method, route_path, status, content, response_headers
            )
        if len(content) > MAX_CONTROL_DOCUMENT_BYTES:
            raise ControlResponseTooLarge("control API response exceeds safety limit")
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
        except RecursionError:
            raise ControlMalformedResponse(
                "control API response exceeds the nesting limit"
            ) from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ControlMalformedResponse(
                "control API returned invalid JSON"
            ) from None
        if not isinstance(decoded, dict):
            raise ControlMalformedResponse("control API response must be an object")
        _response_contract(route_path, method, status, decoded)
        return decoded

    def get(self, path: str) -> dict[str, object]:
        return self.request("GET", path)

    def fleet(self) -> FleetSnapshot:
        from ..generated_control.models.fleet_snapshot import FleetSnapshot

        return FleetSnapshot.from_dict(self.request("GET", "/api/fleet"))

    def job(
        self, job_id: str, *, timeout_seconds: float | None = None
    ) -> JobDetailResponse:
        from ..generated_control.api.default import get_job

        return self._call_generated(
            get_job.sync_detailed, job_id, timeout_seconds=timeout_seconds
        )  # type: ignore[return-value]

    def wait_job(
        self, job_id: str, timeout: float, interval: float
    ) -> JobDetailResponse:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ControlClientError("wait timeout must be finite and positive")
        if not math.isfinite(interval) or interval <= 0:
            raise ControlClientError("wait interval must be finite and positive")
        deadline = time.monotonic() + timeout
        result: JobDetailResponse | None = None
        failures = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ControlTimeout(job_id, result, sensitive_values=(self._token,))
            try:
                candidate = self.job(job_id, timeout_seconds=remaining)
                if candidate.id != job_id:
                    raise ControlMalformedResponse(
                        "job observation identifies another job"
                    )
                if time.monotonic() >= deadline:
                    raise ControlTimeout(
                        job_id, result, sensitive_values=(self._token,)
                    )
                result = candidate
            except ControlClientError as error:
                if not observation_unknown(error):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ControlTimeout(
                        job_id, result, sensitive_values=(self._token,)
                    ) from error
                time.sleep(observation_delay(error, failures, remaining))
                failures += 1
                continue
            failures = 0
            if result.state == SUCCEEDED:
                return result
            if result.state in FAILED_JOB_STATES:
                raise JobFailed(result, sensitive_values=(self._token,))
            if result.state in OPERATOR_WAIT_STATES:
                raise JobWaitingForOperator(result, sensitive_values=(self._token,))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ControlTimeout(job_id, result, sensitive_values=(self._token,))
            if interval >= remaining:
                time.sleep(remaining)
                raise ControlTimeout(job_id, result, sensitive_values=(self._token,))
            time.sleep(interval)

    def profile_endpoints(
        self, number: int, alias: str | None = None
    ) -> FleetProfileEndpointsView:
        from ..generated_control.api.default import get_profile_endpoints

        return self._call_generated(
            get_profile_endpoints.sync_detailed, number, alias=alias
        )  # type: ignore[return-value]
