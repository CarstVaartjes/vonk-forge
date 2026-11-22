from __future__ import annotations

import errno
import hashlib
import os
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
import vonk_control.model_cache as model_cache_module
from sqlalchemy import select
from vonk_agent_protocol import LifecycleState
from vonk_control.catalog.entities import CatalogEntityService
from vonk_control.model_cache import (
    _CHUNK_BYTES,
    ArtifactSetManifest,
    ArtifactSpec,
    ModelCacheConflict,
    ModelCacheService,
)
from vonk_control.model_cache_contract import (
    ModelCacheCounters,
    ModelCacheDownloadResult,
    ModelCacheRemovalResult,
)
from vonk_control.models import (
    AgentNode,
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    ModelCacheOperation,
    ModelCacheSet,
)
from vonk_forge_contracts import document_sha256

from . import model_cache_support as _support
from .display_fixtures import create_draft
from .model_cache_support import (
    NOW,
    _admit_fresh_download,
    _artifact,
    _canonical_model,
    _download,
    _first_manifest_artifact,
    _gone_handler,
    _http_artifact,
    _http_cache_service,
    _manifest_document,
    _required_text,
)
from .non_blocking import assert_ended_without_blocking
from .removal_settle import advance_until_ended, release_ended_gates

cache = _support.cache


def _remove_model(
    service: ModelCacheService,
    selector: str,
    *,
    actor: str,
    request_key: str,
    model_content_sha256: str,
):
    return service.remove_model_selector(
        selector,
        actor=actor,
        request_key=request_key,
    )


@pytest.mark.parametrize("streams", [2, 10])
def test_sampled_streams_share_one_progress_write_without_storage_reads(
    cache, tmp_path, streams
):
    """Every stream samples once per second; the cost per window stays flat."""
    from vonk_control.model_cache.checkpoints import PROGRESS_CHECKPOINT_SECONDS

    service, sessions = cache
    clock = [datetime.now(UTC)]
    service._clock = lambda: clock[0]
    raws = [
        _artifact(
            tmp_path,
            bytes([index]) * 100,
            artifact_id=f"shard-{index}",
            path=f"shard-{index}.bin",
        )
        for index in range(streams)
    ]
    operation = service.start_download(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000995",
        artifacts=raws,
    )
    with sessions() as session:
        manifest = ArtifactSetManifest.from_document(
            session.get(ModelCacheOperation, operation.id).payload["manifest"]
        )
    storage_reads = {"count": 0}
    real_stored_object = service._stored_object

    def counted_stored_object(digest, expected_bytes):
        storage_reads["count"] += 1
        return real_stored_object(digest, expected_bytes)

    writes = {"count": 0}
    real_session = service._session

    @contextmanager
    def counted_session(*, write: bool = False):
        writes["count"] += int(write)
        with real_session(write=write) as session:
            yield session

    service._stored_object = counted_stored_object
    service._session = counted_session

    def sample(spec, count: int, state: str = "partial") -> None:
        service._checkpoint_artifact(
            spec,
            operation_id=operation.id,
            set_digest=manifest.digest,
            actual_bytes=count,
            state=state,
            force_progress=state != "partial",
        )

    def sample_every_stream(count: int) -> None:
        for spec in manifest.artifacts:
            sample(spec, count)

    sample_every_stream(10)
    clock[0] += timedelta(seconds=PROGRESS_CHECKPOINT_SECONDS)
    sample_every_stream(20)
    assert storage_reads["count"] == 0
    assert writes["count"] == 2
    # The due write carried every stream's sample, not only its own.
    assert service.get_operation(operation.id).progress["downloaded_bytes"] == (
        20 + 10 * (streams - 1)
    )
    # One file's verification does not hide the other streams' transfer.
    first, *others = manifest.artifacts
    sample(first, 100, state="verifying")
    assert service.get_operation(operation.id).progress["phase"] == "downloading"
    clock[0] += timedelta(seconds=PROGRESS_CHECKPOINT_SECONDS)
    for spec in others:
        sample(spec, 30)
    progress = service.get_operation(operation.id).progress
    assert progress["phase"] == "downloading"
    assert progress["measurement"]["bytes_per_second"] > 0
    # With no stream left transferring, the next stream's start is due at once.
    for spec in others:
        sample(spec, 100, state="verifying")
    assert service.get_operation(operation.id).progress["phase"] == "verifying"
    clock[0] += timedelta(seconds=1)
    sample(first, 0)
    assert service.get_operation(operation.id).progress["phase"] == "downloading"


def test_cache_measurements_handle_unknown_total_and_observation_gap():
    from vonk_control.model_cache_progress import cache_progress, project_cache_progress

    def snapshot(count: int, total: int | None = 100) -> ModelCacheCounters:
        return ModelCacheCounters(
            phase="downloading",
            completed_artifacts=0,
            total_artifacts=1,
            downloaded_bytes=count,
            expected_bytes=total,
        )

    first = cache_progress(snapshot(0), previous=None, now=NOW)
    second = cache_progress(
        snapshot(10), previous=first, now=NOW + timedelta(seconds=1)
    )
    assert second.measurement.eta_seconds == 9
    restarted = cache_progress(
        snapshot(20), previous=second, now=NOW + timedelta(seconds=60)
    )
    assert restarted.measurement.bytes_per_second is None
    unknown = cache_progress(
        snapshot(30, None), previous=restarted, now=NOW + timedelta(seconds=61)
    )
    assert unknown.measurement.bytes_per_second == 10
    assert unknown.measurement.eta_seconds is None
    stale = project_cache_progress(
        unknown.model_dump(mode="json"), NOW + timedelta(seconds=200)
    )
    assert stale["activity"] == "possibly_stalled"
    assert "bytes_per_second" not in stale


