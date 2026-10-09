"""Request deadlines survive restart and never poison exact fresh admission."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest
from vonk_agent_protocol import LifecycleState
from vonk_control.lifecycle.model_cache import RECOVERY_BUDGET
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import ModelCacheOperation

from .test_model_cache import _http_artifact, _http_cache_service
from .test_model_cache import cache as cache  # noqa: PLC0414 -- pytest fixture


def _start(service, artifact):
    preview = service.download_preview(
        model_content_sha256="a" * 64, artifacts=[artifact]
    )
    return service.start_download(
        actor="test",
        request_key=str(uuid4()),
        plan_digest=str(preview["plan_digest"]),
        model_content_sha256="a" * 64,
        artifacts=[artifact],
    )


@pytest.mark.parametrize("failure", ["range", "provider"])
def test_partial_content_survives_unknown_deadline_restart_and_fresh_request(
    cache, tmp_path: Path, failure: str
):
    """Catches renewed deadlines, appended bad ranges and retained terminal claims."""
    _, sessions = cache
    now = [datetime(2026, 10, 9, tzinfo=UTC)]
    data = b"exact model content with a retained prefix"
    mode = ["prefix"]
    requests = []

    def handler(request):
        requests.append(request)
        if mode[0] == "prefix":
            return httpx2.Response(200, request=request, content=data[:7])
        if mode[0] == "unknown":
            if failure == "provider":
                return httpx2.Response(
                    503, request=request, headers={"Retry-After": "999999"}
                )
            return httpx2.Response(
                206,
                request=request,
                content=b"unrelated",
                headers={"Content-Range": "unreadable"},
            )
        offset = 7 if request.headers.get("range") == "bytes=7-" else 0
        return httpx2.Response(
            206 if offset else 200,
            request=request,
            content=data[offset:],
            headers={"Content-Range": f"bytes {offset}-{len(data) - 1}/{len(data)}"},
        )

    service, client = _http_cache_service(
        tmp_path, sessions, handler, clock=lambda: now[0]
    )
    artifact = _http_artifact(data)
    restarted = None
    try:
        original = _start(service, artifact)
        service.run_pending()
        partial = service._partial_path(
            str(original.artifact_set_sha256), str(artifact["sha256"])
        )
        assert partial.read_bytes() == data[:7]
        mode[0] = "unknown"
        service.run_pending()
        assert partial.read_bytes() == data[:7]
        assert not service._object_path(str(artifact["sha256"])).exists()
        with sessions() as session:
            stored = session.get(ModelCacheOperation, original.id)
            assert stored is not None
            deadline = service._lifecycle.recovery_deadline(
                service._lifecycle.lifecycle(stored, now[0])
            )
        service.close()
        now[0] += RECOVERY_BUDGET + timedelta(seconds=1)
        restarted = ModelCacheService(
            sessions,
            service.root,
            reserve_bytes=0,
            fixture_sources=True,
            http_client=client,
            clock=lambda: now[0],
        )
        restarted._reconciler.reconcile()
        assert restarted.run_pending() == 0
        with sessions() as session:
            ended = session.get(ModelCacheOperation, original.id)
            assert ended is not None and ended.completed_at is not None
            assert ended.lease_deadline is None and ended.next_action_at is None
            assert (
                restarted._lifecycle.recovery_deadline(
                    restarted._lifecycle.lifecycle(ended, now[0])
                )
                == deadline
            )
        assert not restarted._publication_allowed(
            original.id, str(original.artifact_set_sha256), str(artifact["sha256"])
        )
        assert partial.read_bytes() == data[:7]
        mode[0] = "healthy"
        fresh = _start(restarted, artifact)
        assert (
            fresh.id != original.id
            and fresh.artifact_set_sha256 == original.artifact_set_sha256
        )
        assert restarted.run_pending() == 1
        assert restarted.get_operation(fresh.id).state == LifecycleState.SUCCEEDED
        assert restarted._object_path(str(artifact["sha256"])).read_bytes() == data
        assert requests[-1].headers.get("range") == "bytes=7-"
    finally:
        service.close()
        if restarted is not None:
            restarted.close()
        client.close()


@pytest.mark.parametrize("dependency", ["capacity", "writer"])
def test_dependency_wait_ends_and_fresh_exact_request_has_its_own_budget(
    cache, tmp_path: Path, monkeypatch, dependency: str
):
    """Catches waits that release a slot but renew their lifetime indefinitely."""
    import fcntl
    from collections import namedtuple

    from .test_model_cache import _artifact

    service, sessions = cache
    now = [datetime(2026, 10, 9, tzinfo=UTC)]
    service._clock = lambda: now[0]
    data = b"exact content after an exhausted dependency wait"
    artifact = _artifact(tmp_path, data)
    digest = str(artifact["sha256"])
    lock_path = service.root / "locks" / digest
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    usage = namedtuple("usage", "total used free")
    real_disk_usage = __import__("shutil").disk_usage
    if dependency == "capacity":
        monkeypatch.setattr(
            "vonk_control.model_cache.shutil.disk_usage", lambda _: usage(100, 100, 0)
        )
    with lock_path.open("a+b") as holder:
        if dependency == "writer":
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        original = _start(service, artifact)
        service.run_pending()
        with sessions() as session:
            waiting = session.get(ModelCacheOperation, original.id)
            assert waiting is not None and waiting.completed_at is None
            assert waiting.lease_deadline is None and waiting.next_action_at is not None
        assert not service._object_path(digest).exists()
        now[0] += RECOVERY_BUDGET + timedelta(seconds=1)
        service.run_pending()
        with sessions() as session:
            ended = session.get(ModelCacheOperation, original.id)
            assert ended is not None and ended.completed_at is not None
            assert ended.lease_deadline is None and ended.next_action_at is None
        fresh = _start(service, artifact)
        assert fresh.id != original.id
        assert fresh.artifact_set_sha256 == original.artifact_set_sha256
        if dependency == "writer":
            fcntl.flock(holder, fcntl.LOCK_UN)
        else:
            monkeypatch.setattr(
                "vonk_control.model_cache.shutil.disk_usage", real_disk_usage
            )
    service.run_pending()
    assert service.get_operation(fresh.id).state == LifecycleState.SUCCEEDED
    assert service._object_path(digest).read_bytes() == data
