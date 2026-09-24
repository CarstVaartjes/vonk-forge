"""Cache recovery through process-owned locks and real PostgreSQL intent."""

from __future__ import annotations

import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import Base, ModelCacheOperation

from .test_model_cache import _artifact
from .test_model_cache_availability_regressions import _drain, _start


@pytest.mark.parametrize("queued_waiters", [1, 4])
def test_busy_artifact_releases_slot_and_recovers_without_exhausting_retries(
    postgres_engine, tmp_path, queued_waiters
):
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine)
    now = [datetime.now(UTC)]
    service = ModelCacheService(
        sessions,
        tmp_path / "cache",
        reserve_bytes=0,
        fixture_sources=True,
        max_parallel_downloads=1,
        clock=lambda: now[0],
    )
    busy = _artifact(
        tmp_path, b"busy model bytes", artifact_id="busy", model_content_sha256="a" * 64
    )
    other = _artifact(
        tmp_path,
        b"unrelated model bytes",
        artifact_id="other",
        model_content_sha256="b" * 64,
    )
    waiters = [
        _start(service, [busy], str(uuid.uuid4())) for _ in range(queued_waiters)
    ]
    waiting = waiters[0]
    lock = service.root / "locks" / str(busy["sha256"])
    program = """
import fcntl, sys
with open(sys.argv[1], "a+b") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print("locked", flush=True)
    sys.stdin.read(1)
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", program, str(lock)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None and holder.stdin is not None
    try:
        assert holder.stdout.readline().strip() == "locked"
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            service.tick()
            if all(
                service.get_operation(item.id).failure is not None for item in waiters
            ):
                break
            time.sleep(0.01)
        now[0] += timedelta(seconds=1)
        eligible = _start(service, [other], str(uuid.uuid4()))
        _drain(service, eligible.id, timeout_seconds=2)
        assert service.get_operation(eligible.id).state == "succeeded"
        for _ in range(5):
            before = now[0]
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                service.tick()
                observed = service.get_operation(waiting.id)
                retry_time = (
                    observed.failure.get("retry_time") if observed.failure else None
                )
                if (
                    observed.state == "queued"
                    and isinstance(retry_time, str)
                    and datetime.fromisoformat(retry_time) > before
                ):
                    break
                time.sleep(0.01)
            else:
                raise AssertionError(
                    f"busy artifact did not release its execution claim: {observed.state}"
                )
            assert observed.failure is not None and isinstance(retry_time, str)
            assert observed.failure["code"] == "model_cache.object_busy"
            assert str(busy["sha256"]) in str(observed.failure["detail"])
            assert observed.attempt == 1, (
                "waiting for a writer consumed an execution retry"
            )
            with sessions() as session:
                row = session.get(ModelCacheOperation, waiting.id)
                assert row is not None and "claim" not in row.payload
            now[0] = datetime.fromisoformat(retry_time)
        holder.stdin.close()
        assert holder.wait(timeout=3) == 0
        _drain(service, waiting.id, timeout_seconds=3)
        recovered = service.get_operation(waiting.id)
        assert recovered.state == "succeeded"
        assert recovered.request_key == waiting.request_key
        assert (
            service.root / "objects" / str(busy["sha256"])[:2] / str(busy["sha256"])
        ).read_bytes() == b"busy model bytes"
    finally:
        if holder.poll() is None:
            holder.stdin.close()
            holder.wait(timeout=3)
        service.close()


def test_model_worker_death_resumes_real_prefix_and_fresh_cli_reconnects(
    postgres_engine, tmp_path
):
    import json

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine)
    now = datetime.now(UTC)
    root = tmp_path / "cache"
    service = ModelCacheService(
        sessions, root, reserve_bytes=0, fixture_sources=True, clock=lambda: now
    )
    data = bytes(range(256)) * 16000
    artifact = _artifact(tmp_path, data, model_content_sha256="c" * 64)
    preview = service.download_preview(
        model_content_sha256="c" * 64, artifacts=[artifact]
    )
    key = str(uuid.uuid4())
    accepted = service.start_download(
        actor="test",
        request_key=key,
        selector="cached-model",
        plan_digest=str(preview["plan_digest"]),
        model_content_sha256="c" * 64,
        artifacts=[artifact],
    )
    token = tmp_path / "token"
    token.write_text("fixture-token")
    token.chmod(0o600)
    config = tmp_path / "process.json"
    configuration = {
        "database": postgres_engine.url.render_as_string(hide_password=False),
        "root": str(root),
        "now": now.isoformat(),
        "key": key,
        "token": str(token),
    }
    config.write_text(json.dumps(configuration))
    config.chmod(0o600)
    program = r"""
