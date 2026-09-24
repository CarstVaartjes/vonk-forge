"""Accepted preparation consumers must participate in build cancellation."""

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
import vonk_control.recipe_image_availability as availability_module
from sqlalchemy import select
from vonk_control import fleet_profiles as profile_module
from vonk_control.models import (
    AgentNode,
    FleetProfileApplication,
    Job,
    NodeInventorySnapshot,
    RecipeBuild,
    User,
)
from vonk_control.recipe_availability_intent import RecipeBuildDependency
from vonk_control.recipe_build_cancellation import (
    BuildConsumerError,
    lock_build_dependency,
)
from vonk_control.recipe_image_availability import (
    RecipeImageAvailabilityError,
    RecipeImageAvailabilityService,
)
from vonk_control.recipe_operations import RecipeOperationConflict
from vonk_forge_contracts import RecipeDefinition

from .test_build_cancellation_recovery import _active_claims, _issue, _services
from .test_profile_build_memory import _accepted_build_profile, _parent_build_request
from .test_profile_port_claims import _ready_profile


def _availability(sessions, storage, now, revision, plan):
    recipe = RecipeDefinition.model_validate_json(json.dumps(revision.document))

    def failed_builder(*_args, **_kwargs):
        raise RecipeImageAvailabilityError("build.invalid_source", "source rejected")

    return RecipeImageAvailabilityService(
        sessions,
        storage=storage,
        authority=lambda *_args, **_kwargs: (
            recipe,
            {
                "recipe_revision_id": revision.id,
                "build_input_sha256": plan.build_input_sha256,
                "builder_node_id": plan.builder_node_id,
            },
        ),
        builder=failed_builder,
        clock=lambda: now,
    )


def test_build_cancellation_preserves_each_accepted_availability_consumer(
    tmp_path, postgres_engine
):
    sessions, _builds, operations, storage, now, _node, revision, plan = _services(
        tmp_path, postgres_engine
    )
    build = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="operator",
        request_id=str(uuid.uuid4()),
    )
    availability = _availability(sessions, storage, now, revision, plan)
    parents = [
        availability.start(revision.id, actor="operator", request_id=str(uuid.uuid4()))
        for _ in range(2)
    ]
    claims = _active_claims(sessions, plan.build_id)
    for remaining in (2, 1):
        with pytest.raises(RecipeOperationConflict, match="build.shared_consumers"):
            operations.cancel(
                build.id,
                actor="operator",
                request_id=str(uuid.uuid4()),
                reason="cancel the underlying build",
            )
        assert operations.get(build.id).state == "running"
        assert _active_claims(sessions, plan.build_id) == claims
        assert (
            sum(availability.get(parent.id).state == "queued" for parent in parents)
            == remaining
        )
        # The real owner settles just one parent on a terminal executor failure.
        assert availability.run_pending(limit=1) == 1
    assert all(availability.get(parent.id).state == "failed" for parent in parents)
    cancelled = operations.cancel(
        build.id,
        actor="operator",
        request_id=str(uuid.uuid4()),
        reason="no remaining preparation consumer",
    )
    assert cancelled.state == "cancelled"


