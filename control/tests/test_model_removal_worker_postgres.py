"""Model-removal owner contention and restart behavior through PostgreSQL."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.model_cache import CacheOperationView, ModelCacheService
from vonk_control.model_cache_contract import (
    ModelCacheRemovalPayload,
    ModelCacheRemovalResult,
    parse_model_cache_payload,
)
from vonk_control.models import ModelCacheOperation
from vonk_forge_contracts import content_sha256

from .test_model_cache import _artifact, _download, _remove_model
from .test_model_removal_reference_lifecycle import (
    _one_model,
    _register_model,
    _removal_service,
    _settle_removal,
)


def _seed_removal(
    service: ModelCacheService,
    sessions: sessionmaker[Session],
    tmp_path: Path,
    *,
    slug: str,
    data: bytes,
) -> tuple[CacheOperationView, str, Path]:
    selector, model_digest, object_path = _prepare_removal_target(
        service,
        sessions,
        tmp_path,
        slug=slug,
        data=data,
    )
    accepted = _accept_removal(service, selector, model_digest)
    return accepted, model_digest, object_path


def _prepare_removal_target(
    service: ModelCacheService,
    sessions: sessionmaker[Session],
    tmp_path: Path,
    *,
    slug: str,
    data: bytes,
) -> tuple[str, str, Path]:
    model = _one_model(tmp_path, slug, data)
    selector = _register_model(sessions, model)
    model_digest = content_sha256(model)
    source_root = tmp_path / f"source-{slug}"
    source_root.mkdir()
    artifact = _artifact(
        source_root,
        data,
        model_content_sha256=model_digest,
    )
    prepared = _download(
        service,
        [artifact],
        model_content_sha256=model_digest,
        request_key=str(uuid4()),
    )
    assert prepared.state == "succeeded", prepared.last_error
    object_digest = hashlib.sha256(data).hexdigest()
    object_path = service._object_path(object_digest)
    assert object_path.read_bytes() == data
    return selector, model_digest, object_path


def _accept_removal(
    service: ModelCacheService, selector: str, model_digest: str
) -> CacheOperationView:
    accepted = _remove_model(
        service,
        selector,
        actor="operator",
        request_key=str(uuid4()),
        model_content_sha256=model_digest,
    )
    assert accepted.state == "queued"
    return accepted


def _removal_payload(
    sessions: sessionmaker[Session], operation_id: str
) -> ModelCacheRemovalPayload:
    with sessions() as session:
        operation = session.get(ModelCacheOperation, operation_id)
        assert operation is not None and operation.kind == "remove"
        payload = parse_model_cache_payload("remove", operation.payload)
    assert isinstance(payload, ModelCacheRemovalPayload)
    return payload


def test_locked_removal_row_is_bounded_and_same_owner_retries_after_release(
    postgres_engine, tmp_path: Path
) -> None:
    service, sessions = _removal_service(postgres_engine, tmp_path)
    accepted, _model_digest, object_path = _seed_removal(
        service,
        sessions,
        tmp_path,
        slug="locked-row-retry",
        data=b"locked row model bytes",
    )
    initial = _removal_payload(sessions, accepted.id)
    key_acquired = threading.Event()
    result_observed = threading.Event()
    release_holder = threading.Event()
    step_errors: list[Exception] = []
    step_results: list[bool] = []
    original_advance = service._advance_model_removal

    def contend_after_candidate_select(
        operation_id: str, *, now: datetime | None = None
    ) -> bool:
        if operation_id != accepted.id:
            return original_advance(operation_id, now=now)
        with sessions() as holder:
            holder.begin()
            locked = holder.scalar(
                select(ModelCacheOperation)
                .where(ModelCacheOperation.id == operation_id)
                .with_for_update()
            )
            assert locked is not None
            key_acquired.set()
            try:
                try:
                    outcome = original_advance(operation_id, now=now)
                except Exception as error:
                    step_errors.append(error)
                    result_observed.set()
                    assert release_holder.wait(timeout=5)
                    raise
                step_results.append(outcome)
                result_observed.set()
                assert release_holder.wait(timeout=5)
                return outcome
            finally:
                holder.rollback()

    service._advance_model_removal = contend_after_candidate_select
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(service.advance_removals, limit=1)
    try:
        assert key_acquired.wait(timeout=5), "worker did not reach the removal owner"
        prompt = result_observed.wait(timeout=2)
        release_holder.set()
        completed_count = future.result(timeout=5)
    finally:
        release_holder.set()
        executor.shutdown(wait=True)

    assert prompt, "NOWAIT removal observation waited on the operation owner"
    assert completed_count == 0
    assert step_errors == []
    assert step_results == [False]
    assert object_path.is_file(), "the locked owner must retain its managed bytes"
    after_busy = _removal_payload(sessions, accepted.id)
    assert after_busy.removal_fence == initial.removal_fence
    assert after_busy.object_index == initial.object_index == 0
    assert after_busy.reclaimed_bytes == 0

    service._advance_model_removal = original_advance
    settled = _settle_removal(service, accepted)
    assert settled.id == accepted.id
    assert settled.request_key == accepted.request_key
    assert settled.state == "succeeded"
    assert not object_path.exists()
    payload = _removal_payload(sessions, accepted.id)
    assert payload.removal_fence == initial.removal_fence
    assert isinstance(payload.result, ModelCacheRemovalResult)
    assert payload.result.reclaimed_bytes == len(b"locked row model bytes")
    service.close()


def test_held_and_not_due_first_removals_do_not_starve_unrelated_due_owner(
    postgres_engine, tmp_path: Path
) -> None:
    service, sessions = _removal_service(postgres_engine, tmp_path)
    held_selector, held_digest, held_path = _prepare_removal_target(
        service,
        sessions,
        tmp_path,
        slug="held-first-owner",
        data=b"held owner bytes",
    )
    waiting_selector, waiting_digest, waiting_path = _prepare_removal_target(
        service,
        sessions,
        tmp_path,
        slug="not-due-middle-owner",
        data=b"not due owner bytes",
    )
    due_selector, due_digest, due_path = _prepare_removal_target(
        service,
        sessions,
        tmp_path,
        slug="due-unrelated-owner",
        data=b"unrelated due owner bytes",
    )
    held = _accept_removal(service, held_selector, held_digest)
    waiting = _accept_removal(service, waiting_selector, waiting_digest)
    due = _accept_removal(service, due_selector, due_digest)
    delayed_until = datetime.now(UTC) + timedelta(hours=1)
    with sessions.begin() as session:
        waiting_row = session.get(ModelCacheOperation, waiting.id)
        assert waiting_row is not None
        waiting_payload = parse_model_cache_payload("remove", waiting_row.payload)
        assert isinstance(waiting_payload, ModelCacheRemovalPayload)
        waiting_document = waiting_payload.model_dump(mode="json")
        waiting_document["retry"]["next_retry_at"] = delayed_until.isoformat()
        waiting_document["retry"]["retry_after_seconds"] = 3600
        waiting_row.payload = waiting_document

    held_before = _removal_payload(sessions, held.id)
    waiting_before = _removal_payload(sessions, waiting.id)
    with sessions() as holder:
        holder.begin()
        locked = holder.scalar(
            select(ModelCacheOperation)
            .where(ModelCacheOperation.id == held.id)
            .with_for_update()
        )
        assert locked is not None
        with sessions() as contender, pytest.raises(DBAPIError):
            contender.scalar(
                select(ModelCacheOperation)
                .where(ModelCacheOperation.id == held.id)
                .with_for_update(nowait=True)
            )
        assert held_path.is_file(), "the held object's bytes were not present initially"
        try:
            observed_held_states: list[str] = []
            for _ in range(8):
                service.advance_removals(limit=1)
                observed_held_states.append(service.get_operation(held.id).state)
                if service.get_operation(due.id).state == "succeeded":
                    break
            assert service.get_operation(due.id).state == "succeeded"
            assert due_path.is_file() is False
            assert held_path.is_file()
            assert set(observed_held_states) == {"queued"}
            assert waiting_path.is_file()
            held_after = _removal_payload(sessions, held.id)
            waiting_after = _removal_payload(sessions, waiting.id)
            assert held_after.removal_fence == held_before.removal_fence
            assert held_after.object_index == held_before.object_index == 0
            assert waiting_after.removal_fence == waiting_before.removal_fence
            assert waiting_after.retry.next_retry_at == delayed_until.isoformat()
        finally:
            holder.rollback()

    held_settled = _settle_removal(service, held)
    assert held_settled.state == "succeeded"
    assert not held_path.exists()
    assert service.get_operation(due.id).state == "succeeded"
    service.close()


def test_model_removal_process_death_after_unlink_reuses_pending_byte_checkpoint(
    postgres_engine, tmp_path: Path
) -> None:
    service, sessions = _removal_service(postgres_engine, tmp_path)
    data = bytes(range(128)) * 64
    accepted, _model_digest, object_path = _seed_removal(
        service,
        sessions,
        tmp_path,
        slug="removal-process-death",
        data=data,
    )
    initial = _removal_payload(sessions, accepted.id)
    assert len(initial.delete_objects) == 1
    receipt_path = service._receipt_path(initial.delete_objects[0])
    database_url = postgres_engine.url.render_as_string(hide_password=False)
    config = tmp_path / "removal-process.json"
    config.write_text(
        json.dumps(
            {
                "database": database_url,
                "root": str(service.root),
                "operation_id": accepted.id,
            }
        ),
        encoding="utf-8",
    )
    config.chmod(0o600)
    program = r"""
