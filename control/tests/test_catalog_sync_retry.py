from __future__ import annotations

from concurrent.futures import Executor, Future

import pytest
from psycopg.errors import InFailedSqlTransaction
from sqlalchemy.exc import InternalError

from vonk_agent_protocol.reason_codes.catalog import CatalogSyncCode
from vonk_agent_protocol.reason_codes.recipes import RecipePackageCode
from vonk_control.catalog.sync import (
    ManagedRecipeCatalogSyncService,
    catalog_sync_failure_reason,
    catalog_sync_retry_delay,
)
from vonk_control.recipe_packages.contracts import RecipePackageError

from .signed_recipe_release import signed_recipe_releases


class _PeriodicCatalog(ManagedRecipeCatalogSyncService):
    """Real shared retry owner with test-provided observations and storage."""

    def __init__(self) -> None:
        pass

    def automatic_wait_delay(self) -> float:
        return 0.0

    def held_elsewhere(self) -> bool:
        return False

    def record_automatic_wait(
        self, delay_seconds: int, *, failed: bool, observed_id: str | None = None
    ) -> None:
        pass


def test_failed_automatic_sync_retries_sooner_with_bounded_backoff(monkeypatch) -> None:
    # A failure must not wait the full steady-state interval (the old loop
    # did), and repeated failures must back off without exceeding it.
    monkeypatch.setattr(
        "vonk_control.catalog.sync.random.randint", lambda low, high: high
    )
    assert [catalog_sync_retry_delay(failures, 900) for failures in range(7)] == [
        900,
        30,
        60,
        120,
        240,
        480,
        900,
    ]
    assert catalog_sync_retry_delay(10_000, 900) == 900
    assert catalog_sync_retry_delay(1, 10) == 10


def test_failed_sync_log_names_the_reason_bounded_and_printable() -> None:
    # The log used to say only "RecipePackageError (recipe_package.response_invalid)".
    error = RecipePackageError(
        RecipePackageCode.RELEASE_INCOMPLETE,
        "signed release v2.1.0 lists package assets that were never uploaded "
        "or were removed: a.tar.gz, b.tar.gz",
    )
    # Preserve the useful external explanation without requiring taxonomy.
    rendered = catalog_sync_failure_reason(error)
    assert error.detail in rendered
    hostile = RecipePackageError(RecipePackageCode.RESPONSE_INVALID, "x\ny\x1b[2Jz")
    rendered = catalog_sync_failure_reason(hostile)
    assert "\n" not in rendered
    assert "\x1b" not in rendered
    assert "x y [2Jz" in rendered
    # An untyped exception's message is never echoed.
    assert "secret" not in catalog_sync_failure_reason(RuntimeError("secret"))


def test_database_sync_log_reports_server_condition_without_private_sql() -> None:
    original = InFailedSqlTransaction("private driver detail")
    error = InternalError("private SQL", {"parameter": "secret"}, original)
    rendered = catalog_sync_failure_reason(error)
    assert f"SQLSTATE {original.sqlstate}" in rendered
    assert "private" not in rendered
    assert "secret" not in rendered


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Inline(Executor):
    """Run the sync in the calling thread: the next tick collects its outcome."""

    def submit(self, fn, /, *args, **kwargs):
        done: Future = Future()
        done.set_result(fn(*args, **kwargs))
        return done