def test_recipe_parent_cancellation_detaches_one_shared_build_consumer(
    tmp_path, postgres_engine
):
    sessions, _builds, operations, storage, now, _node, revision, plan = _services(
        tmp_path, postgres_engine
    )
    with sessions.begin() as session:
        session.add(User(subject="operator", role="operator"))
    availability = _availability(sessions, storage, now, revision, plan)
    first, second = [
        availability.start(revision.id, actor="operator", request_id=str(uuid.uuid4()))
        for _ in range(2)
    ]
    claim = availability.claim_pending(limit=1, owner_id="cancel-shared-build")[0]
    claimed_parent, other_parent = next(
        (parent, other)
        for parent, other in ((first, second), (second, first))
        if parent.id == claim.operation_id
    )
    child_key = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"vonk:recipe-image-build:{claimed_parent.id}:{claim.execution_attempt}",
        )
    )
    build = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="recipe-image-availability",
        request_id=child_key,
    )
    first_dependency = RecipeBuildDependency(
        request_key=uuid.UUID(child_key), operation_id=uuid.UUID(build.id)
    )
    second_dependency = RecipeBuildDependency(
        request_key=uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"vonk:recipe-image-build:{other_parent.id}:1",
        ),
        operation_id=uuid.UUID(build.id),
    )
    with sessions.begin() as session:
        for operation_id, dependency in (
            (claimed_parent.id, first_dependency),
            (other_parent.id, second_dependency),
        ):
            parent = session.get(Job, operation_id)
            assert parent is not None
            parent.payload = dict(parent.payload) | {
                "build_dependency": dependency.model_dump(
                    mode="json", exclude_none=True
                )
            }

    availability.cancel(
        claimed_parent.id,
        actor="operator",
        request_id=str(uuid.uuid4()),
        reason="stop first image preparation",
    )
    availability._release_cancelled_claim(claim)
    availability.reconcile_cancellations()
    assert availability.get(claimed_parent.id).state == "cancelled"
    assert availability.get(other_parent.id).state == "queued"
    with sessions() as session:
        job = session.get(Job, build.id)
        assert job is not None and job.result is None

    # The second accepted parent binds the exact shared child by its operation
    # ID even though the build was originally admitted with the first key.
    availability.cancel(
        other_parent.id,
        actor="operator",
        request_id=str(uuid.uuid4()),
        reason="stop final image preparation",
    )
    availability.reconcile_cancellations()
    assert availability.get(other_parent.id).state == "cancelling"
    with sessions() as session:
        job = session.get(Job, build.id)
        assert job is not None and job.result is not None
        assert job.result["cancel_requested"] is True
        assert job.result["cancel_actor"] == "operator"
    assert operations.reconcile_cancelled_builds()
    availability.reconcile_cancellations()
    assert availability.get(other_parent.id).state == "cancelled"
    assert operations.get(build.id).state == "cancelled"


@pytest.mark.parametrize("checkpoint", ["saved", "lost"])
def test_accepted_profile_consumer_protects_build_until_its_intent_is_cancelled(
    tmp_path, postgres_engine, checkpoint
):
    sessions, profiles, planner, _node, _application, plan = _accepted_build_profile(
        tmp_path, postgres_engine
    )
    parent_id, request_id = _parent_build_request(sessions, profiles)
    if checkpoint == "lost":
        with sessions.begin() as session:
            application = session.get(FleetProfileApplication, _application)
            assert application is not None
            progress = dict(application.progress)
            adapter = progress["switch_adapter"]
            assert isinstance(adapter, dict)
            progress["switch_adapter"] = {
                **adapter,
                "active_operation_id": None,
                "active_kind": None,
            }
            application.progress = progress
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    build = lifecycle.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="operator",
        request_id=request_id,
    )
    with pytest.raises(RecipeOperationConflict, match="build.shared_consumers"):
        lifecycle.cancel(
            build.id,
            actor="operator",
            request_id=str(uuid.uuid4()),
            reason="cancel the underlying build",
        )
    planner.cancel(
        parent_id,
        actor="operator",
        request_key=str(uuid.uuid4()),
        reason="cancel the parent intent",
    )
    assert (
        lifecycle.cancel(
            build.id,
            actor="operator",
            request_id=str(uuid.uuid4()),
            reason="cancel after parent detachment",
        ).state
        == "cancelled"
    )


