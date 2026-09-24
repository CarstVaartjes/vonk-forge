"""The recipe parent delegates model-child cancellation to the durable owner."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import subprocess
import sys
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control.bounded_json import require_mapping
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import Base, CatalogDocument, Job, User
from vonk_control.recipe_image_availability import (
    RecipeImageAvailabilityService,
)
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage
from vonk_forge_contracts import RecipeDefinition

from .test_recipe_image_availability import (
    Transport,
    _add_revision,
    _recipe,
    _runtime,
)


def _hold_model_worker(
    dsn: str,
    cache_root: str,
    reached: Any,
    release: Any,
) -> None:
    """Hold the real per-artifact writer lock after verified partial bytes."""

    engine = create_engine(dsn, pool_pre_ping=True)
    sessions = sessionmaker(engine, expire_on_commit=False)
    cache = ModelCacheService(
        sessions, Path(cache_root), reserve_bytes=0, fixture_sources=True
    )
    verify = cache._verify_file

    def pause_after_verification(path, spec):
        verified = verify(path, spec)
        if path.suffix == ".part" and verified:
            reached.set()
            if not release.wait(30):
                raise TimeoutError(
                    "model cancellation artifact barrier was not released"
                )
        return verified

    cache._verify_file = pause_after_verification
    cache.run_pending()
    cache.close()
    engine.dispose()


_CONTROLLER = r"""
import json, os, sys
from datetime import datetime
from pathlib import Path
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import Actor
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import Job
from vonk_control.recipe_image_availability import RecipeImageAvailabilityService
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage
from vonk_forge_contracts import RecipeDefinition

config = json.loads(Path(sys.argv[1]).read_text())
engine = create_engine(config["database"], pool_pre_ping=True)
sessions = sessionmaker(engine, expire_on_commit=False)
now = datetime.fromisoformat(config["now"])
recipe = RecipeDefinition.model_validate_json(json.dumps(config["recipe"]))
artifact = config["artifact"]
model_digest = config["model_digest"]
cache = ModelCacheService(
    sessions, Path(config["cache_root"]), reserve_bytes=0, fixture_sources=True,
    clock=lambda: now,
)

class CacheAdapter:
    def download_preview(self, *, recipe_revision_id):
        assert recipe_revision_id == config["recipe_revision_id"]
        return cache.download_preview(
            model_content_sha256=model_digest, artifacts=[artifact]
        )
    def resolve_artifact_set(self, *, recipe_revision_id):
        assert recipe_revision_id == config["recipe_revision_id"]
        return cache.resolve_artifact_set(
            model_content_sha256=model_digest, artifacts=[artifact]
        )
    def list_operations(self, *, limit=100):
        return cache.list_operations(limit=limit)
    def start_download(self, **kwargs):
        kwargs.pop("recipe_revision_id", None)
        return cache.start_download(
            model_content_sha256=model_digest, artifacts=[artifact], **kwargs
        )
    def get_operation(self, operation_id):
        return cache.get_operation(operation_id)
    def cancel_operation_in_session(self, *args, **kwargs):
        return cache.cancel_operation_in_session(*args, **kwargs)
    def signal_cancelled_operation(self, operation_id):
        return cache.signal_cancelled_operation(operation_id)

availability = RecipeImageAvailabilityService(
    sessions,
    storage=FilesystemRuntimeImageStorage(Path(config["image_root"])),
    authority=lambda revision_id, **_: (recipe, config["runtime"]),
    model_cache=CacheAdapter(),
    clock=lambda: now,
    claim_lease_seconds=10,
)
mode = sys.argv[2]

if mode == "accept-cancellation":
    original_signal = cache.signal_cancelled_operation
    def signal_after_commit(operation_id):
        # A separate SQLAlchemy session must see the durable child intent before
        # the process-local wakeup is sent.
        child = cache.get_operation(operation_id)
        assert child.state == "cancelling"
        assert child.cancellation["actor"] == "operator"
        assert child.cancellation["reason"] == config["reason"]
        original_signal(operation_id)
    cache.signal_cancelled_operation = signal_after_commit
    receipt = availability.cancel(
        config["parent_id"], actor="operator",
        request_id=config["cancel_request_id"], reason=config["reason"],
    )
    assert receipt.state == "cancelling"
    availability.reconcile_cancellations()
    assert availability.get(config["parent_id"]).state == "cancelling"
    assert cache.get_operation(config["child_id"]).state == "cancelling"
    os._exit(23)

