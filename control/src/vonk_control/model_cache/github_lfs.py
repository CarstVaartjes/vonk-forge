"""GitHub LFS objects: fetched by oid through the official LFS batch API.

The persisted locator is the stable ``media.githubusercontent.com`` URL of the
object (no secret).  Each transfer resolves a signed download link through one
anonymous batch call, keeps it in process memory only, and shares it with every
range worker and resume of that object until it nears expiry: one batch call
per object per link lifetime, against GitHub's 100 anonymous LFS calls an hour.

What each answer means (tests assert the behaviour, not these codes):

| Answer | Behaviour |
| --- | --- |
| batch 200 with a download action | the link is cached and opened |
| batch 200, object error 404/410 | source-gone observation (gone counter) |
| batch 200, object missing, other size, error 409/422 | size disagreement: refetch |
| batch 401 (missing or no longer public repository) | source-gone observation |
| batch 404/410 | source-gone observation |
| batch 429, or 403 with no remaining calls | rate limit: the provider's wait, on its hint |
| batch 403 otherwise, or 509 (bandwidth quota) | the provider's wait, on the provider-wait cadence |
| batch 400/406/413/415/422 | our request is wrong: fails with evidence |
| batch 3xx/5xx, transport error, unreadable reply | unavailable: observed again |
| link on a host outside the allowlist | refused before any request (security edge) |
| link 3xx | refused (security edge) |
| link 403/404/410 (expired, revoked) | the link is dropped and resolved again, once |
| link 429/503 | the provider's wait, on its hint |
| bytes that do not hash to the oid | refused at ingress by the shared verifier |
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

import httpx2
from pydantic import ValidationError
from vonk_agent_protocol import ModelCacheCode, OperatorActionName

from ..bounded_retry import bounded_attempts
from ..operation_contract import AvailabilityRecoveryAction
from .catalog_helpers import _datetime, _github_lfs_binding
from .constants import (
    _GITHUB_HOST,
    _GITHUB_LFS_OBJECT_HOSTS,
    _LFS_HREF_MARGIN_SECONDS,
    _MAX_GITHUB_LFS_BATCH_BYTES,
    _MAX_GITHUB_LFS_LINK_HEADERS,
)
from .errors import (
    ModelCacheError,
    ModelCacheStorageInvalid,
    ModelCacheStorageRefused,
    ModelCacheStorageUnknown,
    _retry_after_seconds,
)
from .provider_contracts import (
    _GitHubLfsBatch,
    _GitHubLfsBatchRequest,
    _GitHubLfsObject,
    _GitHubLfsObjectRef,
    _GitHubLfsOperation,
)

if TYPE_CHECKING:
    from .artifacts import ArtifactSpec
    from .service import ModelCacheService


_LFS_MEDIA_TYPE = "application/vnd.git-lfs+json"
# GitHub answers the batch call with plain JSON (observed 2026-10-26).
_BATCH_REPLY_TYPES = frozenset({_LFS_MEDIA_TYPE, "application/json"})
_LINK_REFUSED = frozenset({403, 404, 410})
_REQUEST_DEFECT = frozenset({400, 406, 413, 415, 422})
_TRANSFER_HEADERS = frozenset({"Range", "Accept-Encoding"})

type _LinkKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class _LfsLink:
    """A signed download link: process memory only, never persisted or logged."""

    href: str
    header: tuple[tuple[str, str], ...]
    expires_at: datetime

    def __repr__(self) -> str:  # never print the signature or token
        return f"_LfsLink(expires_at={self.expires_at!r})"


class _LfsLinks:
    """The signed links of the objects this process transfers, by object.

    A link is reused until it nears expiry.  Resolving is single-flight: while
    one transfer asks GitHub, the others wait for its answer, so parallel range
    workers of one file make one batch call.  The resolve gate is held across
    that one bounded batch call only; it is never the service lock.  Expired
    links are dropped on every store, so the map holds only links that are
    still live (objects resolved within the last link lifetime).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._resolve = threading.Lock()
        self._links: dict[_LinkKey, _LfsLink] = {}

    @staticmethod
    def _live(link: _LfsLink | None, now: datetime) -> bool:
        return link is not None and link.expires_at - now > timedelta(
            seconds=_LFS_HREF_MARGIN_SECONDS
        )

    def get(
        self,
        key: _LinkKey,
        clock: Callable[[], datetime],
        resolve: Callable[[], _LfsLink],
    ) -> _LfsLink:
        with self._lock:
            link = self._links.get(key)
        if self._live(link, clock()):
            assert link is not None
            return link
        with self._resolve:
            with self._lock:
                link = self._links.get(key)
            if self._live(link, clock()):
                assert link is not None
                return link  # a peer resolved it while this one waited
            link = resolve()
            now = clock()
            with self._lock:
                self._links = {
                    other: value
                    for other, value in self._links.items()
                    if self._live(value, now)
                }
                self._links[key] = link
            return link

    def drop(self, key: _LinkKey, link: _LfsLink) -> None:
        """Forget a refused link, unless a peer already replaced it."""
        with self._lock:
            if self._links.get(key) is link:
                del self._links[key]