@pytest.mark.parametrize("change", ["none", "superseded", "invalid-progress"])
def test_profile_protects_its_build_before_the_execution_child_exists(
    tmp_path, postgres_engine, change
):
    sessions, _profiles, planner, node_id, application_id, plan = (
        _accepted_build_profile(tmp_path, postgres_engine, build_memory_bytes=10)
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    # This independently requested producer has its own capacity. Its safety
    # must not depend on the profile's optional memory-inheritance path.
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.host_memory_free_bytes = snapshot.gpu_memory_free_bytes = 1_000
    build = lifecycle.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="operator",
        request_id=str(uuid.uuid4()),
    )
    with sessions.begin() as session:
        assert (
            session.scalar(select(Job.id).where(Job.kind == "recipe.run-switch.v2"))
            is None
        )
        application = session.get(FleetProfileApplication, application_id)
        node = session.get(AgentNode, node_id)
        assert application is not None and node is not None
        if change == "superseded":
            node.workload_intent_ordinal += 1
        elif change == "invalid-progress":
            application.progress = {"completed_steps": -1}
    claims = _active_claims(sessions, plan.build_id)
    cancel_args = {
        "actor": "operator",
        "request_id": str(uuid.uuid4()),
        "reason": "cancel shared build",
    }
    if change == "superseded":
        assert lifecycle.cancel(build.id, **cancel_args).state == "cancelled"
    else:
        code = (
            "build.consumer_invalid"
            if change == "invalid-progress"
            else "build.shared_consumers"
        )
        with pytest.raises(RecipeOperationConflict, match=code):
            lifecycle.cancel(build.id, **cancel_args)
        assert lifecycle.get(build.id).state == "running"
        assert _active_claims(sessions, plan.build_id) == claims


def test_profile_acceptance_joins_the_build_cancellation_boundary(
    tmp_path, postgres_engine
):
    sessions, _profiles, _planner, profile, api, headers, _nodes, _ = _ready_profile(
        tmp_path, postgres_engine
    )
    review = api.post(f"/api/profile/{profile.number}/preview", headers=headers).json()
    assert review["allowed"], review
    request_key = str(uuid.uuid4())
    body = {"plan_digest": review["plan_digest"], "request_key": request_key}
    with sessions.begin() as blocker:
        blocker.scalar(select(RecipeBuild).with_for_update())
        response = api.post(
            f"/api/profile/{profile.number}/load", headers=headers, json=body
        )
        assert response.status_code == 409, response.text
        assert "build.consumer_busy" in response.text
    with sessions() as session:
        assert (
            session.scalar(
                select(FleetProfileApplication.id).where(
                    FleetProfileApplication.request_key == request_key
                )
            )
            is None
        )
    response = api.post(
        f"/api/profile/{profile.number}/load", headers=headers, json=body
    )
    assert response.status_code == 202, response.text


def test_profile_holds_build_dependency_until_its_acceptance_commits(
    tmp_path, postgres_engine, monkeypatch
):
    sessions, _profiles, _planner, profile, api, headers, _nodes, _ = _ready_profile(
        tmp_path, postgres_engine
    )
    review = api.post(f"/api/profile/{profile.number}/preview", headers=headers).json()
    assert review["allowed"], review
    with sessions() as session:
        build = session.scalar(select(RecipeBuild))
        assert build is not None
        identity = {
            "recipe_revision_id": build.recipe_revision_id,
            "builder_node_id": build.builder_node_id,
            "build_input_sha256": build.build_input_sha256,
            "build_id": build.id,
        }
    reached = threading.Event()
    resume = threading.Event()
    original = profile_module.reserve_profile_disk

    def hold_acceptance(*args, **kwargs):
        original(*args, **kwargs)
        reached.set()
        assert resume.wait(10), "profile acceptance barrier was not released"

    monkeypatch.setattr(profile_module, "reserve_profile_disk", hold_acceptance)
    with ThreadPoolExecutor(max_workers=1) as pool:
        acceptance = pool.submit(
            api.post,
            f"/api/profile/{profile.number}/load",
            headers=headers,
            json={
                "plan_digest": review["plan_digest"],
                "request_key": str(uuid.uuid4()),
            },
        )
        try:
            assert reached.wait(10), "profile acceptance did not reach the barrier"
            with sessions.begin() as session:
                with pytest.raises(BuildConsumerError) as caught:
                    lock_build_dependency(
                        session,
                        recipe_revision_id=identity["recipe_revision_id"],
                        builder_node_id=identity["builder_node_id"],
                        build_input_sha256=identity["build_input_sha256"],
                        build_id=identity["build_id"],
                    )
                assert caught.value.code == "build.consumer_busy"
        finally:
            resume.set()
        response = acceptance.result(timeout=10)
    assert response.status_code == 202, response.text
    with sessions.begin() as session:
        assert (
            lock_build_dependency(
                session,
                recipe_revision_id=identity["recipe_revision_id"],
                builder_node_id=identity["builder_node_id"],
                build_input_sha256=identity["build_input_sha256"],
                build_id=identity["build_id"],
            )
            is not None
        )


