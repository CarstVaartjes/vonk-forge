"""A time-budget yield is a wait, never a consumed retry."""

from __future__ import annotations

import itertools
import types
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine
from vonk_control.model_cache import removal_reconcile
from vonk_control.models import ModelCacheOperation

from .test_model_cache import _remove_model as _request_model_removal
from .test_model_cache import threaded_cache  # noqa: F401 - fixture
from .test_model_removal_reference_lifecycle import (
    _one_model,
    _register_model,
    _removal_service,
    _seed_model,
)

_YIELDS = 8  # more than RecoveryPolicy().max_failures


def test_removal_survives_repeated_scan_budget_yields(
    postgres_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, sessions = _removal_service(postgres_engine, tmp_path)
    model = _one_model(tmp_path, "scan-yield")
    selector = _register_model(sessions, model)
    digest, _artifact, _set_digest = _seed_model(
        service, tmp_path, model, str(uuid.uuid4())
    )
    removal = _request_model_removal(
        service,
        selector,
        actor="operator",
        request_key=str(uuid.uuid4()),
        model_content_sha256=digest,
    )
    real = removal_reconcile.time
    ticks = itertools.count(0, 1000)
    monkeypatch.setattr(
        removal_reconcile,
        "time",
        types.SimpleNamespace(monotonic=lambda: float(next(ticks))),
    )

    def scheduled() -> datetime | None:
        with sessions() as session:
            stored = session.get(ModelCacheOperation, removal.id)
            return None if stored is None else stored.next_action_at

    def elapse_backoff() -> None:
        with sessions.begin() as session:
            stored = session.get(ModelCacheOperation, removal.id)
            assert stored is not None
            stored.next_action_at = service._clock() - timedelta(seconds=1)

    # Four real failures are already spent: one more would exhaust the limit,
    # so any yield that counted as an attempt would fail the removal.
    for _ in range(4):
        service._defer_model_removal(removal.id, detail="storage briefly failed")
        elapse_backoff()
    for _ in range(_YIELDS):
        service._advance_model_removal(removal.id)
        row = service.get_operation(removal.id)
        assert row.state not in {"failed", "cancelled", "succeeded"}, row.last_error
        assert scheduled() is not None
        elapse_backoff()
    # The scan completes once it has its budget: the removal succeeds.
    monkeypatch.setattr(removal_reconcile, "time", real)
    for _ in range(12):
        service._advance_model_removal(removal.id)
        if service.get_operation(removal.id).state == "succeeded":
            break
        elapse_backoff()
    assert service.get_operation(removal.id).state == "succeeded"
