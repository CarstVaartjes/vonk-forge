"""Recipe image availability: recipe removal."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_forge_contracts import RecipeDefinition

from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
from vonk_control.artifact_jobs.lifecycle import (
    ArtifactIdentity,
    check_removal_fence_nowait,
)
from vonk_control.bounded_json import require_mapping
from vonk_control.model_primitives import Base
from vonk_control.models.cache import (
    ArtifactLifecycleGate,
    RecipeBuild,
)
from vonk_control.models.catalog import (
    CatalogDocumentHead,
)
from vonk_control.models.fleet import (
    AgentNode,
    FleetProfile,
    FleetProfileApplication,
)
from vonk_control.models.jobs import Job
from vonk_control.recipe_image_availability.contracts import (
    RecipeImageAvailabilityError,
)
from vonk_control.recipe_image_availability.removal_contract import (
    RecipeCacheRemovalOwner,
)
from vonk_control.recipe_image_availability.service import (
    RecipeImageAvailabilityService,
)
from vonk_control.recipe_image_availability.view_contract import (
    RecipeCacheRemovalStatus,
    RecipeImageAvailabilityView,
)
from vonk_control.runtime_image_preparation.contracts import (
    RuntimeImagePreparationError,
)
from vonk_control.runtime_image_preparation.storage import FilesystemRuntimeImageStorage

from .non_blocking import assert_ended_without_blocking
from .recipe_image_availability_support import (
    ARCHIVE,
    ARCHIVE_SHA,
    IMAGE_DIGEST,
    _add_head,
    _add_revision,
    _build_id,
    _build_runtime,
    _empty_recipe_removal_owner,
    _published,
    _recipe,
    _reference_receipt,
    _runtime,
    _service,
)
from .recipe_removal_review_support import remove_after_review
from .runtime_image_fixtures import place_test_image


class _ExitAfterImageUnlinkStorage(FilesystemRuntimeImageStorage):
    def remove_published(self, archive_sha256: str) -> int:
        super().remove_published(archive_sha256)
        os._exit(73)


def _exit_after_recipe_image_unlink(database_url: str, artifact_root: str) -> None:
    engine = create_engine(database_url)
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = _service(
        sessions,
        storage=_ExitAfterImageUnlinkStorage(Path(artifact_root)),
        authority=lambda *_args, **_kwargs: (
            _recipe("recipe-source-build.json"),
            _runtime(),
        ),
        clock=lambda: datetime.now(UTC),
    )
    service.advance_removals(limit=1)
    os._exit(74)


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
        assert build is not None
        assert build.state == "building"
    assert service.advance_removals(limit=1) == 1
    with sessions() as session:
        ended = session.scalar(select(Job).where(Job.id == str(result["operation_id"])))
        assert ended is not None
        assert ended.state == "succeeded"
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
            assert waiting is not None
            assert waiting.state == LifecycleState.BACKOFF
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
        assert completed is not None
        assert completed.state == LifecycleState.SUCCEEDED
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
        assert bad is not None
        assert bad.state == LifecycleState.SUCCEEDED
        assert good is not None
        assert good.state == LifecycleState.SUCCEEDED
        from vonk_control.models.cache import ArtifactLifecycleGate

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
        assert ended is not None
        assert ended.state == LifecycleState.SUCCEEDED
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
        assert ended is not None
        assert ended.state == "succeeded"
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
        assert ended is not None
        assert ended.state == "succeeded"
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


def _seed_archives(
    sessions: sessionmaker[Session],
    storage: FilesystemRuntimeImageStorage,
    revision_id: str,
    recipe: RecipeDefinition,
    count: int,
    *,
    salt: bytes = b"",
) -> tuple[list[str], list[Path]]:
    """One revision with ``count`` built images, each placed with its receipt."""

    first = ARCHIVE + salt
    archives = [first] + [first + b" %d" % number for number in range(1, count)]
    with sessions.begin() as session:
        revision = _add_revision(session, revision_id, recipe, archive=first)
        _add_head(session, revision)
        for number, archive in enumerate(archives[1:], start=1):
            session.add(
                RecipeBuild(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{revision_id}:{number}")),
                    recipe_revision_id=revision_id,
                    builder_node_id="spark-builder",
                    source_bundle_sha256="b" * 64,
                    build_input_sha256=f"{number:x}" * 64,
                    state="succeeded",
                    policy_report={},
                    plan={},
                    image_digest=IMAGE_DIGEST,
                    oci_layout_sha256=hashlib.sha256(archive).hexdigest(),
                    image_bytes=len(archive),
                    error=None,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
    digests: list[str] = []
    receipts: list[Path] = []
    for number, archive in enumerate(archives):
        digest = hashlib.sha256(archive).hexdigest()
        build_id = (
            _build_id(revision_id)
            if number == 0
            else str(uuid.uuid5(uuid.NAMESPACE_URL, f"{revision_id}:{number}"))
        )
        receipt = _reference_receipt(build_id).model_copy(
            update={"oci_archive_sha256": digest, "image_bytes": len(archive)}
        )
        place_test_image(storage, digest, len(archive))
        receipt_file = storage.root / f"{digest}.receipt.json"
        receipt_file.write_text(json.dumps(receipt.model_dump(mode="json")))
        digests.append(digest)
        receipts.append(receipt_file)
    return sorted(digests), receipts


def _removal_owner(
    sessions: sessionmaker[Session], request_id: str
) -> RecipeCacheRemovalOwner:
    with sessions() as session:
        row = session.scalar(select(Job).where(Job.request_id == request_id))
        assert row is not None
        return RecipeCacheRemovalOwner.model_validate_json(json.dumps(row.payload))


def _renamed(recipe: RecipeDefinition, slug: str) -> RecipeDefinition:
    document = _published(recipe)
    document["identity"]["slug"] = slug
    return RecipeDefinition.model_validate(document)


def _archive_removal_service(
    postgres_engine, tmp_path: Path, recipes: list[RecipeDefinition], clock
) -> tuple[
    RecipeImageAvailabilityService,
    sessionmaker[Session],
    FilesystemRuntimeImageStorage,
]:
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    storage = FilesystemRuntimeImageStorage(tmp_path / "controller-artifacts")
    by_slug = {recipe.identity.slug: recipe for recipe in recipes}
    service = _service(
        sessions,
        storage=storage,
        authority=lambda selector, *_args, **_kwargs: (
            by_slug.get(selector, recipes[0]),
            _runtime(),
        ),
        clock=clock,
    )
    return service, sessions, storage


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
    assert set(seen) <= {0, 3}
    assert 3 in seen
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


def test_postgres_recipe_removal_contended_archive_ends_the_batch_and_resumes(
    tmp_path: Path, postgres_engine
) -> None:
    """A held publication lock stops the batch after the archives done so far."""

    current = [datetime.now(UTC)]
    recipe = _recipe("recipe-source-build.json")
    service, sessions, storage = _archive_removal_service(
        postgres_engine, tmp_path, [recipe], lambda: current[0]
    )
    digests, receipts = _seed_archives(
        sessions, storage, "revision-contended", recipe, 3
    )
    request_id = str(uuid.uuid4())
    remove_after_review(
        service,
        recipe.identity.slug,
        actor="operator",
        request_id=request_id,
        with_model=False,
    )
    with sessions() as session:
        operation_id = session.scalar(
            select(Job.id).where(Job.request_id == request_id)
        )
    assert operation_id is not None
    # The scope and its fences are committed before any archive is touched.
    assert service._observe_recipe_removal(operation_id)
    with storage.publication_lock(digests[2]):
        service.advance_removals(limit=1)
        owner = _removal_owner(sessions, request_id)
        # The first two archives are checkpointed together; the third waits.
        assert owner.checkpoint.image_index == 2
        assert owner.checkpoint.failure is not None
        assert not (storage.root / f"{digests[1]}.receipt.json").exists()
        assert (storage.root / f"{digests[2]}.receipt.json").exists()
    current[0] += timedelta(minutes=1)
    for _ in range(5):
        service.advance_removals(limit=1)
    assert _removal_owner(sessions, request_id).checkpoint.image_index == 3
    assert not any(path.exists() for path in receipts)
    ended = service.get_operator_request(request_id, actor="operator")
    assert isinstance(ended, RecipeCacheRemovalStatus)
    assert ended.state == LifecycleState.SUCCEEDED
    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        cast(RecipeCacheRemovalStatus | RecipeImageAvailabilityView, ended),
        end=lambda receipt: receipt,
        fresh=lambda _: service.start(
            "revision-contended",
            actor="operator",
            request_id=str(uuid.uuid4()),
        ),
    )


def test_postgres_recipe_removal_recovers_after_process_death_between_unlink_and_checkpoint(
    tmp_path: Path, postgres_engine
) -> None:
    """An unlink lost to process death before its checkpoint is redone idempotently."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    recipe = _recipe("recipe-source-build.json")
    receipt = _reference_receipt(_build_id("rev-removal-death"))
    now = datetime.now(UTC)
    with sessions.begin() as session:
        revision = _add_revision(session, "rev-removal-death", recipe)
        _add_head(session, revision)

    storage = FilesystemRuntimeImageStorage(tmp_path / "controller-artifacts")
    receipt_file = storage.root / f"{ARCHIVE_SHA}.receipt.json"
    place_test_image(storage, ARCHIVE_SHA, len(ARCHIVE))
    receipt_file.write_text(
        json.dumps(receipt.model_dump(mode="json")), encoding="utf-8"
    )
    request_id = str(uuid.uuid4())
    service = _service(
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
        process.join(timeout=30)
        pytest.fail("recipe removal process did not reach its injected crash point")
    assert process.exitcode == 73
    # Only the receipt is retired; the blobs wait for garbage collection.
    assert storage.existing_archive(ARCHIVE_SHA, len(ARCHIVE)).is_file()
    assert not receipt_file.exists()

    with sessions() as observer:
        owner_row = observer.scalar(select(Job).where(Job.request_id == request_id))
        assert owner_row is not None
        owner = RecipeCacheRemovalOwner.model_validate_json(
            json.dumps(owner_row.payload)
        )
        assert owner_row.state == "queued"
        assert owner_row.result is None
        assert owner.checkpoint.image_index == 0
        assert owner.checkpoint.image_reclaimed_bytes == 0

    restarted_storage = FilesystemRuntimeImageStorage(storage.root.parent)
    restarted = _service(
        sessions,
        storage=restarted_storage,
        authority=lambda *_args, **_kwargs: (recipe, _runtime()),
        clock=lambda: now,
    )
    assert restarted.advance_removals(limit=1) == 1
    assert restarted.advance_removals(limit=1) == 1
    recovered = restarted.get_operator_request(request_id, actor="operator")
    assert isinstance(recovered, RecipeCacheRemovalStatus)
    recovered = recovered.model_dump(mode="json", exclude_none=True)
    assert recovered["state"] == "succeeded"
    # The receipt was already gone when the restart measured, so the reclaimed
    # bytes are the manifest's, not the image's: an accepted display undercount.
    manifest = restarted_storage.layout.blob_path(f"sha256:{ARCHIVE_SHA}")
    assert recovered["reclaimed_bytes"] <= manifest.stat().st_size


def test_postgres_recipe_removal_retries_finalization_after_gate_contention(
    tmp_path: Path, postgres_engine
) -> None:
    """A due finalization retry clears prior contention and settles the owner."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    recipe = _recipe("recipe-source-build.json")
    receipt = _reference_receipt(_build_id("rev-removal-finalize"))
    now = [datetime.now(UTC)]
    with sessions.begin() as session:
        revision = _add_revision(session, "rev-removal-finalize", recipe)
        _add_head(session, revision)
    storage = FilesystemRuntimeImageStorage(tmp_path / "controller-artifacts")
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
    # Only the receipt is retired; the blobs wait for garbage collection.
    assert storage.existing_archive(ARCHIVE_SHA, len(ARCHIVE)).is_file()

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
    assert isinstance(waiting, RecipeCacheRemovalStatus)
    waiting = waiting.model_dump(mode="json", exclude_none=True)
    assert waiting["state"] == LifecycleState.BACKOFF
    failure = require_mapping(waiting["failure"], "removal failure")
    assert failure["retryable"] is True
    retry_time = failure["retry_time"]
    assert isinstance(retry_time, str)
    now[0] = datetime.fromisoformat(retry_time) + timedelta(seconds=1)

    assert service.advance_removals(limit=1) == 1
    recovered = service.get_operator_request(request_key, actor="operator")
    assert isinstance(recovered, RecipeCacheRemovalStatus)
    recovered = recovered.model_dump(mode="json", exclude_none=True)
    assert recovered["state"] == "succeeded"
