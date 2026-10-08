"""Bounded artifact uploads and crash-safe private downloads."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path

from ..control_limits import MAX_CONTROL_DOCUMENT_BYTES
from ..error_reporting import (
    local_io_context,
    protocol_context,
    safe_request_id,
    transport_context,
)
from .client import ControlClient as HTTPControlClient
from .common import _MAX_ARTIFACT_INPUT
from .errors import (
    _MAX_ARTIFACT_OUTPUT,
    _STATUS_ERRORS,
    ControlClientError,
    ControlHTTPError,
    ControlMalformedResponse,
    ControlResponseTooLarge,
    ControlTransportError,
    _retry_after_seconds,
    _structured_http_error_fields,
)
from .schema import (
    _operation,
    _request_media_contract,
    _response_contract,
    _response_definition,
    _response_media_contract,
)


class ControlClient(HTTPControlClient):
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
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOINHERIT", 0)
            | getattr(os, "O_NONBLOCK", 0)
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
                        content = response.read(MAX_CONTROL_DOCUMENT_BYTES + 1)
                        status = response.status
                        response_headers = response.headers
                except urllib.error.HTTPError as error:
                    with error:
                        content = error.read(MAX_CONTROL_DOCUMENT_BYTES + 1)
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
                context.render(
                    "artifact input must be a readable regular non-symlink file"
                ),
                context=context,
            ) from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        try:
            if len(content) > MAX_CONTROL_DOCUMENT_BYTES:
                raise ControlResponseTooLarge(
                    "control API response exceeds safety limit"
                )
            if not 200 <= status < 300:
                error_media_type = response_headers.get("content-type", "").split(
                    ";", 1
                )[0]
                _response_media_contract(
                    path,
                    "PUT",
                    status,
                    error_media_type.strip().lower(),
                    has_content=bool(content),
                )
                try:
                    problem = json.loads(content)
                except RecursionError:
                    raise ControlMalformedResponse(
                        "control API response exceeds the nesting limit"
                    ) from None
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
                    operation=f"PUT {path}",
                    endpoint=path,
                    request_id=response_headers.get("x-request-id"),
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
            response_media_type = response_headers.get("content-type", "").split(
                ";", 1
            )[0]
            _response_media_contract(
                path,
                "PUT",
                status,
                response_media_type.strip().lower(),
                has_content=True,
            )
            _response_contract(path, "PUT", status, decoded)
            return decoded
        except (ControlMalformedResponse, ControlResponseTooLarge) as error:
            if error.context is None:
                error.context = replace(
                    protocol_context(operation=f"PUT {path}", endpoint=path),
                    http_status=status,
                    request_id=safe_request_id(response_headers.get("x-request-id")),
                )
            raise

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
                with error:
                    content = error.read(MAX_CONTROL_DOCUMENT_BYTES + 1)
                if len(content) > MAX_CONTROL_DOCUMENT_BYTES:
                    raise ControlResponseTooLarge(
                        "control API response exceeds safety limit"
                    )
                error_media_type = error.headers.get("content-type", "").split(";", 1)[
                    0
                ]
                _response_media_contract(
                    path,
                    "GET",
                    error.code,
                    error_media_type.strip().lower(),
                    has_content=bool(content),
                )
                try:
                    problem = json.loads(content)
                except RecursionError:
                    raise ControlMalformedResponse(
                        "control API response exceeds the nesting limit"
                    ) from None
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
                fields, body_retry_after = _structured_http_error_fields(problem)
                if fields.get("code") is None:
                    fields["code"] = error.headers.get("x-vonk-error-code")
                retry_after = _retry_after_seconds(error.headers.get("retry-after"))
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
                    response_media_type = response.headers.get(
                        "content-type", ""
                    ).split(";", 1)[0]
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