def _unavailable(
    detail: str,
    *,
    source_status: int | None = None,
    retry_after_seconds: int | None = None,
) -> ModelCacheStorageUnknown:
    return ModelCacheStorageUnknown(
        ModelCacheCode.SOURCE_UNAVAILABLE,
        detail,
        retry_after_seconds=retry_after_seconds,
        recovery=OperatorActionName.RESUME,
        source_status=source_status,
    )


def _batch_answer_error(
    status: int, headers: Mapping[str, str], now: datetime
) -> ModelCacheError:
    """The behaviour of a batch call that did not answer 200."""

    if status == 401:
        # Anonymous GitHub answers 401 for a repository that is missing or no
        # longer public; the contract promises public access, so this is the
        # source being gone, observed like a 404.
        return _unavailable(
            "GitHub LFS answered 401: the public repository is missing or no longer public",
            source_status=404,
        )
    if status in {404, 410}:
        return _unavailable(
            f"GitHub LFS repository answered {status}", source_status=status
        )
    if status == 429 or (status == 403 and headers.get("x-ratelimit-remaining") == "0"):
        return ModelCacheStorageUnknown(
            ModelCacheCode.RATE_LIMITED,
            "GitHub rate limited anonymous LFS requests; the download resumes automatically",
            retry_after_seconds=_retry_after_seconds(headers, now=now),
            recovery=OperatorActionName.RESUME,
        )
    if status in {403, 509}:
        # The bandwidth quota of the repository owner (GitHub documents no
        # status; the LFS spec reserves 509). Its rate-limit reset says
        # nothing about the quota, so only an explicit Retry-After is a hint.
        retry_after = headers.get("retry-after")
        return ModelCacheStorageUnknown(
            ModelCacheCode.RATE_LIMITED,
            f"GitHub LFS refused the download (status {status}): the repository "
            "owner's LFS bandwidth is likely exhausted; rechecking automatically",
            retry_after_seconds=_retry_after_seconds(
                {"retry-after": retry_after} if retry_after else {}, now=now
            ),
            recovery=OperatorActionName.RESUME,
        )
    if status in _REQUEST_DEFECT:
        return ModelCacheStorageInvalid(
            ModelCacheCode.SOURCE_INVALID,
            f"GitHub LFS rejected the batch request (status {status}); "
            "this is a platform defect",
        )
    return _unavailable(
        f"GitHub LFS batch request failed with status {status}",
        source_status=status,
        retry_after_seconds=_retry_after_seconds(headers, now=now)
        if status >= 500
        else None,
    )


def _trusted_link(href: str) -> bool:
    try:
        parsed = urlsplit(href)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname in _GITHUB_LFS_OBJECT_HOSTS
        and parsed.netloc in _GITHUB_LFS_OBJECT_HOSTS
        and port is None
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
        and parsed.path.startswith("/")
    )


