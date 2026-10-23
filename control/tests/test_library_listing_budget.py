"""The Library listing stays fast and bounded as the catalog and the load grow.

Production regression: ``GET /api/recipe/library`` stopped answering within the
client's 15 second timeout once the image store held hundreds of images and
about 150 preparations were in flight. A listing is read-only, so its cost must
not depend on how much history, how many images, or how many stored model
objects exist, and a slow storage or placement read must degrade one recipe's
answer instead of holding the whole response.

These tests count the reads a listing makes and bound its wall time with a wide
margin, so they fail on the unbounded behavior without being timing-sensitive.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select, update
from vonk_control.auth import Actor, TokenCodec
from vonk_control.library_api import install_library_routes
from vonk_control.library_assessment import LibraryAssessment
from vonk_control.library_contract import ModelDetailResponse, ModelLibraryResponse
from vonk_control.library_image_presence import ImagePresenceIndex
from vonk_control.library_projection import LibraryProjection
from vonk_control.model_cache_contract import (
    ModelCacheUsage,
    ModelStorageObservationState,
)
from vonk_control.models import (
    CatalogDocumentHead,
    CatalogDocumentRevision,
    ModelCacheSet,
    RecipeBuild,
)
from vonk_forge_contracts import RecipeDefinition, document_sha256

from .pass_budget_clocks import PassClock
from .runtime_image_fixtures import place_test_image, remove_test_image
from .test_library_assessment import (  # noqa: F401 - the shared fixture
    assessed_library,
)
from .test_library_canonical_projection import _insert_canonical_rows
from .test_recipe_image_availability import _recipe_projection
from .test_recipe_operations import NOW

# A blocked read holds until the test releases it; the listing must answer
# while it is still blocked.
_BLOCKED_SECONDS = 30.0


def _expire_image_wait(monkeypatch, clock: PassClock, entered: threading.Event):
    """Expire only once a real probe has reached the injected fault."""
    from concurrent.futures import wait

    def expired(futures, *, timeout, return_when):
        assert entered.wait(timeout=30)
        clock.advance(timeout)
        return wait(futures, timeout=0, return_when=return_when)

    monkeypatch.setattr("vonk_control.library_image_presence.wait", expired)
    return wait


def _expiring_thread(clock: PassClock, entered: threading.Event):
    class ExpiringThread(threading.Thread):
        def join(self, timeout=None):
            assert entered.wait(timeout=30)
            clock.advance(timeout or 0)
            super().join(timeout=0)

    return ExpiringThread


def _hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _seed_recipes_with_images(fixture, *, recipes: int, history: int) -> int:
    """Give ``recipes`` current recipes one stored image each, plus old revisions.

    The old revisions of a recipe have no build of their own, the way a catalog
    refresh leaves them. Returns the number of distinct stored images.
    """

    projection, sessions, _, _, _, _, _, build_id, _, storage = fixture
    template = projection.recipe_library(assess=False).recipes[0]
    _insert_canonical_rows(
        sessions,
        kind="recipe",
        template=projection.recipe_detail(template.selector).document.model_dump(
            mode="json"
        ),
        count=recipes,
    )
    with sessions() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        columns = {
            column.name: getattr(build, column.name)
            for column in RecipeBuild.__table__.columns
        }
        heads = list(
            session.scalars(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.slug.like("recipe-%"),
                )
            )
        )
    assert len(heads) == recipes
    with sessions.begin() as session:
        for index, revision in enumerate(heads):
            address = _hex(f"image-{index}")
            size = 100 + index
            place_test_image(storage, address, size)
            image_build = RecipeBuild(
                **{
                    **columns,
                    "id": str(uuid.uuid4()),
                    "recipe_revision_id": revision.id,
                    "build_input_sha256": _hex(f"input-{index}"),
                    "oci_layout_sha256": address,
                    "image_bytes": size,
                }
            )
            session.add(image_build)
            session.flush()
            for old in range(history):
                older = json.loads(json.dumps(revision.document))
                older["metadata"]["description"] += f" (revision {old})"
                older = RecipeDefinition.model_validate(older).model_dump(mode="json")
                owner = CatalogDocumentRevision(
                    document_id=revision.document_id,
                    kind="recipe",
                    publisher=revision.publisher,
                    slug=revision.slug,
                    revision_number=revision.revision_number + 1 + old,
                    schema_version=2,
                    state="active",
                    document=older,
                    content_digest=document_sha256(older),
                    projected={},
                    created_by="test",
                    created_at=NOW,
                )
                session.add(owner)
                session.flush()
    return recipes


def test_listing_asks_about_each_current_image_once_whatever_the_history(
    assessed_library,  # noqa: F811
):
    _, sessions, _, _, _, _, _, _, _, storage = assessed_library
    images = _seed_recipes_with_images(assessed_library, recipes=8, history=5)
    asked: list[tuple[str, int]] = []

    def counting(archive: str, size: int) -> bool:
        asked.append((archive, size))
        return storage.build_archive_available(archive, size)

    projection = LibraryProjection(
        sessions,
        cursors=TokenCodec(b"b" * 32).cursor_codec(),
        clock=lambda: NOW,
        runtime_archive_available=counting,
    )
    page = projection.recipe_library(assess=False, limit=100)
    seeded = [item for item in page.recipes if item.identity.slug.startswith("recipe-")]
    assert len(seeded) == images
    assert {item.local.controller for item in seeded} == {"cached"}
    # One storage read per distinct current image, not one per build, per
    # authorization and per old revision (8 * (1 + 6) reads before).
    assert len(asked) == len(set(asked)) <= images + 1


def test_a_blocked_image_read_makes_the_recipe_unknown_not_the_listing_slow(
    monkeypatch,
    frozen_pass_budgets,
    assessed_library,  # noqa: F811
):
    _, sessions, _, _, _, _, _, _, _, storage = assessed_library
    release = threading.Event()
    blocked = threading.Event()
    answered = threading.Event()

    def probe(archive: str, size: int) -> bool:
        blocked.set()
        release.wait(_BLOCKED_SECONDS * 2)
        answered.set()
        return storage.build_archive_available(archive, size)

    projection = LibraryProjection(
        sessions,
        cursors=TokenCodec(b"b" * 32).cursor_codec(),
        clock=lambda: NOW,
        runtime_archive_available=probe,
        image_budget_seconds=0.2,
    )
    wait = _expire_image_wait(monkeypatch, frozen_pass_budgets, blocked)
    try:
        listing = projection.recipe_library(assess=False)
        assert blocked.is_set()
        # The listing answered while the image read was still blocked.
        assert not answered.is_set()
        # Neither "cached" nor "not cached" is known yet.
        assert listing.recipes[0].local.controller == "unknown"
    finally:
        release.set()
    monkeypatch.setattr("vonk_control.library_image_presence.wait", wait)
    # The answer arrives in the background; the next listing has it.
    deadline = time.monotonic() + 30
    state = "unknown"
    while state == "unknown" and time.monotonic() < deadline:
        state = projection.recipe_library(assess=False).recipes[0].local.controller
    assert state == "cached"


def test_a_blocked_placement_read_degrades_the_recipe_not_the_listing(
    monkeypatch,
    frozen_pass_budgets,
    assessed_library,  # noqa: F811
):
    _, sessions, cache, service, _, _, _, _, _, storage = assessed_library
    release = threading.Event()
    answered = threading.Event()
    entered = threading.Event()
    inspect = service.inspect_candidate

    def blocked_inspect(*args, **kwargs):
        entered.set()
        release.wait(_BLOCKED_SECONDS * 2)
        answered.set()
        return inspect(*args, **kwargs)

    from types import SimpleNamespace

    monkeypatch.setattr(
        "vonk_control.library_assessment.threading",
        SimpleNamespace(
            Thread=_expiring_thread(frozen_pass_budgets, entered),
            Lock=threading.Lock,
            Event=threading.Event,
        ),
    )
    service.inspect_candidate = blocked_inspect  # type: ignore[method-assign]
    projection = LibraryProjection(
        sessions,
        cursors=TokenCodec(b"b" * 32).cursor_codec(),
        clock=lambda: NOW,
        runtime_archive_available=storage.build_archive_available,
        assessment=LibraryAssessment(
            sessions,
            run_switch=service,
            model_cache=cache,
            clock=lambda: NOW,
            budget_seconds=0.3,
        ),
    )
    try:
        listing = projection.recipe_library(assess=True)
        # The listing answered while the placement read was still blocked.
        assert not answered.is_set()
    finally:
        release.set()
    assessment = listing.recipes[0].assessment
    assert assessment is not None
    assert assessment.readiness.state == "unavailable"


def test_assessment_reads_do_not_walk_the_stored_model_objects(
    assessed_library,  # noqa: F811
    monkeypatch,
):
    projection, _, cache, *_ = assessed_library

    def stats_for_one_listing() -> int:
        count = 0
        real = os.stat

        def counting(*args, **kwargs):
            nonlocal count
            count += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(os, "stat", counting)
        try:
            projection.recipe_library(limit=1, assess=True)
        finally:
            monkeypatch.setattr(os, "stat", real)
        return count

    stats_for_one_listing()  # warm imports and lazily created state
    empty = stats_for_one_listing()
    for index in range(300):
        digest = _hex(f"object-{index}")
        directory = cache.root / "objects" / digest[:2]
        directory.mkdir(parents=True, exist_ok=True)
        (directory / digest).write_bytes(b"x")
        (directory / f"{digest}.receipt.json").write_text("{}")
    populated = stats_for_one_listing()
    # Admission reads the free space; it does not measure every stored object.
    assert populated - empty < 60, (empty, populated)


def test_image_presence_is_asked_once_for_concurrent_and_recent_readers(
    monkeypatch, frozen_pass_budgets
):
    clock = [0.0]
    entered = threading.Event()
    release = threading.Event()
    calls: list[tuple[str, int]] = []

    def probe(archive: str, size: int) -> bool:
        calls.append((archive, size))
        entered.set()
        release.wait(30)
        return archive != "gone"

    index = ImagePresenceIndex(
        probe,
        present_ttl_seconds=30,
        absent_ttl_seconds=5,
        clock=lambda: clock[0],
    )
    wanted = {("kept", 1), ("gone", 2)}
    wait = _expire_image_wait(monkeypatch, frozen_pass_budgets, entered)
    first = index.lookup(wanted, budget_seconds=0.05)
    assert {answer.state for answer in first.values()} == {"unknown"}
    # A second reader joins the probe already running instead of repeating it.
    assert {
        answer.state for answer in index.lookup(wanted, budget_seconds=0.05).values()
    } == {"unknown"}
    release.set()
    monkeypatch.setattr("vonk_control.library_image_presence.wait", wait)
    settled = index.lookup(wanted, budget_seconds=2)
    assert settled[("kept", 1)].state == "present"
    assert settled[("gone", 2)].state == "absent"
    assert sorted(calls) == [("gone", 2), ("kept", 1)]
    # Recent answers are reused; an absent image is asked again sooner.
    clock[0] = 4
    index.lookup(wanted, budget_seconds=1)
    assert len(calls) == 2
    clock[0] = 6
    index.lookup(wanted, budget_seconds=1)
    assert sorted(calls) == [("gone", 2), ("gone", 2), ("kept", 1)]
    clock[0] = 31
    index.lookup(wanted, budget_seconds=1)
    assert len(calls) == 5


def _queued_download_progress(sessions, tmp_path, cache):
    """The Library's view of a queued model download, from the persisted rows."""

    projection = LibraryProjection(
        sessions,
        cursors=TokenCodec(b"c" * 32).cursor_codec(),
        clock=lambda: NOW,
    )
    recipe = projection.recipe_library(assess=False).recipes[0]
    preview = cache.download_preview(
        recipe_revision_id=recipe.identity.recipe_revision_id
    )
    cache.start_download(
        actor="test",
        request_key=str(uuid.uuid4()),
        plan_digest=str(preview["plan_digest"]),
        recipe_revision_id=recipe.identity.recipe_revision_id,
    )
    listed = projection.recipe_library(assess=False).recipes[0].local
    model = projection.models().models[0].local
    return listed, model, preview