def test_large_model_keeps_exact_aggregate_without_truncated_member_list(
    cache, tmp_path
):
    from dataclasses import replace

    service, _ = cache
    raw = _artifact(tmp_path, b"x")
    manifest = service.resolve_artifact_set(artifacts=[raw])
    specs = tuple(
        replace(manifest.artifacts[0], key=f"file-{i}", sha256=f"{i:064x}")
        for i in range(1025)
    )
    large = replace(manifest, artifacts=specs)
    transfer = {
        "artifacts": {
            spec.sha256: {"baseline_bytes": 0, "received_bytes": 0} for spec in specs
        }
    }
    progress = service._progress(large, phase="downloading", transfer=transfer)
    assert progress.measurement.total_items == 1025
    assert progress.measurement.total_bytes == 1025
    assert progress.measurement.members == []


class _FragmentedByteStream(httpx2.SyncByteStream):
    def __init__(
        self,
        payload: bytes,
        *,
        fragment: int,
        fail_after: int | None = None,
    ) -> None:
        self._payload = payload
        self._fragment = fragment
        self._fail_after = fail_after

    def __iter__(self):
        sent = 0
        while sent < len(self._payload):
            if self._fail_after is not None and sent >= self._fail_after:
                raise httpx2.ReadTimeout("read timeout")
            end = min(len(self._payload), sent + self._fragment)
            yield self._payload[sent:end]
            sent = end


def _track_checkpoint_sessions(service: ModelCacheService):
    sampled = []
    writes = {"count": 0}
    real_checkpoint = service._checkpoint_artifact

    def wrapped(spec, **kwargs):
        sampled.append(kwargs.get("force_progress", True))
        real_session = service._session

        @contextmanager
        def tracking_session(*, write: bool = False):
            if write:
                writes["count"] += 1
            with real_session(write=write) as session:
                yield session

        service._session = tracking_session
        try:
            return real_checkpoint(spec, **kwargs)
        finally:
            service._session = real_session

    service._checkpoint_artifact = wrapped
    return sampled, writes


def test_fragmented_http_download_bounds_checkpoints(
    cache, tmp_path: Path, monkeypatch
) -> None:
    _existing, sessions = cache
    payload = bytes(range(256)) * ((4 * _CHUNK_BYTES) // 256)
    fragment = 16 * 1024
    assert len(payload) // fragment > 20
    import vonk_control.model_cache as model_cache_mod

    fsyncs = {"count": 0}
    real_fsync = model_cache_mod.os.fsync

    def counted_fsync(fd):
        fsyncs["count"] += 1
        return real_fsync(fd)

    monkeypatch.setattr(model_cache_mod.os, "fsync", counted_fsync)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            request=request,
            stream=_FragmentedByteStream(payload, fragment=fragment),
        )

    service, client = _http_cache_service(
        tmp_path, sessions, handler, clock=lambda: NOW
    )
    sampled, writes = _track_checkpoint_sessions(service)
    try:
        operation = _download(
            service,
            [_http_artifact(payload)],
            model_content_sha256="b" * 64,
            request_key="00000000-0000-4000-8000-000000000992",
        )
        assert operation.state == "succeeded"
        assert operation.progress["downloaded_bytes"] == len(payload)
        sampled_progress = sampled.count(False)
        expected_chunks = (len(payload) + _CHUNK_BYTES - 1) // _CHUNK_BYTES
        assert sampled_progress <= expected_chunks
        assert sampled_progress < len(payload) // fragment
        assert writes["count"] <= 3
        assert fsyncs["count"] < len(payload) // fragment
        assert fsyncs["count"] <= expected_chunks + 8
    finally:
        service.close()
        client.close()


