from __future__ import annotations

from vonk_control.catalog_sync import (
    catalog_sync_failure_reason,
    catalog_sync_retry_delay,
)
from vonk_control.recipe_packages import RecipePackageError


def test_failed_automatic_sync_retries_sooner_with_bounded_backoff() -> None:
    # A failure must not wait the full steady-state interval (the old loop
    # did), and repeated failures must back off without exceeding it.
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
        "recipe_package.release_incomplete",
        "signed release v2.1.0 lists package assets that were never uploaded "
        "or were removed: a.tar.gz, b.tar.gz",
    )
    assert catalog_sync_failure_reason(error) == (
        "RecipePackageError (recipe_package.release_incomplete): signed release "
        "v2.1.0 lists package assets that were never uploaded or were removed: "
        "a.tar.gz, b.tar.gz"
    )
    hostile = RecipePackageError("recipe_package.response_invalid", "x\ny\x1b[2Jz")
    assert catalog_sync_failure_reason(hostile) == (
        "RecipePackageError (recipe_package.response_invalid): x y [2Jz"
    )
    # An untyped exception's message is never echoed.
    assert catalog_sync_failure_reason(RuntimeError("secret")) == (
        "RuntimeError (unclassified)"
    )


def test_the_automatic_sync_is_asked_again_after_every_unsettled_attempt() -> None:
    import asyncio

    from vonk_control.catalog_sync import CatalogSyncUnsettled, run_automatic_sync

    stop = asyncio.Event()

    class _Service:
        def __init__(self) -> None:
            self.attempts = 0

        def automatic(self) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise CatalogSyncUnsettled(
                    "catalog.sync_in_progress", "another sync is running"
                )
            if self.attempts == 2:
                raise OSError("library unreachable")
            if self.attempts == 3:
                raise ValueError("unclassified")
            stop.set()

    service = _Service()

    async def run() -> None:
        await asyncio.wait_for(
            run_automatic_sync(
                service,  # type: ignore[arg-type]
                stop,
                interval_seconds=0,
                settle_seconds=0.0,
            ),
            timeout=5,
        )

    asyncio.run(run())
    assert service.attempts == 4
