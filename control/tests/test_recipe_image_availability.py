from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
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
        authority=lambda _revision_id: (recipe, _runtime()),
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


def test_claim_identity_uses_authoritative_image_and_running_claim_is_not_repeated(tmp_path: Path) -> None:
    recipe = _recipe("recipe-image.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-identity", recipe)
    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda _revision_id: (recipe, _runtime()),
        transport=Transport(),
        clock=lambda: datetime.now(UTC),
    )
    service.start("revision-identity", actor="operator", request_id="4" * 36)
    claim = service.claim_pending(owner_id="worker-a")
    assert claim and claim[0].image_identity == IMAGE_DIGEST
    assert service.claim_pending(owner_id="worker-b") == ()


def test_same_immutable_image_reuses_preparation_across_recipe_revisions(tmp_path: Path) -> None:
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
        authority=lambda revision_id: (recipe if revision_id.endswith("-a") else recipe_b, _runtime()),
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


def test_request_replay_returns_original_before_metadata_refresh(tmp_path: Path) -> None:
    recipe = _recipe("recipe-image.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-replay", recipe)
    calls = 0

    def authority(_revision_id: str) -> tuple[RecipeDefinition, dict[str, object]]:
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


def test_same_work_identity_keeps_distinct_authorization_operations(tmp_path: Path) -> None:
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
        authority=lambda _revision_id: (recipe, _runtime()),
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


def test_force_download_is_a_distinct_operation_for_same_revision(tmp_path: Path) -> None:
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
        authority=lambda _revision_id: (recipe, _runtime()),
        transport=transport,
        clock=lambda: datetime.now(UTC),
    )
    cached = service.start("revision-force", actor="operator", request_id="a" * 36)
    forced = service.start("revision-force", actor="operator", request_id="b" * 36, force=True)
    assert forced.id != cached.id
    for claim in service.claim_pending(limit=2, owner_id="worker-a"):
        service.run_claim(claim)
    assert service.get(cached.id).state == "succeeded"
    assert service.get(forced.id).state == "succeeded"
    assert transport.calls == 2