def _check_queued_download_projection(sessions, tmp_path, cache):
    listed, model, preview = _queued_download_progress(sessions, tmp_path, cache)
    for local in (listed, model):
        assert local.preparation is not None
        assert local.preparation.state == "queued"
        assert local.preparation.completed_bytes == 0
        assert local.preparation.total_bytes == preview["new_bytes"]
    assert model.controller == "preparing"


def test_queued_download_progress_is_projected_from_the_persisted_row(
    assessed_library,  # noqa: F811
    tmp_path,
):
    _, sessions, cache, *_ = assessed_library
    _check_queued_download_projection(sessions, tmp_path, cache)


def test_queued_download_progress_is_projected_on_postgres(tmp_path, postgres_engine):
    """The production database reads the same progress through its JSON operators."""

    from importlib import resources

    from vonk_control.model_cache import ModelCacheService
    from vonk_control.models import CatalogDocumentHead
    from vonk_forge_contracts import document_sha256

    from .test_recipe_operations import setup_services

    model = json.loads(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text()
    )
    model["source"]["repository"] = "https://huggingface.co/vonk-forge/synthetic-tiny"
    digest = document_sha256(model)

    def bind_model(recipe):
        recipe["models"][0]["model"]["content_sha256"] = digest

    sessions, *_ = setup_services(
        tmp_path,
        engine=postgres_engine,
        model_transform=lambda document: document.update(model),
        recipe_transform=bind_model,
    )
    with sessions.begin() as session:
        for row in session.scalars(select(CatalogDocumentRevision)):
            session.add(
                CatalogDocumentHead(
                    kind=row.kind,
                    publisher=row.publisher,
                    slug=row.slug,
                    active_revision_id=row.id,
                )
            )
    cache = ModelCacheService(
        sessions, tmp_path / "model-cache", reserve_bytes=0, clock=lambda: NOW
    )
    _check_queued_download_projection(sessions, tmp_path, cache)