@pytest.mark.parametrize("mode", ["interrupt", "timeout"])
def test_fragmented_http_final_progress_is_durable_and_resumes(
    cache, tmp_path: Path, mode: str, monkeypatch
) -> None:
    _existing, sessions = cache
    payload = bytes(range(256)) * ((3 * _CHUNK_BYTES) // 256)
    durable_after = 2 * _CHUNK_BYTES + _CHUNK_BYTES // 2
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        range_header = request.headers.get("range")
        if range_header:
            start = int(range_header.removeprefix("bytes=").split("-", 1)[0])
            body = payload[start:]
            end = start + len(body) - 1 if body else start
            return httpx2.Response(
                206,
                request=request,
                content=body,
                headers={"content-range": f"bytes {start}-{end}/{len(payload)}"},
            )
        if mode == "timeout":
            return httpx2.Response(
                200,
                request=request,
                stream=_FragmentedByteStream(
                    payload, fragment=16 * 1024, fail_after=durable_after
                ),
            )
        return httpx2.Response(
            200,
            request=request,
            stream=_FragmentedByteStream(payload, fragment=16 * 1024),
        )

    service, client = _http_cache_service(
        tmp_path, sessions, handler, clock=lambda: NOW
    )
    # Check ordering, not merely the eventual size visible in the page cache.

    synced_sizes: dict[int, int] = {}
    real_fsync = os.fsync
    real_checkpoint = service._checkpoint_artifact

    def synced(fd):
        result = real_fsync(fd)
        info = os.fstat(fd)
        synced_sizes[info.st_ino] = info.st_size
        return result

    def checked_checkpoint(spec, **kwargs):
        part = service._partial_path(kwargs["set_digest"], spec.sha256)
        assert kwargs["actual_bytes"] <= synced_sizes.get(part.stat().st_ino, 0)
        return real_checkpoint(spec, **kwargs)

    monkeypatch.setattr(os, "fsync", synced)
    monkeypatch.setattr(service, "_checkpoint_artifact", checked_checkpoint)
    artifact = _http_artifact(payload)
    try:
        if mode == "interrupt":
            interrupted = service.start_download(
                actor="test",
                request_key="00000000-0000-4000-8000-000000000993",
                model_content_sha256="b" * 64,
                artifacts=[artifact],
                interrupt_after_bytes=durable_after,
            )
            observed = interrupted
            assert observed.state == LifecycleState.BACKOFF
        else:
            queued = service.start_download(
                actor="test",
                request_key="00000000-0000-4000-8000-000000000994",
                model_content_sha256="b" * 64,
                artifacts=[artifact],
            )
            service.run_pending()
            observed = service.get_operation(queued.id)
            assert observed.state == "queued"
        part = (
            service.root
            / "partials"
            / str(observed.artifact_set_sha256)
            / f"{artifact['sha256']}.part"
        )
        assert part.stat().st_size == durable_after
        assert observed.progress["downloaded_bytes"] == durable_after
        assert observed.progress["downloaded_bytes"] == part.stat().st_size
        service.run_pending()
        resumed = service.get_operation(observed.id)
        assert resumed.state == "succeeded"
        assert resumed.progress["downloaded_bytes"] == len(payload)
        assert any(
            request.headers.get("range") == f"bytes={durable_after}-"
            for request in requests
        )
        assert (
            service.root
            / "objects"
            / str(artifact["sha256"])[0:2]
            / str(artifact["sha256"])
        ).read_bytes() == payload
    finally:
        service.close()
        client.close()


def test_fragmented_http_shutdown_preserves_sub_chunk_tail(
    cache, tmp_path: Path
) -> None:
    _existing, sessions = cache
    payload = b"s" * (2 * _CHUNK_BYTES)
    fragments = []

    class ShutdownStream(httpx2.SyncByteStream):
        def __iter__(self):
            fragments.append(1)
            yield payload[:4096]
            service._closed.set()
            fragments.append(2)
            yield payload[4096:8192]
            raise AssertionError("read another fragment after shutdown")

    def handler(request):
        return httpx2.Response(200, request=request, stream=ShutdownStream())

    service, client = _http_cache_service(tmp_path, sessions, handler)
    sampled, _writes = _track_checkpoint_sessions(service)
    artifact = _http_artifact(payload)
    try:
        operation = service.start_download(
            actor="test",
            request_key="00000000-0000-4000-8000-000000000995",
            model_content_sha256="b" * 64,
            artifacts=[artifact],
        )
        service.run_pending()
        observed = service.get_operation(operation.id)
        assert observed.state == LifecycleState.BACKOFF
        assert fragments == [1, 2]
        assert sampled == [True]
        assert observed.progress["downloaded_bytes"] == 8192
        set_digest = _required_text(observed.artifact_set_sha256, "artifact set digest")
        part = service._partial_path(
            set_digest, _required_text(artifact["sha256"], "artifact digest")
        )
        assert part.read_bytes() == payload[:8192]
    finally:
        service.close()
        client.close()


@pytest.mark.parametrize(
    "ending", ["complete", "truncated", "oversized", "fsync_error"]
)
def test_fragmented_http_tail_checkpoint_never_exceeds_synced_bytes(
    cache, tmp_path, monkeypatch, ending
):
    import stat

    _existing, sessions = cache
    payload = b"t" * 8192
    body = payload[:4096] if ending == "truncated" else payload
    if ending == "oversized":
        body += b"extra"
    synced_sizes = {}
    checkpoints = []
    real_fsync = os.fsync

    def sync(fd):
        info = os.fstat(fd)
        if ending == "fsync_error" and stat.S_ISREG(info.st_mode) and info.st_size:
            raise OSError(errno.EIO, "test durability failure")
        result = real_fsync(fd)
        synced_sizes[info.st_ino] = info.st_size
        return result

    monkeypatch.setattr(os, "fsync", sync)

    def handler(request):
        return httpx2.Response(
            200, request=request, stream=_FragmentedByteStream(body, fragment=4096)
        )

    service, client = _http_cache_service(
        tmp_path, sessions, handler, clock=lambda: NOW
    )
    real_checkpoint = service._checkpoint_artifact

    def checkpoint(spec, **kwargs):
        part = service._partial_path(kwargs["set_digest"], spec.sha256)
        count = kwargs["actual_bytes"]
        if not part.exists():
            # Bytes that disagree with the pin were discarded.
            assert count == 0
            checkpoints.append(count)
            return real_checkpoint(spec, **kwargs)
        assert count <= synced_sizes.get(part.stat().st_ino, 0)
        checkpoints.append(count)
        return real_checkpoint(spec, **kwargs)

    monkeypatch.setattr(service, "_checkpoint_artifact", checkpoint)
    try:
        operation = _download(
            service,
            [_http_artifact(payload)],
            model_content_sha256="b" * 64,
            request_key="00000000-0000-4000-8000-000000000996",
        )
        expected = (
            0
            if ending in {"fsync_error", "oversized"}
            else min(len(body), len(payload))
        )
        assert checkpoints[-1] == expected
        assert operation.progress["downloaded_bytes"] == expected
        assert (operation.state == "succeeded") == (ending == "complete")
        if ending != "complete":
            assert not service._object_path(
                hashlib.sha256(payload).hexdigest()
            ).exists()
    finally:
        service.close()
        client.close()


def test_progress_is_sampled_while_source_read_is_blocked(cache, tmp_path, monkeypatch):
    import threading

    _existing, sessions = cache
    payload = b"s" * 8192
    sampled = threading.Event()
    checkpoints = []

    class SlowStream(httpx2.SyncByteStream):
        def __iter__(self):
            yield payload[:4096]
            # The socket has supplied no new fragment. Progress still advances
            # on its own sampler, without forcing buffered data to stable disk.
            assert sampled.wait(30)
            assert checkpoints == [4096]
            yield payload[4096:]

    def handler(request):
        return httpx2.Response(200, request=request, stream=SlowStream())

    service, client = _http_cache_service(
        tmp_path, sessions, handler, clock=lambda: NOW
    )
    real_checkpoint = service._checkpoint_artifact

    def checkpoint(spec, **kwargs):
        result = real_checkpoint(spec, **kwargs)
        if threading.current_thread().name == "vonk-model-progress":
            checkpoints.append(kwargs["actual_bytes"])
            sampled.set()
        return result

    monkeypatch.setattr(service, "_checkpoint_artifact", checkpoint)
    try:
        operation = _download(
            service,
            [_http_artifact(payload)],
            model_content_sha256="b" * 64,
            request_key="00000000-0000-4000-8000-000000000997",
        )
        assert operation.state == "succeeded"
    finally:
        service.close()
        client.close()


@pytest.mark.parametrize("complete_tail", [False, True])
def test_download_resyncs_retained_bytes_after_disk_failure(
    cache, tmp_path, monkeypatch, complete_tail
):
    import stat

    _existing, sessions = cache
    payload = b"r" * 8192
    fail_sync = [True]
    requests = []
    synced_sizes = {}
    real_fsync = os.fsync

    def sync(fd):
        info = os.fstat(fd)
        if fail_sync[0] and stat.S_ISREG(info.st_mode) and info.st_size:
            raise OSError(errno.EIO, "test durability failure")
        result = real_fsync(fd)
        synced_sizes[info.st_ino] = info.st_size
        return result

    monkeypatch.setattr(os, "fsync", sync)

    def handler(request):
        requests.append(request)
        if "range" in request.headers:
            start = int(request.headers["range"].removeprefix("bytes=").split("-")[0])
            return httpx2.Response(
                206,
                request=request,
                content=payload[start:],
                headers={
                    "content-range": f"bytes {start}-{len(payload) - 1}/{len(payload)}"
                },
            )
        return httpx2.Response(
            200,
            request=request,
            stream=_FragmentedByteStream(
                payload, fragment=4096, fail_after=None if complete_tail else 4096
            ),
        )

    service, client = _http_cache_service(
        tmp_path, sessions, handler, clock=lambda: NOW
    )
    artifact = _http_artifact(payload)
    publish = service._publish_object

    def checked_publish(spec, part):
        assert synced_sizes.get(part.stat().st_ino, 0) == len(payload)
        return publish(spec, part)

    monkeypatch.setattr(service, "_publish_object", checked_publish)
    try:

        def download(number):
            return _download(
                service,
                [artifact],
                model_content_sha256="b" * 64,
                request_key=f"00000000-0000-4000-8000-{number:012d}",
            )

        failed = download(998)
        assert failed.state == "queued"
        assert failed.last_error is not None
        assert "test durability failure" in failed.last_error
        assert failed.progress["downloaded_bytes"] == 0
        # The automatic retry must not publish or request a range while
        # retained bytes still cannot be synced, even if their hash matches.
        service.run_pending()
        assert service.get_operation(failed.id).state == "queued"
        assert len(requests) == 1
        fail_sync[0] = False
        service.run_pending()
        recovered = service.get_operation(failed.id)
        assert recovered.state == "succeeded"
        if complete_tail:
            assert len(requests) == 1
        else:
            assert requests[-1].headers["range"] == "bytes=4096-"
        assert (
            service._object_path(
                _required_text(artifact["sha256"], "artifact digest")
            ).read_bytes()
            == payload
        )
    finally:
        service.close()
        client.close()


def test_cancel_queued_download_is_durable_and_idempotent(cache, tmp_path):
    service, sessions = cache
    artifact = _artifact(tmp_path, b"cancel me")
    operation = service.start_download(
        actor="test",
        request_key="00000000-0000-4000-8000-000000001010",
        model_content_sha256="a" * 64,
        artifacts=[artifact],
    )
    cancellation = {
        "actor": "test",
        "request_key": "00000000-0000-4000-8000-000000001110",
        "reason": "test request",
    }
    cancelled = service.cancel_operation(operation.id, **cancellation)
    assert cancelled.state == "cancelled"
    assert cancelled.progress["phase"] == "completed"
    restarted = ModelCacheService(
        sessions, service.root, free_fraction=0, fixture_sources=True
    )
    try:
        assert restarted.run_pending() == 0
        assert (
            restarted.cancel_operation(operation.id, **cancellation).state
            == "cancelled"
        )
        assert not service._object_path(artifact["sha256"]).exists()
    finally:
        restarted.close()

    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        cancelled,
        end=lambda receipt: service.get_operation(receipt.id),
        fresh=lambda _world: _admit_fresh_download(service, "a" * 64, artifact),
    )


