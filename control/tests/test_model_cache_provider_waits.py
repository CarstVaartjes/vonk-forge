"""A provider rate limit or quota is a typed, visible wait owned by that provider.

It spends no retry, re-checks on the provider's own hint (or on the provider-wait
cadence without one), never runs out the recovery budget while the provider
keeps saying "later", holds fresh requests for the same provider, and every
waiter recovers once the provider answers.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import httpx2
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_agent_protocol import LifecycleState, ModelCacheCode
from vonk_agent_protocol.http_failure import TransientReason
from vonk_control.lifecycle.model_cache import RECOVERY_BUDGET
from vonk_control.model_cache import ModelCacheService
from vonk_control.model_cache.artifacts import ArtifactSpec
from vonk_control.model_cache.constants import ProviderKind
from vonk_control.model_cache.errors import ModelCacheStorageUnknown
from vonk_control.model_cache.providers import PROVIDERS
from vonk_control.models import Base, ModelCacheOperation

from .test_model_cache_github_release import (
    NOW,
    _client,
    _insert_model,
    _model,
    _serve_release_and_asset,
)

THIRTY_HOURS = 30 * 60 * 60
WAITING = {LifecycleState.QUEUED, LifecycleState.BACKOFF}


def _sqlite_sessions():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(engine, expire_on_commit=False)


class _Provider:
    """One provider behind a fake transport: limited until it heals."""

    def __init__(self, kind: str, data: bytes, limit: Callable[[], httpx2.Response]):
        self.kind = kind
        self.data = data
        self.limit = limit
        self.limited = True
        self.calls = 0

    def handler(self) -> Callable[[httpx2.Request], httpx2.Response]:
        healthy = _serve_release_and_asset(self.data, [])

        def handle(request: httpx2.Request) -> httpx2.Response:
            self.calls += 1
            if self.limited:
                reply = self.limit()
                return httpx2.Response(
                    reply.status_code, request=request, headers=reply.headers
                )
            if self.kind == ProviderKind.HUGGINGFACE_FILE:
                return httpx2.Response(200, request=request, content=self.data)
            return healthy(request)

        return handle


def _hf_artifact(data: bytes) -> dict[str, object]:
    return {
        "id": "weights",
        "path": "weights.safetensors",
        "kind": ProviderKind.HUGGINGFACE_FILE,
        "source": "https://huggingface.co/acme/model/resolve/"
        + "a" * 40
        + "/weights.safetensors",
        "revision": "a" * 40,
        "sha256": hashlib.sha256(data).hexdigest(),
        "download_bytes": len(data),
        "roles": ["model"],
        "model_content_sha256": hashlib.sha256(b"hf-model").hexdigest(),
    }


class _Harness:
    """Start requests and run worker passes for one provider kind."""

    def __init__(self, sessions, root: Path, provider: _Provider, now: list[datetime]):
        self.sessions = sessions
        self.provider = provider
        self.now = now
        self.client = _client(provider.handler())
        self.root = root
        self.service = self._build()
        self.release_digest: str | None = None
        if provider.kind == ProviderKind.GITHUB_RELEASE_ASSET:
            self.release_digest, _ = _insert_model(sessions, _model(provider.data))

    def _build(self) -> ModelCacheService:
        service = ModelCacheService(
            self.sessions,
            self.root,
            reserve_bytes=0,
            http_client=self.client,
            fixture_sources=self.provider.kind != ProviderKind.GITHUB_RELEASE_ASSET,
            clock=lambda: self.now[0],
        )
        service.resume_operations()  # the production start-up hook
        return service

    def restart(self) -> None:
        self.service.close()
        self.service = self._build()

    def start(self) -> str:
        service = self.service
        if self.release_digest is not None:
            preview = service.download_preview(model_content_sha256=self.release_digest)
            return service.start_download(
                actor="operator",
                request_key=str(uuid4()),
                plan_digest=str(preview["plan_digest"]),
                model_content_sha256=self.release_digest,
            ).id
        artifacts = [_hf_artifact(self.provider.data)]
        model = str(artifacts[0]["model_content_sha256"])
        preview = service.download_preview(
            model_content_sha256=model, artifacts=artifacts
        )
        return service.start_download(
            actor="operator",
            request_key=str(uuid4()),
            plan_digest=str(preview["plan_digest"]),
            model_content_sha256=model,
            artifacts=artifacts,
        ).id

    def run(self) -> None:
        """One worker pass that honours every backoff and cooldown."""

        for operation_id, kind in self.service._claim_operations(
            limit=4, respect_backoff=True
        ):
            self.service._run_download(operation_id, force=kind == "repair")

    def retries(self, operation_id: str) -> int:
        with self.sessions() as session:
            row = session.get(ModelCacheOperation, operation_id)
            assert row is not None
            return self.service._lifecycle.lifecycle(row, self.now[0]).retry_count

    def assert_waiting(self, operation_id: str) -> datetime:
        """A typed, visible provider wait; returns its next check."""

        view = self.service.get_operation(operation_id)
        assert view.state in WAITING, (view.state, view.last_error)
        assert view.failure is not None
        assert view.failure["code"] == TransientReason.RATE_LIMITED
        assert view.wait is not None
        assert view.wait.reason == TransientReason.RATE_LIMITED
        assert self.retries(operation_id) == 0  # a provider wait spends none
        return view.wait.next_check

    def close(self) -> None:
        self.service.close()
        self.client.close()


def _retry_after(seconds: int) -> Callable[[], httpx2.Response]:
    return lambda: httpx2.Response(429, headers={"Retry-After": str(seconds)})


def _reset_at(at: datetime) -> Callable[[], httpx2.Response]:
    return lambda: httpx2.Response(
        429,
        headers={
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(int(at.timestamp())),
        },
    )


PRODUCTION_KINDS = sorted(kind for kind, row in PROVIDERS.items() if row.production)


def test_every_production_provider_is_proven_here() -> None:
    """Guard: a new production kind must join the provider-wait proofs below."""

    assert PRODUCTION_KINDS == sorted(
        [ProviderKind.GITHUB_RELEASE_ASSET, ProviderKind.HUGGINGFACE_FILE]
    )


@pytest.mark.parametrize("kind", PRODUCTION_KINDS)
@pytest.mark.parametrize("hint", ["retry-after", "reset"])
def test_thirty_hour_rate_limit_waits_past_the_recovery_budget_then_heals(
    tmp_path: Path, kind: str, hint: str
) -> None:
    """Fault injection: the provider says "later" for 30 h, past the 24 h budget.

    On the base branch a GitHub release download ends failed at the budget (no
    cooldown is recorded for it, so its retry is clipped to the deadline) and a
    Hugging Face download fails the moment its cooldown lifts (the stall window
    has run out under it).
    """

    engine, sessions = _sqlite_sessions()
    now = [NOW]
    limit = (
        _retry_after(THIRTY_HOURS)
        if hint == "retry-after"
        else _reset_at(NOW + timedelta(seconds=THIRTY_HOURS))
    )
    provider = _Provider(kind, b"weights after a long provider wait", limit)
    harness = _Harness(sessions, tmp_path / "cache", provider, now)
    try:
        first = harness.start()
        harness.run()
        due = harness.assert_waiting(first)
        assert due >= NOW + timedelta(seconds=THIRTY_HOURS)
        calls = provider.calls

        now[0] = NOW + RECOVERY_BUDGET + timedelta(hours=1)
        harness.run()
        assert harness.assert_waiting(first) == due
        assert provider.calls == calls  # nothing re-checks before the hint

        fresh = harness.start()
        harness.run()
        assert harness.assert_waiting(fresh) >= NOW + timedelta(seconds=THIRTY_HOURS)
        assert provider.calls == calls  # the fresh request waits too

        provider.limited = False
        now[0] = NOW + timedelta(seconds=THIRTY_HOURS + 60)
        harness.run()
        for operation_id in (first, fresh):
            view = harness.service.get_operation(operation_id)
            assert view.state == LifecycleState.SUCCEEDED, view.last_error
        spec = harness.service._object_path(hashlib.sha256(provider.data).hexdigest())
        assert spec.read_bytes() == provider.data
    finally:
        harness.close()
        engine.dispose()


@pytest.mark.parametrize("kind", PRODUCTION_KINDS)
def test_hintless_rate_limit_rechecks_on_the_provider_wait_cadence(
    tmp_path: Path, kind: str
) -> None:
    """A "later" without a hint is re-checked 5 min doubling to 1 h, for days."""

    engine, sessions = _sqlite_sessions()
    now = [NOW]
    provider = _Provider(
        kind, b"weights after a hintless wait", lambda: httpx2.Response(429)
    )
    harness = _Harness(sessions, tmp_path / "cache", provider, now)
    try:
        operation = harness.start()
        harness.run()
        checks: list[datetime] = [now[0]]
        while now[0] < NOW + RECOVERY_BUDGET + timedelta(hours=1):
            due = harness.assert_waiting(operation)
            now[0] = due
            harness.run()
            checks.append(now[0])
        harness.assert_waiting(operation)
        gaps = [later - earlier for earlier, later in pairwise(checks)]
        assert min(gaps) >= timedelta(minutes=4)  # never the 60 s core backoff
        assert max(gaps) <= timedelta(hours=1)  # never silent for long
        assert gaps[-1] >= timedelta(minutes=50)  # settled on the hourly cadence
        assert provider.calls == len(checks)  # one request per check

        provider.limited = False
        now[0] = harness.assert_waiting(operation)
        harness.run()
        assert (
            harness.service.get_operation(operation).state == LifecycleState.SUCCEEDED
        )
    finally:
        harness.close()
        engine.dispose()


def test_provider_wait_survives_a_restart_on_postgres(
    tmp_path: Path, postgres_engine
) -> None:
    """Real PostgreSQL: the wait and the provider's cooldown are rebuilt at start-up."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    now = [NOW]
    provider = _Provider(
        ProviderKind.GITHUB_RELEASE_ASSET,
        b"weights after a restart",
        _retry_after(THIRTY_HOURS),
    )
    harness = _Harness(sessions, tmp_path / "cache", provider, now)
    try:
        first = harness.start()
        harness.run()
        due = harness.assert_waiting(first)
        calls = provider.calls

        harness.restart()
        now[0] = NOW + RECOVERY_BUDGET + timedelta(hours=1)
        fresh = harness.start()
        harness.run()
        assert harness.assert_waiting(first) == due
        assert harness.assert_waiting(fresh) >= NOW + timedelta(seconds=THIRTY_HOURS)
        assert provider.calls == calls  # the rebuilt cooldown holds the fresh one

        provider.limited = False
        now[0] = NOW + timedelta(seconds=THIRTY_HOURS + 60)
        harness.run()
        for operation_id in (first, fresh):
            view = harness.service.get_operation(operation_id)
            assert view.state == LifecycleState.SUCCEEDED, view.last_error
    finally:
        harness.close()


