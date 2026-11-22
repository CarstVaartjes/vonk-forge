from __future__ import annotations

import errno
import hashlib
import os
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from pydantic import ValidationError
from vonk_agent_protocol import LifecycleState, ProgressPhase
from vonk_control.auth import TokenCodec
from vonk_control.bounded_json import require_mapping, require_sequence
from vonk_control.coordinator_failures import CoordinatorFailures
from vonk_control.distribution import (
    ModelCacheObjectSource,
)
from vonk_control.jobs.queue import JobService
from vonk_control.model_cache import (
    ArtifactSetManifest,
    ModelCacheService,
    ModelCacheStorageError,
    _retry_after_seconds,
)
from vonk_control.model_cache_api import (
    ModelCacheOperationProvider,
    model_cache_operation_provider,
)
from vonk_control.model_cache_contract import (
    ModelCacheCounters,
)
from vonk_control.models import (
    ModelCacheOperation,
)
from vonk_control.operations.item_contract import operation_item
from vonk_control.recovery_policy import RecoveryPolicy
from vonk_control.worker.loop import Worker

from . import model_cache_support as _support
from .model_cache_support import NOW, _artifact, _download, _required_text
from .test_model_cache_recovery_support import observe_unknown

cache = _support.cache


@pytest.mark.parametrize(
    "stored_state",
    [
        "verified",
        "missing",
        "symlink",
        "truncated",
        "unreadable",
        "close_unavailable",
    ],
)
def test_preview_uses_durable_verified_cache_metadata_without_reading_model_bytes(
    cache,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stored_state: str,
) -> None:
    service, _sessions = cache
    model = "7" * 64
    data = b"verified cache model bytes"
    artifact = _artifact(tmp_path, data, model_content_sha256=model)
    _download(
        service,
        [artifact],
        model_content_sha256=model,
        request_key="ea2c5c20-038a-4a0d-bdaa-40d756fb01bb",
    )
    spec = service.resolve_artifact_set(
        model_content_sha256=model,
        artifacts=[artifact],
    ).artifacts[0]
    path = service._object_path(spec.sha256)
    if stored_state == "missing":
        path.unlink()
    elif stored_state == "truncated":
        path.write_bytes(data[:-1])
    elif stored_state == "symlink":
        other = tmp_path / "outside-cache.bin"
        other.write_bytes(data)
        path.unlink()
        path.symlink_to(other)

    def reject_byte_scan(*_args, **_kwargs):
        raise AssertionError("preview reread cached model bytes")

    monkeypatch.setattr(service, "_verify_file", reject_byte_scan)
    open_file = os.open
    close_file = os.close
    if stored_state == "unreadable":

        def deny_cache_object(file, *args, **kwargs):
            if file == path:
                raise PermissionError(errno.EACCES, "cache object is unreadable")
            return open_file(file, *args, **kwargs)

        monkeypatch.setattr(os, "open", deny_cache_object)

    if stored_state == "close_unavailable":
        observed_fds: set[int] = set()

        def observe_object(file, *args, **kwargs):
            descriptor = open_file(file, *args, **kwargs)
            if file == path:
                observed_fds.add(descriptor)
            return descriptor

        def unavailable_close(descriptor):
            close_file(descriptor)
            if descriptor in observed_fds:
                observed_fds.remove(descriptor)
                raise OSError("object close observation unavailable")

        monkeypatch.setattr(os, "open", observe_object)
        monkeypatch.setattr(os, "close", unavailable_close)

    preview = service.download_preview(model_content_sha256=model, artifacts=[artifact])
    expected = len(data) if stored_state in ("verified", "receipt_missing") else 0
    assert preview["already_cached_bytes"] == expected
    assert preview["new_bytes"] == len(data) - expected
    if stored_state in ("unreadable", "close_unavailable"):
        admitted = service.start_download(
            actor="test",
            request_key=str(uuid.uuid4()),
            model_content_sha256=model,
            artifacts=[artifact],
        )
        assert admitted.state == LifecycleState.QUEUED
        monkeypatch.setattr(os, "open", open_file)
        monkeypatch.setattr(os, "close", close_file)
        service.run_pending()
        assert service.get_operation(admitted.id).state == LifecycleState.SUCCEEDED
        recovered = service.download_preview(
            model_content_sha256=model, artifacts=[artifact]
        )
        assert recovered["already_cached_bytes"] == len(data)
        assert recovered["new_bytes"] == 0


def test_admission_checks_storage_size_after_the_read_transaction_closes(
    cache, tmp_path: Path, monkeypatch
) -> None:
    """The SQL read transaction never spans the managed-storage size check.

    It fails on the wrong implementation where ``_managed_cached_objects``
    opens and stats stored objects while its ``with self._session()`` read
    transaction is still open, which puts a PostgreSQL lock edge on a
    filesystem check. The service's own session helper is instrumented, so the
    assertion observes a checked-out transaction rather than mocking it away.
    """

    service, _sessions = cache
    model = "3" * 64
    artifact = _artifact(tmp_path, b"admitted bytes", model_content_sha256=model)
    _download(
        service,
        [artifact],
        model_content_sha256=model,
        request_key="00000000-0000-4000-8000-000000000018",
    )
    sessions_open = 0
    checked_at_size_check: list[int] = []
    original_session = service._session

    @contextmanager
    def counting_session(*, write: bool = False):
        nonlocal sessions_open
        sessions_open += 1
        try:
            with original_session(write=write) as session:
                yield session
        finally:
            sessions_open -= 1

    original_fstat = os.fstat

    def recording_fstat(fd: int):
        checked_at_size_check.append(sessions_open)
        return original_fstat(fd)

    monkeypatch.setattr(service, "_session", counting_session)
    monkeypatch.setattr(os, "fstat", recording_fstat)
    preview = service.download_preview(model_content_sha256=model, artifacts=[artifact])
    assert preview["already_cached_bytes"] == len(b"admitted bytes")
    assert checked_at_size_check == [0]


