"""GitHub LFS model sources: fetched by oid through the official batch API.

Every test runs the production cache (no fixture sources) against the faithful
fake in ``github_lfs_fake.py``; nothing reaches GitHub.  The client injects
credentials and cookies, and the fake asserts that no request carries them.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_agent_protocol import LifecycleState, ModelCacheCode
from vonk_agent_protocol.http_failure import TransientReason
from vonk_control.lifecycle.model_cache import RECOVERY_BUDGET
from vonk_control.model_cache import ModelCacheService, github_lfs
from vonk_control.model_cache.artifacts import ArtifactSpec
from vonk_control.model_cache.constants import _SOURCE_GONE_ATTEMPTS, ProviderKind
from vonk_control.model_cache.errors import (
    ModelCacheStorageInvalid,
    ModelCacheStorageUnknown,
)
from vonk_control.model_cache.provider_contracts import _GitHubLfsBatch
from vonk_control.model_cache_contract import ModelCacheDownloadPayload
from vonk_control.models import Base, ModelCacheOperation
from vonk_control.operation_contract import AvailabilityRecoveryAction

from .github_lfs_fake import (
    OBJECT_HOST,
    OWNER,
    REPO,
    REVISION,
    VECTOR,
    GithubLfsFake,
    lfs_model,
    quota_answer,
    rate_limited_until,
    repository_gone,
)
from .test_model_cache_github_release import NOW, _client, _insert_model

DATA = b"verified MotionBricks checkpoint bytes " * 64
PATH = "motionbricks/out/G1-clip.ckpt"
MEDIA_URL = (
    f"https://media.githubusercontent.com/media/{OWNER}/{REPO}/{REVISION}/{PATH}"
)
WAITING = {LifecycleState.QUEUED, LifecycleState.BACKOFF}


class _Nas:
    """A production NAS cache, its catalog and the fake GitHub, on one clock."""

    def __init__(self, root: Path, data: bytes = DATA) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.now = [NOW]
        self.root = root
        self.github = GithubLfsFake(data, lambda: self.now[0])
        self.client = _client(self.github.handler)
        self.digest, _ = _insert_model(self.sessions, lfs_model(data))
        self.service = self._build()

    def _build(self) -> ModelCacheService:
        service = ModelCacheService(
            self.sessions,
            self.root,
            free_fraction=0,
            http_client=self.client,
            fixture_sources=False,
            clock=lambda: self.now[0],
        )
        service.resume_operations()  # the production start-up hook
        return service

    def restart(self) -> None:
        self.service.close()
        self.service = self._build()

    def start(self):
        """Admit a fresh download request; returns its accepted operation."""

        preview = self.service.download_preview(model_content_sha256=self.digest)
        assert preview["blockers"] == []
        return self.service.start_download(
            actor="operator",
            request_key=str(uuid4()),
            plan_digest=str(preview["plan_digest"]),
            model_content_sha256=self.digest,
        )

    def run(self) -> None:
        """One worker pass that honours every backoff and cooldown."""

        for operation_id, kind in self.service._claim_operations(
            limit=4, respect_backoff=True
        ):
            self.service._run_download(operation_id, force=kind == "repair")

    def view(self, operation_id: str):
        return self.service.get_operation(operation_id)

    def retries(self, operation_id: str) -> int:
        with self.sessions() as session:
            row = session.get(ModelCacheOperation, operation_id)
            assert row is not None
            return self.service._lifecycle.lifecycle(row, self.now[0]).retry_count

    def next_check(self, operation_id: str) -> datetime:
        view = self.view(operation_id)
        assert view.state in WAITING, (view.state, view.last_error)
        assert view.wait is not None
        return view.wait.next_check

    def run_until_settled(self, operation_id: str, passes: int = 12):
        for _ in range(passes):
            view = self.view(operation_id)
            if view.state in {LifecycleState.SUCCEEDED, LifecycleState.FAILED}:
                return view
            if view.wait is not None:
                self.now[0] = max(self.now[0], view.wait.next_check)
            self.run()
        return self.view(operation_id)

    def placed(self) -> bytes | None:
        path = self.service._object_path(self.github.oid)
        return path.read_bytes() if path.is_file() else None

    def stored_text(self) -> str:
        """Every row the cache persisted, in every table."""

        with self.engine.connect() as connection:
            return json.dumps(
                [
                    [list(row) for row in connection.execute(table.select())]
                    for table in Base.metadata.sorted_tables
                ],
                default=str,
            )

    def close(self) -> None:
        self.service.close()
        self.client.close()
        self.engine.dispose()


@pytest.fixture
def nas(tmp_path: Path):
    harness = _Nas(tmp_path / "nas-cache")
    yield harness
    harness.close()


def _assert_no_link_secret(nas: _Nas, caplog: pytest.LogCaptureFixture) -> None:
    secrets = [
        value
        for request in nas.github.link_requests
        for key, value in request.url.params.items()
        if key in {"token", "X-Amz-Signature"}
    ]
    assert secrets
    durable = nas.stored_text()
    logged = caplog.text
    for secret in secrets:
        assert secret not in durable
        assert secret not in logged


def test_recorded_batch_vector_reads_through_the_cache_contract() -> None:
    batch = _GitHubLfsBatch.model_validate(VECTOR["body"])
    found, missing = batch.objects
    assert found.actions is not None and found.actions.download is not None
    assert found.actions.download.href.startswith(f"https://{OBJECT_HOST}/")
    assert found.actions.download.expires_in == 3600
    assert missing.error is not None and missing.error.code == 404
    assert VECTOR["headers"]["content-type"].startswith("application/json")


def test_lfs_model_downloads_anonymously_by_oid_and_is_reused_offline(
    nas: _Nas, caplog: pytest.LogCaptureFixture
) -> None:
    """The shared example becomes an LFS object placed by digest; a stable,
    secret-free locator is persisted; offline reuse needs no GitHub."""

    caplog.set_level(logging.DEBUG, logger="vonk_control")
    [spec] = nas.service.resolve_artifact_set(model_content_sha256=nas.digest).artifacts
    assert spec.kind == ProviderKind.GITHUB_LFS_OBJECT
    assert spec.source == MEDIA_URL
    assert spec.revision == REVISION

    operation = nas.start().id
    nas.run()
    assert nas.view(operation).state == LifecycleState.SUCCEEDED
    assert nas.placed() == DATA
    assert nas.github.batch_calls == 1
    assert len(nas.github.link_requests) == 1
    assert MEDIA_URL in nas.stored_text()
    _assert_no_link_secret(nas, caplog)

    offline = nas.start().id
    calls = len(nas.github.requests)
    nas.run()
    assert nas.view(offline).state == LifecycleState.SUCCEEDED
    assert len(nas.github.requests) == calls


def test_a_tampered_persisted_locator_is_refused_before_any_request(
    nas: _Nas,
) -> None:
    [spec] = nas.service.resolve_artifact_set(model_content_sha256=nas.digest).artifacts
    for source in (
        MEDIA_URL.replace(PATH, "motionbricks/out/other.ckpt"),
        MEDIA_URL.replace("media.githubusercontent.com", "evil.example"),
    ):
        with pytest.raises(ModelCacheStorageInvalid) as refused:
            nas.service._open_provider(nas.client, replace(spec, source=source), {})
        assert refused.value.code == ModelCacheCode.SOURCE_INVALID
    assert nas.github.requests == []


def test_a_link_past_its_lifetime_is_resolved_again_and_the_transfer_resumes(
    nas: _Nas, caplog: pytest.LogCaptureFixture
) -> None:
    """Fault injection: the transfer breaks, an hour passes, it resumes."""

    caplog.set_level(logging.DEBUG, logger="vonk_control")
    nas.github.truncate_next_to = 100
    operation = nas.start().id
    nas.run()
    assert nas.view(operation).state in WAITING
    assert nas.github.batch_calls == 1

    nas.now[0] += timedelta(seconds=3601)
    nas.run()
    assert nas.view(operation).state == LifecycleState.SUCCEEDED
    assert nas.github.batch_calls == 2
    assert nas.github.link_requests[-1].headers["range"] == "bytes=100-"
    assert nas.placed() == DATA
    _assert_no_link_secret(nas, caplog)


def test_a_link_refused_before_its_expiry_is_resolved_again_exactly_once(
    nas: _Nas, caplog: pytest.LogCaptureFixture
) -> None:
    """Fault injection: object storage refuses a link the cache still holds
    (revoked or expired early under clock skew): one new batch call, then the
    transfer resumes from its retained bytes."""

    caplog.set_level(logging.DEBUG, logger="vonk_control")
    nas.github.truncate_next_to = 100
    operation = nas.start().id
    nas.run()
    assert nas.github.batch_calls == 1

    nas.now[0] = nas.next_check(operation)
    nas.github.revoke_links()
    nas.run()
    assert nas.view(operation).state == LifecycleState.SUCCEEDED
    assert nas.github.batch_calls == 2
    ranges = [request.headers.get("range") for request in nas.github.link_requests]
    assert ranges == [None, "bytes=100-", "bytes=100-"]
    assert nas.placed() == DATA
    _assert_no_link_secret(nas, caplog)


def test_a_link_that_stays_refused_is_observed_not_looped(nas: _Nas) -> None:
    nas.github.link_status = 403
    operation = nas.start().id
    nas.run()
    view = nas.view(operation)
    assert view.state in WAITING
    assert nas.github.batch_calls == 2  # the link, and its one renewal
    assert len(nas.github.link_requests) == 2
    nas.github.link_status = None
    assert nas.run_until_settled(operation).state == LifecycleState.SUCCEEDED


def test_parallel_range_workers_share_one_batch_call(
    nas: _Nas, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P17: one batch call per object per link lifetime, not one per range."""

    monkeypatch.setattr(
        "vonk_control.model_cache.constants._PARALLEL_RANGE_MIN_BYTES", 1
    )
    operation = nas.start().id
    nas.run()
    assert nas.view(operation).state == LifecycleState.SUCCEEDED
    assert nas.placed() == DATA
    ranges = sorted(request.headers["range"] for request in nas.github.link_requests)
    assert len(ranges) == 4 and len(set(ranges)) == 4
    assert nas.github.batch_calls == 1


