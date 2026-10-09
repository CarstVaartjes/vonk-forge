"""Profile acceptance rechecks capacity under its PostgreSQL admission fence."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import OperationalError
from vonk_agent_protocol import LifecycleState, canonical_message
from vonk_control.agent_jobs import AgentJobService
from vonk_control.fleet_profile_contract import (
    FleetProfileInput,
    FleetProfileSwitchAdapterState,
    profile_switch_child_request_key,
)
from vonk_control.fleet_profiles import (
    _MAX_PARKED_APPLICATION_OBSERVATIONS,
    FleetProfilePermissionDenied,
    FleetProfileService,
    FleetProfileStalePlanConflict,
)
from vonk_control.lifecycle.evidence import Residue
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    CatalogDocumentRevision,
    ClusterMapping,
    FleetProfileApplication,
    Job,
    NodeInventorySnapshot,
    RecipeBuild,
    ResourceReservation,
    User,
)
from vonk_control.platform_ports import ENDPOINT_HOST_PORTS, RENDEZVOUS_PORT
from vonk_control.run_switch_operations import RunSwitchOperationService
from vonk_control.settings import STORAGE_ADMISSION_WAIT_SECONDS
from vonk_control.storage_demands import (
    STORAGE_INSUFFICIENT,
)

from cluster_profiles.cli_render import render_payload

from .test_fleet_profile_api import _client, _headers
from .test_profile_installed_execution import _profile_service
from .test_recipe_operations import installed_recipe, setup_services, started_recipe


def _occupying_ports(
    revision: CatalogDocumentRevision, *, rendezvous: bool
) -> tuple[str, ...]:
    """The reservations that leave a node with no port for the recipe.

    The platform allocates any free authorised endpoint host port, so occupying
    the service takes every one of them; the rendezvous port is a single key.
    """

    if rendezvous:
        return (str(RENDEZVOUS_PORT),)
    return tuple(str(port) for port in ENDPOINT_HOST_PORTS)


def _capacity_profile(tmp_path, engine, *, node_count: int = 1):
    sessions, lifecycle, _, _, _, nodes = setup_services(
        tmp_path, engine=engine, nodes=node_count
    )
    profiles, planner = _profile_service(sessions, lifecycle)
    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe"
            )
        )
        assert revision is not None
        selector = f"{revision.publisher}/{revision.slug}"
    profile = profiles.create(
        FleetProfileInput.model_validate(
            {
                "name": "Reserve reviewed capacity",
                "assignments": [
                    {
                        "recipe_selector": selector,
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                    }
                ],
            }
        ),
        actor="admin",
    )
    api, codec = _client(sessions, profiles=profiles)
    headers = _headers(codec, "administrator")
    review = api.post(f"/api/profile/{profile.number}/preview", headers=headers)
    assert review.status_code == 200 and review.json()["allowed"], review.text
    return sessions, profiles, planner, profile, api, headers, review.json(), nodes


@pytest.mark.parametrize("headroom", [0, -1])
def test_unified_memory_review_and_runtime_share_the_same_capacity_boundary(
    tmp_path, postgres_engine, headroom: int
) -> None:
    sessions, _, planner, profile, api, headers, _, nodes = _capacity_profile(
        tmp_path, postgres_engine
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    with sessions() as session:
        mapping_id = session.scalar(select(ClusterMapping.id))
        build_id = session.scalar(select(RecipeBuild.id))
        assert mapping_id is not None and build_id is not None
    installed = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid4())
    )
    original = lifecycle.preview_run(installed.owner_id, "memory-boundary")
    demand = original.nodes[0].required_memory_bytes
    floor = original.nodes[0].memory_floor_bytes
    planner._memory_floor = floor
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        # Both views report the same available unified memory, despite their
        # different totals. Combining the smaller total with the larger used
        # amount invents occupied memory and wrongly refuses the boundary.
        snapshot.host_memory_total_bytes = demand + floor + 100
        snapshot.gpu_memory_total_bytes = demand + floor + 1_100
        snapshot.host_memory_free_bytes = demand + floor + headroom
        snapshot.gpu_memory_free_bytes = demand + floor + headroom
    runtime = lifecycle.preview_run(installed.owner_id, "memory-boundary")
    review = api.post(f"/api/profile/{profile.number}/preview", headers=headers)
    assert review.status_code == 200, review.text
    # No Vonk claim holds memory here, so a shortfall against the declared
    # envelope is an unverified fit, never a refusal; both views agree.
    assert review.json()["allowed"] is runtime.allowed is True
    fit = review.json()["assessments"][0]["assessment"]["fit_current"]["nodes"][0]
    assert fit["memory_free_after_bytes"] == runtime.nodes[0].free_after_bytes
    assert fit["memory_required_bytes"] == runtime.nodes[0].required_memory_bytes
    assert fit["memory_kind"] == runtime.nodes[0].memory_kind
    assert fit["memory_floor_bytes"] == runtime.nodes[0].memory_floor_bytes
    if headroom == 0:
        started = lifecycle.start(
            runtime,
            plan_digest=runtime.plan_digest,
            actor="admin",
            request_id=str(uuid4()),
        )
        with sessions() as session:
            claim = session.scalar(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id == started.owner_id,
                    ResourceReservation.kind == "unified-memory",
                    ResourceReservation.state == "active",
                )
            )
            assert claim is not None and claim.amount_bytes == demand


@pytest.mark.parametrize(
    "change",
    [
        "memory-reservation",
        "disk-reservation",
        # Not "memory-inventory": memory lost to something outside Vonk's claims
        # is a shortfall nothing here can wait out, so it is no longer parked.
        "disk-inventory",
        "service-port",
        "rendezvous-port",
    ],
)
def test_load_parks_capacity_lost_after_its_last_preview(
    tmp_path, postgres_engine, monkeypatch, change: str
) -> None:
    sessions, _, _, profile, api, headers, _review, nodes = _capacity_profile(
        tmp_path, postgres_engine, node_count=2 if change == "rendezvous-port" else 1
    )
    original_queue = FleetProfileService._queue_application
    with sessions() as session:
        original_jobs = set(session.execute(select(Job.id, Job.state)))
        original_intents = set(
            session.execute(
                select(AgentNode.node_id, AgentNode.workload_intent_ordinal)
            )
        )

    def lose_capacity(service, reviewed, **kwargs):
        with sessions.begin() as session:
            snapshot = session.scalar(select(NodeInventorySnapshot))
            assert snapshot is not None
            if change == "disk-inventory":
                snapshot.disk_free_bytes = 0
            elif change.endswith("-port"):
                revision = session.scalar(
                    select(CatalogDocumentRevision).where(
                        CatalogDocumentRevision.kind == "recipe"
                    )
                )
                assert revision is not None
                for key in _occupying_ports(
                    revision, rendezvous=change == "rendezvous-port"
                ):
                    session.add(
                        ResourceReservation(
                            node_id=nodes[0],
                            kind="port",
                            resource_key=key,
                            amount_bytes=0,
                            owner_kind="run",
                            owner_id=str(uuid4()),
                            state="active",
                            plan_digest="f" * 64,
                            created_at=service._clock(),
                        )
                    )
            else:
                kind = "disk" if change == "disk-reservation" else "unified-memory"
                session.add(
                    ResourceReservation(
                        node_id=nodes[0],
                        kind=kind,
                        resource_key="other-capacity",
                        amount_bytes=snapshot.disk_free_bytes
                        if kind == "disk"
                        else snapshot.host_memory_free_bytes,
                        owner_kind="recipe-build",
                        owner_id=str(uuid4()),
                        state="active",
                        plan_digest="f" * 64,
                        created_at=service._clock(),
                    )
                )
        return original_queue(service, reviewed, **kwargs)

    monkeypatch.setattr(FleetProfileService, "_queue_application", lose_capacity)
    response = api.post(
        f"/api/profile/{profile.number}/load",
        headers=headers,
        json={
            "request_key": str(uuid4()),
        },
    )
    # Lost capacity parks the accepted intent for automatic re-admission; it
    # issues no effect and claims no workload ordinal while it waits.
    assert response.status_code == 202, response.text
    with sessions() as session:
        (parked,) = tuple(session.scalars(select(FleetProfileApplication)))
        assert parked.state == "queued"
        assert parked.progress["admission_pending"] is True
        assert set(session.execute(select(Job.id, Job.state))) == original_jobs
        assert (
            set(
                session.execute(
                    select(AgentNode.node_id, AgentNode.workload_intent_ordinal)
                )
            )
            == original_intents
        )


def test_parked_load_names_a_failing_resource_recheck_and_proceeds_when_it_clears(
    tmp_path, postgres_engine, monkeypatch
) -> None:
    """A recheck that fails is shown with its cause, and the load resumes alone."""

    sessions, profiles, _, profile, api, headers, _review, _nodes = _capacity_profile(
        tmp_path, postgres_engine
    )
    original = RunSwitchOperationService.recheck_resources_in_session

    def defective(self, *args, **kwargs):
        raise ValueError("synthetic recheck defect")

    monkeypatch.setattr(
        RunSwitchOperationService, "recheck_resources_in_session", defective
    )
    response = api.post(
        f"/api/profile/{profile.number}/load",
        headers=headers,
        json={"request_key": str(uuid4())},
    )
    assert response.status_code == 202, response.text
    with sessions() as session:
        (parked,) = tuple(session.scalars(select(FleetProfileApplication)))
        assert parked.state == "queued" and parked.current_operation_id is None
        assert parked.progress["admission_pending"] is True
        (blocker,) = cast(list[dict[str, str]], parked.progress["blockers"])
        # Not the misleading "another change is using a Spark" busy code, and
        # the cause is in the reason.
        assert "ValueError: synthetic recheck defect" in blocker["detail"]
        application_id = parked.id

    monkeypatch.setattr(
        RunSwitchOperationService, "recheck_resources_in_session", original
    )
    for _ in range(6):
        profiles.tick()
        with sessions() as session:
            admitted = session.get(FleetProfileApplication, application_id)
            assert admitted is not None
            if admitted.current_operation_id is not None:
                break
    assert admitted.current_operation_id is not None, admitted.status_reason


@pytest.mark.parametrize("port_kind", ["service", "rendezvous"])
def test_fresh_installation_review_refuses_an_occupied_runtime_port(
    tmp_path, postgres_engine, port_kind: str
) -> None:
    sessions, profiles, _, profile, api, headers, _, nodes = _capacity_profile(
        tmp_path, postgres_engine, node_count=2 if port_kind == "rendezvous" else 1
    )
    with sessions.begin() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe"
            )
        )
        assert revision is not None
        for key in _occupying_ports(revision, rendezvous=port_kind == "rendezvous"):
            session.add(
                ResourceReservation(
                    node_id=nodes[0],
                    kind="port",
                    resource_key=key,
                    amount_bytes=0,
                    owner_kind="run",
                    owner_id=str(uuid4()),
                    state="active",
                    plan_digest="e" * 64,
                    created_at=profiles._clock(),
                )
            )
    response = api.post(f"/api/profile/{profile.number}/preview", headers=headers)
    assert response.status_code == 200, response.text
    assert not response.json()["allowed"]
    with sessions.begin() as session:
        for claim in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.kind == "port",
                ResourceReservation.node_id.in_(nodes),
            )
        ):
            claim.state = "released"
        session.flush()
    fresh = profiles.apply(profile.id, request_key=str(uuid4()), actor="admin")
    assert not fresh.progress.admission_pending


def test_review_reuses_only_ports_owned_by_its_exact_planned_stop(
    tmp_path, postgres_engine, capsys
) -> None:
    sessions, profiles, planner, profile, _, _, _, nodes = _capacity_profile(
        tmp_path, postgres_engine, node_count=2
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    with sessions() as session:
        mapping_id = session.scalar(select(ClusterMapping.id))
        build_id = session.scalar(select(RecipeBuild.id))
        assert mapping_id is not None and build_id is not None
    installation = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid4())
    )
    running = started_recipe(
        sessions,
        lifecycle,
        installation.owner_id,
        nodes,
        request_id=str(uuid4()),
        alias="prior-workload",
    )
    review = profiles.preview(profile.id)
    assert review.allowed, review.reasons
    assessment = review.assessments[0].assessment
    assert {stop.run_id for stop in assessment.stops} == {running.owner_id}
    assert not assessment.fit_current.allowed
    assert assessment.fit_after_stop is not None and assessment.fit_after_stop.allowed
    assert assessment.post_stop_memory_check is None
    with sessions.begin() as session:
        reservations = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id == running.owner_id,
                    ResourceReservation.kind == "port",
                    ResourceReservation.state == "active",
                )
            )
        )
        claimed = {(row.node_id, int(row.resource_key)) for row in reservations}
        reviewed = {
            (node.node_id, port)
            for node in assessment.fit_after_stop.nodes
            for port in node.ports_required
        }
        assert reviewed == claimed

    first_node = assessment.fit_current.nodes[0]
    assert first_node.memory_required_bytes is not None
    assert first_node.memory_floor_bytes is not None
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        # Keep total capacity feasible while the existing aggregate free
        # observation is too small. Review must bind the exact run stop and
        # defer the real memory fit to fresh post-stop inventory.
        snapshot.host_memory_free_bytes = (
            first_node.memory_required_bytes + first_node.memory_floor_bytes - 1
        )
        snapshot.gpu_memory_free_bytes = snapshot.host_memory_free_bytes
    conditional = profiles.preview(profile.id)
    assert conditional.allowed, conditional.reasons
    conditional_assessment = conditional.assessments[0].assessment
    assert conditional_assessment.fit_after_stop is None
    assert conditional_assessment.post_stop_memory_check is not None
    assert conditional_assessment.post_stop_memory_check.stop_run_ids == [
        running.owner_id
    ]
    assert conditional.admission_decisions[0].post_stop_memory_check == (
        conditional_assessment.post_stop_memory_check
    )
    render_payload(conditional.model_dump(mode="json"), "profile", action="preview")
    rendered = capsys.readouterr().out
    assert "recheck fresh capacity before preparing or starting" in rendered
    assert running.owner_id in rendered

    with sessions.begin() as session:
        # A matching identifier on another kind of owner is not authority to
        # borrow its port. The stop only releases this exact run's claims.
        rendezvous = session.scalar(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == running.owner_id,
                ResourceReservation.resource_key == "29500",
                ResourceReservation.state == "active",
            )
        )
        assert rendezvous is not None
        rendezvous.owner_kind = "unrelated"
    changed = profiles.preview(profile.id)
    assert not changed.allowed


def test_admission_accepts_changed_headroom_without_reopening_storage_or_capabilities(
    tmp_path,
    postgres_engine,
    monkeypatch,
) -> None:
    sessions, _, planner, profile, api, headers, _review, _ = _capacity_profile(
        tmp_path, postgres_engine
    )
    original_queue = FleetProfileService._queue_application

    def unexpected(*args, **kwargs):
        raise AssertionError("Admission opened an external inspection boundary")

    def change_observation(service, reviewed, **kwargs):
        with sessions.begin() as session:
            snapshot = session.scalar(select(NodeInventorySnapshot))
            assert snapshot is not None
            snapshot.host_memory_free_bytes -= 1
            snapshot.gpu_memory_free_bytes -= 1
            snapshot.disk_free_bytes -= 1
        monkeypatch.setattr(planner._artifacts, "inspect", unexpected)
        monkeypatch.setattr(planner, "_build_archive_available", unexpected)
        return original_queue(service, reviewed, **kwargs)

    monkeypatch.setattr(FleetProfileService, "_queue_application", change_observation)
    response = api.post(
        f"/api/profile/{profile.number}/load",
        headers=headers,
        json={
            "request_key": str(uuid4()),
        },
    )
    assert response.status_code == 202, response.text


@pytest.mark.parametrize("writer", ["inventory", "reservation"])
def test_capacity_writer_is_excluded_until_profile_acceptance_commits(
    tmp_path,
    postgres_engine,
    monkeypatch,
    writer: str,
) -> None:
    sessions, profiles, _, profile, api, headers, _review, nodes = _capacity_profile(
        tmp_path, postgres_engine
    )
    reservation_id = str(uuid4())
    with sessions.begin() as session:
        session.add(
            ResourceReservation(
                id=reservation_id,
                node_id=nodes[0],
                kind="unified-memory",
                resource_key="competing-capacity",
                amount_bytes=0,
                owner_kind="recipe-build",
                owner_id=str(uuid4()),
                state="active",
                plan_digest="e" * 64,
                created_at=profiles._clock(),
            )
        )
    checked = threading.Event()
    release = threading.Event()
    adapter = profiles._switch_adapter
    assert adapter is not None
    validate = adapter.validate_resources_in_session

    def pause_after_check(session, assignments, reviewed):
        validate(session, assignments, reviewed)
        checked.set()
        assert release.wait(5), "test did not release admission"

    monkeypatch.setattr(adapter, "validate_resources_in_session", pause_after_check)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            api.post,
            f"/api/profile/{profile.number}/load",
            headers=headers,
            json={
                "request_key": str(uuid4()),
            },
        )
        try:
            assert checked.wait(5)
            with (
                pytest.raises(OperationalError) as refused,
                sessions.begin() as session,
            ):
                session.execute(text("SET LOCAL lock_timeout = '100ms'"))
                if writer == "inventory":
                    row = session.scalar(select(NodeInventorySnapshot))
                    assert row is not None
                    row.host_memory_free_bytes = row.gpu_memory_free_bytes = 0
                else:
                    reservation = session.get(ResourceReservation, reservation_id)
                    assert reservation is not None
                    reservation.amount_bytes = 1_000_000
                session.flush()
            assert getattr(refused.value.orig, "sqlstate", None) == "55P03"
        finally:
            release.set()
        response = pending.result(timeout=5)
    assert response.status_code == 202, response.text
    with sessions() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        reservation = session.get(ResourceReservation, reservation_id)
        assert snapshot is not None and snapshot.host_memory_free_bytes > 0
        assert reservation is not None and reservation.amount_bytes == 0


def test_profile_load_parks_while_shared_node_admission_is_held(
    tmp_path, postgres_engine
) -> None:
    sessions, profiles, planner, profile, api, headers, _, nodes = _capacity_profile(
        tmp_path, postgres_engine
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    with sessions() as session:
        mapping_id = session.scalar(select(ClusterMapping.id))
        build_id = session.scalar(select(RecipeBuild.id))
        assert mapping_id is not None and build_id is not None
    installation = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid4())
    )
    review = api.post(f"/api/profile/{profile.number}/preview", headers=headers)
    assert review.status_code == 200 and review.json()["allowed"], review.text
    run_plan = lifecycle.preview_run(installation.owner_id, "shared-node-admission")
    assert run_plan.allowed, run_plan.nodes
    request_key = str(uuid4())
    with sessions() as session:
        intents_before = set(
            session.execute(
                select(AgentNode.node_id, AgentNode.workload_intent_ordinal)
            )
        )

    expected_key = f"vonk-admission:node:{nodes[0]}"
    run_thread_ids: list[int] = []
    run_key_acquired = threading.Event()
    release_run = threading.Event()
    observed_keys: list[tuple[int, str | None]] = []

    def hold_after_run_admission_key(
        _connection, _cursor, statement, parameters, _context, _many
    ):
        if "pg_try_advisory_xact_lock" in statement:
            values = parameters if isinstance(parameters, dict) else {}
            thread_id = threading.get_ident()
            key = values.get("key")
            observed_keys.append((thread_id, key))
        if (
            "pg_try_advisory_xact_lock" in statement
            and key == expected_key
            and run_thread_ids
            and thread_id == run_thread_ids[0]
        ):
            run_key_acquired.set()
            assert release_run.wait(timeout=10), (
                "test did not release the run admission"
            )

    def start_run():
        run_thread_ids.append(threading.get_ident())
        return lifecycle.start(
            run_plan,
            plan_digest=run_plan.plan_digest,
            actor="admin",
            request_id=str(uuid4()),
        )

    event.listen(postgres_engine, "after_cursor_execute", hold_after_run_admission_key)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(start_run)
            try:
                assert run_key_acquired.wait(timeout=10)
                response = api.post(
                    f"/api/profile/{profile.number}/load",
                    headers=headers,
                    json={
                        "request_key": request_key,
                    },
                )
                assert sum(key == expected_key for _, key in observed_keys) >= 2
                assert any(
                    thread_id != run_thread_ids[0] and key == expected_key
                    for thread_id, key in observed_keys
                )
                assert response.status_code == 202, response.text
                assert "busy" in response.json()["status_reason"].lower()
                with sessions() as session:
                    parked = session.scalar(
                        select(FleetProfileApplication).where(
                            FleetProfileApplication.request_key == request_key
                        )
                    )
                    assert parked is not None
                    assert parked.state == "queued"
                    assert parked.progress["admission_pending"] is True
                    assert (
                        set(
                            session.execute(
                                select(
                                    AgentNode.node_id, AgentNode.workload_intent_ordinal
                                )
                            )
                        )
                        == intents_before
                    )
            finally:
                release_run.set()
            started = future.result(timeout=10)
    finally:
        release_run.set()
        event.remove(
            postgres_engine, "after_cursor_execute", hold_after_run_admission_key
        )

    assert started.id
    # A parked, already-fenced retry must not wait for unrelated job writers.
    # The original whole-table admission fence treated routine worker DML as a
    # fleet-wide blocker even though this retry only needs its reviewed rows.
    unrelated_writer = sessions()
    try:
        unrelated_writer.execute(text("LOCK TABLE jobs IN ROW EXCLUSIVE MODE"))
        assert profiles.tick()
    finally:
        unrelated_writer.rollback()
        unrelated_writer.close()
    with sessions() as session:
        assert session.scalar(select(Job.id).where(Job.id == started.id)) is not None
        application = session.scalar(
            select(FleetProfileApplication).where(
                FleetProfileApplication.request_key == request_key
            )
        )
        assert application is not None


def test_pending_admission_reports_constraint_failure_and_recovers_same_request(
    tmp_path,
    postgres_engine,
) -> None:
    """A schema refusal must not spin every tick behind a stale busy message."""
    from datetime import timedelta

    sessions, profiles, _, profile, _, _, _, _ = _capacity_profile(
        tmp_path, postgres_engine
    )
    review = profiles.preview(profile.id)
    pending = profiles._create_pending_application(
        review,
        request_key=str(uuid4()),
        actor="admin",
        operation_kind="fleet-profile.apply",
    )
    profiles._defer_pending_application(
        pending.id, "initial contention", retry_delay=timedelta(0)
    )
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE resource_reservations DROP CONSTRAINT ck_reservations_state"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE resource_reservations ADD CONSTRAINT ck_reservations_state CHECK (state IN ('active','released','expired') AND amount_bytes>=0)"
            )
        )
    assert profiles.tick()
    with sessions() as session:
        row = session.get(FleetProfileApplication, pending.id)
        assert row is not None
        assert row.status_reason is not None
        assert "ck_reservations_state" in row.status_reason
        assert "23514" in row.status_reason
        assert row.progress["admission_pending"] is True
        retry = row.progress["admission_retry_at"]
        assert isinstance(retry, str)
        assert row.current_operation_id is None
    assert not profiles._observe_pending_admissions(profiles._clock())
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE resource_reservations DROP CONSTRAINT ck_reservations_state"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE resource_reservations ADD CONSTRAINT ck_reservations_state CHECK (state IN ('active','promised','released','expired') AND amount_bytes>=0)"
            )
        )
    from datetime import datetime

    profiles._clock = lambda: datetime.fromisoformat(retry)
    assert profiles._observe_pending_admissions(profiles._clock())
    with sessions() as session:
        row = session.get(FleetProfileApplication, pending.id)
        assert row is not None
        assert row.progress["admission_pending"] is False
        assert row.state == "queued"
        assert row.request_key == pending.request_key
        assert row.plan_digest == pending.plan_digest
        intended = profiles._intended_profile(row, session=session)
        assert not isinstance(intended, Residue)
        assert intended.reviewed_plan_digest == review.plan_digest


def test_due_pending_admission_is_not_hidden_by_newer_delayed_rows(
    tmp_path, postgres_engine
) -> None:
    """Only due rows consume the bounded newest-intent observation batch."""
    sessions, profiles, _, profile, _, _, _, _ = _capacity_profile(
        tmp_path, postgres_engine
    )
    review = profiles.preview(profile.id)
    due = profiles._create_pending_application(
        review,
        request_key=str(uuid4()),
        actor="admin",
        operation_kind="fleet-profile.apply",
    )
    profiles._defer_pending_application(
        due.id, "initial contention", retry_delay=timedelta(0)
    )

    delayed_profile = profiles.create(
        FleetProfileInput.model_validate({"name": "Delayed no-op", "assignments": []}),
        actor="admin",
    )
    delayed_review = profiles.preview(delayed_profile.id)
    assert delayed_review.allowed
    assert not delayed_review.steps

    original_clock = profiles._clock()
    delayed_rows = []
    for index in range(_MAX_PARKED_APPLICATION_OBSERVATIONS):
        later = original_clock + timedelta(seconds=index + 1)
        profiles._clock = lambda later=later: later
        delayed = profiles._create_pending_application(
            delayed_review,
            request_key=str(uuid4()),
            actor="admin",
            operation_kind="fleet-profile.apply",
        )
        profiles._defer_pending_application(
            delayed.id, "later request is not due", retry_delay=timedelta(hours=1)
        )
        delayed_rows.append(delayed.id)

    worker_now = original_clock + timedelta(seconds=len(delayed_rows))
    profiles._clock = lambda: worker_now
    assert profiles._observe_pending_admissions(worker_now)

    with sessions() as session:
        recovered = session.get(FleetProfileApplication, due.id)
        assert recovered is not None
        assert recovered.request_key == due.request_key
        assert recovered.state == "queued", recovered.status_reason
        assert recovered.progress["admission_pending"] is False
        assert recovered.current_operation_id is None
        for delayed_id in delayed_rows:
            delayed = session.get(FleetProfileApplication, delayed_id)
            assert delayed is not None
            assert delayed.progress["admission_pending"] is True


def test_profile_admission_recovers_after_agent_heartbeat_row_lock(
    tmp_path, postgres_engine
) -> None:
    """A live node contact parks admission, then restart dispatches same intent."""
    sessions, profiles, planner, profile, api, headers, review, nodes = (
        _capacity_profile(tmp_path, postgres_engine)
    )
    request_key = str(uuid4())
    with sessions() as session:
        before_ordinal = session.scalar(
            select(AgentNode.workload_intent_ordinal).where(
                AgentNode.node_id == nodes[0]
            )
        )
        before_jobs = tuple(
            row.id for row in session.scalars(select(Job).order_by(Job.id))
        )
        certificate = session.scalar(
            select(AgentCertificate).where(AgentCertificate.node_id == nodes[0])
        )
        assert before_ordinal is not None
        assert certificate is not None
        certificate_serial = certificate.serial

    heartbeat = sessions()
    try:
        node = heartbeat.get(
            AgentNode, nodes[0], with_for_update=True, populate_existing=True
        )
        certificate = heartbeat.get(AgentCertificate, certificate_serial)
        assert node is not None
        assert certificate is not None
        AgentJobService._record_contact(
            heartbeat,
            node,
            certificate,
            profiles._clock() + timedelta(seconds=1),
            None,
            None,
            None,
        )
        heartbeat.flush()

        response = api.post(
            f"/api/profile/{profile.number}/load",
            headers=headers,
            json={"request_key": request_key},
        )
        assert response.status_code == 202, response.text
        assert "busy" in response.json()["status_reason"].lower()

        with sessions() as session:
            pending = session.scalar(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.request_key == request_key
                )
            )
            assert pending is not None
            pending_id = pending.id
            pending_plan_digest = pending.plan_digest
            assert pending.state == "queued"
            assert pending.progress["admission_pending"] is True
            assert pending.current_operation_id is None
            assert (
                session.scalar(
                    select(AgentNode.workload_intent_ordinal).where(
                        AgentNode.node_id == nodes[0]
                    )
                )
                == before_ordinal
            )
            assert (
                tuple(row.id for row in session.scalars(select(Job).order_by(Job.id)))
                == before_jobs
            )
            due_at = pending.progress["admission_retry_at"]
            assert isinstance(due_at, str)
            due = datetime.fromisoformat(due_at)

        profiles._clock = lambda: due
        assert profiles.tick()
        with sessions() as session:
            deferred = session.get(FleetProfileApplication, pending_id)
            assert deferred is not None
            assert deferred.progress["admission_pending"] is True
            assert deferred.current_operation_id is None
            assert deferred.status_reason is not None
            assert "active workload owner" in deferred.status_reason.lower()
            next_retry_raw = deferred.progress["admission_retry_at"]
            assert isinstance(next_retry_raw, str)
            next_retry = datetime.fromisoformat(next_retry_raw)
            assert next_retry > due
            assert (
                tuple(row.id for row in session.scalars(select(Job).order_by(Job.id)))
                == before_jobs
            )
    finally:
        heartbeat.commit()
        heartbeat.close()

    assert planner._lifecycle is not None
    restarted, _ = _profile_service(sessions, planner._lifecycle)
    restarted._clock = lambda: next_retry + timedelta(seconds=1)
    assert restarted.tick()

    with sessions() as session:
        recovered = session.get(FleetProfileApplication, pending_id)
        assert recovered is not None
        assert recovered.request_key == request_key
        assert recovered.plan_digest == pending_plan_digest
        assert recovered.progress["admission_pending"] is False
        intended = restarted._intended_profile(recovered, session=session)
        assert not isinstance(intended, Residue)
        assert intended.reviewed_plan_digest == review["plan_digest"]
        switch_state = FleetProfileSwitchAdapterState.model_validate_json(
            canonical_message(recovered.progress["switch_adapter"])
        )
        assert switch_state.pending_children
        for pending_child in switch_state.pending_children:
            item = switch_state.queue[pending_child.queue_index]
            assert pending_child.kind == item.kind
            expected_child_request = profile_switch_child_request_key(
                recovered.id, pending_child.queue_index, item.kind, item.id
            )
            child = session.scalar(
                select(Job).where(Job.request_id == expected_child_request)
            )
            assert child is not None
            assert child.id == pending_child.operation_id


def test_queued_admission_survives_submitter_death_before_first_attempt(
    tmp_path, postgres_engine
) -> None:
    """A committed pending intent must survive death before defer or admission."""
    sessions, profiles, planner, profile, _, _, _, _ = _capacity_profile(
        tmp_path, postgres_engine
    )
    review = profiles.preview(profile.id)
    pending = profiles._create_pending_application(
        review,
        request_key=str(uuid4()),
        actor="admin",
        operation_kind="fleet-profile.apply",
    )
    retry = pending.progress.admission_retry_at
    assert retry is not None
    # Reconstruct the service with no submitter memory, as after worker restart.
    assert planner._lifecycle is not None
    restarted, _ = _profile_service(sessions, planner._lifecycle)
    restarted._clock = lambda: retry
    assert restarted.tick()
    with sessions() as session:
        row = session.get(FleetProfileApplication, pending.id)
        assert row is not None
        assert row.progress["admission_pending"] is False
        assert row.plan_digest == pending.plan_digest
        restarted_intent = restarted._intended_profile(row, session=session)
        assert not isinstance(restarted_intent, Residue)
        assert restarted_intent.reviewed_plan_digest == review.plan_digest
        assert row.state == "running", row.status_reason
        assert row.current_operation_id is not None


@pytest.mark.parametrize("authority_change", ["disabled", "demoted", "removed"])
def test_pending_admission_continues_under_platform_authority_after_actor_revocation(
    tmp_path, postgres_engine, authority_change: str
) -> None:
    """Accepted recovery fences work; revocation only denies new user changes."""
    sessions, profiles, planner, profile, api, headers, _, nodes = _capacity_profile(
        tmp_path, postgres_engine
    )
    review = profiles.preview(profile.id)
    pending = profiles._create_pending_application(
        review,
        request_key=str(uuid4()),
        actor="admin",
        operation_kind="fleet-profile.apply",
    )
    retry_at = pending.progress.admission_retry_at
    assert retry_at is not None

    with sessions() as session:
        before_ordinal = session.scalar(
            select(AgentNode.workload_intent_ordinal).where(
                AgentNode.node_id == nodes[0]
            )
        )
        before_jobs = set(session.scalars(select(Job.id)))
        assert before_ordinal is not None
    with sessions.begin() as session:
        author = session.scalar(
            select(User).where(User.subject == "admin").with_for_update()
        )
        assert author is not None
        if authority_change == "removed":
            session.delete(author)
        elif authority_change == "disabled":
            author.disabled_at = retry_at
        else:
            author.role = "viewer"

    # Reconstruct the worker so admission cannot depend on cached user authority.
    assert planner._lifecycle is not None
    restarted, _ = _profile_service(sessions, planner._lifecycle)
    restarted._clock = lambda: retry_at
    assert restarted.tick()

    with sessions() as session:
        row = session.get(FleetProfileApplication, pending.id)
        assert row is not None
        assert row.request_key == pending.request_key
        assert row.plan_digest == pending.plan_digest
        assert row.state == "running", row.status_reason
        assert row.progress["admission_pending"] is False
        ordinal = row.progress["workload_intent_ordinal"]
        assert ordinal == before_ordinal + 1
        assert dict(
            session.execute(
                select(AgentNode.node_id, AgentNode.workload_intent_ordinal).where(
                    AgentNode.node_id.in_(nodes)
                )
            ).all()
        ) == dict.fromkeys(nodes, ordinal)
        assert row.current_operation_id is not None
        switch_state = FleetProfileSwitchAdapterState.model_validate_json(
            canonical_message(row.progress["switch_adapter"])
        )
        assert switch_state.pending_children
        for pending_child in switch_state.pending_children:
            item = switch_state.queue[pending_child.queue_index]
            expected_request = profile_switch_child_request_key(
                row.id, pending_child.queue_index, item.kind, item.id
            )
            child = session.get(Job, pending_child.operation_id)
            assert child is not None
            assert child.id not in before_jobs
            assert child.request_id == expected_request
            assert child.payload["workload_intent_ordinal"] == ordinal
            assert set(child.targets) == set(nodes)
        admitted_jobs = set(session.scalars(select(Job.id)))

    # Platform maintenance does not grant the revoked non-admin new authority.
    denied_request_key = str(uuid4())
    with pytest.raises(
        FleetProfilePermissionDenied, match="Current profile authority is unavailable"
    ):
        restarted.apply(profile.id, request_key=denied_request_key, actor="admin")
    with sessions() as session:
        assert (
            session.scalar(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.request_key == denied_request_key
                )
            )
            is None
        )
        assert set(session.scalars(select(Job.id))) == admitted_jobs
        node = session.get(AgentNode, nodes[0])
        assert node is not None and node.workload_intent_ordinal == ordinal
        row = session.get(FleetProfileApplication, pending.id)
        assert row is not None and row.state == "running"

    # The denial leaves no gate against a fresh change by a current administrator.
    changed = api.put(
        f"/api/profile/{profile.number}",
        headers=headers,
        json={
            "name": "Fresh authorized change",
            "assignments": [],
            "expected_revision": profile.revision,
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["revision"] == profile.revision + 1


def test_late_submitter_cleanup_preserves_superseded_receipt(
    tmp_path, postgres_engine, monkeypatch
) -> None:
    """A stale submitter cannot delete a receipt cancelled during admission."""
    sessions, profiles, _, profile, _, _, _, _ = _capacity_profile(
        tmp_path, postgres_engine
    )
    request_key = str(uuid4())

    def cancel_before_refusal(_preview, *, pending_application_id, **_kwargs):
        with sessions.begin() as session:
            row = session.get(
                FleetProfileApplication,
                pending_application_id,
                with_for_update=True,
            )
            assert row is not None
            row.state = "cancelled"
            row.status_reason = "newer profile intent superseded this request"
        raise FleetProfileStalePlanConflict("pending intent was superseded")

    monkeypatch.setattr(profiles, "_queue_application", cancel_before_refusal)

    result = profiles.apply(
        profile.id,
        request_key=request_key,
        actor="admin",
    )

    assert result.state == "cancelled"
    assert result.status_reason == "newer profile intent superseded this request"
    with sessions() as session:
        row = session.scalar(
            select(FleetProfileApplication).where(
                FleetProfileApplication.request_key == request_key
            )
        )
        assert row is not None
        assert row.state == "cancelled"
        assert row.request_key == request_key


@pytest.mark.parametrize("winner", ["admitted", "cancelled"])
@pytest.mark.parametrize("late_outcome", ["defer", "finish", "discard"])
def test_late_admission_outcome_preserves_concurrent_winner(
    tmp_path, postgres_engine, winner, late_outcome
) -> None:
    """A losing observer cannot resurrect cancelled work or undo admission."""
    sessions, profiles, _, profile, _, _, _, _ = _capacity_profile(
        tmp_path, postgres_engine
    )
    review = profiles.preview(profile.id)
    pending = profiles._create_pending_application(
        review,
        request_key=str(uuid4()),
        actor="admin",
        operation_kind="fleet-profile.apply",
    )
    attempted = threading.Event()

    def observe_read(connection, cursor, statement, parameters, context, executemany):
        if "SELECT fleet_profile_applications" in statement:
            attempted.set()

    def report_late_outcome():
        if late_outcome == "defer":
            profiles._defer_pending_application(
                pending.id, "obsolete observer contention"
            )
        elif late_outcome == "discard":
            profiles._discard_pending_application(pending.id)
        else:
            profiles._finish_pending_admission(
                pending.id,
                state=LifecycleState.FAILED,
                reason="obsolete observer failure",
            )

    owner = sessions()
    try:
        row = owner.get(FleetProfileApplication, pending.id, with_for_update=True)
        assert row is not None
        row.state = "queued" if winner == "admitted" else "cancelled"
        row.progress = {**row.progress, "admission_pending": winner != "admitted"}
        row.status_reason = "concurrent owner won"
        owner.flush()
        event.listen(postgres_engine, "before_cursor_execute", observe_read)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(report_late_outcome)
            try:
                assert attempted.wait(timeout=5)
            finally:
                owner.commit()
            future.result(timeout=5)
    finally:
        owner.rollback()
        owner.close()
        event.remove(postgres_engine, "before_cursor_execute", observe_read)
    with sessions() as session:
        current = session.get(FleetProfileApplication, pending.id)
        assert current is not None
        assert current.state == ("queued" if winner == "admitted" else "cancelled")
        assert current.progress["admission_pending"] == (winner != "admitted")
        assert current.status_reason == "concurrent owner won"


# -- disk the load waits for ----------------------------------------------------


class _Relief:
    """A storage reclaimer's answer, recording what the waiting load asked for."""

    def __init__(self, code: str, *, paused: bool = False) -> None:
        self.code = code
        self.paused = paused
        self.asked: list[tuple[str, int, str]] = []

    def __call__(self, node_id, required_free_bytes, *, source, subject, reason):
        from vonk_control.storage_demands import StorageRelief

        self.asked.append((node_id, required_free_bytes, source))
        return StorageRelief(
            self.code,
            required_free_bytes,
            0,
            f"Needs {required_free_bytes} more free bytes; the loaded profile "
            "holds the rest.",
            paused=self.paused,
        )


