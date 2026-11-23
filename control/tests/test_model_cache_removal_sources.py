"""Model cache: removal sources."""

from __future__ import annotations

import hashlib
import itertools
import shutil
import uuid
from concurrent.futures import Future, wait
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from vonk_forge_contracts import document_sha256

from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
from vonk_agent_protocol.reason_codes.model_cache import ModelCacheCode
from vonk_control.auth import Actor
from vonk_control.catalog.entities import CatalogEntityService
from vonk_control.model_cache import removal_execution as _removal_execution
from vonk_control.model_cache.api import (
    install_model_operator_routes,
)
from vonk_control.model_cache.artifacts import ArtifactSpec
from vonk_control.model_cache.constants import _SOURCE_GONE_ATTEMPTS
from vonk_control.model_cache.contract import (
    ModelCacheRemovalResult,
)
from vonk_control.model_cache.errors import (
    ModelCacheConflict,
    ModelCacheNotFoundInvalid,
    ModelCacheSelectorAmbiguous,
)
from vonk_control.model_cache.service import ModelCacheService
from vonk_control.models.cache import (
    ModelCacheOperation,
    ModelCacheSet,
)
from vonk_control.models.catalog import (
    CatalogDocumentHead,
)

from .display_fixtures import create_draft
from .insert_collision_support import Rendezvous
from .model_cache_support import (
    NOW,
    _admit_fresh_download,
    _artifact,
    _canonical_model,
    _download,
    _first_manifest_artifact,
    _gone_handler,
    _http_artifact,
    _http_cache_service,
    _manifest_document,
    _required_text,
)
from .non_blocking import assert_ended_without_blocking
from .removal_settle import advance_until_ended, release_ended_gates
from .test_model_cache_recovery_support import observe_unknown


def test_model_removal_waits_for_in_use_sets_then_completes(
    cache, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, sessions = cache
    model = "e" * 64
    downloaded = _download(
        service,
        [_artifact(tmp_path, b"in use weights", model_content_sha256=model)],
        model_content_sha256=model,
        request_key="00000000-0000-4000-8000-000000001040",
    )
    assert downloaded.state == "succeeded"
    set_digest = str(downloaded.artifact_set_sha256)
    owners = {set_digest: ("installation 1",)}

    real_reasons = _removal_execution._reason_projection

    def reasons(findings):
        found = real_reasons(findings)
        return {
            digest: tuple(found.get(digest, ())) + owners.get(digest, ())
            for digest in findings
        }

    monkeypatch.setattr(
        "vonk_control.model_cache.removal_execution._reason_projection",
        reasons,
    )
    removal = service.remove_model_selector(
        model, actor="operator", request_key="00000000-0000-4000-8000-000000001041"
    )
    for _ in range(3):
        service.advance_removals(limit=10)
        with sessions.begin() as session:
            row = session.get(ModelCacheOperation, removal.id)
            assert row is not None
            row.next_action_at = None  # the one retry clock: due now
    waiting = service.get_operation(removal.id)
    assert waiting.state in {"queued", LifecycleState.BACKOFF}
    assert waiting.failure is not None
    assert waiting.failure["retryable"] is True
    with sessions() as session:
        assert session.get(ModelCacheSet, set_digest) is not None

    owners.clear()
    advance_until_ended(service, removal.id, rounds=10)
    assert service.get_operation(removal.id).state == "succeeded"
    with sessions() as session:
        assert session.get(ModelCacheSet, set_digest) is None

    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        removal,
        end=lambda receipt: service.get_operation(receipt.id),
        fresh=lambda _world: _admit_fresh_download(
            service,
            model,
            _artifact(tmp_path, b"in use weights", model_content_sha256=model),
        ),
    )


def _seed_removal(service, tmp_path: Path, model: str, key: str):
    downloaded = _download(
        service,
        [_artifact(tmp_path, b"long wait weights", model_content_sha256=model)],
        model_content_sha256=model,
        request_key=key,
    )
    assert downloaded.state == "succeeded"
    return str(downloaded.artifact_set_sha256)


def _advance_on_fake_clock(
    service, sessions, removal_id: str, clock: list[datetime], rounds: int
):
    for _ in range(rounds):
        clock[0] += timedelta(hours=1)
        service.advance_removals(limit=10)
        with sessions.begin() as session:
            row = session.get(ModelCacheOperation, removal_id)
            assert row is not None
            row.next_action_at = None
        if service.get_operation(removal_id).state in {"succeeded", "failed"}:
            break