def test_cancel_running_download_preserves_partial_and_cannot_be_resurrected(
    cache, tmp_path
):
    _existing, sessions = cache
    payload = b"c" * 8192
    operation_id = []
    api = ModelCacheService(
        sessions, tmp_path / "http-nas-cache", free_fraction=0, fixture_sources=True
    )

    class CancelStream(httpx2.SyncByteStream):
        def __iter__(self):
            yield payload[:4096]
            api.cancel_operation(
                operation_id[0],
                actor="test",
                request_key="00000000-0000-4000-8000-000000001111",
                reason="cancel during transfer",
            )
            # Another service instance owns cancellation; sampler reads its DB
            # state even when the network cannot supply a fragment yet.
            assert service._transfer_stop(operation_id[0]).wait(30)
            yield payload[4096:]

    def handler(request):
        return httpx2.Response(200, request=request, stream=CancelStream())

    service, client = _http_cache_service(tmp_path, sessions, handler)
    artifact = _http_artifact(payload)
    operation = service.start_download(
        actor="test",
        request_key="00000000-0000-4000-8000-000000001011",
        model_content_sha256="b" * 64,
        artifacts=[artifact],
    )
    operation_id.append(operation.id)
    try:
        service.run_pending()
        assert service.get_operation(operation.id).state == "cancelled"
        assert not service._object_path(
            _required_text(artifact["sha256"], "artifact digest")
        ).exists()
        partials = list((service.root / "partials").rglob("*.part"))
        assert len(partials) == 1
        assert partials[0].read_bytes() == payload
        service._finish_succeeded(
            operation.id,
            ModelCacheDownloadResult(
                schema_version=2,
                artifact_set_sha256=str(operation.artifact_set_sha256),
                coverage="complete",
            ),
        )
        assert service.get_operation(operation.id).state == "cancelled"
    finally:
        service.close()
        api.close()
        client.close()