def _load_short_of_disk(tmp_path, monkeypatch, relief: _Relief):
    """A load whose Spark loses its free disk after the review accepted it."""

    sessions, profiles, _, profile, api, headers, _review, nodes = _capacity_profile(
        tmp_path, None
    )
    profiles.bind_storage_relief(relief)
    original_queue = FleetProfileService._queue_application

    def lose_disk(service, reviewed, **kwargs):
        with sessions.begin() as session:
            snapshot = session.scalar(select(NodeInventorySnapshot))
            assert snapshot is not None
            snapshot.disk_free_bytes = 0
        return original_queue(service, reviewed, **kwargs)

    monkeypatch.setattr(FleetProfileService, "_queue_application", lose_disk)
    response = api.post(
        f"/api/profile/{profile.number}/load",
        headers=headers,
        json={"request_key": str(uuid4())},
    )
    assert response.status_code == 202, response.text
    monkeypatch.setattr(FleetProfileService, "_queue_application", original_queue)
    return sessions, profiles, nodes


def _only_application(sessions) -> FleetProfileApplication:
    with sessions() as session:
        (application,) = tuple(session.scalars(select(FleetProfileApplication)))
        return application


def _later(profiles, seconds: int) -> None:
    moved = profiles._clock() + timedelta(seconds=seconds)
    profiles._clock = lambda: moved


