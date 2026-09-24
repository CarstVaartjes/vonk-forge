"""Durable recipe-update intent at the parent/child acceptance boundary."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import (
    Base,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    Job,
    User,
)
from vonk_control.operation_api import OperationQuery, operation_detail_response
from vonk_control.recipe_image_availability import (
    RecipeImageAvailabilityError,
    RecipeImageAvailabilityService,
    RecipeImageAvailabilityView,
)
from vonk_control.recipe_update_contract import RecipeUpdateResponse
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    persist_runtime_image_receipt,
    prepare_runtime_image,
)
from vonk_forge_contracts import RecipeDefinition, content_sha256

from .test_recipe_image_availability import (
    Transport,
    _add_head,
    _add_revision,
    _recipe,
    _runtime,
)


def test_update_commits_parent_and_exact_scope_before_any_child_admission(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'controller.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    recipe = _recipe("recipe-image.json")
    revision_id = str(uuid.uuid4())
    with sessions.begin() as session:
        _add_head(session, _add_revision(session, revision_id, recipe))
        session.add(User(subject="operator", role="operator"))
    authority_calls = []

    def authority(recipe_revision_id, *, force=False):
        authority_calls.append(recipe_revision_id)
        return recipe, _runtime()

    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path / "images"),
        authority=authority,
        transport=Transport(),
        clock=lambda: datetime.now(UTC),
    )
    key = str(uuid.uuid4())
    batch = service.update(
        actor="operator",
        request_id=key,
        selectors=[recipe.identity.slug],
        all=False,
    )
    assert not authority_calls, (
        "acceptance issued child work before persisting its parent"
    )
    with sessions() as session:
        jobs = list(session.scalars(select(Job)))
        assert len(jobs) == 1 and jobs[0].kind == "recipe.cache.update.v2"
        assert jobs[0].request_id == key
    assert batch.request_id == key
    assert batch.children[0].recipe_revision_id == revision_id
    assert batch.children[0].operation_id is None


@pytest.fixture
def update_env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'batch.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    now = [datetime.now(UTC)]
    recipes = {}
    for index in range(2):
        base = _recipe("recipe-image.json")
        recipe = base.model_copy(
            update={
                "identity": base.identity.model_copy(update={"slug": f"batch-{index}"})
            }
        )
        revision_id = str(uuid.uuid4())
        recipes[revision_id] = recipe
        with sessions.begin() as session:
            _add_head(session, _add_revision(session, revision_id, recipe))
    with sessions.begin() as session:
        session.add(User(subject="operator", role="operator"))
    storage = FilesystemRuntimeImageStorage(tmp_path / "images")

    def fresh():
        return RecipeImageAvailabilityService(
            sessions,
            storage=storage,
            authority=lambda recipe_revision_id, **_: (
                recipes[recipe_revision_id],
                _runtime(),
            ),
            transport=Transport(),
            clock=lambda: now[0],
            claim_lease_seconds=10,
        )

    yield sessions, recipes, now, fresh
    engine.dispose()


def _start(service, selectors):
    return service.update(
        actor="operator", request_id=str(uuid.uuid4()), selectors=selectors, all=False
    )


def test_changed_frozen_revision_is_refused_before_child_commit(update_env):
    sessions, recipes, _, fresh = update_env
    service = fresh()
    revision_id = next(iter(recipes))
    parent = _start(service, [revision_id])
    recipe = recipes[revision_id]
    changed = recipe.model_copy(
        update={
            "metadata": recipe.metadata.model_copy(
                update={"description": "changed after acceptance"}
            )
        }
    )
    recipes[revision_id] = changed
    with sessions.begin() as session:
        # Bypass the ORM immutability guard to model corrupt persisted identity.
        session.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == revision_id)
            .values(
                content_digest=content_sha256(changed),
                document=changed.model_dump(mode="json"),
            )
        )
    claim = service.claim_update(owner="worker")
    service.run_update_claim(claim)
    with sessions() as session:
        assert len(list(session.scalars(select(Job)))) == 1, (
            "changed identity was admitted as a child"
        )
    observed = service.get_operator_operation(parent.id)
    assert observed.state == "failed"
    assert observed.children[0].failure.code == "recipe_update.operation_invalid"


def test_malformed_parent_does_not_block_other_eligible_updates(update_env):
    sessions, recipes, now, fresh = update_env
    service = fresh()
    broken = _start(service, [next(iter(recipes))])
    now[0] += timedelta(seconds=1)
    healthy = _start(service, [next(iter(recipes))])
    with sessions.begin() as session:
        session.get(Job, broken.id).payload = {"invalid": True}
    claim = service.claim_update(owner="worker")
    assert claim.operation_id == healthy.id
    with sessions() as session:
        assert session.get(Job, broken.id).state == "failed"
    page = service.update_activity_provider().list_operations(
        OperationQuery(after=None, limit=10, state=None, node_id=None)
    )
    assert {item["id"] for item in page.items} == {broken.id, healthy.id}
    for item in page.items:
        operation_detail_response(item)


@pytest.mark.parametrize(
    "crash_point", ["before_child", "after_child_commit", "after_parent_link"]
)
def test_restart_adopts_frozen_children_across_worker_process_death(
    update_env, crash_point
):
    import subprocess
    import sys

    sessions, recipes, now, fresh = update_env
    service = fresh()
    parent = _start(service, list(recipes))
    # A separate worker process exits without cleanup after the real child
    # transaction commits, before the parent can record its returned ID.
    program = """
