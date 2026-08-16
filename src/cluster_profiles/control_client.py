"""Bounded HTTPS client for normal control-plane administration."""

from __future__ import annotations

import json
import os
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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
_PLATFORM_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_UPDATE_RELEASE = re.compile(
    r"platform/releases/"
    r"(?P<version>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))/"
    r"(?P<sha256>[0-9a-f]{64})\.json\Z"
)
_RAW_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CANDIDATE_ID = re.compile(
    r"(?:[0-9a-f]{64}|[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\Z"
)
_MAX_UPDATE_NODES = 1024


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


def _json_object_copy(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ControlMalformedResponse(f"control API {label} must be an object")
    try:
        copied = json.loads(json.dumps(value, sort_keys=True, separators=(",", ":")))
    except (RecursionError, TypeError, ValueError):
        raise ControlMalformedResponse(
            f"control API {label} contains invalid JSON"
        ) from None
    if not isinstance(copied, dict):
        raise ControlMalformedResponse(f"control API {label} must be an object")
    return copied


def _required_fields(
    document: Mapping[str, object], required: set[str], label: str
) -> None:
    missing = required - set(document)
    if missing:
        raise ControlMalformedResponse(
            f"control API {label} is missing required fields"
        )


def _platform_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _PLATFORM_DIGEST.fullmatch(value) is None:
        raise ControlMalformedResponse(f"control API {label} digest is invalid")
    return value


def _bounded_strings(value: object, label: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > _MAX_UPDATE_NODES
        or any(
            not isinstance(item, str) or not item or len(item) > 256 for item in value
        )
    ):
        raise ControlMalformedResponse(f"control API {label} list is invalid")
    return tuple(value)


def _update_target(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ControlMalformedResponse(f"control API {label} target is invalid")
    _required_fields(
        value,
        {
            "build_digest",
            "platform_version",
            "protocol_maximum",
            "protocol_minimum",
            "release",
            "release_digest",
            "target_sha256",
            "tuf_targets_version",
        },
        f"{label} target",
    )
    release = value["release"]
    match = _UPDATE_RELEASE.fullmatch(release) if isinstance(release, str) else None
    target_sha256 = value["target_sha256"]
    release_digest = value["release_digest"]
    if (
        match is None
        or not isinstance(target_sha256, str)
        or _RAW_SHA256.fullmatch(target_sha256) is None
        or target_sha256 != match.group("sha256")
        or value["platform_version"] != match.group("version")
        or release_digest != f"sha256:{target_sha256}"
    ):
        raise ControlMalformedResponse(
            f"control API {label} target identity is invalid"
        )
    targets_version = value["tuf_targets_version"]
    if (
        isinstance(targets_version, bool)
        or not isinstance(targets_version, int)
        or not 1 <= targets_version <= 2_147_483_647
    ):
        raise ControlMalformedResponse(
            f"control API {label} targets metadata version is invalid"
        )
    protocol_minimum = value["protocol_minimum"]
    protocol_maximum = value["protocol_maximum"]
    if (
        isinstance(protocol_minimum, bool)
        or not isinstance(protocol_minimum, int)
        or isinstance(protocol_maximum, bool)
        or not isinstance(protocol_maximum, int)
        or not 1 <= protocol_minimum <= protocol_maximum <= 65_535
    ):
        raise ControlMalformedResponse(
            f"control API {label} target protocol range is invalid"
        )
    _platform_digest(value["build_digest"], f"{label} build")
    _platform_digest(release_digest, f"{label} release")
    return value


@dataclass(frozen=True)
class UpdateSkewResponse:
    """Typed, immutable view of the server-authoritative fleet skew report."""

    digest: str
    prompt_required: bool
    affected_nodes: tuple[str, ...]
    offline_pending: tuple[str, ...]
    incompatible_nodes: tuple[str, ...]
    _document: dict[str, object]

    @classmethod
    def from_dict(cls, value: object) -> UpdateSkewResponse:
        document = _json_object_copy(value, "update skew response")
        _required_fields(
            document,
            {
                "affected_nodes",
                "digest",
                "incompatible_nodes",
                "nodes",
                "offline_pending",
                "prompt_required",
                "target",
            },
            "update skew response",
        )
        if not isinstance(document["prompt_required"], bool):
            raise ControlMalformedResponse(
                "control API update skew prompt state is invalid"
            )
        nodes = document["nodes"]
        if (
            not isinstance(nodes, list)
            or len(nodes) > _MAX_UPDATE_NODES
            or any(not isinstance(item, dict) for item in nodes)
        ):
            raise ControlMalformedResponse("control API update skew nodes are invalid")
        _update_target(document["target"], "update skew")
        return cls(
            digest=_platform_digest(document["digest"], "update skew"),
            prompt_required=document["prompt_required"],
            affected_nodes=_bounded_strings(
                document["affected_nodes"], "affected update nodes"
            ),
            offline_pending=_bounded_strings(
                document["offline_pending"], "offline update nodes"
            ),
            incompatible_nodes=_bounded_strings(
                document["incompatible_nodes"], "incompatible update nodes"
            ),
            _document=document,
        )

    def to_dict(self) -> dict[str, object]:
        return _json_object_copy(self._document, "update skew response")


@dataclass(frozen=True)
class UpdatePlanResponse:
    """Typed server-authored update plan; clients never reconstruct its digest."""

    plan_digest: str
    canary_node: str | None
    batches: tuple[tuple[str, ...], ...]
    offline_pending: tuple[str, ...]
    incompatible: tuple[str, ...]
    soak_seconds: int
    _document: dict[str, object]

    @classmethod
    def from_dict(cls, value: object) -> UpdatePlanResponse:
        document = _json_object_copy(value, "update plan response")
        _required_fields(
            document,
            {
                "batches",
                "canary_node",
                "incompatible",
                "offline_pending",
                "plan_digest",
                "soak_seconds",
                "target",
            },
            "update plan response",
        )
        batches_value = document["batches"]
        if (
            not isinstance(batches_value, list)
            or len(batches_value) > _MAX_UPDATE_NODES
        ):
            raise ControlMalformedResponse(
                "control API update plan batches are invalid"
            )
        batches = tuple(
            _bounded_strings(batch, "update batch") for batch in batches_value
        )
        if sum(len(batch) for batch in batches) > _MAX_UPDATE_NODES:
            raise ControlMalformedResponse("control API update plan is too large")
        canary = document["canary_node"]
        if canary is not None and (not isinstance(canary, str) or not canary):
            raise ControlMalformedResponse("control API update canary is invalid")
        soak = document["soak_seconds"]
        if (
            isinstance(soak, bool)
            or not isinstance(soak, int)
            or not 0 <= soak <= 86_400
        ):
            raise ControlMalformedResponse("control API update soak is invalid")
        _update_target(document["target"], "update plan")
        return cls(
            plan_digest=_platform_digest(document["plan_digest"], "update plan"),
            canary_node=canary,
            batches=batches,
            offline_pending=_bounded_strings(
                document["offline_pending"], "offline update nodes"
            ),
            incompatible=_bounded_strings(
                document["incompatible"], "incompatible update nodes"
            ),
            soak_seconds=soak,
            _document=document,
        )

    def to_dict(self) -> dict[str, object]:
        return _json_object_copy(self._document, "update plan response")


@dataclass(frozen=True)
class UpdateRolloutResponse:
    """Typed projection of one durable GPU node platform rollout."""

    id: str
    state: str
    plan_digest: str
    can_approve_resume: bool
    resume_required: bool
    required_action: str | None
    _document: dict[str, object]

    @classmethod
    def from_dict(cls, value: object) -> UpdateRolloutResponse:
        document = _json_object_copy(value, "update rollout response")
        _required_fields(
            document,
            {
                "can_approve_resume",
                "id",
                "plan_digest",
                "required_action",
                "resume_required",
                "state",
            },
            "update rollout response",
        )
        rollout_id = document["id"]
        try:
            canonical_id = str(uuid.UUID(rollout_id))  # type: ignore[arg-type]
        except (AttributeError, TypeError, ValueError):
            raise ControlMalformedResponse(
                "control API update rollout ID is invalid"
            ) from None
        if canonical_id != rollout_id:
            raise ControlMalformedResponse("control API update rollout ID is invalid")
        state = document["state"]
        if not isinstance(state, str) or not state or len(state) > 64:
            raise ControlMalformedResponse(
                "control API update rollout state is invalid"
            )
        can_resume = document["can_approve_resume"]
        resume_required = document["resume_required"]
        required_action = document["required_action"]
        if not isinstance(can_resume, bool) or not isinstance(resume_required, bool):
            raise ControlMalformedResponse(
                "control API update rollout permission state is invalid"
            )
        if (
            required_action not in {None, "authorize-rollback", "approve-resume"}
            or (required_action is None) == resume_required
        ):
            raise ControlMalformedResponse(
                "control API update rollout recovery action is invalid"
            )
        return cls(
            id=canonical_id,
            state=state,
            plan_digest=_platform_digest(document["plan_digest"], "update rollout"),
            can_approve_resume=can_resume,
            resume_required=resume_required,
            required_action=required_action,
            _document=document,
        )

    def to_dict(self) -> dict[str, object]:
        return _json_object_copy(self._document, "update rollout response")


@dataclass(frozen=True)
class PackagePlanResponse:
    """An immutable package preview whose digest is authored by control."""

    digest: str
    release_digest: str | None
    candidate_id: str | None
    deployment_id: str | None
    validation_id: str | None
    state: str
    _document: dict[str, object]

    @classmethod
    def from_dict(cls, value: object) -> PackagePlanResponse:
        document = _json_object_copy(value, "package plan response")
        _required_fields(document, {"digest", "state"}, "package plan response")
        digest = _platform_digest(document["digest"], "package plan")
        state = document["state"]
        if not isinstance(state, str) or not 0 < len(state) <= 64:
            raise ControlMalformedResponse("control API package plan state is invalid")
        release = document.get("release_digest")
        if release is not None:
            release = _platform_digest(release, "package release")
        candidate_id = document.get("candidate_id")
        if candidate_id is not None and (
            not isinstance(candidate_id, str) or _CANDIDATE_ID.fullmatch(candidate_id) is None
        ):
            raise ControlMalformedResponse("control API package candidate ID is invalid")
        deployment_id = document.get("deployment_id")
        if deployment_id is not None and (
            not isinstance(deployment_id, str) or not deployment_id or len(deployment_id) > 128
        ):
            raise ControlMalformedResponse("control API package deployment ID is invalid")
        validation_id = document.get("validation_id")
        if validation_id is not None:
            try:
                parsed_validation_id = str(uuid.UUID(validation_id))
            except (AttributeError, TypeError, ValueError):
                raise ControlMalformedResponse("control API validation ID is invalid") from None
            if parsed_validation_id != validation_id:
                raise ControlMalformedResponse("control API validation ID is invalid")
        else:
            parsed_validation_id = None
        return cls(digest, release, candidate_id, deployment_id, parsed_validation_id, state, document)

    def to_dict(self) -> dict[str, object]:
        return _json_object_copy(self._document, "package plan response")


@dataclass(frozen=True)
class PackagePromotionResponse:
    """Typed projection of an accepted, independently versioned release."""

    candidate_id: str
    release_digest: str
    digest: str
    state: str
    _document: dict[str, object]

    @classmethod
    def from_dict(cls, value: object) -> PackagePromotionResponse:
        document = _json_object_copy(value, "package promotion response")
        _required_fields(
            document,
            {"candidate_id", "release_digest", "digest", "state"},
            "package promotion response",
        )
        candidate_id = document["candidate_id"]
        state = document["state"]
        if not isinstance(candidate_id, str) or _CANDIDATE_ID.fullmatch(candidate_id) is None:
            raise ControlMalformedResponse("control API package candidate ID is invalid")
        if not isinstance(state, str) or not 0 < len(state) <= 64:
            raise ControlMalformedResponse("control API package promotion state is invalid")
        return cls(
            candidate_id,
            _platform_digest(document["release_digest"], "package release"),
            _platform_digest(document["digest"], "package promotion"),
            state,
            document,
        )

    def to_dict(self) -> dict[str, object]:
        return _json_object_copy(self._document, "package promotion response")


@dataclass(frozen=True)
class PackageProgressResponse:
    """Bounded progress projection for validation, rollout, repair, or GC."""

    id: str
    state: str
    plan_digest: str
    _document: dict[str, object]

    @classmethod
    def from_dict(cls, value: object) -> PackageProgressResponse:
        document = _json_object_copy(value, "package progress response")
        _required_fields(document, {"id", "state", "plan_digest", "progress"}, "package progress response")
        try:
            identifier = str(uuid.UUID(document["id"]))
        except (AttributeError, TypeError, ValueError):
            raise ControlMalformedResponse("control API package progress ID is invalid") from None
        if identifier != document["id"]:
            raise ControlMalformedResponse("control API package progress ID is invalid")
        state = document["state"]
        progress = document["progress"]
        if not isinstance(state, str) or not 0 < len(state) <= 64 or not isinstance(progress, dict):
            raise ControlMalformedResponse("control API package progress is invalid")
        for key in ("completed", "failed", "running", "total"):
            if isinstance(progress.get(key), bool) or not isinstance(progress.get(key), int) or progress[key] < 0:
                raise ControlMalformedResponse("control API package progress is invalid")
        return cls(identifier, state, _platform_digest(document["plan_digest"], "package plan"), document)

    def to_dict(self) -> dict[str, object]:
        return _json_object_copy(self._document, "package progress response")


@dataclass(frozen=True)
class PackageCandidatesResponse:
    """Bounded candidate page retained as an immutable JSON document."""

    _document: dict[str, object]

    @classmethod
    def from_dict(cls, value: object) -> PackageCandidatesResponse:
        document = _json_object_copy(value, "package candidates response")
        candidates = document.get("candidates")
        if not isinstance(candidates, list) or len(candidates) > 100:
            raise ControlMalformedResponse("control API package candidate page is invalid")
        return cls(document)

    def to_dict(self) -> dict[str, object]:
        return _json_object_copy(self._document, "package candidates response")


@dataclass(frozen=True)
class PackageDocumentResponse:
    """Validated object envelope for bounded package read projections."""

    _document: dict[str, object]

    @classmethod
    def from_dict(cls, value: object, label: str) -> PackageDocumentResponse:
        return cls(_json_object_copy(value, label))

    def to_dict(self) -> dict[str, object]:
        return _json_object_copy(self._document, "package document response")


class ControlHTTPError(ControlClientError):
    def __init__(
        self,
        status_code: int,
        detail: str,
        retry_after_seconds: int | None = None,
        *,
        sensitive_values: tuple[str, ...] = (),
    ) -> None:
        self.status_code = status_code
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
        self._base = base_url.rstrip("/")
        self._token = token
        self._opener = opener if opener is not None else _redirect_denied_opener()
        self._transport = _OpenerTransport(self._opener, timeout_seconds)
        self._timeout = timeout_seconds

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
        raise error_type(
            status_code,
            detail,
            _bounded_retry_after(headers.get("retry-after")),
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
    ) -> dict[str, object]:
        if not path.startswith("/api/v1/") or ".." in path:
            raise ControlClientError("control API path is invalid")
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
        except (OSError, urllib.error.URLError, urllib.error.HTTPError) as error:
            raise ControlClientError(
                f"control API request failed: {type(error).__name__}"
            ) from None
        if len(content) > _MAX_RESPONSE:
            raise ControlClientError("control API response exceeds safety limit")
        if not 200 <= status < 300:
            raise ControlClientError(f"control API returned HTTP {status}")
        try:
            decoded = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ControlClientError("control API returned invalid JSON") from None
        if not isinstance(decoded, dict):
            raise ControlClientError("control API response must be an object")
        return decoded

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

    def update_skew(self) -> UpdateSkewResponse:
        return UpdateSkewResponse.from_dict(self.request("GET", "/api/v1/updates/skew"))

    def plan_update(self, release: str) -> UpdatePlanResponse:
        if (
            not isinstance(release, str)
            or len(release) > 512
            or _UPDATE_RELEASE.fullmatch(release) is None
        ):
            raise ControlClientError("platform update release name is invalid")
        return UpdatePlanResponse.from_dict(
            self.request("POST", "/api/v1/updates/plan", {"release": release})
        )

    def apply_update(self, plan_digest: str) -> UpdateRolloutResponse:
        if (
            not isinstance(plan_digest, str)
            or _PLATFORM_DIGEST.fullmatch(plan_digest) is None
        ):
            raise ControlClientError("platform update plan digest is invalid")
        return UpdateRolloutResponse.from_dict(
            self.request("POST", "/api/v1/updates", {"plan_digest": plan_digest})
        )

    def update_status(self, rollout_id: str) -> UpdateRolloutResponse:
        try:
            canonical_id = str(uuid.UUID(rollout_id))
        except (AttributeError, TypeError, ValueError):
            raise ControlClientError("platform update rollout ID is invalid") from None
        if canonical_id != rollout_id:
            raise ControlClientError("platform update rollout ID is invalid")
        return UpdateRolloutResponse.from_dict(
            self.request("GET", f"/api/v1/updates/{canonical_id}")
        )

    @staticmethod
    def _package_candidate_id(candidate_id: str) -> str:
        if not isinstance(candidate_id, str) or _CANDIDATE_ID.fullmatch(candidate_id) is None:
            raise ControlClientError("package candidate ID is invalid")
        return candidate_id

    @staticmethod
    def _package_digest(digest: str, label: str) -> str:
        if not isinstance(digest, str) or _PLATFORM_DIGEST.fullmatch(digest) is None:
            raise ControlClientError(f"package {label} digest is invalid")
        return digest

    @staticmethod
    def _package_request_id(request_id: str) -> str:
        try:
            canonical_id = str(uuid.UUID(request_id))
        except (AttributeError, TypeError, ValueError):
            raise ControlClientError("package mutation request ID is invalid") from None
        if canonical_id != request_id:
            raise ControlClientError("package mutation request ID is invalid")
        return canonical_id

    @staticmethod
    def _package_deployment_id(deployment_id: str) -> str:
        if (
            not isinstance(deployment_id, str)
            or not deployment_id
            or len(deployment_id) > 128
            or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,126}", deployment_id) is None
        ):
            raise ControlClientError("package deployment ID is invalid")
        return deployment_id

    def package_candidates(
        self,
        family_id: str | None = None,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> PackageCandidatesResponse:
        if family_id is not None:
            self._package_deployment_id(family_id)
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 512):
            raise ControlClientError("package candidate cursor is invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ControlClientError("package candidate limit is invalid")
        query = urllib.parse.urlencode(
            {key: value for key, value in {"family_id": family_id, "cursor": cursor, "limit": limit}.items() if value is not None}
        )
        return PackageCandidatesResponse.from_dict(
            self.request("GET", "/api/v1/packages/candidates?" + query)
        )

    def package_families(self, *, cursor: str | None = None, limit: int = 20) -> PackageDocumentResponse:
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 512):
            raise ControlClientError("package family cursor is invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ControlClientError("package family limit is invalid")
        return PackageDocumentResponse.from_dict(
            self.request("GET", "/api/v1/packages/families?" + urllib.parse.urlencode({"limit": limit, **({"cursor": cursor} if cursor is not None else {})})),
            "package families response",
        )

    def package_candidate(self, candidate_id: str) -> PackageDocumentResponse:
        candidate_id = self._package_candidate_id(candidate_id)
        return PackageDocumentResponse.from_dict(self.request("GET", f"/api/v1/packages/candidates/{candidate_id}"), "package candidate response")

    def package_resolution(self, candidate_id: str) -> PackageDocumentResponse:
        candidate_id = self._package_candidate_id(candidate_id)
        return PackageDocumentResponse.from_dict(self.request("GET", f"/api/v1/packages/candidates/{candidate_id}/resolution"), "package resolution response")

    def package_compatibility(self, candidate_id: str) -> PackageDocumentResponse:
        candidate_id = self._package_candidate_id(candidate_id)
        return PackageDocumentResponse.from_dict(self.request("GET", f"/api/v1/packages/candidates/{candidate_id}/compatibility"), "package compatibility response")

    def preview_package_promotion(self, candidate_id: str) -> PackagePlanResponse:
        candidate_id = self._package_candidate_id(candidate_id)
        return PackagePlanResponse.from_dict(
            self.request("POST", f"/api/v1/packages/candidates/{candidate_id}/promotion-preview", {})
        )

    def preview_package_validation(self, candidate_id: str) -> PackagePlanResponse:
        candidate_id = self._package_candidate_id(candidate_id)
        return PackagePlanResponse.from_dict(
            self.request("POST", f"/api/v1/packages/candidates/{candidate_id}/validation-preview", {})
        )

    def validate_package(
        self, candidate_id: str, plan_digest: str, *, request_id: str
    ) -> PackageProgressResponse:
        candidate_id = self._package_candidate_id(candidate_id)
        plan_digest = self._package_digest(plan_digest, "validation plan")
        request_id = self._package_request_id(request_id)
        return PackageProgressResponse.from_dict(
            self.request(
                "POST",
                f"/api/v1/packages/candidates/{candidate_id}/validate",
                {"plan_digest": plan_digest},
                extra_headers={"X-Request-ID": request_id},
            )
        )

    def package_validation(self, validation_id: str) -> PackageProgressResponse:
        try:
            canonical_id = str(uuid.UUID(validation_id))
        except (AttributeError, TypeError, ValueError):
            raise ControlClientError("package validation ID is invalid") from None
        if canonical_id != validation_id:
            raise ControlClientError("package validation ID is invalid")
        return PackageProgressResponse.from_dict(self.request("GET", f"/api/v1/packages/validations/{canonical_id}"))

    def promote_package(
        self, candidate_id: str, preview_digest: str, *, request_id: str
    ) -> PackagePromotionResponse:
        candidate_id = self._package_candidate_id(candidate_id)
        preview_digest = self._package_digest(preview_digest, "preview")
        request_id = self._package_request_id(request_id)
        return PackagePromotionResponse.from_dict(
            self.request(
                "POST",
                f"/api/v1/packages/candidates/{candidate_id}/promote",
                {"preview_digest": preview_digest},
                extra_headers={"X-Request-ID": request_id},
            )
        )

    def preview_deployment_rollout(self, deployment_id: str) -> PackagePlanResponse:
        deployment_id = self._package_deployment_id(deployment_id)
        return PackagePlanResponse.from_dict(
            self.request("POST", f"/api/v1/deployments/{deployment_id}/rollout-preview", {})
        )

    def package_deployments(self, *, cursor: str | None = None, limit: int = 20) -> PackageDocumentResponse:
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 512):
            raise ControlClientError("package deployment cursor is invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ControlClientError("package deployment limit is invalid")
        return PackageDocumentResponse.from_dict(
            self.request("GET", "/api/v1/deployments?" + urllib.parse.urlencode({"limit": limit, **({"cursor": cursor} if cursor is not None else {})})),
            "package deployments response",
        )

    def package_deployment(self, deployment_id: str) -> PackageDocumentResponse:
        deployment_id = self._package_deployment_id(deployment_id)
        return PackageDocumentResponse.from_dict(self.request("GET", f"/api/v1/deployments/{deployment_id}"), "package deployment response")

    def rollout_deployment(
        self, deployment_id: str, plan_digest: str, *, request_id: str
    ) -> PackageProgressResponse:
        deployment_id = self._package_deployment_id(deployment_id)
        plan_digest = self._package_digest(plan_digest, "plan")
        request_id = self._package_request_id(request_id)
        return PackageProgressResponse.from_dict(
            self.request(
                "POST",
                f"/api/v1/deployments/{deployment_id}/rollouts",
                {"plan_digest": plan_digest},
                extra_headers={"X-Request-ID": request_id},
            )
        )

    def package_rollout(self, deployment_id: str, rollout_id: str) -> PackageProgressResponse:
        deployment_id = self._package_deployment_id(deployment_id)
        try:
            canonical_id = str(uuid.UUID(rollout_id))
        except (AttributeError, TypeError, ValueError):
            raise ControlClientError("package rollout ID is invalid") from None
        if canonical_id != rollout_id:
            raise ControlClientError("package rollout ID is invalid")
        return PackageProgressResponse.from_dict(self.request("GET", f"/api/v1/deployments/{deployment_id}/rollouts/{canonical_id}"))

    def preview_deployment_rollback(self, deployment_id: str, rollout_id: str) -> PackagePlanResponse:
        deployment_id = self._package_deployment_id(deployment_id)
        try:
            canonical_id = str(uuid.UUID(rollout_id))
        except (AttributeError, TypeError, ValueError):
            raise ControlClientError("package rollout ID is invalid") from None
        if canonical_id != rollout_id:
            raise ControlClientError("package rollout ID is invalid")
        return PackagePlanResponse.from_dict(self.request("POST", f"/api/v1/deployments/{deployment_id}/rollouts/{canonical_id}/rollback-preview", {}))

    def rollback_deployment(
        self, deployment_id: str, rollout_id: str, plan_digest: str, *, request_id: str
    ) -> PackageProgressResponse:
        deployment_id = self._package_deployment_id(deployment_id)
        try:
            canonical_id = str(uuid.UUID(rollout_id))
        except (AttributeError, TypeError, ValueError):
            raise ControlClientError("package rollout ID is invalid") from None
        if canonical_id != rollout_id:
            raise ControlClientError("package rollout ID is invalid")
        plan_digest = self._package_digest(plan_digest, "rollback plan")
        request_id = self._package_request_id(request_id)
        return PackageProgressResponse.from_dict(self.request("POST", f"/api/v1/deployments/{deployment_id}/rollouts/{canonical_id}/rollback", {"plan_digest": plan_digest}, extra_headers={"X-Request-ID": request_id}))

    def preview_deployment_repair(self, deployment_id: str) -> PackagePlanResponse:
        deployment_id = self._package_deployment_id(deployment_id)
        return PackagePlanResponse.from_dict(
            self.request("POST", f"/api/v1/deployments/{deployment_id}/repair-preview", {})
        )

    def repair_deployment(
        self, deployment_id: str, plan_digest: str, *, request_id: str
    ) -> PackageProgressResponse:
        deployment_id = self._package_deployment_id(deployment_id)
        plan_digest = self._package_digest(plan_digest, "repair plan")
        request_id = self._package_request_id(request_id)
        return PackageProgressResponse.from_dict(
            self.request("POST", f"/api/v1/deployments/{deployment_id}/repair", {"plan_digest": plan_digest}, extra_headers={"X-Request-ID": request_id})
        )

    def preview_package_gc(self) -> PackagePlanResponse:
        return PackagePlanResponse.from_dict(self.request("POST", "/api/v1/packages/gc-preview", {}))

    def apply_package_gc(
        self, plan_digest: str, *, request_id: str
    ) -> PackageProgressResponse:
        plan_digest = self._package_digest(plan_digest, "garbage collection plan")
        request_id = self._package_request_id(request_id)
        return PackageProgressResponse.from_dict(
            self.request("POST", "/api/v1/packages/gc", {"plan_digest": plan_digest}, extra_headers={"X-Request-ID": request_id})
        )