def test_removal_waiting_past_the_old_lifetime_cap_still_succeeds(
    cache, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, sessions = cache
    set_digest = _seed_removal(
        service, tmp_path, "f" * 64, "00000000-0000-4000-8000-000000001050"
    )
    owners = {set_digest: ("installation 1",)}

    real_reasons = _removal_execution._reason_projection
    monkeypatch.setattr(
        "vonk_control.model_cache.removal_execution._reason_projection",
        lambda findings: {
            digest: tuple(real_reasons(findings).get(digest, ()))
            + owners.get(digest, ())
            for digest in findings
        },
    )
    removal = service.remove_model_selector(
        "f" * 64, actor="operator", request_key="00000000-0000-4000-8000-000000001051"
    )
    clock = [service._clock()]
    service._clock = lambda: clock[0]
    # Hours of waiting: far beyond the old created_at + max_failures * max_delay.
    _advance_on_fake_clock(service, sessions, removal.id, clock, rounds=6)
    assert service.get_operation(removal.id).state not in {"succeeded", "failed"}
    owners.clear()
    _advance_on_fake_clock(service, sessions, removal.id, clock, rounds=10)
    assert service.get_operation(removal.id).state == "succeeded"

    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        removal,
        end=lambda receipt: service.get_operation(receipt.id),
        fresh=lambda _world: _admit_fresh_download(
            service,
            "f" * 64,
            _artifact(tmp_path, b"long wait weights", model_content_sha256="f" * 64),
        ),
    )


def test_removal_with_a_deterministic_failure_still_fails_promptly(
    cache, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, sessions = cache
    _seed_removal(service, tmp_path, "d" * 64, "00000000-0000-4000-8000-000000001052")

    def broken(*_args, **_kwargs):
        raise ValueError("invalid exact deletion contract")

    monkeypatch.setattr(service, "_remove_model_object_files", broken)
    removal = service.remove_model_selector(
        "d" * 64, actor="operator", request_key="00000000-0000-4000-8000-000000001053"
    )
    clock = [service._clock()]
    service._clock = lambda: clock[0]
    _advance_on_fake_clock(service, sessions, removal.id, clock, rounds=20)
    ended = service.get_operation(removal.id)
    assert ended.state == LifecycleState.FAILED
    release_ended_gates(service)

    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        removal,
        end=lambda receipt: service.get_operation(receipt.id),
        assert_reason=lambda ended: ended.failure and ended.failure["code"],
        fresh=lambda _world: _admit_fresh_download(
            service,
            "d" * 64,
            _artifact(tmp_path, b"long wait weights", model_content_sha256="d" * 64),
        ),
    )
    assert "invalid exact deletion contract" in str(ended.last_error or ended.failure)


def test_model_removal_child_replay_and_visible_writer_wait(cache, tmp_path: Path):
    service, sessions = cache
    data = b"removal child bytes"
    artifact = _artifact(tmp_path, data)
    downloaded = _download(
        service,
        [artifact],
        model_content_sha256="a" * 64,
        request_key="00000000-0000-4000-8000-000000001060",
    )
    assert downloaded.state == "succeeded"
    assert downloaded.artifact_set_sha256 is not None

    def accept():
        with sessions.begin() as session:
            return service.accept_removal_for_sets_in_session(
                session,
                actor="test",
                request_key="00000000-0000-4000-8000-000000001061",
                selector="recipe-child",
                selected_sets=[downloaded.artifact_set_sha256],
            )

    accepted = accept()
    replay = accept()
    assert replay.id == accepted.id
    digest = str(artifact["sha256"])
    with service._model_storage_lock(digest):
        assert service.advance_removals() == 0
        waiting = service.get_operation(accepted.id)
        assert waiting.state == LifecycleState.BACKOFF
        assert waiting.failure is not None
        assert waiting.failure["retry_time"] is not None
        assert waiting.failure["artifact_key"] == f"object:{digest}"
        assert service._object_path(digest).read_bytes() == data
    resumed_at = service._clock() + timedelta(seconds=120)
    service._clock = lambda: resumed_at
    for _ in range(3):
        service.advance_removals()
    completed = service.get_operation(accepted.id)
    assert completed.state == "succeeded"
    assert completed.failure is None
    assert isinstance(completed.result, ModelCacheRemovalResult)
    assert completed.result.reclaimed_bytes == len(data)
    assert not service._object_path(digest).exists()


