"""A failing optional constructor cannot remove unrelated Controller routes."""

from __future__ import annotations

import base64
import json
import time
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from vonk_control import api, artifact_blob_store, recipe_packages, route_runtime
from vonk_control.auth import Actor, TokenCodec
from vonk_control.capabilities import CapabilityRegistry, RecoveringService
from vonk_control.capability_contract import (
    CapabilityAvailability,
    CapabilityReason,
    CapabilityUnavailableReply,
    ControllerCapability,
)
from vonk_control.platform_observation import PlatformObservation
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage
from vonk_control.settings import Settings


def _platform(client, headers) -> PlatformObservation:
    response = client.get("/api/platform", headers=headers)
    assert response.status_code == 200, response.text
    records = [json.loads(line) for line in response.content.splitlines()]
    payload = b"".join(base64.b64decode(record["data"]) for record in records[1:-1])
    return PlatformObservation.model_validate_json(payload)


@pytest.mark.parametrize(
    ("service_type", "capability"),
    [
        (FilesystemRuntimeImageStorage, ControllerCapability.RUNTIME_IMAGE_STORAGE),
        (api.ModelCacheService, ControllerCapability.MODEL_CACHE),
        (artifact_blob_store.ArtifactBlobStore, ControllerCapability.ARTIFACT_STORAGE),
        (recipe_packages.RecipePackageClient, ControllerCapability.RECIPE_LIBRARY),
        (api.GatewayKeyService, ControllerCapability.GATEWAY_KEYS),
        (
            route_runtime.AtomicRouteBundlePublisher,
            ControllerCapability.ROUTE_PUBLISHER,
        ),
    ],
)
def test_production_constructor_fault_and_repair(
    tmp_path, monkeypatch, postgres_engine, service_type, capability
):
    # Catches eager startup construction, stale unavailable state, and reads
    # that secretly construct/retry a failed capability.
    from vonk_control.models import Base

    Base.metadata.create_all(postgres_engine)
    now = [datetime.now(UTC)]
    registry = CapabilityRegistry(clock=lambda: now[0])
    monkeypatch.setattr(api, "CapabilityRegistry", lambda: registry)
    broken = [True]
    original = service_type.__init__
    original_gateway = api.GatewayKeyService.__init__
    original_publisher = route_runtime.AtomicRouteBundlePublisher.__init__

    def gateway(self, **kwargs):
        original_gateway(
            self,
            master_key=lambda: "test-master-key",
            transport=httpx2.MockTransport(
                lambda _request: httpx2.Response(200, json={"keys": []})
            ),
        )

    def publisher(self, *args, **kwargs):
        original_publisher(self, tmp_path / "routes", **kwargs)

    monkeypatch.setattr(api.GatewayKeyService, "__init__", gateway)
    monkeypatch.setattr(route_runtime.AtomicRouteBundlePublisher, "__init__", publisher)

    def constructor(self, *args, **kwargs):
        if broken[0]:
            raise OSError("injected construction fault")
        if service_type is route_runtime.AtomicRouteBundlePublisher:
            args = (tmp_path / "routes",)
        if service_type is api.GatewayKeyService:
            gateway(self, **kwargs)
        else:
            original(self, *args, **kwargs)

    monkeypatch.setattr(service_type, "__init__", constructor)
    settings = Settings(
        database_url=postgres_engine.url.render_as_string(hide_password=False),
        deployment_mode="test",
        management_cidrs="127.0.0.1/32",
        state_path=tmp_path / "state",
        secrets_root=tmp_path / "secrets",
        agent_artifact_root=tmp_path / "images",
        model_cache_root=tmp_path / "models",
    )
    app = api.production_app(settings)
    token = TokenCodec(settings.token_signing_key).issue(
        Actor("operator", "administrator"), ttl_seconds=3600, now=int(time.time())
    )
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)
    registry.retry_due()
    status = next(
        item
        for item in _platform(client, headers).capabilities
        if item.capability == capability
    )
    assert status.availability == CapabilityAvailability.UNAVAILABLE
    assert status.reason == CapabilityReason.STORAGE_UNAVAILABLE
    assert status.next_attempt_at is not None
    assert client.get("/api/fleet", headers=headers).status_code == 200
    assert client.get("/api/profile", headers=headers).status_code == 200
    if capability != ControllerCapability.GATEWAY_KEYS:
        assert client.get("/api/key", headers=headers).status_code == 200
    assert _platform(client, headers).capabilities == registry.statuses()
    broken[0] = False
    now[0] += timedelta(seconds=61)
    registry.retry_due()
    status = next(
        item
        for item in _platform(client, headers).capabilities
        if item.capability == capability
    )
    assert status.availability == CapabilityAvailability.AVAILABLE
    assert status.reason is None
    assert client.get("/api/fleet", headers=headers).status_code == 200
    assert client.get("/api/profile", headers=headers).status_code == 200
    assert client.get("/api/key", headers=headers).status_code == 200
    # Release executors/clients without ever starting an unavailable service.
    for service in registry._services:
        value = service._value
        close = getattr(value, "close", None)
        if callable(close):
            close()