def test_operation_transfer_progress_counts_only_missing_objects(
    cache, tmp_path: Path
) -> None:
    service, _sessions = cache
    model = "7" * 64
    cached = _artifact(
        tmp_path, b"cached", artifact_id="cached", model_content_sha256=model
    )
    first = _download(
        service,
        [cached],
        model_content_sha256=model,
        request_key="00000000-0000-4000-8000-000000000016",
    )
    assert first.progress["downloaded_bytes"] == len(b"cached")

    missing = _artifact(
        tmp_path,
        b"new object",
        artifact_id="missing",
        path="missing.bin",
        model_content_sha256=model,
    )
    preview = service.download_preview(
        model_content_sha256=model,
        artifacts=[cached, missing],
    )
    assert preview["already_cached_bytes"] == len(b"cached")
    assert preview["new_bytes"] == len(b"new object")
    operation = service.start_download(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000017",
        model_content_sha256=model,
        artifacts=[cached, missing],
    )
    assert operation.progress["expected_bytes"] == len(b"new object")
    service.run_pending()
    operation = service.get_operation(operation.id)
    assert operation.progress["downloaded_bytes"] == len(b"new object")
    assert operation.progress["expected_bytes"] == len(b"new object")


def test_preview_hashes_the_set_identity_once_whatever_the_file_count(
    cache, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production 2026-11-15: a preview re-hashed the whole manifest once per
    missing file, so a many-file model cost seconds and every Library
    assessment ran out of its read budget. The work must not grow with files."""
    service, _sessions = cache
    model = "8" * 64
    shards = [f"shard {index}".encode() for index in range(40)]
    files = [
        _artifact(
            tmp_path,
            shard,
            artifact_id=f"shard-{index}",
            path=f"shard-{index}.bin",
            model_content_sha256=model,
        )
        for index, shard in enumerate(shards)
    ]
    hashed = 0
    identity = ArtifactSetManifest.identity

    def counted(self: ArtifactSetManifest):
        nonlocal hashed
        hashed += 1
        return identity(self)

    monkeypatch.setattr(ArtifactSetManifest, "identity", counted)
    preview = service.download_preview(model_content_sha256=model, artifacts=files)
    assert preview["artifact_count"] == len(files)
    assert preview["new_bytes"] == sum(len(shard) for shard in shards)
    assert hashed <= 2


def test_download_mutation_is_queued_until_the_controller_worker_runs(
    cache, tmp_path: Path
) -> None:
    service, _sessions = cache
    model = "1" * 64
    data = b"queued payload"
    artifact = _artifact(tmp_path, data, model_content_sha256=model)

    operation = service.start_download(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000014",
        model_content_sha256=model,
        artifacts=[artifact],
    )
    assert operation.state == "queued"
    assert operation.attempt == 1
    assert not list(service.root.joinpath("objects").glob("*/*"))

    assert service.run_pending() == 1
    completed = service.get_operation(operation.id)
    assert completed.state == "succeeded"
    assert completed.attempt == 1


def test_activity_provider_filters_pages_and_projects_attempt_and_progress(
    cache, tmp_path: Path
) -> None:
    from vonk_control import operation_api
    from vonk_control.operations import contract as operation_contract

    service, _sessions = cache
    cursor_codec = TokenCodec(b"c" * 32).cursor_codec()
    provider = model_cache_operation_provider(service, cursors=cursor_codec)
    assert isinstance(provider, operation_api.OperationProvider)
    operation_ids: list[str] = []
    for index in range(2):
        model = str(index + 1) * 64
        artifact = _artifact(
            tmp_path,
            f"queued-{index}".encode(),
            artifact_id=f"weights-{index}",
            path=f"weights-{index}.bin",
            model_content_sha256=model,
        )
        operation = service.start_download(
            actor="test",
            request_key=f"00000000-0000-4000-8000-00000000001{index}",
            model_content_sha256=model,
            artifacts=[artifact],
        )
        assert operation.state == "queued"
        assert operation.attempt == 1
        operation_ids.append(operation.id)

    query = operation_api.OperationQuery(limit=1, after=None, state=None, node_id=None)
    standalone = model_cache_operation_provider(service).list_operations(query)
    assert standalone.next_cursor is not None
    assert len(standalone.items) == 1
    first_page = provider.list_operations(query)
    assert isinstance(first_page, operation_api.OperationListPage)
    assert first_page.total == 2
    assert len(first_page.items) == 1
    first = operation_item(first_page.items[0]).model_dump(mode="json")
    assert first["node_ids"] == []
    assert first["attempt"] == 1
    assert first["supported_actions"] == []
    progress = operation_contract.OperationProgress.model_validate(first["progress"])
    assert progress.phase == "queued"
    assert progress.completed_bytes == 0
    assert progress.total_bytes_known is True
    assert first_page.next_cursor
    decoded_cursor = cursor_codec.decode(
        first_page.next_cursor,
        resource="model-cache-operations",
        order="created-at-desc/id-desc/v1",
        context={"state": None, "node_id": None, "request_id": None},
    )
    assert isinstance(decoded_cursor, list)
    assert len(decoded_cursor) == 2

    after = (
        datetime.fromisoformat(str(decoded_cursor[0])),
        str(decoded_cursor[1]),
    )
    second_page = provider.list_operations(
        operation_api.OperationQuery(limit=1, after=after, state=None, node_id=None)
    )
    assert second_page.total == 2
    assert len(second_page.items) == 1
    assert operation_item(second_page.items[0]).id != first["id"]
    assert second_page.next_cursor is None

    queued_page = provider.list_operations(
        operation_api.OperationQuery(
            limit=100, after=None, state="queued", node_id=None
        )
    )
    assert queued_page.total == 2
    assert {operation_item(item).id for item in queued_page.items} == set(operation_ids)

    node_page = provider.list_operations(
        operation_api.OperationQuery(
            limit=100,
            after=None,
            state=None,
            node_id="spk_" + "a" * 32,
        )
    )
    assert list(node_page.items) == []
    assert node_page.total == 0
    detail = operation_item(provider.get_operation(str(first["id"]))).model_dump(
        mode="json"
    )
    assert detail["id"] == first["id"]
    assert detail["node_ids"] == []
    assert detail["attempt"] == 1

    assert service.run_pending(limit=2) == 2
    succeeded_page = provider.list_operations(
        operation_api.OperationQuery(
            limit=100, after=None, state="succeeded", node_id=None
        )
    )
    assert succeeded_page.total == 2
    assert all(
        operation_item(item).state == "succeeded" for item in succeeded_page.items
    )


@pytest.mark.parametrize("clears", [False, True])
def test_malformed_pagination_observation_is_partial_and_recovers(
    cache, monkeypatch, clears
) -> None:
    """The public provider observes at most three times and retains readable rows."""
    from vonk_control.operation_api import OperationQuery

    service, _sessions = cache
    provider = ModelCacheOperationProvider(
        service, TokenCodec(b"c" * 32).cursor_codec()
    )
    original = service.activity_operations
    calls = 0

    def observe(*args, **kwargs):
        nonlocal calls
        calls += 1
        page = original(*args, **kwargs)
        if not clears or calls < 3:
            page["_next_boundary"] = ("only-one",)
        return page

    monkeypatch.setattr(service, "activity_operations", observe)
    query = OperationQuery(limit=10, after=None, state=None, node_id=None)
    result = provider.list_operations(query)
    assert calls == 3
    assert result.continuation_unavailable is not clears
    assert result.next_cursor is None
    # A fresh request has a fresh budget; the previous read retains no gate.
    monkeypatch.setattr(service, "activity_operations", original)
    fresh = provider.list_operations(query)
    assert not fresh.continuation_unavailable
    assert fresh.items == result.items and fresh.total == result.total


def test_interrupted_download_checkpoint_resumes_after_service_restart(
    cache, tmp_path: Path
) -> None:
    service, sessions = cache
    model = "c" * 64
    data = bytes(range(256)) * 12_000
    artifact = _artifact(tmp_path, data, model_content_sha256=model)

    partial = service.start_download(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000003",
        model_content_sha256=model,
        artifacts=[artifact],
        interrupt_after_bytes=1_100_000,
    )
    assert partial.state == LifecycleState.BACKOFF
    assert partial.attempt == 1
    assert partial.progress["expected_bytes"] == len(data)
    checkpoint_bytes = (
        (
            service.root
            / "partials"
            / str(partial.artifact_set_sha256)
            / f"{artifact['sha256']}.part"
        )
        .stat()
        .st_size
    )
    assert partial.progress["downloaded_bytes"] == checkpoint_bytes
    # Replay uses the original plan identity even though the current preview
    # now sees a shorter remaining range.
    replay = service.start_download(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000003",
        model_content_sha256=model,
        artifacts=[artifact],
    )
    assert replay.id == partial.id
    assert replay.progress["downloaded_bytes"] == partial.progress["downloaded_bytes"]
    set_digest = partial.artifact_set_sha256 or ""
    part = service.root / "partials" / set_digest / f"{artifact['sha256']}.part"
    assert 0 < part.stat().st_size < len(data)

    restarted = ModelCacheService(
        sessions,
        service.root,
        free_fraction=0,
        fixture_sources=True,
    )
    assert restarted.resume_operations() == 1
    restarted.run_pending()
    resumed = restarted.get_operation(partial.id)
    assert resumed.state == "succeeded"
    assert resumed.progress["downloaded_bytes"] == len(data)
    assert resumed.progress["expected_bytes"] == len(data)
    assert resumed.attempt == 2
    assert (
        service.root
        / "objects"
        / str(artifact["sha256"])[0:2]
        / str(artifact["sha256"]).strip()
    ).read_bytes() == data
    assert restarted.get_entry(set_digest)["coverage"] == "complete"


def test_transient_download_failure_requeues_with_exact_identity_and_bound(
    cache, tmp_path: Path
) -> None:
    service, _sessions = cache
    model = "e" * 64
    data = b"retryable payload"
    artifact = _artifact(tmp_path, data, model_content_sha256=model)
    preview = service.download_preview(model_content_sha256=model, artifacts=[artifact])
    original = service._open_source
    calls = 0

    def flaky_source(spec, offset):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(errno.ETIMEDOUT, "network copy temporarily unavailable")
        return original(spec, offset)

    service._open_source = flaky_source
    operation = service.start_download(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000019",
        model_content_sha256=model,
        artifacts=[artifact],
    )
    service.run_pending()
    queued = service.get_operation(operation.id)
    assert queued.state == "queued"
    # The attempt number counts claims: the retry shows the attempt that failed
    # until the next claim starts attempt 2.
    assert queued.attempt == 1
    assert queued.plan_digest == preview["plan_digest"]
    assert queued.artifact_set_sha256 == preview["artifact_set_sha256"]
    assert queued.progress["downloaded_bytes"] == 0

    service.run_pending()
    completed = service.get_operation(operation.id)
    assert completed.state == "succeeded"
    # A network timeout is a wait: it spends no attempt, so the same attempt
    # resumes and completes.
    assert completed.attempt == 1
    assert completed.plan_digest == queued.plan_digest
    assert completed.artifact_set_sha256 == queued.artifact_set_sha256


def test_transient_download_failures_wait_on_a_bounded_cadence_until_success(
    cache, tmp_path: Path
) -> None:
    service, sessions = cache
    model = "a" * 64
    data = b"persistent retry payload"
    artifact = _artifact(tmp_path, data, model_content_sha256=model)
    original = service._open_source
    calls = 0
    failures = 4

    def flaky_source(spec, offset):
        nonlocal calls
        calls += 1
        if calls <= failures:
            raise OSError(errno.ETIMEDOUT, "timed out")
        return original(spec, offset)

    service._open_source = flaky_source
    operation = service.start_download(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000020",
        model_content_sha256=model,
        artifacts=[artifact],
    )
    delays = []
    for _ in range(failures):
        assert service.run_pending() == 1
        waiting = service.get_operation(operation.id)
        assert waiting.state == "queued"
        assert waiting.failure is not None and waiting.failure["retryable"] is True
        delays.append(waiting.failure["retry_after_seconds"])
    # A network timeout is a wait: it is re-checked on the one bounded,
    # jittered wait cadence, never backed off by failures it did not spend and
    # never exhausting the retry limit however often it recurs.
    policy = RecoveryPolicy()
    assert all(1 <= delay <= policy.max_delay_seconds for delay in delays)
    assert max(delays) <= 2 * min(delays) + 1

    restarted = ModelCacheService(
        sessions, service.root, free_fraction=0, fixture_sources=True
    )
    restarted._open_source = flaky_source
    assert restarted.resume_operations() == 1
    restarted.run_pending()
    assert restarted.get_operation(operation.id).state == "succeeded"


def test_waiting_download_reports_queued_with_blockers_and_next_attempt(
    cache, tmp_path: Path, caplog
) -> None:
    service, _sessions = cache
    model = "d" * 64
    artifact = _artifact(tmp_path, b"waiting payload", model_content_sha256=model)
    original = service._open_source
    failing = True

    def flaky_source(spec, offset):
        if failing:
            raise OSError(errno.ETIMEDOUT, "timed out")
        return original(spec, offset)

    service._open_source = flaky_source
    operation = service.start_download(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000021",
        model_content_sha256=model,
        artifacts=[artifact],
    )
    with caplog.at_level("INFO"):
        service.run_pending()
    waiting = service.get_operation(operation.id)
    assert waiting.state == "queued"
    assert [item.severity for item in waiting.blockers] == ["warning"]
    assert waiting.next_attempt_at is not None
    assert sum("is waiting" in line for line in caplog.messages) == 1

    failing = False
    service.run_pending()
    done = service.get_operation(operation.id)
    assert done.state == "succeeded"
    assert done.blockers == () and done.next_attempt_at is None


def test_operator_retry_of_a_failed_download_is_always_accepted(
    cache, tmp_path: Path
) -> None:
    service, _sessions = cache
    model = "c" * 64
    artifact = _artifact(tmp_path, b"retry after terminal", model_content_sha256=model)
    original = service._open_source
    denied = [True]

    def source(spec, offset):
        if denied[0]:
            raise ModelCacheStorageError(
                "model_cache.source_untrusted", "source is not trusted"
            )
        return original(spec, offset)

    service._open_source = source
    operation = service.start_download(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000022",
        model_content_sha256=model,
        artifacts=[artifact],
    )
    previous = operation
    for number in range(23, 28):
        service.run_pending()
        failed = service.get_operation(previous.id)
        assert failed.state == "failed"
        previous = service.retry(
            failed.id,
            actor="operator",
            request_key=f"00000000-0000-4000-8000-{number:012d}",
        )
        assert previous.state == "queued"
    denied[0] = False
    service.run_pending()
    assert service.get_operation(previous.id).state == "succeeded"


def test_provider_retry_after_and_rate_limit_reset_are_bounded_hints() -> None:
    now = datetime(2026, 9, 5, 12, tzinfo=UTC)
    assert _retry_after_seconds({"retry-after": "7"}, now=now) == 7
    assert _retry_after_seconds({"ratelimit-reset": "30"}, now=now) == 30
    assert (
        _retry_after_seconds(
            {"x-ratelimit-reset": str(int(now.timestamp()) + 11)}, now=now
        )
        == 11
    )
    assert _retry_after_seconds({"RateLimit": '"resolvers";r=0;t=123'}, now=now) == 123
    assert (
        _retry_after_seconds(
            {"Retry-After": "7", "RateLimit": '"resolvers";r=0;t=123'}, now=now
        )
        == 123
    )
    assert (
        _retry_after_seconds({"RateLimit": '"resolvers";r=0;t=123junk'}, now=now)
        is None
    )
    assert (
        _retry_after_seconds(
            {"RateLimit": '"resolvers";r=0;t=999999999999999999999'}, now=now
        )
        == 365 * 24 * 60 * 60
    )


def test_worker_prefers_nonblocking_cache_tick_when_available(cache) -> None:
    calls: list[str] = []

    class Jobs(JobService):
        def __init__(self) -> None:
            pass

        def claim(self, *_args, **_kwargs):
            raise AssertionError("generic jobs should not run before cache tick")

    class Cache:
        def tick(self):
            calls.append("tick")
            return True

    worker = Worker(
        Jobs(),
        "worker",
        {},
        model_cache=Cache(),
        coordinator_failures=CoordinatorFailures(cache[1], lambda: NOW, "a" * 64),
    )
    assert worker.run_once() is True
    assert calls == ["tick"]


def test_same_pin_repair_verifies_before_atomic_replace_and_preserves_old_bytes(
    cache, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _sessions = cache
    model = "d" * 64
    good = b"good payload"
    artifact = _artifact(
        tmp_path, good, artifact_id="weights", model_content_sha256=model
    )
    source = tmp_path / "weights.source"
    set_digest = (
        _download(
            service,
            [artifact],
            model_content_sha256=model,
            request_key="00000000-0000-4000-8000-000000000004",
        ).artifact_set_sha256
        or ""
    )
    target = (
        service.root
        / "objects"
        / str(artifact["sha256"])[0:2]
        / str(artifact["sha256"])
    )
    assert target.read_bytes() == good

    source.write_bytes(b"bad! payload")
    failed = service.start_repair(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000005",
        artifact_set_sha256=set_digest,
    )
    service.run_pending()
    failed = service.get_operation(failed.id)
    # Bad bytes never replace the verified object; they are discarded and the
    # repair keeps retrying until the source serves the pinned content.
    assert failed.state == "queued"
    assert failed.failure is not None
    assert target.read_bytes() == good

    source.write_bytes(good)
    replace = __import__("vonk_control.model_cache", fromlist=["os"]).os.replace
    calls = 0

    def fail_final_replace(source_path, target_path):
        nonlocal calls
        calls += 1
        assert target.read_bytes() == good
        if target_path == target:
            raise OSError("simulated atomic publish failure")
        return replace(source_path, target_path)

    monkeypatch.setattr("vonk_control.model_cache.os.replace", fail_final_replace)
    service.run_pending()
    swap_failed = service.get_operation(failed.id)
    assert swap_failed.state == "queued"
    assert target.read_bytes() == good
    assert not any(service.root.joinpath("quarantine").iterdir())

    monkeypatch.undo()
    service.run_pending()
    repaired = service.get_operation(failed.id)
    assert repaired.state == "succeeded"
    assert target.read_bytes() == good


def test_distribution_manifest_uses_receipts_and_serves_without_hashing(
    cache, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    service, _sessions = cache
    primary = _artifact(tmp_path, b"primary weights")
    auxiliary = _artifact(
        tmp_path, b"tokenizer", artifact_id="tokenizer", path="tokenizer.json"
    )
    operation = _download(
        service,
        [primary, auxiliary],
        model_content_sha256="a" * 64,
        request_key="ef456dfd-03ba-44a8-9304-219c721dc908",
    )
    assert operation.artifact_set_sha256 is not None
    checks = []
    verify = service._verify_file

    def record_check(path, spec):
        checks.append(spec.sha256)
        return verify(path, spec)

    monkeypatch.setattr(service, "_verify_file", record_check)
    source = ModelCacheObjectSource.from_service(service)
    objects = source.objects_for_set(operation.artifact_set_sha256)
    assert len(objects) == 2
    assert checks == [], "loading descriptors must not scan model bytes"
    digest = str(primary["sha256"])
    opened = source.open_object(digest, len(b"primary weights"))
    try:
        assert opened.stream.read() == b"primary weights"
    finally:
        opened.stream.close()
    assert checks == [], "serving must not hash any cached object"

    # Serving trusts the ingress-verified receipt but still checks that the
    # stored object has the exact recorded size.
    service._object_path(digest).write_bytes(b"truncated")
    with observe_unknown():
        observed = source.open_object(digest, len(b"primary weights"))
        assert not observed
    fresh = _download(
        service,
        [primary, auxiliary],
        model_content_sha256="a" * 64,
        request_key=str(uuid.uuid4()),
    )
    # The read queued recovery first; drive both admitted requests.
    service.run_pending(limit=16)
    assert service.get_operation(fresh.id).state == LifecycleState.SUCCEEDED
    available = source.open_object(digest, len(b"primary weights"))
    try:
        assert available.stream.read() == b"primary weights"
    finally:
        available.stream.close()


def test_verified_serving_seam_refuses_incomplete_or_tampered_sets(
    cache, tmp_path: Path
) -> None:
    service, _sessions = cache
    model = "f" * 64
    data = b"bounded bytes"
    artifact = _artifact(tmp_path, data, model_content_sha256=model)
    interrupted = _download(
        service,
        [artifact],
        model_content_sha256=model,
        request_key="00000000-0000-4000-8000-000000000013",
        interrupt_after_bytes=1,
    )
    with observe_unknown():
        observed = service.resolve_verified_artifact_set(
            interrupted.artifact_set_sha256 or ""
        )
        assert not observed

    assert service.resume_operations() == 1
    service.run_pending()
    set_digest = interrupted.artifact_set_sha256 or ""
    target = (
        service.root
        / "objects"
        / str(artifact["sha256"])[0:2]
        / str(artifact["sha256"])
    )
    assert (
        service.read_verified_artifact(
            set_digest, str(artifact["sha256"]), "weights.bin"
        )
        == data
    )
    target.write_bytes(b"tampered!!!")
    with observe_unknown():
        observed = service.read_verified_artifact(
            set_digest, str(artifact["sha256"]), "weights.bin"
        )
        assert not observed


def test_controller_worker_drains_queued_cache_operations_without_inline_api_transfer(
    cache,
) -> None:
    calls: list[int] = []

    class Jobs(JobService):
        def __init__(self) -> None:
            pass

        def claim(self, *_args, **_kwargs):
            raise AssertionError("cache work should be selected before generic jobs")

    class CacheWorker:
        def run_pending(self, *, limit: int) -> int:
            calls.append(limit)
            return 1

    worker = Worker(
        Jobs(),
        "worker",
        {},
        model_cache=CacheWorker(),
        coordinator_failures=CoordinatorFailures(cache[1], lambda: NOW, "a" * 64),
    )
    assert worker.run_once() is True
    assert calls == [1]


def test_empty_http_support_artifact_does_not_issue_an_invalid_zero_range(
    cache, tmp_path: Path
) -> None:
    _service, sessions = cache
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, request=request, content=b"")

    http_client = httpx2.Client(
        transport=httpx2.MockTransport(handler),
        follow_redirects=False,
    )
    service = ModelCacheService(
        sessions,
        tmp_path / "http-nas-cache",
        free_fraction=0,
        http_client=http_client,
        fixture_sources=True,
    )
    artifact = {
        "id": "metadata",
        "path": "config/empty.json",
        "kind": "http.file",
        "source": "http://fixture.invalid/empty",
        "repository": "http://fixture.invalid/empty",
        "revision": "fixture-revision",
        "sha256": hashlib.sha256(b"").hexdigest(),
        "download_bytes": 0,
        "roles": ["auxiliary"],
        "model_content_sha256": "e" * 64,
    }
    try:
        operation = _download(
            service,
            [artifact],
            model_content_sha256="e" * 64,
            request_key="00000000-0000-4000-8000-000000000018",
        )
        assert operation.state == "succeeded"
        assert len(requests) == 1
        assert requests[0].headers.get("range") is None
        assert (
            service.get_entry(operation.artifact_set_sha256 or "")["coverage"]
            == "complete"
        )
    finally:
        http_client.close()


def test_activity_progress_with_unknown_total_has_no_rate_or_eta_fields() -> None:
    from vonk_control.model_cache_progress import cache_progress, progress_document

    value = progress_document(
        cache_progress(
            ModelCacheCounters(
                phase="downloading",
                completed_artifacts=0,
                total_artifacts=1,
                downloaded_bytes=12,
                expected_bytes=None,
            ),
            previous=None,
            now=NOW,
        )
    )
    progress = ModelCacheOperationProvider._progress(value)
    assert progress["phase"] == ProgressPhase.DOWNLOADING
    assert progress["completed_bytes"] == 12
    assert progress["total_bytes_known"] is False
    assert "eta_seconds" not in progress
    assert "bytes_per_second" not in progress
    with pytest.raises(ValidationError):
        ModelCacheOperationProvider._progress(
            {"phase": "downloading", "downloaded_bytes": 12}
        )


def test_repair_resumes_quarantined_bytes_after_restart(cache, tmp_path, monkeypatch):
    service, sessions = cache
    data = b"x" * (2 * 1024 * 1024 + 3)
    artifact = _artifact(tmp_path, data)
    small = _artifact(tmp_path, b"config", artifact_id="tokenizer", path="config.json")
    downloaded = _download(
        service,
        [small, artifact],
        model_content_sha256="a" * 64,
        request_key="00000000-0000-4000-8000-000000001001",
    )
    digest = downloaded.artifact_set_sha256
    assert digest is not None
    repair = service.start_repair(
        actor="test",
        request_key="00000000-0000-4000-8000-000000001002",
        artifact_set_sha256=digest,
    )
    service._run_download(repair.id, force=True, interrupt_after_bytes=1024 * 1024)
    assert service.get_operation(repair.id).state == LifecycleState.BACKOFF
    available = service.download_preview(
        model_content_sha256="a" * 64, artifacts=[small, artifact]
    )
    assert available["already_cached_bytes"] == len(data) + len(b"config")
    artifact_digest = _required_text(artifact["sha256"], "artifact digest")
    assert (
        service.read_verified_artifact(digest, artifact_digest, "weights.bin") == data
    )
    service.close()
    restarted = ModelCacheService(
        sessions, service.root, free_fraction=0, fixture_sources=True
    )
    offsets = []
    original = restarted._open_source

    def open_source(spec, offset):
        offsets.append(offset)
        return original(spec, offset)

    monkeypatch.setattr(restarted, "_open_source", open_source)
    restarted.run_pending()
    assert restarted.get_operation(repair.id).state == "succeeded"
    assert offsets == [1024 * 1024]
    assert (
        restarted.read_verified_artifact(digest, artifact_digest, "weights.bin") == data
    )
    restarted.close()


def test_serving_one_object_does_not_inspect_the_rest_of_the_set(cache, tmp_path):
    service, _ = cache
    first = _artifact(tmp_path, b"served", artifact_id="a", path="a.bin")
    second = _artifact(tmp_path, b"other", artifact_id="b", path="b.bin")
    digest = _download(
        service,
        [first, second],
        model_content_sha256="a" * 64,
        request_key="00000000-0000-4000-8000-000000001004",
    ).artifact_set_sha256
    inspected = []
    original = service._object_is_available

    def spy(object_digest, expected_bytes):
        inspected.append(object_digest)
        return original(object_digest, expected_bytes)

    service._object_is_available = spy
    path, size, _ = service.cached_artifact_file(digest, first["sha256"], "a.bin")
    assert path.read_bytes() == b"served" and size == 6
    assert inspected == [first["sha256"]]
    # A damaged object is still refused when it is the one being served.
    service._object_path(second["sha256"]).unlink()
    with observe_unknown():
        observed = service.cached_artifact_file(digest, second["sha256"], "b.bin")
        assert not observed
    fresh = _download(
        service,
        [first, second],
        model_content_sha256="a" * 64,
        request_key=str(uuid.uuid4()),
    )
    # The read queued recovery first; drive both admitted requests.
    service.run_pending(limit=16)
    assert service.get_operation(fresh.id).state == LifecycleState.SUCCEEDED
    assert (
        service.cached_artifact_file(digest, second["sha256"], "b.bin")[0].read_bytes()
        == b"other"
    )


def test_atomic_repair_keeps_path_and_open_reader_available(
    cache, tmp_path, monkeypatch
):
    service, _ = cache
    data = b"immutable model"
    artifact = _artifact(tmp_path, data)
    downloaded = _download(
        service,
        [artifact],
        model_content_sha256="a" * 64,
        request_key="00000000-0000-4000-8000-000000001003",
    )
    digest = downloaded.artifact_set_sha256
    target, _, _ = service.cached_artifact_file(
        digest, artifact["sha256"], "weights.bin"
    )
    original = __import__("os").replace
    replacements = []
    with target.open("rb") as reader:

        def replace(source, destination):
            # The managed-storage receipt uses the same atomic rename. This test
            # is about the object's pathname and its already-open reader.
            if destination != target:
                original(source, destination)
                return
            assert target.read_bytes() == data
            assert destination == target
            original(source, destination)
            assert target.read_bytes() == data
            assert reader.read() == data
            replacements.append(destination)

        monkeypatch.setattr("vonk_control.model_cache.os.replace", replace)
        repair = service.start_repair(
            actor="test",
            request_key="00000000-0000-4000-8000-000000001004",
            artifact_set_sha256=digest,
        )
        service.run_pending()
    assert service.get_operation(repair.id).state == "succeeded"
    assert replacements == [target]


def test_reconciliation_and_serving_do_not_rehash_cached_objects(
    cache, tmp_path, monkeypatch
):
    service, _ = cache
    artifact = _artifact(tmp_path, b"good")
    downloaded = _download(
        service,
        [artifact],
        model_content_sha256="a" * 64,
        request_key="00000000-0000-4000-8000-000000001005",
    )

    def fail_hash(*_args, **_kwargs):
        raise AssertionError("cached objects are hashed once, on ingress")

    monkeypatch.setattr(service, "_verify_file", fail_hash)
    service.reconcile_storage()
    service.get_entry(downloaded.artifact_set_sha256)
    _path, size, digest = service.cached_artifact_file(
        downloaded.artifact_set_sha256, artifact["sha256"], "weights.bin"
    )
    assert (size, digest) == (4, artifact["sha256"])
    # Size and receipt are the reuse check: a resized object is not served.
    service._object_path(artifact["sha256"]).write_bytes(b"evil!")
    with observe_unknown():
        observed = service.cached_artifact_file(
            downloaded.artifact_set_sha256, artifact["sha256"], "weights.bin"
        )
        assert not observed
    monkeypatch.undo()
    fresh = _download(
        service,
        [artifact],
        model_content_sha256="a" * 64,
        request_key=str(uuid.uuid4()),
    )
    # The read queued recovery first; drive both admitted requests.
    service.run_pending(limit=16)
    assert service.get_operation(fresh.id).state == LifecycleState.SUCCEEDED
    assert (
        service.cached_artifact_file(
            downloaded.artifact_set_sha256, artifact["sha256"], "weights.bin"
        )[0].read_bytes()
        == b"good"
    )


def test_ingress_rejects_bytes_that_do_not_match_the_pinned_digest(cache, tmp_path):
    service, _ = cache
    artifact = _artifact(tmp_path, b"good payload")
    artifact["sha256"] = hashlib.sha256(b"evil payload").hexdigest()
    failed = _download(
        service,
        [artifact],
        model_content_sha256="a" * 64,
        request_key="00000000-0000-4000-8000-000000001007",
    )
    assert failed.state != "succeeded"
    assert not service._object_path(str(artifact["sha256"])).exists()


def test_repair_capacity_admission_preserves_verified_object(
    cache, tmp_path, monkeypatch
):
    from collections import namedtuple

    service, _ = cache
    artifact = _artifact(tmp_path, b"model")
    downloaded = _download(
        service,
        [artifact],
        model_content_sha256="a" * 64,
        request_key="00000000-0000-4000-8000-000000001006",
    )
    digest = downloaded.artifact_set_sha256
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        "vonk_control.model_cache.shutil.disk_usage", lambda _: usage(100, 100, 0)
    )
    # No capacity is a wait, not a refusal (top-10 #8): the repair is queued
    # behind a retryable blocker and nothing is touched until the space exists.
    queued = service.start_repair(
        actor="test",
        request_key="00000000-0000-4000-8000-000000001007",
        artifact_set_sha256=digest,
    )
    assert queued.state == "queued"
    assert queued.failure is not None
    assert queued.failure["retryable"] is True
    assert queued.next_attempt_at is not None
    assert (
        service.read_verified_artifact(digest, artifact["sha256"], "weights.bin")
        == b"model"
    )
    # Still no space: the core reschedules it instead of starting the transfer.
    assert service._claim_operations(limit=1, respect_backoff=False) == []
    monkeypatch.setattr(
        "vonk_control.model_cache.shutil.disk_usage",
        lambda _: usage(10**12, 0, 10**12),
    )
    assert service._claim_operations(limit=1, respect_backoff=False) == [
        (queued.id, "repair")
    ]


def test_repair_checkpoint_requires_exact_nested_contract():
    from vonk_control.model_cache_contract import ModelCacheRepairCheckpoint

    valid = {"transfer_id": "a" * 32, "completed_objects": ["b" * 64]}
    assert (
        ModelCacheRepairCheckpoint.model_validate(valid).model_dump(mode="json")
        == valid
    )
    for invalid in (
        {"transfer_id": "a" * 32},
        dict(valid, transfer_id="../object"),
        dict(valid, completed_objects=[7]),
        dict(valid, legacy=True),
    ):
        with pytest.raises(ValidationError):
            ModelCacheRepairCheckpoint.model_validate(invalid)


def test_sampled_progress_writes_once_per_interval_and_rate_survives_restart(
    cache, tmp_path
):
    from vonk_control.model_cache.checkpoints import PROGRESS_CHECKPOINT_SECONDS
    from vonk_control.model_cache_progress import project_cache_progress

    service, sessions = cache
    clock = [datetime.now(UTC)]
    half = timedelta(seconds=PROGRESS_CHECKPOINT_SECONDS / 2)
    service._clock = lambda: clock[0]
    raw = _artifact(tmp_path, b"x" * 100)
    operation = service.start_download(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000991",
        artifacts=[raw],
    )
    with sessions() as session:
        manifest = ArtifactSetManifest.from_document(
            session.get(ModelCacheOperation, operation.id).payload["manifest"]
        )
    spec = manifest.artifacts[0]

    def checkpoint(owner, count, state="partial", force=False):
        owner._checkpoint_artifact(
            spec,
            operation_id=operation.id,
            set_digest=manifest.digest,
            actual_bytes=count,
            state=state,
            force_progress=force,
        )

    checkpoint(service, 10)
    clock[0] += half
    checkpoint(service, 20)
    assert service.get_operation(operation.id).progress["downloaded_bytes"] == 10
    clock[0] += half
    checkpoint(service, 30)
    measured = service.get_operation(operation.id).progress["measurement"]
    assert measured["bytes_per_second"] == pytest.approx(
        20 / PROGRESS_CHECKPOINT_SECONDS
    )
    assert measured["members"][0]["bytes_per_second"] == pytest.approx(
        20 / PROGRESS_CHECKPOINT_SECONDS
    )
    restarted = ModelCacheService(
        sessions,
        service.root,
        free_fraction=0,
        fixture_sources=True,
        clock=lambda: clock[0],
    )
    clock[0] += 2 * half
    checkpoint(restarted, 40)
    current = restarted.get_operation(operation.id).progress
    current_measurement = require_mapping(current["measurement"], "cache measurement")
    assert current_measurement["bytes_per_second"] == pytest.approx(
        10 / PROGRESS_CHECKPOINT_SECONDS
    )
    smoothed = current_measurement["smoothed_bytes_per_second"]
    assert isinstance(smoothed, (int, float))
    assert (
        10 / PROGRESS_CHECKPOINT_SECONDS < smoothed < 20 / PROGRESS_CHECKPOINT_SECONDS
    )
    projected = project_cache_progress(current, clock[0])
    projected_members = require_sequence(
        projected["members"], "cache measurement members"
    )
    projected_member = require_mapping(projected_members[0], "cache measurement member")
    assert projected_member["observed_at"] == clock[0].isoformat()
    clock[0] += timedelta(seconds=0.1)
    checkpoint(restarted, 100, state="verifying", force=True)
    verifying = require_mapping(
        restarted.get_operation(operation.id).progress["measurement"],
        "cache measurement",
    )
    assert verifying["completed_bytes"] == 100
    assert verifying["phase"] == ProgressPhase.VERIFYING
    assert "eta_seconds" not in verifying
    assert "bytes_per_second" not in verifying
    restarted.close()