@pytest.mark.parametrize(
    "answer,earliest",
    [
        pytest.param(
            lambda: rate_limited_until(NOW + timedelta(seconds=1800)),
            lambda now: NOW + timedelta(seconds=1800),  # the provider's reset
            id="429",
        ),
        pytest.param(
            lambda: quota_answer(403),
            lambda now: now + timedelta(minutes=4),  # the provider-wait cadence
            id="403-quota",
        ),
    ],
)
def test_parallel_range_workers_share_a_refused_batch_call(
    nas: _Nas, monkeypatch: pytest.MonkeyPatch, answer, earliest
) -> None:
    """P3/P17: a refused resolve is the transfer's typed provider wait, and the
    other range workers never ask GitHub: each check makes one batch call."""

    monkeypatch.setattr(
        "vonk_control.model_cache.constants._PARALLEL_RANGE_MIN_BYTES", 1
    )
    nas.github.batch_answer = answer()
    operation = nas.start().id
    nas.run()
    for _check in range(4):
        view = nas.view(operation)
        assert view.state in WAITING, view.last_error
        assert view.failure is not None
        assert view.failure["code"] == TransientReason.RATE_LIMITED, view.failure
        assert view.wait is not None
        assert view.wait.next_check >= earliest(nas.now[0])
        assert nas.github.batch_calls == _check + 1  # one per check
        nas.now[0] = nas.next_check(operation)
        nas.run()
    nas.github.batch_answer = None
    nas.now[0] = nas.next_check(operation)
    nas.run()
    assert nas.view(operation).state == LifecycleState.SUCCEEDED
    assert nas.placed() == DATA