def test_guarded_method_binding_and_retry_rate():
    # Catches an eager bound-method lookup, duplicate construction, retry on
    # every read, and a permanent refusal after the input has been repaired.
    now = [datetime.now(UTC)]
    calls = [0]
    broken = [True]

    class Service:
        def __init__(self):
            calls[0] += 1
            if broken[0]:
                raise ValueError

        def issue(self) -> int:
            return 7

    registry = CapabilityRegistry(clock=lambda: now[0])
    service = registry.guard(
        ControllerCapability.CERTIFICATE_AUTHORITY, Service, Service
    )
    issue = service.issue
    assert calls[0] == 0
    registry.retry_due()
    for _ in range(3):
        with pytest.raises(HTTPException) as failure:
            issue()
        assert isinstance(failure.value.detail, CapabilityUnavailableReply)
        reply = CapabilityUnavailableReply.model_validate_json(
            failure.value.detail.model_dump_json()
        )
        assert reply.retryable
    assert calls[0] == 1
    broken[0] = False
    now[0] += timedelta(seconds=1)
    registry.retry_due()
    assert issue() == 7
    assert calls[0] == 2


def test_health_outage_recovers_without_reconstructing_or_replaying_effects():
    now = [datetime.now(UTC)]
    healthy = [True]
    effects = [0]
    constructions = [0]

    class Service:
        def __init__(self):
            constructions[0] += 1

        def issue(self):
            effects[0] += 1

    registry = CapabilityRegistry(clock=lambda: now[0])
    service = registry.guard(
        ControllerCapability.GATEWAY_KEYS,
        Service,
        Service,
        check=lambda _service: healthy[0],
    )
    registry.retry_due()
    service.issue()
    healthy[0] = False
    now[0] += timedelta(seconds=30)
    registry.retry_due()
    with pytest.raises(HTTPException):
        service.issue()
    healthy[0] = True
    now[0] += timedelta(seconds=1)
    registry.retry_due()
    service.issue()
    assert effects[0] == 2 and constructions[0] == 1


def test_background_recovery_is_independent_of_reads_and_requests():
    import asyncio

    async def scenario():
        healthy = [False]
        registry = CapabilityRegistry()
        registry.guard(
            ControllerCapability.GATEWAY_KEYS,
            object,
            object,
            check=lambda _value: healthy[0],
        )
        registry.start_recovery()
        try:
            # One scheduler deadline covers construction failure and repair.
            deadline = asyncio.get_running_loop().time() + 4
            while registry.statuses()[0].reason == CapabilityReason.INITIALIZING:
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.01)
            assert (
                registry.statuses()[0].availability
                == CapabilityAvailability.UNAVAILABLE
            )
            healthy[0] = True
            while (
                registry.statuses()[0].availability != CapabilityAvailability.AVAILABLE
            ):
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.01)
        finally:
            await registry.stop_recovery()

    asyncio.run(scenario())


def test_initializer_retry_reuses_owned_resources_and_shutdown_does_not_construct():
    now = [datetime.now(UTC)]
    broken = [True]
    constructed = [0]
    initialized = [0]
    closed = [0]

    class Service:
        def __init__(self):
            constructed[0] += 1

        def close(self):
            closed[0] += 1

    def initialize(service):
        initialized[0] += 1
        if broken[0]:
            raise OSError

    registry = CapabilityRegistry(clock=lambda: now[0])
    service = registry.guard(
        ControllerCapability.MODEL_CACHE, Service, Service, initialize=initialize
    )
    registry.retry_due()
    assert constructed[0] == initialized[0] == 1
    broken[0] = False
    now[0] += timedelta(seconds=1)
    registry.retry_due()
    assert constructed[0] == 1 and initialized[0] == 2
    service.close()
    assert closed[0] == 1
    unopened = registry.guard(ControllerCapability.ARTIFACT_STORAGE, Service, Service)
    unopened.close()
    assert constructed[0] == 1


@pytest.mark.parametrize("boundary", ("factory", "initializer", "health"))
def test_hung_construction_releases_owner_and_fences_late_result(boundary, monkeypatch):
    """Catches a timeout that leaves the lock owned or publishes a stale result."""
    from threading import Event, Thread

    from vonk_control import capabilities

    threads = []

    def worker(**kwargs):
        thread = Thread(**kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(capabilities, "Thread", worker)

    now = [datetime.now(UTC)]
    entered = Event()
    release = Event()
    finished = Event()
    calls = [0]
    first = object()
    fresh = object()

    def hang():
        entered.set()
        assert release.wait(timeout=5)

    def factory():
        calls[0] += 1
        if calls[0] == 1:
            if boundary == "factory":
                hang()
            return first
        return fresh

    def initialize(value):
        if value is first and boundary == "initializer":
            hang()

    def health(value):
        if value is first and boundary == "health":
            hang()
        if value is first:
            finished.set()
        return True

    owner = RecoveringService(
        ControllerCapability.MODEL_CACHE,
        object,
        factory,
        lambda: now[0],
        initialize=initialize,
        check=health,
        construction_timeout_seconds=0.05,
    )
    try:
        start = time.monotonic()
        owner.attempt_construction()
        assert time.monotonic() - start < 1
        assert entered.is_set()
        assert owner._lock.acquire(blocking=False)
        owner._lock.release()
        assert owner.status.next_attempt_at is not None
        now[0] += timedelta(seconds=1)
        owner.attempt_construction()
        assert owner.require_service() is fresh
        release.set()
        assert finished.wait(timeout=1)
        threads[0].join(timeout=1)
        assert not threads[0].is_alive()
        assert owner.require_service() is fresh
    finally:
        release.set()
