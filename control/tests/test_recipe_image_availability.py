from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.bounded_json import require_mapping, require_sequence
from vonk_control.catalog_entities import _build_projection
from vonk_control.catalog_revision_contract import write_catalog_projection
from vonk_control.failure_evidence import failure_receipt
from vonk_control.model_cache import ModelCacheError
from vonk_control.model_cache_progress import cache_progress
from vonk_control.models import (
    AgentNode,
    Base,
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    Job,
    RecipeBuild,
    RuntimeImageAuthorization,
)
from vonk_control.recipe_image_availability import (
    SUPERSEDED_PREPARATION_CODE,
    RecipeImageAvailabilityError,
    RecipeImageAvailabilityService,
    _retryable,
)
from vonk_control.recipe_image_availability_api import _recipe_error, _view_document
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    PulledImageEvidence,
    RuntimeImagePreparationError,
    prepare_runtime_image,
    resolve_persisted_runtime_image_receipt,
)
from vonk_forge_contracts import RecipeDefinition, content_sha256

IMAGE_DIGEST = "sha256:" + "d" * 64
PLATFORM_DIGEST = "sha256:" + "e" * 64
CONFIG_DIGEST = "sha256:" + "c" * 64
ARCHIVE = b"availability image archive"
ARCHIVE_SHA = hashlib.sha256(ARCHIVE).hexdigest()


def _recipe(name: str) -> RecipeDefinition:
    raw = json.loads(
        files("vonk_forge_contracts").joinpath("examples", name).read_text()
    )
    return RecipeDefinition.model_validate(raw)


def _runtime() -> dict[str, object]:
    return {
        "architecture": "linux/arm64",
        "interface": "vonk.runtime.v1",
        "image_bytes": len(ARCHIVE),
    }


def _build_runtime() -> dict[str, object]:
    return _runtime() | {"build_input_sha256": "f" * 64}


def _progress_members(value: object) -> list[Mapping[str, object]]:
    """Read the decoded progress member array as mappings, in order."""

    return [
        require_mapping(member, "progress member")
        for member in require_sequence(value, "progress members")
    ]


@pytest.mark.parametrize(
    ("code", "explicit_retryable", "detail", "expected"),
    [
        ("runtime_image.cache_missing", False, "network timeout", False),
        ("recipe_image.identity_conflict", True, "network timeout", False),
        ("recipe_image.other_failure", None, "network timeout", True),
    ],
)
def test_explicit_retry_decision_precedes_fallback_classification(
    code: str,
    explicit_retryable: bool | None,
    detail: str,
    expected: bool,
) -> None:
    error = RecipeImageAvailabilityError(
        code,
        detail,
        retryable=explicit_retryable,
    )

    assert _retryable(error) is expected


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


class Transport:
    def __init__(self, payload: bytes = ARCHIVE) -> None:
        self.calls = 0
        self.payload = payload

    def pull_and_export(
        self, reference: str, destination: Path, **_: object
    ) -> PulledImageEvidence:
        self.calls += 1
        destination.write_bytes(self.payload)
        return PulledImageEvidence(
            manifest_digest=PLATFORM_DIGEST,
            requested_manifest_digest=IMAGE_DIGEST,
            config_id=CONFIG_DIGEST,
            local_reference=reference,
            architecture="linux/arm64",
            runtime_interface="v1",
            archive_sha256=hashlib.sha256(self.payload).hexdigest(),
            archive_bytes=len(self.payload),
        )

    def inspect_archive(self, archive: Path, **_: object) -> PulledImageEvidence:
        raise AssertionError(archive)


def _add_revision(
    session: Session, revision_id: str, recipe: RecipeDefinition
) -> CatalogDocumentRevision:
    projected = {
        "title": recipe.metadata.title,
        "description": recipe.metadata.description,
        "tags": list(recipe.metadata.tags),
        "runtime_engine": recipe.runtime.engine,
        "topology": recipe.topology.model_dump(mode="json"),
    }
    projected.update(_build_projection(recipe))
    revision = CatalogDocumentRevision(
        id=revision_id,
        document_id="document-" + revision_id,
        kind="recipe",
        publisher=recipe.identity.publisher,
        slug=recipe.identity.slug,
        revision_number=1,
        schema_version=2,
        state="active",
        document=recipe.model_dump(mode="json"),
        content_digest=content_sha256(recipe),
        artifact_key="b" * 64,
        execution_key="a" * 64,
        projected=write_catalog_projection(projected, kind="recipe"),
        created_by="test",
        created_at=datetime.now(UTC),
    )
    session.add(revision)
    return revision