@pytest.mark.parametrize(
    "field", ["actor", "request_key", "selector", "removal_fence", "delete_objects"]
)
def test_model_removal_retires_persisted_intent_drift_before_effects(
    cache, tmp_path: Path, field: str
):
    service, sessions = cache
    artifact = _artifact(tmp_path, b"bound bytes")
    downloaded = _download(
        service,
        [artifact],
        model_content_sha256="a" * 64,
        request_key="00000000-0000-4000-8000-000000001070",
    )
    assert downloaded.artifact_set_sha256 is not None
    with sessions.begin() as session:
        accepted = service.accept_removal_for_sets_in_session(
            session,
            actor="test",
            request_key="00000000-0000-4000-8000-000000001071",
            selector="recipe-child",
            selected_sets=[downloaded.artifact_set_sha256],
        )
    # Acceptance can already resolve the exact scope; observation is idempotent.
    service._observe_model_removal_scope(accepted.id)
    with sessions.begin() as session:
        row = session.get(ModelCacheOperation, accepted.id)
        assert row is not None
        if field in {"actor", "request_key"}:
            setattr(row, field, "00000000-0000-4000-8000-000000001072")
        else:
            payload = dict(row.payload)
            payload[field] = (
                []
                if field == "delete_objects"
                else "00000000-0000-4000-8000-000000001072"
            )
            row.payload = payload
    # The drifted plan is destructive intent nobody accepted: nothing is
    # removed, the row ends (kept for inspection) and the worker carries on.
    assert service._advance_model_removal(accepted.id) is False
    assert service._object_path(str(artifact["sha256"])).read_bytes() == b"bound bytes"
    retired = service.get_operation(accepted.id)
    assert retired.state == "failed"
    assert "persisted-state-damaged" in str(retired.last_error)
    assert service.advance_removals() == 0
    fresh = _download(
        service,
        [artifact],
        model_content_sha256="a" * 64,
        request_key="00000000-0000-4000-8000-000000001073",
    )
    assert fresh.id != accepted.id
    assert fresh.state == LifecycleState.SUCCEEDED.value


def test_model_removal_resolves_the_selector_at_acceptance_and_replays_by_key(
    cache,
):
    service, sessions = cache
    model_a = _canonical_model(
        publisher="vonk-forge",
        slug="exact-removal",
        file_id="weights",
        file_digest="a" * 64,
    )
    model_b = _canonical_model(
        publisher="vonk-forge",
        slug="exact-removal",
        file_id="weights",
        file_digest="b" * 64,
    )
    digest_a = document_sha256(model_a.model_dump(mode="json"))
    catalog = CatalogEntityService(sessions, clock=lambda: NOW)
    draft = create_draft(catalog, model_a.model_dump(mode="json"), actor="operator")
    original = catalog.resolve(draft.id, actor="operator")

    request_key = "00000000-0000-4000-8000-000000001052"
    # A stale client-side identity is advisory: the selector resolves now.
    accepted = service.remove_model_selector(
        "vonk-forge/exact-removal",
        actor="operator",
        request_key=request_key,
    )
    assert accepted.model_content_sha256 == digest_a
    assert service.get_operation(accepted.id).model_content_sha256 == digest_a

    successor = catalog.revise(
        original.document_id, model_b.model_dump(mode="json"), actor="operator"
    )
    catalog.resolve(successor.id, actor="operator")

    replay = service.remove_model_selector(
        "vonk-forge/exact-removal",
        actor="operator",
        request_key=request_key,
    )
    assert replay.id == accepted.id
    assert replay.model_content_sha256 == digest_a
    with pytest.raises(ModelCacheConflict):
        service.remove_model_selector(
            "vonk-forge/other-model",
            actor="operator",
            request_key=request_key,
        )

    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        accepted,
        end=lambda receipt: _finish_cache_removal(service, receipt),
        fresh=lambda _world: service.download_model_selector(
            digest_a, actor="operator", request_key=str(uuid.uuid4())
        ),
    )