def test_availability_acceptance_cannot_pass_a_busy_build_cancellation_boundary(
    tmp_path, postgres_engine
):
    sessions, _builds, _operations, storage, now, _node, revision, plan = _services(
        tmp_path, postgres_engine
    )
    availability = _availability(sessions, storage, now, revision, plan)
    request_id = str(uuid.uuid4())
    with sessions.begin() as blocker:
        blocker.scalar(
            select(RecipeBuild).where(RecipeBuild.id == plan.build_id).with_for_update()
        )
        with pytest.raises(RecipeImageAvailabilityError) as caught:
            availability.start(revision.id, actor="operator", request_id=request_id)
        assert caught.value.code == "build.consumer_busy"
    with sessions() as session:
        assert (
            session.scalar(select(Job.id).where(Job.request_id == request_id)) is None
        )
    accepted = availability.start(revision.id, actor="operator", request_id=request_id)
    assert accepted.state == "queued"


def test_new_availability_consumer_committing_before_cancellation_keeps_shared_build(
    tmp_path, postgres_engine, monkeypatch
):
    sessions, _builds, operations, storage, now, _node, revision, plan = _services(
        tmp_path, postgres_engine
    )
    with sessions.begin() as session:
        session.add(User(subject="operator", role="operator"))
    availability = _availability(sessions, storage, now, revision, plan)
    first = availability.start(
        revision.id, actor="operator", request_id=str(uuid.uuid4())
    )
    claim = availability.claim_pending(limit=1, owner_id="cancel-race-first")[0]
    child_key = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"vonk:recipe-image-build:{first.id}:{claim.execution_attempt}",
        )
    )
    build = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="recipe-image-availability",
        request_id=child_key,
    )
    dependency = RecipeBuildDependency(
        request_key=uuid.UUID(child_key), operation_id=uuid.UUID(build.id)
    )
    with sessions.begin() as session:
        parent = session.get(Job, first.id)
        assert parent is not None
        parent.payload = dict(parent.payload) | {
            "build_dependency": dependency.model_dump(mode="json", exclude_none=True)
        }
    availability.cancel(
        first.id,
        actor="operator",
        request_id=str(uuid.uuid4()),
        reason="detach the first preparation",
    )
    availability._release_cancelled_claim(claim)

    locked = threading.Event()
    resume = threading.Event()
    original_lock = availability._lock_build_consumer

    def hold_accepted_consumer(session, payload):
        original_lock(session, payload)
        locked.set()
        assert resume.wait(10), "new consumer acceptance barrier was not released"

    monkeypatch.setattr(availability, "_lock_build_consumer", hold_accepted_consumer)
    request_id = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=1) as pool:
        acceptance = pool.submit(
            availability.start,
            revision.id,
            actor="operator",
            request_id=request_id,
        )
        try:
            assert locked.wait(10), "new consumer did not lock the shared build"
            # The accepted-request transaction owns the build row. Cancellation
            # must remain durable and pending instead of cancelling its child.
            availability.reconcile_cancellations()
            assert availability.get(first.id).state == "cancelling"
        finally:
            resume.set()
        second = acceptance.result(timeout=10)

    assert second.state == "queued"
    availability.reconcile_cancellations()
    assert availability.get(first.id).state == "cancelled"
    with sessions() as session:
        child = session.get(Job, build.id)
        assert child is not None and child.result is None
        accepted = session.scalar(select(Job).where(Job.request_id == request_id))
        assert accepted is not None and accepted.state == "queued"


