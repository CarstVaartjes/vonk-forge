from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from vonk_forge_contracts import RecipeDefinition, content_sha256

from vonk_control.models import Base, CatalogDocumentRevision, Job
from vonk_control.recipe_image_availability import (
    RecipeImageAvailabilityError,
    RecipeImageAvailabilityService,
)
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    PulledImageEvidence,
    RuntimeImagePreparationError,
    prepare_runtime_image,
)

IMAGE_DIGEST = "sha256:" + "d" * 64
PLATFORM_DIGEST = "sha256:" + "e" * 64
CONFIG_DIGEST = "sha256:" + "c" * 64
ARCHIVE = b"availability image archive"
ARCHIVE_SHA = hashlib.sha256(ARCHIVE).hexdigest()


def _recipe(name: str) -> RecipeDefinition:
    raw = json.loads(files("vonk_forge_contracts").joinpath("examples", name).read_text())
    return RecipeDefinition.model_validate(raw)


def _runtime() -> dict[str, object]:
    return {"architecture": "linux/arm64", "interface": "vonk.runtime.v1", "image_bytes": len(ARCHIVE)}


def _build_runtime() -> dict[str, object]:
    return _runtime() | {"build_input_sha256": "f" * 64}


class Transport:
    def __init__(self, payload: bytes = ARCHIVE) -> None:
        self.calls = 0
        self.payload = payload

    def pull_and_export(self, reference: str, destination: Path, **_: object) -> PulledImageEvidence:
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


def _add_revision(session: Session, revision_id: str, recipe: RecipeDefinition) -> CatalogDocumentRevision:
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
        projected={},
        created_by="test",
        created_at=datetime.now(UTC),
    )
    session.add(revision)
    return revision


def test_force_download_skips_verified_cache_but_preserves_archive(tmp_path: Path) -> None:
    recipe = _recipe("recipe-image.json")
    storage = FilesystemRuntimeImageStorage(tmp_path)
    transport = Transport()
    first = prepare_runtime_image(recipe, runtime=_runtime(), storage=storage, transport=transport)
    second = prepare_runtime_image(recipe, runtime=_runtime(), storage=storage, transport=transport)
    forced = prepare_runtime_image(recipe, runtime=_runtime(), storage=storage, transport=transport, force=True)

    assert first == second == forced
    assert transport.calls == 2
    assert Path(first.archive_path).read_bytes() == ARCHIVE


def test_forced_digest_failure_does_not_replace_valid_archive(tmp_path: Path) -> None:
    recipe = _recipe("recipe-image.json")
    storage = FilesystemRuntimeImageStorage(tmp_path)
    transport = Transport()
    first = prepare_runtime_image(recipe, runtime=_runtime(), storage=storage, transport=transport)

    class Wrong(Transport):
        def pull_and_export(self, reference: str, destination: Path, **_: object) -> PulledImageEvidence:
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
        prepare_runtime_image(recipe, runtime=_runtime(), storage=storage, transport=Wrong(), force=True)
    assert Path(first.archive_path).read_bytes() == ARCHIVE


def test_build_failure_is_bounded_and_exposes_step_and_retry_contract(tmp_path: Path) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-source", recipe)

    def authority(_revision_id: str) -> tuple[RecipeDefinition, dict[str, object]]:
        return recipe, _build_runtime()

    def builder(*_: object, **__: object) -> dict[str, object]:
        raise RecipeImageAvailabilityError(
            "recipe_image.build_failed", "compiler failed at step 4", retryable=True,
            recovery_actions=("retry",), log_excerpt="Step 4: compiler failed", step="Step 4",
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
        "revision-source", actor="operator", request_id="1" * 36,
        build_input_sha256="f" * 64,
    )
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
        authority=lambda _revision_id: (recipe, _runtime()),
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
        operation.payload = dict(operation.payload) | {"claim_until": "2000-01-01T00:00:00+00:00"}
    reclaimed = service.claim_pending(owner_id="worker-b")
    assert reclaimed and reclaimed[0].claim_owner == "worker-b"
