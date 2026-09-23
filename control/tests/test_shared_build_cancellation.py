"""Parent detachment preserves shared and independent build intent."""

import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from vonk_control import recipe_operations as operations_module
from vonk_control.models import AgentNode, CatalogDocumentRevision, Job, RecipeBuild
from vonk_control.recipe_image_availability import RecipeImageAvailabilityError
from vonk_control.recipe_operations import RecipeOperationService
from vonk_control.run_switch_operations import RunSwitchOperationConflict
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

from .test_build_cancellation_recovery import _active_claims, _issue, _settle_cleanup
from .test_build_consumer_ownership import _availability
from .test_run_switch_build_fencing import _direct_parent, _snapshot


@pytest.mark.parametrize("independent", [False, True])
def test_parent_detaches_without_cancelling_an_independent_producer(
    tmp_path, postgres_engine, independent
):
    sessions, planner, parent, _request, selected = _direct_parent(
        tmp_path, postgres_engine
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    if independent:
        lifecycle.build(
            selected,
            build_input_sha256=selected.build_input_sha256,
            actor="admin",
            request_id=str(uuid4()),
        )
    assert planner._advance(parent.operation_id)
    with sessions() as session:
        child_id = session.scalar(select(Job.id).where(Job.kind == "recipe.build.v1"))
        assert child_id is not None
    claims = _active_claims(sessions, selected.build_id)
    cancelled = planner.cancel(
        parent.operation_id,
        actor="admin",
        request_key=str(uuid4()),
        reason="Detach this request",
    )
    assert cancelled.state == "cancelled"
    # Reconstruct the owner from durable data; an in-memory consumer count or
    # producer flag cannot preserve the independent request across this seam.
    lifecycle = RecipeOperationService(
        sessions,
        install_admission=lifecycle._install_admission,
        run_admission=lifecycle._run_admission,
        agent_jobs=lifecycle._agent_jobs,
        clock=lifecycle._clock,
        builds=lifecycle._builds,
    )
    assert lifecycle.reconcile_cancelled_builds() is (not independent)
    assert lifecycle.get(child_id).state == ("running" if independent else "cancelled")
    assert bool(_active_claims(sessions, selected.build_id)) is independent
    if independent:
        assert _active_claims(sessions, selected.build_id) == claims
        assert (
            lifecycle.cancel(
                child_id,
                actor="admin",
                request_id=str(uuid4()),
                reason="Cancel the independent request",
            ).state
            == "cancelled"
        )


def test_shared_build_survives_one_detachment_then_stops_after_last_consumer(
    tmp_path, postgres_engine
):
    sessions, planner, parent, _request, selected = _direct_parent(
        tmp_path, postgres_engine
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    assert planner._advance(parent.operation_id)
    with sessions() as session:
        child_id = session.scalar(select(Job.id).where(Job.kind == "recipe.build.v1"))
        revision = session.get(CatalogDocumentRevision, selected.recipe_revision_id)
        assert child_id is not None and revision is not None
    availability = _availability(
        sessions,
        FilesystemRuntimeImageStorage(tmp_path / "images"),
        lifecycle._clock(),
        revision,
        selected,
    )
    consumer = availability.start(revision.id, actor="admin", request_id=str(uuid4()))
    claims = _active_claims(sessions, selected.build_id)
    assert (
        planner.cancel(
            parent.operation_id,
            actor="admin",
            request_key=str(uuid4()),
            reason="Detach one parent",
        ).state
        == "cancelled"
    )
    assert not lifecycle.reconcile_cancelled_builds()
    assert lifecycle.get(child_id).state == "running"
    assert _active_claims(sessions, selected.build_id) == claims
    # The remaining accepted owner encounters a terminal source refusal.
    # Its real executor relinquishes demand; it does not delete shared bytes.
    assert availability.run_pending(limit=1) == 1
    assert availability.get(consumer.id).state == "failed"
    assert lifecycle.reconcile_cancelled_builds()
    assert lifecycle.get(child_id).state == "cancelled"


def test_last_consumer_retains_issued_capacity_until_exact_cleanup_receipt(
    tmp_path, postgres_engine
):
    sessions, planner, parent, _request, selected = _direct_parent(
        tmp_path, postgres_engine
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    assert planner._advance(parent.operation_id)
    with sessions() as session:
        child_id = session.scalar(select(Job.id).where(Job.kind == "recipe.build.v1"))
        assert child_id is not None
    executor_id = _issue(sessions, child_id)
    with sessions.begin() as session:
        node = session.get(AgentNode, selected.builder_node_id)
        assert node is not None
        node.capabilities = [*node.capabilities, "recipe.build.cleanup.v1"]
    claims = _active_claims(sessions, selected.build_id)
    assert (
        planner.cancel(
            parent.operation_id,
            actor="admin",
            request_key=str(uuid4()),
            reason="Stop dependent build",
        ).state
        == "cancelled"
    )
    assert lifecycle.reconcile_cancelled_builds()
    assert _active_claims(sessions, selected.build_id) == claims
    _settle_cleanup(
        sessions, lifecycle, selected.build_id, selected.builder_node_id, executor_id
    )
    assert lifecycle.get(child_id).state == "cancelled"
    assert not _active_claims(sessions, selected.build_id)


def test_parent_detachment_refuses_a_busy_build_boundary_without_partial_changes(
    tmp_path, postgres_engine
):
    sessions, planner, parent, _request, selected = _direct_parent(
        tmp_path, postgres_engine
    )
    assert planner._advance(parent.operation_id)
    before = _snapshot(sessions, parent.operation_id)
    key = str(uuid4())
    with sessions.begin() as blocker:
        blocker.get(RecipeBuild, selected.build_id, with_for_update=True)
        with pytest.raises(RunSwitchOperationConflict, match="build.consumer_busy"):
            planner.cancel(
                parent.operation_id, actor="admin", request_key=key, reason="Detach"
            )
    assert _snapshot(sessions, parent.operation_id) == before
    assert (
        planner.cancel(
            parent.operation_id, actor="admin", request_key=key, reason="Detach"
        ).state
        == "cancelled"
    )


@pytest.mark.parametrize("issued", [False, True])
def test_new_consumer_cannot_join_across_last_consumer_cleanup(
    tmp_path, postgres_engine, monkeypatch, issued
):
    sessions, planner, parent, _request, selected = _direct_parent(
        tmp_path, postgres_engine
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    assert planner._advance(parent.operation_id)
    with sessions.begin() as session:
        child_id = session.scalar(select(Job.id).where(Job.kind == "recipe.build.v1"))
        revision = session.get(CatalogDocumentRevision, selected.recipe_revision_id)
        node = session.get(AgentNode, selected.builder_node_id)
        assert child_id is not None and revision is not None and node is not None
        node.capabilities = [*node.capabilities, "recipe.build.cleanup.v1"]
    executor_id = _issue(sessions, child_id) if issued else None
    planner.cancel(
        parent.operation_id,
        actor="admin",
        request_key=str(uuid4()),
        reason="Detach the last consumer",
    )
    availability = _availability(
        sessions,
        FilesystemRuntimeImageStorage(tmp_path / "images"),
        lifecycle._clock(),
        revision,
        selected,
    )
    original = operations_module.current_build_consumers
    reached = threading.Event()
    resume = threading.Event()

    def paused_consumers(*args, **kwargs):
        consumers = original(*args, **kwargs)
        assert consumers == ()
        reached.set()
        assert resume.wait(10), "cleanup barrier was not released"
        return consumers

    key = str(uuid4())
    monkeypatch.setattr(operations_module, "current_build_consumers", paused_consumers)
    with ThreadPoolExecutor(max_workers=1) as pool:
        cleaning = pool.submit(lifecycle.reconcile_cancelled_builds)
        try:
            assert reached.wait(10), "cleanup did not reach its ownership boundary"
            with pytest.raises(RecipeImageAvailabilityError) as caught:
                availability.start(revision.id, actor="admin", request_id=key)
            assert caught.value.code == "build.consumer_busy"
        finally:
            resume.set()
        assert cleaning.result(timeout=10)
    monkeypatch.setattr(operations_module, "current_build_consumers", original)
    if executor_id is not None:
        with pytest.raises(RecipeImageAvailabilityError) as caught:
            availability.start(revision.id, actor="admin", request_id=key)
        assert caught.value.code == "build.cancellation_pending"
        _settle_cleanup(
            sessions,
            lifecycle,
            selected.build_id,
            selected.builder_node_id,
            executor_id,
        )
    with sessions() as session:
        assert session.scalar(select(Job.id).where(Job.request_id == key)) is None
    accepted = availability.start(revision.id, actor="admin", request_id=key)
    assert accepted.state == "queued"
    assert lifecycle.get(child_id).state == "cancelled"


def test_malformed_ownership_cannot_starve_an_unneeded_valid_build(
    tmp_path, postgres_engine
):
    sessions, planner, parent, _request, _selected = _direct_parent(
        tmp_path, postgres_engine
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    assert planner._advance(parent.operation_id)
    planner.cancel(
        parent.operation_id,
        actor="admin",
        request_key=str(uuid4()),
        reason="Detach the last consumer",
    )
    with sessions.begin() as session:
        child = session.scalar(select(Job).where(Job.kind == "recipe.build.v1"))
        assert child is not None
        child_id = child.id
        # Cross the existing scan batch boundary with malformed persisted
        # ownership. A scan that starts at the beginning on every pass never
        # reaches the legitimate job, despite its lack of current consumers.
        malformed_ids = [str(UUID(int=UUID(child_id).int - n)) for n in range(1, 33)]
        for identity in malformed_ids:
            session.add(
                Job(
                    id=identity,
                    request_id=str(uuid4()),
                    kind="recipe.build.v1",
                    state="running",
                    actor="admin",
                    authority_revision=child.authority_revision,
                    targets=[],
                    payload_digest="a" * 64,
                    payload={"build_intent": {"kind": "dependency"}},
                    created_at=lifecycle._clock(),
                    updated_at=lifecycle._clock(),
                )
            )
    assert not lifecycle.reconcile_cancelled_builds()
    assert lifecycle.reconcile_cancelled_builds()
    assert lifecycle.get(child_id).state == "cancelled"
    with sessions() as session:
        for identity in malformed_ids:
            malformed = session.get(Job, identity)
            assert malformed is not None
            assert malformed.state == "running" and malformed.result is None
