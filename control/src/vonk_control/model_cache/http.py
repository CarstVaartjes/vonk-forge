"""Http."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import urljoin, urlsplit

import httpx2
from sqlalchemy import func, select
from vonk_agent_protocol import ModelCacheCode, SecurityRefusalReason

from ..bounded_retry import bounded_attempts
from ..cached_file_verification import verified_files
from ..lifecycle import Tick
from ..model_cache_progress import cache_phase, progress_document
from ..models import ModelCacheOperation
from ..runtime_init import RuntimeSecretError, read_runtime_secret
from .artifacts import ArtifactSetManifest, ArtifactSpec, _unique_artifacts
from .constants import (
    _CREDENTIAL_FAILURE_PUBLIC_CODES,
    _MAX_HTTP_REDIRECTS,
    _RETRY_BASE_SECONDS,
)
from .errors import (
    ModelCacheCredentialPathUnsafe,
    ModelCacheStorageRefused,
    ModelCacheStorageUnknown,
    _huggingface_access_url,
    _retry_after_seconds,
)
from .persistence import _operation_progress, _store_operation_payload
from .source_helpers import (
    _fsync_directory,
    _is_allowed_huggingface_redirect,
    _is_hf_authority,
    _is_hf_canonical_url,
)

if TYPE_CHECKING:
    from .service import ModelCacheService


class HttpMixin:
    """Http behavior of the cache service."""

    def _open_http_response(
        self,
        client: httpx2.Client,
        source: str,
        headers: Mapping[str, str],
    ) -> httpx2.Response:
        """Observe the exact request again within a bounded request retry budget."""
        cache = cast("ModelCacheService", self)
        refused: ModelCacheStorageUnknown | None = None
        for _attempt in bounded_attempts():
            try:
                return cache._open_http_response_once(client, source, headers)
            except ModelCacheStorageUnknown as error:
                refused = error
                if error.source_status in {404, 410} or error.retry_after_seconds:
                    break  # the durable source-gone counter owns these observations
        assert refused is not None
        raise refused

    def _open_http_response_once(
        self,
        client: httpx2.Client,
        source: str,
        headers: Mapping[str, str],
    ) -> httpx2.Response:
        """Open a pinned source, authenticating only the HF authority.

        Hugging Face commonly redirects a resolve URL to a signed CDN URL.
        Explicit redirect handling ensures an Authorization header is never
        copied to an arbitrary host. A configured token is sent only on the
        canonical authority request; without a token file, the request stays
        anonymous until the authority reports that access is required.
        """
        cache = cast("ModelCacheService", self)
        current_url = source
        try:
            source_host = urlsplit(source).hostname
        except ValueError:
            source_host = None
        source_is_huggingface = _is_hf_authority(source_host)
        if source_is_huggingface:
            with cache._lock:
                cooldown = cache._hf_cooldown_until
            if cooldown is not None and cooldown > cache._clock():
                raise ModelCacheStorageRefused(
                    ModelCacheCode.RATE_LIMITED,
                    "Hugging Face download cooldown is active",
                    retry_after_seconds=max(
                        1, int((cooldown - cache._clock()).total_seconds())
                    ),
                    recovery="resume",
                )
        authenticated = False
        token: str | None = None
        token_loaded = False
        if source_is_huggingface and cache._huggingface_token_path is not None:
            # A configured token is used on the canonical request so gated
            # files do not incur a public anonymous request. It is never
            # copied to a redirect/CDN host.
            token = cache._load_huggingface_token()
            token_loaded = True
            # Compose always names the optional normalized secret path. Its
            # absent/empty projection means anonymous public downloads; only
            # an actual provider denial requires account access and a token.
            authenticated = token is not None
        for redirect_count in range(_MAX_HTTP_REDIRECTS + 1):
            request_headers = dict(headers)
            if authenticated and _is_hf_canonical_url(current_url):
                # The token is intentionally constructed only for the
                # canonical Hugging Face authority. It is never sent to CDN
                # redirect hosts, even when they are trusted HF domains.
                request_headers["Authorization"] = f"Bearer {token}"
            request = client.build_request("GET", current_url, headers=request_headers)
            response = client.send(request, stream=True)
            status_code = response.status_code
            if status_code == 429:
                retry_after = _retry_after_seconds(response.headers, now=cache._clock())
                if source_is_huggingface:
                    until = cache._clock() + timedelta(
                        seconds=retry_after or _RETRY_BASE_SECONDS
                    )
                    with cache._lock:
                        if (
                            cache._hf_cooldown_until is None
                            or until > cache._hf_cooldown_until
                        ):
                            cache._hf_cooldown_until = until
                response.close()
                if source_is_huggingface:
                    cache._streams.throttled(
                        retry_after, "Hugging Face answered 429 (rate limited)"
                    )
                raise ModelCacheStorageRefused(
                    ModelCacheCode.RATE_LIMITED,
                    "artifact provider rate limited this download; it will resume automatically",
                    retry_after_seconds=retry_after,
                    recovery="resume",
                )
            if status_code in {401, 403} and _is_hf_canonical_url(current_url):
                if not token_loaded:
                    token = cache._load_huggingface_token()
                    token_loaded = True
                if token is not None and not authenticated:
                    response.close()
                    authenticated = True
                    continue
                response.close()
                if authenticated:
                    raise ModelCacheStorageRefused(
                        SecurityRefusalReason.MODEL_CACHE_CREDENTIALS_DENIED.value,
                        "Hugging Face denied the configured account or token scope for "
                        f"{_huggingface_access_url(source)}; the download resumes automatically when the token changes",
                        recovery="access_denied",
                    )
                raise ModelCacheStorageRefused(
                    ModelCacheCode.CREDENTIALS_MISSING,
                    "Hugging Face account access is required for "
                    f"{_huggingface_access_url(source)}; HF_TOKEN_FILE credentials are missing; the download resumes automatically when the token changes",
                    recovery="access_required",
                )
            if status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                response.close()
                if not location:
                    raise ModelCacheStorageRefused(
                        ModelCacheCode.REDIRECT_FORBIDDEN,
                        "cache source redirect did not provide a destination",
                    )
                redirected_url = urljoin(current_url, location)
                if not source_is_huggingface or not _is_allowed_huggingface_redirect(
                    redirected_url
                ):
                    raise ModelCacheStorageRefused(
                        ModelCacheCode.REDIRECT_FORBIDDEN,
                        "cache source redirected outside the trusted Hugging Face authorities",
                    )
                current_url = redirected_url
                continue
            if status_code not in {200, 206}:
                retry_after = (
                    _retry_after_seconds(response.headers, now=cache._clock())
                    if status_code >= 500
                    else None
                )
                response.close()
                if status_code >= 500 and source_is_huggingface:
                    cache._streams.throttled(
                        retry_after, f"Hugging Face answered {status_code}"
                    )
                raise ModelCacheStorageUnknown(
                    ModelCacheCode.SOURCE_UNAVAILABLE,
                    f"cache source request failed with status {status_code}",
                    retry_after_seconds=retry_after,
                    source_status=status_code,
                )
            return response
        raise ModelCacheStorageRefused(
            ModelCacheCode.REDIRECT_FORBIDDEN,
            "cache source exceeded the trusted Hugging Face redirect limit",
        )

    def _huggingface_credential_fingerprint(self) -> str:
        """Identify the configured credential file without reading the secret."""
        cache = cast("ModelCacheService", self)

        path = cache._huggingface_token_path
        if path is None:
            return "unconfigured"
        try:
            stat = path.lstat()
        except FileNotFoundError:
            return "absent"
        except OSError:
            return "unreadable"
        return f"{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"

    def _resume_after_credential_change(self) -> int:
        """Requeue authorization failures once the credential file changed."""
        cache = cast("ModelCacheService", self)

        current = cache._huggingface_credential_fingerprint()
        if current == cache._observed_credential_fingerprint:
            return 0
        now = cache._clock()
        resumed = 0
        with cache._lock, cache._session(write=True) as session:
            rows = list(
                session.scalars(
                    select(ModelCacheOperation)
                    .where(ModelCacheOperation.kind.in_(["download", "repair"]))
                    .where(ModelCacheOperation.state == "failed")
                    .order_by(ModelCacheOperation.updated_at.desc())
                    .limit(256)
                    .with_for_update(skip_locked=True)
                )
            )
            for operation in rows:
                failure = cache._canonical_failure(operation)
                if (
                    failure is None
                    or failure.code not in _CREDENTIAL_FAILURE_PUBLIC_CODES
                ):
                    continue
                payload = cache._payload_or_none(operation)
                if payload is None or payload.cancellation is not None:
                    continue
                retry = payload.retry
                if retry.credential_fingerprint == current:
                    continue
                newer = session.scalar(
                    select(func.count())
                    .select_from(ModelCacheOperation)
                    .where(
                        ModelCacheOperation.artifact_set_sha256
                        == operation.artifact_set_sha256,
                        ModelCacheOperation.kind.in_(["download", "repair"]),
                        ModelCacheOperation.created_at > operation.created_at,
                    )
                )
                if newer:
                    # A later request owns this artifact set now.
                    continue
                _store_operation_payload(
                    operation,
                    operation.kind,
                    payload.model_copy(
                        update={
                            "retry": retry.model_copy(
                                update={"credential_fingerprint": None}
                            )
                        }
                    ),
                )
                # The credential changed: withdraw the end and let the core decide
                # again (a queued, immediately claimable operation).
                cache._lifecycle.reopen(operation, now)
                cache._lifecycle.settle(operation, Tick(), now)
                operation.attempt = int(operation.attempt) + 1
                operation.progress = progress_document(
                    cache_phase(_operation_progress(operation), "queued", now)
                )
                resumed += 1
        cache._observed_credential_fingerprint = current
        return resumed

    def _load_huggingface_token(self) -> str | None:
        cache = cast("ModelCacheService", self)
        path = cache._huggingface_token_path
        if path is None:
            return None
        try:
            if path.is_symlink():
                raise ModelCacheCredentialPathUnsafe(
                    "Hugging Face credential path is unsafe"
                )
            if not path.exists():
                return None
            if not path.is_file():
                raise ModelCacheCredentialPathUnsafe(
                    "Hugging Face credential path is unsafe"
                )
            if path.stat().st_size == 0:
                return None
            raw = read_runtime_secret(path)
        except (OSError, RuntimeSecretError):
            raise ModelCacheStorageRefused(
                SecurityRefusalReason.MODEL_CACHE_CREDENTIALS_INVALID.value,
                "Hugging Face credential file is unavailable; configure HF_TOKEN_FILE",
                recovery="credentials_invalid",
            ) from None
        value = raw.strip()
        if not value:
            return None
        try:
            token = value.decode("ascii")
        except UnicodeDecodeError:
            raise ModelCacheStorageRefused(
                SecurityRefusalReason.MODEL_CACHE_CREDENTIALS_INVALID.value,
                "Hugging Face credential file must contain one ASCII bearer token",
                recovery="credentials_invalid",
            ) from None
        if any(character.isspace() for character in token) or "\x00" in token:
            raise ModelCacheStorageRefused(
                SecurityRefusalReason.MODEL_CACHE_CREDENTIALS_INVALID.value,
                "Hugging Face credential file must contain one bearer token",
                recovery="credentials_invalid",
            )
        return token

    def _verify_file(self, path: Path, spec: ArtifactSpec) -> bool:
        if path.is_symlink() or not path.is_file():
            return False
        return verified_files.verify_path(path, spec.sha256, spec.expected_bytes)

    def _object_is_stored(self, spec: ArtifactSpec) -> bool:
        """Whether the object is in the cache: receipt plus exact size.

        Bytes are hashed once, where they enter from Hugging Face, before the
        object is published and its receipt written. Reuse trusts that.
        """
        cache = cast("ModelCacheService", self)
        return cache._object_is_available(spec.sha256, spec.expected_bytes)

    def _manifest_coverage_complete(self, manifest: ArtifactSetManifest) -> bool:
        """Whether every unique object is stored (receipt and size), including empty ones."""
        cache = cast("ModelCacheService", self)
        return all(
            cache._object_is_stored(spec)
            for spec in _unique_artifacts(manifest.artifacts).values()
        )

    def _publish_object(self, spec: ArtifactSpec, part: Path) -> None:
        cache = cast("ModelCacheService", self)
        if not cache._verify_file(part, spec):
            part.unlink(missing_ok=True)
            raise ModelCacheStorageRefused(
                ModelCacheCode.DIGEST_MISMATCH,
                "cache artifact failed verification; the bytes were discarded and the download restarts",
                recovery="resume",
            )
        cache._place_object(spec, part)

    def _place_object(self, spec: ArtifactSpec, verified: Path) -> None:
        """Atomically install bytes whose digest was already verified at ingress."""
        cache = cast("ModelCacheService", self)

        target = cache._object_path(spec.sha256)
        target.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        # Atomic overwrite preserves both the pathname and already-open
        # readers until the verified replacement is ready. Moving the old
        # object aside first creates an availability gap (and a crash window).
        os.replace(verified, target)
        _fsync_directory(target.parent)
