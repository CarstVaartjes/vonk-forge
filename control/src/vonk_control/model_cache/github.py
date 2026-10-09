"""Github."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, cast
from urllib.parse import urljoin

import httpx2
from pydantic import ValidationError
from vonk_agent_protocol import (
    ModelCacheCode,
    OperatorActionName,
    SecurityRefusalReason,
)

from ..bounded_retry import bounded_attempts
from .artifacts import ArtifactSpec
from .catalog_helpers import _github_release_asset_binding
from .constants import (
    _GITHUB_USER_AGENT,
    _MAX_GITHUB_ERROR_METADATA_BYTES,
)
from .errors import (
    ModelCacheStorageRefused,
    ModelCacheStorageUnknown,
    _retry_after_seconds,
)
from .provider_contracts import _GitHubErrorMetadata
from .source_helpers import _is_allowed_github_release_redirect

if TYPE_CHECKING:
    from .service import ModelCacheService


class GithubMixin:
    """Github behavior of the cache service."""

    @staticmethod
    def _send_anonymous_github_request(
        client: httpx2.Client, url: str, headers: Mapping[str, str]
    ) -> httpx2.Response:
        """Send only these headers, ignoring injected client auth and cookies.

        `Client.build_request` merges client headers and the cookie jar. A raw
        Request plus explicit `auth=None` keeps caller-level credentials away
        from GitHub and its signed asset CDN.
        """

        request_headers = dict(headers)
        request_headers["User-Agent"] = _GITHUB_USER_AGENT
        request = httpx2.Request("GET", url, headers=request_headers)
        return client.send(
            request,
            stream=True,
            auth=None,
            follow_redirects=False,
        )

    def _raise_github_http_status(
        self, response: httpx2.Response, *, allow_binary: bool
    ) -> None:
        cache = cast("ModelCacheService", self)
        status = response.status_code
        remaining = response.headers.get("x-ratelimit-remaining")
        retry_after_header = response.headers.get("retry-after")
        secondary_rate_limit = False
        if status in {403, 429} and remaining != "0" and not retry_after_header:
            secondary_rate_limit = cache._github_error_reports_secondary_rate_limit(
                response
            )
        rate_limited = status == 429 or (
            status == 403
            and (remaining == "0" or bool(retry_after_header) or secondary_rate_limit)
        )
        if rate_limited:
            retry_after = _retry_after_seconds(response.headers, now=cache._clock())
            if secondary_rate_limit and retry_after is None and remaining != "0":
                # GitHub's secondary-limit guidance asks clients to wait at
                # least one minute when it supplies no explicit retry hint.
                retry_after = 60
            response.close()
            raise ModelCacheStorageUnknown(
                ModelCacheCode.RATE_LIMITED,
                "GitHub rate limited this anonymous release download; it will resume automatically",
                retry_after_seconds=retry_after,
                recovery=OperatorActionName.RESUME,
            )
        if response.status_code in ({200, 206} if allow_binary else {200}):
            return
        if response.status_code in {301, 302, 303, 307, 308} and allow_binary:
            return
        response.close()
        if status in {401, 403}:
            raise ModelCacheStorageRefused(
                SecurityRefusalReason.MODEL_CACHE_SOURCE_ACCESS_DENIED.value,
                "GitHub denied anonymous access to the public release source",
                recovery="inspect",
            )
        raise ModelCacheStorageUnknown(
            ModelCacheCode.SOURCE_UNAVAILABLE,
            f"GitHub release request failed with status {status}",
            retry_after_seconds=_retry_after_seconds(
                response.headers, now=cache._clock()
            ),
            recovery=OperatorActionName.RESUME,
            source_status=status,
        )

    @staticmethod
    def _github_error_reports_secondary_rate_limit(
        response: httpx2.Response,
    ) -> bool:
        """Read only a small typed error body; never persist its message."""

        raw = bytearray()
        try:
            for chunk in response.iter_bytes(chunk_size=8192):
                if len(raw) + len(chunk) > _MAX_GITHUB_ERROR_METADATA_BYTES:
                    return False
                raw.extend(chunk)
            error = _GitHubErrorMetadata.model_validate_json(raw)
        except (httpx2.HTTPError, TypeError, ValueError, ValidationError):
            return False
        return "secondary rate limit" in error.message.casefold()

    def _open_github_release_asset(
        self,
        client: httpx2.Client,
        spec: ArtifactSpec,
        headers: Mapping[str, str],
    ) -> httpx2.Response:
        """Observe the exact request again within a bounded request retry budget."""
        cache = cast("ModelCacheService", self)
        refused: ModelCacheStorageUnknown | None = None
        for _attempt in bounded_attempts():
            try:
                return cache._open_github_release_asset_once(client, spec, headers)
            except ModelCacheStorageUnknown as error:
                refused = error
                if error.source_status in {404, 410} or error.retry_after_seconds:
                    break  # the durable source-gone counter owns these observations
        if refused is not None:
            raise refused
        raise ModelCacheStorageUnknown(
            ModelCacheCode.SOURCE_UNAVAILABLE, "exact source observation is unavailable"
        )

    def _open_github_release_asset_once(
        self,
        client: httpx2.Client,
        spec: ArtifactSpec,
        headers: Mapping[str, str],
    ) -> httpx2.Response:
        cache = cast("ModelCacheService", self)
        _github_release_asset_binding(spec)
        cache._validate_http_download(spec)
        request_headers = {
            "Accept": "application/octet-stream",
            "X-GitHub-Api-Version": "2022-11-28",
            **headers,
        }
        try:
            response = cache._send_anonymous_github_request(
                client, spec.source, request_headers
            )
        except httpx2.HTTPError as error:
            raise ModelCacheStorageUnknown(
                ModelCacheCode.SOURCE_UNAVAILABLE,
                "GitHub release asset request failed",
                recovery=OperatorActionName.RESUME,
            ) from error
        cache._raise_github_http_status(response, allow_binary=True)
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("location")
        response.close()
        if not location:
            raise ModelCacheStorageUnknown(
                ModelCacheCode.SOURCE_UNAVAILABLE,
                "GitHub asset redirect did not provide a destination",
                recovery=OperatorActionName.RESUME,
            )
        redirected_url = urljoin(spec.source, location)
        if not _is_allowed_github_release_redirect(redirected_url):
            raise ModelCacheStorageRefused(
                ModelCacheCode.REDIRECT_FORBIDDEN,
                "GitHub asset redirected outside the trusted release CDN",
                recovery="inspect",
            )
        try:
            response = cache._send_anonymous_github_request(
                client,
                redirected_url,
                {
                    key: value
                    for key, value in request_headers.items()
                    if key in {"Range", "Accept-Encoding"}
                },
            )
        except httpx2.HTTPError as error:
            raise ModelCacheStorageUnknown(
                ModelCacheCode.SOURCE_UNAVAILABLE,
                "GitHub release asset transfer failed",
                recovery=OperatorActionName.RESUME,
            ) from error
        cache._raise_github_http_status(response, allow_binary=True)
        if 300 <= response.status_code < 400:
            response.close()
            raise ModelCacheStorageUnknown(
                ModelCacheCode.SOURCE_UNAVAILABLE,
                "GitHub release CDN returned an incomplete transfer response",
                recovery=OperatorActionName.RESUME,
            )
        return response