def _library(sessions, storage) -> LibraryProjection:
    return LibraryProjection(
        sessions,
        cursors=TokenCodec(b"b" * 32).cursor_codec(),
        clock=lambda: NOW,
        runtime_archive_available=storage.build_archive_available,
    )


def test_a_head_reusing_its_predecessors_image_is_cached_while_it_is_stored(
    assessed_library,  # noqa: F811
):
    """The image is the recipe's: a successor has no build row and no grant."""

    _, sessions, _, _, _, _, _, _, _, storage = assessed_library
    _seed_recipes_with_images(assessed_library, recipes=1, history=0)
    with sessions.begin() as session:
        built = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.slug.like("recipe-%"),
            )
        )
        assert built is not None
        # The recipes are built from one source bundle; a successor with the
        # same source reuses the build.
        projection = _recipe_projection(RecipeDefinition.model_validate(built.document))
        session.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == built.id)
            .values(projected=projection)
        )
        for build in session.scalars(
            select(RecipeBuild).where(RecipeBuild.recipe_revision_id == built.id)
        ):
            build.source_bundle_sha256 = "b" * 64
        document = json.loads(json.dumps(built.document))
        document["metadata"]["description"] += " (editorial successor)"
        document = RecipeDefinition.model_validate(document).model_dump(mode="json")
        successor = CatalogDocumentRevision(
            document_id=built.document_id,
            kind="recipe",
            publisher=built.publisher,
            slug=built.slug,
            revision_number=built.revision_number + 1,
            schema_version=2,
            state="active",
            document=document,
            content_digest=document_sha256(document),
            projected=projection,
            created_by="test",
            created_at=NOW,
        )
        session.add(successor)
        session.flush()
        head = session.scalar(
            select(CatalogDocumentHead).where(
                CatalogDocumentHead.slug == built.slug,
                CatalogDocumentHead.kind == "recipe",
            )
        )
        assert head is not None
        head.active_revision_id = successor.id
        session.add(
            ModelCacheSet(
                artifact_set_sha256=_hex("successor-set"),
                schema_version=2,
                model_content_sha256=_hex("successor-model"),
                recipe_revision_sha256=successor.content_digest,
                manifest={},
                expected_bytes=1,
                verified_bytes=1,
                state="cached",
                created_at=NOW,
                updated_at=NOW,
                verified_at=NOW,
                last_accessed_at=NOW,
            )
        )
        slug = successor.slug

    def controller() -> str:
        page = _library(sessions, storage).recipe_library(assess=False, limit=100)
        (item,) = [item for item in page.recipes if item.identity.slug == slug]
        return item.local.controller

    assert controller() == "cached"
    remove_test_image(storage, _hex("image-0"))
    assert controller() == "not_cached"