if mode == "new-consumer":
    second = availability.start(
        config["recipe_revision_id"], actor="operator",
        request_id=config["second_request_id"],
    )
    claim = availability.claim_pending(limit=1, owner_id="after-cancel-restart")[0]
    with sessions() as session:
        row = session.get(Job, second.id)
        assert row is not None
        payload = dict(row.payload)
    child = availability._current_model_child(
        payload, actor="operator", parent_request_key=second.request_id
    )
    assert child is not None
    assert child["id"] != config["child_id"]
    assert availability._update_model_progress(claim, child)
    try:
        # A stale candidate read that raced cancellation cannot attach the old
        # child after its durable cancellation fence committed.
        availability._update_model_progress(
            claim,
            {"id": config["child_id"], "state": "running"},
        )
    except Exception as error:
        assert getattr(error, "code", None) == "recipe_image.model_child_cancelled"
    else:
        raise AssertionError("a new parent attached to a cancelling ModelCache child")
    cache._reconcile_pending_cancellations()
    availability.reconcile_cancellations()
    first = availability.get(config["parent_id"])
    old_child = cache.get_operation(config["child_id"])
    print(json.dumps({
        "parent_state": first.state,
        "child_state": old_child.state,
        "child_cancellation": old_child.cancellation,
        "second_id": second.id,
        "second_child_id": availability.get(second.id).model_child["id"],
    }))
    engine.dispose()
    raise SystemExit(0)

if mode == "recover-cancellation":
    cache._reconcile_pending_cancellations()
    availability.reconcile_cancellations()
    parent = availability.get(config["parent_id"])
    child = cache.get_operation(config["child_id"])
    with sessions() as session:
        second = session.scalar(
            select(Job).where(Job.request_id == config["second_request_id"])
        )
        assert second is not None
    print(json.dumps({
        "parent_state": parent.state,
        "parent_cancel_request_id": parent.cancellation.cancel_request_id,
        "child_state": child.state,
        "child_cancellation": child.cancellation,
        "second_state": availability.get(second.id).state,
    }))
    engine.dispose()
    raise SystemExit(0)