def test_the_automatic_sync_is_asked_again_after_every_unsettled_attempt(
    tmp_path, monkeypatch
) -> None:
    from vonk_control.catalog.sync import AutomaticCatalogSync, CatalogSyncUnsettled

    from .test_catalog_sync_topology import _Catalog, _example, _item, _sync

    completed = _sync(
        tmp_path, _Catalog(None), _item(_example("recipe-source-build.json"))
    )
    monkeypatch.setattr(
        "vonk_control.catalog.sync.random.randint", lambda low, high: high
    )

    class _Service(_PeriodicCatalog):
        def __init__(self) -> None:
            self.attempts = 0

        def automatic(self):
            self.attempts += 1
            if self.attempts == 1:
                raise CatalogSyncUnsettled(
                    CatalogSyncCode.IN_PROGRESS, "another sync is running"
                )
            if self.attempts == 2:
                raise OSError("library unreachable")
            if self.attempts == 3:
                raise ValueError("unclassified")
            return completed

    service = _Service()
    clock = _Clock()
    cadence = AutomaticCatalogSync(
        service, interval_seconds=900, monotonic=clock, executor=Inline()
    )
    ran = []
    for _ in range(5):
        ran.append(cadence.tick())
        # Nothing runs again before the delay the attempt chose.
        assert not cadence.tick()
        clock.now += 900
    assert ran == [True] * 5
    assert service.attempts == 5


def test_partial_receipts_back_off_then_success_resets_the_retry_episode(
    tmp_path, monkeypatch
):
    from dataclasses import replace

    from vonk_agent_protocol.state_machines import CatalogSyncState
    from vonk_control.catalog import sync as catalog_sync

    from .test_catalog_sync_topology import _Catalog, _example, _item, _sync

    completed = _sync(
        tmp_path, _Catalog(None), _item(_example("recipe-source-build.json"))
    )
    episodes = []
    delay = catalog_sync.catalog_sync_retry_delay

    def observe_delay(failures, interval_seconds):
        episodes.append(failures)
        return delay(failures, interval_seconds)

    class Service(_PeriodicCatalog):
        attempts = 0

        def automatic(self):
            self.attempts += 1
            if self.attempts < 3:
                return replace(completed, state=CatalogSyncState.PARTIAL)
            return completed

    monkeypatch.setattr(catalog_sync, "catalog_sync_retry_delay", observe_delay)
    clock = _Clock()
    cadence = catalog_sync.AutomaticCatalogSync(
        Service(), interval_seconds=60, monotonic=clock, executor=Inline()
    )
    for _ in range(3):
        assert cadence.tick()
        assert not cadence.tick()  # collects the outcome; the next one waits
        clock.now += 60
    assert episodes == [1, 2, 0]
    fresh = _sync(tmp_path, _Catalog(None), _item(_example("recipe-source-build.json")))
    assert fresh.id != completed.id
    assert fresh.completed_at is not None


@pytest.mark.usefixtures(signed_recipe_releases.__name__)
def test_worker_runs_the_first_sync_and_then_follows_the_persisted_wait(
    tmp_path,
) -> None:
    """Catches a fresh install never syncing, or a restart ignoring the recorded wait."""
    from datetime import UTC, datetime, timedelta

    from vonk_control.catalog.sync import AutomaticCatalogSync

    from .test_catalog_sync import _fixture

    sessions, catalog, reader, _item = _fixture(tmp_path)
    now = [datetime(2026, 11, 20, tzinfo=UTC)]
    service = ManagedRecipeCatalogSyncService(
        sessions, catalog=catalog, reader=reader, clock=lambda: now[0]
    )
    assert service.latest() is None
    clock = _Clock()
    assert AutomaticCatalogSync(
        service, interval_seconds=60, monotonic=clock, executor=Inline()
    ).tick()
    first = service.latest()
    assert first is not None
    assert first.completed_at is not None
    # A restarted worker waits out the recorded check, then syncs again.
    restarted = AutomaticCatalogSync(
        service, interval_seconds=60, monotonic=clock, executor=Inline()
    )
    assert not restarted.tick()
    now[0] += timedelta(seconds=60)
    clock.now += 60
    assert restarted.tick()


def test_retry_jitter_keeps_the_delay_capped(
    monkeypatch,
):
    """Catches synchronized retries and jitter escaping the interval cap."""
    monkeypatch.setattr(
        "vonk_control.catalog.sync.random.randint", lambda low, high: low
    )
    assert catalog_sync_retry_delay(1, 60) == 15
    assert catalog_sync_retry_delay(10_000, 60) == 30
    assert catalog_sync_retry_delay(0, 60) == 60
