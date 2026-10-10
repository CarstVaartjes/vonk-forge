from __future__ import annotations

import pytest
from vonk_agent_protocol import CatalogSyncCode, RecipePackageCode
from vonk_control.catalog_sync import (
    catalog_sync_failure_reason,
    catalog_sync_retry_delay,
)
from vonk_control.recipe_packages import RecipePackageError


def test_failed_automatic_sync_retries_sooner_with_bounded_backoff(monkeypatch) -> None:
    # A failure must not wait the full steady-state interval (the old loop
    # did), and repeated failures must back off without exceeding it.
    monkeypatch.setattr(
        "vonk_control.catalog_sync.random.randint", lambda low, high: high
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
    assert "\n" not in rendered and "\x1b" not in rendered
    assert "x y [2Jz" in rendered
    # An untyped exception's message is never echoed.
    assert "secret" not in catalog_sync_failure_reason(RuntimeError("secret"))


def test_the_automatic_sync_is_asked_again_after_every_unsettled_attempt(
    tmp_path,
) -> None:
    import asyncio

    from vonk_control.catalog_sync import CatalogSyncUnsettled, run_automatic_sync

    from .test_catalog_sync_topology import _Catalog, _example, _item, _sync

    completed = _sync(
        tmp_path, _Catalog(None), _item(_example("recipe-source-build.json"))
    )
    stop = asyncio.Event()

    class _Service:
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
            stop.set()
            return completed

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


def test_partial_receipts_back_off_then_success_resets_the_retry_episode(
    tmp_path, monkeypatch
):
    import asyncio
    from dataclasses import replace

    from vonk_agent_protocol import CatalogSyncState
    from vonk_control import catalog_sync

    from .test_catalog_sync_topology import _Catalog, _example, _item, _sync

    completed = _sync(
        tmp_path, _Catalog(None), _item(_example("recipe-source-build.json"))
    )
    stop = asyncio.Event()
    checks = []
    delay = catalog_sync.catalog_sync_retry_delay
    wait_for = asyncio.wait_for
    scheduled_delay = None

    def observe_delay(failures, interval_seconds):
        nonlocal scheduled_delay
        scheduled_delay = failures
        return delay(failures, interval_seconds)

    async def observe_wait(awaitable, *, timeout):
        if scheduled_delay is not None:
            checks.append(scheduled_delay)
        return await wait_for(awaitable, timeout=timeout)

    class Service:
        attempts = 0

        def automatic(self):
            self.attempts += 1
            if self.attempts < 3:
                return replace(completed, state=CatalogSyncState.PARTIAL)
            stop.set()
            return completed

    service = Service()
    monkeypatch.setattr(catalog_sync, "catalog_sync_retry_delay", observe_delay)
    monkeypatch.setattr(asyncio, "wait_for", observe_wait)

    async def run():
        await asyncio.wait_for(
            catalog_sync.run_automatic_sync(
                service,  # type: ignore[arg-type]
                stop,
                interval_seconds=0,
                settle_seconds=0,
            ),
            timeout=5,
        )

    asyncio.run(run())
    assert checks == [1, 2, 0]
    assert service.attempts == 3
    fresh = _sync(tmp_path, _Catalog(None), _item(_example("recipe-source-build.json")))
    assert fresh.id != completed.id and fresh.completed_at is not None


def test_periodic_reconciliation_restarts_ended_default_key_observations(tmp_path):
    import asyncio

    import httpx2
    from vonk_control.catalog_sync import run_automatic_sync
    from vonk_control.gateway_keys import GatewayKeyService, keep_default_key

    from .test_catalog_sync_topology import _Catalog, _example, _item, _sync
    from .test_gateway_keys import MASTER, FakeLiteLlm

    completed = _sync(
        tmp_path, _Catalog(None), _item(_example("recipe-source-build.json"))
    )

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
            return completed

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


@pytest.mark.parametrize("fault_owner", ["catalog", "reconcile"])
def test_periodic_recovery_paths_do_not_suppress_each_other(
    tmp_path, monkeypatch, fault_owner
):
    import asyncio

    from vonk_control import catalog_sync

    from .test_catalog_sync_topology import _Catalog, _example, _item, _sync

    completed = _sync(
        tmp_path, _Catalog(None), _item(_example("recipe-source-build.json"))
    )
    stop = asyncio.Event()
    attempts = {"catalog": 0, "reconcile": 0}
    retry_episodes = []

    def delay(failures, interval_seconds):
        retry_episodes.append(failures)
        return 0

    def observe(owner):
        attempts[owner] += 1
        if owner == fault_owner and attempts[owner] < 3:
            raise OSError("temporarily unavailable")

    def reconcile():
        observe("reconcile")

    class Catalog:
        def automatic(self):
            observe("catalog")
            if attempts["catalog"] == 3:
                stop.set()
            return completed

    monkeypatch.setattr(catalog_sync, "catalog_sync_retry_delay", delay)

    async def run():
        await asyncio.wait_for(
            catalog_sync.run_automatic_sync(
                Catalog(),  # type: ignore[arg-type]
                stop,
                interval_seconds=0,
                settle_seconds=0,
                reconcile=reconcile,
            ),
            timeout=5,
        )

    asyncio.run(run())
    assert attempts == {"catalog": 3, "reconcile": 3}
    # Compute one delay for both logging and scheduling; recovery resets it.
    assert retry_episodes == [1, 2, 0]
    fresh = _sync(tmp_path, _Catalog(None), _item(_example("recipe-source-build.json")))
    assert fresh.id != completed.id and fresh.completed_at is not None


@pytest.mark.parametrize("typed_unknown", [False, True])
def test_gateway_capability_recovers_without_blocking_fresh_catalog_requests(
    tmp_path, monkeypatch, caplog, typed_unknown
):
    """Catches probing a blocked relay route and dropping the 503 typed cause."""
    import asyncio
    from datetime import UTC, datetime, timedelta

    import httpx2
    from vonk_agent_protocol import ErrorCategory, UnknownError, WaitReason
    from vonk_control import catalog_sync
    from vonk_control.capabilities import RecoveringService
    from vonk_control.capability_contract import ControllerCapability
    from vonk_control.gateway_keys import GatewayKeyService

    from .test_catalog_sync_topology import _Catalog, _example, _item, _sync
    from .test_gateway_keys import MASTER, FakeLiteLlm, _authorizes

    ready = False
    now = datetime(2026, 10, 10, tzinfo=UTC)
    peer = FakeLiteLlm()
    stop = asyncio.Event()
    key_path = tmp_path / "client-key"
    item = _item(_example("recipe-source-build.json"))
    catalog = _Catalog(None)

    def transport(request):
        # Match Caddy's actual key-only route boundary, even after recovery.
        if not request.url.path.startswith("/key/"):
            return httpx2.Response(404)
        if not ready:
            return httpx2.Response(503, json={"error": "dependency unavailable"})
        return peer.handle(request)

    gateway = GatewayKeyService(
        master_key=lambda: MASTER,
        transport=httpx2.MockTransport(transport),
        intent_root=tmp_path / "mutations",
    )
    guarded = RecoveringService(
        ControllerCapability.GATEWAY_KEYS,
        GatewayKeyService,
        lambda: gateway,
        lambda: now,
        check=lambda service: service.check_health(),
    )

    class Catalog:
        def automatic(self):
            receipt = _sync(tmp_path, catalog, item)
            if ready:
                stop.set()
            return receipt

    def reconcile():
        if typed_unknown and not ready:
            return UnknownError(
                category=ErrorCategory.UNKNOWN,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        return guarded.ensure_default(key_path)

    async def advance_dependency(awaitable, *, timeout):
        nonlocal ready, now
        awaitable.close()
        if stop.is_set():
            return
        if timeout:
            if typed_unknown:
                assert "CatalogSyncUnsettled (observation-unavailable)" in caplog.text
            else:
                assert "HTTP 503; capability.dependency_unavailable" in caplog.text
                assert "capability=gateway-keys" in caplog.text
            assert f"retrying in {timeout} seconds" in caplog.text
            assert "unclassified" not in caplog.text
            # A new request succeeds while the dependency remains unavailable.
            assert _sync(tmp_path, catalog, item).state == "current"
            ready = True
            now += timedelta(minutes=1)
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", advance_dependency)
    asyncio.run(
        catalog_sync.run_automatic_sync(
            Catalog(),  # type: ignore[arg-type] - actual catalog work above
            stop,
            interval_seconds=60,
            settle_seconds=0,
            reconcile=reconcile,
        )
    )
    assert _authorizes(peer, key_path.read_text().strip())


def test_retry_jitter_keeps_the_delay_capped(
    monkeypatch,
):
    """Catches synchronized retries and jitter escaping the interval cap."""
    monkeypatch.setattr(
        "vonk_control.catalog_sync.random.randint", lambda low, high: low
    )
    assert catalog_sync_retry_delay(1, 60) == 15
    assert catalog_sync_retry_delay(10_000, 60) == 30
    assert catalog_sync_retry_delay(0, 60) == 60


def test_gateway_health_uses_the_key_only_relay(tmp_path):
    """Catches marking a healthy gateway unavailable through a forbidden probe."""
    import httpx2
    from vonk_control.gateway_keys import GatewayKeyService

    from .test_gateway_keys import MASTER, FakeLiteLlm

    peer = FakeLiteLlm()

    def relay(request):
        if request.url.path.startswith("/key/"):
            return peer.handle(request)
        return httpx2.Response(404)

    gateway = GatewayKeyService(
        master_key=lambda: MASTER,
        transport=httpx2.MockTransport(relay),
        intent_root=tmp_path / "mutations",
    )
    assert gateway.check_health()