import json, os, sys
from datetime import datetime
from pathlib import Path
from io import BytesIO
from email.message import Message
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.model_cache import ModelCacheService
configuration = json.loads(Path(sys.argv[1]).read_text())
engine = create_engine(configuration["database"])
sessions = sessionmaker(engine)
service = ModelCacheService(sessions, Path(configuration["root"]), reserve_bytes=0, fixture_sources=True, clock=lambda: datetime.fromisoformat(configuration["now"]))
if sys.argv[2] == "die":
    checkpoint = service._checkpoint_artifact
    def die_after_checkpoint(*args, **kwargs):
        checkpoint(*args, **kwargs)
        if kwargs.get("state") == "partial" and kwargs.get("actual_bytes", 0) > 0:
            os._exit(23)
    service._checkpoint_artifact = die_after_checkpoint
    claimed = service._claim_operations(limit=1, respect_backoff=True)
    assert len(claimed) == 1
    service._run_download(claimed[0][0], force=False, interrupt_after_bytes=1048576)
    raise AssertionError("worker did not reach its partial checkpoint")
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from vonk_control.auth import Actor
from vonk_control.model_cache_api import install_model_operator_routes
from cluster_profiles.cli import main
from cluster_profiles.control_client import ControlClient
opened = service._open_source
offsets = []
def source(spec, offset):
    offsets.append(offset)
    return opened(spec, offset)
service._open_source = source
assert service.run_pending() == 1
app = FastAPI()
install_model_operator_routes(app, actor_dependency=Depends(lambda: Actor("test", "operator")), service=service, audits=None)
class Response(BytesIO):
    def __init__(self, response):
        super().__init__(response.content)
        self.status = response.status_code
        self.headers = Message()
        for name, value in response.headers.items():
            self.headers[name] = value
    def __exit__(self, *args):
        self.close()
with TestClient(app) as api:
    def opener(request, timeout):
        assert request.get_method() == "GET", "reconnection must not submit fresh work"
        return Response(api.get(request.selector))
    client = ControlClient("https://forge.example.test", Path(configuration["token"]), opener=opener)
    result = main(("model", "progress", "--request-key", configuration["key"], "--json"), control_client=client)
assert result == 0
Path(configuration["root"]).joinpath("observed-offsets.json").write_text(json.dumps(offsets))
service.close()
engine.dispose()
"""
    try:
        crashed = subprocess.run(
            [sys.executable, "-c", program, str(config), "die"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert crashed.returncode == 23, crashed.stderr
        assert accepted.artifact_set_sha256 is not None
        part = (
            root
            / "partials"
            / accepted.artifact_set_sha256
            / f"{artifact['sha256']}.part"
        )
        prefix = part.stat().st_size
        assert 0 < prefix < len(data) and part.read_bytes() == data[:prefix]
        with sessions() as session:
            operation = session.get(ModelCacheOperation, accepted.id)
            assert operation is not None
            assert operation.state == "running", (
                "the process unexpectedly cleaned up its execution claim"
            )
        configuration["now"] = (now + timedelta(seconds=121)).isoformat()
        config.write_text(json.dumps(configuration))
        restarted = subprocess.run(
            [sys.executable, "-c", program, str(config), "resume"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert restarted.returncode == 0, restarted.stderr
        receipt = json.loads(restarted.stdout)
        assert receipt["operation_id"] == accepted.id and receipt["request_key"] == key
        assert receipt["state"] == "succeeded"
        assert json.loads((root / "observed-offsets.json").read_text()) == [prefix]
        assert (
            root / "objects" / str(artifact["sha256"])[:2] / str(artifact["sha256"])
        ).read_bytes() == data
    finally:
        service.close()


def test_image_contention_resumes_after_release_without_exhausting_transfer_budget(
    postgres_engine, tmp_path
):
    from vonk_control.recipe_image_availability import RecipeImageAvailabilityService
    from vonk_control.runtime_image_preparation import (
        FilesystemRuntimeImageStorage,
        _claim_registry_layer_lock,
    )

    from .test_os_lock_contention import _hold
    from .test_recipe_image_availability import (
        Transport,
        _add_head,
        _add_revision,
        _recipe,
        _runtime,
    )

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine)
    recipe = _recipe("recipe-image.json")
    revision_id = str(uuid.uuid4())
    with sessions.begin() as session:
        revision = _add_revision(session, revision_id, recipe)
        revision.document_id = str(uuid.uuid4())
        _add_head(session, revision)
    lock_path = tmp_path / "index.lock"
    now = [datetime.now(UTC)]

    class LockedTransport(Transport):
        def pull_and_export(self, reference, destination, **kwargs):
            with lock_path.open("a+b") as lock:
                _claim_registry_layer_lock(lock, reference=reference)
                return super().pull_and_export(reference, destination, **kwargs)

    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path / "images"),
        authority=lambda *args, **kwargs: (recipe, _runtime()),
        transport=LockedTransport(),
        clock=lambda: now[0],
        automatic_attempt_limit=2,
    )
    accepted = service.start(revision_id, actor="test", request_id=str(uuid.uuid4()))
    holder = _hold(lock_path)
    try:
        for _ in range(5):
            assert service.run_pending() == 1
            waiting = service.get(accepted.id)
            assert waiting.state == "queued" and waiting.failure is not None
            assert waiting.failure["code"] == "runtime_image.transfer_contended"
            assert waiting.failure["retryable"] is True
            retry_time = waiting.failure["retry_time"]
            assert isinstance(retry_time, str)
            now[0] = datetime.fromisoformat(retry_time)
    finally:
        holder.terminate()
        holder.wait(timeout=3)
    assert service.run_pending() == 1
    assert service.get(accepted.id).state == "succeeded"