def test_remove_model_supersedes_an_older_download_of_the_same_set(
    cache, tmp_path: Path
):
    service, sessions = cache
    model_a = _canonical_model(
        publisher="vonk-forge",
        slug="remove-model-a",
        file_id="weights",
        file_digest="a" * 64,
    )
    model_b = _canonical_model(
        publisher="vonk-forge",
        slug="remove-model-b",
        file_id="weights",
        file_digest="a" * 64,
    )
    digest_a = document_sha256(model_a.model_dump(mode="json"))
    digest_b = document_sha256(model_b.model_dump(mode="json"))
    with sessions.begin() as session:
        for index, (slug, model, digest) in enumerate(
            (
                ("remove-model-a", model_a, digest_a),
                ("remove-model-b", model_b, digest_b),
            ),
            start=1,
        ):
            document_id = f"00000000-0000-4000-8000-00000000010{index}"
            root = CatalogDocument(
                id=document_id,
                kind="model",
                publisher="vonk-forge",
                slug=slug,
                title=slug,
                created_by="test",
                created_at=NOW,
                updated_at=NOW,
            )
            session.add(root)
            session.flush()
            session.add(
                CatalogDocumentRevision(
                    id=f"00000000-0000-4000-8000-00000000011{index}",
                    document_id=document_id,
                    kind="model",
                    publisher="vonk-forge",
                    slug=slug,
                    revision_number=1,
                    schema_version=2,
                    state="active",
                    document=model.model_dump(mode="json"),
                    content_digest=digest,
                    projected={},
                    created_by="test",
                    created_at=NOW,
                )
            )
            session.flush()
            session.add(
                CatalogDocumentHead(
                    kind=root.kind,
                    publisher=root.publisher,
                    slug=root.slug,
                    active_revision_id=f"00000000-0000-4000-8000-00000000011{index}",
                    generation=1,
                )
            )
        session.add(AgentNode(node_id="spark-live", state="active"))

    payload = b"shared model payload"
    first = _download(
        service,
        [_artifact(tmp_path, payload, model_content_sha256=digest_a)],
        model_content_sha256=digest_a,
        request_key="00000000-0000-4000-8000-000000001030",
    )
    second = _download(
        service,
        [_artifact(tmp_path, payload, model_content_sha256=digest_b)],
        model_content_sha256=digest_b,
        request_key="00000000-0000-4000-8000-000000001031",
    )
    assert first.state == second.state == "succeeded"
    artifact = _artifact(tmp_path, payload, model_content_sha256=digest_a)
    pending = service.start_download(
        actor="operator",
        request_key="00000000-0000-4000-8000-000000001032",
        model_content_sha256=digest_a,
        artifacts=[artifact],
        force=True,
        selector="vonk-forge/remove-model-a",
    )
    removal = _remove_model(
        service,
        "vonk-forge/remove-model-a",
        actor="operator",
        request_key="00000000-0000-4000-8000-000000001033",
        model_content_sha256=digest_a,
    )
    # The newer removal supersedes the older in-flight download of the set.
    assert service.get_operation(pending.id).state == "cancelled"
    for _ in range(10):
        if service.get_operation(removal.id).state == "succeeded":
            break
        service.run_pending()
    assert service.get_operation(removal.id).state == "succeeded"

    with sessions() as session:
        assert (
            session.scalar(
                select(ModelCacheSet).where(
                    ModelCacheSet.model_content_sha256 == digest_a,
                )
            )
            is None
        )
        assert (
            session.scalar(
                select(ModelCacheSet).where(
                    ModelCacheSet.model_content_sha256 == digest_b,
                )
            )
            is not None
        )
        assert session.get(AgentNode, "spark-live") is not None
    # The object shared with model B is retained.
    assert service._object_path(artifact["sha256"]).is_file()

    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        removal,
        end=lambda receipt: service.get_operation(receipt.id),
        fresh=lambda _world: _admit_fresh_download(service, digest_a, artifact),
    )


def test_model_removal_waits_for_in_use_sets_then_completes(
    cache, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, sessions = cache
    model = "e" * 64
    downloaded = _download(
        service,
        [_artifact(tmp_path, b"in use weights", model_content_sha256=model)],
        model_content_sha256=model,
        request_key="00000000-0000-4000-8000-000000001040",
    )
    assert downloaded.state == "succeeded"
    set_digest = str(downloaded.artifact_set_sha256)
    owners = {set_digest: ("installation 1",)}
    from vonk_control.model_cache import removal_execution as _removal_execution

    real_reasons = _removal_execution._reason_projection

    def reasons(findings):
        found = real_reasons(findings)
        return {
            digest: tuple(found.get(digest, ())) + owners.get(digest, ())
            for digest in findings
        }

    monkeypatch.setattr(
        "vonk_control.model_cache.removal_execution._reason_projection",
        reasons,
    )
    removal = service.remove_model_selector(
        model, actor="operator", request_key="00000000-0000-4000-8000-000000001041"
    )
    for _ in range(3):
        service.advance_removals(limit=10)
        with sessions.begin() as session:
            row = session.get(ModelCacheOperation, removal.id)
            assert row is not None
            row.next_action_at = None  # the one retry clock: due now
    waiting = service.get_operation(removal.id)
    assert waiting.state in {"queued", LifecycleState.BACKOFF}
    assert waiting.failure is not None and waiting.failure["retryable"] is True
    with sessions() as session:
        assert session.get(ModelCacheSet, set_digest) is not None

    owners.clear()
    advance_until_ended(service, removal.id, rounds=10)
    assert service.get_operation(removal.id).state == "succeeded"
    with sessions() as session:
        assert session.get(ModelCacheSet, set_digest) is None

    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        removal,
        end=lambda receipt: service.get_operation(receipt.id),
        fresh=lambda _world: _admit_fresh_download(
            service,
            model,
            _artifact(tmp_path, b"in use weights", model_content_sha256=model),
        ),
    )


