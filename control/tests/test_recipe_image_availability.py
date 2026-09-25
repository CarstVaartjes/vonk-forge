from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import sqlite3
import threading
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from vonk_control import artifact_reference_scan, runtime_image_preparation
from vonk_control.artifact_lifecycle import ArtifactLifecycleError
from vonk_control.artifact_reference_scan import (
    runtime_image_reference_findings,
    runtime_image_reference_reasons,
)
from vonk_control.bounded_json import require_mapping, require_sequence
from vonk_control.catalog_entities import _build_projection
from vonk_control.catalog_revision_contract import write_catalog_projection
from vonk_control.failure_evidence import failure_receipt
from vonk_control.model_cache import ModelCacheError, ModelCacheService
from vonk_control.model_cache_contract import (
    ModelCacheDownloadPayload,
    ModelCacheOperationProgress,
)
from vonk_control.model_cache_progress import cache_progress
from vonk_control.models import (
    AgentNode,
    ArtifactLifecycleGate,
    Base,
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    FleetProfile,
    FleetProfileApplication,
    Job,
    ModelCacheOperation,
    ModelCacheSet,
    RecipeBuild,
    RuntimeImageAuthorization,
    User,
)
from vonk_control.recipe_availability_intent import RecipeRevisionIntent
from vonk_control.recipe_image_availability import (
    SUPERSEDED_PREPARATION_CODE,
    RecipeImageAvailabilityError,
    RecipeImageAvailabilityService,
    _retryable,
)
from vonk_control.recipe_image_availability_api import _recipe_error, _view_document
from vonk_control.recipe_image_removal_contract import (
    RecipeCacheRemovalOwner,
)
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    PulledImageEvidence,
    RuntimeImagePreparationError,
    RuntimeImageReceipt,
    RuntimeImageReferenceIntent,
    prepare_runtime_image,
    read_runtime_image_reference_intent,
    resolve_persisted_runtime_image_receipt,
)
from vonk_forge_contracts import RecipeDefinition, content_sha256

from .recipe_removal_review_support import remove_after_review

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


def _reference_receipt() -> RuntimeImageReceipt:
    return RuntimeImageReceipt(
        schema_version=2,
        source="published",
        distribution_publisher="test-publisher",
        distribution_slug="test-image",
        distribution_content_sha256="a" * 64,
        registry_manifest_digest=IMAGE_DIGEST,
        platform_manifest_digest=PLATFORM_DIGEST,
        image_digest=PLATFORM_DIGEST,
        oci_archive_sha256=ARCHIVE_SHA,
        image_bytes=len(ARCHIVE),
        local_image_config_id=CONFIG_DIGEST,
        local_image_reference=None,
        architecture="linux-arm64",
        runtime_interface="vonk.runtime.v1",
        runtime_interface_label="v1",
        archive_path="/managed/image-cache/" + ARCHIVE_SHA,
        recorded_at=datetime.now(UTC).isoformat(),
        build_id=None,
    )


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
    assert _recipe_error(error).status_code == (503 if expected else 409)


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


class _ExitAfterImageUnlinkStorage(FilesystemRuntimeImageStorage):
    def remove_published(self, archive_sha256: str) -> int:
        super().remove_published(archive_sha256)
        os._exit(73)


def _exit_after_recipe_image_unlink(database_url: str, artifact_root: str) -> None:
    engine = create_engine(database_url)
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = RecipeImageAvailabilityService(
        sessions,
        storage=_ExitAfterImageUnlinkStorage(Path(artifact_root)),
        authority=lambda *_args, **_kwargs: (_recipe("recipe-image.json"), _runtime()),
        clock=lambda: datetime.now(UTC),
    )
    service.advance_removals(limit=1)
    os._exit(74)


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


def _persist_fake_model_cache_child(
    sessions: sessionmaker[Session],
    *,
    child: SimpleNamespace,
    model_content_sha256: str,
    now: datetime,
) -> None:
    """Give fake owner responses the same durable identity the service requires."""

    operation_id = child.id
    request_key = child.request_key
    artifact_set_sha256 = child.artifact_set_sha256
    plan_digest = child.plan_digest
    state = child.state
    failure = child.failure
    progress_value = child.progress
    assert isinstance(operation_id, str)
    assert isinstance(request_key, str)
    assert isinstance(artifact_set_sha256, str)
    assert isinstance(plan_digest, str)
    assert state in {"running", "succeeded", "failed"}
    assert isinstance(progress_value, Mapping)
    progress = ModelCacheOperationProgress.model_validate(progress_value)
    expected_bytes = progress.expected_bytes
    assert expected_bytes is not None
    manifest = {
        "schema_version": 2,
        "source_policy": "nas-first",
        "model_content_sha256": model_content_sha256,
        "recipe_revision_sha256": None,
        "model_definition_ref": None,
        "model_content_digests": [model_content_sha256],
        "artifacts": [],
    }
    payload = ModelCacheDownloadPayload.model_validate(
        {
            "schema_version": 2,
            "source_policy": "nas-first",
            "artifact_set_sha256": artifact_set_sha256,
            "manifest": manifest,
            "plan_digest": plan_digest,
            "transfer": {
                "schema_version": 2,
                "total_bytes": expected_bytes,
                "artifacts": {},
            },
            "retry": {"automatic_attempts": 1, "operator_retries": 0},
            "failure": failure,
            "result": (
                {
                    "schema_version": 2,
                    "artifact_set_sha256": artifact_set_sha256,
                    "coverage": "complete",
                }
                if state == "succeeded"
                else None
            ),
        }
    )
    cache_set_state = {
        "running": "downloading",
        "succeeded": "cached",
        "failed": "failed",
    }[state]
    with sessions.begin() as session:
        cache_set = session.get(ModelCacheSet, artifact_set_sha256)
        if cache_set is None:
            session.add(
                ModelCacheSet(
                    artifact_set_sha256=artifact_set_sha256,
                    model_content_sha256=model_content_sha256,
                    recipe_revision_sha256=None,
                    manifest=manifest,
                    expected_bytes=expected_bytes,
                    verified_bytes=min(progress.downloaded_bytes, expected_bytes),
                    state=cache_set_state,
                    created_at=now,
                    updated_at=now,
                    last_accessed_at=now,
                )
            )
        else:
            cache_set.state = cache_set_state
            cache_set.expected_bytes = expected_bytes
            cache_set.verified_bytes = min(progress.downloaded_bytes, expected_bytes)
            cache_set.updated_at = now
        operation = session.get(ModelCacheOperation, operation_id)
        if operation is None:
            operation = ModelCacheOperation(
                id=operation_id,
                request_key=request_key,
                kind="download",
                state=state,
                artifact_set_sha256=artifact_set_sha256,
                plan_digest=plan_digest,
                payload=payload.model_dump(mode="json", exclude_none=True),
                progress=progress.model_dump(mode="json"),
                actor="operator",
                created_at=now,
                updated_at=now,
                completed_at=now if state in {"succeeded", "failed"} else None,
            )
            session.add(operation)
        else:
            operation.state = state
            operation.plan_digest = plan_digest
            operation.payload = payload.model_dump(mode="json", exclude_none=True)
            operation.progress = progress.model_dump(mode="json")
            operation.updated_at = now
            operation.completed_at = now if state in {"succeeded", "failed"} else None


