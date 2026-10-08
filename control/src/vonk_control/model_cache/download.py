"""Download."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime
from io import BufferedReader
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import unquote, urlsplit

import httpx2
from sqlalchemy.orm import Session
from vonk_agent_protocol import ModelCacheCode, ModelFileState

from ..artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactLifecycleError,
    reference_gate_is_open_nowait,
)
from ..artifact_reference_scan import require_model_sets_open
from ..categorized_faults import OperationInterrupted
from ..model_cache_ranges import cleanup_ranges, download_ranges, range_partial_bytes
from ..models import ModelCacheOperation, ModelCacheSet
from .artifacts import ArtifactSpec
from .catalog_helpers import _github_release_asset_binding
from .constants import _CHUNK_BYTES, _GITHUB_API_HOST, _PARALLEL_RANGE_WORKERS
from .errors import (
    ModelCacheConflictRefused,
    ModelCacheError,
    ModelCacheResolutionError,
    ModelCacheStorageError,
    ModelCacheStorageRefused,
    ModelCacheStorageUnknown,
    _ArtifactWriterBusy,
)
from .source_helpers import _is_private_host

if TYPE_CHECKING:
    from .service import ModelCacheService


class DownloadMixin:
    """Download behavior of the cache service."""

    def _require_transfer_running(self, operation_id: str) -> None:
        cache = cast("ModelCacheService", self)
        if cache._transfer_stop(operation_id).is_set() or cache._closed.is_set():
            raise OperationInterrupted(
                "model download stopped; partial files preserved"
            )

    def _stream_gate(self, operation_id: str):
        cache = cast("ModelCacheService", self)
        stop = cache._transfer_stop(operation_id)
        return lambda: cache._streams.stream(
            lambda: stop.is_set() or cache._closed.is_set()
        )

    def _download_sequential(
        self,
        spec: ArtifactSpec,
        set_digest: str,
        part: Path,
        offset: int,
        received: int,
        *,
        operation_id: str,
        completed_artifacts: int,
        interrupt_after_bytes: int | None,
    ) -> None:
        cache = cast("ModelCacheService", self)
        try:
            stream, effective_offset, close = cache._open_source(spec, offset)
        except ModelCacheStorageError as error:
            if error.code == ModelCacheCode.SOURCE_SIZE_MISMATCH:
                part.unlink(missing_ok=True)
            raise
        if effective_offset != offset:
            received = effective_offset
        durable_received = received
        try:
            mode = "ab" if effective_offset else "wb"
            with (
                part.open(mode) as output,
                cache._sample_transfer(
                    spec, set_digest, operation_id, completed_artifacts, received
                ) as observe,
            ):

                def sync_received() -> None:
                    nonlocal durable_received
                    output.flush()
                    os.fsync(output.fileno())
                    durable_received = received

                try:
                    # Read at most the pinned remaining bytes, then one extra
                    # byte to reject an oversized source without consuming it.
                    remaining = spec.expected_bytes - received
                    while remaining >= 0:
                        observe(received)
                        if isinstance(stream, BufferedReader):
                            chunk = stream.read(min(_CHUNK_BYTES, remaining + 1))
                        else:
                            chunk = next(stream, b"")
                        if not chunk:
                            break
                        if not isinstance(chunk, bytes):
                            chunk = bytes(chunk)
                        next_received = received + len(chunk)
                        if next_received > spec.expected_bytes:
                            raise ModelCacheStorageRefused(
                                ModelCacheCode.SOURCE_SIZE_MISMATCH,
                                "source returned more bytes than the immutable artifact pin",
                                recovery="resume",
                            )
                        output.write(chunk)
                        cache._streams.record_bytes(len(chunk))
                        received = next_received
                        remaining -= len(chunk)
                        if (
                            interrupt_after_bytes is not None
                            and received >= interrupt_after_bytes
                        ):
                            raise OperationInterrupted(
                                "download interrupted at a durable checkpoint"
                            )
                        # Ordinary buffered writes remain independent of progress.
                        # Completion/interruption syncs once; a crash resumes from
                        # the actual retained file length, never a progress counter.
                        observe(received)
                except (OSError, httpx2.HTTPError, ModelCacheError):
                    # Preserve even a sub-MiB tail when a source fails. Never
                    # publish its byte count until the sync has succeeded.
                    if received > durable_received:
                        sync_received()
                    raise
                if received > durable_received:
                    sync_received()
        except (OSError, httpx2.HTTPError, ModelCacheError) as error:
            if getattr(error, "code", None) == ModelCacheCode.SOURCE_SIZE_MISMATCH:
                # The source disagrees with the pin; retained bytes are
                # untrusted, so the retry starts again from byte zero.
                part.unlink(missing_ok=True)
                durable_received = 0
            cache._checkpoint_artifact(
                spec,
                operation_id=operation_id,
                set_digest=set_digest,
                actual_bytes=durable_received,
                state=ModelFileState.PARTIAL,
                completed_artifacts=completed_artifacts,
            )
            if spec.kind == "github-release.asset" and isinstance(
                error, httpx2.HTTPError
            ):
                raise ModelCacheStorageUnknown(
                    ModelCacheCode.SOURCE_UNAVAILABLE,
                    "GitHub release asset transfer failed",
                    recovery="resume",
                ) from error
            raise
        finally:
            close()
        if received != spec.expected_bytes:
            cache._checkpoint_artifact(
                spec,
                operation_id=operation_id,
                set_digest=set_digest,
                actual_bytes=received,
                state=ModelFileState.PARTIAL,
            )
            raise ModelCacheStorageUnknown(
                ModelCacheCode.SOURCE_TRUNCATED,
                "source ended before the immutable artifact size",
                recovery="resume",
            )

    def _complete_download(
        self,
        spec: ArtifactSpec,
        set_digest: str,
        part: Path,
        operation_id: str,
        completed_artifacts: int,
    ) -> None:
        cache = cast("ModelCacheService", self)
        if cache._transfer_stop(operation_id).is_set() or cache._closed.is_set():
            raise OperationInterrupted(
                "model download stopped; partial files preserved"
            )
        received = spec.expected_bytes
        cache._checkpoint_artifact(
            spec,
            operation_id=operation_id,
            set_digest=set_digest,
            actual_bytes=received,
            state="verifying",
            completed_artifacts=completed_artifacts,
        )
        if not cache._verify_file(part, spec):
            # Never keep bytes that failed the pin: discard them so the
            # automatic retry downloads the artifact again from byte zero.
            part.unlink(missing_ok=True)
            cache._checkpoint_artifact(
                spec,
                operation_id=operation_id,
                set_digest=set_digest,
                actual_bytes=0,
                state=ModelFileState.CORRUPT,
            )
            raise ModelCacheStorageRefused(
                ModelCacheCode.DIGEST_MISMATCH,
                "downloaded artifact failed SHA-256 verification; the bytes were discarded and the download restarts",
                recovery="resume",
            )
        if (
            cache._transfer_stop(operation_id).is_set()
            or cache._closed.is_set()
            or cache._operation_cancellation_pending(operation_id)
        ):
            raise OperationInterrupted("model download cancelled during verification")
        # Removal and publication share this process lock.  The durable
        # operation state is checked while holding it, closing the race where
        # a worker verifies an object just as an operator removes its set.
        with cache._lock:
            if not cache._publication_allowed(operation_id, set_digest, spec.sha256):
                raise OperationInterrupted(
                    "model download was removed during verification"
                )
            cache._publish_object(spec, part)
            cache._mark_artifact_verified(spec, set_digest)

    def _publication_allowed(
        self, operation_id: str, set_digest: str, object_digest: str | None = None
    ) -> bool:
        cache = cast("ModelCacheService", self)
        try:
            with cache._session() as session:
                operation = session.get(ModelCacheOperation, operation_id)
                if operation is None or operation.state == "cancelled":
                    return False
                payload = cache._payload_or_none(operation)
                if payload is None or payload.cancellation is not None:
                    return False  # unreadable: not published; it is reconciled
                if payload.removal_fence is not None:
                    return False
                if session.get(ModelCacheSet, set_digest) is None:
                    return False
                if not reference_gate_is_open_nowait(
                    session, ArtifactIdentity("model-set", set_digest)
                ):
                    return False
                return object_digest is None or reference_gate_is_open_nowait(
                    session, ArtifactIdentity("model-object", object_digest)
                )
        except ArtifactLifecycleError as error:
            if isinstance(cache._sessions, Session) or not error.retryable:
                raise
            raise _ArtifactWriterBusy(object_digest or set_digest) from error

    @staticmethod
    def _require_model_sets_open(
        session: Session,
        set_digests: Sequence[str],
        *,
        now: datetime,
        object_digests: Sequence[str] = (),
        allow_pending_removal: bool = False,
    ) -> dict[str, tuple[str, ...]]:
        try:
            return require_model_sets_open(
                session,
                set_digests,
                now=now,
                object_digests=object_digests,
                allow_pending_removal=allow_pending_removal,
            )
        except ArtifactLifecycleError as error:
            raise ModelCacheConflictRefused(
                error.code,
                error.detail,
                recovery="retry" if error.retryable else None,
            ) from error

    def _validate_http_download(self, spec: ArtifactSpec) -> None:
        cache = cast("ModelCacheService", self)
        if spec.kind == "github-release.asset":
            try:
                _github_release_asset_binding(spec)
                parsed = urlsplit(spec.source)
            except (ModelCacheResolutionError, TypeError, ValueError) as error:
                raise ModelCacheStorageRefused(
                    ModelCacheCode.SOURCE_INVALID, "GitHub release asset URL is invalid"
                ) from error
            if parsed.scheme != "https" or parsed.hostname != _GITHUB_API_HOST:
                raise ModelCacheStorageRefused(
                    ModelCacheCode.SOURCE_UNTRUSTED,
                    "GitHub release downloads must use the canonical GitHub API host",
                )
            return
        try:
            parsed = urlsplit(spec.source)
            hostname = parsed.hostname
            port = parsed.port
        except (TypeError, ValueError) as error:
            raise ModelCacheStorageRefused(
                ModelCacheCode.SOURCE_INVALID, "cache source URL is invalid"
            ) from error
        if not cache._fixture_sources and (
            parsed.scheme != "https"
            or hostname is None
            or port is not None
            or hostname.lower().rstrip(".") not in cache._trusted_source_hosts
            or _is_private_host(hostname)
        ):
            raise ModelCacheStorageRefused(
                ModelCacheCode.SOURCE_UNTRUSTED,
                "production cache downloads require a trusted HTTPS artifact host",
            )

    def _download_parallel_ranges(
        self,
        spec: ArtifactSpec,
        part: Path,
        set_digest: str,
        operation_id: str,
        completed_artifacts: int,
    ) -> bool:
        cache = cast("ModelCacheService", self)
        cache._validate_http_download(spec)
        # Range segments and atomic assembly coexist temporarily. Reserve the
        # worst-case additional footprint across this process's active files.
        # The retained prefix is already excluded from current free space:
        # peak total is prefix + 2*object, but growth is at most 2*object.
        # Existing range segments make growth smaller, never larger. A tight
        # disk uses the ordinary sequential stream instead.
        if (
            cache._http is not None
            and not cache._fixture_sources
            and getattr(cache._http, "follow_redirects", False)
        ):
            raise ModelCacheStorageRefused(
                ModelCacheCode.REDIRECT_FORBIDDEN,
                "production cache HTTP clients must not follow redirects",
            )
        reservation = 2 * spec.expected_bytes
        with cache._lock:
            free = shutil.disk_usage(cache._root).free
            if free - cache._reserve_bytes - cache._range_reserved_bytes < reservation:
                cleanup_ranges(
                    part, spec.expected_bytes, workers=_PARALLEL_RANGE_WORKERS
                )
                cache._checkpoint_artifact(
                    spec,
                    operation_id=operation_id,
                    set_digest=set_digest,
                    actual_bytes=part.stat().st_size if part.exists() else 0,
                    state=ModelFileState.PARTIAL,
                    completed_artifacts=completed_artifacts,
                )
                return False
            cache._range_reserved_bytes += reservation
        client = cache._http
        owns_client = client is None
        try:
            if client is None:
                client = httpx2.Client(
                    follow_redirects=False,
                    timeout=httpx2.Timeout(30.0),
                    trust_env=False,
                )

            def open_range(start: int, end: int) -> httpx2.Response:
                headers = {
                    "Range": f"bytes={start}-{end}",
                    "Accept-Encoding": "identity",
                }
                if spec.kind == "github-release.asset":
                    return cache._open_github_release_asset(client, spec, headers)
                return cache._open_http_response(client, spec.source, headers)

            with cache._sample_transfer(
                spec,
                set_digest,
                operation_id,
                completed_artifacts,
                range_partial_bytes(
                    part, spec.expected_bytes, workers=_PARALLEL_RANGE_WORKERS
                ),
            ) as observe:
                completed = download_ranges(
                    part,
                    spec.expected_bytes,
                    open_range,
                    cache._transfer_stop(operation_id),
                    observe,
                    workers=_PARALLEL_RANGE_WORKERS,
                    stream_gate=cache._stream_gate(operation_id),
                    on_bytes=cache._streams.record_bytes,
                )
            if not completed:
                cache._checkpoint_artifact(
                    spec,
                    operation_id=operation_id,
                    set_digest=set_digest,
                    actual_bytes=part.stat().st_size if part.exists() else 0,
                    state=ModelFileState.PARTIAL,
                    completed_artifacts=completed_artifacts,
                )
            return completed
        except (OSError, httpx2.HTTPError, ValueError, ModelCacheError) as error:
            cache._checkpoint_artifact(
                spec,
                operation_id=operation_id,
                set_digest=set_digest,
                actual_bytes=range_partial_bytes(
                    part, spec.expected_bytes, workers=_PARALLEL_RANGE_WORKERS
                ),
                state=ModelFileState.PARTIAL,
                completed_artifacts=completed_artifacts,
            )
            if spec.kind == "github-release.asset" and isinstance(
                error, httpx2.HTTPError
            ):
                raise ModelCacheStorageUnknown(
                    ModelCacheCode.SOURCE_UNAVAILABLE,
                    "GitHub release asset transfer failed",
                    recovery="resume",
                ) from error
            raise
        finally:
            if owns_client and client is not None:
                client.close()
            with cache._lock:
                cache._range_reserved_bytes -= reservation

    def _open_source(
        self, spec: ArtifactSpec, offset: int
    ) -> tuple[Iterator[bytes] | BufferedReader, int, Callable[[], object]]:
        cache = cast("ModelCacheService", self)
        try:
            parsed = urlsplit(spec.source)
        except (TypeError, ValueError) as error:
            raise ModelCacheStorageRefused(
                ModelCacheCode.SOURCE_INVALID, "cache source URL is invalid"
            ) from error
        if parsed.scheme == "file":
            if not cache._fixture_sources:
                raise ModelCacheStorageRefused(
                    ModelCacheCode.SOURCE_UNTRUSTED,
                    "production cache downloads cannot read file sources",
                )
            path = Path(unquote(parsed.path))
            if path.is_symlink() or not path.is_file():
                raise ModelCacheStorageUnknown(
                    ModelCacheCode.SOURCE_UNAVAILABLE,
                    "cache file source is unavailable",
                )
            handle = path.open("rb")
            size = path.stat().st_size
            if offset > size:
                handle.close()
                raise ModelCacheStorageRefused(
                    ModelCacheCode.SOURCE_SIZE_MISMATCH,
                    "cache file source is shorter than its checkpoint",
                )
            handle.seek(offset)
            return handle, offset, handle.close
        cache._validate_http_download(spec)
        client = cache._http
        owns_client = client is None
        if client is None:
            client = httpx2.Client(
                follow_redirects=False,
                timeout=httpx2.Timeout(30.0),
                trust_env=False,
            )
        elif not cache._fixture_sources and getattr(client, "follow_redirects", False):
            raise ModelCacheStorageRefused(
                ModelCacheCode.REDIRECT_FORBIDDEN,
                "production cache HTTP clients must not follow redirects",
            )
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        response = (
            cache._open_github_release_asset(client, spec, headers)
            if spec.kind == "github-release.asset"
            else cache._open_http_response(client, spec.source, headers)
        )
        effective_offset = offset
        if offset and response.status_code == 200:
            # The server ignored the range request; restart safely rather than
            # appending a complete payload to a checkpoint.
            response.close()
            response = (
                cache._open_github_release_asset(client, spec, {})
                if spec.kind == "github-release.asset"
                else cache._open_http_response(client, spec.source, {})
            )
            effective_offset = 0
        if response.status_code == 206:
            content_range = response.headers.get("content-range", "")
            if not content_range.startswith(f"bytes {effective_offset}-"):
                response.close()
                if owns_client:
                    client.close()
                raise ModelCacheStorageRefused(
                    ModelCacheCode.RANGE_INVALID,
                    "cache source returned an invalid byte range",
                )
        return (
            response.iter_bytes(),
            effective_offset,
            lambda: (response.close(), client.close() if owns_client else None),
        )
