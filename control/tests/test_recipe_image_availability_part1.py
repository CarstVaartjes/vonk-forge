from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import LifecycleState
from vonk_control.artifact_lifecycle import (
    ArtifactIdentity,
    check_removal_fence_nowait,
)
from vonk_control.auth import Actor
from vonk_control.bounded_json import require_mapping
from vonk_control.model_cache import ModelCacheError
from vonk_control.models import (
    AgentNode,
    Base,
    CatalogDocumentHead,
    FleetProfile,
    FleetProfileApplication,
    Job,
    RecipeBuild,
)
from vonk_control.recipe_image_availability import (
    RecipeImageAvailabilityError,
)
from vonk_control.recipe_image_availability_api import (
    _recipe_error,
    _view_document,
    install_recipe_operator_routes,
)
from vonk_control.recipe_image_availability_view_contract import (
    RecipeCacheRemovalStatus,
    RecipeImageAvailabilityView,
)
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    PulledImageEvidence,
    RuntimeImagePreparationError,
)
from vonk_forge_contracts import RecipeDefinition, document_sha256

from .display_fixtures import reworded
from .non_blocking import assert_ended_without_blocking
from .recipe_image_availability_support import (
    ARCHIVE,
    ARCHIVE_SHA,
    CONFIG_DIGEST,
    IMAGE_DIGEST,
    Transport,
    _add_head,
    _add_revision,
    _archive_removal_service,
    _availability_payload,
    _build_runtime,
    _empty_recipe_removal_owner,
    _published,
    _recipe,
    _reference_receipt,
    _removal_owner,
    _runtime,
    _seed_archives,
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


def test_remove_recipe_does_not_cancel_accepted_build_or_preparation(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_head(
            session,
            _add_revision(session, "revision-remove-build", recipe, built=False),
        )

    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _build_runtime()),
        clock=lambda: datetime.now(UTC),
    )
    queued = service.start(
        "revision-remove-build",
        actor="operator",
        request_id="a" * 36,
    )
    with sessions.begin() as session:
        session.add(AgentNode(node_id="spark-builder", state="active"))
        session.add(
            RecipeBuild(
                id="00000000-0000-4000-8000-000000000901",
                recipe_revision_id="revision-remove-build",
                builder_node_id="spark-builder",
                source_bundle_sha256="b" * 64,
                build_input_sha256="f" * 64,
                state="building",
                policy_report={},
                plan={},
                image_digest=None,
                oci_layout_sha256=None,
                image_bytes=None,
                error=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )

    result = remove_after_review(
        service,
        recipe.identity.slug,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000023",
    )
    assert result["operation_id"]
    assert result["cancelled_operations"] == []
    assert result["cancelled_builds"] == []
    assert result["preserved"] == [
        "profile-assignments",
        "spark-local-copies",
        "model-download",
    ]
    assert service.get(queued.id).state == "queued"
    observed = service.get_operator_operation(str(result["operation_id"]))
    assert isinstance(observed, RecipeCacheRemovalStatus)
    assert observed.operation_id == result["operation_id"]
    with sessions() as session:
        build = session.get(RecipeBuild, "00000000-0000-4000-8000-000000000901")
        assert build is not None and build.state == "building"
    assert service.advance_removals(limit=1) == 1
    with sessions() as session:
        ended = session.scalar(select(Job).where(Job.id == str(result["operation_id"])))
        assert ended is not None and ended.state == "succeeded"
    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        cast(Job | RecipeImageAvailabilityView, ended),
        end=lambda receipt: receipt,
        fresh=lambda _: service.start(
            "revision-remove-build",
            actor="operator",
            request_id=str(uuid.uuid4()),
        ),
    )


