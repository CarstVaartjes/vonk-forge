from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from sqlalchemy import event, select
from vonk_control.install_admission import InstallAdmissionBusy
from vonk_control.models import AgentNode, Job, RecipeBuild, RecipeInstallation
from vonk_control.recipe_builds import RecipeBuildService

from .recipe_builds_support import setup as setup_build
from .recipe_operations_support import setup_services


def test_source_build_plan_writer_and_install_retry_share_builder_admission(
    tmp_path, postgres_engine
) -> None:
    sessions, operations, queue, mapping_id, installed_build_id, nodes = setup_services(
        tmp_path, engine=postgres_engine, model_artifact=True
    )
    build_sessions, bundles, build_now, builder_node_id, revision = setup_build(
        tmp_path,
        engine=postgres_engine,
        existing_node=True,
        builder_node_id=nodes[0],
        recipe_slug="qwen3-vllm-build-lock-race",
    )
    assert builder_node_id == nodes[0]
    builds = RecipeBuildService(build_sessions, bundles=bundles)
    prepared = builds.prepare_plan(revision.id, builder_node_id, now=build_now)
    install_plan = operations.preview_install(mapping_id, installed_build_id)
    assert install_plan.allowed

    with sessions() as session:
        ordinals_before = tuple(
            session.execute(
                select(AgentNode.node_id, AgentNode.workload_intent_ordinal).where(
                    AgentNode.node_id.in_(nodes)
                )
            )
        )
        existing_installations = tuple(session.scalars(select(RecipeInstallation)))
        existing_jobs = tuple(
            session.scalars(
                select(Job).where(Job.request_id == "source-build-install-request")
            )
        )
    assert existing_installations == ()
    assert existing_jobs == ()
    available_before = queue.available

    expected_key = f"vonk-admission:node:{builder_node_id}"
    owner_thread: int | None = None
    owner_lock = threading.Lock()
    owner_has_key = threading.Event()
    release_owner = threading.Event()
    key_attempt_threads: set[int] = set()

    def hold_source_build_key(
        _connection, _cursor, statement, parameters, _context, _many
    ) -> None:
        nonlocal owner_thread
        if "pg_try_advisory_xact_lock" not in statement:
            return
        values = parameters if isinstance(parameters, dict) else {}
        if values.get("key") != expected_key:
            return
        thread_id = threading.get_ident()
        with owner_lock:
            key_attempt_threads.add(thread_id)
            if owner_thread is None:
                owner_thread = thread_id
            owns_key = thread_id == owner_thread
        if owns_key:
            owner_has_key.set()
            assert release_owner.wait(timeout=10), (
                "test did not release the build writer"
            )

    def persist_source_build_plan():
        with build_sessions.begin() as session:
            return builds.persist_plan_in_session(session, prepared, now=build_now)

    install_request_key = str(uuid4())
    event.listen(postgres_engine, "after_cursor_execute", hold_source_build_key)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(persist_source_build_plan)
            try:
                assert owner_has_key.wait(timeout=10)
                try:
                    operations.install(
                        install_plan,
                        plan_digest=install_plan.plan_digest,
                        actor="admin",
                        request_id=install_request_key,
                    )
                except InstallAdmissionBusy:
                    pass
                else:
                    raise AssertionError(
                        "install was accepted while source-build admission held the node key"
                    )
                assert len(key_attempt_threads) == 2
                assert queue.available == available_before
                with sessions() as observer:
                    assert observer.get(RecipeBuild, prepared.build_id) is None
                    assert (
                        tuple(
                            observer.scalars(
                                select(RecipeInstallation).where(
                                    RecipeInstallation.mapping_id == mapping_id
                                )
                            )
                        )
                        == ()
                    )
                    assert (
                        observer.scalar(
                            select(Job.id).where(Job.request_id == install_request_key)
                        )
                        is None
                    )
                    assert (
                        tuple(
                            observer.execute(
                                select(
                                    AgentNode.node_id,
                                    AgentNode.workload_intent_ordinal,
                                ).where(AgentNode.node_id.in_(nodes))
                            )
                        )
                        == ordinals_before
                    )
            finally:
                release_owner.set()
            persisted = future.result(timeout=10)
    finally:
        release_owner.set()
        event.remove(postgres_engine, "after_cursor_execute", hold_source_build_key)

    assert persisted.build_id == prepared.build_id
    accepted = operations.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id=install_request_key,
    )
    assert accepted.kind == "recipe.install"
    with sessions() as observer:
        installations = tuple(
            observer.scalars(
                select(RecipeInstallation).where(
                    RecipeInstallation.mapping_id == mapping_id
                )
            )
        )
        install_jobs = tuple(
            observer.scalars(
                select(Job).where(
                    Job.kind == "recipe.install",
                    Job.request_id == install_request_key,
                )
            )
        )
        builds_after = tuple(
            observer.scalars(
                select(RecipeBuild).where(RecipeBuild.id == prepared.build_id)
            )
        )
    assert len(installations) == 1
    assert len(install_jobs) == 1
    assert len(builds_after) == 1 and builds_after[0].state == "planned"


def test_install_admission_ignores_a_row_lock_on_a_released_reservation(
    tmp_path, postgres_engine
) -> None:
    from datetime import UTC, datetime

    from sqlalchemy.orm import Session
    from vonk_agent_protocol import ReservationState
    from vonk_control.models import ResourceReservation

    sessions, operations, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, engine=postgres_engine, model_artifact=True
    )
    plan = operations.preview_install(mapping_id, build_id)
    assert plan.allowed
    now = datetime.now(UTC)
    with sessions.begin() as session:
        released = ResourceReservation(
            node_id=nodes[0],
            kind="disk",
            resource_key="history",
            amount_bytes=1,
            owner_kind="installation",
            owner_id=str(uuid4()),
            state=ReservationState.RELEASED,
            plan_digest="0" * 64,
            created_at=now,
            released_at=now,
        )
        session.add(released)
        session.flush()
        released_id = released.id

    # Retention or another writer is mid-transaction on the kept history row.
    with Session(postgres_engine) as holder, holder.begin():
        holder.scalars(
            select(ResourceReservation)
            .where(ResourceReservation.id == released_id)
            .with_for_update()
        ).all()
        request_id = str(uuid4())
        operations.install(
            plan,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id=request_id,
        )

    with sessions() as observer:
        assert observer.scalar(select(Job.id).where(Job.request_id == request_id))
        live_disk = tuple(
            observer.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.node_id == nodes[0],
                    ResourceReservation.kind == "disk",
                    ResourceReservation.state == ReservationState.ACTIVE,
                )
            )
        )
        assert live_disk and all(c.id != released_id for c in live_disk)