def _add_head(
    session: Session, revision: CatalogDocumentRevision
) -> CatalogDocumentHead:
    session.add(
        CatalogDocument(
            id=revision.document_id,
            kind="recipe",
            publisher=revision.publisher,
            slug=revision.slug,
            title="Recipe",
            created_by="test",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    head = CatalogDocumentHead(
        kind="recipe",
        publisher=revision.publisher,
        slug=revision.slug,
        active_revision_id=revision.id,
        generation=1,
    )
    session.add(head)
    return head


def _add_recipe_successors(
    session: Session,
    *,
    older_id: str,
    older: RecipeDefinition,
    newer_id: str,
    newer: RecipeDefinition,
) -> tuple[CatalogDocumentRevision, CatalogDocumentRevision]:
    """Two revisions of one recipe document with the head on the older one."""

    older_revision = _add_revision(session, older_id, older)
    newer_revision = _add_revision(session, newer_id, newer)
    document_id = older_revision.document_id
    newer_revision.document_id = document_id
    newer_revision.revision_number = 2
    _add_head(session, older_revision)
    return older_revision, newer_revision


def _set_active_head(session: Session, revision_id: str) -> None:
    """Move the authoritative head, as a catalogue sync would."""

    revision = session.get(CatalogDocumentRevision, revision_id)
    assert revision is not None
    head = session.scalar(
        select(CatalogDocumentHead).where(
            CatalogDocumentHead.kind == revision.kind,
            CatalogDocumentHead.publisher == revision.publisher,
            CatalogDocumentHead.slug == revision.slug,
        )
    )
    assert head is not None
    head.active_revision_id = revision_id


def _successor(recipe: RecipeDefinition, title: str) -> RecipeDefinition:
    return recipe.model_copy(
        update={"metadata": recipe.metadata.model_copy(update={"title": title})}
    )


def test_logical_recipe_selectors_follow_the_head_without_losing_exact_revisions(
    tmp_path,
):
    recipe = _recipe("recipe-image.json")
    old_recipe = recipe.model_copy(
        update={
            "metadata": recipe.metadata.model_copy(
                update={"description": "Previous accepted recipe"}
            )
        }
    )
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
    service = RecipeImageAvailabilityService(
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
    assert service._resolve_recipe_selector(content_sha256(old_recipe)) == old_id
    other = recipe.model_copy(
        update={
            "identity": recipe.identity.model_copy(
                update={"publisher": "another-publisher"}
            )
        }
    )
    with sessions.begin() as session:
        _add_head(session, _add_revision(session, "another-recipe", other))
    with pytest.raises(RecipeImageAvailabilityError) as ambiguous:
        service.start_selector(
            recipe.identity.slug, actor="operator", request_id="ambiguous-name"
        )
    assert ambiguous.value.code == "recipe_image.selector_ambiguous"
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
    with pytest.raises(RecipeImageAvailabilityError) as missing:
        service.start_selector(qualified, actor="operator", request_id="missing-head")
    assert missing.value.code == "recipe_image.selector_missing"


def test_force_download_skips_verified_cache_but_preserves_archive(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-image.json")
    storage = FilesystemRuntimeImageStorage(tmp_path)
    transport = Transport()
    first = prepare_runtime_image(
        recipe, runtime=_runtime(), storage=storage, transport=transport
    )
    second = prepare_runtime_image(
        recipe, runtime=_runtime(), storage=storage, transport=transport
    )
    forced = prepare_runtime_image(
        recipe, runtime=_runtime(), storage=storage, transport=transport, force=True
    )

    assert first == second == forced
    assert transport.calls == 2
    assert Path(first.archive_path).read_bytes() == ARCHIVE


@pytest.mark.parametrize("revoked", [None, "receipt", "authorization"])
def test_download_after_cache_removal_restores_only_unrevoked_authority(
    tmp_path, revoked
):
    recipe = _recipe("recipe-image.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_head(session, _add_revision(session, "revision-restore", recipe))
    storage = FilesystemRuntimeImageStorage(tmp_path)
    transport = Transport()
    service = RecipeImageAvailabilityService(
        sessions,
        storage=storage,
        transport=transport,
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        clock=lambda: datetime.now(UTC),
        automatic_attempt_limit=1,
    )
    first = service.start_selector(
        recipe.identity.slug, actor="operator", request_id="1" * 36
    )
    service.run_pending()
    assert service.get(first.id).state == "succeeded"
    cached = storage.read_receipt(ARCHIVE_SHA)
    with sessions.begin() as session:
        # One verified archive has one authorization here; the storage receipt
        # beside the bytes is the immutable observation and has no state.
        authorization = session.scalar(select(RuntimeImageAuthorization))
        assert authorization is not None
        archive_sha256 = authorization.oci_archive_sha256
        authorization_id = authorization.id
        execution_key = authorization.effective_execution_key
        if revoked is not None:
            authorization.state = "revoked"
    service.remove_selector(recipe.identity.slug, actor="operator", request_id="2" * 36)
    # Removal takes the bytes and the managed-storage receipt with them; SQL
    # keeps the authorization decision, which a restore re-checks.
    assert not (storage.root / ARCHIVE_SHA).exists()
    assert not (storage.root / f"{ARCHIVE_SHA}.receipt.json").exists()
    # Restart and use the real download path, including SQL receipt persistence.
    restarted = RecipeImageAvailabilityService(
        sessions,
        storage=storage,
        transport=transport,
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        clock=lambda: datetime.now(UTC),
        automatic_attempt_limit=1,
    )
    download = restarted.start_selector(
        recipe.identity.slug, actor="operator", request_id="3" * 36
    )
    restarted.run_pending()
    result = restarted.get(download.id)
    if revoked is None:
        assert result.state == "succeeded", result.failure
        assert (storage.root / ARCHIVE_SHA).read_bytes() == ARCHIVE
        with sessions() as session:
            restored = resolve_persisted_runtime_image_receipt(
                session,
                recipe_revision_id="revision-restore",
                current_content_digest=content_sha256(recipe),
                effective_execution_key=execution_key,
                receipt=cached,
            )
            assert restored.oci_archive_sha256 == archive_sha256
            authorization = session.get(RuntimeImageAuthorization, authorization_id)
            assert authorization is not None and authorization.state == "authorized"
    else:
        assert result.state == "failed"
        assert result.failure is not None
        assert result.failure["code"] == ("runtime_image.authorization_revoked")


def test_forced_digest_failure_does_not_replace_valid_archive(tmp_path: Path) -> None:
    recipe = _recipe("recipe-image.json")
    storage = FilesystemRuntimeImageStorage(tmp_path)
    transport = Transport()
    first = prepare_runtime_image(
        recipe, runtime=_runtime(), storage=storage, transport=transport
    )

    class Wrong(Transport):
        def pull_and_export(
            self, reference: str, destination: Path, **_: object
        ) -> PulledImageEvidence:
            destination.write_bytes(b"wrong")
            return PulledImageEvidence(
                manifest_digest="sha256:" + "f" * 64,
                requested_manifest_digest="sha256:" + "a" * 64,
                config_id=CONFIG_DIGEST,
                local_reference=reference,
                architecture="linux/arm64",
                runtime_interface="v1",
                archive_sha256=hashlib.sha256(b"wrong").hexdigest(),
                archive_bytes=5,
            )

    with pytest.raises(RuntimeImagePreparationError):
        prepare_runtime_image(
            recipe, runtime=_runtime(), storage=storage, transport=Wrong(), force=True
        )
    assert Path(first.archive_path).read_bytes() == ARCHIVE


def test_build_failure_is_bounded_and_exposes_step_and_retry_contract(
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

    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=authority,
        builder=builder,
        clock=lambda: datetime.now(UTC),
        automatic_attempt_limit=1,
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
    assert failed.state == "failed"
    assert failed.failure is not None
    assert failed.result is None
    assert failed.failure["code"] == "recipe_image.build_failed"
    assert failed.failure["retryable"] is True
    assert failed.failure["log_excerpt"] == "Step 4: compiler failed"
    assert failed.supported_actions == ("retry",)
    response = _view_document(failed)
    assert response.failure is not None
    assert response.failure.code == "recipe_image.build_failed"
    with sessions.begin() as session:
        row = session.get(Job, queued.id)
        assert row is not None
        row.payload = {
            key: value for key, value in row.payload.items() if key != "failure"
        }
    restarted = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=authority,
        builder=builder,
        clock=lambda: datetime.now(UTC),
        automatic_attempt_limit=1,
    )
    with pytest.raises(ValueError, match="requires failure evidence"):
        restarted.get(queued.id)


def test_database_integrity_failure_names_the_violated_constraint(
    tmp_path: Path,
) -> None:
    """A database failure must not be reported as SQLAlchemy's own slug.

    The availability worker records the verified archive through
    ``persist_runtime_image_receipt``, which inserts into
    ``runtime_image_authorizations`` and flushes. When the database refuses
    that write the raw ``sqlalchemy.exc.IntegrityError`` reaches ``_fail``.
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
        _add_revision(session, "revision-integrity-failure", recipe)
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
        (storage.root / ARCHIVE_SHA).write_bytes(ARCHIVE)
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
                requested_manifest_digest=None,
                config_id=CONFIG_DIGEST,
                local_reference="docker-archive:" + str(archive),
                architecture=expected_architecture,
                runtime_interface=expected_runtime_interface,
                archive_sha256=expected_archive_sha256,
                archive_bytes=expected_archive_bytes,
            )

    # Built exactly as the DBAPI layer builds it (``statement``, ``params``,
    # ``orig``): the driver error is the ``orig`` the failure reporter must
    # surface, while the SQLAlchemy wrapper contributes the empty ``detail``
    # list and the ``gkpj`` slug that used to win.
    def receipt_writer(*_args: object) -> None:
        refusal = sqlite3.IntegrityError(
            "UNIQUE constraint failed: runtime_image_authorizations."
            "recipe_revision_id, runtime_image_authorizations."
            "effective_execution_key, runtime_image_authorizations."
            "oci_archive_sha256"
        )
        error = IntegrityError(None, None, refusal)
        assert error.code == "gkpj"
        assert error.detail == []
        raise error

    service = RecipeImageAvailabilityService(
        sessions,
        storage=storage,
        authority=lambda recipe_revision_id, *, force=False: (
            recipe,
            _build_runtime(),
        ),
        transport=BuildTransport(),
        builder=builder,
        receipt_writer=receipt_writer,
        clock=lambda: datetime.now(UTC),
        automatic_attempt_limit=1,
    )
    queued = service.start(
        "revision-integrity-failure",
        actor="operator",
        request_id="i" * 36,
    )
    assert service.run_pending() == 1
    failed = service.get(queued.id)
    assert failed.state == "failed"
    failure = require_mapping(failed.failure, "failure")
    assert failure["code"] != "gkpj"
    assert failure["code"] == "integrityerror"
    detail = failure["detail"]
    assert isinstance(detail, str)
    assert detail != "[]"
    assert "UNIQUE constraint failed" in detail
    assert "runtime_image_authorizations" in detail
    excerpt = failure["log_excerpt"]
    assert isinstance(excerpt, str) and "UNIQUE constraint failed" in excerpt
    view = _view_document(failed)
    assert view.failure is not None
    assert view.failure.code == "integrityerror"
    # The operator-facing evidence bundle reuses this contract, so it must
    # carry the failure instead of a summary of "[]" -- including the table
    # name, which names the constraint the operator has to repair.
    receipt = failure_receipt(failure)
    assert receipt.error_code == "integrityerror"
    assert receipt.summary != "[]"
    assert "runtime_image_authorizations" in receipt.summary


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
        _add_revision(session, "revision-missing-build-archive", recipe)
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
        (storage.root / ARCHIVE_SHA).write_bytes(ARCHIVE)
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
            assert archive.read_bytes() == ARCHIVE
            return PulledImageEvidence(
                manifest_digest=IMAGE_DIGEST,
                requested_manifest_digest=None,
                config_id=CONFIG_DIGEST,
                local_reference="docker-archive:" + str(archive),
                architecture=expected_architecture,
                runtime_interface=expected_runtime_interface,
                archive_sha256=expected_archive_sha256,
                archive_bytes=expected_archive_bytes,
            )

    service = RecipeImageAvailabilityService(
        sessions,
        storage=storage,
        authority=lambda recipe_revision_id, *, force=False: (
            recipe,
            _build_runtime(),
        ),
        transport=BuildTransport(),
        builder=builder,
        receipt_writer=lambda *_args: None,
        clock=lambda: datetime.now(UTC),
        automatic_attempt_limit=1,
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
    assert (storage.root / ARCHIVE_SHA).read_bytes() == ARCHIVE


def test_remove_recipe_cancels_build_and_publishes_no_late_receipt(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_head(session, _add_revision(session, "revision-remove-build", recipe))

    service = RecipeImageAvailabilityService(
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

    result = service.remove_selector(
        recipe.identity.slug,
        actor="operator",
        request_id="c" * 36,
    )
    assert result["operation_id"]
    assert queued.id in require_sequence(
        result["cancelled_operations"], "cancelled operations"
    )
    assert result["cancelled_builds"] == ["00000000-0000-4000-8000-000000000901"]
    assert result["preserved"] == [
        "profile-assignments",
        "spark-local-copies",
        "model-download",
    ]
    assert service.run_pending() == 0
    assert service.get(queued.id).state == "cancelled"
    observed = service.get_operator_operation(str(result["operation_id"]))
    assert isinstance(observed, dict)
    assert observed["operation_id"] == result["operation_id"]
    with sessions() as session:
        build = session.get(RecipeBuild, "00000000-0000-4000-8000-000000000901")
        assert build is not None and build.state == "failed"
        assert session.scalars(select(RuntimeImageAuthorization)).all() == []


def test_builder_capacity_wait_remains_durable_queue_after_automatic_limit(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-capacity-wait", recipe)

    def builder(*_: object, **__: object) -> dict[str, object]:
        raise RecipeImageAvailabilityError(
            "recipe_image.build_capacity_wait",
            "all compatible builders are currently occupied",
            retryable=True,
            retry_after_seconds=1,
            recovery_actions=("resume", "retry"),
        )

    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _build_runtime()),
        builder=builder,
        clock=lambda: datetime.now(UTC),
        automatic_attempt_limit=1,
    )
    queued = service.start(
        "revision-capacity-wait", actor="operator", request_id="w" * 36
    )

    assert service.run_pending() == 1
    waiting = service.get(queued.id)
    assert waiting.state == "queued"
    assert waiting.failure is not None
    assert waiting.failure["code"] == "recipe_image.build_capacity_wait"

    with sessions.begin() as session:
        operation = session.get(Job, queued.id)
        assert operation is not None
        operation.payload = dict(operation.payload) | {
            "retry_after_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        }

    assert service.run_pending() == 1
    still_waiting = service.get(queued.id)
    assert still_waiting.state == "queued"
    assert still_waiting.attempt == 2
    assert still_waiting.failure is not None
    assert still_waiting.failure["code"] == "recipe_image.build_capacity_wait"


def test_failure_without_step_keeps_structured_retry_fields(tmp_path: Path) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-no-step", recipe)

    def builder(*_: object, **__: object) -> dict[str, object]:
        raise RecipeImageAvailabilityError(
            "model_cache.credentials_denied",
            "access remains denied",
            retryable=True,
            retry_time="2026-09-06T13:00:00+00:00",
            retry_after_seconds=60,
            recovery_actions=("check_access_and_resume",),
            log_excerpt="HF denied",
        )

    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _build_runtime()),
        builder=builder,
        clock=lambda: datetime.now(UTC),
        automatic_attempt_limit=1,
    )
    queued = service.start("revision-no-step", actor="operator", request_id="n" * 36)
    assert service.run_pending() == 1
    failed = service.get(queued.id)
    assert failed.failure is not None
    assert failed.failure["code"] == "model_cache.credentials_denied"
    assert failed.failure["retry_time"] == "2026-09-06T13:00:00+00:00"
    assert failed.failure["recovery_actions"] == ["check_access_and_resume"]


def test_expired_claim_is_reclaimable_after_restart(tmp_path: Path) -> None:
    recipe = _recipe("recipe-image.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-image", recipe)

    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        transport=Transport(),
        clock=lambda: datetime.now(UTC),
        claim_lease_seconds=10,
    )
    queued = service.start("revision-image", actor="operator", request_id="2" * 36)
    claim = service.claim_pending(owner_id="worker-a")
    assert claim and claim[0].operation_id == queued.id
    with sessions.begin() as session:
        operation = session.get(Job, queued.id)
        assert operation is not None
        operation.payload = dict(operation.payload) | {
            "claim_until": "2000-01-01T00:00:00+00:00"
        }
    reclaimed = service.claim_pending(owner_id="worker-b")
    assert reclaimed and reclaimed[0].claim_owner == "worker-b"


def test_claim_skips_backoff_and_renews_live_lease(tmp_path: Path) -> None:
    recipe = _recipe("recipe-image.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-backoff", recipe)
    now = datetime.now(UTC)
    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        transport=Transport(),
        clock=lambda: now,
    )
    queued = service.start("revision-backoff", actor="operator", request_id="3" * 36)
    with sessions.begin() as session:
        operation = session.get(Job, queued.id)
        assert operation is not None
        operation.payload = dict(operation.payload) | {
            "retry_after_at": (now + timedelta(minutes=5)).isoformat(),
        }
    assert service.claim_pending(owner_id="worker-a") == ()
    with sessions.begin() as session:
        operation = session.get(Job, queued.id)
        assert operation is not None
        operation.payload = dict(operation.payload) | {
            "retry_after_at": (now - timedelta(seconds=1)).isoformat(),
        }

    claim = service.claim_pending(owner_id="worker-a")
    assert claim and claim[0].operation_id == queued.id
    with sessions.begin() as session:
        operation = session.get(Job, queued.id)
        assert operation is not None
        before = operation.payload["claim_until"]
    assert service._renew_claim(queued.id, "worker-a") is True
    with sessions.begin() as session:
        operation = session.get(Job, queued.id)
        assert operation is not None
        assert operation.payload["claim_until"] == before


def test_claim_identity_uses_authoritative_image_and_running_claim_is_not_repeated(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-image.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-identity", recipe)
    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        transport=Transport(),
        clock=lambda: datetime.now(UTC),
    )
    service.start("revision-identity", actor="operator", request_id="4" * 36)
    claim = service.claim_pending(owner_id="worker-a")
    assert claim and claim[0].image_identity == IMAGE_DIGEST
    assert service.claim_pending(owner_id="worker-b") == ()


def test_postgres_claims_are_fenced_and_respect_build_capacity(
    tmp_path: Path, postgres_engine
) -> None:
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    image_recipe = _recipe("recipe-image.json")
    build_recipe = _recipe("recipe-source-build.json")
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add_all(
            [
                CatalogDocument(
                    id="document-pg-image",
                    kind="recipe",
                    publisher=image_recipe.identity.publisher,
                    slug=image_recipe.identity.slug,
                    title=image_recipe.metadata.title,
                    created_by="test",
                    created_at=now,
                    updated_at=now,
                ),
                CatalogDocument(
                    id="document-pg-build",
                    kind="recipe",
                    publisher=build_recipe.identity.publisher,
                    slug=build_recipe.identity.slug,
                    title=build_recipe.metadata.title,
                    created_by="test",
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        session.flush()
        image_revision = _add_revision(session, "revision-pg-image", image_recipe)
        image_revision.document_id = "document-pg-image"
        build_revision = _add_revision(session, "revision-pg-build", build_recipe)
        build_revision.document_id = "document-pg-build"

    def authority(recipe_revision_id: str, *, force: bool = False):
        del force
        if recipe_revision_id == "revision-pg-image":
            return image_recipe, _runtime()
        return build_recipe, _build_runtime()

    def new_service(root: Path) -> RecipeImageAvailabilityService:
        return RecipeImageAvailabilityService(
            sessions,
            storage=FilesystemRuntimeImageStorage(root),
            authority=authority,
            transport=Transport(),
            clock=lambda: datetime.now(UTC),
            max_parallel=2,
            max_parallel_builds=1,
            claim_lease_seconds=10,
        )

    first = new_service(tmp_path / "first")
    second = new_service(tmp_path / "second")
    image = first.start("revision-pg-image", actor="operator", request_id="p" * 36)
    build_a = first.start("revision-pg-build", actor="operator", request_id="q" * 36)
    build_b = first.start("revision-pg-build", actor="operator", request_id="r" * 36)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = tuple(
            executor.map(
                lambda service: service.claim_pending(limit=1, owner_id="pg-worker"),
                (first, second),
            )
        )
    claimed = [claim for batch in claims for claim in batch]
    assert len(claimed) == 2
    assert len({claim.operation_id for claim in claimed}) == 2
    assert len({claim.operation_id for claim in claimed} & {image.id}) <= 1
    build_claims = [
        claim for claim in claimed if claim.operation_id in {build_a.id, build_b.id}
    ]
    assert len(build_claims) == 1
    assert {claim.operation_id for claim in claimed} <= {
        image.id,
        build_a.id,
        build_b.id,
    }

    # The live build lease fences its sibling even when another worker asks for
    # a fresh claim; the worker cannot evade the global build cap.
    assert first.claim_pending(limit=1, owner_id="pg-worker-c") == ()

    build_claim = build_claims[0]
    with sessions.begin() as session:
        operation = session.get(Job, build_claim.operation_id)
        assert operation is not None
        operation.updated_at = datetime.now(UTC) - timedelta(seconds=10)
        operation.payload = dict(operation.payload) | {
            "claim_until": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        }
        sibling = session.get(
            Job, build_b.id if build_claim.operation_id == build_a.id else build_a.id
        )
        assert sibling is not None
        sibling.updated_at = datetime.now(UTC) + timedelta(seconds=10)
    reclaimed = first.claim_pending(limit=1, owner_id="pg-worker-d")
    assert len(reclaimed) == 1
    assert reclaimed[0].operation_id == build_claim.operation_id
    assert first.claim_pending(limit=1, owner_id="pg-worker-e") == ()


def test_same_immutable_image_reuses_preparation_across_recipe_revisions(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-image.json")
    recipe_b_raw = recipe.model_dump(mode="json")
    recipe_b_raw["metadata"]["title"] = "Synthetic Tiny Image (notes update)"
    recipe_b = RecipeDefinition.model_validate(recipe_b_raw)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-image-a", recipe)
        _add_revision(session, "revision-image-b", recipe_b)
    transport = Transport()
    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (
            recipe if recipe_revision_id.endswith("-a") else recipe_b,
            _runtime(),
        ),
        transport=transport,
        clock=lambda: datetime.now(UTC),
        max_parallel=2,
    )
    first = service.start("revision-image-a", actor="operator", request_id="5" * 36)
    second = service.start("revision-image-b", actor="operator", request_id="6" * 36)
    claims = service.claim_pending(limit=2, owner_id="worker-a")
    assert {claim.operation_id for claim in claims} == {first.id, second.id}
    for claim in claims:
        service.run_claim(claim)
    assert service.get(first.id).state == "succeeded"
    assert service.get(second.id).state == "succeeded"
    assert transport.calls == 1


def test_newer_preparation_intent_cancels_the_older_queued_preparation(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-image.json")
    successor = _successor(recipe, "Successor recipe revision")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_recipe_successors(
            session,
            older_id="revision-superseded",
            older=recipe,
            newer_id="revision-current",
            newer=successor,
        )
    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (
            recipe if recipe_revision_id == "revision-superseded" else successor,
            _runtime(),
        ),
        transport=Transport(),
        clock=lambda: datetime.now(UTC),
        max_parallel=1,
    )
    older = service.start(
        "revision-superseded", actor="operator", request_id="o" * 36
    )
    newer = service.start("revision-current", actor="operator", request_id="n" * 36)

    cancelled = service.get(older.id)
    assert cancelled.state == "cancelled"
    assert cancelled.result is None
    failure = cancelled.failure
    assert failure is not None
    assert failure["code"] == SUPERSEDED_PREPARATION_CODE
    detail = failure["detail"]
    assert isinstance(detail, str)
    assert "revision-current" in detail
    with sessions() as session:
        stored = session.get(Job, older.id)
        assert stored is not None
        assert stored.status_reason == (
            "superseded by newer recipe revision revision-current"
        )
        supersession = stored.payload["supersession"]
        assert isinstance(supersession, Mapping)
        assert supersession["code"] == SUPERSEDED_PREPARATION_CODE
        assert stored.result is None

    # The newer intent is untouched, and the cancelled preparation released its
    # single scheduler slot: the only claim available is the current revision.
    assert service.get(newer.id).state == "queued"
    claims = service.claim_pending(limit=1, owner_id="worker-a")
    assert [claim.operation_id for claim in claims] == [newer.id]
    assert service.resume_operations() == 1


def test_active_head_advance_cancels_a_queued_older_preparation_at_dispatch(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-image.json")
    successor = _successor(recipe, "Successor recipe revision")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_recipe_successors(
            session,
            older_id="revision-superseded",
            older=recipe,
            newer_id="revision-current",
            newer=successor,
        )
    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (
            recipe if recipe_revision_id == "revision-superseded" else successor,
            _runtime(),
        ),
        transport=Transport(),
        clock=lambda: datetime.now(UTC),
        max_parallel=1,
    )
    older = service.start(
        "revision-superseded", actor="operator", request_id="o" * 36
    )
    # A catalogue sync advances the active revision with no fresh download
    # request.  The dispatch boundary must refuse to start the older build.
    with sessions.begin() as session:
        _set_active_head(session, "revision-current")

    assert service.claim_pending(limit=1, owner_id="worker-a") == ()
    cancelled = service.get(older.id)
    assert cancelled.state == "cancelled"
    assert cancelled.failure is not None
    assert cancelled.failure["code"] == SUPERSEDED_PREPARATION_CODE
    assert service.resume_operations() == 0


def test_running_preparation_for_an_older_revision_is_not_cancelled(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-source-build.json")
    successor = _successor(recipe, "Successor source-build revision")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_recipe_successors(
            session,
            older_id="revision-running",
            older=recipe,
            newer_id="revision-current",
            newer=successor,
        )
    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (
            recipe if recipe_revision_id == "revision-running" else successor,
            _build_runtime(),
        ),
        transport=Transport(),
        clock=lambda: datetime.now(UTC),
        max_parallel=1,
        max_parallel_builds=1,
        claim_lease_seconds=120,
    )
    running = service.start("revision-running", actor="operator", request_id="o" * 36)
    claim = service.claim_pending(limit=1, owner_id="worker-a")
    assert [item.operation_id for item in claim] == [running.id]
    with sessions.begin() as session:
        _set_active_head(session, "revision-current")

    # The older attempt is already building with a live lease.  Its shared build
    # inputs may still be reused by the active revision, so it is fail-closed
    # and must keep running.
    assert service.claim_pending(limit=1, owner_id="worker-b") == ()
    retained = service.get(running.id)
    assert retained.state == "running"
    assert retained.failure is None
    with sessions() as session:
        stored = session.get(Job, running.id)
        assert stored is not None
        assert stored.state == "running"
        assert stored.payload["claim_owner"] == "worker-a"


def test_request_replay_returns_original_before_metadata_refresh(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-image.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-replay", recipe)
    calls = 0

    def authority(
        recipe_revision_id: str, *, force: bool = False
    ) -> tuple[RecipeDefinition, dict[str, object]]:
        nonlocal calls
        calls += 1
        return recipe, _runtime()

    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=authority,
        transport=Transport(),
        clock=lambda: datetime.now(UTC),
    )
    first = service.start("revision-replay", actor="operator", request_id="7" * 36)
    replay = service.start("revision-replay", actor="operator", request_id="7" * 36)
    assert replay.id == first.id
    assert calls == 1


def test_same_work_identity_keeps_distinct_authorization_operations(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-image.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-auth", recipe)
    transport = Transport()
    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        transport=transport,
        clock=lambda: datetime.now(UTC),
    )
    first = service.start("revision-auth", actor="operator-a", request_id="8" * 36)
    second = service.start("revision-auth", actor="operator-b", request_id="9" * 36)
    assert second.id != first.id
    claims = service.claim_pending(limit=2, owner_id="worker-a")
    for claim in claims:
        service.run_claim(claim)
    assert service.get(first.id).state == "succeeded"
    assert service.get(second.id).state == "succeeded"
    assert transport.calls == 1


def test_model_child_and_image_complete_through_one_sql_operation(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-image.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-model-image", recipe)

    child = SimpleNamespace(
        id="model-operation",
        request_key="model-request",
        state="succeeded",
        artifact_set_sha256="c" * 64,
        plan_digest="d" * 64,
        progress=cache_progress(
            {
                "phase": "downloading",
                "downloaded_bytes": 1024,
                "expected_bytes": 1024,
                "completed_artifacts": 0,
                "total_artifacts": 1,
            },
            previous=None,
            now=datetime.now(UTC),
        ),
        failure=None,
    )

    class ModelCache:
        def __init__(self) -> None:
            self.start_calls = 0

        def download_preview(self, *, recipe_revision_id: str) -> dict[str, object]:
            assert recipe_revision_id == "revision-model-image"
            return {
                "plan_digest": "d" * 64,
                "artifact_set_sha256": "c" * 64,
                "new_bytes": 0,
            }

        def resolve_artifact_set(self, *, recipe_revision_id: str) -> SimpleNamespace:
            return SimpleNamespace(
                digest="c" * 64,
                document=lambda: {"model_content_digests": ["d" * 64], "artifacts": []},
            )

        def list_operations(self, *, limit: int) -> tuple[object, ...]:
            return (child,) if self.start_calls else ()

        def start_download(self, **_: object) -> SimpleNamespace:
            self.start_calls += 1
            return child

        def get_operation(self, operation_id: str) -> SimpleNamespace:
            assert operation_id == child.id
            return child

    model_cache = ModelCache()
    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        transport=Transport(),
        model_cache=model_cache,
        clock=lambda: datetime.now(UTC),
    )
    queued = service.start(
        "revision-model-image", actor="operator", request_id="m" * 36
    )
    assert queued.model_child is not None
    assert queued.model_child["id"] == child.id
    assert model_cache.start_calls == 1
    second = service.start(
        "revision-model-image", actor="operator-2", request_id="n" * 36
    )
    assert second.model_child is not None
    assert second.model_child["id"] == child.id
    assert model_cache.start_calls == 1
    forced = service.start(
        "revision-model-image", actor="operator-3", request_id="f" * 36, force=True
    )
    assert forced.model_child is not None
    assert forced.model_child["id"] == child.id
    assert model_cache.start_calls == 1
    assert service.run_pending() == 1
    completed = service.get(queued.id)
    assert completed.state == "succeeded"
    assert completed.result is not None
    assert (
        require_mapping(completed.result["model_child"], "model child")["id"]
        == child.id
    )
    from vonk_control.recipe_image_availability_api import _view_document

    response = _view_document(completed)
    assert response.result is not None
    assert response.result.model_content_digests == ["d" * 64]
    assert response.children[0].model_content_digests == ["d" * 64]


def test_model_and_image_children_advance_independently_and_reuse_image(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-image.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-overlap", recipe)
    child = SimpleNamespace(
        id="model-overlap",
        request_key="model-overlap-request",
        state="running",
        artifact_set_sha256="c" * 64,
        plan_digest="d" * 64,
        progress=cache_progress(
            {
                "phase": "downloading",
                "downloaded_bytes": 40,
                "expected_bytes": 100,
                "completed_artifacts": 0,
                "total_artifacts": 1,
            },
            previous=None,
            now=datetime.now(UTC),
        ),
        failure=None,
    )

    class ModelCache:
        def download_preview(self, **_: object) -> dict[str, object]:
            return {
                "plan_digest": "d" * 64,
                "artifact_set_sha256": "c" * 64,
                "new_bytes": 0,
            }

        def resolve_artifact_set(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(
                digest="c" * 64,
                document=lambda: {"model_content_digests": ["d" * 64], "artifacts": []},
            )

        def list_operations(self, **_: object) -> tuple[object, ...]:
            return ()

        def start_download(self, **_: object) -> SimpleNamespace:
            return child

        def get_operation(self, _operation_id: str) -> SimpleNamespace:
            return child

    transport = Transport()
    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        transport=transport,
        model_cache=ModelCache(),
        clock=lambda: datetime.now(UTC),
    )
    queued = service.start("revision-overlap", actor="operator", request_id="q" * 36)
    assert service.run_pending() == 1
    partial = service.get(queued.id)
    assert partial.state == "partial"
    assert partial.result is None
    assert transport.calls == 1
    assert partial.image_state == "succeeded"
    assert partial.image_failure is None
    assert partial.progress["completed_bytes"] == 40 + len(ARCHIVE)
    image_child = next(
        item
        for item in _view_document(partial).children
        if item.kind == "runtime-image"
    )
    assert image_child.state == "succeeded"
    assert image_child.progress.completed_bytes == len(ARCHIVE)
    assert (
        _progress_members(partial.progress["members"])[-1]["member_id"] == "model-cache"
    )
    (service._storage.root / ARCHIVE_SHA).unlink()
    child.state = "succeeded"
    with sessions.begin() as session:
        row = session.get(Job, queued.id)
        assert row is not None
        row.payload = dict(row.payload) | {
            "retry_after_at": "2000-01-01T00:00:00+00:00"
        }
    restarted = RecipeImageAvailabilityService(
        sessions,
        storage=service._storage,
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        transport=transport,
        model_cache=ModelCache(),
        clock=lambda: datetime.now(UTC),
    )
    assert restarted.run_pending() == 1
    completed = restarted.get(queued.id)
    assert completed.state == "succeeded", completed.failure
    assert transport.calls == 2


def test_recipe_retry_uses_model_access_recheck_for_terminal_auth(
    tmp_path: Path,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    failed = SimpleNamespace(
        id="failed-model",
        request_key="failed-request",
        state="failed",
        artifact_set_sha256="c" * 64,
        plan_digest="d" * 64,
        progress=cache_progress(
            {
                "phase": "downloading",
                "downloaded_bytes": 4,
                "expected_bytes": 10,
                "completed_artifacts": 0,
                "total_artifacts": 1,
            },
            previous=None,
            now=datetime.now(UTC),
        ),
        failure={
            "code": "access_denied",
            "detail": "HF access denied",
            "recovery_actions": ["open_model_access", "check_access_and_resume"],
            "retryable": False,
            "retry_time": None,
            "retry_after_seconds": None,
            "log_excerpt": "denied",
            "required_bytes": None,
            "free_bytes": None,
            "shortfall_bytes": None,
        },
    )

    class ModelCache:
        def __init__(self) -> None:
            self.called: dict[str, object] | None = None

        def get_operation(self, _operation_id: str) -> SimpleNamespace:
            return failed

        def check_access_and_resume(
            self, operation_id: str, **kwargs: object
        ) -> SimpleNamespace:
            self.called = {"operation_id": operation_id, **kwargs}
            return failed

        def list_operations(self, **_: object) -> tuple[object, ...]:
            return ()

    cache = ModelCache()
    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (
            _recipe("recipe-image.json"),
            _runtime(),
        ),
        model_cache=cache,
        clock=lambda: datetime.now(UTC),
    )
    service._resume_model_child(
        {"id": failed.id, "state": "failed", "failure": failed.failure},
        actor="operator",
        parent_request_key="p" * 36,
    )
    assert cache.called is not None
    assert cache.called["artifact_set_sha256"] == "c" * 64
    assert cache.called["plan_digest"] == "d" * 64


def test_recipe_retry_repairs_terminal_model_integrity_child_and_reuses_image(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-image.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-integrity-repair", recipe)

    failed = SimpleNamespace(
        id="model-download",
        request_key="model-download-request",
        state="running",
        artifact_set_sha256="c" * 64,
        plan_digest="d" * 64,
        progress=cache_progress(
            {
                "phase": "downloading",
                "downloaded_bytes": 4,
                "expected_bytes": 10,
                "completed_artifacts": 0,
                "total_artifacts": 1,
            },
            previous=None,
            now=datetime.now(UTC),
        ),
        failure=None,
    )
    repaired = SimpleNamespace(
        id="model-repair",
        request_key="model-repair-request",
        state="succeeded",
        artifact_set_sha256="c" * 64,
        plan_digest="e" * 64,
        progress=cache_progress(
            {
                "phase": "downloading",
                "downloaded_bytes": 10,
                "expected_bytes": 10,
                "completed_artifacts": 0,
                "total_artifacts": 1,
            },
            previous=None,
            now=datetime.now(UTC),
        ),
        failure=None,
    )

    class ModelCache:
        def __init__(self) -> None:
            self.failed = False
            self.repair_calls: list[dict[str, object]] = []

        def download_preview(self, **_: object) -> dict[str, object]:
            return {
                "plan_digest": "d" * 64,
                "artifact_set_sha256": "c" * 64,
                "new_bytes": 0,
            }

        def resolve_artifact_set(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(
                digest="c" * 64,
                document=lambda: {"model_content_digests": ["d" * 64], "artifacts": []},
            )

        def list_operations(self, **_: object) -> tuple[object, ...]:
            return (failed,)

        def start_download(self, **_: object) -> SimpleNamespace:
            raise AssertionError("the existing ModelCache child should be reused")

        def get_operation(self, operation_id: str) -> SimpleNamespace:
            if operation_id == repaired.id:
                return repaired
            if self.failed:
                failed.state = "failed"
                failed.failure = {
                    "code": "integrity_mismatch",
                    "detail": "downloaded bytes did not match the pinned digest",
                    "recovery_actions": ["download_again"],
                    "retryable": False,
                    "retry_time": None,
                    "retry_after_seconds": None,
                    "log_excerpt": "digest mismatch",
                    "required_bytes": 10,
                    "free_bytes": 100,
                    "shortfall_bytes": 0,
                }
            return failed

        def repair_preview(self, artifact_set_sha256: str) -> dict[str, object]:
            assert artifact_set_sha256 == "c" * 64
            return {"plan_digest": "e" * 64}

        def start_repair(self, **kwargs: object) -> SimpleNamespace:
            self.repair_calls.append(kwargs)
            return repaired

    model_cache = ModelCache()
    transport = Transport()
    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        transport=transport,
        model_cache=model_cache,
        clock=lambda: datetime.now(UTC),
    )
    parent = service.start(
        "revision-integrity-repair",
        actor="operator",
        request_id="i" * 36,
    )
    assert service.run_pending() == 1
    partial = service.get(parent.id)
    assert partial.state == "partial"
    assert transport.calls == 1

    model_cache.failed = True
    with sessions.begin() as session:
        row = session.get(Job, parent.id)
        assert row is not None
        row.payload = dict(row.payload) | {
            "retry_after_at": "2000-01-01T00:00:00+00:00"
        }
    assert service.run_pending() == 1
    assert service.get(parent.id).state == "failed"

    resumed = service.retry(parent.id, actor="operator", request_id="j" * 36)
    assert resumed.model_child is not None
    assert resumed.model_child["id"] == repaired.id
    assert len(model_cache.repair_calls) == 1
    assert model_cache.repair_calls[0]["artifact_set_sha256"] == "c" * 64
    assert model_cache.repair_calls[0]["plan_digest"] == "e" * 64

    assert service.run_pending() == 1
    completed = service.get(resumed.id)
    assert completed.state == "succeeded"
    assert completed.result is not None
    assert (
        require_mapping(completed.result["model_child"], "model child")["id"]
        == repaired.id
    )
    assert transport.calls == 1


def test_force_download_is_a_distinct_operation_for_same_revision(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-image.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-force", recipe)
    transport = Transport()
    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        transport=transport,
        clock=lambda: datetime.now(UTC),
    )
    cached = service.start("revision-force", actor="operator", request_id="a" * 36)
    forced = service.start(
        "revision-force", actor="operator", request_id="b" * 36, force=True
    )
    assert forced.id != cached.id
    for claim in service.claim_pending(limit=2, owner_id="worker-a"):
        service.run_claim(claim)
    assert service.get(cached.id).state == "succeeded"
    assert service.get(forced.id).state == "succeeded"
    assert transport.calls == 2


@pytest.mark.parametrize("model_state", ["running", "failed"])
def test_parent_progress_retains_ready_image_while_model_is_incomplete(
    tmp_path: Path, model_state: str
) -> None:
    recipe = _recipe("recipe-image.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    now = datetime.now(UTC)
    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        transport=Transport(),
        clock=lambda: now,
    )
    payload = {
        "recipe_revision_id": "revision-progress",
        "recipe_content_sha256": content_sha256(recipe),
        "progress": {
            "phase": "available",
            "completed_bytes": 20,
            "total_bytes": 20,
            "total_bytes_known": True,
        },
        "image_result": {"image_bytes": 20},
        "model_child": {
            "id": "model-child",
            "state": model_state,
            "model_content_digests": ["d" * 64],
            "progress": {
                "phase": "download",
                "completed_bytes": 40,
                "total_bytes": 100,
                "total_bytes_known": True,
            },
        },
    }
    if model_state == "failed":
        payload["failure"] = {
            "code": "recipe_image.model_cache_failed",
            "detail": "model download failed",
            "retryable": True,
            "recovery_actions": ["retry"],
        }
    operation = Job(
        id="availability-progress",
        request_id="p" * 36,
        kind="recipe.image.availability.v2",
        state="failed" if model_state == "failed" else "partial",
        actor="operator",
        authority_revision="revision-progress",
        targets=["revision-progress"],
        payload_digest="a" * 64,
        payload=payload,
        result=None,
        current_attempt=1,
        created_at=now,
        updated_at=now,
    )
    with sessions.begin() as session:
        session.add(operation)
    view = service.get("availability-progress")
    assert view.progress["completed_bytes"] == 60
    assert view.progress["total_bytes"] == 120
    members = {
        member["member_id"]: member
        for member in _progress_members(view.progress["members"])
    }
    assert members["model-cache"]["completed_bytes"] == 40
    assert members["model-cache"]["total_bytes"] == 100
    assert members["runtime-image"]["completed_bytes"] == 20
    assert members["runtime-image"]["total_bytes"] == 20
    assert members["runtime-image"]["state"] == "succeeded"
    response = _view_document(view)
    children = {child.kind: child for child in response.children}
    assert children["runtime-image"].state == "succeeded"
    assert children["runtime-image"].failure is None
    assert children["model-cache"].state == model_state
    assert view.image_progress is not None
    assert view.image_progress["total_bytes"] == 20
    rows, total, cursor = service.list_page(limit=1)
    assert total == 1
    assert len(rows) == 1
    assert cursor is None