def test_blocked_image_attempt_is_deduplicated_and_other_image_progresses(
    monkeypatch, frozen_pass_budgets
):
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def probe(archive: str, _size: int) -> bool:
        calls.append(archive)
        if archive == "blocked":
            entered.set()
            assert release.wait(timeout=30)
        return True

    index = ImagePresenceIndex(probe, workers=2, present_ttl_seconds=30)
    wait = _expire_image_wait(monkeypatch, frozen_pass_budgets, entered)
    try:
        index.lookup({("blocked", 1)}, budget_seconds=0.01)
        assert entered.wait(timeout=30)
        for _ in range(20):
            index.lookup({("blocked", 1)}, budget_seconds=0)
        monkeypatch.setattr("vonk_control.library_image_presence.wait", wait)
        observed = index.lookup({("independent", 1)}, budget_seconds=1)
        assert observed[("independent", 1)].state == "present"
        assert calls.count("blocked") == 1
        release.set()
        repaired = index.lookup({("blocked", 1)}, budget_seconds=1)
        assert repaired[("blocked", 1)].state == "present"
        assert (
            index.lookup({("fresh", 1)}, budget_seconds=1)[("fresh", 1)].state
            == "present"
        )
    finally:
        release.set()


def test_blocked_assessment_reuses_one_executor_then_fresh_read_is_assessed(
    assessed_library,  # noqa: F811 -- shared pytest fixture
    monkeypatch,
    frozen_pass_budgets,
):
    projection, sessions, cache, service, *_ = assessed_library
    recipes = projection.recipe_library(assess=False).recipes
    entered = threading.Event()
    release = threading.Event()
    workers = []

    class RecoveryThread(threading.Thread):
        def join(self, timeout=None):
            if not release.is_set():
                assert entered.wait(timeout=30)
                frozen_pass_budgets.advance(timeout or 0)
                super().join(timeout=0)
            else:
                super().join()

    real_thread = RecoveryThread

    def worker(*args, **kwargs):
        target = kwargs["target"]

        def blocked_target():
            # Inject the wait before roster/cache/placement reads, whose runtime
            # on a loaded runner must not decide whether the fault was reached.
            entered.set()
            assert release.wait(timeout=30)
            target()

        if not workers:
            kwargs["target"] = blocked_target
        thread = real_thread(*args, **kwargs)
        workers.append(thread)
        return thread

    from types import SimpleNamespace

    monkeypatch.setattr(
        "vonk_control.library_assessment.threading",
        SimpleNamespace(Thread=worker, Lock=threading.Lock, Event=threading.Event),
    )
    assessment = LibraryAssessment(
        sessions,
        run_switch=service,
        model_cache=cache,
        clock=lambda: NOW,
        budget_seconds=10.0,
    )
    try:
        # Only the blocked read needs to expire quickly. Recovery exercises real
        # placement SQL and gets the full bounded budget on a loaded CI runner.
        first = assessment(recipes, budget_seconds=1.0)
        assert entered.wait(timeout=30)
        for _ in range(20):
            assert assessment(recipes)[0].assessment is not None
        assert len(workers) == 1
        assert first[0].assessment is not None
        assert projection.models().models
        release.set()
        workers[0].join(timeout=10)
        assert not workers[0].is_alive()
        fresh = assessment(recipes)
        assert len(workers) == 2
        assert fresh[0].assessment is not None
        assert fresh[0].assessment.group is not None
    finally:
        release.set()


