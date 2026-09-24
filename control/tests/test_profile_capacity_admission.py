"""Profile acceptance rechecks capacity under its PostgreSQL admission fence."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import OperationalError
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import FleetProfileService
from vonk_control.models import (
    AgentNode,
    CatalogDocumentRevision,
    ClusterMapping,
    FleetProfileApplication,
    Job,
    NodeInventorySnapshot,
    RecipeBuild,
    ResourceReservation,
)
from vonk_forge_contracts import RecipeDefinition

from cluster_profiles.cli_render import render_payload

from .test_fleet_profile_api import _client, _headers
from .test_profile_installed_execution import _profile_service
from .test_recipe_operations import installed_recipe, setup_services, started_recipe


def _service_port(revision: CatalogDocumentRevision) -> int:
    recipe = RecipeDefinition.model_validate_json(json.dumps(revision.document))
    return next(item.port for item in recipe.interfaces if item.adapter == "openai")


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
    assert review.json()["allowed"] == runtime.allowed == (headroom == 0)
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
        "memory-inventory",
        "disk-inventory",
        "service-port",
        "rendezvous-port",
    ],
)
def test_load_refuses_capacity_lost_after_its_last_preview(
    tmp_path, postgres_engine, monkeypatch, change: str
) -> None:
    sessions, _, _, profile, api, headers, review, nodes = _capacity_profile(
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
            if change == "memory-inventory":
                snapshot.host_memory_free_bytes = snapshot.gpu_memory_free_bytes = 0
            elif change == "disk-inventory":
                snapshot.disk_free_bytes = 0
            elif change.endswith("-port"):
                revision = session.scalar(
                    select(CatalogDocumentRevision).where(
                        CatalogDocumentRevision.kind == "recipe"
                    )
                )
                assert revision is not None
                service_port = _service_port(revision)
                session.add(
                    ResourceReservation(
                        node_id=nodes[0],
                        kind="port",
                        resource_key=str(
                            29500 if change == "rendezvous-port" else service_port
                        ),
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
            "plan_digest": review["plan_digest"],
            "request_key": str(uuid4()),
        },
    )
    assert response.status_code == 409, response.text
    with sessions() as session:
        assert not tuple(session.scalars(select(FleetProfileApplication)))
        assert set(session.execute(select(Job.id, Job.state))) == original_jobs
        assert (
            set(
                session.execute(
                    select(AgentNode.node_id, AgentNode.workload_intent_ordinal)
                )
            )
            == original_intents
        )


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
        service_port = _service_port(revision)
        port = 29500 if port_kind == "rendezvous" else service_port
        session.add(
            ResourceReservation(
                node_id=nodes[0],
                kind="port",
                resource_key=str(port),
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
    expected = (
        "run.rendezvous_port_occupied"
        if port_kind == "rendezvous"
        else "run.port_occupied"
    )
    assert expected in {
        reason["code"]
        for item in response.json()["assessments"]
        for reason in item["assessment"]["blockers"]
    }


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
    assert "run.rendezvous_port_occupied" in {
        reason.code
        for item in changed.assessments
        for reason in item.assessment.blockers
    }


def test_admission_accepts_changed_headroom_without_reopening_storage_or_capabilities(
    tmp_path,
    postgres_engine,
    monkeypatch,
) -> None:
    sessions, _, planner, profile, api, headers, review, _ = _capacity_profile(
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
        monkeypatch.setattr(planner, "_model_capability_summary", unexpected)
        return original_queue(service, reviewed, **kwargs)

    monkeypatch.setattr(FleetProfileService, "_queue_application", change_observation)
    response = api.post(
        f"/api/profile/{profile.number}/load",
        headers=headers,
        json={
            "plan_digest": review["plan_digest"],
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
    sessions, profiles, _, profile, api, headers, review, nodes = _capacity_profile(
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
                "plan_digest": review["plan_digest"],
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


def test_profile_load_refuses_a_run_while_shared_node_admission_is_held(
    tmp_path, postgres_engine
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
            assert release_run.wait(timeout=10), "test did not release the run admission"

    def start_run():
        run_thread_ids.append(threading.get_ident())
        return lifecycle.start(
            run_plan,
            plan_digest=run_plan.plan_digest,
            actor="admin",
            request_id=str(uuid4()),
        )

    event.listen(
        postgres_engine, "after_cursor_execute", hold_after_run_admission_key
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(start_run)
            try:
                assert run_key_acquired.wait(timeout=10)
                response = api.post(
                    f"/api/profile/{profile.number}/load",
                    headers=headers,
                    json={
                        "plan_digest": review.json()["plan_digest"],
                        "request_key": request_key,
                    },
                )
                assert sum(key == expected_key for _, key in observed_keys) >= 2
                assert any(
                    thread_id != run_thread_ids[0] and key == expected_key
                    for thread_id, key in observed_keys
                )
                assert response.status_code == 409, response.text
                assert "busy" in response.json()["detail"].lower()
                with sessions() as session:
                    assert (
                        session.scalar(
                            select(FleetProfileApplication.id).where(
                                FleetProfileApplication.request_key == request_key
                            )
                        )
                        is None
                    )
                    assert set(
                        session.execute(
                            select(AgentNode.node_id, AgentNode.workload_intent_ordinal)
                        )
                    ) == intents_before
            finally:
                release_run.set()
            started = future.result(timeout=10)
    finally:
        release_run.set()
        event.remove(
            postgres_engine, "after_cursor_execute", hold_after_run_admission_key
        )

    assert started.id
    with sessions() as session:
        assert session.scalar(select(Job.id).where(Job.id == started.id)) is not None
        assert (
            session.scalar(
                select(FleetProfileApplication.id).where(
                    FleetProfileApplication.request_key == request_key
                )
            )
            is None
        )
