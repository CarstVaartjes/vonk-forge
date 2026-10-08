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


def test_periodic_reconciliation_restarts_ended_default_key_observations(tmp_path):
    import asyncio

    import httpx2
    from vonk_control.catalog_sync import run_automatic_sync
    from vonk_control.gateway_keys import GatewayKeyService, keep_default_key

    from .test_gateway_keys import MASTER, FakeLiteLlm

    peer = FakeLiteLlm()
    ready = [False]

    def transport(request):
        if not ready[0]:
            raise httpx2.ReadError("offline", request=request)
        return peer.handle(request)

    gateway = GatewayKeyService(
        master_key=lambda: MASTER,
        transport=httpx2.MockTransport(transport),
        intent_root=tmp_path / "mutations",
    )
    path = tmp_path / "client-key"
    stop = asyncio.Event()

    class Catalog:
        def automatic(self):
            stop.set()

    async def run():
        await asyncio.wait_for(
            keep_default_key(
                gateway, stop, path=path, first_delay=0.001, maximum_delay=0.001
            ),
            timeout=10,
        )
        assert not peer.keys
        ready[0] = True
        await asyncio.wait_for(
            run_automatic_sync(
                Catalog(),  # type: ignore[arg-type] - peer fake only implements the exercised boundary
                stop,
                interval_seconds=0,
                settle_seconds=0,
                reconcile=lambda: gateway.ensure_default(path),
            ),
            timeout=10,
        )

    asyncio.run(run())
    assert peer.keys["default"]["key"] == path.read_text().strip()
    assert peer.counter == 1
    from .test_gateway_keys import _created

    assert _created(gateway.create("fresh")).key != path.read_text().strip()