@pytest.mark.parametrize("range_supported", [True, False])
def test_model_cache_parallel_ranges_publish_or_fall_back(
    cache, tmp_path, monkeypatch, range_supported
):

    _existing, sessions = cache
    payload = bytes(range(256)) * 256
    monkeypatch.setattr(
        "vonk_control.model_cache.constants._PARALLEL_RANGE_MIN_BYTES", 1
    )
    barrier = Rendezvous(4)
    requests = []

    class AfterPeers(httpx2.SyncByteStream):
        """A range body that flows only once every peer is streaming too."""

        def __init__(self, content: bytes) -> None:
            self.content = content

        def __iter__(self):
            barrier.wait()  # fails if the range transfers serialize
            yield self.content

    def handler(request):
        requested = request.headers.get("range")
        requests.append(requested)
        if requested and range_supported:
            start, end = map(int, requested.removeprefix("bytes=").split("-"))
            return httpx2.Response(
                206,
                request=request,
                stream=AfterPeers(payload[start : end + 1]),
                headers={"content-range": f"bytes {start}-{end}/{len(payload)}"},
            )
        return httpx2.Response(200, request=request, content=payload)

    service, client = _http_cache_service(tmp_path, sessions, handler)
    artifact = _http_artifact(payload)
    try:
        operation = _download(
            service,
            [artifact],
            model_content_sha256="b" * 64,
            request_key="00000000-0000-4000-8000-000000001020",
        )
        assert operation.state == "succeeded", operation.last_error
        assert operation.progress["downloaded_bytes"] == len(payload)
        assert (
            service._object_path(
                _required_text(artifact["sha256"], "artifact digest")
            ).read_bytes()
            == payload
        )
        # An ignored range is learned from the first answer: no peer asks.
        assert len([request for request in requests if request]) == (
            4 if range_supported else 1
        )
        assert (None in requests) == (not range_supported)
        assert not list((service.root / "partials").rglob("*.range-*"))
    finally:
        service.close()
        client.close()


def test_cancel_during_verification_does_not_publish(cache, tmp_path, monkeypatch):
    service, sessions = cache
    artifact = _artifact(tmp_path, b"verified but cancelled")
    operation = service.start_download(
        actor="test",
        request_key="00000000-0000-4000-8000-000000001021",
        model_content_sha256="a" * 64,
        artifacts=[artifact],
    )
    api = ModelCacheService(
        sessions, service.root, free_fraction=0, fixture_sources=True
    )
    real_verify = service._verify_file

    def verify(path, spec):
        result = real_verify(path, spec)
        if path.suffix == ".part" and result:
            api.cancel_operation(
                operation.id,
                actor="test",
                request_key="00000000-0000-4000-8000-000000001112",
                reason="cancel during verification",
            )
        return result

    monkeypatch.setattr(service, "_verify_file", verify)
    try:
        service.run_pending()
        assert service.get_operation(operation.id).state == "cancelled"
        assert not service._object_path(artifact["sha256"]).exists()
    finally:
        api.close()


def test_range_disk_reservation_falls_back_without_source_io(
    cache, tmp_path, monkeypatch
):

    service, _sessions = cache
    spec = ArtifactSpec.from_manifest(
        _first_manifest_artifact(_manifest_document(tmp_path))
    )
    partial = service._partial_path("b" * 64, spec.sha256)
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"m")
    usage = shutil.disk_usage(tmp_path)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: type(usage)(100, 99, 1))
    monkeypatch.setattr(service, "_validate_http_download", lambda spec: None)
    assert not service._download_parallel_ranges(spec, partial, "b" * 64, "unused", 0)
    assert partial.read_bytes() == b"m"
    assert service._range_reserved_bytes == 0


