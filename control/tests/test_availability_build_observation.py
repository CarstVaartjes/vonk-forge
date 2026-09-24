"""Production build observation must yield and recover its accepted child."""

import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from vonk_control import availability_production
from vonk_control.availability_production import build_recipe_image_availability
from vonk_control.bounded_json import require_mapping
from vonk_control.models import Job, NodeInventorySnapshot, RecipeBuild
from vonk_control.recipe_image_availability import RecipeImageAvailabilityError

from .recipe_removal_review_support import remove_after_review
from .test_build_cancellation_recovery import _active_claims, _evidence, _services
from .test_build_consumer_ownership import _availability
from .test_recipe_builds import _write_controller_build_receipt


def test_build_observer_yields_and_recovers_a_committed_child_before_replanning(
    tmp_path, postgres_engine, monkeypatch
):
    sessions, builds, operations, storage, now, node, revision, plan = _services(
        tmp_path, postgres_engine
    )
    owner = _availability(sessions, storage, now, revision, plan)
    parent = owner.start(revision.id, actor="operator", request_id=str(uuid.uuid4()))
    claim = owner.claim_pending(limit=1)[0]

    def production():
        return build_recipe_image_availability(
            sessions,
            artifact_root=tmp_path / "images",
            managed_catalog_sync=None,
            recipe_builds=builds,
            recipe_operations=operations,
            clock=lambda: now,
        )

    def observe(composition):
        with sessions() as session:
            row = session.get(Job, parent.id)
            assert row is not None
            runtime = dict(require_mapping(row.payload["runtime"], "runtime"))
            assert owner._authority is not None
            recipe, _ = owner._authority(revision.id)
        assert composition.service._builder is not None
        return composition.service._builder(
            recipe,
            runtime,
            claim=claim,
            build_input_sha256=plan.build_input_sha256,
            force=False,
            progress=lambda _value: None,
        )

    def forbidden_poll(_seconds):
        raise AssertionError("a parent cannot retain a worker while its child runs")

    # Isolate only the old polling seam; do not replace global time.sleep.
    monkeypatch.setattr(
        availability_production,
        "time",
        SimpleNamespace(sleep=forbidden_poll),
        raising=False,
    )
    with pytest.raises(RecipeImageAvailabilityError) as waiting:
        observe(production())
    assert waiting.value.code == "recipe_image.build_wait"
    with sessions.begin() as session:
        jobs = tuple(session.scalars(select(Job).where(Job.kind == "recipe.build.v1")))
        assert len(jobs) == 1
        child_id = jobs[0].id
        parent_row = session.get(Job, parent.id)
        assert parent_row is not None
        dependency = dict(
            require_mapping(parent_row.payload["build_dependency"], "build dependency")
        )
        # The request is durable before dispatch; the child ID checkpoint can
        # be lost after the lifecycle service commits the actual child.
        dependency.pop("operation_id", None)
        parent_row.payload = dict(parent_row.payload) | {
            "build_dependency": dependency,
            "claim_until": (now - timedelta(seconds=1)).isoformat(),
        }
    claim = owner.claim_pending(limit=1)[0]

    def forbidden_replan(*_args, **_kwargs):
        raise AssertionError("accepted build must be recovered before mutable planning")

    monkeypatch.setattr(builds, "resolve", forbidden_replan)
    monkeypatch.setattr(builds, "prepare_plan", forbidden_replan)
    with pytest.raises(RecipeImageAvailabilityError) as resumed:
        observe(production())
    assert resumed.value.code == "recipe_image.build_wait"
    operations.record_node_result(
        child_id, node, succeeded=True, evidence=_evidence(plan)
    )
    result = observe(production())
    assert result["build_id"] == plan.build_id
    assert result["build_input_sha256"] == plan.build_input_sha256
    with sessions() as session:
        assert tuple(
            session.scalars(select(Job.id).where(Job.kind == "recipe.build.v1"))
        ) == (child_id,)