raise AssertionError("unknown test controller mode")
"""


def _controller(
    config_path: Path, mode: str, *, expected_code: int = 0
) -> dict[str, object] | None:
    result = subprocess.run(
        [sys.executable, "-c", _CONTROLLER, str(config_path), mode],
        capture_output=True,
        text=True,
        timeout=35,
        check=False,
    )
    assert result.returncode == expected_code, result.stderr
    if expected_code != 0:
        return None
    parsed = json.loads(result.stdout)
    if not isinstance(parsed, dict):
        raise TypeError("controller response was not a JSON object")
    response: dict[str, object] = {}
    for key, value in parsed.items():
        if not isinstance(key, str):
            raise TypeError("controller response had a non-string key")
        response[key] = value
    return response


def test_recipe_parent_waits_for_model_child_effect_after_controller_restart(
    tmp_path: Path, postgres_engine
) -> None:
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    recipe = _recipe("recipe-image.json")
    # _add_revision prefixes the revision key with ``document-`` for the
    # owning document identity, which is limited to 36 characters.
    recipe_revision_id = uuid.uuid4().hex[:24]
    now = datetime.now(UTC)
    first_model = next(iter(recipe.models), None)
    assert first_model is not None
    model_digest = first_model.model.content_sha256
    content = b"verified model artifact retained across recipe cancellation" * 64
    source = tmp_path / "weights.source"
    source.write_bytes(content)
    artifact = {
        "id": "weights",
        "path": "weights.bin",
        "kind": "file",
        "source": source.as_uri(),
        "sha256": hashlib.sha256(content).hexdigest(),
        "download_bytes": len(content),
        "roles": ["weights"],
        "model_content_sha256": model_digest,
    }
    with sessions.begin() as session:
        session.add(
            CatalogDocument(
                id="document-" + recipe_revision_id,
                kind="recipe",
                publisher=recipe.identity.publisher,
                slug=recipe.identity.slug,
                title=recipe.metadata.title,
                created_by="test",
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        _add_revision(session, recipe_revision_id, recipe)
        session.add(User(subject="operator", role="operator"))

    cache_root = tmp_path / "model-cache"
    image_root = tmp_path / "image-cache"
    cache = ModelCacheService(
        sessions, cache_root, reserve_bytes=0, fixture_sources=True
    )

    class CacheAdapter:
        def download_preview(self, *, recipe_revision_id: str):
            assert recipe_revision_id == recipe_revision_id_value
            return cache.download_preview(
                model_content_sha256=model_digest, artifacts=[artifact]
            )

        def resolve_artifact_set(self, *, recipe_revision_id: str):
            assert recipe_revision_id == recipe_revision_id_value
            return cache.resolve_artifact_set(
                model_content_sha256=model_digest, artifacts=[artifact]
            )

        def list_operations(self, *, limit: int = 100):
            return cache.list_operations(limit=limit)

        def start_download(self, **kwargs: Any):
            kwargs.pop("recipe_revision_id", None)
            return cache.start_download(
                model_content_sha256=model_digest, artifacts=[artifact], **kwargs
            )

        def get_operation(self, operation_id: str):
            return cache.get_operation(operation_id)

        def cancel_operation_in_session(self, *args: Any, **kwargs: Any):
            return cache.cancel_operation_in_session(*args, **kwargs)

        def signal_cancelled_operation(self, operation_id: str):
            return cache.signal_cancelled_operation(operation_id)

    recipe_revision_id_value = recipe_revision_id

    def resolve_recipe_authority(
        recipe_revision_id: str, *, force: bool = False
    ) -> tuple[RecipeDefinition, Mapping[str, object]]:
        del recipe_revision_id, force
        return recipe, _runtime()

    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(image_root),
        authority=resolve_recipe_authority,
        transport=Transport(),
        model_cache=CacheAdapter(),
        clock=lambda: now,
    )
    parent_request_id = str(uuid.uuid4())
    parent = service.start(
        recipe_revision_id,
        actor="operator",
        request_id=parent_request_id,
    )
    assert service.run_pending() == 1
    parent = service.get(parent.id)
    assert parent.state == "partial"
    assert parent.model_child is not None
    child_id = str(parent.model_child["id"])
    child = cache.get_operation(child_id)
    assert child.state == "queued"
    set_digest = child.artifact_set_sha256
    assert set_digest is not None

    context = multiprocessing.get_context("spawn")
    reached = context.Event()
    release = context.Event()
    worker = context.Process(
        target=_hold_model_worker,
        args=(
            postgres_engine.url.render_as_string(hide_password=False),
            str(cache_root),
            reached,
            release,
        ),
    )
    cancel_request_id = str(uuid.uuid4())
    second_request_id = str(uuid.uuid4())
    reason = "stop this recipe preparation"
    config = {
        "database": postgres_engine.url.render_as_string(hide_password=False),
        "cache_root": str(cache_root),
        "image_root": str(image_root),
        "recipe_revision_id": recipe_revision_id,
        "recipe": recipe.model_dump(mode="json"),
        "runtime": _runtime(),
        "artifact": artifact,
        "model_digest": model_digest,
        "now": now.isoformat(),
        "parent_id": parent.id,
        "child_id": child_id,
        "cancel_request_id": cancel_request_id,
        "second_request_id": second_request_id,
        "reason": reason,
    }
    config_path = tmp_path / "model-cancel-process.json"
    config_path.write_text(json.dumps(config))
    config_path.chmod(0o600)
    try:
        worker.start()
        assert reached.wait(25), (
            f"model worker missed artifact barrier: {worker.exitcode}"
        )
        _controller(config_path, "accept-cancellation", expected_code=23)
        durable = cache.get_operation(child_id)
        assert durable.state == "cancelling"
        assert durable.cancellation is not None
        assert durable.cancellation["actor"] == "operator"
        assert durable.cancellation["reason"] == reason
        assert service.get(parent.id).state == "cancelling"

        joined = _controller(config_path, "new-consumer")
        assert joined is not None
        assert joined.get("parent_state") == "cancelling"
        assert joined.get("child_state") == "cancelling"
        assert joined.get("second_child_id") != child_id
        expected_child_cancel_key = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "vonk:recipe-availability-model-cancel:"
                f"{parent.id}:{cancel_request_id}:{child_id}",
            )
        )
        child_cancellation = require_mapping(
            joined.get("child_cancellation"), "child cancellation"
        )
        assert child_cancellation.get("request_key") == expected_child_cancel_key
        assert child_cancellation.get("reason") == reason
        assert child_cancellation.get("actor") == "operator"

        worker.terminate()
        worker.join(timeout=10)
        assert worker.exitcode is not None
        recovered = _controller(config_path, "recover-cancellation")
        assert recovered is not None
        assert recovered.get("parent_state") == "cancelled"
        assert recovered.get("parent_cancel_request_id") == cancel_request_id
        assert recovered.get("child_state") == "cancelled"
        assert recovered.get("second_state") == "running"
        partial = cache_root / "partials" / set_digest / f"{artifact['sha256']}.part"
        assert partial.read_bytes() == content
        with sessions() as session:
            second = session.scalar(
                select(Job).where(Job.request_id == second_request_id)
            )
            assert second is not None
            second_payload = require_mapping(second.payload, "second parent payload")
            model_child = require_mapping(
                second_payload.get("model_child"), "second model child"
            )
            assert model_child.get("id") != child_id
    finally:
        if worker.is_alive():
            # Wake a live worker before cleanup; setting a multiprocessing
            # Event after forcibly terminating its waiter can deadlock notify.
            release.set()
            worker.terminate()
        worker.join(timeout=10)
        cache.close()
