"""Model removal is decided by what the database and the disk show.

A removal waits (visibly, keeping the bytes) while an installation uses the
model, and finishes once it ends; newer intent for the model wins over a removal
that has removed nothing yet; a removal of a model that is already gone ends at
once with nothing removed; an installation plan that cannot be read keeps only
its own model. Every installation plan is read as an installation plan, never
as another owner's document.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_control import model_cache_states
from vonk_control.agent_operation_facts import aware
from vonk_control.artifact_reference_scan import runtime_image_reference_findings
from vonk_control.model_cache import ModelCacheService
from vonk_control.model_cache_contract import ModelCacheRemovalResult
from vonk_control.models import (
    ArtifactLifecycleGate,
    ModelCacheOperation,
    ModelCacheSet,
    RecipeInstallation,
)
from vonk_control.recipe_execution_contract import installation_plan_document
from vonk_control.recovery_policy import RecoveryPolicy

from .test_catalog_revision_collection import NODE, NOW, Catalog
from .test_model_cache import _artifact, _download, _remove_model
from .test_model_removal_reference_lifecycle import (
    _freeze_removal_reconcile_budget,  # noqa: F401  (autouse: reference ownership, not the work budget)
    _removal_service,
    _settle_removal,
)
from .test_model_removal_worker_postgres import _prepare_removal_target

_COMPILED = Path(__file__).parent / "fixtures" / "compiled_workload_v2.json"


def _world(postgres_engine, tmp_path: Path) -> tuple[Catalog, ModelCacheService]:
    service, _sessions = _removal_service(postgres_engine, tmp_path)
    world = Catalog(postgres_engine)
    service._clock = lambda: world.now
    return world, service


def _cached_model(
    service: ModelCacheService, world: Catalog, tmp_path: Path, slug: str
) -> tuple[str, str, str, Path]:
    """A model on the NAS: its selector, digest, cached set and object file."""

    selector, model_digest, object_path = _prepare_removal_target(
        service, world.sessions, tmp_path, slug=slug, data=slug.encode()
    )
    with world.sessions() as session:
        set_digest = session.scalar(
            select(ModelCacheSet.artifact_set_sha256).where(
                ModelCacheSet.model_content_sha256 == model_digest
            )
        )
    assert set_digest is not None
    return selector, model_digest, set_digest, object_path


def _request_download(
    service: ModelCacheService, tmp_path: Path, slug: str, model_digest: str
) -> str:
    """A fresh request for the model after the removal; its end state."""

    source = tmp_path / f"again-{slug}"
    source.mkdir()
    artifact = _artifact(source, slug.encode(), model_content_sha256=model_digest)
    return _download(
        service, [artifact], model_content_sha256=model_digest, request_key=str(uuid4())
    ).state


def _installation(
    world: Catalog, set_digest: str, *, plan: dict[str, object] | None = None
) -> str:
    """An installed recipe whose own plan runs the model set on the Spark."""

    revision = world.revision(f"recipe-{uuid4().hex[:8]}", 1)
    installation_id, _run_id = world.workload(
        revision, run="stopped", touched=world.now
    )
    compiled = json.loads(_COMPILED.read_text())
    compiled["identity"]["model_artifact_set_sha256"] = set_digest
    with world.sessions.begin() as session:
        row = session.get(RecipeInstallation, installation_id)
        assert row is not None
        stored = dict(row.plan)
        stored["nodes"] = [
            {
                "node_id": NODE,
                "rank": 0,
                "role": "main",
                "allowed": True,
                "inventory_observed_at": None,
                "free_bytes": 1024,
                "active_reserved_bytes": 0,
                "reused_bytes": 0,
                "required_download_bytes": 0,
                "required_bytes": 1,
                "disk_floor_bytes": 0,
                "free_after_bytes": 0,
                "blockers": [],
                "warnings": [],
            }
        ]
        stored["compiled_execution_plans"] = {NODE: compiled}
        row.plan = installation_plan_document(stored) if plan is None else plan
        row.model_content_sha256 = session.scalar(
            select(ModelCacheSet.model_content_sha256).where(
                ModelCacheSet.artifact_set_sha256 == set_digest
            )
        )
    return installation_id


def _uninstall(world: Catalog, installation_id: str) -> None:
    with world.sessions.begin() as session:
        row = session.get(RecipeInstallation, installation_id)
        assert row is not None
        row.state = "uninstalled"


def _remove(service: ModelCacheService, selector: str) -> str:
    return _remove_model(
        service,
        selector,
        actor="operator",
        request_key=str(uuid4()),
        model_content_sha256="",
    ).id


def _wait_out(service: ModelCacheService, world: Catalog, operation_id: str) -> None:
    """Give the removal more turns than any failure budget, each when it is due."""

    for _ in range(RecoveryPolicy().max_failures + 2):
        service.advance_removals(limit=1)
        with world.sessions() as session:
            row = session.get(ModelCacheOperation, operation_id)
            assert row is not None
            if row.state not in model_cache_states.LIVE:
                return
            due = row.next_action_at
        world.now = max(world.now + timedelta(seconds=1), aware(due) if due else NOW)


def _operation(
    sessions: sessionmaker[Session], operation_id: str
) -> ModelCacheOperation:
    with sessions() as session:
        row = session.get(ModelCacheOperation, operation_id)
        assert row is not None
        return row


def test_a_removal_waits_for_the_installation_using_the_model_then_finishes(
    postgres_engine, tmp_path: Path
) -> None:
    world, service = _world(postgres_engine, tmp_path)
    try:
        selector, model, set_digest, object_path = _cached_model(
            service, world, tmp_path, "in-use"
        )
        installation = _installation(world, set_digest)

        removal = _remove(service, selector)
        _wait_out(service, world, removal)

        waiting = _operation(world.sessions, removal)
        assert waiting.state in model_cache_states.LIVE, waiting.last_error
        assert installation in str(waiting.last_error)
        assert object_path.exists()

        _uninstall(world, installation)
        ended = _settle_removal(service, service.get_operation(removal))
        assert ended.state == "succeeded", ended.last_error
        assert not object_path.exists()

        # A fresh request for the model is admitted and brings it back.
        assert _request_download(service, tmp_path, "in-use", model) == "succeeded"
        assert object_path.exists()
    finally:
        service.close()


def test_newer_intent_for_the_model_wins_over_a_waiting_removal(
    postgres_engine, tmp_path: Path
) -> None:
    world, service = _world(postgres_engine, tmp_path)
    try:
        selector, model, set_digest, object_path = _cached_model(
            service, world, tmp_path, "wanted-again"
        )
        _installation(world, set_digest)
        removal = _remove(service, selector)
        _wait_out(service, world, removal)

        world.now += timedelta(minutes=1)
        newer = _installation(world, set_digest)
        _wait_out(service, world, removal)

        ended = _operation(world.sessions, removal)
        assert ended.state == "cancelled"
        assert newer in str(ended.last_error)
        assert object_path.exists()
        with world.sessions() as session:
            gate = session.get(ArtifactLifecycleGate, ("model-set", set_digest))
            assert gate is None or gate.removal_owner_id is None
        # The released fence admits the newer consumer's own request.
        assert (
            _request_download(service, tmp_path, "wanted-again", model) == "succeeded"
        )
    finally:
        service.close()


def test_removing_a_model_that_is_already_gone_ends_with_nothing_removed(
    postgres_engine, tmp_path: Path
) -> None:
    world, service = _world(postgres_engine, tmp_path)
    try:
        selector, model, _set, object_path = _cached_model(
            service, world, tmp_path, "gone"
        )
        first = _settle_removal(
            service, service.get_operation(_remove(service, selector))
        )
        assert first.state == "succeeded" and not object_path.exists()

        again = _settle_removal(
            service, service.get_operation(_remove(service, selector))
        )

        assert again.state == "succeeded", again.last_error
        assert again.result == ModelCacheRemovalResult(
            schema_version=2, removed_entries=[], reclaimed_bytes=0
        )
        assert _request_download(service, tmp_path, "gone", model) == "succeeded"
        assert object_path.exists()
    finally:
        service.close()


def test_an_unreadable_installation_plan_keeps_only_its_own_model(
    postgres_engine, tmp_path: Path
) -> None:
    world, service = _world(postgres_engine, tmp_path)
    try:
        kept, _kept, kept_set, kept_path = _cached_model(
            service, world, tmp_path, "kept"
        )
        other, other_model, _other_set, other_path = _cached_model(
            service, world, tmp_path, "other"
        )
        damaged = _installation(world, kept_set, plan={})

        unrelated = _settle_removal(
            service, service.get_operation(_remove(service, other))
        )
        assert unrelated.state == "succeeded", unrelated.last_error
        assert not other_path.exists()

        removal = _remove(service, kept)
        _wait_out(service, world, removal)
        waiting = _operation(world.sessions, removal)
        assert waiting.state in model_cache_states.LIVE
        assert f"installation {damaged} (plan unreadable)" in str(waiting.last_error)
        assert kept_path.exists()
        # The unknown never held the other model: it can be fetched afresh.
        assert _request_download(service, tmp_path, "other", other_model) == "succeeded"
    finally:
        service.close()


def test_an_installation_keeps_the_image_its_own_plan_runs(postgres_engine, tmp_path):
    world, service = _world(postgres_engine, tmp_path)
    try:
        compiled = json.loads(_COMPILED.read_text())
        archive = compiled["runtime_image"]["oci_layout_sha256"]
        unrelated = "f" * 64
        installation = _installation(world, "a" * 64)
        with world.sessions() as session:
            found = runtime_image_reference_findings(session, (archive, unrelated))
        assert {item.owner_id for item in found[archive]} >= {installation}
        assert not found[unrelated]

        with world.sessions.begin() as session:
            row = session.get(RecipeInstallation, installation)
            assert row is not None
            row.plan = {}
        with world.sessions() as session:
            found = runtime_image_reference_findings(session, (archive,))
        # Its recipe has no known builds: the unknown keeps what was asked about.
        assert any("plan unreadable" in item.reason for item in found[archive])
    finally:
        service.close()