import json, os, sys
from datetime import datetime
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.models import CatalogDocumentRevision
from vonk_control.recipe_image_availability import RecipeImageAvailabilityService
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage
from vonk_forge_contracts import RecipeDefinition
sessions = sessionmaker(create_engine(sys.argv[1]))
def authority(recipe_revision_id, **kwargs):
    with sessions() as session:
        row = session.get(CatalogDocumentRevision, recipe_revision_id)
        return RecipeDefinition.model_validate_json(json.dumps(row.document)), json.loads(sys.argv[3])
service = RecipeImageAvailabilityService(sessions, storage=FilesystemRuntimeImageStorage(Path(sys.argv[2])), authority=authority, clock=lambda: datetime.fromisoformat(sys.argv[4]), claim_lease_seconds=10)
actual = service._start_request
def die_after_commit(*args, **kwargs):
    result = actual(*args, **kwargs)
    if sys.argv[5] == "after_child_commit":
        os._exit(23)
    return result
service._start_request = die_after_commit
claim = service.claim_update(owner="crashed-process")
if sys.argv[5] == "before_child":
    os._exit(23)
service.run_update_claim(claim)
os._exit(23)
"""
    with sessions() as session:
        database = str(session.get_bind().url)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            database,
            str(service._storage.root),
            json.dumps(_runtime()),
            now[0].isoformat(),
            crash_point,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 23, result.stderr
    with sessions() as session:
        jobs = list(session.scalars(select(Job)))
        assert len(jobs) == (1 if crash_point == "before_child" else 2)
        child = next((job for job in jobs if job.id != parent.id), None)
        child_id = None if child is None else child.id
    saved = fresh().get_operator_operation(parent.id)
    assert saved.children[0].operation_id == (
        child_id if crash_point == "after_parent_link" else None
    )
    now[0] += timedelta(seconds=11)
    restarted = fresh()
    restarted.run_update_claim(restarted.claim_update(owner="new-worker"))
    observed = restarted.get_operator_operation(parent.id)
    assert observed.children[0].operation_id is not None
    if child_id is not None:
        assert observed.children[0].operation_id == child_id
    assert [child.recipe_revision_id for child in observed.children] == list(recipes)
    with sessions() as session:
        assert len(list(session.scalars(select(Job)))) == (
            3 if crash_point == "after_parent_link" else 2
        )


def test_revoked_authority_observes_issued_child_and_refuses_new_children(update_env):
    sessions, recipes, now, fresh = update_env
    service = fresh()
    parent = _start(service, list(recipes))
    service.run_update_claim(service.claim_update(owner="worker"))
    issued = service.get_operator_operation(parent.id).children[0].operation_id
    with sessions.begin() as session:
        user = session.scalar(select(User).where(User.subject == "operator"))
        user.disabled_at = now[0]
    service.run_update_claim(service.claim_update(owner="worker"))
    observed = service.get_operator_operation(parent.id)
    assert observed.children[0].operation_id == issued
    assert observed.children[1].failure.code == "recipe_update.authority_denied"
    assert observed.state in {"queued", "running"}, "issued work is still executing"
    now[0] += timedelta(seconds=3)
    service.run_update_claim(service.claim_update(owner="worker"))
    assert service.get_operator_operation(parent.id).children[0].operation_id == issued
    with sessions() as session:
        assert len(list(session.scalars(select(Job)))) == 2


def test_stale_parent_cannot_admit_or_replace_new_claim(update_env):
    sessions, recipes, now, fresh = update_env
    service = fresh()
    parent = _start(service, [next(iter(recipes))])
    stale = service.claim_update(owner="first")
    now[0] += timedelta(seconds=11)
    current = fresh().claim_update(owner="second")
    with pytest.raises(RecipeImageAvailabilityError, match="no longer owns"):
        service.run_update_claim(stale)
    fresh().run_update_claim(current)
    assert (
        fresh().get_operator_operation(parent.id).children[0].operation_id is not None
    )
    with sessions() as session:
        assert len(list(session.scalars(select(Job)))) == 2


def test_one_admission_failure_does_not_suppress_other_children(update_env):
    _, recipes, now, fresh = update_env
    service = fresh()
    first, second = recipes
    normal_authority = service._authority

    def authority(revision, **kwargs):
        if revision == first:
            raise RecipeImageAvailabilityError(
                "recipe_image.recipe_unavailable", "recipe was withdrawn"
            )
        return normal_authority(revision, **kwargs)

    service._authority = authority
    parent = _start(service, [first, second])
    service.run_update_claim(service.claim_update(owner="worker"))
    service.run_update_claim(service.claim_update(owner="worker"))
    child_id = service.get_operator_operation(parent.id).children[1].operation_id
    assert child_id is not None
    for child_claim in service.claim_pending():
        service.run_claim(child_claim)
    now[0] += timedelta(seconds=3)
    service.run_update_claim(service.claim_update(owner="worker"))
    result = service.get_operator_operation(parent.id)
    assert result.state == "partial"
    assert [child.state for child in result.children] == ["failed", "succeeded"]
    assert result.progress.completed_items == 2


def test_all_uses_complete_verified_cache_not_job_history_and_replay_keeps_scope(
    update_env,
):
    sessions, recipes, now, fresh = update_env
    service = fresh()
    empty_key = str(uuid.uuid4())
    empty = service.update(
        actor="operator", request_id=empty_key, selectors=[], all=True
    )
    assert empty.state == "succeeded" and empty.children == []
    receipt = prepare_runtime_image(
        next(iter(recipes.values())),
        runtime=_runtime(),
        storage=service._storage,
        transport=Transport(),
    )
    base = next(iter(recipes.values()))
    expected = []
    with sessions.begin() as session:
        for index in range(103):
            recipe = base.model_copy(
                update={
                    "identity": base.identity.model_copy(
                        update={"slug": f"cached-{index:03d}"}
                    )
                }
            )
            revision_id = str(uuid.uuid4())
            revision = _add_revision(session, revision_id, recipe)
            _add_head(session, revision)
            assert revision.execution_key is not None
            persist_runtime_image_receipt(
                session,
                recipe_revision_id=revision_id,
                original_content_digest=revision.content_digest,
                effective_execution_key=revision.execution_key,
                receipt=receipt,
                verified_at=now[0],
            )
            expected.append(revision_id)
    # No successful availability Jobs exist: managed files are the evidence.
    parent = service.update(
        actor="operator", request_id=str(uuid.uuid4()), selectors=[], all=True
    )
    assert [child.recipe_revision_id for child in parent.children] == expected
    assert (
        service.update(actor="operator", request_id=empty_key, selectors=[], all=True)
        == empty
    )
    Path(receipt.archive_path).unlink()
    after_loss = service.update(
        actor="operator", request_id=str(uuid.uuid4()), selectors=[], all=True
    )
    assert after_loss.children == []
    assert (
        service.update(
            actor="operator", request_id=parent.request_id, selectors=[], all=True
        ).id
        == parent.id
    )
    assert len(service.get_operator_operation(parent.id).children) == 103


def test_scope_that_cannot_fit_future_failure_details_is_refused_before_acceptance(
    update_env, monkeypatch
):
    sessions, recipes, _, fresh = update_env
    service = fresh()
    parent = _start(service, list(recipes))
    # Use the actual serialized complete receipt as the small test budget.
    # It fits now but cannot fit future per-child failure observations.
    current_size = len(json.dumps(parent.model_dump(mode="json")).encode())
    monkeypatch.setattr(
        "vonk_control.recipe_update_batches.MAX_CONTROL_DOCUMENT_BYTES", current_size
    )
    with pytest.raises(RecipeImageAvailabilityError, match="document limit") as refused:
        _start(service, list(recipes))
    assert refused.value.code == "recipe_update.scope_invalid"
    with sessions() as session:
        assert len(list(session.scalars(select(Job)))) == 1


def test_replay_binds_issuer_scope_and_frozen_current_head(update_env):
    sessions, recipes, _, fresh = update_env
    service = fresh()
    revision_id = next(iter(recipes))
    recipe = recipes[revision_id]
    parent = _start(service, [recipe.identity.slug])
    with sessions.begin() as session:
        head = session.scalar(
            select(CatalogDocumentHead).where(
                CatalogDocumentHead.slug == recipe.identity.slug
            )
        )
        replacement_id = str(uuid.uuid4())
        replacement = recipe.model_copy(
            update={
                "metadata": recipe.metadata.model_copy(
                    update={"description": "new accepted head"}
                )
            }
        )
        _add_revision(session, replacement_id, replacement)
        head.active_revision_id = replacement_id
    replay = service.update(
        actor="operator",
        request_id=parent.request_id,
        selectors=[recipe.identity.slug],
        all=False,
    )
    assert replay.children[0].recipe_revision_id == revision_id
    for actor, selectors in [
        ("another", [recipe.identity.slug]),
        ("operator", [replacement_id]),
    ]:
        with pytest.raises(RecipeImageAvailabilityError) as reused:
            service.update(
                actor=actor,
                request_id=parent.request_id,
                selectors=selectors,
                all=False,
            )
        assert reused.value.code == "recipe_update.request_key_reused"


def test_activity_projects_parent_with_common_ordering_and_state_filter(update_env):
    _, recipes, now, fresh = update_env
    service = fresh()
    older = _start(service, list(recipes))
    now[0] += timedelta(seconds=1)
    newer = _start(service, list(recipes))
    provider = service.update_activity_provider()
    query = OperationQuery(after=None, limit=1, state="queued", node_id=None)
    page = provider.list_operations(query)
    assert page.total == 2 and page.items[0]["id"] == newer.id
    item = operation_detail_response(provider.get_operation(newer.id))
    assert item.progress is not None
    assert item.progress.total_items == 2 and item.node_ids == []
    next_page = provider.list_operations(
        OperationQuery(
            after=(newer.created_at, newer.id), limit=1, state="queued", node_id=None
        )
    )
    assert next_page.items[0]["id"] == older.id
    assert (
        provider.list_operations(
            OperationQuery(after=None, limit=1, state=None, node_id="spk_" + "a" * 32)
        ).total
        == 0
    )


def test_registered_update_route_cli_recovers_lost_receipt_and_follows_same_parent(
    update_env, tmp_path, capsys
):
    from email.message import Message
    from io import BytesIO
    from urllib.error import URLError

    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient
    from vonk_control.auth import Actor
    from vonk_control.recipe_image_availability_api import (
        install_recipe_operator_routes,
    )

    from cluster_profiles.cli import main
    from cluster_profiles.control_client import ControlClient

    _, recipes, now, fresh = update_env
    service = fresh()
    actor = Actor("operator", "operator")
    app = FastAPI()
    install_recipe_operator_routes(
        app, actor_dependency=Depends(lambda: actor), service=service
    )
    token = tmp_path / "token"
    token.write_text("fixture-token")
    token.chmod(0o600)
    calls = []
    receipt = {}
    key = str(uuid.uuid4())

    class Response(BytesIO):
        def __init__(self, response):
            super().__init__(response.content)
            self.status = response.status_code
            self.headers = Message()
            for name, value in response.headers.items():
                self.headers[name] = value

        def __exit__(self, *_args: object) -> None:
            self.close()

    with TestClient(app) as client:

        def opener(request, timeout):
            calls.append((request.get_method(), request.selector))
            if request.selector.startswith("/api/recipe/operations/"):
                service.run_pending()
                now[0] += timedelta(seconds=3)
                service.run_pending()
            response = client.request(
                request.get_method(),
                request.selector,
                content=request.data,
                headers=dict(request.header_items()),
            )
            assert response.status_code in {200, 202}, response.text
            if request.get_method() == "POST":
                assert not receipt, "lost response was blindly replayed"
                receipt.update(response.json())
                raise URLError(ConnectionResetError())
            return Response(response)

        control = ControlClient("https://forge.example.test", token, opener=opener)
        status = main(
            (
                "recipe",
                "update",
                next(iter(recipes)),
                "--request-key",
                key,
                "--json",
                "--interval-seconds",
                "0.01",
            ),
            control_client=control,
        )
        completed = json.loads(capsys.readouterr().out)
        assert status == 0, completed
        assert completed["id"] == receipt["id"] and completed["state"] == "succeeded"
        assert completed["children"][0]["operation_id"]
        assert calls == [
            ("POST", "/api/recipe/update"),
            ("GET", f"/api/recipe/requests/{key}"),
            ("GET", f"/api/recipe/operations/{receipt['id']}"),
        ]
        hidden_actor = Actor("another", "administrator")
        actor = hidden_actor
        assert client.get(f"/api/recipe/requests/{key}").status_code == 404
        assert client.get(f"/api/recipe/operations/{receipt['id']}").status_code == 200


@pytest.fixture
def postgres_update_env(postgres_engine, tmp_path):
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine)
    recipe = _recipe("recipe-image.json")
    revision_id = str(uuid.uuid4())
    with sessions.begin() as session:
        revision = _add_revision(session, revision_id, recipe)
        revision.document_id = str(uuid.uuid4())
        _add_head(session, revision)
        session.add(User(subject="operator", role="operator"))
    now = [datetime.now(UTC)]

    def fresh():
        return RecipeImageAvailabilityService(
            sessions,
            storage=FilesystemRuntimeImageStorage(tmp_path / "images"),
            authority=lambda *args, **kwargs: (recipe, _runtime()),
            transport=Transport(),
            clock=lambda: now[0],
            claim_lease_seconds=10,
        )

    return sessions, revision_id, recipe, now, fresh


@pytest.fixture
def postgres_multi_update_env(postgres_engine, tmp_path):
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine)
    recipes: dict[str, RecipeDefinition] = {}
    with sessions.begin() as session:
        for index in range(2):
            base = _recipe("recipe-image.json")
            recipe = base.model_copy(
                update={
                    "identity": base.identity.model_copy(
                        update={"slug": f"cancel-batch-{index}"}
                    )
                }
            )
            revision_id = str(uuid.uuid4())
            revision = _add_revision(session, revision_id, recipe)
            revision.document_id = str(uuid.uuid4())
            _add_head(session, revision)
            recipes[revision_id] = recipe
        session.add(User(subject="operator", role="operator"))
    now = [datetime.now(UTC)]
    storage = FilesystemRuntimeImageStorage(tmp_path / "images")

    def fresh():
        return RecipeImageAvailabilityService(
            sessions,
            storage=storage,
            authority=lambda recipe_revision_id, **_: (
                recipes[recipe_revision_id],
                _runtime(),
            ),
            transport=Transport(),
            clock=lambda: now[0],
            claim_lease_seconds=10,
        )

    return sessions, list(recipes), now, fresh


def test_postgres_update_cancel_after_first_child_prevents_later_admission_after_restart(
    postgres_multi_update_env,
):
    sessions, revisions, now, fresh = postgres_multi_update_env
    service = fresh()
    parent = _start(service, revisions)
    assert len(parent.children) == 2

    claim = service.claim_update(owner="first-update-worker")
    assert claim is not None
    service.run_update_claim(claim)

    after_first_admission = service.get_operator_operation(parent.id)
    first, second = after_first_admission.children
    assert first.operation_id is not None
    assert first.state == "queued"
    assert second.operation_id is None
    assert second.state == "pending"
    with sessions() as session:
        admitted = tuple(
            session.scalars(select(Job).where(Job.request_id == first.request_key))
        )
        not_admitted = tuple(
            session.scalars(select(Job).where(Job.request_id == second.request_key))
        )
    assert len(admitted) == 1 and admitted[0].id == first.operation_id
    assert not not_admitted

    cancellation_key = str(uuid.uuid4())
    requested = service.cancel(
        parent.id,
        actor="operator",
        request_id=cancellation_key,
        reason="stop after the first recipe was admitted",
    )
    assert requested.state == "cancelling"
    assert requested.cancellation is not None
    assert requested.cancellation.cancel_request_id == cancellation_key

    # Make the next normal observation due. A live parent would now be
    # claimable; after process reconstruction, its durable cancellation must
    # keep the second child from being admitted.
    now[0] += timedelta(seconds=3)
    restarted = fresh()
    assert restarted.claim_update(owner="replacement-update-worker") is None
    with sessions() as session:
        not_admitted_after_restart = tuple(
            session.scalars(select(Job).where(Job.request_id == second.request_key))
        )
    assert not not_admitted_after_restart

    # The first child is exact durable work and must be cancelled before the
    # parent can settle; the second child's absence is the durable unissued
    # outcome. Reconcile through a reconstructed Controller owner.
    restarted.reconcile_cancellations()
    restarted.reconcile_cancellations()
    settled = restarted.get_operator_operation(parent.id)
    assert settled.state == "cancelled"
    assert settled.cancellation is not None
    assert settled.cancellation.cancel_request_id == cancellation_key
    first_after = settled.children[0]
    second_after = settled.children[1]
    assert first_after.operation_id == first.operation_id
    assert first_after.state == "cancelled"
    assert second_after.operation_id is None
    assert second_after.state == "cancelled"
    assert restarted.claim_update(owner="after-settlement-worker") is None
    with sessions() as session:
        child_jobs = tuple(
            session.scalars(
                select(Job).where(
                    Job.request_id.in_([first.request_key, second.request_key])
                )
            )
        )
        stored_parent = session.get(Job, parent.id)
    assert len(child_jobs) == 1
    assert child_jobs[0].id == first.operation_id
    assert child_jobs[0].state == "cancelled"
    assert stored_parent is not None and stored_parent.state == "cancelled"


def test_postgres_update_cancel_preserves_shared_model_child_for_unrelated_consumer(
    postgres_multi_update_env, tmp_path: Path, request: pytest.FixtureRequest
):
    from typing import Any

    sessions, revisions, now, _fresh = postgres_multi_update_env
    base_recipe = _recipe("recipe-image.json")
    recipes = {
        revision_id: base_recipe.model_copy(
            update={
                "identity": base_recipe.identity.model_copy(
                    update={"slug": f"cancel-batch-{index}"}
                )
            }
        )
        for index, revision_id in enumerate(revisions)
    }
    model = next(iter(recipes[revisions[0]].models)).model
    model_digest = model.content_sha256
    content = b"shared ModelCache artifact needed after update cancellation" * 32
    source = tmp_path / "shared-model.source"
    source.write_bytes(content)
    artifact: dict[str, object] = {
        "id": "weights",
        "path": "weights.bin",
        "kind": "file",
        "source": source.as_uri(),
        "sha256": hashlib.sha256(content).hexdigest(),
        "download_bytes": len(content),
        "roles": ["weights"],
        "model_content_sha256": model_digest,
    }
    cache = ModelCacheService(
        sessions,
        tmp_path / "models",
        reserve_bytes=0,
        clock=lambda: now[0],
        fixture_sources=True,
    )
    request.addfinalizer(cache.close)

    class CacheAdapter:
        def download_preview(self, *, recipe_revision_id: str):
            assert recipe_revision_id in recipes
            return cache.download_preview(
                model_content_sha256=model_digest, artifacts=[artifact]
            )

        def resolve_artifact_set(self, *, recipe_revision_id: str):
            assert recipe_revision_id in recipes
            return cache.resolve_artifact_set(
                model_content_sha256=model_digest, artifacts=[artifact]
            )

        def list_operations(self, *, limit: int = 100):
            return cache.list_operations(limit=limit)

        def start_download(self, **kwargs: Any):
            kwargs.pop("recipe_revision_id", None)
            return cache.start_download(
                model_content_sha256=model_digest,
                artifacts=[artifact],
                interrupt_after_bytes=1,
                **kwargs,
            )

        def get_operation(self, operation_id: str):
            return cache.get_operation(operation_id)

        def cancel_operation_in_session(self, *args: Any, **kwargs: Any):
            return cache.cancel_operation_in_session(*args, **kwargs)

        def signal_cancelled_operation(self, operation_id: str):
            return cache.signal_cancelled_operation(operation_id)

    def authority(
        recipe_revision_id: str, *, force: bool = False
    ) -> tuple[RecipeDefinition, dict[str, object]]:
        del force
        return recipes[recipe_revision_id], _runtime()

    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path / "images"),
        authority=authority,
        transport=Transport(),
        model_cache=CacheAdapter(),
        clock=lambda: now[0],
        claim_lease_seconds=10,
    )
    batch = _start(service, revisions)
    first_claim = service.claim_update(owner="shared-update-first")
    assert first_claim is not None
    service.run_update_claim(first_claim)
    first_parent = service.get_operator_operation(batch.id)
    assert isinstance(first_parent, RecipeUpdateResponse)
    first = first_parent.children[0]
    assert first.operation_id is not None
    image_claim = service.claim_pending(limit=1, owner_id="shared-update-child-one")
    assert len(image_claim) == 1 and image_claim[0].operation_id == first.operation_id
    service.run_claim(image_claim[0])
    first_view = service.get(first.operation_id)
    assert isinstance(first_view, RecipeImageAvailabilityView)
    assert first_view.state == "partial" and first_view.model_child is not None
    shared_child_id = str(first_view.model_child["id"])
    shared_child = cache.get_operation(shared_child_id)
    assert shared_child.state == "partial"
    shared_artifact_set = shared_child.artifact_set_sha256
    assert isinstance(shared_artifact_set, str)
    downloaded_bytes = shared_child.progress.get("downloaded_bytes")
    assert isinstance(downloaded_bytes, int) and downloaded_bytes > 0

    now[0] += timedelta(seconds=3)
    second_claim = service.claim_update(owner="shared-update-second")
    assert second_claim is not None
    service.run_update_claim(second_claim)
    admitted = service.get_operator_operation(batch.id)
    assert isinstance(admitted, RecipeUpdateResponse)
    second = admitted.children[1]
    assert second.operation_id is not None
    child_claims = service.claim_pending(limit=2, owner_id="shared-update-children")
    assert {claim.operation_id for claim in child_claims} == {
        first.operation_id,
        second.operation_id,
    }
    for claim in child_claims:
        service.run_claim(claim)
    first_view = service.get(first.operation_id)
    second_view = service.get(second.operation_id)
    assert isinstance(first_view, RecipeImageAvailabilityView)
    assert isinstance(second_view, RecipeImageAvailabilityView)
    assert first_view.model_child is not None
    assert second_view.model_child is not None
    assert first_view.model_child["id"] == second_view.model_child["id"]
    assert first_view.model_child["id"] == shared_child_id
    assert first_view.model_child["artifact_set_sha256"] == shared_artifact_set
    assert second_view.model_child["artifact_set_sha256"] == shared_artifact_set

    unrelated = service.start(
        revisions[0],
        actor="operator",
        request_id=str(uuid.uuid4()),
    )
    unrelated_claim = service.claim_pending(
        limit=1, owner_id="unrelated-shared-consumer"
    )
    assert len(unrelated_claim) == 1
    assert unrelated_claim[0].operation_id == unrelated.id
    service.run_claim(unrelated_claim[0])
    unrelated_view = service.get(unrelated.id)
    assert isinstance(unrelated_view, RecipeImageAvailabilityView)
    assert unrelated_view.state == "partial"
    assert unrelated_view.model_child is not None
    assert unrelated_view.model_child["id"] == shared_child_id
    assert unrelated_view.model_child["artifact_set_sha256"] == shared_artifact_set

    partial_path = cache._partial_path(shared_artifact_set, str(artifact["sha256"]))
    partial_bytes = partial_path.read_bytes()
    assert partial_bytes and content.startswith(partial_bytes)
    cancellation_key = str(uuid.uuid4())
    accepted = service.cancel(
        batch.id,
        actor="operator",
        request_id=cancellation_key,
        reason="stop the multi-recipe update",
    )
    assert accepted.state == "cancelling"
    service.reconcile_cancellations()
    service.reconcile_cancellations()

    settled = service.get_operator_operation(batch.id)
    assert isinstance(settled, RecipeUpdateResponse)
    assert settled.state == "cancelled"
    assert settled.cancellation is not None
    assert settled.cancellation.cancel_request_id == cancellation_key
    assert [child.state for child in settled.children] == ["cancelled", "cancelled"]
    unchanged = service.get(unrelated.id)
    assert isinstance(unchanged, RecipeImageAvailabilityView)
    assert unchanged.state == "partial"
    assert unchanged.cancellation is None
    assert unchanged.model_child is not None
    assert unchanged.model_child["id"] == shared_child_id
    assert unchanged.model_child["artifact_set_sha256"] == shared_artifact_set
    shared_after_cancel = cache.get_operation(shared_child_id)
    assert shared_after_cancel.state == "partial"
    assert shared_after_cancel.cancellation is None
    remaining_bytes = shared_after_cancel.progress.get("downloaded_bytes")
    assert isinstance(remaining_bytes, int) and remaining_bytes >= downloaded_bytes
    assert partial_path.read_bytes() == partial_bytes

    assert cache.run_pending() == 1
    assert cache.get_operation(shared_child_id).state == "succeeded"
    assert cache._object_path(str(artifact["sha256"])).read_bytes() == content
    now[0] += timedelta(seconds=3)
    unrelated_claim = service.claim_pending(limit=1, owner_id="unrelated-resume")
    assert len(unrelated_claim) == 1
    assert unrelated_claim[0].operation_id == unrelated.id
    service.run_claim(unrelated_claim[0])
    completed_unrelated = service.get(unrelated.id)
    assert completed_unrelated.state == "succeeded"
    assert completed_unrelated.cancellation is None
    assert completed_unrelated.model_child is not None
    assert completed_unrelated.model_child["id"] == shared_child_id
    assert completed_unrelated.model_child["artifact_set_sha256"] == shared_artifact_set


@pytest.mark.parametrize("changed_scope", [False, True])
def test_postgres_concurrent_parent_acceptance_reconciles_original_key(
    postgres_update_env, changed_scope
):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    sessions, revision, recipe, _, fresh = postgres_update_env
    barrier = threading.Barrier(2, timeout=5)
    services = [fresh(), fresh()]
    key = str(uuid.uuid4())

    for service in services:
        actual_replay = service._updates._replay

        def replay(*args, actual=actual_replay):
            result = actual(*args)
            barrier.wait()
            return result

        service._updates._replay = replay

    def submit(index):
        try:
            return services[index].update(
                actor="operator",
                request_id=key,
                selectors=[
                    recipe.identity.slug if changed_scope and index else revision
                ],
                all=False,
            )
        except RecipeImageAvailabilityError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    errors = [
        result for result in results if isinstance(result, RecipeImageAvailabilityError)
    ]
    assert len(errors) == int(changed_scope)
    assert all(error.code == "recipe_update.request_key_reused" for error in errors)
    assert (
        len(
            {
                result.id
                for result in results
                if not isinstance(result, RecipeImageAvailabilityError)
            }
        )
        == 1
    )
    with sessions() as session:
        assert len(list(session.scalars(select(Job)))) == 1


@pytest.mark.parametrize("pause_after_commit", [False, True])
def test_postgres_takeover_fences_old_parent_admission_and_result(
    postgres_update_env, pause_after_commit
):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    sessions, revision, _, now, fresh = postgres_update_env
    service = fresh()
    parent = _start(service, [revision])
    old_claim = service.claim_update(owner="old-worker")
    entered, release = threading.Event(), threading.Event()
    attr = "_start_request" if pause_after_commit else "_authority"
    actual = getattr(service, attr)

    def paused(*args, **kwargs):
        result = actual(*args, **kwargs)
        entered.set()
        assert release.wait(10)
        return result

    setattr(service, attr, paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old_future = pool.submit(service.run_update_claim, old_claim)
        try:
            assert entered.wait(5)
            now[0] += timedelta(seconds=11)
            replacement = fresh()
            new_claim = replacement.claim_update(owner="new-worker")
            assert new_claim is not None
            replacement.run_update_claim(new_claim)
            accepted = replacement.get_operator_operation(parent.id)
        finally:
            release.set()
        if pause_after_commit:
            with pytest.raises(RecipeImageAvailabilityError, match="no longer owns"):
                old_future.result(timeout=5)
        else:
            old_future.result(timeout=5)
    assert fresh().get_operator_operation(parent.id) == accepted
    with sessions() as session:
        assert len(list(session.scalars(select(Job)))) == 2


def test_postgres_revocation_wins_before_child_acceptance(postgres_update_env):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from vonk_control.user_authority import serialize_user_authority

    sessions, revision, _, now, fresh = postgres_update_env
    service = fresh()
    parent = _start(service, [revision])
    entered, release = threading.Event(), threading.Event()
    actual = service._authority

    def paused(*args, **kwargs):
        result = actual(*args, **kwargs)
        entered.set()
        assert release.wait(10)
        return result

    service._authority = paused
    claim = service.claim_update(owner="worker")
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(service.run_update_claim, claim)
        try:
            assert entered.wait(5)
            with sessions.begin() as session:
                serialize_user_authority(session)
                session.scalar(
                    select(User).where(User.subject == "operator")
                ).disabled_at = now[0]
        finally:
            release.set()
        worker.result(timeout=5)
    observed = fresh().get_operator_operation(parent.id)
    assert observed.state == "failed"
    assert observed.children[0].failure.code == "recipe_update.authority_denied"
    with sessions() as session:
        assert len(list(session.scalars(select(Job)))) == 1


def test_scheduler_keeps_image_slot_available_while_parent_admits_another_child(
    update_env,
):
    import threading

    from vonk_control.availability_production import RecipeImageAvailabilityScheduler

    _, recipes, _, fresh = update_env
    service = fresh()
    first, second = recipes
    existing = service.start(first, actor="operator", request_id=str(uuid.uuid4()))
    parent = _start(service, [second])
    metadata_entered, release_metadata, image_finished = (
        threading.Event() for _ in range(3)
    )
    actual_authority, actual_run = service._authority, service.run_claim

    def slow_metadata(*args, **kwargs):
        metadata_entered.set()
        assert release_metadata.wait(10)
        return actual_authority(*args, **kwargs)

    def image_run(claim):
        actual_run(claim)
        image_finished.set()

    service._authority = slow_metadata
    service.run_claim = image_run
    scheduler = RecipeImageAvailabilityScheduler(service, max_workers=1)
    try:
        assert scheduler.tick() == 2
        assert metadata_entered.wait(3)
        assert image_finished.wait(3), "parent consumed the only image execution slot"
        assert service.get(existing.id).state == "succeeded"
        waiting = service.get_operator_operation(parent.id)
        assert waiting.next_attempt_at is not None and waiting.wait_owner
    finally:
        release_metadata.set()
        scheduler.close()


def test_cancelling_update_before_admission_settles_without_issuing_children(
    update_env,
):
    sessions, recipes, _, fresh = update_env
    service = fresh()
    parent = _start(service, list(recipes))

    accepted = service.cancel(
        parent.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000911",
        reason="stop this update",
    )
    assert accepted.state == "cancelling"
    assert accepted.cancellation is not None
    service.reconcile_cancellations()

    observed = service.get_operator_operation(parent.id)
    assert observed.state == "cancelled"
    assert all(child.state == "cancelled" for child in observed.children)
    assert all(child.operation_id is None for child in observed.children)
    with sessions() as session:
        jobs = list(session.scalars(select(Job)))
        assert len(jobs) == 1 and jobs[0].id == parent.id


def test_update_cancellation_recovers_child_committed_before_parent_checkpoint(
    update_env,
):
    sessions, recipes, _, fresh = update_env
    service = fresh()
    revision_id = next(iter(recipes))
    parent = _start(service, [revision_id])
    claim = service.claim_update(owner="crash-window")
    assert claim is not None
    with sessions() as session:
        parent_row = session.get(Job, parent.id)
        assert parent_row is not None
        document = service._updates._document(parent_row)
        child_intent = service._updates._intent(document.children[0])
        child_request_key = document.children[0].request_key

    child = service._start_request(
        child_intent,
        actor="operator",
        request_id=child_request_key,
        update_claim=claim,
    )
    assert child.state == "queued"
    assert service.get_operator_operation(parent.id).children[0].operation_id is None

    service.cancel(
        parent.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000912",
        reason="stop after child acceptance",
    )
    service.reconcile_cancellations()
    assert service.get(child.id).state == "cancelled"
    # The child existed durably before the parent recorded its returned ID.
    # Reconciliation binds that exact request and then settles the parent.
    service.reconcile_cancellations()
    observed = service.get_operator_operation(parent.id)
    assert observed.state == "cancelled"
    assert observed.children[0].operation_id == child.id
    assert observed.children[0].state == "cancelled"
    with sessions() as session:
        matching = list(
            session.scalars(select(Job).where(Job.request_id == child_request_key))
        )
        assert len(matching) == 1 and matching[0].id == child.id
