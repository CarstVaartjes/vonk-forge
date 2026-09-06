from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.models import Base, CatalogDocument, CatalogDocumentRevision, Job
from vonk_control.recipe_image_availability import (
    RecipeImageAvailabilityError,
    RecipeImageAvailabilityService,
)
from vonk_control.recipe_image_availability_api import _view_document
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    PulledImageEvidence,
    RuntimeImagePreparationError,
    prepare_runtime_image,
)
from vonk_forge_contracts import RecipeDefinition, content_sha256

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

    def authority(_revision_id: str, *, force: bool = False) -> tuple[RecipeDefinition, dict[str, object]]:
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
        authority=lambda _revision_id, *, force=False: (recipe, _build_runtime()),
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
        authority=lambda _revision_id, *, force=False: (recipe, _build_runtime()),
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
        authority=lambda _revision_id, *, force=False: (recipe, _runtime()),
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
        authority=lambda _revision_id, *, force=False: (recipe, _runtime()),
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
        authority=lambda _revision_id, *, force=False: (recipe, _runtime()),
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

    def authority(revision_id: str, *, force: bool = False):
        del force
        if revision_id == "revision-pg-image":
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
    image = first.start(
        "revision-pg-image", actor="operator", request_id="p" * 36
    )
    build_a = first.start(
        "revision-pg-build", actor="operator", request_id="q" * 36
    )
    build_b = first.start(
        "revision-pg-build", actor="operator", request_id="r" * 36
    )

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
    assert {claim.operation_id for claim in claimed} <= {image.id, build_a.id, build_b.id}

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
        sibling = session.get(Job, build_b.id if build_claim.operation_id == build_a.id else build_a.id)
        assert sibling is not None
        sibling.updated_at = datetime.now(UTC) + timedelta(seconds=10)
    reclaimed = first.claim_pending(limit=1, owner_id="pg-worker-d")
    assert len(reclaimed) == 1
    assert reclaimed[0].operation_id == build_claim.operation_id
    assert first.claim_pending(limit=1, owner_id="pg-worker-e") == ()


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
        authority=lambda revision_id, *, force=False: (recipe if revision_id.endswith("-a") else recipe_b, _runtime()),
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

    def authority(_revision_id: str, *, force: bool = False) -> tuple[RecipeDefinition, dict[str, object]]:
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
        authority=lambda _revision_id, *, force=False: (recipe, _runtime()),
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


def test_model_child_and_image_complete_through_one_sql_operation(tmp_path: Path) -> None:
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
        progress={"phase": "download", "completed_bytes": 1024, "total_bytes": 1024, "total_bytes_known": True},
        failure=None,
    )

    class ModelCache:
        def __init__(self) -> None:
            self.start_calls = 0

        def download_preview(self, *, recipe_revision_id: str) -> dict[str, object]:
            assert recipe_revision_id == "revision-model-image"
            return {"plan_digest": "d" * 64, "artifact_set_sha256": "c" * 64}

        def resolve_artifact_set(self, *, recipe_revision_id: str) -> SimpleNamespace:
            return SimpleNamespace(digest="c" * 64, document=lambda: {"model_versions": [], "artifacts": []})

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
        authority=lambda _revision_id, *, force=False: (recipe, _runtime()),
        transport=Transport(),
        model_cache=model_cache,
        clock=lambda: datetime.now(UTC),
    )
    queued = service.start("revision-model-image", actor="operator", request_id="m" * 36)
    assert queued.model_child is not None
    assert queued.model_child["id"] == child.id
    assert model_cache.start_calls == 1
    second = service.start("revision-model-image", actor="operator-2", request_id="n" * 36)
    assert second.model_child is not None
    assert second.model_child["id"] == child.id
    assert model_cache.start_calls == 1
    forced = service.start("revision-model-image", actor="operator-3", request_id="f" * 36, force=True)
    assert forced.model_child is not None
    assert forced.model_child["id"] == child.id
    assert model_cache.start_calls == 1
    assert service.run_pending() == 1
    completed = service.get(queued.id)
    assert completed.state == "succeeded"
    assert completed.result is not None
    assert completed.result["model_child"]["id"] == child.id


def test_model_and_image_children_advance_independently_and_reuse_image(tmp_path: Path) -> None:
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
        progress={"phase": "download", "completed_bytes": 40, "total_bytes": 100, "total_bytes_known": True},
        failure=None,
    )

    class ModelCache:
        def download_preview(self, **_: object) -> dict[str, object]:
            return {"plan_digest": "d" * 64, "artifact_set_sha256": "c" * 64}

        def resolve_artifact_set(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(digest="c" * 64, document=lambda: {"model_versions": [], "artifacts": []})

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
        authority=lambda _revision_id, *, force=False: (recipe, _runtime()),
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
        item for item in _view_document(partial).children if item.kind == "runtime-image"
    )
    assert image_child.state == "succeeded"
    assert image_child.progress.completed_bytes == len(ARCHIVE)
    assert partial.progress["members"][-1]["member_id"] == "model-cache"
    child.state = "succeeded"
    with sessions.begin() as session:
        row = session.get(Job, queued.id)
        assert row is not None
        row.payload = dict(row.payload) | {"retry_after_at": "2000-01-01T00:00:00+00:00"}
    assert service.run_pending() == 1
    assert service.get(queued.id).state == "succeeded"
    assert transport.calls == 1


def test_recipe_retry_uses_model_access_recheck_for_terminal_auth(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    failed = SimpleNamespace(
        id="failed-model",
        request_key="failed-request",
        state="failed",
        artifact_set_sha256="c" * 64,
        plan_digest="d" * 64,
        progress={"phase": "download", "completed_bytes": 4, "total_bytes": 10, "total_bytes_known": True},
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

        def check_access_and_resume(self, operation_id: str, **kwargs: object) -> SimpleNamespace:
            self.called = {"operation_id": operation_id, **kwargs}
            return failed

        def list_operations(self, **_: object) -> tuple[object, ...]:
            return ()

    cache = ModelCache()
    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda _revision_id, *, force=False: (_recipe("recipe-image.json"), _runtime()),
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
        progress={"phase": "download", "completed_bytes": 4, "total_bytes": 10, "total_bytes_known": True},
        failure=None,
    )
    repaired = SimpleNamespace(
        id="model-repair",
        request_key="model-repair-request",
        state="succeeded",
        artifact_set_sha256="c" * 64,
        plan_digest="e" * 64,
        progress={"phase": "download", "completed_bytes": 10, "total_bytes": 10, "total_bytes_known": True},
        failure=None,
    )

    class ModelCache:
        def __init__(self) -> None:
            self.failed = False
            self.repair_calls: list[dict[str, object]] = []

        def download_preview(self, **_: object) -> dict[str, object]:
            return {"plan_digest": "d" * 64, "artifact_set_sha256": "c" * 64}

        def resolve_artifact_set(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(
                digest="c" * 64,
                document=lambda: {"model_versions": [], "artifacts": []},
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
        authority=lambda _revision_id, *, force=False: (recipe, _runtime()),
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
        row.payload = dict(row.payload) | {"retry_after_at": "2000-01-01T00:00:00+00:00"}
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
    assert completed.result["model_child"]["id"] == repaired.id
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
        authority=lambda _revision_id, *, force=False: (recipe, _runtime()),
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
        authority=lambda _revision_id, *, force=False: (recipe, _runtime()),
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
            "progress": {
                "phase": "download",
                "downloaded_bytes": 40,
                "expected_bytes": 100,
                "total_bytes_known": True,
            },
        },
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
    members = {member["member_id"]: member for member in view.progress["members"]}
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