def _seed_removal(service, tmp_path: Path, model: str, key: str):
    downloaded = _download(
        service,
        [_artifact(tmp_path, b"long wait weights", model_content_sha256=model)],
        model_content_sha256=model,
        request_key=key,
    )
    assert downloaded.state == "succeeded"
    return str(downloaded.artifact_set_sha256)


def _advance_on_fake_clock(
    service, sessions, removal_id: str, clock: list, rounds: int
):
    for _ in range(rounds):
        clock[0] += timedelta(hours=1)
        service.advance_removals(limit=10)
        with sessions.begin() as session:
            row = session.get(ModelCacheOperation, removal_id)
            assert row is not None
            row.next_action_at = None
        if service.get_operation(removal_id).state in {"succeeded", "failed"}:
            break


def test_removal_waiting_past_the_old_lifetime_cap_still_succeeds(
    cache, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, sessions = cache
    set_digest = _seed_removal(
        service, tmp_path, "f" * 64, "00000000-0000-4000-8000-000000001050"
    )
    owners = {set_digest: ("installation 1",)}
    from vonk_control.model_cache import removal_execution as _removal_execution

    real_reasons = _removal_execution._reason_projection
    monkeypatch.setattr(
        "vonk_control.model_cache.removal_execution._reason_projection",
        lambda findings: {
            digest: tuple(real_reasons(findings).get(digest, ()))
            + owners.get(digest, ())
            for digest in findings
        },
    )
    removal = service.remove_model_selector(
        "f" * 64, actor="operator", request_key="00000000-0000-4000-8000-000000001051"
    )
    clock = [service._clock()]
    service._clock = lambda: clock[0]
    # Hours of waiting: far beyond the old created_at + max_failures * max_delay.
    _advance_on_fake_clock(service, sessions, removal.id, clock, rounds=6)
    assert service.get_operation(removal.id).state not in {"succeeded", "failed"}
    owners.clear()
    _advance_on_fake_clock(service, sessions, removal.id, clock, rounds=10)
    assert service.get_operation(removal.id).state == "succeeded"

    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        removal,
        end=lambda receipt: service.get_operation(receipt.id),
        fresh=lambda _world: _admit_fresh_download(
            service,
            "f" * 64,
            _artifact(tmp_path, b"long wait weights", model_content_sha256="f" * 64),
        ),
    )


def test_removal_with_a_deterministic_failure_still_fails_promptly(
    cache, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, sessions = cache
    _seed_removal(service, tmp_path, "d" * 64, "00000000-0000-4000-8000-000000001052")

    def broken(*_args, **_kwargs):
        raise ValueError("invalid exact deletion contract")

    monkeypatch.setattr(service, "_remove_model_object_files", broken)
    removal = service.remove_model_selector(
        "d" * 64, actor="operator", request_key="00000000-0000-4000-8000-000000001053"
    )
    clock = [service._clock()]
    service._clock = lambda: clock[0]
    _advance_on_fake_clock(service, sessions, removal.id, clock, rounds=20)
    ended = service.get_operation(removal.id)
    assert ended.state == LifecycleState.FAILED
    release_ended_gates(service)

    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        removal,
        end=lambda receipt: service.get_operation(receipt.id),
        assert_reason=lambda ended: ended.failure and ended.failure["code"],
        fresh=lambda _world: _admit_fresh_download(
            service,
            "d" * 64,
            _artifact(tmp_path, b"long wait weights", model_content_sha256="d" * 64),
        ),
    )
    assert "invalid exact deletion contract" in str(ended.last_error or ended.failure)


def test_model_removal_child_replay_and_visible_writer_wait(cache, tmp_path: Path):
    service, sessions = cache
    data = b"removal child bytes"
    artifact = _artifact(tmp_path, data)
    downloaded = _download(
        service,
        [artifact],
        model_content_sha256="a" * 64,
        request_key="00000000-0000-4000-8000-000000001060",
    )
    assert downloaded.state == "succeeded"
    assert downloaded.artifact_set_sha256 is not None

    def accept():
        with sessions.begin() as session:
            return service.accept_removal_for_sets_in_session(
                session,
                actor="test",
                request_key="00000000-0000-4000-8000-000000001061",
                selector="recipe-child",
                selected_sets=[downloaded.artifact_set_sha256],
            )

    accepted = accept()
    replay = accept()
    assert replay.id == accepted.id
    digest = str(artifact["sha256"])
    with service._model_storage_lock(digest):
        assert service.advance_removals() == 0
        waiting = service.get_operation(accepted.id)
        assert waiting.state == LifecycleState.BACKOFF
        assert waiting.failure is not None
        assert waiting.failure["retry_time"] is not None
        assert waiting.failure["artifact_key"] == f"object:{digest}"
        assert service._object_path(digest).read_bytes() == data
    resumed_at = service._clock() + timedelta(seconds=120)
    service._clock = lambda: resumed_at
    for _ in range(3):
        service.advance_removals()
    completed = service.get_operation(accepted.id)
    assert completed.state == "succeeded"
    assert completed.failure is None
    assert isinstance(completed.result, ModelCacheRemovalResult)
    assert completed.result.reclaimed_bytes == len(data)
    assert not service._object_path(digest).exists()