def _add_head(
    session: Session, revision: CatalogDocumentRevision
) -> CatalogDocumentHead:
    document = CatalogDocument(
        id=revision.document_id,
        kind="recipe",
        publisher=revision.publisher,
        slug=revision.slug,
        title="Recipe",
        created_by="test",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(document)
    session.flush([document])
    session.flush([revision])
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


def test_selector_replay_keeps_original_revision_and_issuer(tmp_path: Path) -> None:
    recipe = _recipe("recipe-image.json")
    newer = recipe.model_copy(
        update={
            "metadata": recipe.metadata.model_copy(update={"description": "New head"})
        }
    )
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

    service = RecipeImageAvailabilityService(
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
        with pytest.raises(RecipeImageAvailabilityError) as refused:
            service.start_selector(
                requested, actor=actor, request_id="original-request", force=True
            )
        assert refused.value.code == "recipe_image.request_key_reused"


def test_replay_does_not_collapse_different_image_actions(tmp_path: Path) -> None:
    recipe = _recipe("recipe-image.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "image-actions", recipe)
    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        transport=Transport(),
        authority=lambda *_args, **_kwargs: (recipe, _runtime()),
        clock=lambda: datetime.now(UTC),
    )
    service.start(
        "image-actions", actor="operator", request_id="action", force_download=True
    )
    with pytest.raises(RecipeImageAvailabilityError) as refused:
        service.start(
            "image-actions", actor="operator", request_id="action", force_rebuild=True
        )
    assert refused.value.code == "recipe_image.request_key_reused"


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
    assert isinstance(removed, dict)
    assert removed["state"] == "succeeded"
    # The worker takes the bytes and storage receipt only after committing its
    # exact checkpoint and deletion fence; SQL keeps the authorization decision.
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


def test_remove_recipe_does_not_cancel_accepted_build_or_preparation(
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
    assert isinstance(observed, dict)
    assert observed["operation_id"] == result["operation_id"]
    with sessions() as session:
        build = session.get(RecipeBuild, "00000000-0000-4000-8000-000000000901")
        assert build is not None and build.state == "building"
        assert session.scalars(select(RuntimeImageAuthorization)).all() == []


def test_recipe_removal_reference_scan_enforces_accumulated_owner_budget(
    tmp_path: Path, monkeypatch
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
    monkeypatch.setattr(artifact_reference_scan, "MAX_ARTIFACT_OWNER_SCAN_BYTES", 1)

    with sessions() as session, pytest.raises(ArtifactLifecycleError) as refused:
        runtime_image_reference_reasons(session, (ARCHIVE_SHA,))

    assert refused.value.code == "artifact.reference_scan_limited"
    engine.dispose()


def test_recipe_removal_fails_closed_on_symlinked_archive(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-image.json")
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'symlink-removal.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    now = datetime.now(UTC)
    receipt = _reference_receipt()
    with sessions.begin() as session:
        revision = _add_revision(session, "rev-symlink-removal", recipe)
        _add_head(session, revision)
        session.add(
            RuntimeImageAuthorization(
                recipe_revision_id=revision.id,
                source="published",
                original_content_digest=content_sha256(recipe),
                effective_execution_key=revision.execution_key,
                registry_manifest_digest=receipt.registry_manifest_digest,
                platform_manifest_digest=receipt.platform_manifest_digest,
                local_image_config_id=receipt.local_image_config_id,
                oci_archive_sha256=receipt.oci_archive_sha256,
                image_bytes=receipt.image_bytes,
                build_id=None,
                authorized_at=now,
            )
        )
    storage = FilesystemRuntimeImageStorage(tmp_path / "managed")
    outside = tmp_path / "outside archive"
    outside.write_bytes(ARCHIVE)
    archive = storage.root / ARCHIVE_SHA
    archive.symlink_to(outside)
    receipt_path = storage.root / f"{ARCHIVE_SHA}.receipt.json"
    receipt_path.write_text(
        json.dumps(receipt.model_dump(mode="json")), encoding="utf-8"
    )
    service = RecipeImageAvailabilityService(
        sessions,
        storage=storage,
        authority=lambda *_args, **_kwargs: (recipe, _runtime()),
        clock=lambda: now,
    )

    with pytest.raises(RecipeImageAvailabilityError) as refused:
        remove_after_review(
            service,
            recipe.identity.slug,
            actor="operator",
            request_id="00000000-0000-4000-8000-000000000034",
            with_model=False,
        )

    assert refused.value.code == "runtime_image.removal_path_unsafe"
    with sessions() as session:
        operation = session.scalar(
            select(Job).where(Job.request_id == "00000000-0000-4000-8000-000000000034")
        )
        gate = session.get(ArtifactLifecycleGate, ("runtime-image", ARCHIVE_SHA))
        assert operation is None
        assert gate is None or gate.removal_owner_id is None
    assert archive.is_symlink()
    assert outside.read_bytes() == ARCHIVE
    assert receipt_path.exists()
    engine.dispose()


@pytest.mark.parametrize("fault", ["publication-lock", "archive-stat"])
def test_recipe_removal_transient_storage_failure_uses_automatic_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    recipe = _recipe("recipe-image.json")
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'retry-removal.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    now = [datetime.now(UTC)]
    receipt = _reference_receipt()
    with sessions.begin() as session:
        revision = _add_revision(session, "rev-retry-removal", recipe)
        _add_head(session, revision)
        session.add(
            RuntimeImageAuthorization(
                recipe_revision_id=revision.id,
                source="published",
                original_content_digest=content_sha256(recipe),
                effective_execution_key=revision.execution_key,
                registry_manifest_digest=receipt.registry_manifest_digest,
                platform_manifest_digest=receipt.platform_manifest_digest,
                local_image_config_id=receipt.local_image_config_id,
                oci_archive_sha256=receipt.oci_archive_sha256,
                image_bytes=receipt.image_bytes,
                build_id=None,
                authorized_at=now[0],
            )
        )
    storage = FilesystemRuntimeImageStorage(tmp_path / "managed-retry")
    archive = storage.root / ARCHIVE_SHA
    archive.write_bytes(ARCHIVE)
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
        original_stat = runtime_image_preparation.os.stat
        stat_failed = False

        def stat_with_one_failure(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            *,
            dir_fd: int | None = None,
            follow_symlinks: bool = True,
        ) -> os.stat_result:
            nonlocal stat_failed
            if (
                removal_started
                and path == ARCHIVE_SHA
                and dir_fd is not None
                and not stat_failed
            ):
                stat_failed = True
                raise PermissionError("injected archive-stat failure")
            return original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(runtime_image_preparation.os, "stat", stat_with_one_failure)
    service = RecipeImageAvailabilityService(
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
    assert service.advance_removals(limit=1) == 1
    waiting = service.get_operator_request(request_key, actor="operator")
    assert isinstance(waiting, dict)
    failure = require_mapping(waiting["failure"], "removal failure")
    retry_time = failure["retry_time"]
    assert waiting["state"] == "partial"
    assert failure["code"] == failure_code
    assert failure["retryable"] is True
    assert failure["recovery_actions"] == []
    assert waiting["next_actions"] == []
    assert isinstance(retry_time, str)
    assert archive.read_bytes() == ARCHIVE
    assert receipt_path.exists()

    now[0] = datetime.fromisoformat(retry_time) + timedelta(seconds=1)
    assert service.advance_removals(limit=1) == 1
    assert not archive.exists()
    assert not receipt_path.exists()
    assert service.advance_removals(limit=1) == 1
    completed = service.get_operator_request(request_key, actor="operator")
    assert isinstance(completed, dict)
    assert completed["state"] == "succeeded"
    engine.dispose()


def test_oversized_removal_owner_does_not_hold_up_later_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, _, service, selector = _empty_recipe_removal_owner(tmp_path)
    bad_key = "00000000-0000-4000-8000-000000000035"
    good_key = "00000000-0000-4000-8000-000000000036"
    remove_after_review(service, selector, actor="operator", request_id=bad_key)
    remove_after_review(service, selector, actor="operator", request_id=good_key)
    monkeypatch.setattr(artifact_reference_scan, "MAX_ARTIFACT_OWNER_SCAN_BYTES", 4096)
    with sessions.begin() as session:
        bad = session.scalar(select(Job).where(Job.request_id == bad_key))
        assert bad is not None
        bad.payload = dict(bad.payload) | {"padding": "x" * 5000}
        bad.updated_at = datetime.now(UTC) - timedelta(seconds=1)

    assert service.advance_removals(limit=1) == 1

    with sessions() as session:
        bad = session.scalar(select(Job).where(Job.request_id == bad_key))
        good = session.scalar(select(Job).where(Job.request_id == good_key))
        assert bad is not None and bad.state == "queued"
        assert good is not None and good.state == "succeeded"


def _empty_recipe_removal_owner(
    tmp_path: Path,
) -> tuple[
    sessionmaker[Session],
    FilesystemRuntimeImageStorage,
    RecipeImageAvailabilityService,
    str,
]:
    image_recipe = _recipe("recipe-image.json")
    job_recipe = _recipe("recipe-job.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_head(session, _add_revision(session, "revision-image", image_recipe))
        _add_head(session, _add_revision(session, "revision-job", job_recipe))
    storage = FilesystemRuntimeImageStorage(tmp_path / "cache")
    service = RecipeImageAvailabilityService(
        sessions,
        storage=storage,
        authority=lambda *_args, **_kwargs: (image_recipe, _runtime()),
        clock=lambda: datetime.now(UTC),
    )
    return (
        sessions,
        storage,
        service,
        "vonk-forge/synthetic-tiny-image",
    )


@pytest.mark.parametrize(
    ("replay_selector", "replay_actor", "replay_with_model"),
    [
        ("vonk-forge/synthetic-image-job", "operator", False),
        ("vonk-forge/synthetic-tiny-image", "different-operator", False),
        ("vonk-forge/synthetic-tiny-image", "operator", True),
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

    The disposable database contains two catalog recipes and no cache
    authorizations or managed artifacts, so this owner-boundary regression
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
    assert isinstance(original, dict)
    assert original["state"] == "succeeded"
    with sessions() as session:
        assert session.scalars(select(RuntimeImageAuthorization)).all() == []
    assert not any(path.is_file() for path in storage.root.rglob("*"))

    with pytest.raises(RecipeImageAvailabilityError) as refused:
        remove_after_review(
            service,
            replay_selector,
            actor=replay_actor,
            request_id=request_id,
            with_model=replay_with_model,
            review_digest=str(original["review_digest"]),
        )
    assert refused.value.code == "recipe_image.request_key_reused"


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
    assert isinstance(observed, dict)
    original = observed
    with sessions.begin() as session:
        head = session.scalar(
            select(CatalogDocumentHead).where(
                CatalogDocumentHead.slug == "synthetic-tiny-image"
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
        review_digest=str(original["review_digest"]),
    )

    assert replay == original
    assert replay["with_model"] is False


def test_active_recipe_removal_blocks_fresh_review_but_replays_accepted_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing deletion fence is visible, while its accepted key still recovers."""

    recipe = _recipe("recipe-image.json")
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'active-review.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    receipt = _reference_receipt()
    with sessions.begin() as session:
        revision = _add_revision(session, "revision-active-review", recipe)
        _add_head(session, revision)
        session.add(
            RuntimeImageAuthorization(
                recipe_revision_id=revision.id,
                source="published",
                original_content_digest=content_sha256(recipe),
                effective_execution_key=revision.execution_key,
                registry_manifest_digest=receipt.registry_manifest_digest,
                platform_manifest_digest=receipt.platform_manifest_digest,
                local_image_config_id=receipt.local_image_config_id,
                oci_archive_sha256=receipt.oci_archive_sha256,
                image_bytes=receipt.image_bytes,
                build_id=None,
                authorized_at=now,
            )
        )

    storage = FilesystemRuntimeImageStorage(tmp_path / "review-managed")
    (storage.root / ARCHIVE_SHA).write_bytes(ARCHIVE)
    (storage.root / f"{ARCHIVE_SHA}.receipt.json").write_text(
        json.dumps(receipt.model_dump(mode="json")), encoding="utf-8"
    )
    service = RecipeImageAvailabilityService(
        sessions,
        storage=storage,
        authority=lambda *_args, **_kwargs: (recipe, _runtime()),
        clock=lambda: now,
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
        review_digest=before.review_digest,
    )
    assert accepted["state"] == "queued"

    changed_key = "00000000-0000-4000-8000-000000000042"
    with pytest.raises(RecipeImageAvailabilityError) as stale:
        service.remove_selector(
            selector,
            actor="operator",
            request_id=changed_key,
            with_model=False,
            review_digest=before.review_digest,
        )
    assert stale.value.code == "recipe_image.review_stale"
    with sessions() as session:
        assert session.scalar(select(Job).where(Job.request_id == changed_key)) is None
        existing = session.scalar(select(Job).where(Job.request_id == request_key))
        gate = session.get(ArtifactLifecycleGate, ("runtime-image", ARCHIVE_SHA))
        assert existing is not None and gate is not None
        assert gate.removal_owner_id == existing.id
    assert (storage.root / ARCHIVE_SHA).read_bytes() == ARCHIVE

    during = service.review_removal(selector, with_model=False)
    assert any(
        blocker.code == "artifact.deletion_in_progress" for blocker in during.blockers
    )
    assert during.review_digest != before.review_digest

    def no_mutable_review(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("same-key recovery must precede mutable review")

    monkeypatch.setattr(service, "review_removal", no_mutable_review)
    replay = service.remove_selector(
        selector,
        actor="operator",
        request_id=request_key,
        with_model=False,
        review_digest=str(accepted["review_digest"]),
    )
    assert replay["operation_id"] == accepted["operation_id"]
    assert replay["review_digest"] == before.review_digest


def test_postgres_recipe_removal_persists_owner_before_first_unlink(
    tmp_path: Path, postgres_engine, monkeypatch
) -> None:
    """The committed checkpoint and fence precede each managed image unlink."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    recipe = _recipe("recipe-image.json")
    receipt = _reference_receipt()
    now = datetime.now(UTC)
    with sessions.begin() as session:
        revision = _add_revision(session, "revision-removal-pre-effect", recipe)
        _add_head(session, revision)
        session.add(
            RuntimeImageAuthorization(
                recipe_revision_id=revision.id,
                source="published",
                original_content_digest=content_sha256(recipe),
                effective_execution_key=revision.execution_key,
                registry_manifest_digest=receipt.registry_manifest_digest,
                platform_manifest_digest=receipt.platform_manifest_digest,
                local_image_config_id=receipt.local_image_config_id,
                oci_archive_sha256=receipt.oci_archive_sha256,
                image_bytes=receipt.image_bytes,
                build_id=None,
                authorized_at=now,
            )
        )

    storage = FilesystemRuntimeImageStorage(tmp_path / "controller-artifacts")
    archive = storage.root / ARCHIVE_SHA
    receipt_file = storage.root / f"{ARCHIVE_SHA}.receipt.json"
    archive.write_bytes(ARCHIVE)
    receipt_document = json.dumps(receipt.model_dump(mode="json"))
    receipt_file.write_text(receipt_document)
    request_id = str(uuid.uuid4())
    actor = "operator"
    service = RecipeImageAvailabilityService(
        sessions,
        storage=storage,
        authority=lambda *_args, **_kwargs: (recipe, _runtime()),
        clock=lambda: now,
    )
    original_remove = storage.remove_published
    removal_calls: list[str] = []

    def require_committed_checkpoint_before_unlink(archive_digest: str) -> int:
        with sessions() as observer:
            owner_row = observer.scalar(select(Job).where(Job.request_id == request_id))
            assert owner_row is not None
            owner = RecipeCacheRemovalOwner.model_validate_json(
                json.dumps(owner_row.payload)
            )
            intent = owner.plan.intent
            assert owner_row.kind == intent.kind
            assert owner_row.actor == intent.actor == actor
            assert intent.request_key == request_id
            assert intent.action == "remove"
            assert intent.selector == recipe.identity.slug
            assert intent.recipe_revision_id == "revision-removal-pre-effect"
            assert owner_row.state == "running"
            assert owner.plan.image_archives == [archive_digest]
            assert owner.checkpoint.image_pending_bytes == len(ARCHIVE)
        removal_calls.append(archive_digest)
        return original_remove(archive_digest)

    monkeypatch.setattr(
        storage, "remove_published", require_committed_checkpoint_before_unlink
    )
    try:
        accepted = remove_after_review(
            service,
            recipe.identity.slug,
            actor=actor,
            request_id=request_id,
            with_model=False,
        )
        assert accepted["state"] == "queued"
        assert archive.read_bytes() == ARCHIVE
        assert receipt_file.read_text() == receipt_document
        assert service.advance_removals(limit=1) == 1
    except AssertionError:
        assert archive.read_bytes() == ARCHIVE
        assert receipt_file.read_text() == receipt_document
        raise
    assert removal_calls == [ARCHIVE_SHA]
    assert not archive.exists()
    assert not receipt_file.exists()


def test_postgres_recipe_removal_recovers_after_process_death_between_unlink_and_checkpoint(
    tmp_path: Path, postgres_engine
) -> None:
    """A committed byte checkpoint reconciles an unlink lost to process death."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    recipe = _recipe("recipe-image.json")
    receipt = _reference_receipt()
    now = datetime.now(UTC)
    with sessions.begin() as session:
        revision = _add_revision(session, "rev-removal-death", recipe)
        _add_head(session, revision)
        session.add(
            RuntimeImageAuthorization(
                recipe_revision_id=revision.id,
                source="published",
                original_content_digest=content_sha256(recipe),
                effective_execution_key=revision.execution_key,
                registry_manifest_digest=receipt.registry_manifest_digest,
                platform_manifest_digest=receipt.platform_manifest_digest,
                local_image_config_id=receipt.local_image_config_id,
                oci_archive_sha256=receipt.oci_archive_sha256,
                image_bytes=receipt.image_bytes,
                build_id=None,
                authorized_at=now,
            )
        )

    storage = FilesystemRuntimeImageStorage(tmp_path / "controller-artifacts")
    archive = storage.root / ARCHIVE_SHA
    receipt_file = storage.root / f"{ARCHIVE_SHA}.receipt.json"
    archive.write_bytes(ARCHIVE)
    receipt_file.write_text(
        json.dumps(receipt.model_dump(mode="json")), encoding="utf-8"
    )
    request_id = str(uuid.uuid4())
    service = RecipeImageAvailabilityService(
        sessions,
        storage=storage,
        authority=lambda *_args, **_kwargs: (recipe, _runtime()),
        clock=lambda: now,
    )
    accepted = remove_after_review(
        service,
        recipe.identity.slug,
        actor="operator",
        request_id=request_id,
        with_model=False,
    )
    assert accepted["state"] == "queued"
    postgres_engine.dispose()

    process = multiprocessing.get_context("fork").Process(
        target=_exit_after_recipe_image_unlink,
        args=(
            postgres_engine.url.render_as_string(hide_password=False),
            str(storage.root.parent),
        ),
    )
    process.start()
    process.join(timeout=20)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("recipe removal process did not reach its injected crash point")
    assert process.exitcode == 73
    assert not archive.exists()
    assert not receipt_file.exists()

    with sessions() as observer:
        owner_row = observer.scalar(select(Job).where(Job.request_id == request_id))
        assert owner_row is not None
        owner = RecipeCacheRemovalOwner.model_validate_json(
            json.dumps(owner_row.payload)
        )
        assert owner_row.state == "running"
        assert owner_row.result is None
        assert owner.checkpoint.image_index == 0
        assert owner.checkpoint.image_pending_bytes == len(ARCHIVE)
        assert owner.checkpoint.image_reclaimed_bytes == 0

    restarted = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(storage.root.parent),
        authority=lambda *_args, **_kwargs: (recipe, _runtime()),
        clock=lambda: now,
    )
    assert restarted.advance_removals(limit=1) == 1
    assert restarted.advance_removals(limit=1) == 1
    recovered = restarted.get_operator_request(request_id, actor="operator")
    assert isinstance(recovered, dict)
    assert recovered["state"] == "succeeded"
    assert recovered["reclaimed_bytes"] == len(ARCHIVE)


def test_postgres_recipe_removal_retries_finalization_after_gate_contention(
    tmp_path: Path, postgres_engine
) -> None:
    """A due finalization retry clears prior contention and settles the owner."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    recipe = _recipe("recipe-image.json")
    receipt = _reference_receipt()
    now = [datetime.now(UTC)]
    with sessions.begin() as session:
        revision = _add_revision(session, "rev-removal-finalize", recipe)
        _add_head(session, revision)
        session.add(
            RuntimeImageAuthorization(
                recipe_revision_id=revision.id,
                source="published",
                original_content_digest=content_sha256(recipe),
                effective_execution_key=revision.execution_key,
                registry_manifest_digest=receipt.registry_manifest_digest,
                platform_manifest_digest=receipt.platform_manifest_digest,
                local_image_config_id=receipt.local_image_config_id,
                oci_archive_sha256=receipt.oci_archive_sha256,
                image_bytes=receipt.image_bytes,
                build_id=None,
                authorized_at=now[0],
            )
        )
    storage = FilesystemRuntimeImageStorage(tmp_path / "controller-artifacts")
    archive = storage.root / ARCHIVE_SHA
    archive.write_bytes(ARCHIVE)
    (storage.root / f"{ARCHIVE_SHA}.receipt.json").write_text(
        json.dumps(receipt.model_dump(mode="json")), encoding="utf-8"
    )
    service = RecipeImageAvailabilityService(
        sessions,
        storage=storage,
        authority=lambda *_args, **_kwargs: (recipe, _runtime()),
        clock=lambda: now[0],
    )
    request_key = str(uuid.uuid4())
    accepted = remove_after_review(
        service,
        recipe.identity.slug,
        actor="operator",
        request_id=request_key,
        with_model=False,
    )
    assert accepted["state"] == "queued"
    assert service.advance_removals(limit=1) == 1
    assert not archive.exists()

    with sessions.begin() as holder:
        locked_gate = holder.scalar(
            select(ArtifactLifecycleGate)
            .where(
                ArtifactLifecycleGate.artifact_kind == "runtime-image",
                ArtifactLifecycleGate.artifact_sha256 == ARCHIVE_SHA,
            )
            .with_for_update()
        )
        assert locked_gate is not None
        assert service.advance_removals(limit=1) == 1

    waiting = service.get_operator_request(request_key, actor="operator")
    assert isinstance(waiting, dict)
    assert waiting["state"] == "partial"
    failure = require_mapping(waiting["failure"], "removal failure")
    assert failure["code"] == "artifact.reference_busy"
    assert failure["retryable"] is True
    retry_time = failure["retry_time"]
    assert isinstance(retry_time, str)
    now[0] = datetime.fromisoformat(retry_time) + timedelta(seconds=1)

    assert service.advance_removals(limit=1) == 1
    recovered = service.get_operator_request(request_key, actor="operator")
    assert isinstance(recovered, dict)
    assert recovered["state"] == "succeeded"


def test_recipe_removal_replay_rejects_malformed_stored_intent(
    tmp_path: Path,
) -> None:
    sessions, _, service, selector = _empty_recipe_removal_owner(tmp_path)
    request_id = "00000000-0000-4000-8000-000000000018"
    accepted = remove_after_review(
        service, selector, actor="operator", request_id=request_id, with_model=False
    )
    assert service.advance_removals(limit=1) == 1
    with sessions.begin() as session:
        operation = session.scalar(select(Job).where(Job.request_id == request_id))
        assert operation is not None
        malformed_payload = dict(require_mapping(operation.payload, "removal owner"))
        plan = dict(require_mapping(malformed_payload["plan"], "removal plan"))
        intent = dict(require_mapping(plan["intent"], "removal intent"))
        intent.pop("with_model")
        plan["intent"] = intent
        malformed_payload["plan"] = plan
        operation.payload = malformed_payload

    with pytest.raises(RecipeImageAvailabilityError) as refused:
        remove_after_review(
            service,
            selector,
            actor="operator",
            request_id=request_id,
            with_model=False,
            review_digest=str(accepted["review_digest"]),
        )

    assert refused.value.code == "recipe_image.operation_invalid"


def test_recipe_removal_replay_rejects_issuer_drift_in_job_envelope(
    tmp_path: Path,
) -> None:
    sessions, _, service, selector = _empty_recipe_removal_owner(tmp_path)
    request_id = "00000000-0000-4000-8000-000000000019"
    accepted = remove_after_review(
        service, selector, actor="operator", request_id=request_id, with_model=False
    )
    assert service.advance_removals(limit=1) == 1
    with sessions.begin() as session:
        operation = session.scalar(select(Job).where(Job.request_id == request_id))
        assert operation is not None
        operation.actor = "another-issuer"

    with pytest.raises(RecipeImageAvailabilityError) as refused:
        remove_after_review(
            service,
            selector,
            actor="operator",
            request_id=request_id,
            with_model=False,
            review_digest=str(accepted["review_digest"]),
        )

    assert refused.value.code == "recipe_image.operation_invalid"


def test_recipe_removal_replay_rejects_integer_stored_model_choice(
    tmp_path: Path,
) -> None:
    sessions, _, service, selector = _empty_recipe_removal_owner(tmp_path)
    request_id = "00000000-0000-4000-8000-000000000020"
    accepted = remove_after_review(
        service, selector, actor="operator", request_id=request_id, with_model=False
    )
    assert service.advance_removals(limit=1) == 1
    with sessions.begin() as session:
        operation = session.scalar(select(Job).where(Job.request_id == request_id))
        assert operation is not None and operation.result is not None
        malformed_result = dict(operation.result) | {"with_model": 0}
        session.execute(
            update(Job)
            .where(Job.request_id == request_id)
            .values(result=malformed_result)
        )
    with sessions() as session:
        operation = session.scalar(select(Job).where(Job.request_id == request_id))
        assert operation is not None and operation.result is not None
        stored_choice = operation.result["with_model"]
        assert isinstance(stored_choice, int) and not isinstance(stored_choice, bool)

    with pytest.raises(RecipeImageAvailabilityError) as refused:
        remove_after_review(
            service,
            selector,
            actor="operator",
            request_id=request_id,
            with_model=False,
            review_digest=str(accepted["review_digest"]),
        )

    assert refused.value.code == "recipe_image.operation_invalid"


@pytest.mark.parametrize(
    "wait_code",
    [
        "recipe_image.build_capacity_wait",
        "build.consumer_busy",
        "build.cancellation_pending",
    ],
)
def test_builder_dependency_wait_remains_durable_queue_after_automatic_limit(
    tmp_path: Path,
    wait_code: str,
) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-capacity-wait", recipe)

    def builder(*_: object, **__: object) -> dict[str, object]:
        raise RecipeImageAvailabilityError(
            wait_code,
            "build dependency has not settled",
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
    assert waiting.failure["code"] == wait_code

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
    assert still_waiting.failure["code"] == wait_code
    with sessions() as session:
        operation = session.get(Job, queued.id)
        assert operation is not None
        retry = operation.payload["retry"]
        assert isinstance(retry, dict) and retry["automatic_attempts"] == 0
        assert operation.payload["claim_owner"] is None
        assert operation.payload["claim_until"] is None


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
    assert service._renew_claim(claim[0]) is True
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


def test_publication_contention_reschedules_without_spending_transfer_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe = _recipe("recipe-image.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    now = [datetime.now(UTC)]
    with sessions.begin() as session:
        _add_revision(session, "publication-contention", recipe)
        session.add(User(subject="operator", role="operator"))
    storage = FilesystemRuntimeImageStorage(tmp_path / "image-cache")
    original_lock = storage.publication_lock
    contended = False

    def contend_once(archive_sha256: str):
        nonlocal contended
        if not contended:
            contended = True
            raise RuntimeImagePreparationError(
                "runtime_image.publication_contended",
                "another owner has the exact image publication lock",
                retryable=True,
                recovery_actions=("retry",),
            )
        return original_lock(archive_sha256)

    monkeypatch.setattr(storage, "publication_lock", contend_once)
    transport = Transport()
    service = RecipeImageAvailabilityService(
        sessions,
        storage=storage,
        authority=lambda recipe_revision_id, **_: (recipe, _runtime()),
        transport=transport,
        clock=lambda: now[0],
    )
    operation = service.start(
        "publication-contention",
        actor="operator",
        request_id="publication-contention-request",
    )
    first_claim = service.claim_pending(owner_id="publication-worker")[0]
    service.run_claim(first_claim)

    assert service.get(operation.id).state == "queued"
    with sessions() as session:
        row = session.get(Job, operation.id)
        assert row is not None
        retry_state = row.payload["retry"]
        assert isinstance(retry_state, Mapping)
        assert retry_state["automatic_attempts"] == 0
    now[0] += timedelta(seconds=6)
    retry = service.claim_pending(owner_id="publication-worker")[0]
    service.run_claim(retry)
    assert service.get(operation.id).state == "succeeded"
    assert transport.calls == 2
    engine.dispose()


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


def test_postgres_model_child_lock_contention_resumes_same_preparation(
    tmp_path: Path, postgres_engine
) -> None:
    """NOWAIT contention must defer the parent and reuse its exact child."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    recipe = _recipe("recipe-image.json")
    now = [datetime.now(UTC)]
    with sessions.begin() as session:
        revision = _add_revision(session, "revision-pg-model-lock", recipe)
        _add_head(session, revision)

    child = SimpleNamespace(
        id="00000000-0000-4000-8000-000000000301",
        request_key="00000000-0000-4000-8000-000000000302",
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
            now=now[0],
        ),
        failure=None,
    )
    model_content_sha256 = recipe.models[0].model.content_sha256
    _persist_fake_model_cache_child(
        sessions,
        child=child,
        model_content_sha256=model_content_sha256,
        now=now[0],
    )

    class ModelCache:
        start_calls = 0

        def download_preview(self, *, recipe_revision_id: str) -> dict[str, object]:
            assert recipe_revision_id == revision.id
            return {
                "plan_digest": child.plan_digest,
                "artifact_set_sha256": child.artifact_set_sha256,
                "new_bytes": 0,
            }

        def resolve_artifact_set(self, *, recipe_revision_id: str) -> SimpleNamespace:
            assert recipe_revision_id == revision.id
            return SimpleNamespace(
                digest=child.artifact_set_sha256,
                document=lambda: {
                    "model_content_digests": [model_content_sha256],
                    "artifacts": [],
                },
            )

        def list_operations(self, *, limit: int) -> tuple[object, ...]:
            assert limit > 0
            return (child,)

        def start_download(self, **_: object) -> SimpleNamespace:
            self.start_calls += 1
            raise AssertionError("a lock retry must reuse the existing child")

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
        clock=lambda: now[0],
        claim_lease_seconds=10,
    )
    request_id = "10000000-0000-4000-8000-000000000301"
    queued = service.start(revision.id, actor="operator", request_id=request_id)
    claim = service.claim_pending(owner_id="lock-contention-worker")[0]

    locker = sessions()
    try:
        locked_child = locker.scalar(
            select(ModelCacheOperation)
            .where(ModelCacheOperation.id == child.id)
            .with_for_update()
        )
        assert locked_child is not None
        service.run_claim(claim)
    finally:
        locker.rollback()
        locker.close()

    waiting = service.get(queued.id)
    assert waiting.state == "queued"
    assert waiting.failure is not None
    assert waiting.failure["retryable"] is True
    assert waiting.failure["code"] == "operationalerror"
    with sessions() as session:
        stored_waiting = session.get(Job, queued.id)
        assert stored_waiting is not None
        assert stored_waiting.request_id == request_id
        assert stored_waiting.state == "queued"
        retry_state = stored_waiting.payload["retry"]
        assert isinstance(retry_state, Mapping)
        assert retry_state["automatic_attempts"] == 1
        retry_after_at = stored_waiting.payload["retry_after_at"]
        assert isinstance(retry_after_at, str)
        retry_at = datetime.fromisoformat(retry_after_at)
        assert stored_waiting.payload.get("model_child") is None

    assert service.claim_pending(owner_id="too-early-worker") == ()
    with sessions() as session:
        still_waiting = session.get(Job, queued.id)
        assert still_waiting is not None
        assert still_waiting.payload["retry_after_at"] == retry_at.isoformat()

    # Let the original bounded retry deadline elapse. The retry must select the
    # existing completed model operation under the same parent request.
    _persist_fake_model_cache_child(
        sessions,
        child=SimpleNamespace(
            id=child.id,
            request_key=child.request_key,
            state="succeeded",
            artifact_set_sha256=child.artifact_set_sha256,
            plan_digest=child.plan_digest,
            progress=cache_progress(
                {
                    "phase": "completed",
                    "downloaded_bytes": 100,
                    "expected_bytes": 100,
                    "completed_artifacts": 1,
                    "total_artifacts": 1,
                },
                previous=None,
                now=now[0],
            ),
            failure=None,
        ),
        model_content_sha256=model_content_sha256,
        now=now[0],
    )
    child.state = "succeeded"
    child.progress = cache_progress(
        {
            "phase": "completed",
            "downloaded_bytes": 100,
            "expected_bytes": 100,
            "completed_artifacts": 1,
            "total_artifacts": 1,
        },
        previous=None,
        now=now[0],
    )
    now[0] = max(now[0] + timedelta(seconds=1), retry_at)
    retry = service.claim_pending(owner_id="lock-contention-recovery-worker")
    assert len(retry) == 1
    assert retry[0].operation_id == queued.id
    service.run_claim(retry[0])

    recovered = service.get(queued.id)
    assert recovered.state == "succeeded"
    assert recovered.result is not None
    recovered_child = require_mapping(
        recovered.result["model_child"], "recovered model child"
    )
    assert recovered_child["id"] == child.id
    assert model_cache.start_calls == 0
    with sessions() as session:
        stored_recovered = session.get(Job, queued.id)
        assert stored_recovered is not None
        assert stored_recovered.request_id == request_id
        assert stored_recovered.current_attempt == 2


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
    older = service.start("revision-superseded", actor="operator", request_id="o" * 36)
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
    older = service.start("revision-superseded", actor="operator", request_id="o" * 36)
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
    now = datetime.now(UTC)

    child = SimpleNamespace(
        id="00000000-0000-4000-8000-000000000101",
        request_key="00000000-0000-4000-8000-000000000102",
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
            now=now,
        ),
        failure=None,
    )
    _persist_fake_model_cache_child(
        sessions,
        child=child,
        model_content_sha256=next(
            model.model.content_sha256 for model in recipe.models
        ),
        now=now,
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
        clock=lambda: now,
    )
    queued = service.start(
        "revision-model-image", actor="operator", request_id="m" * 36
    )
    assert queued.model_child is None
    assert model_cache.start_calls == 0
    # Only the worker can issue children, after the parent is durable.
    assert service.run_pending() == 1
    for actor, key, force in [
        ("operator-2", "n" * 36, False),
        ("operator-3", "f" * 36, True),
    ]:
        later = service.start(
            "revision-model-image", actor=actor, request_id=key, force=force
        )
        assert later.model_child is None
        assert service.run_pending() == 1
        observed_child = service.get(later.id).model_child
        assert observed_child is not None
        assert observed_child["id"] == child.id
    assert model_cache.start_calls == 1
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
    now = datetime.now(UTC)
    child = SimpleNamespace(
        id="00000000-0000-4000-8000-000000000201",
        request_key="00000000-0000-4000-8000-000000000202",
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
            now=now,
        ),
        failure=None,
    )
    _persist_fake_model_cache_child(
        sessions,
        child=child,
        model_content_sha256=next(
            model.model.content_sha256 for model in recipe.models
        ),
        now=now,
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
        clock=lambda: now,
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
        id="00000000-0000-4000-8000-000000000301",
        request_key="00000000-0000-4000-8000-000000000302",
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
        id="00000000-0000-4000-8000-000000000303",
        request_key="00000000-0000-4000-8000-000000000304",
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
    now = datetime.now(UTC)
    model_content_sha256 = next(model.model.content_sha256 for model in recipe.models)
    _persist_fake_model_cache_child(
        sessions,
        child=failed,
        model_content_sha256=model_content_sha256,
        now=now,
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
                _persist_fake_model_cache_child(
                    sessions,
                    child=failed,
                    model_content_sha256=model_content_sha256,
                    now=datetime.now(UTC),
                )
            return failed

        def repair_preview(self, artifact_set_sha256: str) -> dict[str, object]:
            assert artifact_set_sha256 == "c" * 64
            return {"plan_digest": "e" * 64}

        def start_repair(self, **kwargs: object) -> SimpleNamespace:
            self.repair_calls.append(kwargs)
            _persist_fake_model_cache_child(
                sessions,
                child=repaired,
                model_content_sha256=model_content_sha256,
                now=datetime.now(UTC),
            )
            return repaired

    model_cache = ModelCache()
    transport = Transport()
    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        transport=transport,
        model_cache=model_cache,
        clock=lambda: now,
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
    assert resumed.model_child["id"] == failed.id
    assert model_cache.repair_calls == []
    assert service.run_pending() == 1
    assert len(model_cache.repair_calls) == 1
    assert model_cache.repair_calls[0]["artifact_set_sha256"] == "c" * 64
    assert model_cache.repair_calls[0]["plan_digest"] == "e" * 64

    completed = service.get(resumed.id)
    assert completed.state == "succeeded"
    assert completed.result is not None
    assert (
        require_mapping(completed.result["model_child"], "model child")["id"]
        == repaired.id
    )
    assert transport.calls == 1


def test_accepted_cancellation_is_idempotent_and_prevents_queued_dispatch(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-image.json")
    engine = create_engine(f"sqlite:///{tmp_path / 'cancel-queued.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "cancel-queued-revision", recipe)
        session.add(User(subject="operator", role="operator"))
    transport = Transport()
    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path / "image-cache"),
        authority=lambda recipe_revision_id, **_: (recipe, _runtime()),
        transport=transport,
        clock=lambda: datetime.now(UTC),
    )
    operation = service.start(
        "cancel-queued-revision", actor="operator", request_id="cancel-queued"
    )
    cancel_key = "00000000-0000-4000-8000-000000000901"

    accepted = service.cancel(
        operation.id,
        actor="operator",
        request_id=cancel_key,
        reason="stop queued preparation",
    )
    assert accepted.state == "cancelling"
    assert accepted.cancellation is not None
    replay = service.cancel(
        operation.id,
        actor="operator",
        request_id=cancel_key,
        reason="stop queued preparation",
    )
    assert replay.cancellation == accepted.cancellation
    with pytest.raises(RecipeImageAvailabilityError) as reused:
        service.cancel(
            operation.id,
            actor="operator",
            request_id="00000000-0000-4000-8000-000000000902",
            reason="another cancellation intent",
        )
    assert reused.value.code == "recipe_image.cancel_request_key_reused"

    assert service.run_pending() == 0
    assert service.get(operation.id).state == "cancelled"
    assert transport.calls == 0
    engine.dispose()


def test_late_verified_image_result_cannot_publish_after_cancellation(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-image.json")
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'cancel-late.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "cancel-late-revision", recipe)
        session.add(User(subject="operator", role="operator"))
    now = datetime.now(UTC)
    entered = threading.Event()
    release = threading.Event()

    class BlockingTransport(Transport):
        def pull_and_export(self, reference: str, destination: Path, **kwargs):
            entered.set()
            if not release.wait(timeout=10):
                raise TimeoutError("test transport was not released")
            return super().pull_and_export(reference, destination, **kwargs)

    transport = BlockingTransport()
    storage = FilesystemRuntimeImageStorage(tmp_path / "image-cache")
    service = RecipeImageAvailabilityService(
        sessions,
        storage=storage,
        authority=lambda recipe_revision_id, **_: (recipe, _runtime()),
        transport=transport,
        clock=lambda: now,
        claim_lease_seconds=30,
    )
    operation = service.start(
        "cancel-late-revision", actor="operator", request_id="cancel-late"
    )
    claim = service.claim_pending(owner_id="late-image-result")[0]
    with ThreadPoolExecutor(max_workers=1) as executor:
        worker = executor.submit(service.run_claim, claim)
        assert entered.wait(timeout=5), "worker did not reach the issued image pull"
        accepted = service.cancel(
            operation.id,
            actor="operator",
            request_id="00000000-0000-4000-8000-000000000903",
            reason="stop image preparation",
        )
        assert accepted.state == "cancelling"
        service.reconcile_cancellations()
        assert service.get(operation.id).state == "cancelling"
        release.set()
        worker.result(timeout=10)

    observed = service.get(operation.id)
    assert observed.state == "cancelled"
    assert observed.result is None
    assert observed.cancellation is not None
    retained = storage.verify_existing(ARCHIVE_SHA, len(ARCHIVE))
    assert retained.read_bytes() == ARCHIVE
    with sessions() as session:
        assert session.scalar(select(RuntimeImageAuthorization)) is None
        row = session.get(Job, operation.id)
        assert row is not None and row.state == "cancelled"
        assert "image_result" not in row.payload
        assert "image_reference_intent" not in row.payload
    engine.dispose()


def test_cancelling_image_reference_intent_is_counted_until_claim_release(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-image.json")
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'cancel-reference.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        revision = _add_revision(session, "cancel-reference-revision", recipe)
        revision_id = revision.id
        session.add(User(subject="operator", role="operator"))
    now = datetime.now(UTC)
    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path / "image-cache"),
        authority=lambda recipe_revision_id, **_: (recipe, _runtime()),
        transport=Transport(),
        clock=lambda: now,
    )
    operation = service.start(
        revision_id, actor="operator", request_id="cancel-reference"
    )
    claim = service.claim_pending(owner_id="cancel-reference-writer")[0]
    accepted = service.cancel(
        operation.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000904",
        reason="stop image preparation",
    )
    assert accepted.state == "cancelling"

    service._persist_provisional_image_reference(
        claim,
        receipt=_reference_receipt(),
    )
    with sessions() as session:
        row = session.get(Job, operation.id)
        assert row is not None
        reference = read_runtime_image_reference_intent(
            row.payload["image_reference_intent"]
        )
        assert reference == RuntimeImageReferenceIntent(
            schema_version=2,
            operation_id=operation.id,
            recipe_revision_id=revision_id,
            attempt=claim.execution_attempt,
            claim_owner=claim.claim_owner,
            oci_archive_sha256=ARCHIVE_SHA,
            image_digest=PLATFORM_DIGEST,
            image_bytes=len(ARCHIVE),
        )
        assert runtime_image_reference_reasons(session, [ARCHIVE_SHA]) == {
            ARCHIVE_SHA: (f"image publication operation {operation.id}",)
        }
        findings = runtime_image_reference_findings(session, [ARCHIVE_SHA])
        assert [
            (
                item.asset.kind,
                item.asset.sha256,
                item.owner_kind,
                item.owner_id,
                item.state,
                item.classification,
                item.reason,
            )
            for item in findings[ARCHIVE_SHA]
        ] == [
            (
                "runtime-image",
                ARCHIVE_SHA,
                "recipe-image-availability-operation",
                operation.id,
                "cancelling",
                "active-work",
                f"image publication operation {operation.id}",
            )
        ]
        persisted_reference = reference.model_dump(mode="json")

    with sessions.begin() as session:
        row = session.get(Job, operation.id)
        assert row is not None
        payload = dict(row.payload)
        payload["image_reference_intent"] = persisted_reference | {
            "unrecognized": "must be rejected"
        }
        row.payload = payload
    with pytest.raises(RuntimeImagePreparationError):
        service._persist_provisional_image_reference(
            claim,
            receipt=_reference_receipt(),
        )
    with sessions() as session, pytest.raises(ArtifactLifecycleError) as malformed:
        runtime_image_reference_reasons(session, [ARCHIVE_SHA])
    assert malformed.value.code == "artifact.reference_scan_failed"
    with sessions.begin() as session:
        row = session.get(Job, operation.id)
        assert row is not None
        payload = dict(row.payload)
        payload["image_reference_intent"] = persisted_reference
        row.payload = payload

    with service._storage.publication_lock(ARCHIVE_SHA):
        service.reconcile_cancellations()
        assert service.get(operation.id).state == "cancelling"
        with sessions() as session:
            row = session.get(Job, operation.id)
            assert row is not None
            assert row.payload["claim_owner"] == claim.claim_owner
            assert "image_reference_intent" in row.payload

    assert service._release_cancelled_claim(claim)
    assert service._reconcile_availability_cancellation(operation.id)
    assert service.get(operation.id).state == "cancelled"
    engine.dispose()


@pytest.mark.parametrize("mutation", ["stale-owner", "removal-fence"])
def test_cancelling_reference_intent_rejects_stale_or_fenced_claim(
    tmp_path: Path, mutation: str
) -> None:
    recipe = _recipe("recipe-image.json")
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / f'cancel-reference-{mutation}.sqlite'}"
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        revision = _add_revision(session, f"cancel-reference-{mutation}", recipe)
        revision_id = revision.id
        session.add(User(subject="operator", role="operator"))
    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path / "image-cache"),
        authority=lambda recipe_revision_id, **_: (recipe, _runtime()),
        transport=Transport(),
        clock=lambda: datetime.now(UTC),
    )
    operation = service.start(
        revision_id, actor="operator", request_id=f"cancel-reference-{mutation}"
    )
    claim = service.claim_pending(owner_id="cancel-reference-writer")[0]
    accepted = service.cancel(
        operation.id,
        actor="operator",
        request_id=(
            "00000000-0000-4000-8000-000000000905"
            if mutation == "stale-owner"
            else "00000000-0000-4000-8000-000000000906"
        ),
        reason="stop image preparation",
    )
    assert accepted.state == "cancelling"
    with sessions.begin() as session:
        row = session.get(Job, operation.id)
        assert row is not None
        payload = dict(row.payload)
        if mutation == "stale-owner":
            payload["claim_owner"] = "replacement-writer"
        else:
            payload["removal_fence"] = "reserved-for-removal"
        row.payload = payload

    with pytest.raises(RuntimeImagePreparationError):
        service._persist_provisional_image_reference(
            claim,
            receipt=_reference_receipt(),
        )
    with sessions() as session:
        row = session.get(Job, operation.id)
        assert row is not None
        assert "image_reference_intent" not in row.payload
    engine.dispose()


def test_cancelling_one_parent_preserves_a_shared_partial_model_transfer(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-image.json")
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'cancel-shared-model.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    revision_id = "cancel-shared-revision"
    with sessions.begin() as session:
        _add_revision(session, revision_id, recipe)
        session.add(User(subject="operator", role="operator"))
    cache = ModelCacheService(
        sessions,
        tmp_path / "model-cache",
        reserve_bytes=0,
        fixture_sources=True,
    )
    source = tmp_path / "weights.source"
    source.write_bytes(b"shared model weights")
    model_digest = "a" * 64
    artifact = {
        "id": "weights",
        "path": "weights.bin",
        "kind": "file",
        "source": source.as_uri(),
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "download_bytes": source.stat().st_size,
        "roles": ["weights"],
        "model_content_sha256": model_digest,
    }
    parent_request_id = "first-model-consumer"
    child_request_key = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"vonk:recipe-availability-model:{revision_id}:{parent_request_id}",
        )
    )
    preview = cache.download_preview(
        model_content_sha256=model_digest, artifacts=[artifact]
    )
    child = cache.start_download(
        actor="operator",
        request_key=child_request_key,
        plan_digest=str(preview["plan_digest"]),
        model_content_sha256=model_digest,
        artifacts=[artifact],
        interrupt_after_bytes=1,
    )
    assert child.state == "partial"
    before_bytes = child.progress["downloaded_bytes"]

    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path / "image-cache"),
        authority=lambda recipe_revision_id, **_: (recipe, _runtime()),
        transport=Transport(),
        model_cache=cache,
        clock=lambda: datetime.now(UTC),
    )
    first = service.start(revision_id, actor="operator", request_id=parent_request_id)
    second = service.start(
        revision_id, actor="operator", request_id="second-model-consumer"
    )
    with sessions.begin() as session:
        for operation_id in (first.id, second.id):
            row = session.get(Job, operation_id)
            assert row is not None
            row.payload = dict(row.payload) | {
                "model_child": {
                    "id": child.id,
                    "artifact_set_sha256": child.artifact_set_sha256,
                }
            }

    service.cancel(
        first.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000904",
        reason="stop one preparation",
    )
    service.reconcile_cancellations()

    assert service.get(first.id).state == "cancelled"
    assert service.get(second.id).state == "queued"
    still_shared = cache.get_operation(child.id)
    assert still_shared.state == "partial"
    assert still_shared.progress["downloaded_bytes"] == before_bytes
    cache.close()
    engine.dispose()


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
        "request": RecipeRevisionIntent(
            recipe_revision_id="revision-progress"
        ).model_dump(mode="json"),
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
