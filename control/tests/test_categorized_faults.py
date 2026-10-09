"""Removal-owner loss recovers and permits a fresh model download."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from vonk_agent_protocol import (
    LifecycleState,
)
from vonk_control.artifact_lifecycle import ArtifactIdentity, reserve_removal

from .test_model_cache import cache  # noqa: F401 - pytest fixture
from .test_model_cache_lifecycle import _queue


def test_model_cache_artifact_lifecycle_leaves_accept_retryable(
    cache,  # noqa: F811 - imported fixture
    tmp_path: Path,
) -> None:
    """A missing stored removal owner heals without refusing a fresh download."""
    service, sessions = cache
    accepted, _artifact = _queue(service, tmp_path, str(uuid4()))
    service.run_pending()
    assert service.get_operation(accepted.id).state == LifecycleState.SUCCEEDED
    assert accepted.artifact_set_sha256 is not None
    with sessions.begin() as session:
        scope = service._model_removal_scope_for_sets(
            session, (accepted.artifact_set_sha256,)
        )
        reserve_removal(
            session,
            (ArtifactIdentity("model-set", accepted.artifact_set_sha256),),
            owner_kind="model-cache-operation",
            owner_id=str(uuid4()),
            fence=str(uuid4()),
            now=datetime.now(UTC),
        )
    with sessions.begin() as session:
        removal = service.accept_removal_for_sets_in_session(
            session,
            actor="test",
            request_key=str(uuid4()),
            selector="owner-recovery",
            selected_sets=scope.selected_sets,
        )
    for _ in range(8):
        service.advance_removals(limit=1)
        if service.get_operation(removal.id).state == LifecycleState.SUCCEEDED:
            break
    assert service.get_operation(removal.id).state == LifecycleState.SUCCEEDED
    fresh, _artifact = _queue(service, tmp_path, str(uuid4()))
    service.run_pending()
    assert service.get_operation(fresh.id).state == LifecycleState.SUCCEEDED