@pytest.mark.parametrize("status", [404, 410])
def test_permanent_missing_source_ends_and_admits_a_fresh_download(
    cache, tmp_path: Path, status: int
) -> None:
    _existing, sessions = cache
    handler, payload, served = _gone_handler([status] * 5 + [200])
    service, client = _http_cache_service(
        tmp_path, sessions, handler, clock=lambda: NOW
    )
    try:
        artifact = _http_artifact(payload)
        operation = service.start_download(
            actor="test",
            request_key="00000000-0000-4000-8000-000000000940",
            model_content_sha256="b" * 64,
            artifacts=[artifact],
        )
        # Bounded: a 404 is first a transient wait, never a terminal on sight.
        for _ in range(4):
            service.run_pending()
            waiting = service.get_operation(operation.id)
            assert waiting.state == "queued"
            assert waiting.failure is not None
        service.run_pending()
        gone = service.get_operation(operation.id)
        assert gone.state == "failed"
        assert gone.failure is not None
        assert gone.failure["retryable"] is False
        detail = str(gone.failure["detail"])
        assert "weights.bin" in detail
        assert f"HTTP {status}" in detail
        assert served["count"] == 5
        # Nothing waits any more: no blocker, no retry clock.
        assert gone.blockers == ()
        assert gone.next_attempt_at is None
        assert service.run_pending() == 0
        fresh = _download(
            service,
            [artifact],
            model_content_sha256="b" * 64,
            request_key="00000000-0000-4000-8000-000000000945",
        )
        assert fresh.id != gone.id
        assert fresh.state == "succeeded"
        assert service.get_operation(gone.id).state == "failed"
    finally:
        service.close()
        client.close()


def test_a_transient_404_retries_and_then_succeeds(cache, tmp_path: Path) -> None:
    _existing, sessions = cache
    handler, payload, _served = _gone_handler([404, 404, 200])
    service, client = _http_cache_service(
        tmp_path, sessions, handler, clock=lambda: NOW
    )
    try:
        operation = _download(
            service,
            [_http_artifact(payload)],
            model_content_sha256="b" * 64,
            request_key="00000000-0000-4000-8000-000000000941",
        )
        assert operation.state == "queued"
        for _ in range(2):
            service.run_pending()
            operation = service.get_operation(operation.id)
        assert operation.state == "succeeded"
    finally:
        service.close()
        client.close()


def test_a_server_error_never_becomes_source_gone(cache, tmp_path: Path) -> None:
    _existing, sessions = cache
    handler, payload, served = _gone_handler([503] * 5 + [200])
    service, client = _http_cache_service(
        tmp_path, sessions, handler, clock=lambda: NOW
    )
    try:
        operation = _download(
            service,
            [_http_artifact(payload)],
            model_content_sha256="b" * 64,
            request_key="00000000-0000-4000-8000-000000000942",
        )
        # Transport retries and lifecycle retries share this request. Even
        # repeated 503s never count as evidence that the source disappeared.
        for _ in range(_SOURCE_GONE_ATTEMPTS + 3):
            service.run_pending()
            operation = service.get_operation(operation.id)
            if operation.state == LifecycleState.SUCCEEDED.value:
                break
            assert operation.failure is not None
            assert operation.retryable
        assert operation.state == LifecycleState.SUCCEEDED.value
        assert served["count"] == 6
        assert operation.failure is None
        assert operation.next_attempt_at is None
        fresh = _admit_fresh_download(service, "b" * 64, _http_artifact(payload))
        assert fresh.id != operation.id
        service.run_pending()
        assert service.get_operation(fresh.id).state == LifecycleState.SUCCEEDED.value
    finally:
        service.close()
        client.close()


def test_operation_reads_answer_without_an_operator_selector(
    cache, tmp_path: Path
) -> None:
    """A recipe preparation or a repair has no operator selector: reads still answer."""

    service, _sessions = cache
    model = "e" * 64
    artifact = _artifact(tmp_path, b"selectorless", model_content_sha256=model)
    download_key = "00000000-0000-4000-8000-000000000943"
    download = _download(
        service,
        [artifact],
        model_content_sha256=model,
        request_key=download_key,
    )
    repair = service.start_repair(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000944",
        artifact_set_sha256=download.artifact_set_sha256 or "",
    )
    actor = Actor("test", "administrator")
    app = FastAPI()
    install_model_operator_routes(
        app, actor_dependency=Depends(lambda: actor), service=service
    )
    with TestClient(app) as api:
        for operation in (download, repair):
            read = api.get(f"/api/model/operations/{operation.id}")
            assert read.status_code == 200, read.text
            body = read.json()
            assert body["operation_id"] == operation.id
            assert body["action"] == "download"
            # No catalog row here, so the content digest names the model.
            assert body["selector"] == model
            by_key = api.get(f"/api/model/requests/{operation.request_key}")
            assert by_key.status_code == 200, by_key.text
            assert by_key.json()["operation_id"] == operation.id