def test_admission_short_of_disk_asks_for_the_bytes_and_names_what_it_waits_for(
    tmp_path, monkeypatch
) -> None:
    """The parked load asks the collector for exactly the shortfall (it used to
    only retry), shows eviction as its typed blocker, and resumes alone once the
    Spark has room."""

    relief = _Relief("storage.evicting")
    sessions, profiles, nodes = _load_short_of_disk(tmp_path, monkeypatch, relief)

    parked = _only_application(sessions)
    assert parked.state == "queued" and parked.current_operation_id is None
    assert parked.progress["admission_pending"] is True
    (blocker,) = cast(list[dict[str, object]], parked.progress["blockers"])
    assert blocker["node_ids"] == [nodes[0]]
    assert parked.progress["storage_wait_since"] is not None
    ((node_id, needed, source),) = relief.asked
    assert (node_id, source) == (nodes[0], "profile-load") and needed > 0

    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.disk_free_bytes = snapshot.disk_total_bytes
    _later(profiles, 90)
    for _ in range(6):
        profiles.tick()
        if _only_application(sessions).current_operation_id is not None:
            break
    admitted = _only_application(sessions)
    assert admitted.current_operation_id is not None, admitted.status_reason
    assert admitted.progress["blockers"] == []
    assert admitted.progress["storage_wait_since"] is None


