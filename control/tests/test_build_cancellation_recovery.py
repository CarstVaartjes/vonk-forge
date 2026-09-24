"""Cancellation belongs to an issued build attempt, never its canonical input."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select
from vonk_control.install_admission import InstallAdmissionService
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    Job,
    RecipeBuild,
    ResourceReservation,
)
from vonk_control.recipe_builds import RecipeBuildService
from vonk_control.recipe_execution_contract import parse_stored_build_plan
from vonk_control.recipe_image_availability import (
    RecipeImageAvailabilityService,
)
from vonk_control.recipe_operations import (
    RecipeOperationConflict,
    RecipeOperationService,
)
from vonk_control.run_admission import RunAdmissionService
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage
from vonk_forge_contracts import RecipeDefinition

from .recipe_removal_review_support import remove_after_review
from .test_recipe_builds import RecordingQueue, _write_controller_build_receipt, setup


def _services(tmp_path, engine):
    sessions, bundles, now, node_id, revision = setup(tmp_path, engine=engine)
    storage = FilesystemRuntimeImageStorage(tmp_path / "images")
    builds = RecipeBuildService(
        sessions,
        bundles=bundles,
        build_archive_available=storage.build_archive_available,
        prepared_builds=storage.find_build,
    )
    operations = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(sessions),
        run_admission=RunAdmissionService(sessions),
        agent_jobs=RecordingQueue(),
        clock=lambda: now,
        builds=builds,
    )
    with sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        node.capabilities = [*node.capabilities, "recipe.build.cleanup.v1"]
    plan = builds.plan(revision.id, node_id, now=now)
    return sessions, builds, operations, storage, now, node_id, revision, plan


def _issue(sessions, operation_id):
    with sessions.begin() as session:
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == operation_id)
        )
        assert child is not None
        child.current_attempt = 1
        child.state = "running"
        return child.id


def _settle_cleanup(sessions, operations, build_id, node_id, child_id):
    with sessions() as session:
        cleanup = session.scalar(
            select(Job).where(Job.kind == "recipe.build.cleanup.v1")
        )
        assert cleanup is not None
        cleanup_id = cleanup.id
    evidence = {
        "schema_version": 1,
        "build_id": build_id,
        "operation_id": child_id,
        "stopped": True,
    }
    operations.record_node_result(
        cleanup_id, node_id, succeeded=True, evidence=evidence
    )
    return cleanup_id, evidence


def _active_claims(sessions, build_id):
    with sessions() as session:
        return set(
            session.scalars(
                select(ResourceReservation.id).where(
                    ResourceReservation.owner_kind == "recipe-build",
                    ResourceReservation.owner_id == build_id,
                    ResourceReservation.state == "active",
                )
            )
        )


def _evidence(plan):
    return {
        "build_input_sha256": plan.build_input_sha256,
        "image_bytes": 500,
        "image_digest": "sha256:" + "b" * 64,
        "oci_layout_sha256": "c" * 64,
        "policy": {"dockerfile": "Dockerfile", "findings": [], "passed": True},
    }


@pytest.mark.parametrize("issued", [False, True])
def test_explicit_cancellation_does_not_corrupt_the_canonical_build_request(
    tmp_path, postgres_engine, issued
):
    sessions, _builds, operations, _storage, _now, _node_id, _revision, plan = (
        _services(tmp_path, postgres_engine)
    )
    original = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="operator",
        request_id=str(uuid.uuid4()),
    )
    if issued:
        _issue(sessions, original.id)
    operations.cancel(
        original.id,
        actor="operator",
        request_id=str(uuid.uuid4()),
        reason="cancel this preparation",
    )
    with sessions() as session:
        build = session.get(RecipeBuild, plan.build_id)
        job = session.get(Job, original.id)
        assert build is not None and job is not None
        assert parse_stored_build_plan(build.plan) == parse_stored_build_plan(
            plan.agent_payload
        )
        assert job.result is not None and job.result["cancel_requested"] is True
    assert bool(_active_claims(sessions, plan.build_id)) is issued


@pytest.mark.parametrize("issued", [False, True])
def test_cache_removal_preserves_an_accepted_build_and_its_claim(
    tmp_path, postgres_engine, issued
):
    sessions, _builds, operations, storage, now, _node_id, revision, plan = _services(
        tmp_path, postgres_engine
    )
    original = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="operator",
        request_id=str(uuid.uuid4()),
    )
    if issued:
        _issue(sessions, original.id)
    with sessions() as session:
        before_build = session.get(RecipeBuild, plan.build_id)
        before_job = session.get(Job, original.id)
        before_child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == original.id)
        )
        assert before_build is not None and before_job is not None
        assert before_child is not None
        child_id = before_child.id
        child_state = before_child.state
        child_attempt = before_child.current_attempt
        job_payload = dict(before_job.payload)
        assert before_build.state == "building"
        assert before_job.result is None
        assert child_state == ("running" if issued else "queued")
    claims = _active_claims(sessions, plan.build_id)
    assert claims

    recipe = RecipeDefinition.model_validate_json(json.dumps(revision.document))
    availability = RecipeImageAvailabilityService(
        sessions,
        storage=storage,
        authority=lambda *_args, **_kwargs: (recipe, {}),
        clock=lambda: now,
    )
    removal = remove_after_review(
        availability,
        recipe.identity.slug,
        actor="operator",
        request_id=str(uuid.uuid4()),
    )

    assert removal["action"] == "remove"
    assert removal["cancelled_builds"] == []
    assert not operations.reconcile_cancelled_builds()
    with sessions() as session:
        build = session.get(RecipeBuild, plan.build_id)
        job = session.get(Job, original.id)
        child = session.get(AgentOperation, child_id)
        build_jobs = tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.build.v1"))
        )
        assert build is not None and job is not None and child is not None
        assert build.state == "building"
        assert parse_stored_build_plan(build.plan) == parse_stored_build_plan(
            plan.agent_payload
        )
        assert job.state == original.state == "running"
        assert job.payload == job_payload
        assert job.result is None
        assert child.id == child_id
        assert child.state == child_state
        assert child.current_attempt == child_attempt
        assert tuple(candidate.id for candidate in build_jobs) == (original.id,)
    assert _active_claims(sessions, plan.build_id) == claims


@pytest.mark.parametrize("issued", [False, True])
def test_new_intent_after_cancellation_cannot_revive_or_release_the_old_attempt(
    tmp_path, postgres_engine, issued
):
    sessions, builds, operations, _storage, now, node_id, revision, plan = _services(
        tmp_path, postgres_engine
    )
    original_key = str(uuid.uuid4())
    original = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="operator",
        request_id=original_key,
    )
    child_id = _issue(sessions, original.id) if issued else None
    operations.cancel(
        original.id,
        actor="operator",
        request_id=str(uuid.uuid4()),
        reason="cancel this preparation",
    )
    cleanup = None
    if child_id is not None:
        claims = _active_claims(sessions, plan.build_id)
        with pytest.raises(RecipeOperationConflict, match="cancel|cleanup"):
            operations.build(
                plan,
                build_input_sha256=plan.build_input_sha256,
                actor="operator",
                request_id=str(uuid.uuid4()),
            )
        with pytest.raises(RecipeOperationConflict, match="cancel|retryable"):
            operations.retry(
                original.id, actor="operator", request_id=str(uuid.uuid4())
            )
        assert _active_claims(sessions, plan.build_id) == claims
        cleanup = _settle_cleanup(
            sessions, operations, plan.build_id, node_id, child_id
        )
    # Reconstruct from the persisted canonical request, not the old Python object.
    restored = builds.plan(revision.id, node_id, now=now)
    fresh = operations.build(
        restored,
        build_input_sha256=restored.build_input_sha256,
        actor="operator",
        request_id=str(uuid.uuid4()),
    )
    assert fresh.id != original.id and fresh.owner_id == original.owner_id
    claims = _active_claims(sessions, plan.build_id)
    assert claims
    # A delayed original result and duplicate cleanup must not touch the new use.
    operations.record_node_result(
        original.id, node_id, succeeded=True, evidence=_evidence(plan)
    )
    if cleanup is not None:
        operations.record_node_result(
            cleanup[0], node_id, succeeded=True, evidence=cleanup[1]
        )
    with sessions() as session:
        build = session.get(RecipeBuild, plan.build_id)
        assert build is not None and build.state == "building"
    assert _active_claims(sessions, plan.build_id) == claims
    assert operations.get(original.id).state == "cancelled"
    operations.record_node_result(
        fresh.id, node_id, succeeded=True, evidence=_evidence(plan)
    )
    replay = operations.build(
        restored,
        build_input_sha256=restored.build_input_sha256,
        actor="operator",
        request_id=original_key,
    )
    assert replay.id == original.id and replay.state == "cancelled"


@pytest.mark.parametrize("issued", [False, True])
def test_cancelled_replacement_preserves_the_verified_image(
    tmp_path, postgres_engine, issued
):
    sessions, _builds, operations, storage, _now, node_id, revision, plan = _services(
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
        archive=b"verified archive",
        image_digest="sha256:" + "b" * 64,
        build_id=plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        distribution_content_sha256=revision.content_digest,
    )
    evidence = _evidence(plan) | {
        "image_bytes": receipt.image_bytes,
        "oci_layout_sha256": receipt.oci_archive_sha256,
    }
    operations.record_node_result(
        original.id, node_id, succeeded=True, evidence=evidence
    )
    replacement_key = str(uuid.uuid4())
    replacement = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="operator",
        request_id=replacement_key,
        force=True,
    )
    child_id = _issue(sessions, replacement.id) if issued else None
    operations.cancel(
        replacement.id,
        actor="operator",
        request_id=str(uuid.uuid4()),
        reason="keep the verified image",
    )
    if child_id is not None:
        operations.record_node_result(
            replacement.id,
            node_id,
            succeeded=True,
            evidence=_evidence(plan) | {"image_digest": "sha256:" + "d" * 64},
        )
        claims = _active_claims(sessions, plan.build_id)
        assert claims
        reused = operations.build(
            plan,
            build_input_sha256=plan.build_input_sha256,
            actor="operator",
            request_id=str(uuid.uuid4()),
        )
        assert reused.state == "succeeded"
        assert reused.result == operations.get(original.id).result
        assert _active_claims(sessions, plan.build_id) == claims
        _settle_cleanup(sessions, operations, plan.build_id, node_id, child_id)
    with sessions() as session:
        build = session.get(RecipeBuild, plan.build_id)
        assert build is not None and build.state == "succeeded"
        assert build.oci_layout_sha256 == receipt.oci_archive_sha256
    assert storage.build_archive_available(
        receipt.oci_archive_sha256, receipt.image_bytes
    )
    replay = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="operator",
        request_id=replacement_key,
    )
    assert replay.id == replacement.id and replay.state == "cancelled"


def test_removal_of_an_unissued_plan_does_not_invent_a_failed_attempt(
    tmp_path, postgres_engine
):
    sessions, builds, operations, storage, now, node_id, revision, plan = _services(
        tmp_path, postgres_engine
    )
    recipe = RecipeDefinition.model_validate_json(json.dumps(revision.document))
    availability = RecipeImageAvailabilityService(
        sessions,
        storage=storage,
        authority=lambda *_args, **_kwargs: (recipe, {}),
        clock=lambda: now,
    )
    remove_after_review(
        availability,
        recipe.identity.slug,
        actor="operator",
        request_id=str(uuid.uuid4()),
    )
    assert not operations.reconcile_cancelled_builds()
    with sessions() as session:
        assert session.scalar(select(Job).where(Job.kind == "recipe.build.v1")) is None
    restored = builds.plan(revision.id, node_id, now=now)
    fresh = operations.build(
        restored,
        build_input_sha256=restored.build_input_sha256,
        actor="operator",
        request_id=str(uuid.uuid4()),
    )
    assert fresh.owner_id == plan.build_id and fresh.state == "running"


@pytest.mark.parametrize("locked_owner", ["job", "build"])
def test_removal_does_not_lock_or_mutate_an_accepted_build_owner(
    tmp_path, postgres_engine, locked_owner
):
    sessions, _builds, operations, storage, now, _node_id, revision, plan = _services(
        tmp_path, postgres_engine
    )
    original = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="operator",
        request_id=str(uuid.uuid4()),
    )
    recipe = RecipeDefinition.model_validate_json(json.dumps(revision.document))
    availability = RecipeImageAvailabilityService(
        sessions,
        storage=storage,
        authority=lambda *_args, **_kwargs: (recipe, {}),
        clock=lambda: now,
    )
    claims = _active_claims(sessions, plan.build_id)
    with sessions() as session:
        child_before = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == original.id)
        )
        job_before = session.get(Job, original.id)
        build_before = session.get(RecipeBuild, plan.build_id)
        assert child_before is not None and job_before is not None
        assert build_before is not None
        child_id = child_before.id
        child_state = child_before.state
        child_attempt = child_before.current_attempt
        job_payload = dict(job_before.payload)
        build_plan = parse_stored_build_plan(build_before.plan)
    with sessions.begin() as blocker:
        statement = (
            select(Job).where(Job.id == original.id)
            if locked_owner == "job"
            else select(RecipeBuild).where(RecipeBuild.id == plan.build_id)
        )
        blocker.scalar(statement.with_for_update())
        removal = remove_after_review(
            availability,
            recipe.identity.slug,
            actor="operator",
            request_id=str(uuid.uuid4()),
        )
        assert removal["action"] == "remove"
        assert removal["cancelled_builds"] == []
        # Cache removal has no build-cancellation ownership to acquire. It
        # accepts its removal intent while the unrelated build rows stay locked.
    with sessions() as session:
        build = session.get(RecipeBuild, plan.build_id)
        job = session.get(Job, original.id)
        child = session.get(AgentOperation, child_id)
        build_jobs = tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.build.v1"))
        )
        assert build is not None and build.state == "building"
        assert parse_stored_build_plan(build.plan) == build_plan
        assert job is not None and job.state == "running" and job.result is None
        assert job.payload == job_payload
        assert child is not None and child.id == child_id
        assert child.state == child_state
        assert child.current_attempt == child_attempt
        assert tuple(candidate.id for candidate in build_jobs) == (original.id,)
    assert _active_claims(sessions, plan.build_id) == claims
    assert not operations.reconcile_cancelled_builds()