def test_prior_server_errors_do_not_count_as_missing_source_observations(
    cache, tmp_path
):
    _existing, sessions = cache
    handler, payload, _served = _gone_handler([500] * 5 + [404, 200])
    service, client = _http_cache_service(
        tmp_path, sessions, handler, clock=lambda: NOW
    )
    try:
        operation = _download(
            service,
            [_http_artifact(payload)],
            model_content_sha256="b" * 64,
            request_key="00000000-0000-4000-8000-000000000949",
        )
        service.run_pending()  # bounded request attempts consume the remaining 500s
        # The first 404 counts once, regardless of earlier 500 responses.
        waiting = service.get_operation(operation.id)
        assert waiting.state == "queued"
        assert waiting.failure is not None
        service.run_pending()
        assert service.get_operation(operation.id).state == "succeeded"
    finally:
        service.close()
        client.close()


def test_missing_source_observations_follow_exact_file_and_survive_restart(
    cache, tmp_path
):
    _existing, sessions = cache
    first_payload, second_payload = b"first file", b"second file"
    served = {"/first.bin": 0, "/second.bin": 0}

    def handler(request):
        served[request.url.path] += 1
        if request.url.path == "/first.bin":
            status, payload = (404 if served["/first.bin"] <= 1 else 200), first_payload
        else:
            status, payload = (
                (404 if served["/second.bin"] <= 5 else 200),
                second_payload,
            )
        return httpx2.Response(
            status, request=request, content=payload if status == 200 else b""
        )

    artifacts = []
    for name, payload in [("first", first_payload), ("second", second_payload)]:
        artifact = _http_artifact(payload)
        artifact.update(
            id=name, path=f"{name}.bin", source=f"https://example.test/{name}.bin"
        )
        artifacts.append(artifact)
    service, client = _http_cache_service(
        tmp_path, sessions, handler, clock=lambda: NOW
    )
    try:
        operation = _download(
            service,
            artifacts,
            model_content_sha256="b" * 64,
            request_key="00000000-0000-4000-8000-000000000950",
        )
        for _ in range(1):
            service.run_pending()
        assert served == {"/first.bin": 2, "/second.bin": 1}
        assert service.get_operation(operation.id).state == "queued"
        service.close()
        service = ModelCacheService(
            sessions,
            tmp_path / "http-nas-cache",
            free_fraction=0,
            http_client=client,
            fixture_sources=True,
            clock=lambda: NOW,
        )
        for _ in range(_SOURCE_GONE_ATTEMPTS - 2):
            service.run_pending()
            assert service.get_operation(operation.id).state == "queued"
        service.run_pending()
        assert served["/second.bin"] == _SOURCE_GONE_ATTEMPTS
        gone = service.get_operation(operation.id)
        assert gone.state == "failed"
        assert gone.failure is not None
        assert gone.next_attempt_at is None
        fresh = service.start_download(
            actor="test",
            request_key=str(uuid.uuid4()),
            model_content_sha256="b" * 64,
            artifacts=artifacts,
        )
        service.run_pending()
        service.run_pending()
        assert service.get_operation(fresh.id).state == LifecycleState.SUCCEEDED.value
        # The completed first file remains reusable after the request ends.
        assert served["/first.bin"] == 2
    finally:
        service.close()
        client.close()


