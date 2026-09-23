"""Accepted preparation consumers must participate in build cancellation."""

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select
from vonk_control import fleet_profiles as profile_module
from vonk_control.models import (
    AgentNode,
    FleetProfileApplication,
    Job,
    NodeInventorySnapshot,
    RecipeBuild,
)
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
    body = {"expected_plan_digest": review["plan_digest"], "request_key": request_key}
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
                "expected_plan_digest": review["plan_digest"],
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