@pytest.mark.parametrize("inventory", ["fresh", "saturated", "stale"])
def test_one_availability_slot_serves_two_parents_sharing_one_real_build(
    tmp_path, postgres_engine, inventory
):
    sessions, builds, operations, _storage, now, node, revision, plan = _services(
        tmp_path, postgres_engine
    )
    clock = [now]
    production = build_recipe_image_availability(
        sessions,
        artifact_root=tmp_path / "images",
        managed_catalog_sync=None,
        recipe_builds=builds,
        recipe_operations=operations,
        clock=lambda: clock[0],
        max_parallel=1,
        max_parallel_builds=1,
        with_scheduler=True,
    )
    scheduler = production.scheduler
    assert scheduler is not None
    try:
        parents = [
            production.service.start(
                revision.id, actor="operator", request_id=str(uuid.uuid4())
            )
            for _ in range(2)
        ]
        for index, _ in enumerate(parents):
            assert scheduler.tick() == 1
            for future in tuple(scheduler._futures):
                future.result(timeout=5)
            if index == 0 and inventory != "fresh":
                # The second parent has not selected a builder yet. Joining
                # an already accepted exact execution needs no new capacity
                # and must not be blocked by a subsequent inventory change.
                with sessions.begin() as session:
                    snapshot = session.scalar(select(NodeInventorySnapshot))
                    assert snapshot is not None
                    if inventory == "saturated":
                        snapshot.host_memory_free_bytes = 0
                        snapshot.gpu_memory_free_bytes = 0
                        snapshot.disk_free_bytes = 0
                    else:
                        snapshot.observed_at -= timedelta(hours=1)
        for parent in parents:
            waiting = production.service.get(parent.id)
            assert waiting.state == "queued", waiting.failure
            assert waiting.failure is not None
            assert waiting.failure["code"] == "recipe_image.build_wait"
        with sessions() as session:
            children = tuple(
                session.scalars(select(Job).where(Job.kind == "recipe.build.v1"))
            )
            assert len(children) == 1
            child_id = children[0].id
            for parent in parents:
                row = session.get(Job, parent.id)
                assert row is not None
                dependency = require_mapping(
                    row.payload["build_dependency"], "dependency"
                )
                assert dependency["operation_id"] == child_id
                assert row.payload["claim_owner"] is None
                retry = require_mapping(row.payload["retry"], "retry")
                assert retry["automatic_attempts"] == 0
        receipt = _write_controller_build_receipt(
            production.storage,
            archive=b"shared verified build archive",
            image_digest="sha256:" + "b" * 64,
            build_id=plan.build_id,
            build_input_sha256=plan.build_input_sha256,
            distribution_content_sha256=revision.content_digest,
        )
        operations.record_node_result(
            child_id,
            node,
            succeeded=True,
            evidence=_evidence(plan)
            | {
                "image_bytes": receipt.image_bytes,
                "oci_layout_sha256": receipt.oci_archive_sha256,
            },
        )
        clock[0] += timedelta(seconds=6)
        for _ in parents:
            assert scheduler.tick() == 1
            for future in tuple(scheduler._futures):
                future.result(timeout=5)
        for parent in parents:
            completed = production.service.get(parent.id)
            assert completed.state == "succeeded", completed.failure
        assert (
            production.storage.root / receipt.oci_archive_sha256
        ).read_bytes() == b"shared verified build archive"
    finally:
        production.close()


@pytest.mark.parametrize("invalid", ["builder_identity", "recipe_content"])
def test_unbound_adoption_refuses_incomplete_or_changed_execution_identity(
    tmp_path, postgres_engine, invalid
):
    sessions, builds, operations, _storage, now, _node, revision, plan = _services(
        tmp_path, postgres_engine
    )
    child = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="operator",
        request_id=str(uuid.uuid4()),
    )
    production = build_recipe_image_availability(
        sessions,
        artifact_root=tmp_path / "images",
        managed_catalog_sync=None,
        recipe_builds=builds,
        recipe_operations=operations,
        clock=lambda: now,
    )
    parent = production.service.start(
        revision.id, actor="operator", request_id=str(uuid.uuid4())
    )
    claims = _active_claims(sessions, plan.build_id)
    with sessions.begin() as session:
        build = session.get(RecipeBuild, plan.build_id)
        assert build is not None
        if invalid == "builder_identity":
            build.policy_report = dict(build.policy_report) | {
                "builder_binary_digest": None
            }
        else:
            build.plan = dict(build.plan) | {"recipe_content_sha256": "d" * 64}
    assert production.service.run_pending(limit=1) == 1
    refused = production.service.get(parent.id)
    assert refused.state == "failed"
    assert refused.failure is not None
    assert refused.failure["code"] == "recipe_image.build_invalid"
    assert operations.get(child.id).state == "running"
    assert _active_claims(sessions, plan.build_id) == claims
    with sessions() as session:
        assert tuple(
            session.scalars(select(Job.id).where(Job.kind == "recipe.build.v1"))
        ) == (child.id,)