def _size_disagreement() -> ModelCacheStorageUnknown:
    return ModelCacheStorageUnknown(
        ModelCacheCode.SOURCE_SIZE_MISMATCH,
        "GitHub LFS does not hold this object at the pinned size",
        recovery=OperatorActionName.RESUME,
    )


def _object_link(entry: _GitHubLfsObject, now: datetime) -> _LfsLink:
    """The download link of the one batch entry for this object."""

    if entry.error is not None:
        code = entry.error.code
        if code in {404, 410}:
            raise _unavailable(
                f"GitHub LFS does not hold this object (object answer {code})",
                source_status=code,
            )
        if code in {409, 422}:
            raise _size_disagreement()
        raise _unavailable(
            f"GitHub LFS could not serve this object (object answer {code})"
        )
    download = entry.actions.download if entry.actions is not None else None
    if download is None:
        raise _unavailable("GitHub LFS offered no download for this object")
    if not _trusted_link(download.href):
        raise ModelCacheStorageRefused(
            ModelCacheCode.SOURCE_UNTRUSTED,
            "GitHub LFS offered a download outside the trusted object storage host",
            recovery=AvailabilityRecoveryAction.INSPECT,
        )
    header = download.header or {}
    if len(header) > _MAX_GITHUB_LFS_LINK_HEADERS:
        raise _unavailable("GitHub LFS download link carries too many headers")
    if download.expires_in is not None and download.expires_in > 0:
        expires_at = now + timedelta(seconds=download.expires_in)
    elif download.expires_at is not None:
        try:
            expires_at = _datetime(datetime.fromisoformat(download.expires_at))
        except ValueError:
            expires_at = now  # unreadable: the link is used once
    else:
        expires_at = now
    return _LfsLink(
        href=download.href,
        header=tuple(sorted(header.items())),
        expires_at=expires_at,
    )