@pytest.mark.parametrize(
    "field", ["actor", "request_key", "selector", "removal_fence", "delete_objects"]
)
def test_model_removal_retires_persisted_intent_drift_before_effects(
    cache, tmp_path: Path, field: str
):
    service, sessions = cache
    artifact = _artifact(tmp_path, b"bound bytes")
    downloaded = _download(
        service,
        [artifact],
        model_content_sha256="a" * 64,
        request_key="00000000-0000-4000-8000-000000001070",
    )
    assert downloaded.artifact_set_sha256 is not None
    with sessions.begin() as session:
        accepted = service.accept_removal_for_sets_in_session(
            session,
            actor="test",
            request_key="00000000-0000-4000-8000-000000001071",
            selector="recipe-child",
            selected_sets=[downloaded.artifact_set_sha256],
        )
    # Acceptance can already resolve the exact scope; observation is idempotent.
    service._observe_model_removal_scope(accepted.id)
    with sessions.begin() as session:
        row = session.get(ModelCacheOperation, accepted.id)
        assert row is not None
        if field in {"actor", "request_key"}:
            setattr(row, field, "00000000-0000-4000-8000-000000001072")
        else:
            payload = dict(row.payload)
            payload[field] = (
                []
                if field == "delete_objects"
                else "00000000-0000-4000-8000-000000001072"
            )
            row.payload = payload
    # The drifted plan is destructive intent nobody accepted: nothing is
    # removed, the row ends (kept for inspection) and the worker carries on.
    assert service._advance_model_removal(accepted.id) is False
    assert service._object_path(str(artifact["sha256"])).read_bytes() == b"bound bytes"
    retired = service.get_operation(accepted.id)
    assert retired.state == "failed"
    assert "persisted-state-damaged" in str(retired.last_error)
    assert service.advance_removals() == 0
    fresh = _download(
        service,
        [artifact],
        model_content_sha256="a" * 64,
        request_key="00000000-0000-4000-8000-000000001073",
    )
    assert fresh.id != accepted.id
    assert fresh.state == LifecycleState.SUCCEEDED.value


def test_model_removal_resolves_the_selector_at_acceptance_and_replays_by_key(
    cache,
):
    service, sessions = cache
    model_a = _canonical_model(
        publisher="vonk-forge",
        slug="exact-removal",
        file_id="weights",
        file_digest="a" * 64,
    )
    model_b = _canonical_model(
        publisher="vonk-forge",
        slug="exact-removal",
        file_id="weights",
        file_digest="b" * 64,
    )
    digest_a = document_sha256(model_a.model_dump(mode="json"))
    catalog = CatalogEntityService(sessions, clock=lambda: NOW)
    draft = create_draft(catalog, model_a.model_dump(mode="json"), actor="operator")
    original = catalog.resolve(draft.id, actor="operator")

    request_key = "00000000-0000-4000-8000-000000001052"
    # A stale client-side identity is advisory: the selector resolves now.
    accepted = service.remove_model_selector(
        "vonk-forge/exact-removal",
        actor="operator",
        request_key=request_key,
    )
    assert accepted.model_content_sha256 == digest_a
    assert service.get_operation(accepted.id).model_content_sha256 == digest_a

    successor = catalog.revise(
        original.document_id, model_b.model_dump(mode="json"), actor="operator"
    )
    catalog.resolve(successor.id, actor="operator")

    replay = service.remove_model_selector(
        "vonk-forge/exact-removal",
        actor="operator",
        request_key=request_key,
    )
    assert replay.id == accepted.id
    assert replay.model_content_sha256 == digest_a
    with pytest.raises(ModelCacheConflict):
        service.remove_model_selector(
            "vonk-forge/other-model",
            actor="operator",
            request_key=request_key,
        )

    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        accepted,
        end=lambda receipt: _finish_cache_removal(service, receipt),
        fresh=lambda _world: service.download_model_selector(
            digest_a, actor="operator", request_key=str(uuid.uuid4())
        ),
    )


@pytest.mark.parametrize("range_supported", [True, False])
def test_model_cache_parallel_ranges_publish_or_fall_back(
    cache, tmp_path, monkeypatch, range_supported
):
    import threading

    _existing, sessions = cache
    payload = bytes(range(256)) * 256
    monkeypatch.setattr(
        "vonk_control.model_cache.constants._PARALLEL_RANGE_MIN_BYTES", 1
    )
    barrier = threading.Barrier(4)
    requests = []

    class AfterPeers(httpx2.SyncByteStream):
        """A range body that flows only once every peer is streaming too."""

        def __init__(self, content: bytes) -> None:
            self.content = content

        def __iter__(self):
            barrier.wait(timeout=30)  # fails if the range transfers serialize
            yield self.content

    def handler(request):
        requested = request.headers.get("range")
        requests.append(requested)
        if requested and range_supported:
            start, end = map(int, requested.removeprefix("bytes=").split("-"))
            return httpx2.Response(
                206,
                request=request,
                stream=AfterPeers(payload[start : end + 1]),
                headers={"content-range": f"bytes {start}-{end}/{len(payload)}"},
            )
        return httpx2.Response(200, request=request, content=payload)

    service, client = _http_cache_service(tmp_path, sessions, handler)
    artifact = _http_artifact(payload)
    try:
        operation = _download(
            service,
            [artifact],
            model_content_sha256="b" * 64,
            request_key="00000000-0000-4000-8000-000000001020",
        )
        assert operation.state == "succeeded", operation.last_error
        assert operation.progress["downloaded_bytes"] == len(payload)
        assert (
            service._object_path(
                _required_text(artifact["sha256"], "artifact digest")
            ).read_bytes()
            == payload
        )
        # An ignored range is learned from the first answer: no peer asks.
        assert len([request for request in requests if request]) == (
            4 if range_supported else 1
        )
        assert (None in requests) == (not range_supported)
        assert not list((service.root / "partials").rglob("*.range-*"))
    finally:
        service.close()
        client.close()