@pytest.mark.parametrize("linked", [True, False])
def test_new_availability_consumer_cannot_join_after_cancellation_fences_shared_build(
    tmp_path, postgres_engine, monkeypatch, linked
):
    sessions, _builds, operations, storage, now, _node, revision, plan = _services(
        tmp_path, postgres_engine
    )
    with sessions.begin() as session:
        session.add(User(subject="operator", role="operator"))
    availability = _availability(sessions, storage, now, revision, plan)
    parent = availability.start(
        revision.id, actor="operator", request_id=str(uuid.uuid4())
    )
    claim = availability.claim_pending(limit=1, owner_id="cancel-race-last")[0]
    child_key = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"vonk:recipe-image-build:{parent.id}:{claim.execution_attempt}",
        )
    )
    build = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="recipe-image-availability",
        request_id=child_key,
    )
    dependency = RecipeBuildDependency(
        request_key=uuid.UUID(child_key), operation_id=uuid.UUID(build.id)
    )
    if linked:
        with sessions.begin() as session:
            current = session.get(Job, parent.id)
            assert current is not None
            current.payload = dict(current.payload) | {
                "build_dependency": dependency.model_dump(
                    mode="json", exclude_none=True
                )
            }
    availability.cancel(
        parent.id,
        actor="operator",
        request_id=str(uuid.uuid4()),
        reason="cancel the final preparation consumer",
    )

    fenced = threading.Event()
    resume = threading.Event()
    original_request = availability_module.request_build_cancellation

    def hold_cancelled_child(job, **kwargs):
        original_request(job, **kwargs)
        fenced.set()
        assert resume.wait(10), "build cancellation barrier was not released"

    monkeypatch.setattr(
        availability_module, "request_build_cancellation", hold_cancelled_child
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        reconciliation = pool.submit(availability.reconcile_cancellations)
        try:
            assert fenced.wait(10), "last-consumer cancellation did not fence the build"
            blocked_request_id = str(uuid.uuid4())
            with pytest.raises(RecipeImageAvailabilityError) as busy:
                availability.start(
                    revision.id,
                    actor="operator",
                    request_id=blocked_request_id,
                )
            assert busy.value.code == "build.consumer_busy"
        finally:
            resume.set()
        reconciliation.result(timeout=10)

    retry_request_id = str(uuid.uuid4())
    with pytest.raises(RecipeImageAvailabilityError) as pending:
        availability.start(
            revision.id,
            actor="operator",
            request_id=retry_request_id,
        )
    assert pending.value.code == "build.cancellation_pending"
    with sessions() as session:
        child = session.get(Job, build.id)
        assert child is not None and child.result is not None
        assert child.result["cancel_requested"] is True
        assert (
            session.scalar(
                select(Job.id).where(
                    Job.request_id.in_((blocked_request_id, retry_request_id))
                )
            )
            is None
        )


def test_existing_build_identity_mismatch_cannot_be_treated_as_an_unbound_consumer(
    tmp_path, postgres_engine
):
    sessions, _builds, _operations, _storage, _now, _node, revision, plan = _services(
        tmp_path, postgres_engine
    )
    with sessions.begin() as session:
        with pytest.raises(BuildConsumerError) as caught:
            lock_build_dependency(
                session,
                recipe_revision_id=revision.id,
                builder_node_id=plan.builder_node_id,
                build_input_sha256="0" * 64,
                build_id=plan.build_id,
            )
        assert caught.value.code == "build.consumer_invalid"


def test_invalid_cleanup_evidence_refuses_consumer_acceptance_with_a_typed_error(
    tmp_path, postgres_engine
):
    sessions, _builds, operations, storage, now, _node, revision, plan = _services(
        tmp_path, postgres_engine
    )
    build = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="operator",
        request_id=str(uuid.uuid4()),
    )
    _issue(sessions, build.id)
    operations.cancel(
        build.id,
        actor="operator",
        request_id=str(uuid.uuid4()),
        reason="cancel before a new consumer joins",
    )
    with sessions.begin() as session:
        job = session.get(Job, build.id)
        assert job is not None and job.result is not None
        job.result = dict(job.result) | {"cancel_request_id": None}
    claims = _active_claims(sessions, plan.build_id)
    availability = _availability(sessions, storage, now, revision, plan)
    request_id = str(uuid.uuid4())
    with pytest.raises(RecipeImageAvailabilityError) as caught:
        availability.start(revision.id, actor="operator", request_id=request_id)
    assert caught.value.code == "build.consumer_invalid"
    assert not caught.value.retryable
    assert _active_claims(sessions, plan.build_id) == claims
    with sessions() as session:
        assert (
            session.scalar(select(Job.id).where(Job.request_id == request_id)) is None
        )
