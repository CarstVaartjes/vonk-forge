"""Gated access waits recover without a second operator action or another token."""

from __future__ import annotations

import hashlib
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import LifecycleState
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import Base, ModelCacheOperation
from vonk_control.operation_contract import (
    AvailabilityOperationFailure,
    AvailabilityRecoveryAction,
    ModelAccessReason,
)
from vonk_control.strict_json import read_stored_model

from .test_model_cache import _http_artifact
from .test_model_cache_cancel_recovery import _api

URL = "https://huggingface.co/facebook/dinov3-vitl16-pretrain-lvd1689m"
SOURCE = URL + "/resolve/" + "a" * 40 + "/weights.bin"
DATA = b"verified gated model"


def _drain(service: ModelCacheService, operation_id: str) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        service.tick()
        if not service._background_operations:
            return
        time.sleep(0.01)
    pytest.fail(f"transfer {operation_id} did not settle")


@pytest.mark.parametrize(
    "resume", ["approval", "token", "missing-token", "restart", "cancel", "newer"]
)
def test_gated_model_recovers_through_worker_without_manual_resume(
    postgres_engine: Engine, tmp_path: Path, resume: str
) -> None:
    """Catches terminal auth failures, stat-only watching, lost clocks and revived intent."""
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    now = [datetime(2026, 10, 19, tzinfo=UTC)]
    allowed = False
    requests: list[httpx2.Request] = []
    token_path = tmp_path / "hf-token"
    if resume != "missing-token":
        token_path.write_text("hf_old_token\n")

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert request.url.host == "huggingface.co"
        assert request.headers.get("authorization") in {
            None,
            "Bearer hf_old_token",
            "Bearer hf_new_token",
        }
        return httpx2.Response(200, content=DATA) if allowed else httpx2.Response(403)

    client = httpx2.Client(transport=httpx2.MockTransport(handler))

    def service() -> ModelCacheService:
        return ModelCacheService(
            sessions,
            tmp_path / "cache",
            reserve_bytes=0,
            fixture_sources=True,
            http_client=client,
            huggingface_token_path=token_path,
            clock=lambda: now[0],
        )

    cache = service()
    artifact = _http_artifact(DATA)
    artifact["source"] = SOURCE
    preview = cache.download_preview(
        model_content_sha256="b" * 64, artifacts=[artifact]
    )
    operation = cache.start_download(
        actor="operator",
        request_key=str(uuid4()),
        selector="dinov3",
        plan_digest=str(preview["plan_digest"]),
        model_content_sha256="b" * 64,
        artifacts=[artifact],
    )
    try:
        cache.tick()
        _drain(cache, operation.id)
        waiting = cache.get_operation(operation.id)
        assert waiting.state in {LifecycleState.QUEUED, LifecycleState.BACKOFF}
        assert waiting.failure is not None
        failure = read_stored_model(AvailabilityOperationFailure, waiting.failure)
        assert failure.code == (
            ModelAccessReason.ACCESS_REQUIRED
            if resume == "missing-token"
            else ModelAccessReason.ACCESS_DENIED
        )
        assert (
            AvailabilityRecoveryAction.CHECK_ACCESS_AND_RESUME
            in failure.recovery_actions
        )
        assert waiting.blockers[0].code == failure.code
        assert waiting.next_attempt_at is not None
        due = datetime.fromisoformat(waiting.next_attempt_at)
        assert 300 <= (due - now[0]).total_seconds() <= 360
        assert waiting.failure["retry_time"] == waiting.next_attempt_at
        assert not cache._object_path(hashlib.sha256(DATA).hexdigest()).exists()
        for _ in range(3):
            cache.tick()
        assert len(requests) == 1
        if resume == "restart":
            cache.close()
            cache = service()
            cache.tick()
            assert (
                cache.get_operation(operation.id).next_attempt_at
                == waiting.next_attempt_at
            )
            assert len(requests) == 1
            now[0] = due + timedelta(days=2)
            cache.tick()
            _drain(cache, operation.id)
            waiting = cache.get_operation(operation.id)
            assert waiting.next_attempt_at is not None
            due = datetime.fromisoformat(waiting.next_attempt_at)
        if resume in {"approval", "restart"}:
            # Several still-denied probes exercise growing backoff and its ceiling.
            for _ in range(6):
                now[0] = due
                cache.tick()
                _drain(cache, operation.id)
                waiting = cache.get_operation(operation.id)
                assert waiting.next_attempt_at is not None
                next_due = datetime.fromisoformat(waiting.next_attempt_at)
                assert 300 <= (next_due - now[0]).total_seconds() <= 3600
                due = next_due
        allowed = True
        if resume in {"token", "missing-token", "cancel", "newer"}:
            if resume == "cancel":
                cache.cancel_operation(
                    operation.id,
                    actor="operator",
                    request_key=str(uuid4()),
                    reason="superseded",
                )
            elif resume == "newer":
                now[0] += timedelta(seconds=1)
                cache.start_download(
                    actor="operator",
                    request_key=str(uuid4()),
                    selector="dinov3",
                    plan_digest=str(preview["plan_digest"]),
                    model_content_sha256="b" * 64,
                    artifacts=[artifact],
                )
            before = token_path.stat() if token_path.exists() else None
            token_path.write_text("hf_new_token\n")
            if before is not None:
                os.utime(token_path, ns=(before.st_atime_ns, before.st_mtime_ns))
                assert token_path.stat().st_size == before.st_size
            if resume in {"token", "missing-token"}:
                assert cache._resume_after_credential_change() == 1
                queued = cache.get_operation(operation.id)
                assert queued.failure is not None
                assert queued.failure["retry_time"] == now[0].isoformat()
                assert queued.failure["retry_after_seconds"] == 0
        else:
            now[0] = due - timedelta(microseconds=1)
            cache.tick()
            count = len(requests)
            now[0] = due
        cache.tick()
        _drain(cache, operation.id)
        final = cache.get_operation(operation.id)
        if resume == "cancel":
            assert final.state == LifecycleState.CANCELLED
            assert len(requests) == 1
        elif resume == "newer":
            now[0] = due
            cache.tick()
            _drain(cache, operation.id)
            assert cache.get_operation(operation.id).state == LifecycleState.CANCELLED
        else:
            assert final.state == LifecycleState.SUCCEEDED
            assert final.id == operation.id
            assert final.plan_digest == operation.plan_digest
            assert final.artifact_set_sha256 == operation.artifact_set_sha256
            assert (
                cache._object_path(hashlib.sha256(DATA).hexdigest()).read_bytes()
                == DATA
            )
            assert final.failure is None and not final.blockers
            if resume in {"approval", "restart"}:
                assert len(requests) == count + 1
    finally:
        cache.close()
        client.close()