def test_local_state_query_count_does_not_grow_with_page_size(assessed_library):  # noqa: F811
    """Latest-request selection must issue one batch, rather than one query per digest."""

    projection, sessions, *_ = assessed_library
    statements = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    engine = sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", observe)
    try:
        projection._database_local_state([_hex("one")])
        one = len(statements)
        statements.clear()
        projection._database_local_state(
            [_hex(f"digest-{index}") for index in range(50)]
        )
        fifty = len(statements)
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert one == fifty


def _model_reads(client: TestClient, selector: str):
    pages = []
    for query in ({}, {"limit": 1}, {"cached": True}, {"search": "no-such-model"}):
        response = client.get("/api/model/library", params=query)
        assert response.status_code == 200, response.text
        pages.append(ModelLibraryResponse.model_validate_json(response.content))
    response = client.get(f"/api/model/{selector}")
    assert response.status_code == 200, response.text
    return pages, ModelDetailResponse.model_validate_json(response.content)


def _model_storage_app(sessions, provider, clock):
    projection = LibraryProjection(
        sessions,
        cursors=TokenCodec(b"s" * 32).cursor_codec(),
        model_storage=provider,
        clock=clock,
    )
    app = FastAPI()
    install_library_routes(
        app,
        actor_dependency=Depends(lambda: Actor("viewer", "viewer")),
        projection=projection,
    )
    return projection, app