def test_cancel_during_verification_does_not_publish(cache, tmp_path, monkeypatch):
    service, sessions = cache
    artifact = _artifact(tmp_path, b"verified but cancelled")
    operation = service.start_download(
        actor="test",
        request_key="00000000-0000-4000-8000-000000001021",
        model_content_sha256="a" * 64,
        artifacts=[artifact],
    )
    api = ModelCacheService(
        sessions, service.root, free_fraction=0, fixture_sources=True
    )
    real_verify = service._verify_file

    def verify(path, spec):
        result = real_verify(path, spec)
        if path.suffix == ".part" and result:
            api.cancel_operation(
                operation.id,
                actor="test",
                request_key="00000000-0000-4000-8000-000000001112",
                reason="cancel during verification",
            )
        return result

    monkeypatch.setattr(service, "_verify_file", verify)
    try:
        service.run_pending()
        assert service.get_operation(operation.id).state == "cancelled"
        assert not service._object_path(artifact["sha256"]).exists()
    finally:
        api.close()


def test_range_disk_reservation_falls_back_without_source_io(
    cache, tmp_path, monkeypatch
):
    import shutil

    service, _sessions = cache
    spec = ArtifactSpec.from_manifest(
        _first_manifest_artifact(_manifest_document(tmp_path))
    )
    partial = service._partial_path("b" * 64, spec.sha256)
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"m")
    usage = shutil.disk_usage(tmp_path)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: type(usage)(100, 99, 1))
    monkeypatch.setattr(service, "_validate_http_download", lambda spec: None)
    assert not service._download_parallel_ranges(spec, partial, "b" * 64, "unused", 0)
    assert partial.read_bytes() == b"m"
    assert service._range_reserved_bytes == 0


@pytest.mark.parametrize("status", [404, 410])
def test_permanent_missing_source_ends_and_admits_a_fresh_download(
    cache, tmp_path: Path, status: int
) -> None:
    _existing, sessions = cache
    handler, payload, served = _gone_handler([status] * 5 + [200])
    service, client = _http_cache_service(
        tmp_path, sessions, handler, clock=lambda: NOW
    )
    try:
        artifact = _http_artifact(payload)
        operation = service.start_download(
            actor="test",
            request_key="00000000-0000-4000-8000-000000000940",
            model_content_sha256="b" * 64,
            artifacts=[artifact],
        )
        # Bounded: a 404 is first a transient wait, never a terminal on sight.
        for _ in range(4):
            service.run_pending()
            waiting = service.get_operation(operation.id)
            assert waiting.state == "queued"
            assert waiting.failure is not None
        service.run_pending()
        gone = service.get_operation(operation.id)
        assert gone.state == "failed"
        assert gone.failure is not None
        assert gone.failure["retryable"] is False
        detail = str(gone.failure["detail"])
        assert "weights.bin" in detail
        assert f"HTTP {status}" in detail
        assert served["count"] == 5
        # Nothing waits any more: no blocker, no retry clock.
        assert gone.blockers == () and gone.next_attempt_at is None
        assert service.run_pending() == 0
        fresh = _download(
            service,
            [artifact],
            model_content_sha256="b" * 64,
            request_key="00000000-0000-4000-8000-000000000945",
        )
        assert fresh.id != gone.id
        assert fresh.state == "succeeded"
        assert service.get_operation(gone.id).state == "failed"
    finally:
        service.close()
        client.close()


def test_a_transient_404_retries_and_then_succeeds(cache, tmp_path: Path) -> None:
    _existing, sessions = cache
    handler, payload, _served = _gone_handler([404, 404, 200])
    service, client = _http_cache_service(
        tmp_path, sessions, handler, clock=lambda: NOW
    )
    try:
        operation = _download(
            service,
            [_http_artifact(payload)],
            model_content_sha256="b" * 64,
            request_key="00000000-0000-4000-8000-000000000941",
        )
        assert operation.state == "queued"
        for _ in range(2):
            service.run_pending()
            operation = service.get_operation(operation.id)
        assert operation.state == "succeeded"
    finally:
        service.close()
        client.close()


def test_a_server_error_never_becomes_source_gone(cache, tmp_path: Path) -> None:
    _existing, sessions = cache
    handler, payload, served = _gone_handler([503] * 5 + [200])
    service, client = _http_cache_service(
        tmp_path, sessions, handler, clock=lambda: NOW
    )
    try:
        operation = _download(
            service,
            [_http_artifact(payload)],
            model_content_sha256="b" * 64,
            request_key="00000000-0000-4000-8000-000000000942",
        )
        # Transport retries and lifecycle retries share this request. Even
        # repeated 503s never count as evidence that the source disappeared.
        for _ in range(model_cache_module._SOURCE_GONE_ATTEMPTS + 3):
            service.run_pending()
            operation = service.get_operation(operation.id)
            if operation.state == LifecycleState.SUCCEEDED.value:
                break
            assert operation.failure is not None
            assert operation.retryable
        assert operation.state == LifecycleState.SUCCEEDED.value
        assert served["count"] == 6
        assert operation.failure is None
        assert operation.next_attempt_at is None
        fresh = _admit_fresh_download(service, "b" * 64, _http_artifact(payload))
        assert fresh.id != operation.id
        service.run_pending()
        assert service.get_operation(fresh.id).state == LifecycleState.SUCCEEDED.value
    finally:
        service.close()
        client.close()


def _finish_cache_removal(service, receipt):
    advance_until_ended(service, receipt.id, rounds=10)
    return service.get_operation(receipt.id)