@pytest.mark.usefixtures("damaged_json_rows")
def test_removal_recovers_when_damaged_reference_observation_clears(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'reference-budget.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    now = datetime.now(UTC)
    with sessions.begin() as session:
        profile = FleetProfile(
            id="00000000-0000-4000-8000-000000000031",
            number=1,
            revision=1,
            name="budget test",
            description="",
            installation_policy="keep-cached",
            assignments=[],
            labels={},
            favorite=False,
            created_by="operator",
            created_at=now,
            updated_at=now,
        )
        session.add(profile)
        session.add(
            FleetProfileApplication(
                id="00000000-0000-4000-8000-000000000032",
                request_key="00000000-0000-4000-8000-000000000033",
                profile_id=profile.id,
                profile_digest="a" * 64,
                plan_digest="b" * 64,
                state="running",
                plan={"oversized": "owner payload"},
                current_step=0,
                current_operation_id=None,
                progress={},
                result=None,
                status_reason=None,
                actor="operator",
                created_at=now,
                updated_at=now,
            )
        )

    recipe = _recipe("recipe-source-build.json")
    with sessions.begin() as session:
        _add_head(session, _add_revision(session, "damaged-reference", recipe))
    storage = FilesystemRuntimeImageStorage(tmp_path / "damaged-reference-cache")
    place_test_image(storage, ARCHIVE_SHA, len(ARCHIVE))
    receipt_path = storage.root / f"{ARCHIVE_SHA}.receipt.json"
    receipt_path.write_text(_reference_receipt().model_dump_json())
    observed = [now]
    service = _service(
        sessions,
        storage=storage,
        authority=lambda *_args, **_kwargs: (recipe, _runtime()),
        clock=lambda: observed[0],
    )
    key = str(uuid.uuid4())
    accepted = service.remove_selector(
        recipe.identity.slug, actor="operator", request_id=key
    )
    for _ in range(8):
        service.advance_removals(limit=1)
        observed[0] += timedelta(seconds=60)
        with sessions() as session:
            waiting = session.get(Job, accepted.operation_id)
            assert waiting is not None and waiting.state == LifecycleState.BACKOFF
            assert waiting.next_action_at is not None
        assert receipt_path.exists()
        assert storage.existing_archive(ARCHIVE_SHA, len(ARCHIVE)).is_file()
    # Remove the injected unreadable reference; the same removal observes again.
    with sessions.begin() as session:
        broken = session.get(
            FleetProfileApplication, "00000000-0000-4000-8000-000000000032"
        )
        assert broken is not None
        session.delete(broken)
    for _ in range(4):
        service.advance_removals(limit=1)
        observed[0] += timedelta(seconds=60)
    with sessions() as session:
        completed = session.get(Job, accepted.operation_id)
        assert completed is not None and completed.state == LifecycleState.SUCCEEDED
    assert not receipt_path.exists()
    fresh = service.start(
        "damaged-reference", actor="operator", request_id=str(uuid.uuid4())
    )
    assert fresh.id != accepted.operation_id
    engine.dispose()


@pytest.mark.parametrize("fault", ["publication-lock", "receipt-unlink"])
def test_recipe_removal_transient_storage_failure_uses_automatic_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'retry-removal.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    now = [datetime.now(UTC)]
    receipt = _reference_receipt()
    with sessions.begin() as session:
        revision = _add_revision(session, "rev-retry-removal", recipe)
        _add_head(session, revision)
    storage = FilesystemRuntimeImageStorage(tmp_path / "managed-retry")
    place_test_image(storage, ARCHIVE_SHA, len(ARCHIVE))
    receipt_path = storage.root / f"{ARCHIVE_SHA}.receipt.json"
    receipt_path.write_text(json.dumps(receipt.model_dump(mode="json")))
    failure_code = "runtime_image.publication_contended"
    removal_started = False
    if fault == "publication-lock":
        original_lock = storage.publication_lock
        contended = False

        def contend_once(archive_sha256: str):
            nonlocal contended
            if removal_started and not contended:
                contended = True
                raise RuntimeImagePreparationError(
                    failure_code,
                    "another owner holds the exact image publication lock",
                    retryable=True,
                    recovery_actions=("retry",),
                )
            return original_lock(archive_sha256)

        monkeypatch.setattr(storage, "publication_lock", contend_once)
    else:
        failure_code = "runtime_image.removal_storage_failed"
        original_unlink = Path.unlink
        unlink_failed = False

        def unlink_with_one_failure(path: Path, missing_ok: bool = False) -> None:
            nonlocal unlink_failed
            if removal_started and path == receipt_path and not unlink_failed:
                unlink_failed = True
                raise PermissionError("injected receipt-unlink failure")
            original_unlink(path, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", unlink_with_one_failure)
    service = _service(
        sessions,
        storage=storage,
        authority=lambda *_args, **_kwargs: (recipe, _runtime()),
        clock=lambda: now[0],
    )
    request_key = "00000000-0000-4000-8000-000000000037"
    accepted = remove_after_review(
        service,
        recipe.identity.slug,
        actor="operator",
        request_id=request_key,
        with_model=False,
    )
    assert accepted["state"] == "queued"
    removal_started = True
    service.advance_removals(limit=1)
    waiting = service.get_operator_request(request_key, actor="operator")
    assert isinstance(waiting, RecipeCacheRemovalStatus)
    waiting = waiting.model_dump(mode="json", exclude_none=True)
    failure = require_mapping(waiting["failure"], "removal failure")
    retry_time = failure["retry_time"]
    assert waiting["state"] == LifecycleState.BACKOFF
    assert failure["retryable"] is True
    assert failure["recovery_actions"] == []
    assert waiting["next_actions"] == []
    assert isinstance(retry_time, str)
    assert storage.existing_archive(ARCHIVE_SHA, len(ARCHIVE)).is_file()
    assert receipt_path.exists()

    now[0] = datetime.fromisoformat(retry_time) + timedelta(seconds=1)
    assert service.advance_removals(limit=1) == 1
    # Only the receipt is retired; the image's blobs wait for garbage collection.
    assert storage.existing_archive(ARCHIVE_SHA, len(ARCHIVE)).is_file()
    assert not receipt_path.exists()
    assert service.advance_removals(limit=1) == 1
    completed = service.get_operator_request(request_key, actor="operator")
    assert isinstance(completed, RecipeCacheRemovalStatus)
    completed = completed.model_dump(mode="json", exclude_none=True)
    assert completed["state"] == "succeeded"
    engine.dispose()


@pytest.mark.usefixtures("damaged_json_rows")
def test_damaged_removal_owner_does_not_hold_up_later_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, _, service, selector = _empty_recipe_removal_owner(tmp_path)
    bad_key = "00000000-0000-4000-8000-000000000035"
    good_key = "00000000-0000-4000-8000-000000000036"
    remove_after_review(service, selector, actor="operator", request_id=bad_key)
    remove_after_review(service, selector, actor="operator", request_id=good_key)
    with sessions.begin() as session:
        bad = session.scalar(select(Job).where(Job.request_id == bad_key))
        assert bad is not None
        bad.payload = dict(bad.payload) | {"padding": "x" * 5000}
        bad.updated_at = datetime.now(UTC) - timedelta(seconds=1)

    # Recoverable extra bookkeeping is normalized without retaining the queue.
    for _ in range(6):
        service.advance_removals(limit=2)

    with sessions() as session:
        bad = session.scalar(select(Job).where(Job.request_id == bad_key))
        good = session.scalar(select(Job).where(Job.request_id == good_key))
        assert bad is not None and bad.state == LifecycleState.SUCCEEDED
        assert good is not None and good.state == LifecycleState.SUCCEEDED
        from vonk_control.models import ArtifactLifecycleGate

        assert (
            session.scalar(
                select(ArtifactLifecycleGate).where(
                    ArtifactLifecycleGate.removal_owner_id == bad.id
                )
            )
            is None
        )
    with sessions() as session:
        ended = session.scalar(select(Job).where(Job.request_id == bad_key))
        assert ended is not None and ended.state == LifecycleState.SUCCEEDED
    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        cast(Job | RecipeImageAvailabilityView, ended),
        end=lambda receipt: receipt,
        fresh=lambda _: service.start(
            "revision-image",
            actor="operator",
            request_id=str(uuid.uuid4()),
        ),
    )


@pytest.mark.parametrize(
    ("replay_selector", "replay_actor", "replay_with_model"),
    [
        ("vonk-forge/synthetic-image-job", "operator", False),
        ("vonk-forge/synthetic-tiny-build", "different-operator", False),
        ("vonk-forge/synthetic-tiny-build", "operator", True),
    ],
    ids=["selector", "actor", "with-model"],
)
def test_recipe_removal_request_key_rejects_changed_intent(
    tmp_path: Path,
    replay_selector: str,
    replay_actor: str,
    replay_with_model: bool,
) -> None:
    """A successful remove key must not authorize a different later intent.

    The disposable database contains two catalog recipes and no builds or
    managed artifacts, so this owner-boundary regression
    cannot remove real or test artifact bytes.
    """

    sessions, storage, service, original_selector = _empty_recipe_removal_owner(
        tmp_path
    )
    request_id = "00000000-0000-4000-8000-000000000016"
    original = remove_after_review(
        service,
        original_selector,
        actor="operator",
        request_id=request_id,
        with_model=False,
    )
    assert original["state"] == "queued"
    assert service.advance_removals(limit=1) == 1
    original = service.get_operator_request(request_id, actor="operator")
    assert isinstance(original, RecipeCacheRemovalStatus)
    original = original.model_dump(mode="json", exclude_none=True)
    assert original["state"] == "succeeded"
    assert not any(path.is_file() for path in storage.root.rglob("*"))

    with pytest.raises(RecipeImageAvailabilityError):
        remove_after_review(
            service,
            replay_selector,
            actor=replay_actor,
            request_id=request_id,
            with_model=replay_with_model,
        )
    with sessions() as session:
        ended = session.scalar(select(Job).where(Job.request_id == request_id))
        assert ended is not None and ended.state == "succeeded"
    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        cast(Job | RecipeImageAvailabilityView, ended),
        end=lambda receipt: receipt,
        fresh=lambda _: service.start(
            "revision-image",
            actor="operator",
            request_id=str(uuid.uuid4()),
        ),
    )


