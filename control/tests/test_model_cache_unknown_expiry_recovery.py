"""An unconfirmed cancellation must not poison a later exact artifact request."""

from __future__ import annotations

import fcntl
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import Engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control.lifecycle import STOP_BUDGET, Effect, Outcome, Reported, State
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import Base, ModelCacheOperation

from .non_blocking import assert_no_orphaned_holds
from .test_model_cache import _artifact


def test_unknown_cancel_expiry_restart_admits_fresh_exact_artifact_and_fences_old_owner(
    tmp_path: Path, postgres_engine: Engine
) -> None:
    """Catch a terminal cancel retaining claims or accepting its late executor."""
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    now = [datetime.now(UTC)]
    root = tmp_path / "cache"
    content = b"same exact artifact after unknown cancellation"
    artifact = _artifact(tmp_path, content)
    model_digest = "a" * 64
    service = ModelCacheService(
        sessions, root, reserve_bytes=0, fixture_sources=True, clock=lambda: now[0]
    )
    restarted: ModelCacheService | None = None
    try:
        preview = service.download_preview(
            model_content_sha256=model_digest, artifacts=[artifact]
        )
        original = service.start_download(
            actor="operator",
            request_key=str(uuid4()),
            plan_digest=str(preview["plan_digest"]),
            model_content_sha256=model_digest,
            artifacts=[artifact],
        )
        assert original.artifact_set_sha256 is not None
        assert service._claim_operations(limit=1, respect_backoff=False) == [
            (original.id, "download")
        ]
        with sessions() as session:
            claimed = session.get(ModelCacheOperation, original.id)
            assert claimed is not None and claimed.state == "running"
            old_fence = claimed.fence
            assert old_fence is not None and claimed.lease_deadline is not None

        object_digest = str(artifact["sha256"])
        writer_lock = root / "locks" / object_digest
        writer_lock.parent.mkdir(parents=True, exist_ok=True)
        with writer_lock.open("a+b") as writer:
            # An empty owner record is conservatively this operation's writer.
            # The real flock stays busy through every stop-budget observation.
            fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            cancelling = service.cancel_operation(
                original.id,
                actor="operator",
                request_key=str(uuid4()),
                reason="new operator intent",
            )
            assert cancelling.state == "observing"
            for _ in range(STOP_BUDGET + 3):
                now[0] += timedelta(minutes=5)
                service._reconciler.reconcile()
                if service.get_operation(original.id).state == "cancelled":
                    break
            with sessions() as session:
                ended = session.get(ModelCacheOperation, original.id)
                assert ended is not None and ended.state == "cancelled"
                residue = ended.last_error
                assert residue is not None and "effect unknown" in residue
                assert ended.completed_at is not None
                assert ended.next_action_at is None and ended.lease_deadline is None
                assert (
                    service._lifecycle.lifecycle(ended, now[0]).effect is Effect.UNKNOWN
                )
                assert_no_orphaned_holds(session)
            assert not service._publication_allowed(
                original.id, original.artifact_set_sha256, object_digest
            )
            assert not service._object_path(object_digest).exists()

        # The physical writer is now gone. A replacement Controller reads the
        # original ending but accepts a new exact reviewed request normally.
        service.close()
        restarted = ModelCacheService(
            sessions, root, reserve_bytes=0, fixture_sources=True, clock=lambda: now[0]
        )
        with sessions() as session:
            ended = session.get(ModelCacheOperation, original.id)
            assert ended is not None and ended.last_error == residue
            assert (
                restarted._lifecycle.lifecycle(ended, now[0]).effect is Effect.UNKNOWN
            )
        reviewed = restarted.download_preview(
            model_content_sha256=model_digest, artifacts=[artifact]
        )
        fresh = restarted.start_download(
            actor="operator",
            request_key=str(uuid4()),
            plan_digest=str(reviewed["plan_digest"]),
            model_content_sha256=model_digest,
            artifacts=[artifact],
        )
        assert fresh.id != original.id and fresh.request_key != original.request_key
        assert fresh.artifact_set_sha256 == original.artifact_set_sha256
        assert restarted._claim_operations(limit=1, respect_backoff=False) == [
            (fresh.id, "download")
        ]
        with sessions() as session:
            current = session.get(ModelCacheOperation, fresh.id)
            assert current is not None and current.state == "running"
            assert current.fence == restarted._claim_owner != old_fence
        restarted._run_download(fresh.id, force=False)
        assert restarted.get_operation(fresh.id).state == "succeeded"
        assert restarted._object_path(object_digest).read_bytes() == content

        # A delayed completion from the expired owner cannot resurrect its
        # cancelled intent or alter the new operation's verified bytes.
        with sessions.begin() as session:
            ended = session.get(ModelCacheOperation, original.id, with_for_update=True)
            assert ended is not None
            late = restarted._lifecycle.complete(
                ended, Reported(Outcome.DONE, fence=old_fence), now[0]
            )
            assert late.state is State.CANCELLED
            assert ended.last_error == residue
        assert service._mark_running(original.id) is None
        assert not restarted._publication_allowed(
            original.id, original.artifact_set_sha256, object_digest
        )
        assert restarted._object_path(object_digest).read_bytes() == content
        with sessions() as session:
            assert set(session.scalars(select(ModelCacheOperation.id))) == {
                original.id,
                fresh.id,
            }
            assert_no_orphaned_holds(session)
    finally:
        if restarted is not None:
            restarted.close()
        service.close()