@pytest.mark.parametrize("refused", [False, True], ids=["answered", "refused"])
def test_concurrent_resolves_of_one_object_make_one_batch_call(
    refused: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two transfers resolving one object at once: one batch call, and the
    peer gets its outcome, whether a link or the typed refusal."""

    peer_waiting = threading.Event()

    class _WatchedEvent(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            peer_waiting.set()
            return super().wait(timeout)

    class _WatchedFlight(github_lfs._Flight):
        def __init__(self) -> None:
            super().__init__()
            self.done = _WatchedEvent()

    monkeypatch.setattr(github_lfs, "_Flight", _WatchedFlight)
    links = github_lfs._LfsLinks()
    resolves: list[int] = []
    refusal = ModelCacheStorageUnknown(ModelCacheCode.RATE_LIMITED, "later")
    link = github_lfs._LfsLink("https://object.example/x", (), NOW + timedelta(hours=1))

    def resolve() -> github_lfs._LfsLink:
        resolves.append(1)
        assert peer_waiting.wait(timeout=30)  # the peer waits on this flight
        if refused:
            raise refusal
        return link

    outcomes: list[object] = []

    def transfer() -> None:
        try:
            outcomes.append(links.get(("o/r", "a" * 64), lambda: NOW, resolve))
        except ModelCacheStorageUnknown as error:
            outcomes.append(error)

    threads = [threading.Thread(target=transfer) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert len(resolves) == 1
    if not refused:
        assert outcomes == [link, link]
        return
    assert refusal in outcomes  # the flight's own request got the answer
    (shared,) = [outcome for outcome in outcomes if outcome is not refusal]
    assert isinstance(shared, ModelCacheStorageUnknown)
    assert shared.code == ModelCacheCode.RATE_LIMITED  # the same typed wait
    assert shared.__cause__ is refusal


def test_one_shared_batch_429_halves_the_streams_once(
    nas: _Nas, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three requests wait on one refused resolve: the provider answered once,
    so the stream limit halves once and one cooldown is recorded."""

    operation = nas.start().id  # admitted, not run: no link, no cooldown
    with nas.sessions() as session:
        row = session.get(ModelCacheOperation, operation)
        assert row is not None
        payload = nas.service._payload_or_none(row)
    assert isinstance(payload, ModelCacheDownloadPayload)
    spec = ArtifactSpec._from_wire(payload.manifest.artifacts[0])
    waiting = threading.Semaphore(0)

    class _WatchedEvent(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            waiting.release()
            return super().wait(timeout)

    class _WatchedFlight(github_lfs._Flight):
        def __init__(self) -> None:
            super().__init__()
            self.done = _WatchedEvent()

    monkeypatch.setattr(github_lfs, "_Flight", _WatchedFlight)
    real_resolve = nas.service._resolve_github_lfs_link

    def resolve_once_peers_wait(*args):
        for _peer in range(2):
            assert waiting.acquire(timeout=30)  # both peers wait on this flight
        return real_resolve(*args)

    monkeypatch.setattr(
        nas.service, "_resolve_github_lfs_link", resolve_once_peers_wait
    )
    nas.github.batch_answer = rate_limited_until(NOW + timedelta(seconds=1800))
    width = nas.service._streams.status().limit
    codes: list[str] = []

    def request() -> None:
        try:
            nas.service._open_provider(nas.client, spec, {})
        except ModelCacheStorageUnknown as error:
            codes.append(error.code)

    threads = [threading.Thread(target=request) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert codes == [ModelCacheCode.RATE_LIMITED] * 3
    assert nas.github.batch_calls == 1
    assert nas.service._streams.status().limit == width // 2
    cooldown = nas.service._provider_cooldown([ProviderKind.GITHUB_LFS_OBJECT])
    assert cooldown == NOW + timedelta(seconds=1800)


def test_a_rate_limit_is_the_providers_wait_and_holds_fresh_requests(
    nas: _Nas,
) -> None:
    """Fault injection: 429 with a reset half an hour out."""

    reset = NOW + timedelta(seconds=1800)
    nas.github.batch_answer = rate_limited_until(reset)
    first = nas.start().id
    nas.run()
    view = nas.view(first)
    assert view.state in WAITING
    assert view.failure is not None
    assert view.failure["code"] == TransientReason.RATE_LIMITED
    assert nas.retries(first) == 0  # a provider wait spends no retry
    assert nas.next_check(first) >= reset
    cooldown = nas.service._provider_cooldown([ProviderKind.GITHUB_LFS_OBJECT])
    assert cooldown is not None and cooldown >= reset
    calls = nas.github.batch_calls

    fresh = nas.start().id
    nas.run()
    assert nas.next_check(fresh) >= reset
    assert nas.github.batch_calls == calls  # the fresh request waits too

    nas.github.batch_answer = None
    nas.now[0] = reset + timedelta(seconds=60)
    nas.run()
    for operation in (first, fresh):
        assert nas.run_until_settled(operation).state == LifecycleState.SUCCEEDED
    assert nas.placed() == DATA


def test_a_resolve_after_a_recorded_refusal_makes_no_batch_call(nas: _Nas) -> None:
    """A transfer that passed the opener before a peer's refusal was recorded
    reaches the resolve after it: it refuses with the typed wait, no batch call."""

    nas.github.batch_answer = rate_limited_until(NOW + timedelta(seconds=1800))
    operation = nas.start().id
    nas.run()
    assert nas.github.batch_calls == 1
    with nas.sessions() as session:
        row = session.get(ModelCacheOperation, operation)
        assert row is not None
        payload = nas.service._payload_or_none(row)
    assert isinstance(payload, ModelCacheDownloadPayload)
    spec = ArtifactSpec._from_wire(payload.manifest.artifacts[0])
    with pytest.raises(ModelCacheStorageUnknown) as refused:
        nas.service._open_github_lfs_object(nas.client, spec, {})
    assert refused.value.code == ModelCacheCode.RATE_LIMITED
    assert refused.value.retry_after_seconds == 1800
    assert nas.github.batch_calls == 1


@pytest.mark.parametrize("status", [403, 509])
def test_an_exhausted_bandwidth_quota_waits_for_days_then_heals(
    nas: _Nas, status: int
) -> None:
    """Fault injection: the repository owner's LFS quota is spent (GitHub names
    no status; both candidates behave alike). The rate-limit reset on the reply
    is not the quota's, so re-checks follow the provider-wait cadence."""

    nas.github.batch_answer = quota_answer(status)
    operation = nas.start().id
    nas.run()
    checks = [nas.now[0]]
    while nas.now[0] < NOW + RECOVERY_BUDGET + timedelta(hours=1):
        view = nas.view(operation)
        assert view.state in WAITING, view.last_error
        assert view.failure is not None
        assert view.failure["code"] == TransientReason.RATE_LIMITED
        assert nas.retries(operation) == 0
        nas.now[0] = nas.next_check(operation)
        nas.run()
        checks.append(nas.now[0])
    assert nas.view(operation).state in WAITING
    gaps = [later - earlier for earlier, later in pairwise(checks)]
    assert gaps[0] <= timedelta(minutes=10)  # not the rate-limit window's reset
    assert min(gaps) >= timedelta(minutes=4)
    assert max(gaps) <= timedelta(hours=1)
    assert nas.github.batch_calls == len(checks)  # one batch call per check

    nas.github.batch_answer = None
    nas.now[0] = nas.next_check(operation)
    nas.run()
    assert nas.view(operation).state == LifecycleState.SUCCEEDED
    assert nas.placed() == DATA


def test_a_restart_resumes_the_object_with_a_range_and_a_new_link(
    nas: _Nas,
) -> None:
    """Links live in process memory only: a new process resolves again and
    resumes the retained bytes."""

    nas.github.truncate_next_to = 300
    operation = nas.start().id
    nas.run()
    assert nas.github.batch_calls == 1
    nas.restart()
    assert nas.run_until_settled(operation).state == LifecycleState.SUCCEEDED
    assert nas.github.batch_calls == 2
    assert nas.github.link_requests[-1].headers["range"] == "bytes=300-"
    assert nas.placed() == DATA


@pytest.mark.parametrize("gone", ["object-404", "repository-401"])
def test_a_missing_object_or_repository_ends_as_source_gone_then_heals(
    nas: _Nas, gone: str
) -> None:
    if gone == "object-404":
        nas.github.object_error = 404
    else:
        nas.github.batch_answer = repository_gone
    operation = nas.start().id
    nas.run()
    ended = nas.run_until_settled(operation)
    assert ended.state == LifecycleState.FAILED
    assert ended.failure is not None
    assert ended.failure["code"] == ModelCacheCode.SOURCE_GONE
    assert ended.failure["recovery_actions"] == [
        AvailabilityRecoveryAction.DOWNLOAD_AGAIN
    ]
    assert nas.github.batch_calls == _SOURCE_GONE_ATTEMPTS  # one per observation
    assert nas.github.link_requests == []

    # The source heals: a fresh request is admitted and completes.
    nas.github.object_error = None
    nas.github.batch_answer = None
    fresh = nas.start()
    assert fresh.id != ended.id
    nas.service.run_pending()
    assert nas.view(fresh.id).state == LifecycleState.SUCCEEDED
    assert nas.placed() == DATA


def test_a_link_off_the_object_storage_host_is_refused_before_any_request(
    nas: _Nas,
) -> None:
    """Security edge: the batch reply cannot send the NAS anywhere else."""

    nas.github.link_host = "evil.example"
    operation = nas.start().id
    nas.run()
    view = nas.run_until_settled(operation)
    assert view.state == LifecycleState.FAILED
    assert view.failure is not None
    assert view.failure["code"] == ModelCacheCode.SOURCE_UNTRUSTED
    assert nas.github.batch_calls == 1  # a refusal, not a retry
    assert nas.github.requests_to("evil.example") == []
    assert nas.placed() is None


def test_a_link_that_redirects_is_refused(nas: _Nas) -> None:
    nas.github.link_status = 302
    operation = nas.start().id
    nas.run()
    view = nas.run_until_settled(operation)
    assert view.state == LifecycleState.FAILED
    assert view.failure is not None
    assert view.failure["code"] == ModelCacheCode.REDIRECT_FORBIDDEN
    assert nas.github.requests_to("elsewhere.example") == []
    assert nas.placed() is None


def test_bytes_that_do_not_match_the_oid_are_refused_at_ingress(nas: _Nas) -> None:
    nas.github.served = bytes(reversed(DATA))
    operation = nas.start().id
    nas.run()
    assert nas.view(operation).state in WAITING
    assert nas.placed() is None
    nas.github.served = None
    nas.now[0] = nas.next_check(operation)
    nas.run()
    assert nas.view(operation).state == LifecycleState.SUCCEEDED
    assert nas.placed() == DATA
    # The retry fetched the object again from byte zero.
    assert [request.headers.get("range") for request in nas.github.link_requests] == [
        None,
        None,
    ]