def test_model_reads_never_invoke_storage_provider(assessed_library):  # noqa: F811
    """Catches the full storage walk on every list variant and detail, before filtering."""
    original, sessions, *_ = assessed_library
    model = original.models().models[0]
    calls = []
    usage = ModelCacheUsage(cached=True, disk_bytes=7, unique_bytes=7, shared_bytes=0)

    def provider():
        calls.append(True)
        return {model.identity.content_sha256: usage}

    projection, app = _model_storage_app(sessions, provider, lambda: NOW)
    with TestClient(app) as client:
        for _ in range(3):
            _model_reads(client, model.selector)
        pages, detail = _model_reads(client, model.selector)
        assert detail.storage_state == ModelStorageObservationState.NOT_OBSERVED
        assert all(
            page.storage_state == ModelStorageObservationState.NOT_OBSERVED
            for page in pages
        )
        assert calls == []  # Fresh startup is typed unknown, never a lazy walk.
        projection.refresh_catalog()
        assert len(calls) == 1
        for _ in range(3):
            pages, detail = _model_reads(client, model.selector)
            assert all(page.storage_observed_at == NOW for page in pages)
            assert detail.cache_usage == usage
            assert detail.storage_observed_at == NOW
        assert len(calls) == 1


@pytest.mark.parametrize("previously_observed", [False, True])
def test_model_reads_survive_blocked_and_failed_storage_refresh_then_heal(
    assessed_library,  # noqa: F811
    previously_observed,
):
    """Reads complete while refresh is blocked, retain old facts on error, then converge."""
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta

    original, sessions, *_ = assessed_library
    model = original.models().models[0]
    observed = [NOW]
    entered = threading.Event()
    release = threading.Event()
    fault = threading.Event()
    calls = []
    usage = ModelCacheUsage(cached=True, disk_bytes=7, unique_bytes=7, shared_bytes=0)

    def provider():
        calls.append(True)
        if fault.is_set():
            entered.set()
            assert release.wait(timeout=30)
            raise OSError("injected storage outage")
        return {model.identity.content_sha256: usage}

    projection, app = _model_storage_app(sessions, provider, lambda: observed[0])
    if previously_observed:
        projection.refresh_catalog()
    fault.set()
    expected_time = NOW if previously_observed else None
    expected_usage = usage if previously_observed else None

    def assert_reads(client):
        pages, detail = _model_reads(client, model.selector)
        assert all(page.storage_observed_at == expected_time for page in pages)
        assert detail.storage_observed_at == expected_time
        assert detail.cache_usage == expected_usage
        expected_state = (
            ModelStorageObservationState.OBSERVED
            if previously_observed
            else ModelStorageObservationState.NOT_OBSERVED
        )
        assert detail.storage_state == expected_state
        assert all(page.storage_state == expected_state for page in pages)
        assert detail.resources.disk_bytes == (7 if previously_observed else None)

    with ThreadPoolExecutor(max_workers=1) as executor, TestClient(app) as client:
        refresh = executor.submit(projection.refresh_catalog)
        try:
            assert entered.wait(timeout=30)
            for _ in range(3):
                assert_reads(client)
            assert not refresh.done()  # No short wall-clock claim about real work.
        finally:
            release.set()
        with pytest.raises(OSError, match="injected storage outage"):
            refresh.result(timeout=30)
        for _ in range(3):
            assert_reads(client)
        assert len(calls) == (2 if previously_observed else 1)
        fault.clear()
        observed[0] += timedelta(minutes=1)
        projection.refresh_catalog()  # The same background path recovers.
        pages, detail = _model_reads(client, model.selector)
        assert all(page.storage_observed_at == observed[0] for page in pages)
        assert detail.storage_observed_at == observed[0]
        assert detail.cache_usage == usage