def test_a_lost_background_failure_ack_counts_one_missing_file_observation(
    threaded_cache, tmp_path, monkeypatch
):

    _existing, sessions = threaded_cache
    handler, payload, served = _gone_handler([404] * 5)
    service, client = _http_cache_service(
        tmp_path, sessions, handler, clock=lambda: NOW
    )
    try:
        artifact = _http_artifact(payload)
        operation = service.start_download(
            actor="test",
            request_key="00000000-0000-4000-8000-000000000951",
            model_content_sha256="b" * 64,
            artifacts=[artifact],
        )
        real_finish = service._finish_failed
        lost_ack = [True]
        first_report = []

        def finish(*args, **kwargs):
            if not first_report:
                first_report.append((args, kwargs))
            real_finish(*args, **kwargs)
            if lost_ack[0]:
                lost_ack[0] = False
                raise ConnectionError("commit succeeded but its reply was lost")

        monkeypatch.setattr(service, "_finish_failed", finish)
        service.tick(limit=1)
        futures = service._background_operations[operation.id].futures
        assert isinstance(futures, list)
        running = [future for future in futures if isinstance(future, Future)]
        assert len(running) == len(futures) == 1
        assert not wait(running, timeout=30).not_done
        with observe_unknown():
            observed = service.tick(limit=1)
            assert not observed
        waiting = service.get_operation(operation.id)
        next_attempt = waiting.next_attempt_at
        assert waiting.state == "queued"
        service.tick(limit=1)  # same Future report, not another HTTP observation
        assert served["count"] == 1
        assert service.get_operation(operation.id).next_attempt_at == next_attempt
        # Even this worker's fence cannot replay the old attempt into a newly
        # claimed attempt. The fresh claim still owns its execution unchanged.
        assert service._claim_operations(limit=1, respect_backoff=False) == [
            (operation.id, "download")
        ]
        assert service._mark_running(operation.id) is not None
        args, kwargs = first_report[0]
        real_finish(*args, **kwargs)
        assert service.get_operation(operation.id).state == "running"
        assert served["count"] == 1
        service._run_download(operation.id, force=False)
        assert service.get_operation(operation.id).state == "queued"
        for _ in range(2):
            service.run_pending()
            assert service.get_operation(operation.id).state == "queued"
        service.run_pending()
        assert served["count"] == 5
        gone = service.get_operation(operation.id)
        assert gone.failure is not None
    finally:
        service.close()
        client.close()


def _finish_cache_removal(service, receipt):
    advance_until_ended(service, receipt.id, rounds=10)
    return service.get_operation(receipt.id)


def _ticking_catalog(sessions):
    """A catalog whose revisions get strictly increasing creation times."""
    ticks = itertools.count()
    return CatalogEntityService(
        sessions, clock=lambda: NOW + timedelta(seconds=next(ticks))
    )


def _cache_model_revision(
    service, tmp_path, catalog, *, publisher="vonk-forge", slug, weight, document=None
):
    """Activate one model revision (a new document, or a revision of one) and cache it."""
    model = _canonical_model(
        publisher=publisher,
        slug=slug,
        file_id="weights",
        file_digest=hashlib.sha256(weight).hexdigest(),
    )
    if document is None:
        draft = create_draft(catalog, model.model_dump(mode="json"), actor="operator")
        row = catalog.resolve(draft.id, actor="operator")
    else:
        revised = catalog.revise(
            document, model.model_dump(mode="json"), actor="operator"
        )
        row = catalog.resolve(revised.id, actor="operator")
    _download(
        service,
        [
            _artifact(
                tmp_path,
                weight,
                path="weights.safetensors",
                model_content_sha256=row.content_digest,
            )
        ],
        model_content_sha256=row.content_digest,
        request_key=str(uuid.uuid4()),
    )
    return row


def _retract_heads(sessions, slug):
    """Retract the way the catalogue sync will: no active revision, generation up."""
    with sessions.begin() as session:
        for head in session.scalars(
            select(CatalogDocumentHead).where(CatalogDocumentHead.slug == slug)
        ):
            head.active_revision_id = None
            head.generation += 1


def _retracted_lineage(service, sessions, tmp_path):
    catalog = _ticking_catalog(sessions)
    first = _cache_model_revision(
        service, tmp_path, catalog, slug="gone", weight=b"aaa"
    )
    second = _cache_model_revision(
        service,
        tmp_path,
        catalog,
        slug="gone",
        weight=b"bbb",
        document=first.document_id,
    )
    return catalog, first, second


def test_retracted_publisher_slug_resolves_to_the_newest_revision_with_a_set(
    cache, tmp_path
):
    service, sessions = cache
    _catalog, _first, second = _retracted_lineage(service, sessions, tmp_path)
    _retract_heads(sessions, "gone")
    assert service._resolve_model_selector("vonk-forge/gone") == second.content_digest
    assert service._resolve_model_selector("gone") == second.content_digest
    assert service._resolve_model_selector(second.document_id) == second.content_digest


