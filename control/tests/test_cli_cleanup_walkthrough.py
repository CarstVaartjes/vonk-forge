"""Disposable installed-CLI U8 cleanup setup using real cache owners."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.cache_removal_review import CacheRemovalReview
from vonk_control.jobs import JobService
from vonk_control.library_projection import LibraryProjection
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import (
    Base,
    CatalogDocumentHead,
    Job,
    ModelCacheOperation,
    ModelCacheSet,
    ModelCacheSetArtifact,
    RuntimeImageAuthorization,
)
from vonk_control.recipe_image_availability import RecipeImageAvailabilityService
from vonk_control.recipe_image_removal_contract import (
    RECIPE_CACHE_REMOVE_KIND,
    RecipeCacheRemovalOwner,
)
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage
from vonk_forge_contracts import RecipeDefinition, content_sha256

from .test_cli_operator_walkthrough import (
    _AuthorizationHeaders,
    _session_environment,
)
from .test_model_removal_reference_lifecycle import (
    _one_model,
    _register_model,
    _seed_model,
)
from .test_profile_load_installed_cli import (
    _https_api_peer,
)
from .test_recipe_image_availability import (
    ARCHIVE,
    ARCHIVE_SHA,
    _add_head,
    _add_revision,
    _reference_receipt,
    _runtime,
)
from .test_recipe_model_removal_postgres import _recipe_using_model

pytest_plugins = ("tests.test_profile_load_installed_cli",)
pytestmark = [
    pytest.mark.lane,
    pytest.mark.skipif(
        "VONK_U8_CLEANUP_MODE" not in os.environ,
        reason="set VONK_U8_CLEANUP_MODE=smoke or interactive to opt in",
    ),
]

_MODEL_BYTES = b"abc"
_TOKEN_KEY = b"u8-cleanup-walkthrough-controller-key"


@dataclass(slots=True)
class _CleanupScenario:
    clock: list[datetime]
    sessions: sessionmaker[Session]
    model_cache: ModelCacheService
    recipe_images: RecipeImageAvailabilityService
    api: TestClient
    headers: _AuthorizationHeaders
    recipe: RecipeDefinition
    recipe_selector: str
    recipe_revision_id: str
    recipe_digest: str
    selected_model_digest: str
    selected_model_set: str
    sibling_model_set: str
    shared_object_digest: str
    shared_object: Path
    shared_receipt: Path
    image_storage: FilesystemRuntimeImageStorage
    image_archive: Path
    image_receipt: Path


def _cleanup_scenario(
    engine: Engine,
    workspace: Path,
) -> _CleanupScenario:
    """Seed real verified image/model bytes and registered removal owners."""

    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = [datetime.now(UTC)]
    model_cache = ModelCacheService(
        sessions,
        workspace / "managed-model-cache",
        reserve_bytes=0,
        fixture_sources=True,
        clock=lambda: now[0],
    )

    selected_model = _one_model(workspace, "u8-selected-model", _MODEL_BYTES)
    selected_selector = _register_model(sessions, selected_model)
    selected_model_digest, _selected_artifact, selected_model_set = _seed_model(
        model_cache,
        workspace,
        selected_model,
        str(uuid4()),
    )
    sibling_model = _one_model(workspace, "u8-sibling-model", _MODEL_BYTES)
    _register_model(sessions, sibling_model)
    sibling_model_digest, _sibling_artifact, sibling_model_set = _seed_model(
        model_cache,
        workspace,
        sibling_model,
        str(uuid4()),
    )
    assert selected_selector == "vonk-forge/u8-selected-model"
    assert selected_model_digest == content_sha256(selected_model)
    assert sibling_model_digest == content_sha256(sibling_model)
    assert selected_model_set != sibling_model_set

    shared_object_digest = hashlib.sha256(_MODEL_BYTES).hexdigest()
    shared_object = model_cache._object_path(shared_object_digest)
    shared_receipt = model_cache._receipt_path(shared_object_digest)
    assert shared_object.read_bytes() == _MODEL_BYTES
    assert shared_receipt.is_file()
    with sessions() as session:
        memberships = tuple(
            session.scalars(
                select(ModelCacheSetArtifact.artifact_set_sha256).where(
                    ModelCacheSetArtifact.artifact_sha256 == shared_object_digest
                )
            )
        )
    assert set(memberships) == {selected_model_set, sibling_model_set}

    recipe = _recipe_using_model(
        selected_model_digest,
        selected_model.identity.publisher,
        selected_model.identity.slug,
    )
    recipe_selector = f"{recipe.identity.publisher}/{recipe.identity.slug}"
    recipe_revision_id = "u8-" + uuid4().hex[:24]
    recipe_digest = content_sha256(recipe)
    image_receipt = _reference_receipt().model_copy(
        update={
            "distribution_publisher": recipe.identity.publisher,
            "distribution_slug": recipe.identity.slug,
            "distribution_content_sha256": recipe_digest,
        }
    )
    with sessions.begin() as session:
        revision = _add_revision(session, recipe_revision_id, recipe)
        _add_head(session, revision)
        session.add(
            RuntimeImageAuthorization(
                recipe_revision_id=revision.id,
                source="published",
                original_content_digest=recipe_digest,
                effective_execution_key=revision.execution_key,
                registry_manifest_digest=image_receipt.registry_manifest_digest,
                platform_manifest_digest=image_receipt.platform_manifest_digest,
                local_image_config_id=image_receipt.local_image_config_id,
                oci_archive_sha256=image_receipt.oci_archive_sha256,
                image_bytes=image_receipt.image_bytes,
                build_id=None,
                authorized_at=now[0],
                state="authorized",
            )
        )

    image_storage = FilesystemRuntimeImageStorage(workspace / "managed-images")
    staged = image_storage.prepare_path()
    staged.write_bytes(ARCHIVE)
    assert hashlib.sha256(staged.read_bytes()).hexdigest() == ARCHIVE_SHA
    published = image_storage.commit(staged, receipt=image_receipt)
    assert published.oci_archive_sha256 == ARCHIVE_SHA
    image_archive = image_storage.root / ARCHIVE_SHA
    image_receipt_path = image_storage.root / f"{ARCHIVE_SHA}.receipt.json"
    assert image_archive.read_bytes() == ARCHIVE
    assert image_receipt_path.is_file()

    recipe_images = RecipeImageAvailabilityService(
        sessions,
        storage=image_storage,
        authority=lambda *_args, **_kwargs: (recipe, _runtime()),
        model_cache=model_cache,
        clock=lambda: now[0],
    )
    actor = Actor("u8-cleanup-facilitator", "operator")
    codec = TokenCodec(_TOKEN_KEY)
    token = codec.issue(actor, ttl_seconds=1_000, now=0)
    raw_headers = {"Authorization": f"Bearer {token}"}
    api = create_app(
        jobs=JobService(sessions, clock=lambda: now[0], cursors=codec.cursor_codec()),
        tokens=codec,
        audits=MemoryAuditStore(),
        library_projection=LibraryProjection(
            sessions,
            cursors=codec.cursor_codec(),
            clock=lambda: now[0],
        ),
        model_cache=model_cache,
        recipe_image_availability=recipe_images,
        now=lambda: 100,
    )
    return _CleanupScenario(
        clock=now,
        sessions=sessions,
        model_cache=model_cache,
        recipe_images=recipe_images,
        api=TestClient(api),
        headers=_AuthorizationHeaders(raw_headers),
        recipe=recipe,
        recipe_selector=recipe_selector,
        recipe_revision_id=recipe_revision_id,
        recipe_digest=recipe_digest,
        selected_model_digest=selected_model_digest,
        selected_model_set=selected_model_set,
        sibling_model_set=sibling_model_set,
        shared_object_digest=shared_object_digest,
        shared_object=shared_object,
        shared_receipt=shared_receipt,
        image_storage=image_storage,
        image_archive=image_archive,
        image_receipt=image_receipt_path,
    )


def _cli(
    executable: Path,
    environment: dict[str, str],
    cwd: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [str(executable), *arguments],
        stdin=subprocess.DEVNULL,
        env=environment,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    token = Path(environment["VONK_CONTROL_TOKEN_FILE"]).read_text(encoding="utf-8")
    if token in result.stdout or token in result.stderr:
        pytest.fail("installed CLI exposed the private U8 credential", pytrace=False)
    return result


def _json_line(output: str) -> dict[str, object]:
    assert output.endswith("\n") and output.count("\n") == 1
    value = json.loads(output)
    assert isinstance(value, dict)
    return value


def _assert_shared_model_reference(
    scenario: _CleanupScenario, *, selected_present: bool = False
) -> None:
    """The sibling cache set keeps the one shared verified object alive."""

    assert scenario.shared_object.read_bytes() == _MODEL_BYTES
    assert scenario.shared_receipt.is_file()
    with scenario.sessions() as session:
        assert (
            session.get(ModelCacheSet, scenario.selected_model_set) is not None
        ) is selected_present
        assert session.get(ModelCacheSet, scenario.sibling_model_set) is not None
        memberships = tuple(
            session.scalars(
                select(ModelCacheSetArtifact.artifact_set_sha256).where(
                    ModelCacheSetArtifact.artifact_sha256
                    == scenario.shared_object_digest
                )
            )
        )
    expected = {scenario.sibling_model_set}
    if selected_present:
        expected.add(scenario.selected_model_set)
    assert set(memberships) == expected


def _check_review_scope(scenario: _CleanupScenario, document: dict[str, object]) -> str:
    review = CacheRemovalReview.model_validate_json(json.dumps(document))
    assert review.resource_kind == "recipe"
    assert review.selector == scenario.recipe_selector
    assert review.target_identity == scenario.recipe_revision_id
    assert review.with_model is True
    assert review.blockers == []
    assert review.active_work == []

    assets = {(asset.kind, asset.sha256): asset for asset in review.assets}
    assert set(assets) == {
        ("runtime-image", ARCHIVE_SHA),
        ("model-set", scenario.selected_model_set),
        ("model-object", scenario.shared_object_digest),
    }
    image = assets[("runtime-image", ARCHIVE_SHA)]
    assert image.disposition == "remove"
    assert image.availability == "verified"
    assert image.expected_bytes == len(ARCHIVE)
    assert image.available_bytes == len(ARCHIVE)
    selected = assets[("model-set", scenario.selected_model_set)]
    assert selected.disposition == "remove"
    assert selected.availability == "verified"
    assert selected.expected_bytes == len(_MODEL_BYTES)
    assert selected.available_bytes == len(_MODEL_BYTES)
    shared = assets[("model-object", scenario.shared_object_digest)]
    assert shared.disposition == "retain-shared"
    assert shared.availability == "verified"
    assert shared.expected_bytes == len(_MODEL_BYTES)
    assert shared.available_bytes == len(_MODEL_BYTES)

    assert any(
        item.classification == "saved-reference"
        and item.asset_kind == "model-object"
        and item.asset_sha256 == scenario.shared_object_digest
        and item.owner_id == scenario.sibling_model_set
        for item in review.references
    )
    return review.review_digest


def _assert_no_removal_job(scenario: _CleanupScenario) -> None:
    with scenario.sessions() as session:
        assert not tuple(
            session.scalars(select(Job).where(Job.kind == RECIPE_CACHE_REMOVE_KIND))
        )


def _smoke(
    *,
    scenario: _CleanupScenario,
    executable: Path,
    environment: dict[str, str],
    cwd: Path,
    peer_calls: list[tuple[str, str, object]],
) -> None:
    selector = scenario.recipe_selector
    review_result = _cli(
        executable,
        environment,
        cwd,
        "--json",
        "recipe",
        "remove",
        selector,
        "--with-model",
        "--review",
    )
    review = _json_line(review_result.stdout)
    assert review_result.returncode == 0, review_result.stdout + review_result.stderr
    digest = _check_review_scope(scenario, review)
    assert review_result.stderr == ""
    assert peer_calls and all(method == "GET" for method, _, _ in peer_calls)
    assert any(
        "/remove-review?with_model=true" in path.casefold() for _, path, _ in peer_calls
    )
    _assert_no_removal_job(scenario)
    assert scenario.image_archive.read_bytes() == ARCHIVE
    assert scenario.image_receipt.is_file()
    assert scenario.shared_object.read_bytes() == _MODEL_BYTES

    # A redirected operator cannot turn the reviewed digest into consent by
    # itself. No removal request or owner row is created without --yes.
    request_key = str(uuid4())
    calls_before_refusal = len(peer_calls)
    refusal = _cli(
        executable,
        environment,
        cwd,
        "--no-input",
        "--json",
        "recipe",
        "remove",
        selector,
        "--with-model",
        "--review-digest",
        digest,
        "--request-key",
        request_key,
        "--detach",
    )
    error = _json_line(refusal.stdout).get("error")
    assert refusal.returncode == 2
    assert isinstance(error, str) and "--yes" in error
    assert refusal.stderr == ""
    assert not any(
        method == "POST" and path.endswith("/remove")
        for method, path, _ in peer_calls[calls_before_refusal:]
    )
    _assert_no_removal_job(scenario)

    accepted = _cli(
        executable,
        environment,
        cwd,
        "--no-input",
        "--json",
        "recipe",
        "remove",
        selector,
        "--with-model",
        "--review-digest",
        digest,
        "--yes",
        "--request-key",
        request_key,
        "--detach",
    )
    receipt = _json_line(accepted.stdout)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert accepted.stderr == ""
    operation_id = receipt.get("operation_id")
    assert isinstance(operation_id, str)
    assert receipt.get("action") == "remove"
    assert receipt.get("request_key") == request_key
    assert receipt.get("selector") == selector
    assert receipt.get("with_model") is True
    assert receipt.get("review_digest") == digest
    assert receipt.get("state") == "queued"
    assert receipt.get("reclaimed_bytes") == 0
    posts = [
        document
        for method, path, document in peer_calls
        if method == "POST" and path.endswith("/remove")
    ]
    assert len(posts) == 1 and isinstance(posts[0], dict)
    assert posts[0].get("request_key") == request_key
    assert posts[0].get("with_model") is True
    assert posts[0].get("review_digest") == digest

    progress_result = _cli(
        executable,
        environment,
        cwd,
        "--json",
        "recipe",
        "progress",
        "--request-key",
        request_key,
    )
    pending = _json_line(progress_result.stdout)
    assert progress_result.returncode == 0, (
        progress_result.stdout + progress_result.stderr
    )
    assert pending.get("operation_id") == operation_id
    assert pending.get("request_key") == request_key
    assert pending.get("state") == "queued"
    assert pending.get("reclaimed_bytes") == 0
    progress = pending.get("progress")
    assert isinstance(progress, dict)
    assert progress.get("completed_bytes") == 0
    assert progress.get("completed_items") == 0
    assert scenario.image_archive.read_bytes() == ARCHIVE
    assert scenario.image_receipt.is_file()
    assert scenario.shared_object.read_bytes() == _MODEL_BYTES

    with scenario.sessions() as session:
        parent = session.get(Job, operation_id)
        assert parent is not None and parent.state == "queued"
        owner = RecipeCacheRemovalOwner.model_validate_json(json.dumps(parent.payload))
        assert owner.plan.intent.request_key == request_key
        assert owner.plan.intent.actor == "u8-cleanup-facilitator"
        assert owner.plan.intent.recipe_revision_id == scenario.recipe_revision_id
        assert owner.plan.intent.with_model is True
        assert owner.plan.intent.review_digest == digest
        assert owner.plan.image_archives == [ARCHIVE_SHA]
        assert len(owner.plan.model_children) == 1
        model_child = owner.plan.model_children[0]
        assert model_child.selected_sets == [scenario.selected_model_set]
        child_id = model_child.operation_id
        child_key = model_child.request_key
        child_digest = model_child.plan_digest

    # The real parent owner first reclaims the image and then waits for its
    # already accepted model-cache child. It does not report completion while
    # the selected set is still present.
    partial: dict[str, object] | None = None
    for _ in range(8):
        # Move the fixture clock through the owner's bounded retry interval.
        scenario.clock[0] += timedelta(seconds=10)
        scenario.recipe_images.advance_removals(limit=1)
        current = scenario.recipe_images.get_operator_request(
            request_key, actor="u8-cleanup-facilitator"
        )
        assert isinstance(current, dict)
        partial = current
        if current.get("state") == "partial":
            break
    assert partial is not None and partial.get("state") == "partial"
    assert partial.get("operation_id") == operation_id
    assert not scenario.image_archive.exists()
    assert not scenario.image_receipt.exists()
    with scenario.sessions() as session:
        assert session.get(ModelCacheSet, scenario.selected_model_set) is not None
        child_row = session.get(ModelCacheOperation, child_id)
        assert child_row is not None and child_row.state == "queued"
        assert child_row.request_key == child_key
        assert child_row.plan_digest == child_digest
    _assert_shared_model_reference(scenario, selected_present=True)

    observed_partial = _cli(
        executable,
        environment,
        cwd,
        "--json",
        "recipe",
        "progress",
        "--request-key",
        request_key,
    )
    partial_receipt = _json_line(observed_partial.stdout)
    assert partial_receipt.get("operation_id") == operation_id
    assert partial_receipt.get("state") == "partial"
    assert partial_receipt.get("reclaimed_bytes") == len(ARCHIVE)

    # Advance the exact accepted child with the real model-cache owner, then
    # let the parent reconcile it and publish its durable terminal result.
    for _ in range(16):
        child = scenario.model_cache.get_operation(child_id)
        if child.state == "succeeded":
            break
        scenario.model_cache.advance_removals(limit=1)
    child = scenario.model_cache.get_operation(child_id)
    assert child.state == "succeeded"
    assert child.request_key == child_key
    for _ in range(8):
        # Move the fixture clock through the owner's bounded retry interval.
        scenario.clock[0] += timedelta(seconds=10)
        scenario.recipe_images.advance_removals(limit=1)
        final = scenario.recipe_images.get_operator_request(
            request_key, actor="u8-cleanup-facilitator"
        )
        assert isinstance(final, dict)
        if final.get("state") == "succeeded":
            break
    assert final.get("state") == "succeeded"
    assert final.get("operation_id") == operation_id
    assert final.get("review_digest") == digest
    assert final.get("reclaimed_bytes") == len(ARCHIVE)
    assert not scenario.image_archive.exists()
    assert not scenario.image_receipt.exists()
    _assert_shared_model_reference(scenario)

    completed = _cli(
        executable,
        environment,
        cwd,
        "--json",
        "recipe",
        "progress",
        "--request-key",
        request_key,
    )
    completed_view = _json_line(completed.stdout)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed_view.get("operation_id") == operation_id
    assert completed_view.get("state") == "succeeded"
    assert completed_view.get("review_digest") == digest
    assert completed_view.get("reclaimed_bytes") == len(ARCHIVE)
    removal_posts = [
        (method, path)
        for method, path, _ in peer_calls
        if method == "POST" and path.endswith("/remove")
    ]
    assert len(removal_posts) == 1

    with scenario.sessions() as session:
        parent = session.get(Job, operation_id)
        head = session.scalar(
            select(CatalogDocumentHead).where(
                CatalogDocumentHead.kind == "recipe",
                CatalogDocumentHead.publisher == scenario.recipe.identity.publisher,
                CatalogDocumentHead.slug == scenario.recipe.identity.slug,
            )
        )
        child_row = session.get(ModelCacheOperation, child_id)
        assert parent is not None and parent.state == "succeeded"
        assert parent.actor == "u8-cleanup-facilitator"
        assert parent.authority_revision == scenario.recipe_revision_id
        assert (
            head is not None and head.active_revision_id == scenario.recipe_revision_id
        )
        assert child_row is not None and child_row.state == "succeeded"
        assert (
            session.scalar(select(Job).where(Job.request_id == request_key)) is parent
        )

    print(
        "U8 cleanup smoke passed: the installed CLI displayed the exact reviewed "
        "scope before explicit consent; queued and partial progress stayed "
        "distinct from completion; the selected image and model set were removed "
        "while the sibling model set kept their shared verified object. No live "
        "Controller, external artifact source, or Spark was used.",
        flush=True,
    )


def _interactive(
    *,
    scenario: _CleanupScenario,
    executable: Path,
    environment: dict[str, str],
    cwd: Path,
    peer_calls: list[tuple[str, str, object]],
) -> None:
    token_file = Path(environment["VONK_CONTROL_TOKEN_FILE"])
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    print(f"Disposable Controller: {environment['VONK_CONTROL_URL']}", flush=True)
    print(f"Installed CLI: {executable}", flush=True)
    print(
        "This loopback Controller owns only temporary test catalog/cache data. "
        "Use the original U8 outcome card and shipped runbook; do not replace "
        "the private URL or credential. The review and consent path is active. "
        "Type `exit` or press Ctrl-D to close this shell.",
        flush=True,
    )
    shell = shutil.which("bash") or "/bin/bash"
    completed = subprocess.run(
        [shell, "--noprofile", "--norc", "-i"],
        env=environment,
        cwd=cwd,
        check=False,
    )
    print(f"Operator shell exited with status {completed.returncode}.", flush=True)

    with scenario.sessions() as session:
        accepted = tuple(
            session.scalars(select(Job).where(Job.kind == RECIPE_CACHE_REMOVE_KIND))
        )
    if not accepted:
        _assert_no_removal_job(scenario)
        assert scenario.image_archive.read_bytes() == ARCHIVE
        assert scenario.image_receipt.is_file()
        assert scenario.shared_object.read_bytes() == _MODEL_BYTES
        print("No cleanup request was accepted; no owner was advanced.", flush=True)
        return

    assert len(accepted) == 1
    parent = accepted[0]
    owner = RecipeCacheRemovalOwner.model_validate_json(json.dumps(parent.payload))
    assert owner.plan.intent.recipe_revision_id == scenario.recipe_revision_id
    assert owner.plan.intent.selector == scenario.recipe_selector
    assert owner.plan.intent.review_digest
    assert any(
        method == "GET" and "/remove-review?with_model=" in path
        for method, path, _ in peer_calls
    )
    submissions = [
        document
        for method, path, document in peer_calls
        if method == "POST" and path.endswith("/remove")
    ]
    assert len(submissions) == 1 and isinstance(submissions[0], dict)
    assert submissions[0].get("review_digest") == owner.plan.intent.review_digest
    assert submissions[0].get("request_key") == parent.request_id
    assert submissions[0].get("with_model") is owner.plan.intent.with_model
    assert parent.state == "queued"
    print(
        "The accepted request is still queued; the facilitator will now reconcile "
        "only this temporary owner and verify the retained references.",
        flush=True,
    )
    for _ in range(16):
        # Move the fixture clock through the owner's bounded retry interval.
        scenario.clock[0] += timedelta(seconds=10)
        scenario.recipe_images.advance_removals(limit=1)
        current = scenario.recipe_images.get_operator_request(
            parent.request_id, actor="u8-cleanup-facilitator"
        )
        assert isinstance(current, dict)
        if current.get("state") == "succeeded":
            break
        for child in owner.plan.model_children:
            if (
                scenario.model_cache.get_operation(child.operation_id).state
                != "succeeded"
            ):
                scenario.model_cache.advance_removals(limit=1)
    settled = scenario.recipe_images.get_operator_request(
        parent.request_id, actor="u8-cleanup-facilitator"
    )
    assert isinstance(settled, dict)
    assert settled.get("state") == "succeeded"
    assert settled.get("review_digest") == owner.plan.intent.review_digest
    if owner.plan.intent.with_model:
        _assert_shared_model_reference(scenario)
    else:
        assert scenario.shared_object.read_bytes() == _MODEL_BYTES
        with scenario.sessions() as session:
            assert session.get(ModelCacheSet, scenario.selected_model_set) is not None
            assert session.get(ModelCacheSet, scenario.sibling_model_set) is not None
    assert not scenario.image_archive.exists()
    assert not scenario.image_receipt.exists()
    print(
        "The fixture owner reached its terminal result after reconciliation. This "
        "is disposable facilitator evidence only; the independent human scorecard "
        "must record what the participant discovered and explained.",
        flush=True,
    )


def _walkthrough(
    engine: Engine,
    mode: str,
    installed_vonkctl: Path,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / f"u8-cleanup-{mode}"
    workspace.mkdir(mode=0o700)
    cwd = workspace / "operator-cwd"
    cwd.mkdir(mode=0o700)
    scenario = _cleanup_scenario(engine, workspace)
    try:
        with (
            scenario.api,
            _https_api_peer(workspace, scenario.api, scenario.headers) as (
                url,
                certificate,
                peer,
            ),
        ):
            environment = _session_environment(
                installed_vonkctl=installed_vonkctl,
                workspace=workspace,
                url=url,
                certificate=certificate,
                headers=scenario.headers,
            )
            token_file = Path(environment["VONK_CONTROL_TOKEN_FILE"])
            token = scenario.headers["Authorization"].removeprefix("Bearer ")
            assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
            assert token_file.read_text(encoding="utf-8") == token
            if mode == "smoke":
                _smoke(
                    scenario=scenario,
                    executable=installed_vonkctl,
                    environment=environment,
                    cwd=cwd,
                    peer_calls=peer.calls,
                )
            else:
                _interactive(
                    scenario=scenario,
                    executable=installed_vonkctl,
                    environment=environment,
                    cwd=cwd,
                    peer_calls=peer.calls,
                )
    finally:
        scenario.model_cache.close()
        shutil.rmtree(workspace)
    assert not workspace.exists()
    print(
        "U8 private wheel, HOME, token, TLS files, managed storage, and operator "
        "directory were removed; pytest disposes of this test's PostgreSQL database "
        "and container.",
        flush=True,
    )


@pytest.mark.skipif(
    os.environ.get("VONK_U8_CLEANUP_MODE") != "smoke",
    reason="set VONK_U8_CLEANUP_MODE=smoke to run U8 cleanup smoke",
)
def test_disposable_u8_cleanup_walkthrough_smoke(
    installed_vonkctl: Path,
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    _walkthrough(postgres_engine, "smoke", installed_vonkctl, tmp_path)


@pytest.mark.skipif(
    os.environ.get("VONK_U8_CLEANUP_MODE") != "interactive",
    reason="set VONK_U8_CLEANUP_MODE=interactive to start U8 cleanup shell",
)
def test_disposable_u8_cleanup_walkthrough_interactive(
    installed_vonkctl: Path,
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    _walkthrough(postgres_engine, "interactive", installed_vonkctl, tmp_path)
