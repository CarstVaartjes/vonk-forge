"""Github."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import urljoin

import httpx2
from pydantic import ValidationError
from vonk_agent_protocol import ModelCacheCode, SecurityRefusalReason

from ..bounded_retry import bounded_attempts
from .artifacts import ArtifactSpec
from .catalog_helpers import _github_release_asset_binding
from .constants import (
    _GITHUB_API_HOST,
    _GITHUB_USER_AGENT,
    _MAX_GITHUB_ERROR_METADATA_BYTES,
    _MAX_GITHUB_RELEASE_METADATA_BYTES,
)
from .errors import (
    ModelCacheStorageRefused,
    ModelCacheStorageUnknown,
    _retry_after_seconds,
)
from .provider_contracts import _GitHubErrorMetadata, _GitHubReleaseMetadata
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

    def _validate_github_release_asset(self, spec: ArtifactSpec) -> None:
        cache = cast("ModelCacheService", self)
        release_id, asset_id, owner, name = _github_release_asset_binding(spec)
        cache._validate_http_download(spec)
        client = cache._http
        owns_client = client is None
        if client is None:
            client = httpx2.Client(
                follow_redirects=False,
                timeout=httpx2.Timeout(30.0),
                trust_env=False,
            )
        release_url = (
            f"https://{_GITHUB_API_HOST}/repos/{owner}/{name}/releases/{release_id}"
        )
        response: httpx2.Response | None = None
        try:
            response = cache._send_anonymous_github_request(
                client,
                release_url,
                {
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            cache._raise_github_http_status(response, allow_binary=False)
            declared_size = response.headers.get("content-length")
            if (
                declared_size is not None
                and declared_size.isdigit()
                and int(declared_size) > _MAX_GITHUB_RELEASE_METADATA_BYTES
            ):
                raise ModelCacheStorageRefused(
                    ModelCacheCode.RELEASE_METADATA_INVALID,
                    "GitHub release metadata exceeds the size limit",
                    recovery="inspect",
                )
            raw = bytearray()
            try:
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > _MAX_GITHUB_RELEASE_METADATA_BYTES:
                        raise ModelCacheStorageRefused(
                            ModelCacheCode.RELEASE_METADATA_INVALID,
                            "GitHub release metadata exceeds the size limit",
                            recovery="inspect",
                        )
            except httpx2.HTTPError as error:
                raise ModelCacheStorageUnknown(
                    ModelCacheCode.SOURCE_UNAVAILABLE,
                    "GitHub release metadata transfer failed",
                    recovery="resume",
                ) from error
            try:
                release = _GitHubReleaseMetadata.model_validate_json(raw)
            except (TypeError, ValueError, ValidationError) as error:
                raise ModelCacheStorageRefused(
                    ModelCacheCode.RELEASE_METADATA_INVALID,
                    "GitHub release metadata does not match the required response fields",
                    recovery="inspect",
                ) from error
            if release.id != release_id:
                raise ModelCacheStorageRefused(
                    ModelCacheCode.RELEASE_METADATA_INVALID,
                    "GitHub returned metadata for a different or invalid release",
                    recovery="inspect",
                )
            matches = [asset for asset in release.assets if asset.id == asset_id]
            if len(matches) != 1:
                raise ModelCacheStorageRefused(
                    ModelCacheCode.RELEASE_ASSET_IDENTITY_CONFLICT,
                    "the pinned asset ID is not a unique member of the pinned GitHub release",
                    recovery="inspect",
                )
            asset = matches[0]
            if (
                asset.name != Path(spec.path).name
                or asset.size != spec.expected_bytes
                or asset.state != "uploaded"
            ):
                raise ModelCacheStorageRefused(
                    ModelCacheCode.RELEASE_ASSET_IDENTITY_CONFLICT,
                    "the pinned GitHub release asset name, state, or size does not match the model file",
                    recovery="inspect",
                )
            provider_digest = asset.digest
            if provider_digest is not None and (
                not isinstance(provider_digest, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", provider_digest) is None
                or provider_digest.removeprefix("sha256:") != spec.sha256
            ):
                raise ModelCacheStorageRefused(
                    ModelCacheCode.RELEASE_ASSET_IDENTITY_CONFLICT,
                    "the pinned GitHub release asset digest does not match the model file",
                    recovery="inspect",
                )
        finally:
            if response is not None:
                response.close()
            if owns_client:
                client.close()

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
                recovery="resume",
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
            recovery="resume",
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
        assert refused is not None
        raise refused

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
                recovery="resume",
            ) from error
        cache._raise_github_http_status(response, allow_binary=True)
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("location")
        response.close()
        if not location:
            raise ModelCacheStorageRefused(
                ModelCacheCode.REDIRECT_FORBIDDEN,
                "GitHub asset redirect did not provide a destination",
                recovery="inspect",
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
                recovery="resume",
            ) from error
        cache._raise_github_http_status(response, allow_binary=True)
        if 300 <= response.status_code < 400:
            response.close()
            raise ModelCacheStorageRefused(
                ModelCacheCode.REDIRECT_FORBIDDEN,
                "GitHub release CDN returned a second redirect",
                recovery="inspect",
            )
        return response