def test_admission_ends_with_a_typed_refusal_when_nothing_more_can_be_freed(
    tmp_path, monkeypatch
) -> None:
    """Catches retrying for ever (86 attempts and counting) against a Spark
    whose space eviction cannot free: the load fails once, naming what holds it."""

    relief = _Relief(STORAGE_INSUFFICIENT)
    sessions, profiles, nodes = _load_short_of_disk(tmp_path, monkeypatch, relief)

    refused = _only_application(sessions)
    assert refused.state == "failed"
    assert refused.progress["admission_pending"] is False
    assert refused.progress["admission_retry_at"] is None
    (blocker,) = cast(list[dict[str, object]], refused.progress["blockers"])
    assert blocker["node_ids"] == [nodes[0]]
    assert "the loaded profile holds the rest" in str(blocker["detail"])
    assert refused.current_operation_id is None
    fresh = profiles.apply(refused.profile_id, request_key=str(uuid4()), actor="admin")
    assert fresh.id != refused.id


def test_admission_keeps_waiting_while_eviction_is_paused_then_refuses_after_the_bound(
    tmp_path, monkeypatch
) -> None:
    """A paused eviction has a bounded wait; ending releases admission ownership
    and preserves the observed shortfall while a fresh load is admitted."""

    relief = _Relief(STORAGE_INSUFFICIENT, paused=True)
    sessions, profiles, nodes = _load_short_of_disk(tmp_path, monkeypatch, relief)
    assert _only_application(sessions).state == "queued"

    _later(profiles, 600)
    profiles.tick()
    still = _only_application(sessions)
    assert (
        still.state == "queued" and cast(int, still.progress["admission_attempt"]) >= 2
    )

    _later(profiles, STORAGE_ADMISSION_WAIT_SECONDS + 600)
    profiles.tick()
    ended = profiles.application(_only_application(sessions).id)
    assert ended.state == LifecycleState.CANCELLED
    assert ended.current_operation_id is None
    assert not ended.progress.admission_pending
    assert ended.progress.admission_retry_at is None
    assert any(item.node_ids == [nodes[0]] for item in ended.progress.blockers)

    from types import SimpleNamespace

    from .non_blocking import assert_ended_without_blocking

    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        ended,
        end=lambda receipt: receipt,
        fresh=lambda _world: profiles.apply(
            ended.profile_id, request_key=str(uuid4()), actor="admin"
        ),
    )
