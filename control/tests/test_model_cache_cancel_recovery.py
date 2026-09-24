"""PostgreSQL/process/storage coverage for operator model cancellation."""

from __future__ import annotations

import hashlib
import multiprocessing
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import Actor
from vonk_control.model_cache import ModelCacheConflict, ModelCacheService
from vonk_control.model_cache_api import install_model_operator_routes
from vonk_control.models import Base

OPERATION_REQUEST = "00000000-0000-4000-8000-000000000041"
SHARED_REQUEST = "00000000-0000-4000-8000-000000000042"
LATER_REQUEST = "00000000-0000-4000-8000-000000000043"
CANCEL_REQUEST = "00000000-0000-4000-8000-000000000044"
PRESERVE_REQUEST = "00000000-0000-4000-8000-000000000045"
PRESERVE_CANCEL = "00000000-0000-4000-8000-000000000046"
CANCEL_REASON = "operator selected a different model"


def _artifact(source: Path, content: bytes) -> dict[str, Any]:
    source.write_bytes(content)
    return {
        "id": "weights",
        "path": "weights.bin",
        "kind": "file",
        "source": source.as_uri(),
        "sha256": hashlib.sha256(content).hexdigest(),
        "download_bytes": len(content),
        "roles": ["weights"],
        "model_content_sha256": "a" * 64,
    }


def _hold_at_verification(
    dsn: str,
    cache_root: str,
    operation_id: str,
    reached: Any,
    release: Any,
    errors: Any,
) -> None:
    """Hold the real artifact flock after transfer, before publication."""

    try:
        engine = create_engine(dsn, pool_pre_ping=True)
        sessions = sessionmaker(engine, expire_on_commit=False)
        service = ModelCacheService(
            sessions, Path(cache_root), reserve_bytes=0, fixture_sources=True
        )
        verify = service._verify_file

        def pause_after_verification(path, spec):
            valid = verify(path, spec)
            if path.suffix == ".part" and valid:
                reached.set()
                if not release.wait(30):
                    raise TimeoutError("verification barrier was not released")
            return valid

        service._verify_file = pause_after_verification
        service.run_pending()
        service.close()
        engine.dispose()
    except Exception as error:  # noqa: BLE001 - forward child failure to the parent
        errors.put(f"{type(error).__name__}: {error}")


def _api(service: ModelCacheService) -> TestClient:
    app = FastAPI()
    install_model_operator_routes(
        app,
        actor_dependency=Depends(lambda: Actor("operator", "operator")),
        service=service,
        audits=None,
    )
    return TestClient(app)