@pytest.mark.parametrize("outcome", ["failed", "missing_archive"])
def test_settled_child_failure_gets_new_execution_identity_on_recovery(
    tmp_path, postgres_engine, outcome
):
    sessions, builds, operations, _storage, now, node, revision, plan = _services(
        tmp_path, postgres_engine
    )
    clock = [now]
    production = build_recipe_image_availability(
        sessions,
        artifact_root=tmp_path / "images",
        managed_catalog_sync=None,
        recipe_builds=builds,
        recipe_operations=operations,
        clock=lambda: clock[0],
    )
    parent = production.service.start(
        revision.id, actor="operator", request_id=str(uuid.uuid4())
    )
    assert production.service.run_pending(limit=1) == 1
    with sessions() as session:
        original = session.scalar(select(Job).where(Job.kind == "recipe.build.v1"))
        assert original is not None
        original_id, original_key = original.id, original.request_id
    operations.record_node_result(
        original_id,
        node,
        succeeded=outcome == "missing_archive",
        evidence=_evidence(plan)
        if outcome == "missing_archive"
        else {"reason": "build executor failed"},
    )
    clock[0] += timedelta(seconds=6)
    assert production.service.run_pending(limit=1) == 1
    waiting = production.service.get(parent.id)
    assert waiting.state == "queued", waiting.failure
    assert waiting.failure is not None
    assert waiting.failure["code"] == (
        "recipe_image.build_failed"
        if outcome == "failed"
        else "runtime_image.cache_missing"
    )
    clock[0] += timedelta(seconds=6)
    assert production.service.run_pending(limit=1) == 1
    with sessions() as session:
        children = tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.build.v1"))
        )
        assert len(children) == 2, production.service.get(parent.id).failure
        replacement = next(child for child in children if child.id != original_id)
        assert replacement.request_id != original_key
        replacement_id = replacement.id
    receipt = _write_controller_build_receipt(
        production.storage,
        archive=b"verified replacement archive",
        image_digest="sha256:" + "b" * 64,
        build_id=plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        distribution_content_sha256=revision.content_digest,
    )
    operations.record_node_result(
        replacement_id,
        node,
        succeeded=True,
        evidence=_evidence(plan)
        | {
            "image_bytes": receipt.image_bytes,
            "oci_layout_sha256": receipt.oci_archive_sha256,
        },
    )
    clock[0] += timedelta(seconds=6)
    assert production.service.run_pending(limit=1) == 1
    completed = production.service.get(parent.id)
    assert completed.state == "succeeded", completed.failure


def test_new_availability_intent_after_removal_joins_the_accepted_build(
    tmp_path, postgres_engine
):
    sessions, builds, operations, _storage, now, _node, revision, plan = _services(
        tmp_path, postgres_engine
    )
    production = build_recipe_image_availability(
        sessions,
        artifact_root=tmp_path / "images",
        managed_catalog_sync=None,
        recipe_builds=builds,
        recipe_operations=operations,
        clock=lambda: now,
    )
    original = production.service.start(
        revision.id, actor="operator", request_id=str(uuid.uuid4())
    )
    assert production.service.run_pending(limit=1) == 1
    with sessions() as session:
        child = session.scalar(select(Job).where(Job.kind == "recipe.build.v1"))
        assert child is not None
        child_id = child.id
        child_payload = dict(child.payload)
        child_state = child.state
        child_result = child.result
    removal = remove_after_review(
        production.service, revision.id, actor="operator", request_id=str(uuid.uuid4())
    )
    assert removal["cancelled_builds"] == []
    assert not operations.reconcile_cancelled_builds()
    assert operations.get(child_id).state == child_state == "running"
    assert production.service.get(original.id).state == "queued"
    claims = _active_claims(sessions, plan.build_id)
    assert claims

    fresh = production.service.start(
        revision.id, actor="operator", request_id=str(uuid.uuid4())
    )
    assert production.service.run_pending(limit=2) >= 1
    for parent_id in (original.id, fresh.id):
        waiting = production.service.get(parent_id)
        assert waiting.state == "queued", waiting.failure
        assert waiting.failure is not None
        assert waiting.failure["code"] == "recipe_image.build_wait"
    assert operations.reconcile_cancelled_builds() is False
    assert operations.get(child_id).state == child_state == "running"
    with sessions() as session:
        children = tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.build.v1"))
        )
        assert len(children) == 1
        child = children[0]
        assert child.id == child_id
        assert child.state == child_state
        assert child.result == child_result
        assert child.payload == child_payload
        for parent_id in (original.id, fresh.id):
            parent = session.get(Job, parent_id)
            assert parent is not None
            dependency = require_mapping(
                parent.payload["build_dependency"], "build dependency"
            )
            assert dependency["operation_id"] == child_id
    assert _active_claims(sessions, plan.build_id) == claims