import json, os, sys
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.model_cache import ModelCacheService
config = json.loads(Path(sys.argv[1]).read_text())
engine = create_engine(config["database"])
service = ModelCacheService(
    sessionmaker(engine, expire_on_commit=False),
    Path(config["root"]),
    reserve_bytes=0,
)
persist = service._persist_model_removal_checkpoint
def exit_before_completion(*args, **kwargs):
    if kwargs.get("object_step") is True and kwargs.get("complete_step") is True:
        os._exit(47)
    return persist(*args, **kwargs)
service._persist_model_removal_checkpoint = exit_before_completion
service.advance_removals(limit=1)
raise AssertionError("removal worker did not reach the post-unlink checkpoint")
"""

    try:
        service.close()
        crashed = subprocess.run(
            [sys.executable, "-c", program, str(config)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert crashed.returncode == 47, crashed.stderr
        assert not object_path.exists()
        assert not receipt_path.exists()
        with sessions() as session:
            row = session.get(ModelCacheOperation, accepted.id)
            assert row is not None and row.state == "running"
            assert row.request_key == accepted.request_key
            crashed_payload = parse_model_cache_payload("remove", row.payload)
        assert isinstance(crashed_payload, ModelCacheRemovalPayload)
        assert crashed_payload.removal_fence == initial.removal_fence
        assert crashed_payload.object_index == 0
        assert crashed_payload.object_pending_bytes == len(data)
        assert crashed_payload.reclaimed_bytes == 0

        restarted = ModelCacheService(
            sessions,
            service.root,
            reserve_bytes=0,
        )
        settled = _settle_removal(restarted, accepted)
        assert settled.id == accepted.id
        assert settled.request_key == accepted.request_key
        assert settled.state == "succeeded"
        payload = _removal_payload(sessions, accepted.id)
        assert payload.removal_fence == initial.removal_fence
        assert payload.object_index == len(payload.delete_objects) == 1
        assert payload.object_pending_bytes is None
        assert isinstance(payload.result, ModelCacheRemovalResult)
        assert payload.result.reclaimed_bytes == len(data)
        assert restarted.advance_removals(limit=1) == 0
        with sessions() as session:
            count = session.scalar(
                select(func.count())
                .select_from(ModelCacheOperation)
                .where(ModelCacheOperation.request_key == accepted.request_key)
            )
        assert count == 1
        assert not object_path.exists()
        restarted.close()
    finally:
        service.close()
