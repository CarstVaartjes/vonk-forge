from __future__ import annotations

import hashlib
import itertools
import uuid
from datetime import timedelta
from pathlib import Path

import httpx2
import pytest
import vonk_control.model_cache as model_cache_module
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import LifecycleState, ModelCacheCode
from vonk_control.auth import Actor
from vonk_control.catalog.entities import CatalogEntityService
from vonk_control.model_cache import (
    ModelCacheService,
)
from vonk_control.model_cache.errors import (
    ModelCacheNotFoundInvalid,
    ModelCacheSelectorAmbiguous,
)
from vonk_control.model_cache_api import (
    install_model_operator_routes,
)
from vonk_control.models import (
    Base,
    CatalogDocumentHead,
    ModelCacheSet,
)

from . import model_cache_support as _support
from .display_fixtures import create_draft
from .model_cache_support import (
    NOW,
    _artifact,
    _canonical_model,
    _download,
    _gone_handler,
    _http_artifact,
    _http_cache_service,
)
from .test_model_cache_recovery_support import observe_unknown

cache = _support.cache


@pytest.fixture
def threaded_cache(tmp_path: Path):
    """Background workers use independent connections, like the Controller."""

    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'background-cache.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = ModelCacheService(
        sessions,
        tmp_path / "background-nas-cache",
        free_fraction=0,
        fixture_sources=True,
    )
    try:
        yield service, sessions
    finally:
        service.close()
        engine.dispose()


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
        for _ in range(model_cache_module._SOURCE_GONE_ATTEMPTS - 2):
            service.run_pending()
            assert service.get_operation(operation.id).state == "queued"
        service.run_pending()
        assert served["/second.bin"] == model_cache_module._SOURCE_GONE_ATTEMPTS
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
    from concurrent.futures import Future, wait

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
