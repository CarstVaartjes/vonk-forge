"""Recipe image availability: observation selectors build."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import Actor
from vonk_control.model_cache.errors import ModelCacheError
from vonk_control.model_primitives import Base
from vonk_control.models.cache import (
    RecipeBuild,
)
from vonk_control.models.catalog import (
    CatalogDocumentHead,
)
from vonk_control.models.fleet import (
    AgentNode,
)
from vonk_control.models.jobs import Job
from vonk_control.recipe_image_availability.api import (
    _recipe_error,
    _view_document,
    install_recipe_operator_routes,
)
from vonk_control.recipe_image_availability.contracts import (
    RecipeImageAvailabilityError,
)
from vonk_control.recipe_image_availability.view_contract import (
    RecipeCacheRemovalStatus,
)
from vonk_control.runtime_image_preparation.contracts import (
    PulledImageEvidence,
)
from vonk_control.runtime_image_preparation.storage import FilesystemRuntimeImageStorage
from vonk_forge_contracts import RecipeDefinition, document_sha256

from .display_fixtures import reworded
from .recipe_image_availability_support import (
    ARCHIVE,
    ARCHIVE_SHA,
    CONFIG_DIGEST,
    IMAGE_DIGEST,
    Transport,
    _add_head,
    _add_revision,
    _availability_payload,
    _build_runtime,
    _published,
    _recipe,
    _runtime,
    _service,
)
from .recipe_removal_review_support import remove_after_review
from .runtime_image_fixtures import place_test_image, remove_test_image


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize("invalid_metadata", [None, 12])
def test_authenticated_observation_preserves_operation_through_metadata_repair(
    tmp_path: Path,
    invalid_metadata: int | None,
) -> None:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'availability.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    payload = _availability_payload("revision-observation").model_dump(mode="json")
    now = datetime.now(UTC)
    operation_id = "00000000-0000-4000-8000-000000000207"
    with sessions.begin() as session:
        session.add(
            Job(
                id=operation_id,
                request_id="00000000-0000-4000-8000-000000000208",
                kind="recipe.image.availability.v2",
                state="running",
                actor="owner",
                authority_revision="revision-observation",
                targets=[],
                payload_digest="a" * 64,
                payload=payload,
                current_attempt=3,
                created_at=now,
                updated_at=now,
            )
        )
    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path / "images"),
        authority=lambda **_: (_recipe("recipe-source-build.json"), _runtime()),
        clock=lambda: now,
    )
    app = FastAPI()
    install_recipe_operator_routes(
        app,
        actor_dependency=Depends(lambda: Actor("reader", "viewer")),
        service=service,
    )
    client = TestClient(app)
    path = f"/api/recipe/operations/{operation_id}"
    healthy = client.get(path)
    assert healthy.status_code == 200, healthy.text
    damaged = payload | {
        "recipe": None,
        "request": None,
        "recipe_revision_id": invalid_metadata,
        "recipe_content_sha256": invalid_metadata,
    }
    with sessions.begin() as session:
        session.execute(
            update(Job).where(Job.id == operation_id).values(payload=damaged)
        )
    unknown = client.get(path)
    assert unknown.status_code == 200, unknown.text
    observed = unknown.json()
    assert observed["id"] == operation_id
    assert observed["state"] == "running"
    assert observed["attempt"] == 3
    assert observed["progress"]["phase"] == payload["progress"]["phase"]
    assert observed["request"] is None
    assert observed["recipe_content_sha256"] is None
    assert observed["residue"]["kind"] == "recipe-image.recipe"
    assert observed["residue"]["reason"] == "evidence-unavailable"
    with sessions.begin() as session:
        stored = session.get(Job, operation_id)
        assert stored is not None
        assert stored.payload == damaged
        stored.payload = payload
    repaired = client.get(path)
    assert repaired.status_code == 200, repaired.text
    assert repaired.json() == healthy.json()


@pytest.mark.parametrize(
    ("retryable", "expected_status"),
    [(None, 409), (False, 409), (True, 503)],
)
def test_api_treats_unspecified_retryability_like_terminal_default(
    retryable: bool | None,
    expected_status: int,
) -> None:
    error = RecipeImageAvailabilityError(
        "recipe_image.build_failed",
        "canonical Recipe build failed",
        retryable=retryable,
    )

    assert _recipe_error(error).status_code == expected_status


def test_logical_recipe_selectors_follow_the_head_without_losing_exact_revisions(
    tmp_path,
):
    recipe = _recipe("recipe-source-build.json")
    old_recipe = reworded(recipe, "Previous accepted recipe")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    document_id = "00000000-0000-4000-8000-000000000001"
    old_id = "00000000-0000-4000-8000-000000000002"
    current_id = "00000000-0000-4000-8000-000000000003"
    with sessions.begin() as session:
        old = _add_revision(session, old_id, old_recipe)
        current = _add_revision(session, current_id, recipe)
        old.document_id = current.document_id = document_id
        current.revision_number = 2
        _add_head(session, current)
    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        transport=Transport(),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        clock=lambda: datetime.now(UTC),
    )
    for selector in (
        recipe.identity.slug,
        f"{recipe.identity.publisher}/{recipe.identity.slug}",
        document_id,
    ):
        started = service.start_selector(
            selector, actor="operator", request_id=selector
        )
        assert started.recipe_revision_id == current_id
    assert service._resolve_recipe_selector(old_id) == old_id
    assert (
        service._resolve_recipe_selector(document_sha256(_published(old_recipe)))
        == old_id
    )
    other = recipe.model_copy(
        update={
            "identity": recipe.identity.model_copy(
                update={"publisher": "another-publisher"}
            )
        }
    )
    with sessions.begin() as session:
        _add_head(session, _add_revision(session, "another-recipe", other))
    with pytest.raises(RecipeImageAvailabilityError):
        service.start_selector(
            recipe.identity.slug, actor="operator", request_id="ambiguous-name"
        )
    qualified = f"{recipe.identity.publisher}/{recipe.identity.slug}"
    assert service._resolve_recipe_selector(qualified) == current_id
    with sessions.begin() as session:
        head = session.scalar(
            select(CatalogDocumentHead).where(
                CatalogDocumentHead.publisher == recipe.identity.publisher
            )
        )
        assert head is not None
        head.active_revision_id = None
    with pytest.raises(RecipeImageAvailabilityError):
        service.start_selector(qualified, actor="operator", request_id="missing-head")


def test_selector_replay_keeps_original_revision_and_issuer(tmp_path: Path) -> None:
    recipe = _recipe("recipe-source-build.json")
    newer = reworded(recipe, "New head")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        original = _add_revision(session, "original-head", recipe)
        _add_head(session, original)
    calls = []

    def authority(recipe_revision_id: str, *, force: bool = False):
        calls.append(recipe_revision_id)
        return (recipe if recipe_revision_id == "original-head" else newer), _runtime()

    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        transport=Transport(),
        authority=authority,
        clock=lambda: datetime.now(UTC),
    )
    selector = f"{recipe.identity.publisher}/{recipe.identity.slug}"
    accepted = service.start_selector(
        selector, actor="operator", request_id="original-request", force=True
    )
    with sessions.begin() as session:
        current = _add_revision(session, "new-head", newer)
        head = session.scalar(select(CatalogDocumentHead))
        assert head is not None
        head.active_revision_id = current.id
    recovered = service.start_selector(
        selector, actor="operator", request_id="original-request", force=True
    )
    assert recovered.id == accepted.id
    assert recovered.recipe_revision_id == "original-head"
    assert calls == ["original-head"]
    for actor, requested in [("other-operator", selector), ("operator", "missing")]:
        with pytest.raises(RecipeImageAvailabilityError):
            service.start_selector(
                requested, actor=actor, request_id="original-request", force=True
            )


def test_replay_does_not_collapse_different_image_actions(tmp_path: Path) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "image-actions", recipe)
    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        transport=Transport(),
        authority=lambda *_args, **_kwargs: (recipe, _runtime()),
        clock=lambda: datetime.now(UTC),
    )
    service.start("image-actions", actor="operator", request_id="action", force=True)
    with pytest.raises(RecipeImageAvailabilityError):
        service.start(
            "image-actions", actor="operator", request_id="action", force_rebuild=True
        )


def test_download_after_cache_removal_restores_the_image(tmp_path):
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_head(session, _add_revision(session, "revision-restore", recipe))
    storage = FilesystemRuntimeImageStorage(tmp_path)
    transport = Transport()
    service = _service(
        sessions,
        storage=storage,
        transport=transport,
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        clock=lambda: datetime.now(UTC),
    )
    first = service.start_selector(
        recipe.identity.slug, actor="operator", request_id="1" * 36
    )
    service.run_pending()
    assert service.get(first.id).state == "succeeded"
    removal = remove_after_review(
        service,
        recipe.identity.slug,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000022",
    )
    assert removal["state"] == "queued"
    assert service.advance_removals(limit=1) == 1
    assert service.advance_removals(limit=1) == 1
    removed = service.get_operator_request(
        "00000000-0000-4000-8000-000000000022", actor="operator"
    )
    assert isinstance(removed, RecipeCacheRemovalStatus)
    removed = removed.model_dump(mode="json", exclude_none=True)
    assert removed["state"] == "succeeded"
    # The worker takes the storage receipt only after committing its exact
    # checkpoint and deletion fence.
    # The image's blobs wait for garbage collection, which then reclaims them.
    assert not (storage.root / f"{ARCHIVE_SHA}.receipt.json").exists()
    remove_test_image(storage, ARCHIVE_SHA)
    # Restart and use the real download path, including SQL receipt persistence.
    restarted = _service(
        sessions,
        storage=storage,
        transport=transport,
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        clock=lambda: datetime.now(UTC),
    )
    download = restarted.start_selector(
        recipe.identity.slug, actor="operator", request_id="3" * 36
    )
    restarted.run_pending()
    result = restarted.get(download.id)
    assert result.state == "succeeded", result.failure
    assert storage.existing_archive(ARCHIVE_SHA, len(ARCHIVE)).is_file()


def test_sibling_recipes_publishing_the_same_image_both_download(tmp_path):
    """The same image bytes under another build are the same image.

    Two recipes publish one prebuilt image and each has its own build row. The
    second download used to fail with a permanent
    ``runtime_image.receipt_identity_conflict`` because the stored receipt
    named the first recipe's build.
    """

    first = _recipe("recipe-source-build.json")
    document = first.model_dump(mode="json")
    document["identity"]["slug"] = "sibling-recipe"
    second = RecipeDefinition.model_validate(document)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_head(session, _add_revision(session, "sibling-first", first))
        _add_head(session, _add_revision(session, "sibling-second", second))
    storage = FilesystemRuntimeImageStorage(tmp_path)
    recipes = {"sibling-first": first, "sibling-second": second}
    service = _service(
        sessions,
        storage=storage,
        transport=Transport(),
        authority=lambda recipe_revision_id, *, force=False: (
            recipes[recipe_revision_id],
            _runtime(),
        ),
        clock=lambda: datetime.now(UTC),
    )
    downloads = [
        service.start_selector(
            recipe.identity.slug, actor="operator", request_id=str(index) * 36
        )
        for index, recipe in enumerate((first, second), start=1)
    ]
    service.run_pending()
    service.run_pending()
    for download in downloads:
        result = service.get(download.id)
        assert result.state == "succeeded", result.failure
    assert storage.existing_archive(ARCHIVE_SHA, len(ARCHIVE)).is_file()


def test_build_failure_waits_for_retry_and_exposes_step_contract(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-source", recipe)

    def authority(
        recipe_revision_id: str, *, force: bool = False
    ) -> tuple[RecipeDefinition, dict[str, object]]:
        return recipe, _build_runtime()

    def builder(*_: object, **__: object) -> dict[str, object]:
        raise RecipeImageAvailabilityError(
            "recipe_image.build_failed",
            "compiler failed at step 4",
            retryable=True,
            recovery_actions=("retry",),
            log_excerpt="Step 4: compiler failed",
            step="Step 4",
        )

    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=authority,
        builder=builder,
        clock=lambda: datetime.now(UTC),
    )
    queued = service.start(
        "revision-source",
        actor="operator",
        request_id="1" * 36,
    )
    assert queued.build_input_sha256 == "f" * 64
    assert queued.state == "queued"
    assert service.run_pending() == 1
    failed = service.get(queued.id)
    # A retryable failure stays queued for its next automatic attempt.
    assert failed.state == "queued"
    assert failed.failure is not None
    assert failed.result is None
    assert failed.failure["retryable"] is True
    assert failed.failure["retry_time"] is not None
    assert failed.failure["log_excerpt"] == "Step 4: compiler failed"
    response = _view_document(failed)
    assert response.failure is not None


def test_database_integrity_failure_names_the_violated_constraint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database failure must not be reported as SQLAlchemy's own slug.

    The availability worker records the verified archive in the operation's
    payload (``_persist_receipt``) and flushes. When the database refuses that
    write the raw ``sqlalchemy.exc.IntegrityError`` reaches ``_fail``.
    That exception carries ``code = "gkpj"`` -- SQLAlchemy's documentation slug
    -- and ``detail = []``, the empty ``StatementError.detail`` list, so a
    reporter that trusts those attributes stores ``{"code": "gkpj",
    "detail": "[]"}`` and the operator loses the constraint entirely.
    """

    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    build_id = "00000000-0000-4000-8000-000000000903"
    with sessions.begin() as session:
        _add_revision(session, "revision-integrity-failure", recipe, built=False)
        session.add(AgentNode(node_id="spark-builder", state="active"))
        session.add(
            RecipeBuild(
                id=build_id,
                recipe_revision_id="revision-integrity-failure",
                builder_node_id="spark-builder",
                source_bundle_sha256="b" * 64,
                build_input_sha256="f" * 64,
                state="succeeded",
                policy_report={},
                plan={},
                image_digest=IMAGE_DIGEST,
                oci_layout_sha256=ARCHIVE_SHA,
                image_bytes=len(ARCHIVE),
                error=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    storage = FilesystemRuntimeImageStorage(tmp_path)

    def builder(*_: object, **__: object) -> dict[str, object]:
        place_test_image(storage, ARCHIVE_SHA, len(ARCHIVE))
        return {
            "state": "succeeded",
            "build_id": build_id,
            "build_input_sha256": "f" * 64,
            "image_digest": IMAGE_DIGEST,
            "oci_layout_sha256": ARCHIVE_SHA,
            "image_bytes": len(ARCHIVE),
        }

    class BuildTransport(Transport):
        def inspect_archive(
            self,
            archive: Path,
            *,
            expected_architecture: str,
            expected_runtime_interface: str,
            expected_archive_sha256: str,
            expected_archive_bytes: int,
        ) -> PulledImageEvidence:
            return PulledImageEvidence(
                manifest_digest=IMAGE_DIGEST,
                config_id=CONFIG_DIGEST,
                local_reference="oci-layout:" + archive.name,
                architecture=expected_architecture,
                runtime_interface=expected_runtime_interface,
                archive_sha256=expected_archive_sha256,
                archive_bytes=expected_archive_bytes,
            )

    # Built exactly as the DBAPI layer builds it (``statement``, ``params``,
    # ``orig``): the driver error is the ``orig`` the failure reporter must
    # surface, while the SQLAlchemy wrapper contributes the empty ``detail``
    # list and the ``gkpj`` slug that used to win.
    def receipt_writer(*_args: object, **_kwargs: object) -> None:
        refusal = sqlite3.IntegrityError(
            "UNIQUE constraint failed: jobs.request_id, jobs.kind"
        )
        error = IntegrityError(None, None, refusal)
        assert error.detail == []
        raise error

    service = _service(
        sessions,
        storage=storage,
        authority=lambda recipe_revision_id, *, force=False: (
            recipe,
            _build_runtime(),
        ),
        transport=BuildTransport(),
        builder=builder,
        clock=lambda: datetime.now(UTC),
    )
    original_writer = service._persist_receipt
    monkeypatch.setattr(service, "_persist_receipt", receipt_writer)
    queued = service.start(
        "revision-integrity-failure",
        actor="operator",
        request_id="i" * 36,
    )
    assert service.run_pending() == 1
    failed = service.get(queued.id)
    assert failed.state == "queued"
    assert failed.artifact is None
    monkeypatch.setattr(service, "_persist_receipt", original_writer)
    service._clock = lambda: datetime.now(UTC) + timedelta(minutes=1)
    assert service.run_pending() == 1
    completed = service.get(queued.id)
    assert completed.artifact is not None
    fresh = service.start(
        "revision-integrity-failure", actor="operator", request_id=str(uuid.uuid4())
    )
    assert service.run_pending() == 1
    assert service.get(fresh.id).artifact is not None


def test_model_cache_error_coerces_a_non_string_detail() -> None:
    """``str(error)`` must never become ``[]`` for a model cache failure."""

    # A caller can reach this with a sequence at runtime even though the
    # parameter is declared as text; the constructor must not store it as is.
    error = ModelCacheError("model_cache.rate_limited", cast("str", []))
    assert isinstance(error.detail, str)
    assert error.detail == "[]"
    assert str(error) == "[]"


def test_build_mode_dispatches_when_no_verified_build_receipt_exists(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    build_id = "00000000-0000-4000-8000-000000000902"
    with sessions.begin() as session:
        _add_revision(session, "revision-missing-build-archive", recipe, built=False)
        session.add(AgentNode(node_id="spark-builder", state="active"))
        session.add(
            RecipeBuild(
                id=build_id,
                recipe_revision_id="revision-missing-build-archive",
                builder_node_id="spark-builder",
                source_bundle_sha256="b" * 64,
                build_input_sha256="f" * 64,
                state="succeeded",
                policy_report={},
                plan={},
                image_digest=IMAGE_DIGEST,
                oci_layout_sha256=ARCHIVE_SHA,
                image_bytes=len(ARCHIVE),
                error=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    storage = FilesystemRuntimeImageStorage(tmp_path)
    forced: list[bool] = []

    def builder(*_: object, force: bool, **__: object) -> dict[str, object]:
        forced.append(force)
        place_test_image(storage, ARCHIVE_SHA, len(ARCHIVE))
        return {
            "state": "succeeded",
            "build_id": build_id,
            "build_input_sha256": "f" * 64,
            "image_digest": IMAGE_DIGEST,
            "oci_layout_sha256": ARCHIVE_SHA,
            "image_bytes": len(ARCHIVE),
        }

    class BuildTransport(Transport):
        def inspect_archive(
            self,
            archive: Path,
            *,
            expected_architecture: str,
            expected_runtime_interface: str,
            expected_archive_sha256: str,
            expected_archive_bytes: int,
        ) -> PulledImageEvidence:
            assert archive.name == expected_archive_sha256
            return PulledImageEvidence(
                manifest_digest=IMAGE_DIGEST,
                config_id=CONFIG_DIGEST,
                local_reference="oci-layout:" + archive.name,
                architecture=expected_architecture,
                runtime_interface=expected_runtime_interface,
                archive_sha256=expected_archive_sha256,
                archive_bytes=expected_archive_bytes,
            )

    service = _service(
        sessions,
        storage=storage,
        authority=lambda recipe_revision_id, *, force=False: (
            recipe,
            _build_runtime(),
        ),
        transport=BuildTransport(),
        builder=builder,
        clock=lambda: datetime.now(UTC),
    )
    queued = service.start(
        "revision-missing-build-archive",
        actor="operator",
        request_id="r" * 36,
    )

    # Cache reconciliation belongs to the builder's own filesystem-first
    # resolution; the durable service only dispatches and records the result.
    assert service.run_pending() == 1
    completed = service.get(queued.id)
    assert completed.state == "succeeded", completed.failure
    assert forced == [False]
    assert storage.existing_archive(ARCHIVE_SHA, len(ARCHIVE)).is_file()