def test_unbound_consumer_reuses_verified_image_while_replacement_build_runs(
    tmp_path, postgres_engine
):
    sessions, builds, operations, storage, now, node, revision, plan = _services(
        tmp_path, postgres_engine
    )
    original = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="operator",
        request_id=str(uuid.uuid4()),
    )
    receipt = _write_controller_build_receipt(
        storage,
        archive=b"usable prior build",
        image_digest="sha256:" + "b" * 64,
        build_id=plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        distribution_content_sha256=revision.content_digest,
    )
    operations.record_node_result(
        original.id,
        node,
        succeeded=True,
        evidence=_evidence(plan)
        | {
            "image_bytes": receipt.image_bytes,
            "oci_layout_sha256": receipt.oci_archive_sha256,
        },
    )
    replacement = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="operator",
        request_id=str(uuid.uuid4()),
        force=True,
    )
    production = build_recipe_image_availability(
        sessions,
        artifact_root=tmp_path / "images",
        managed_catalog_sync=None,
        recipe_builds=builds,
        recipe_operations=operations,
        clock=lambda: now,
    )
    parent = production.service.start(
        revision.id, actor="operator", request_id=str(uuid.uuid4())
    )
    assert production.service.run_pending(limit=1) == 1
    completed = production.service.get(parent.id)
    assert completed.state == "succeeded", completed.failure
    assert completed.result is not None
    assert completed.result["image_digest"] == receipt.image_digest
    assert operations.get(replacement.id).state == "running"


def test_expired_observers_failure_cannot_erase_the_new_claims_build_dependency(
    tmp_path, postgres_engine, monkeypatch
):
    sessions, builds, operations, _storage, now, _node, revision, _plan = _services(
        tmp_path, postgres_engine
    )
    production = build_recipe_image_availability(
        sessions,
        artifact_root=tmp_path / "images",
        managed_catalog_sync=None,
        recipe_builds=builds,
        recipe_operations=operations,
        clock=lambda: now,
    )
    service = production.service
    parent = service.start(revision.id, actor="operator", request_id=str(uuid.uuid4()))
    original = service.claim_pending(limit=1, owner_id="old-worker")[0]
    build = service._builder
    assert build is not None
    inherited = []

    def fail_after_claim_replacement(*args, **kwargs):
        with pytest.raises(RecipeImageAvailabilityError) as waiting:
            build(*args, **kwargs)
        assert waiting.value.code == "recipe_image.build_wait"
        with sessions.begin() as session:
            row = session.get(Job, parent.id)
            assert row is not None
            inherited.append(row.payload["build_dependency"])
            row.payload = dict(row.payload) | {
                "claim_until": (now - timedelta(seconds=1)).isoformat()
            }
        assert service.claim_pending(limit=1, owner_id="new-worker")
        # An expired lease did not stop this executor. Its delayed failure
        # must not reset the newer claim or discard its exact child reference.
        raise RecipeImageAvailabilityError(
            "recipe_image.build_failed", "old executor failed", retryable=True
        )

    monkeypatch.setattr(service, "_builder", fail_after_claim_replacement)
    service.run_claim(original)
    with sessions() as session:
        row = session.get(Job, parent.id)
        assert row is not None
        assert row.state == "running"
        assert row.payload["claim_owner"] == "new-worker"
        assert row.payload["build_dependency"] == inherited[0]
        assert require_mapping(row.payload["retry"], "retry")["automatic_attempts"] == 0