def test_recipe_removal_request_key_replays_before_resolving_current_head(
    tmp_path: Path,
) -> None:
    sessions, _, service, selector = _empty_recipe_removal_owner(tmp_path)
    request_id = "00000000-0000-4000-8000-000000000017"
    original = remove_after_review(
        service, selector, actor="operator", request_id=request_id, with_model=False
    )
    assert original["state"] == "queued"
    assert service.advance_removals(limit=1) == 1
    observed = service.get_operator_request(request_id, actor="operator")
    assert isinstance(observed, RecipeCacheRemovalStatus)
    observed = observed.model_dump(mode="json", exclude_none=True)
    original = observed
    with sessions.begin() as session:
        head = session.scalar(
            select(CatalogDocumentHead).where(
                CatalogDocumentHead.slug == "synthetic-tiny-build"
            )
        )
        assert head is not None
        head.active_revision_id = None

    replay = remove_after_review(
        service,
        selector,
        actor="operator",
        request_id=request_id,
        with_model=False,
    )

    assert replay == original
    assert replay["with_model"] is False
    with sessions() as session:
        ended = session.scalar(select(Job).where(Job.request_id == request_id))
        assert ended is not None and ended.state == "succeeded"
    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        cast(Job | RecipeImageAvailabilityView, ended),
        end=lambda receipt: receipt,
        fresh=lambda _: service.start(
            "revision-image",
            actor="operator",
            request_id=str(uuid.uuid4()),
        ),
    )