def test_retracted_selector_ignores_a_newer_revision_without_a_set(cache, tmp_path):
    service, sessions = cache
    catalog = _ticking_catalog(sessions)
    first = _cache_model_revision(
        service, tmp_path, catalog, slug="gone", weight=b"aaa"
    )
    newer = _canonical_model(
        publisher="vonk-forge",
        slug="gone",
        file_id="weights",
        file_digest=hashlib.sha256(b"bbb").hexdigest(),
    )
    catalog.resolve(
        catalog.revise(
            first.document_id, newer.model_dump(mode="json"), actor="operator"
        ).id,
        actor="operator",
    )
    _retract_heads(sessions, "gone")
    assert service._resolve_model_selector("vonk-forge/gone") == first.content_digest


def test_retracted_selector_counts_an_incomplete_set_as_a_set(cache, tmp_path):
    service, sessions = cache
    catalog = _ticking_catalog(sessions)
    first = _cache_model_revision(
        service, tmp_path, catalog, slug="gone", weight=b"aaa"
    )
    model = _canonical_model(
        publisher="vonk-forge",
        slug="gone",
        file_id="weights",
        file_digest=hashlib.sha256(b"bbb").hexdigest(),
    )
    second = catalog.resolve(
        catalog.revise(
            first.document_id, model.model_dump(mode="json"), actor="operator"
        ).id,
        actor="operator",
    )
    _download(
        service,
        [
            _artifact(
                tmp_path,
                b"bbb",
                path="weights.safetensors",
                model_content_sha256=second.content_digest,
            )
        ],
        model_content_sha256=second.content_digest,
        request_key=str(uuid.uuid4()),
        interrupt_after_bytes=1,
    )
    with sessions() as session:
        states = set(
            session.scalars(
                select(ModelCacheSet.state).where(
                    ModelCacheSet.model_content_sha256 == second.content_digest
                )
            )
        )
    assert states
    _retract_heads(sessions, "gone")
    assert service._resolve_model_selector("vonk-forge/gone") == second.content_digest


def test_retracted_bare_slug_across_publishers_is_ambiguous(cache, tmp_path):
    service, sessions = cache
    catalog = _ticking_catalog(sessions)
    rows = [
        _cache_model_revision(
            service, tmp_path, catalog, publisher=publisher, slug="gone", weight=weight
        )
        for publisher, weight in (("first", b"aaa"), ("second", b"bbb"))
    ]
    _retract_heads(sessions, "gone")
    with pytest.raises(ModelCacheSelectorAmbiguous) as caught:
        service._resolve_model_selector("gone")
    assert caught.value.candidates == tuple(sorted(r.content_digest for r in rows))
    for publisher, row in zip(("first", "second"), rows, strict=True):
        assert (
            service._resolve_model_selector(f"{publisher}/gone") == row.content_digest
        )


def test_retracted_selector_without_any_set_is_missing(cache):
    service, sessions = cache
    catalog = _ticking_catalog(sessions)
    model = _canonical_model(
        publisher="vonk-forge",
        slug="uncached",
        file_id="weights",
        file_digest="c" * 64,
    )
    catalog.resolve(
        create_draft(catalog, model.model_dump(mode="json"), actor="operator").id,
        actor="operator",
    )
    _retract_heads(sessions, "uncached")
    with pytest.raises(ModelCacheNotFoundInvalid) as caught:
        service._resolve_model_selector("vonk-forge/uncached")
    assert caught.value.code == ModelCacheCode.SELECTOR_MISSING


def test_publisher_slug_selector_with_a_head_still_follows_the_head(cache, tmp_path):
    service, sessions = cache
    catalog = _ticking_catalog(sessions)
    first = _cache_model_revision(
        service, tmp_path, catalog, slug="headed", weight=b"aaa"
    )
    second = _cache_model_revision(
        service,
        tmp_path,
        catalog,
        slug="headed",
        weight=b"bbb",
        document=first.document_id,
    )
    assert service._resolve_model_selector("vonk-forge/headed") == second.content_digest


def test_removal_review_of_a_retracted_model_covers_every_set_of_the_lineage(
    cache, tmp_path
):
    service, sessions = cache
    _catalog, _first, second = _retracted_lineage(service, sessions, tmp_path)
    _retract_heads(sessions, "gone")
    review = service.review_model_removal("vonk-forge/gone")
    assert review.target_identity == second.content_digest
    assert not review.blockers
    assets = {item.sha256 for item in review.assets}
    assert {
        hashlib.sha256(b"aaa").hexdigest(),
        hashlib.sha256(b"bbb").hexdigest(),
    } <= assets