def test_manual_access_api_reconciles_replays_and_retains_exact_transfer(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    """Catches hidden routes, duplicate requests and automatic predecessors left live."""
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    allowed = False
    requests = []

    def handler(request):
        requests.append(request)
        return httpx2.Response(200, content=DATA) if allowed else httpx2.Response(403)

    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    cache = ModelCacheService(
        sessions,
        tmp_path / "cache",
        reserve_bytes=0,
        fixture_sources=True,
        http_client=client,
    )
    artifact = _http_artifact(DATA)
    artifact["source"] = SOURCE
    preview = cache.download_preview(
        model_content_sha256="b" * 64, artifacts=[artifact]
    )
    operation = cache.start_download(
        actor="operator",
        request_key=str(uuid4()),
        selector="dinov3",
        plan_digest=str(preview["plan_digest"]),
        model_content_sha256="b" * 64,
        artifacts=[artifact],
    )
    try:
        cache.run_pending()
        with _api(cache) as api:
            target_id = operation.id
            for granted in (False, True):
                path = f"/api/model/operations/{target_id}/resume-access"
                allowed = granted
                key = str(uuid4())
                response = api.post(path, json={"request_key": key})
                assert response.status_code == 202, response.text
                receipt = response.json()
                assert receipt["operation_id"] != target_id
                assert receipt["request_key"] == key
                assert cache.get_operation(target_id).state == LifecycleState.CANCELLED
                count = len(requests)
                assert api.post(path, json={"request_key": key}).json() == receipt
                lookup = api.get(f"/api/model/requests/{key}")
                assert lookup.status_code == 200
                assert lookup.json() == receipt
                assert len(requests) == count
                cache.run_pending()
                target_id = receipt["operation_id"]
                observed = cache.get_operation(target_id)
                assert observed.artifact_set_sha256 == operation.artifact_set_sha256
                assert observed.plan_digest == operation.plan_digest
                assert (observed.state == LifecycleState.SUCCEEDED) == granted
        cache.run_pending()
        assert cache.get_operation(target_id).state == LifecycleState.SUCCEEDED
        with sessions() as session:
            assert session.query(ModelCacheOperation).count() == 3
    finally:
        cache.close()
        client.close()