def test_cancel_fences_process_publication_and_keeps_shared_work_resumable(
    tmp_path: Path, postgres_engine
) -> None:
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    cache_root = tmp_path / "nas-cache"
    content = b"model bytes retained at the verification boundary" * 64
    artifact = _artifact(tmp_path / "weights.source", content)

    service = ModelCacheService(
        sessions, cache_root, reserve_bytes=0, fixture_sources=True
    )
    preview = service.download_preview(
        model_content_sha256="a" * 64, artifacts=[artifact]
    )
    operation = service.start_download(
        actor="operator",
        request_key=OPERATION_REQUEST,
        selector="qwen",
        plan_digest=str(preview["plan_digest"]),
        model_content_sha256="a" * 64,
        artifacts=[artifact],
    )
    set_digest = operation.artifact_set_sha256
    assert set_digest is not None

    context = multiprocessing.get_context("spawn")
    reached = context.Event()
    release = context.Event()
    errors = context.Queue()
    worker = context.Process(
        target=_hold_at_verification,
        args=(
            postgres_engine.url.render_as_string(hide_password=False),
            str(cache_root),
            operation.id,
            reached,
            release,
            errors,
        ),
    )
    shared = ModelCacheService(
        sessions, cache_root, reserve_bytes=0, fixture_sources=True
    )
    restarted = ModelCacheService(
        sessions, cache_root, reserve_bytes=0, fixture_sources=True
    )
    try:
        worker.start()
        assert reached.wait(25), (
            worker.exitcode,
            errors.get() if not errors.empty() else None,
        )
        response = _api(service).post(
            f"/api/model/operations/{operation.id}/cancel",
            json={
                "schema_version": 2,
                "request_key": CANCEL_REQUEST,
                "reason": CANCEL_REASON,
            },
        )
        assert response.status_code == 202, response.text
        receipt = response.json()
        assert receipt["state"] == "cancelling"
        assert receipt["cancellation"]["request_key"] == CANCEL_REQUEST
        assert receipt["cancellation"]["reason"] == CANCEL_REASON

        shared_preview = shared.download_preview(
            model_content_sha256="a" * 64, artifacts=[artifact]
        )
        shared_operation = shared.start_download(
            actor="operator",
            request_key=SHARED_REQUEST,
            selector="qwen",
            plan_digest=str(shared_preview["plan_digest"]),
            model_content_sha256="a" * 64,
            artifacts=[artifact],
        )
        shared.run_pending()
        assert shared.get_operation(shared_operation.id).state == "queued"
        assert service.get_operation(operation.id).state == "cancelling"

        # Process death releases the OS flock. The new Controller instance
        # settles the persisted request before claiming any further downloads.
        worker.terminate()
        worker.join(timeout=10)
        assert worker.exitcode is not None
        part = service._partial_path(set_digest, str(artifact["sha256"]))
        assert part.read_bytes() == content
        restarted._reconcile_pending_cancellations()
        settled = restarted.get_operation(operation.id)
        assert settled.state == "cancelled"
        assert settled.progress["phase"] == "completed"
        assert not restarted._object_path(str(artifact["sha256"])).exists()
        restarted._set_operation_state(operation.id, "succeeded")
        assert restarted.get_operation(operation.id).state == "cancelled"

        restarted.run_pending(limit=2)
        assert restarted.get_operation(shared_operation.id).state == "succeeded"
        assert restarted._object_path(str(artifact["sha256"])).read_bytes() == content

        repeated = restarted.cancel_operation(
            operation.id,
            actor="operator",
            request_key=CANCEL_REQUEST,
            reason=CANCEL_REASON,
        )
        assert repeated.state == "cancelled"
        try:
            restarted.cancel_operation(
                operation.id,
                actor="operator",
                request_key=LATER_REQUEST,
                reason=CANCEL_REASON,
            )
        except ModelCacheConflict as error:
            assert error.code == "model_cache.cancellation_key_reused"
        else:
            raise AssertionError("a second cancellation identity replaced the first")

        preserve_preview = restarted.download_preview(
            model_content_sha256="a" * 64, artifacts=[artifact]
        )
        verified_operation = restarted.start_download(
            actor="operator",
            request_key=PRESERVE_REQUEST,
            selector="qwen",
            plan_digest=str(preserve_preview["plan_digest"]),
            model_content_sha256="a" * 64,
            artifacts=[artifact],
        )
        verified_cancel = restarted.cancel_operation(
            verified_operation.id,
            actor="operator",
            request_key=PRESERVE_CANCEL,
            reason="preserve the completed object",
        )
        assert verified_cancel.state == "cancelled"
        assert restarted._object_path(str(artifact["sha256"])).read_bytes() == content

        # A new explicit download can bind a new request identity and reuse
        # the verified object without consulting its now-removed source file.
        Path(unquote(urlsplit(str(artifact["source"])).path)).unlink()
        later_preview = restarted.download_preview(
            model_content_sha256="a" * 64, artifacts=[artifact]
        )
        later = restarted.start_download(
            actor="operator",
            request_key=LATER_REQUEST,
            selector="qwen",
            plan_digest=str(later_preview["plan_digest"]),
            model_content_sha256="a" * 64,
            artifacts=[artifact],
        )
        restarted.run_pending()
        assert restarted.get_operation(later.id).state == "succeeded"
        assert restarted._object_path(str(artifact["sha256"])).read_bytes() == content
    finally:
        # This process is intentionally terminated while blocked in Event.wait
        # so it cannot run cleanup or publish after the restart boundary.
        if worker.is_alive():
            worker.terminate()
        worker.join(timeout=10)
        shared.close()
        restarted.close()
        service.close()
