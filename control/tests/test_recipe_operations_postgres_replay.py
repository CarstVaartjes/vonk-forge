"""Recipe operations: postgres replay."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, select
from vonk_forge_contracts import (
    read_recipe,
)

from vonk_agent_protocol.contracts import AgentInstallResult, canonical_message
from vonk_agent_protocol.contracts import AgentOperation as WireAgentOperation
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState, WaitReason
from vonk_agent_protocol.reason_codes.admission import InstallAdmissionCode
from vonk_agent_protocol.reason_codes.recipes import RecipeImageCode
from vonk_agent_protocol.reason_codes.runtime import RuntimePreflightCode
from vonk_control.agent_operation_facts import SUPERSEDED_CANCELLATION_SECONDS
from vonk_control.install_admission.contracts import (
    AdmissionReason,
    InstallAdmissionBusy,
    InstallNodePlan,
    InstallPlan,
)
from vonk_control.install_admission.validation import (
    require_admissible as require_install_admissible,
)
from vonk_control.jobs.documents import RecipeStopParent
from vonk_control.lifecycle.image_availability import CANCEL_BUDGET
from vonk_control.model_primitives import Base
from vonk_control.models.cache import RecipeBuild
from vonk_control.models.catalog import (
    CatalogDocumentRevision,
)
from vonk_control.models.execution import (
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
)
from vonk_control.models.fleet import AgentNode
from vonk_control.models.jobs import Job
from vonk_control.models.operations import AgentOperation
from vonk_control.recipe_image_availability.availability_contract import (
    AvailabilityBuildReceipt,
)
from vonk_control.recipe_image_availability.contracts import (
    OPERATION_KIND,
    BuildUnsettled,
)
from vonk_control.recipe_image_availability.service import (
    RecipeImageAvailabilityService,
)
from vonk_control.recipe_operations import persistence
from vonk_control.recipe_operations.errors import (
    RecipeOperationConflict,
)
from vonk_control.run_admission import RunAdmissionBusy
from vonk_control.runtime_image_preparation.preparation import (
    stored_runtime_image_resolver,
)
from vonk_control.runtime_image_preparation.storage import FilesystemRuntimeImageStorage

from .recipe_operations_support import (
    NOW,
    ConcurrentPublisher,
    _CanonicalImageTransport,
    _postgres_backend_pid,
    _wait_for_postgres_block,
    bind_route_publications,
    installed_recipe,
    setup_services,
    started_recipe,
)


def test_postgres_duplicate_stop_request_returns_same_operation(
    tmp_path: Path, postgres_engine
) -> None:
    # Both Stops carry a preview the run has moved on from: the first is
    # re-planned, never refused. The duplicate pauses at its run fence while
    # the first is accepted, withdrawn and dispatched, then returns that Stop.
    Base.metadata.drop_all(postgres_engine)
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="e" * 35 + "5"
    )
    run = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="e" * 35 + "6",
    )
    publisher = ConcurrentPublisher()
    service, routes = bind_route_publications(sessions, service, publisher)
    routes.publish_run(run.owner_id)
    plan = service.preview_stop(run.owner_id)
    with sessions.begin() as session:
        moved = session.get(RecipeRun, run.owner_id)
        assert moved is not None
        moved.state = "failed"
    assert service.preview_stop(run.owner_id).plan_digest != plan.plan_digest
    request_key = "e" * 35 + "7"
    role = threading.local()
    second_at_fence = threading.Event()
    release_second = threading.Event()

    def at_fence(_connection, _cursor, statement, _parameters, _context, _many):
        if (
            getattr(role, "value", None) == "second"
            and "FROM recipe_runs" in statement
            and "FOR UPDATE" in statement
            and not second_at_fence.is_set()
        ):
            second_at_fence.set()
            assert release_second.wait(timeout=10)

    def stop(label: str) -> str:
        role.value = label
        return service.stop(
            run.owner_id,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id=request_key,
        ).id

    event.listen(postgres_engine, "before_cursor_execute", at_fence)
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        second = pool.submit(stop, "second")
        assert second_at_fence.wait(timeout=10)
        first_id = pool.submit(stop, "first").result(timeout=10)
        release_second.set()
        second_id = second.result(timeout=10)
    finally:
        release_second.set()
        pool.shutdown(wait=True)
        event.remove(postgres_engine, "before_cursor_execute", at_fence)

    assert second_id == first_id
    with sessions() as session:
        assert (
            len(
                tuple(session.scalars(select(Job).where(Job.request_id == request_key)))
            )
            == 1
        )


@pytest.mark.parametrize("release_on_wait", [True, False], ids=["released", "deadline"])
def test_postgres_duplicate_stop_waiting_on_dispatch_replays(
    tmp_path: Path,
    postgres_engine,
    monkeypatch: pytest.MonkeyPatch,
    release_on_wait: bool,
) -> None:
    # Dispatch already owns the accepted parent and run. Re-taking the request
    # creation key here makes its own duplicate spend every try-lock attempt
    # before it can read that receipt. Hold dispatch until the duplicate waits
    # on its run, then let the committed receipt answer both callers.

    Base.metadata.drop_all(postgres_engine)
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="e" * 35 + "b"
    )
    run = started_recipe(
        sessions, service, installation.owner_id, nodes, request_id="e" * 35 + "c"
    )
    service, routes = bind_route_publications(sessions, service, ConcurrentPublisher())
    routes.publish_run(run.owner_id)
    plan = service.preview_stop(run.owner_id)
    request_key = "e" * 35 + "d"
    role = threading.local()
    backend_pids: dict[str, int] = {}
    dispatch_locked = threading.Event()
    duplicate_progress = threading.Event()
    release_dispatch = threading.Event()
    acquire = persistence.acquire_admission_keys

    def held_dispatch(session, keys, *, holder=None):
        acquire(session, keys, holder=holder)
        if getattr(role, "value", None) == "first":
            backend_pids["first"] = _postgres_backend_pid(session.connection())
            dispatch_locked.set()
            assert release_dispatch.wait(timeout=10)

    def before_lock(connection, _cursor, statement, _parameters, _context, _many):
        if (
            getattr(role, "value", None) == "second"
            and "FROM recipe_runs" in statement
            and "FOR UPDATE" in statement
        ):
            backend_pids["second"] = _postgres_backend_pid(connection)
            duplicate_progress.set()

    def stop(label: str) -> str:
        role.value = label
        return service.stop(
            run.owner_id,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id=request_key,
        ).id

    monkeypatch.setattr(persistence, "acquire_admission_keys", held_dispatch)
    event.listen(postgres_engine, "before_cursor_execute", before_lock)
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        first = pool.submit(stop, "first")
        assert dispatch_locked.wait(timeout=10)
        second = pool.submit(stop, "second")
        second.add_done_callback(lambda _future: duplicate_progress.set())
        assert duplicate_progress.wait(timeout=10)
        if second.done():
            # Surface an early refusal as its actual error, not a sync timeout.
            second.result()
            pytest.fail("duplicate Stop returned before dispatch released its run")
        _wait_for_postgres_block(
            postgres_engine,
            blocked_pid=backend_pids["second"],
            blocker_pid=backend_pids["first"],
        )
        if release_on_wait:
            release_dispatch.set()
        # At the SQL lock deadline the duplicate must still return the receipt,
        # while dispatch remains held. It neither cancels nor fails that Stop.
        second_id = second.result(timeout=10)
        release_dispatch.set()
        first_id = first.result(timeout=10)
    finally:
        release_dispatch.set()
        pool.shutdown(wait=True)
        event.remove(postgres_engine, "before_cursor_execute", before_lock)

    assert second_id == first_id
    with sessions() as session:
        assert [
            job.id
            for job in session.scalars(select(Job).where(Job.request_id == request_key))
        ] == [first_id]
        assert len(
            tuple(
                session.scalars(
                    select(AgentOperation.id).where(
                        AgentOperation.parent_job_id == first_id
                    )
                )
            )
        ) == len(nodes)


def test_postgres_duplicate_stop_waiting_on_withdrawal_claim_replays(
    tmp_path: Path, postgres_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first Stop holds the run lock in its withdrawal-claim transaction
    # (route WITHDRAWN, review stage advanced, not yet committed) while the
    # duplicate arrives; the duplicate must replay, never be refused.
    Base.metadata.drop_all(postgres_engine)
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="e" * 35 + "8"
    )
    run = started_recipe(
        sessions, service, installation.owner_id, nodes, request_id="e" * 35 + "9"
    )
    service, routes = bind_route_publications(sessions, service, ConcurrentPublisher())
    routes.publish_run(run.owner_id)
    plan = service.preview_stop(run.owner_id)
    request_key = "e" * 35 + "a"
    role = threading.local()
    backend_pids: dict[str, int] = {}
    first_claimed = threading.Event()
    second_lock_started = threading.Event()
    release_first = threading.Event()
    withdrawal_publication = routes._withdrawal_publication

    def held_withdrawal_publication(session, withdrawal, *, after=None):
        publication = withdrawal_publication(session, withdrawal, after=after)
        if getattr(role, "value", None) == "first" and not first_claimed.is_set():
            backend_pids["first"] = _postgres_backend_pid(session.connection())
            first_claimed.set()
            assert release_first.wait(timeout=10)
        return publication

    def before_lock(connection, _cursor, statement, _parameters, _context, _many):
        if (
            getattr(role, "value", None) == "second"
            and "FROM recipe_runs" in statement
            and "FOR UPDATE" in statement
            and not second_lock_started.is_set()
        ):
            backend_pids["second"] = _postgres_backend_pid(connection)
            second_lock_started.set()

    def stop(label: str) -> str:
        role.value = label
        return service.stop(
            run.owner_id,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id=request_key,
        ).id

    monkeypatch.setattr(routes, "_withdrawal_publication", held_withdrawal_publication)
    event.listen(postgres_engine, "before_cursor_execute", before_lock)
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        first = pool.submit(stop, "first")
        assert first_claimed.wait(timeout=10)
        second = pool.submit(stop, "second")
        assert second_lock_started.wait(timeout=10)
        _wait_for_postgres_block(
            postgres_engine,
            blocked_pid=backend_pids["second"],
            blocker_pid=backend_pids["first"],
        )
        release_first.set()
        first_id = first.result(timeout=10)
        second_id = second.result(timeout=10)
    finally:
        release_first.set()
        pool.shutdown(wait=True)
        event.remove(postgres_engine, "before_cursor_execute", before_lock)

    assert second_id == first_id
    with sessions() as session:
        assert [
            job.id
            for job in session.scalars(select(Job).where(Job.request_id == request_key))
        ] == [first_id]


def test_postgres_pending_stop_reconciler_waits_for_dispatch_of_accepted_stop(
    tmp_path: Path, postgres_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reconciler ends an overdue accepted Stop holding only its Job lock.
    # While the Stop is being dispatched it must wait for that dispatch and
    # then leave the dispatched Stop alone: never fail a Stop whose children
    # are being queued, which would leave work without a live parent.
    Base.metadata.drop_all(postgres_engine)
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="e" * 35 + "b"
    )
    run = started_recipe(
        sessions, service, installation.owner_id, nodes, request_id="e" * 35 + "c"
    )
    service, routes = bind_route_publications(sessions, service, ConcurrentPublisher())
    routes.publish_run(run.owner_id)
    plan = service.preview_stop(run.owner_id)
    request_key = "e" * 35 + "d"
    role = threading.local()
    clocks = {
        "dispatch": NOW + timedelta(seconds=1),
        "reconciler": NOW + timedelta(seconds=SUPERSEDED_CANCELLATION_SECONDS + 1),
    }
    service._clock = lambda: clocks.get(getattr(role, "value", ""), NOW)
    backend_pids: dict[str, int] = {}
    dispatch_paused = threading.Event()
    reconciler_waits = threading.Event()
    release_dispatch = threading.Event()
    dispatch = service._dispatch_stop_after_withdrawal

    def dispatching(*args, **kwargs):
        role.value = "dispatch"
        return dispatch(*args, **kwargs)

    def sync_point(connection, _cursor, statement, _parameters, _context, _many):
        label = getattr(role, "value", None)
        if (
            label == "dispatch"
            and "FROM recipe_runs" in statement
            and "FOR UPDATE" in statement
            and not dispatch_paused.is_set()
        ):
            backend_pids["dispatch"] = _postgres_backend_pid(connection)
            dispatch_paused.set()
            assert release_dispatch.wait(timeout=10)
        elif (
            label == "reconciler"
            and "FROM jobs" in statement
            and "FOR UPDATE" in statement
            and "NOWAIT" not in statement
            and not reconciler_waits.is_set()
        ):
            backend_pids["reconciler"] = _postgres_backend_pid(connection)
            reconciler_waits.set()

    def stop() -> str:
        role.value = "first"
        return service.stop(
            run.owner_id,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id=request_key,
        ).id

    def reconcile() -> bool:
        role.value = "reconciler"
        return service.reconcile_pending_service_stops()

    monkeypatch.setattr(service, "_dispatch_stop_after_withdrawal", dispatching)
    event.listen(postgres_engine, "before_cursor_execute", sync_point)
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        stopped = pool.submit(stop)
        assert dispatch_paused.wait(timeout=10)
        reconciled = pool.submit(reconcile)
        assert reconciler_waits.wait(timeout=10)
        _wait_for_postgres_block(
            postgres_engine,
            blocked_pid=backend_pids["reconciler"],
            blocker_pid=backend_pids["dispatch"],
        )
        release_dispatch.set()
        stop_id = stopped.result(timeout=10)
        reconciled.result(timeout=10)
    finally:
        release_dispatch.set()
        pool.shutdown(wait=True)
        event.remove(postgres_engine, "before_cursor_execute", sync_point)

    with sessions() as session:
        job = session.get(Job, stop_id)
        assert job is not None
        assert job.state == LifecycleState.RUNNING.value
        review = RecipeStopParent.model_validate_json(
            canonical_message(job.payload)
        ).service_stop_review
        assert review is not None
        assert review.stage == "dispatched"
        assert session.scalar(
            select(AgentOperation.id).where(AgentOperation.parent_job_id == stop_id)
        )


def test_postgres_duplicate_uninstall_rechecks_replay_after_installation_lock(
    tmp_path: Path, postgres_engine
) -> None:
    Base.metadata.drop_all(postgres_engine)
    sessions, service, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, engine=postgres_engine
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="f" * 35 + "2"
    )
    request_key = "f" * 35 + "3"
    available_before = queue.available
    role = threading.local()
    backend_pids: dict[str, int] = {}
    first_locked = threading.Event()
    second_lock_started = threading.Event()
    release_first = threading.Event()

    def before_lock(connection, _cursor, statement, _parameters, _context, _many):
        if (
            getattr(role, "value", None) == "second"
            and "FROM recipe_installations" in statement
            and "FOR UPDATE" in statement
        ):
            backend_pids["second"] = _postgres_backend_pid(connection)
            second_lock_started.set()

    def after_lock(connection, _cursor, statement, _parameters, _context, _many):
        if (
            getattr(role, "value", None) == "first"
            and "FROM recipe_installations" in statement
            and "FOR UPDATE" in statement
        ):
            backend_pids["first"] = _postgres_backend_pid(connection)
            first_locked.set()
            assert release_first.wait(timeout=10)

    def uninstall(label: str):
        role.value = label
        return service.uninstall(
            installation.owner_id,
            actor="admin",
            request_id=request_key,
        )

    event.listen(postgres_engine, "before_cursor_execute", before_lock)
    event.listen(postgres_engine, "after_cursor_execute", after_lock)
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        first = pool.submit(uninstall, "first")
        assert first_locked.wait(timeout=10)
        second = pool.submit(uninstall, "second")
        assert second_lock_started.wait(timeout=10)
        _wait_for_postgres_block(
            postgres_engine,
            blocked_pid=backend_pids["second"],
            blocker_pid=backend_pids["first"],
        )
        release_first.set()
        first_view = first.result(timeout=10)
        second_view = second.result(timeout=10)
    finally:
        release_first.set()
        pool.shutdown(wait=True)
        event.remove(postgres_engine, "before_cursor_execute", before_lock)
        event.remove(postgres_engine, "after_cursor_execute", after_lock)

    assert second_view == first_view
    assert queue.available == available_before + 1
    with sessions() as session:
        parents = tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.uninstall"))
        )
        children = tuple(
            session.scalars(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == first_view.id
                )
            )
        )
    assert [parent.id for parent in parents] == [first_view.id]
    assert {child.node_id for child in children} == set(nodes)


def test_postgres_start_waiting_on_accepted_uninstall_is_rejected(
    tmp_path: Path, postgres_engine
) -> None:
    Base.metadata.drop_all(postgres_engine)
    sessions, service, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, engine=postgres_engine
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="f" * 35 + "4"
    )
    run_plan = service.preview_run(installation.owner_id, "must-not-start")
    available_before = queue.available
    role = threading.local()
    uninstall_locked = threading.Event()
    start_lock_started = threading.Event()
    release_uninstall = threading.Event()

    def before_lock(connection, _cursor, statement, _parameters, _context, _many):
        if (
            getattr(role, "value", None) == "start"
            and "FROM recipe_installations" in statement
            and "FOR UPDATE" in statement
        ):
            assert "NOWAIT" in statement.upper()
            start_lock_started.set()

    def after_lock(connection, _cursor, statement, _parameters, _context, _many):
        if (
            getattr(role, "value", None) == "uninstall"
            and "FROM recipe_installations" in statement
            and "FOR UPDATE" in statement
        ):
            uninstall_locked.set()
            # Outlives the observer's bound: only a bounded start returns.
            assert release_uninstall.wait(timeout=60)

    def uninstall():
        role.value = "uninstall"
        return service.uninstall(
            installation.owner_id,
            actor="admin",
            request_id="f" * 35 + "5",
        )

    def start():
        role.value = "start"
        try:
            return service.start(
                run_plan,
                plan_digest=run_plan.plan_digest,
                actor="admin",
                request_id="f" * 35 + "6",
            )
        except (RecipeOperationConflict, RunAdmissionBusy) as error:
            return error

    event.listen(postgres_engine, "before_cursor_execute", before_lock)
    event.listen(postgres_engine, "after_cursor_execute", after_lock)
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        uninstall_future = pool.submit(uninstall)
        assert uninstall_locked.wait(timeout=10)
        start_future = pool.submit(start)
        assert start_lock_started.wait(timeout=10)
        completed, _ = wait((start_future,), timeout=30)
        returned_while_uninstall_held = start_future in completed
        start_result_while_held = (
            start_future.result() if returned_while_uninstall_held else None
        )
        release_uninstall.set()
        uninstall_view = uninstall_future.result(timeout=10)
        start_result = (
            start_result_while_held
            if returned_while_uninstall_held
            else start_future.result(timeout=10)
        )
    finally:
        release_uninstall.set()
        pool.shutdown(wait=True)
        event.remove(postgres_engine, "before_cursor_execute", before_lock)
        event.remove(postgres_engine, "after_cursor_execute", after_lock)

    assert returned_while_uninstall_held, (
        "start waited on the accepted uninstall instead of returning a bounded "
        f"admission refusal: {start_result!r}"
    )
    assert isinstance(start_result, RunAdmissionBusy), (
        "start did not report retryable row-lock contention while uninstall "
        f"was still held: {start_result!r}"
    )

    # The busy refusal has no durable request owner, so the exact request can
    # be retried after the uninstall commits. At that point the committed
    # uninstall is authoritative and refuses the run for its actual reason.
    with pytest.raises(Exception) as _ending:
        service.start(
            run_plan,
            plan_digest=run_plan.plan_digest,
            actor="admin",
            request_id="f" * 35 + "6",
        )
    assert queue.available == available_before + 1
    with sessions() as session:
        start_jobs = tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.start"))
        )
        runs = tuple(session.scalars(select(RecipeRun)))
        uninstall_children = tuple(
            session.scalars(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == uninstall_view.id
                )
            )
        )
    assert start_jobs == ()
    assert runs == ()
    assert {child.node_id for child in uninstall_children} == set(nodes)


def test_postgres_start_refuses_without_waiting_on_a_busy_agent_node(
    tmp_path: Path, postgres_engine
) -> None:
    """A node-row conflict returns a domain refusal before releasing its holder."""
    Base.metadata.drop_all(postgres_engine)
    sessions, service, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, engine=postgres_engine
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="e" * 36
    )
    plan = service.preview_run(installation.owner_id, "busy-node")
    assert plan.allowed
    available_before = queue.available
    role = threading.local()
    start_node_lock_requested = threading.Event()

    def observe_start_node_lock(
        _connection, _cursor, statement, _parameters, _context, _many
    ):
        if (
            getattr(role, "value", None) == "start"
            and "FROM agent_nodes" in statement
            and "FOR NO KEY UPDATE" in statement
        ):
            start_node_lock_requested.set()

    def start():
        role.value = "start"
        return service.start(
            plan,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id="e" * 35 + "1",
        )

    event.listen(postgres_engine, "before_cursor_execute", observe_start_node_lock)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with sessions() as holder:
            holder.begin()
            locked_node = holder.scalar(
                select(AgentNode).where(AgentNode.node_id == nodes[0]).with_for_update()
            )
            assert locked_node is not None
            future = pool.submit(start)
            lock_requested = start_node_lock_requested.wait(timeout=10)
            # The holder keeps the row for longer than every admission bound
            # (lock timeout, bounded retries), however slow the runner: a start
            # that waits for the release cannot answer inside this window.
            completed, _ = wait((future,), timeout=10)
            returned_while_held = future in completed
            held_error = future.exception() if returned_while_held else None
            holder.commit()

        try:
            start_result = future.result(timeout=10)
            start_error: Exception | None = None
        except (RecipeOperationConflict, RunAdmissionBusy) as error:
            start_result = None
            start_error = error
    finally:
        pool.shutdown(wait=True)
        event.remove(postgres_engine, "before_cursor_execute", observe_start_node_lock)

    assert lock_requested, "start never attempted to lock the target AgentNode"
    assert returned_while_held, (
        "lifecycle start waited for a busy AgentNode row; after holder release "
        f"it returned {start_result!r} with error={start_error!r} "
        f"(while held: {held_error!r})"
    )
    assert isinstance(held_error, RunAdmissionBusy), (
        f"start did not return the current refusal while the row was held: "
        f"error={held_error!r} result_after_release={start_result!r} "
        f"error_after_release={start_error!r}"
    )
    assert queue.available == available_before
    with sessions() as session:
        start_jobs = tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.start"))
        )
        runs = tuple(session.scalars(select(RecipeRun)))
    assert start_jobs == ()
    assert runs == ()

    request_id = "e" * 35 + "1"
    retried = service.start(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id=request_id,
    )
    assert queue.available == available_before + 1
    replay = service.start(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id=request_id,
    )
    assert replay.id == retried.id
    assert queue.available == available_before + 1
    with sessions() as session:
        assert (
            len(tuple(session.scalars(select(Job).where(Job.kind == "recipe.start"))))
            == 1
        )
        assert len(tuple(session.scalars(select(RecipeRun)))) == 1


def test_postgres_start_bounds_uncommitted_job_request_unique_wait(
    tmp_path: Path, postgres_engine
) -> None:
    """An implicit unique-index wait rolls back and preserves exact-key retry."""
    Base.metadata.drop_all(postgres_engine)
    sessions, service, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, engine=postgres_engine
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="d" * 36
    )
    plan = service.preview_run(installation.owner_id, "busy-request")
    assert plan.allowed
    request_id = "e" * 35 + "2"
    available_before = queue.available
    created_at = NOW
    holder = sessions()
    holder.begin()
    holder.add(
        Job(
            id="f" * 36,
            request_id=request_id,
            kind="recipe.start",
            state="running",
            actor="admin",
            authority_revision="a" * 64,
            targets=list(nodes),
            payload_digest="b" * 64,
            payload={
                "schema_version": 1,
                "owner_kind": "run",
                "owner_id": "c" * 36,
                "plan_digest": plan.plan_digest,
            },
            created_at=created_at,
            updated_at=created_at,
        )
    )
    holder.flush()

    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(
        service.start,
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id=request_id,
    )
    try:
        completed, _ = wait((future,), timeout=30)
        returned_while_unique_row_uncommitted = future in completed
        busy_error = (
            future.exception() if returned_while_unique_row_uncommitted else None
        )
    finally:
        holder.rollback()
        holder.close()
        if not future.done():
            future.result(timeout=10)
        pool.shutdown(wait=True)

    assert returned_while_unique_row_uncommitted, (
        "start waited on the jobs.request_id unique index; the admission lock "
        "budget must bound implicit unique waits"
    )
    assert isinstance(busy_error, RunAdmissionBusy), (
        f"uncommitted request collision did not become a retryable admission "
        f"refusal: {busy_error!r}"
    )
    assert queue.available == available_before
    with sessions() as session:
        assert tuple(session.scalars(select(RecipeRun))) == ()
        assert (
            tuple(session.scalars(select(Job).where(Job.kind == "recipe.start"))) == ()
        )
        assert (
            tuple(
                session.scalars(
                    select(ResourceReservation).where(
                        ResourceReservation.owner_kind == "run"
                    )
                )
            )
            == ()
        )

    retried = service.start(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id=request_id,
    )
    replay = service.start(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id=request_id,
    )
    assert replay.id == retried.id
    assert queue.available == available_before + 1
    with sessions() as session:
        assert len(tuple(session.scalars(select(RecipeRun)))) == 1
        assert (
            len(tuple(session.scalars(select(Job).where(Job.kind == "recipe.start"))))
            == 1
        )


def test_install_requires_reviewed_digest_and_request_keys_remain_scoped(
    tmp_path: Path,
) -> None:
    _sessions, service, _queue, mapping_id, build_id, _nodes = setup_services(tmp_path)
    plan = service.preview_install(mapping_id, build_id)
    with pytest.raises(Exception) as _ending:
        service.install(plan, plan_digest="0" * 64, actor="admin", request_id="9" * 36)
    service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="9" * 36
    )
    with pytest.raises(Exception) as _ending:
        service.stop(
            "f" * 36,
            plan_digest="0" * 64,
            actor="admin",
            request_id="9" * 36,
        )


def _blocked_install_plan(
    codes: tuple[str, ...], details: tuple[str, ...] | None = None
) -> InstallPlan:
    return InstallPlan(
        mapping_id="10000000-0000-4000-8000-000000000001",
        mapping_generation=1,
        recipe_build_id=None,
        image_digest="a" * 64,
        recipe_revision_id="20000000-0000-4000-8000-000000000002",
        recipe_content_sha256="b" * 64,
        allowed=False,
        nodes=(
            InstallNodePlan(
                node_id="spk_00000000000000000000000000000001",
                rank=0,
                role="entrypoint",
                allowed=False,
                inventory_observed_at=None,
                free_bytes=None,
                active_reserved_bytes=0,
                reused_bytes=0,
                required_download_bytes=0,
                required_bytes=0,
                required_payload_bytes=0,
                disk_floor_bytes=0,
                free_after_bytes=None,
                blockers=tuple(
                    AdmissionReason(
                        code=code,
                        detail=(
                            details[index]
                            if details is not None and index < len(details)
                            else code
                        ),
                    )
                    for index, code in enumerate(codes)
                ),
                warnings=(),
            ),
        ),
        plan_digest="c" * 64,
    )


@pytest.mark.parametrize(
    "codes",
    [
        (RuntimePreflightCode.HOST_CHANGED,),
        (RuntimePreflightCode.HOST_CHANGED, InstallAdmissionCode.INSUFFICIENT_DISK),
    ],
)
def test_prepare_installation_wait_releases_fresh_admission(
    tmp_path, monkeypatch, codes
) -> None:
    sessions, service, _queue, mapping, build, _nodes = setup_services(
        tmp_path, nodes=1
    )
    plan = service.preview_install(mapping, build)
    original = service._install_admission.plan_install
    blocked = replace(
        plan,
        allowed=False,
        nodes=tuple(
            replace(
                node,
                allowed=False,
                blockers=tuple(
                    AdmissionReason(code=code, detail="evidence unavailable")
                    for code in codes
                ),
            )
            for node in plan.nodes
        ),
    )
    monkeypatch.setattr(
        service._install_admission, "plan_install", lambda *a, **k: blocked
    )
    with pytest.raises(Exception) as _ending:
        service.prepare_installation(plan, actor="admin")
    with sessions() as session:
        assert session.scalar(select(RecipeInstallation)) is None
        assert session.scalar(select(ResourceReservation)) is None
    monkeypatch.setattr(service._install_admission, "plan_install", original)
    assert service.prepare_installation(plan, actor="admin")


def test_malformed_install_request_has_no_effect_and_fresh_valid_request_is_admitted(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, _nodes = setup_services(tmp_path)
    reviewed = service.preview_install(mapping_id, build_id)
    ended = False
    try:
        service.install(
            reviewed, plan_digest="invalid", actor="admin", request_id="malformed"
        )
    except Exception:  # noqa: BLE001 - outcome assertions cover effects and fresh admission
        ended = True
    assert ended
    with sessions() as session:
        assert (
            session.scalar(
                select(AgentOperation).where(
                    AgentOperation.kind == WireAgentOperation.RECIPE_INSTALL.value
                )
            )
            is None
        )
        assert session.scalar(select(RecipeInstallation)) is None
    fresh = service.install(
        reviewed,
        plan_digest=reviewed.plan_digest,
        actor="admin",
        request_id="fresh-valid",
    )
    assert fresh.owner_id


@pytest.mark.parametrize(
    "fault", ["missing", "malformed", "malformed-reply", "persistent"]
)
def test_installation_requests_exact_receipt_repair_before_any_agent_effect(
    tmp_path: Path,
    fault: str,
) -> None:

    sessions, service, _queue, mapping_id, build_id, _nodes = setup_services(tmp_path)
    reviewed = service.preview_install(mapping_id, build_id)
    assert reviewed.allowed, reviewed.nodes
    provider = service._install_admission._compiled_plan_provider
    assert provider is not None
    compiler = provider.__self__
    storage = FilesystemRuntimeImageStorage(tmp_path / "runtime-images")
    resolver = stored_runtime_image_resolver(storage)
    reply_damaged = [fault == "malformed-reply"]
    compiler._runtime_image_preparer = None

    def observe(*args):
        if reply_damaged[0]:
            reply_damaged[0] = False
            return None
        return resolver(*args)

    compiler._runtime_image_resolver = observe
    with sessions() as session:
        build = session.get(RecipeBuild, build_id)
        revision = session.get(CatalogDocumentRevision, reviewed.recipe_revision_id)
        assert revision is not None
        assert build is not None
        assert build.image_digest is not None
        assert build.oci_layout_sha256 is not None
        assert build.image_bytes is not None
        recipe = read_recipe(revision.document)
        evidence = AvailabilityBuildReceipt(
            state=LifecycleState.SUCCEEDED,
            build_id=build.id,
            build_input_sha256=build.build_input_sha256,
            image_digest=build.image_digest,
            oci_layout_sha256=build.oci_layout_sha256,
            image_bytes=build.image_bytes,
        )
    path = storage.root / f"{evidence.oci_layout_sha256}.receipt.json"
    if fault in ("missing", "persistent"):
        path.unlink()
    elif fault == "malformed":
        path.write_text("{")
    with sessions() as session:
        previous_jobs = set(session.scalars(select(Job.id)))

    blocked = [fault == "persistent"]
    now = [NOW]

    def build_exact(*_args, **_kwargs):
        with sessions() as session:
            assert (
                session.scalar(
                    select(AgentOperation).where(
                        AgentOperation.kind == WireAgentOperation.RECIPE_INSTALL.value
                    )
                )
                is None
            )
        if blocked[0]:
            return BuildUnsettled(
                RecipeImageCode.BUILD_WAIT,
                "verified build observation unavailable",
                retryable=True,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        return evidence.model_dump(mode="json")

    preparation = RecipeImageAvailabilityService(
        sessions,
        storage=storage,
        transport=_CanonicalImageTransport(),
        authority=lambda *_args, **_kwargs: (
            recipe,
            {
                "architecture": "linux/arm64",
                "interface": "vonk.runtime.v1",
                "build_input_sha256": evidence.build_input_sha256,
            },
        ),
        builder=build_exact,
        clock=lambda: now[0],
    )
    service.bind_install_preparation(preparation.prepare_install)
    request_id = "receipt-repair"
    installed = None
    ended = False
    try:
        installed = service.install(
            reviewed,
            plan_digest=reviewed.plan_digest,
            actor="admin",
            request_id=request_id,
        )
    except Exception:  # noqa: BLE001 -- assert ending, effects and fresh admission
        ended = True
    with sessions() as session:
        parents = tuple(
            session.scalars(
                select(Job).where(
                    Job.id.not_in(previous_jobs),
                    Job.kind == OPERATION_KIND,
                )
            )
        )
        assert len(parents) == 1
    if fault == "persistent":
        assert ended
        assert installed is None
        with sessions() as session:
            assert (
                session.scalar(
                    select(AgentOperation).where(
                        AgentOperation.kind == WireAgentOperation.RECIPE_INSTALL.value
                    )
                )
                is None
            )

        now[0] += timedelta(minutes=16)
        preparation.run_pending()
        now[0] += CANCEL_BUDGET
        preparation.run_pending()
        exhausted = preparation.get(parents[0].id)
        assert exhausted.result is None
        assert exhausted.state not in (
            LifecycleState.QUEUED,
            LifecycleState.RUNNING,
            LifecycleState.BACKOFF,
            LifecycleState.OBSERVING,
        )
        with sessions() as session:
            row = session.get(Job, parents[0].id)
            assert row is not None
            assert row.payload.get("claim_owner") is None
            assert row.payload.get("claim_until") is None
        blocked[0] = False
        request_id = "fresh-after-exhaustion"
        installed = service.install(
            reviewed,
            plan_digest=reviewed.plan_digest,
            actor="admin",
            request_id=request_id,
        )
    else:
        assert not ended
        assert preparation.get(parents[0].id).state == LifecycleState.SUCCEEDED
    assert installed is not None
    receipt = storage.read_receipt(evidence.oci_layout_sha256)
    assert receipt.image_digest == reviewed.image_digest
    assert installed.owner_id

    for node_id in _nodes:
        service.record_node_result(
            installed.id,
            node_id,
            succeeded=True,
            evidence=AgentInstallResult(installed_bytes=120).model_dump(mode="json"),
        )
    assert service.get(installed.id).state == LifecycleState.SUCCEEDED
    with sessions() as session:
        assert (
            session.scalar(
                select(AgentOperation).where(
                    AgentOperation.kind == WireAgentOperation.RECIPE_INSTALL.value
                )
            )
            is not None
        )
    assert (
        service.install(
            reviewed,
            plan_digest=reviewed.plan_digest,
            actor="admin",
            request_id=request_id,
        ).id
        == installed.id
    )


def test_a_bounded_install_blocker_keeps_the_specific_cause() -> None:
    detail = (
        "Controller-issued compiled execution plan is unavailable. "
        "compiled execution plan for spk_2818d189042b4c77aefa7796f4befd23 "
        "is unavailable: runtime image receipt identity is unavailable or malformed"
    )
    with pytest.raises(InstallAdmissionBusy) as error:
        require_install_admissible(
            _blocked_install_plan(
                (RuntimePreflightCode.HOST_CHANGED,), details=(detail,)
            )
        )
    assert "malformed" in str(error.value)
    require_install_admissible(replace(_blocked_install_plan(()), allowed=True))
