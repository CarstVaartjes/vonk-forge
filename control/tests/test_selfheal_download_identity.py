"""Cancelled selector requests are history, never the identity of a fresh request."""

from uuid import uuid4

from vonk_agent_protocol import LifecycleState
from vonk_control.model_cache import ModelCacheService

from .non_blocking import assert_ended_without_blocking
from .test_model_cache import _artifact
from .test_model_cache import cache as cache  # noqa: PLC0414 -- pytest fixture


def test_cancelled_selector_replays_only_original_key_after_restart(
    cache, tmp_path, monkeypatch
):
    """Catches selector/set deduplication reattaching new intent to a cancelled request."""
    service, sessions = cache
    content = b"fresh exact bytes after cancelled selector request"
    artifact = _artifact(tmp_path, content)
    manifest = service.resolve_artifact_set(
        model_content_sha256="a" * 64, artifacts=[artifact]
    )
    selector = "vonk-forge/skintokens-pytorch-single"
    original_key = str(uuid4())

    def bind_selector(owner):
        monkeypatch.setattr(
            owner, "_resolve_model_selector", lambda _selector: "a" * 64
        )
        monkeypatch.setattr(owner, "resolve_artifact_set", lambda **_kwargs: manifest)

    bind_selector(service)
    original = service.download_model_selector(
        selector, actor="test", request_key=original_key
    )
    cancelled = service.cancel_operation(
        original.id, actor="test", request_key=str(uuid4()), reason="new intent"
    )
    assert cancelled.state == LifecycleState.CANCELLED
    restarted = ModelCacheService(
        sessions, service.root, reserve_bytes=0, fixture_sources=True
    )
    try:
        bind_selector(restarted)
        replay = restarted.download_model_selector(
            selector, actor="test", request_key=original_key
        )
        assert replay.id == original.id
        assert replay.state == LifecycleState.CANCELLED
        _, fresh = assert_ended_without_blocking(
            restarted,
            cancelled,
            end=lambda receipt: restarted.get_operation(receipt.id),
            fresh=lambda _world: restarted.download_model_selector(
                selector, actor="test", request_key=str(uuid4())
            ),
            assert_released=lambda: _assert_no_claim(sessions, original.id),
        )
        assert fresh.id != original.id
        assert fresh.artifact_set_sha256 == original.artifact_set_sha256
        assert restarted.run_pending() == 1
        assert restarted.get_operation(fresh.id).state == LifecycleState.SUCCEEDED
        assert restarted.get_operation(original.id).state == LifecycleState.CANCELLED
        assert (
            restarted._object_path(manifest.artifacts[0].sha256).read_bytes() == content
        )
    finally:
        restarted.close()


def _assert_no_claim(sessions, operation_id):
    from vonk_control.models import ModelCacheOperation

    with sessions() as session:
        operation = session.get(ModelCacheOperation, operation_id)
        assert operation is not None
        assert operation.lease_deadline is None
        assert operation.next_action_at is None