class GithubLfsMixin:
    """GitHub LFS behaviour of the cache service."""

    def _open_github_lfs_object(
        self,
        client: httpx2.Client,
        spec: ArtifactSpec,
        headers: Mapping[str, str],
    ) -> httpx2.Response:
        """Open the object through its signed link; a refused link is resolved
        again once (expired early, revoked, clock skew)."""

        cache = cast("ModelCacheService", self)
        cache._validate_http_download(spec)
        owner, name, _revision, _path = _github_lfs_binding(spec)
        key = (f"{owner}/{name}", spec.sha256)
        status = 0
        for _refresh in range(2):
            link = cache._lfs_links.get(
                key,
                cache._clock,
                lambda: cache._resolve_github_lfs_link(client, spec, owner, name),
            )
            response = cache._get_github_lfs_link(client, link, headers)
            if response.status_code not in _LINK_REFUSED:
                return response
            status = response.status_code
            response.close()
            cache._lfs_links.drop(key, link)
        raise _unavailable(
            f"GitHub LFS object storage refused a freshly resolved link (status {status})",
            source_status=status,
        )

    def _resolve_github_lfs_link(
        self, client: httpx2.Client, spec: ArtifactSpec, owner: str, name: str
    ) -> _LfsLink:
        """One batch call, repeated in-request only for a transport failure."""

        cache = cast("ModelCacheService", self)
        refused: ModelCacheError | None = None
        for _attempt in bounded_attempts():
            try:
                return cache._post_github_lfs_batch(client, spec, owner, name)
            except ModelCacheStorageUnknown as error:
                refused = error
                if (
                    error.code != ModelCacheCode.SOURCE_UNAVAILABLE
                    or error.retry_after_seconds
                    or (error.source_status is not None and error.source_status < 500)
                ):
                    break  # the durable failure path owns these observations
        assert refused is not None
        raise refused

    def _post_github_lfs_batch(
        self, client: httpx2.Client, spec: ArtifactSpec, owner: str, name: str
    ) -> _LfsLink:
        cache = cast("ModelCacheService", self)
        body = (
            _GitHubLfsBatchRequest(
                operation=_GitHubLfsOperation.DOWNLOAD,
                transfers=["basic"],
                objects=[
                    _GitHubLfsObjectRef(oid=spec.sha256, size=spec.expected_bytes)
                ],
                hash_algo="sha256",
            )
            .model_dump_json()
            .encode()
        )
        try:
            response = cache._send_anonymous_github_request(
                client,
                f"https://{_GITHUB_HOST}/{owner}/{name}.git/info/lfs/objects/batch",
                {"Accept": _LFS_MEDIA_TYPE, "Content-Type": _LFS_MEDIA_TYPE},
                method="POST",
                content=body,
            )
        except httpx2.HTTPError as error:
            raise _unavailable("GitHub LFS batch request failed") from error
        try:
            now = cache._clock()
            if response.status_code != 200:
                raise _batch_answer_error(response.status_code, response.headers, now)
            media_type = response.headers.get("content-type", "").split(";")[0]
            if media_type.strip().lower() not in _BATCH_REPLY_TYPES:
                raise _unavailable("GitHub LFS batch reply is not JSON")
            raw = bytearray()
            for chunk in response.iter_bytes(chunk_size=8192):
                if len(raw) + len(chunk) > _MAX_GITHUB_LFS_BATCH_BYTES:
                    raise _unavailable("GitHub LFS batch reply is too large")
                raw.extend(chunk)
            batch = _GitHubLfsBatch.model_validate_json(bytes(raw))
        except httpx2.HTTPError as error:
            raise _unavailable("GitHub LFS batch reply was interrupted") from error
        except ValidationError as error:
            raise _unavailable("GitHub LFS batch reply is unreadable") from error
        finally:
            response.close()
        if (batch.transfer is not None and batch.transfer != "basic") or (
            batch.hash_algo is not None and batch.hash_algo != "sha256"
        ):
            raise ModelCacheStorageInvalid(
                ModelCacheCode.SOURCE_UNSUPPORTED,
                "GitHub LFS offered a transfer this cache does not speak",
            )
        entry = next((item for item in batch.objects if item.oid == spec.sha256), None)
        if entry is None or entry.size != spec.expected_bytes:
            raise _size_disagreement()
        return _object_link(entry, now)

    def _get_github_lfs_link(
        self,
        client: httpx2.Client,
        link: _LfsLink,
        headers: Mapping[str, str],
    ) -> httpx2.Response:
        """GET the signed link; a refused link is returned for the caller to renew."""

        cache = cast("ModelCacheService", self)
        request_headers = dict(link.header)
        request_headers.update(
            (key, value) for key, value in headers.items() if key in _TRANSFER_HEADERS
        )
        try:
            response = cache._send_anonymous_github_request(
                client, link.href, request_headers
            )
        except httpx2.HTTPError as error:
            raise _unavailable("GitHub LFS object transfer failed") from error
        status = response.status_code
        if status in {200, 206} or status in _LINK_REFUSED:
            return response
        response.close()
        if 300 <= status < 400:
            raise ModelCacheStorageRefused(
                ModelCacheCode.REDIRECT_FORBIDDEN,
                "GitHub LFS object storage redirected; redirects are not followed",
                recovery=AvailabilityRecoveryAction.INSPECT,
            )
        if status in {429, 503}:
            raise ModelCacheStorageUnknown(
                ModelCacheCode.RATE_LIMITED,
                "GitHub LFS object storage asked to slow down; the download resumes automatically",
                retry_after_seconds=_retry_after_seconds(
                    response.headers, now=cache._clock()
                ),
                recovery=OperatorActionName.RESUME,
            )
        raise _unavailable(
            f"GitHub LFS object storage answered {status}",
            source_status=status,
            retry_after_seconds=_retry_after_seconds(
                response.headers, now=cache._clock()
            )
            if status >= 500
            else None,
        )