def test_active_recipe_removal_admits_independent_request_and_replays_accepted_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches refusal of a newer request or replay through mutable observations."""

    recipe = _recipe("recipe-source-build.json")
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'active-review.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = [datetime.now(UTC)]
    receipt = _reference_receipt()
    with sessions.begin() as session:
        revision = _add_revision(session, "revision-active-review", recipe)
        _add_head(session, revision)

    storage = FilesystemRuntimeImageStorage(tmp_path / "review-managed")
    place_test_image(storage, ARCHIVE_SHA, len(ARCHIVE))
    (storage.root / f"{ARCHIVE_SHA}.receipt.json").write_text(
        json.dumps(receipt.model_dump(mode="json")), encoding="utf-8"
    )
    service = _service(
        sessions,
        storage=storage,
        authority=lambda *_args, **_kwargs: (recipe, _runtime()),
        clock=lambda: now[0],
    )
    selector = recipe.identity.slug
    before = service.review_removal(selector, with_model=False)
    assert before.blockers == []
    request_key = "00000000-0000-4000-8000-000000000041"
    accepted = service.remove_selector(
        selector,
        actor="operator",
        request_id=request_key,
        with_model=False,
    )
    assert accepted.state == LifecycleState.QUEUED
    assert storage.existing_archive(ARCHIVE_SHA, len(ARCHIVE)).is_file()

    changed_key = "00000000-0000-4000-8000-000000000042"
    newer = service.remove_selector(
        selector,
        actor="operator",
        request_id=changed_key,
        with_model=False,
    )
    assert newer.operation_id != accepted.operation_id
    with sessions() as session:
        assert (
            session.scalar(select(Job).where(Job.request_id == changed_key)) is not None
        )
        assert (
            session.scalar(select(Job).where(Job.request_id == request_key)) is not None
        )
    assert storage.existing_archive(ARCHIVE_SHA, len(ARCHIVE)).is_file()

    def no_mutable_review(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("same-key recovery must precede mutable review")

    with monkeypatch.context() as fault:
        fault.setattr(service, "review_removal", no_mutable_review)
        replay = service.remove_selector(
            selector,
            actor="operator",
            request_id=request_key,
            with_model=False,
        )
        assert replay.operation_id == accepted.operation_id

    # Both requests keep their identities. After the first removes the receipt,
    # the second must retain its unknown observation instead of ending by count.
    for _ in range(12):
        service.advance_removals(limit=10)
        now[0] += timedelta(minutes=1)
    waiting = [
        service.get_operator_request(request.request_key, actor="operator")
        for request in (accepted, newer)
    ]
    assert any(item.state == LifecycleState.BACKOFF for item in waiting)
    # Restore the missing metadata observation, without changing either intent.
    (storage.root / f"{ARCHIVE_SHA}.receipt.json").write_text(receipt.model_dump_json())
    for _ in range(12):
        now[0] += timedelta(minutes=1)
        service.advance_removals(limit=10)
    for request in (accepted, newer):
        ended = service.get_operator_request(request.request_key, actor="operator")
        assert isinstance(ended, RecipeCacheRemovalStatus)
        assert ended.state == LifecycleState.SUCCEEDED
        assert_ended_without_blocking(
            SimpleNamespace(sessions=sessions),
            cast(RecipeCacheRemovalStatus | RecipeImageAvailabilityView, ended),
            end=lambda receipt: receipt,
            fresh=lambda _: service.start(
                "revision-active-review",
                actor="operator",
                request_id=str(uuid.uuid4()),
            ),
        )


def _renamed(recipe: RecipeDefinition, slug: str) -> RecipeDefinition:
    document = _published(recipe)
    document["identity"]["slug"] = slug
    return RecipeDefinition.model_validate(document)


def test_a_newer_request_for_an_image_supersedes_its_running_removal(
    tmp_path: Path,
) -> None:
    """Catches the newest request losing to an older removal of its image.

    The request's recorded recipe digest is the fact: a digest of its parsed
    recipe's full dump is not the published document's digest, so re-hashing
    it refused every supersede.
    """

    engine = create_engine(
        f"sqlite:///{tmp_path / 'control.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    now = datetime.now(UTC)
    recipe = _recipe("recipe-source-build.json")
    service, sessions, storage = _archive_removal_service(
        engine, tmp_path, [recipe], lambda: now
    )
    _seed_archives(sessions, storage, "revision-yield", recipe, 1)
    removal_id = str(uuid.uuid4())
    remove_after_review(
        service,
        recipe.identity.slug,
        actor="operator",
        request_id=removal_id,
        with_model=False,
    )
    service.advance_removals(limit=1)  # the removal holds its image's gate
    service.start("revision-yield", actor="operator", request_id=str(uuid.uuid4()))

    service.reconcile_requested_removals()

    removal = service.get_operator_request(removal_id, actor="operator")
    assert isinstance(removal, RecipeCacheRemovalStatus)
    assert removal.state == LifecycleState.CANCELLED


def test_postgres_recipe_removal_fence_is_committed_before_first_unlink(
    tmp_path: Path, postgres_engine, monkeypatch
) -> None:
    """The removal fence is committed before each managed image unlink."""

    now = datetime.now(UTC)
    recipe = _recipe("recipe-source-build.json")
    service, sessions, storage = _archive_removal_service(
        postgres_engine, tmp_path, [recipe], lambda: now
    )
    digests, receipts = _seed_archives(sessions, storage, "revision-fence", recipe, 1)
    request_id = str(uuid.uuid4())
    original_remove = storage.remove_published
    removal_calls: list[str] = []

    def require_committed_fence_before_unlink(archive_digest: str) -> int:
        owner = _removal_owner(sessions, request_id)
        with sessions.begin() as observer:
            assert check_removal_fence_nowait(
                observer,
                ArtifactIdentity("runtime-image", archive_digest),
                owner_kind="recipe-image-job",
                owner_id=str(
                    observer.scalar(select(Job.id).where(Job.request_id == request_id))
                ),
                fence=owner.plan.intent.removal_fence,
            )
        removal_calls.append(archive_digest)
        return original_remove(archive_digest)

    monkeypatch.setattr(
        storage, "remove_published", require_committed_fence_before_unlink
    )
    accepted = remove_after_review(
        service,
        recipe.identity.slug,
        actor="operator",
        request_id=request_id,
        with_model=False,
    )
    assert accepted["state"] == "queued"
    assert all(path.exists() for path in receipts)
    for _ in range(5):
        service.advance_removals(limit=1)
    assert removal_calls == digests
    assert not any(path.exists() for path in receipts)
    ended = service.get_operator_request(request_id, actor="operator")
    assert isinstance(ended, RecipeCacheRemovalStatus)
    assert ended.state == LifecycleState.SUCCEEDED
    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        cast(RecipeCacheRemovalStatus | RecipeImageAvailabilityView, ended),
        end=lambda receipt: receipt,
        fresh=lambda _: service.start(
            "revision-fence",
            actor="operator",
            request_id=str(uuid.uuid4()),
        ),
    )


def test_postgres_recipe_removal_retires_all_archives_in_one_step(
    tmp_path: Path, postgres_engine, monkeypatch
) -> None:
    """N archives take one worker step and one checkpoint write, not N."""

    now = datetime.now(UTC)
    recipe = _recipe("recipe-source-build.json")
    service, sessions, storage = _archive_removal_service(
        postgres_engine, tmp_path, [recipe], lambda: now
    )
    digests, receipts = _seed_archives(sessions, storage, "revision-batch", recipe, 3)
    request_id = str(uuid.uuid4())
    persists: list[int] = []
    original_complete = service._complete_recipe_image_removal

    def counted(*args: Any, **kwargs: Any) -> bool:
        persists.append(kwargs["completed"])
        return original_complete(*args, **kwargs)

    monkeypatch.setattr(service, "_complete_recipe_image_removal", counted)
    remove_after_review(
        service,
        recipe.identity.slug,
        actor="operator",
        request_id=request_id,
        with_model=False,
    )
    assert _removal_owner(sessions, request_id).plan.image_archives == digests
    seen: list[int] = []
    for _ in range(5):
        service.advance_removals(limit=1)
        seen.append(_removal_owner(sessions, request_id).checkpoint.image_index)
    assert set(seen) <= {0, 3} and 3 in seen
    assert persists == [3]
    assert not any(path.exists() for path in receipts)
    ended = service.get_operator_request(request_id, actor="operator")
    assert isinstance(ended, RecipeCacheRemovalStatus)
    assert ended.state == LifecycleState.SUCCEEDED
    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        cast(RecipeCacheRemovalStatus | RecipeImageAvailabilityView, ended),
        end=lambda receipt: receipt,
        fresh=lambda _: service.start(
            "revision-batch",
            actor="operator",
            request_id=str(uuid.uuid4()),
        ),
    )


def test_postgres_recipe_removal_batch_budget_is_shared_between_removals(
    tmp_path: Path, postgres_engine, monkeypatch, frozen_pass_budgets
) -> None:
    """One step deadline covers every removal in it; each still retires one."""

    now = datetime.now(UTC)
    first = _recipe("recipe-source-build.json")
    second = _renamed(first, "second-recipe")
    service, sessions, storage = _archive_removal_service(
        postgres_engine, tmp_path, [first, second], lambda: now
    )
    _seed_archives(sessions, storage, "revision-budget-1", first, 3)
    _seed_archives(sessions, storage, "revision-budget-2", second, 3, salt=b" other")
    original_remove = storage.remove_published

    def slow_remove(archive_digest: str) -> int:
        result = original_remove(archive_digest)
        frozen_pass_budgets.advance(3.0)  # two unlinks spend the 5 s budget
        return result

    monkeypatch.setattr(storage, "remove_published", slow_remove)
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    for request_id, recipe in zip(ids, (first, second), strict=True):
        remove_after_review(
            service,
            recipe.identity.slug,
            actor="operator",
            request_id=request_id,
            with_model=False,
        )
    service.reconcile_requested_removals()
    service.advance_removals(limit=2)
    # A shared deadline: one removal retires two archives, the other only the
    # one that always runs. A per-removal budget would retire two in each.
    indexes = sorted(
        _removal_owner(sessions, request_id).checkpoint.image_index
        for request_id in ids
    )
    assert indexes == [1, 2]
    for _ in range(8):
        service.advance_removals(limit=2)
    for request_id, revision_id in zip(
        ids, ("revision-budget-1", "revision-budget-2"), strict=True
    ):
        ended = service.get_operator_request(request_id, actor="operator")
        assert isinstance(ended, RecipeCacheRemovalStatus)
        assert ended.state == LifecycleState.SUCCEEDED
        assert_ended_without_blocking(
            SimpleNamespace(sessions=sessions),
            cast(RecipeCacheRemovalStatus | RecipeImageAvailabilityView, ended),
            end=lambda receipt: receipt,
            fresh=lambda _, revision_id=revision_id: service.start(
                revision_id, actor="operator", request_id=str(uuid.uuid4())
            ),
        )
