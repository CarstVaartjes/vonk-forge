"""A recipe cache removal is never ended by a clock; only real failures end it."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import LifecycleState, ModelCacheCode
from vonk_control.models import Base, Job
from vonk_control.recipe_image_availability_view_contract import (
    RecipeCacheRemovalStatus,
)
from vonk_control.recovery_policy import RecoveryPolicy
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    RuntimeImagePreparationError,
)

from .recipe_removal_review_support import remove_after_review
from .runtime_image_fixtures import place_test_image
from .test_recipe_image_availability import (
    ARCHIVE,
    ARCHIVE_SHA,
    _add_head,
    _add_revision,
    _recipe,
    _reference_receipt,
    _runtime,
    _service,
)

_REQUEST = "00000000-0000-4000-8000-000000000071"


def _removal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contended: list[int]):
    """A removal whose image publication lock is held ``contended[0]`` times."""

    recipe = _recipe("recipe-source-build.json")
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'waits.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    now = [datetime.now(UTC)]
    with sessions.begin() as session:
        _add_head(session, _add_revision(session, "rev-waits", recipe))
    storage = FilesystemRuntimeImageStorage(tmp_path / "managed-waits")
    place_test_image(storage, ARCHIVE_SHA, len(ARCHIVE))
    receipt_path = storage.root / f"{ARCHIVE_SHA}.receipt.json"
    receipt_path.write_text(json.dumps(_reference_receipt().model_dump(mode="json")))
    original_lock = storage.publication_lock
    started = [False]

    def contend(archive_sha256: str):
        if started[0] and contended[0] > 0:
            contended[0] -= 1
            raise RuntimeImagePreparationError(
                "runtime_image.publication_contended",
                "another owner holds the exact image publication lock",
                retryable=True,
                recovery_actions=("retry",),
            )
        return original_lock(archive_sha256)

    monkeypatch.setattr(storage, "publication_lock", contend)
    service = _service(
        sessions,
        storage=storage,
        authority=lambda *_args, **_kwargs: (recipe, _runtime()),
        clock=lambda: now[0],
    )
    accepted = remove_after_review(
        service, recipe.identity.slug, actor="operator", request_id=_REQUEST
    )
    assert accepted["state"] == "queued"
    started[0] = True
    return engine, sessions, service, now, recipe


def _assert_fresh_preparation_admitted(service) -> None:
    fresh = service.start(
        "rev-waits", actor="operator", request_id="after-recipe-removal-wait"
    )
    assert fresh.state == "queued"


def _status(service) -> RecipeCacheRemovalStatus:
    status = service.get_operator_request(_REQUEST, actor="operator")
    assert isinstance(status, RecipeCacheRemovalStatus)
    return status


def test_a_removal_retried_past_the_old_five_minute_cap_still_completes(
    tmp_path, monkeypatch
) -> None:
    """Catches the created_at + 300 s cap ending a removal whose retry is due."""

    engine, _sessions, service, now, _ = _removal(tmp_path, monkeypatch, [1])
    try:
        service.advance_removals(limit=1)
        assert _status(service).state == LifecycleState.BACKOFF
        # The retry falls due an hour later (a Controller restart, a slow tick).
        now[0] += timedelta(hours=1)
        for _ in range(4):
            service.advance_removals(limit=1)
        assert _status(service).state == "succeeded"
        _assert_fresh_preparation_admitted(service)
    finally:
        engine.dispose()


def test_a_removal_whose_own_attempts_keep_failing_ends_with_that_evidence(
    tmp_path, monkeypatch
) -> None:
    """Real failures still end it: the retry budget, with the failure as cause."""

    engine, _sessions, service, now, recipe = _removal(tmp_path, monkeypatch, [10**6])
    try:
        for _ in range(RecoveryPolicy().max_failures + 2):
            service.advance_removals(limit=1)
            now[0] += timedelta(hours=1)
        ended = _status(service)
        assert ended.state == LifecycleState.FAILED
        assert ended.failure is not None
        assert ended.failure.code == "runtime_image.publication_contended"
        fresh = remove_after_review(
            service,
            recipe.identity.slug,
            actor="operator",
            request_id="00000000-0000-4000-8000-000000000072",
        )
        assert fresh["state"] == "queued"
        _assert_fresh_preparation_admitted(service)
    finally:
        engine.dispose()


def test_a_wait_on_a_live_model_removal_child_never_exhausts(
    tmp_path, monkeypatch
) -> None:
    """A wait on the live model-removal child is not a failure: it never ends it."""

    engine, sessions, service, now, _ = _removal(tmp_path, monkeypatch, [0])
    try:
        with sessions() as session:
            operation_id = (
                session.query(Job.id)
                .filter(Job.kind == "recipe.cache.remove.v2")
                .scalar()
            )
        assert operation_id is not None
        for _ in range(RecoveryPolicy().max_failures * 3):
            service._record_recipe_removal_failure(
                operation_id,
                code=ModelCacheCode.REMOVAL_CHILD_PENDING,
                detail="waiting for the accepted model-removal child to finish",
                retryable=True,
                consume_retry=False,
            )
            now[0] += timedelta(hours=1)
        waiting = _status(service)
        assert waiting.state == LifecycleState.BACKOFF
        assert waiting.failure is not None and waiting.failure.retryable
        for _ in range(4):
            service.advance_removals(limit=1)
        assert _status(service).state == "succeeded"
        _assert_fresh_preparation_admitted(service)
    finally:
        engine.dispose()