@pytest.mark.parametrize("kind", sorted(PROVIDERS))
def test_every_provider_kind_owns_its_own_cooldown(
    tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard over the provider table: a cooldown holds its own provider only.

    Every kind is opened through the one provider opener: while a kind's
    cooldown runs that provider is never contacted, and every other provider
    still is.
    """

    opened: list[str] = []
    for name, row in PROVIDERS.items():

        def record(cache, client, spec, headers, name=name):
            opened.append(name)
            return httpx2.Response(200)

        monkeypatch.setitem(PROVIDERS, name, replace(row, open=record))
    engine, sessions = _sqlite_sessions()
    service = ModelCacheService(
        sessions, tmp_path / "cache", reserve_bytes=0, clock=lambda: NOW
    )
    client = httpx2.Client()
    try:
        service._record_provider_cooldown(kind, NOW + timedelta(hours=2))
        for other in sorted(PROVIDERS):
            spec = cast("ArtifactSpec", SimpleNamespace(kind=other))
            if other == kind:
                with pytest.raises(ModelCacheStorageUnknown) as refused:
                    service._open_provider(client, spec, {})
                assert refused.value.code == ModelCacheCode.RATE_LIMITED
                assert refused.value.retry_after_seconds == 2 * 60 * 60
            else:
                assert service._open_provider(client, spec, {}).status_code == 200
        assert sorted(opened) == sorted(set(PROVIDERS) - {kind})
    finally:
        client.close()
        service.close()
        engine.dispose()
