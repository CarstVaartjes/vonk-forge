"""Installed model cancellation recovers through its durable Controller owner."""

from __future__ import annotations

import json
import multiprocessing
import subprocess
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import Base

from .test_model_cache_cancel_recovery import _api, _artifact, _hold_at_verification
from .test_profile_load_installed_cli import (
    _https_api_peer,
    _process_environment,
)

pytest_plugins = ("tests.test_profile_load_installed_cli",)

CANCEL_REASON = "operator selected a different model"


def test_installed_model_cancel_recovers_lost_receipt_and_observes_settlement(
    installed_vonkctl: Path,
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    cache_root = tmp_path / "managed-model-cache"
    content = b"verified model bytes kept at the cancel boundary" * 128
    artifact = _artifact(tmp_path / "weights.source", content)
    operation_key = "00000000-0000-4000-8000-000000000151"
    cancellation_key = "00000000-0000-4000-8000-000000000152"
    service = ModelCacheService(
        sessions, cache_root, reserve_bytes=0, fixture_sources=True
    )
    preview = service.download_preview(
        model_content_sha256="a" * 64, artifacts=[artifact]
    )
    operation = service.start_download(
        actor="operator",
        request_key=operation_key,
        selector="qwen",
        plan_digest=str(preview["plan_digest"]),
        model_content_sha256="a" * 64,
        artifacts=[artifact],
    )
    artifact_set = operation.artifact_set_sha256
    assert artifact_set is not None

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
    worker_started = False
    restarted: ModelCacheService | None = None
    try:
        worker.start()
        worker_started = True
        if not reached.wait(25):
            detail = errors.get(timeout=1) if not errors.empty() else worker.exitcode
            pytest.fail(f"worker did not reach verified staging: {detail}")

        headers = {"Authorization": "Bearer installed-model-cancel-test-token"}
        peer_root = tmp_path / "cancel-peer"
        peer_root.mkdir()
        with (
            _api(service) as api,
            _https_api_peer(peer_root, api, headers) as (
                url,
                certificate,
                peer,
            ),
        ):
            cancel_path = f"/api/model/operations/{operation.id}/cancel"
            lookup_path = f"/api/model/operations/{operation.id}"
            peer.drop_responses.add(("POST", cancel_path))
            environment = _process_environment(peer_root, url, certificate, headers)
            cancelled = subprocess.run(
                [
                    str(installed_vonkctl),
                    "--json",
                    "model",
                    "cancel",
                    operation.id,
                    "--yes",
                    "--request-key",
                    cancellation_key,
                    "--reason",
                    CANCEL_REASON,
                    "--detach",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                env=environment,
                cwd=peer_root,
            )
            assert cancelled.returncode == 0, cancelled.stderr
            recovered_receipt = json.loads(cancelled.stdout)
            assert recovered_receipt["operation_id"] == operation.id
            assert recovered_receipt["state"] == "cancelling"
            assert recovered_receipt["cancellation"]["request_key"] == (
                cancellation_key
            )
            assert recovered_receipt["cancellation"]["reason"] == CANCEL_REASON
            assert peer.dropped_responses == [("POST", cancel_path)]
            assert [(method, path) for method, path, _ in peer.calls] == [
                ("POST", cancel_path),
                ("GET", lookup_path),
            ]
            body = peer.calls[0][2]
            assert isinstance(body, dict)
            assert body["request_key"] == cancellation_key
            assert body["reason"] == CANCEL_REASON
            assert "installed-model-cancel-test-token" not in cancelled.stdout
            assert "installed-model-cancel-test-token" not in cancelled.stderr

        worker.terminate()
        worker.join(timeout=10)
        assert worker.exitcode is not None
        partial = service._partial_path(artifact_set, str(artifact["sha256"]))
        assert partial.read_bytes() == content
        final_object = service._object_path(str(artifact["sha256"]))
        assert not final_object.exists()

        # Reconstruct the storage owner after process death and settle only the
        # durable cancellation. The staged bytes remain resumable, not published.
        restarted = ModelCacheService(
            sessions, cache_root, reserve_bytes=0, fixture_sources=True
        )
        restarted._reconcile_pending_cancellations()
        settled = restarted.get_operation(operation.id)
        assert settled.state == "cancelled"
        assert settled.progress["phase"] == "completed"
        assert partial.read_bytes() == content
        assert not final_object.exists()

        # A new installed process reconnects by the original download key and
        # receives the settled operation plus its exact durable cancel intent.
        resume_root = tmp_path / "progress-peer"
        resume_root.mkdir()
        with (
            _api(restarted) as api,
            _https_api_peer(resume_root, api, headers) as (
                url,
                certificate,
                peer,
            ),
        ):
            environment = _process_environment(resume_root, url, certificate, headers)
            progress = subprocess.run(
                [
                    str(installed_vonkctl),
                    "--json",
                    "model",
                    "progress",
                    "--request-key",
                    operation_key,
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
                env=environment,
                cwd=resume_root,
            )
            assert progress.returncode == 0, progress.stderr
            observed = json.loads(progress.stdout)
            assert observed["operation_id"] == operation.id
            assert observed["state"] == "cancelled"
            assert observed["cancellation"]["request_key"] == cancellation_key
            assert observed["cancellation"]["reason"] == CANCEL_REASON
            assert peer.calls == [("GET", f"/api/model/requests/{operation_key}", None)]
            assert "installed-model-cancel-test-token" not in progress.stdout
            assert "installed-model-cancel-test-token" not in progress.stderr
    finally:
        if worker_started:
            if worker.is_alive():
                worker.terminate()
            worker.join(timeout=10)
        if restarted is not None:
            restarted.close()
        service.close()
