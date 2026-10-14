"""A profile load is admitted whatever promises earlier work left on its ports.

Production (2026-10-13): a load placing two assignments on one Spark promised
both the same endpoint port; admission met the unique promise index and the
retained load retried the same plan forever.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, update
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.models import (
    CatalogDocumentRevision,
    FleetProfileApplication,
    ResourceReservation,
)
from vonk_control.platform_ports import ENDPOINT_HOST_PORTS
from vonk_control.run_switch_operations import RunSwitchOperationService

from .test_profile_installed_execution import _drive_to_job, _profile_service
from .test_profile_port_claims import _ready_profile
from .test_recipe_operations import NOW, setup_services, started_recipe


def _selector(sessions) -> str:
    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe"
            )
        )
        assert revision is not None
        return f"{revision.publisher}/{revision.slug}"


def _two_on_one_spark(tmp_path, engine):
    sessions, lifecycle, _, _, _, nodes = setup_services(
        tmp_path, engine=engine, nodes=1
    )
    profiles, _ = _profile_service(sessions, lifecycle)
    selector = _selector(sessions)
    profile = profiles.create(
        FleetProfileInput.model_validate(
            {
                "name": "Two on one Spark",
                "assignments": [
                    {
                        "recipe_selector": selector,
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "assignment_name": name,
                    }
                    for name in ("first", "second")
                ],
            }
        ),
        actor="admin",
    )
    return sessions, profiles, profile


def _promises(sessions, owner_id):
    with sessions() as session:
        return {
            (claim.node_id, claim.resource_key)
            for claim in session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == owner_id,
                    ResourceReservation.kind == "port",
                    ResourceReservation.state == "promised",
                )
            )
        }


def _admitted(sessions, application_id) -> bool:
    with sessions() as session:
        row = session.get(FleetProfileApplication, application_id)
        assert row is not None
        return row.state in {"queued", "running"} and not (row.progress or {}).get(
            "admission_pending", False
        )


def test_assignments_sharing_a_spark_are_promised_distinct_ports(
    tmp_path, postgres_engine
):
    sessions, profiles, profile = _two_on_one_spark(tmp_path, postgres_engine)
    review = profiles.preview(profile.id)
    assert review.allowed
    planned = [
        (node.node_id, port)
        for decision in review.admission_decisions
        for node in decision.requirements
        for port in node.ports_required
    ]
    assert len(planned) == len(set(planned)) == 2

    view = profiles.apply(profile.id, request_key=str(uuid4()), actor="admin")

    assert _admitted(sessions, view.id)
    assert {port for _, port in _promises(sessions, view.id)} == {
        str(port) for port in ENDPOINT_HOST_PORTS
    }


def test_retained_load_whose_plan_promises_one_port_twice_heals(
    tmp_path, postgres_engine, monkeypatch
):
    sessions, profiles, profile = _two_on_one_spark(tmp_path, postgres_engine)
    # Fault: the review of an older Controller gave both assignments one port.
    inspect = RunSwitchOperationService.inspect_request
    with monkeypatch.context() as patched:
        patched.setattr(
            RunSwitchOperationService,
            "inspect_request",
            lambda self, request, **kwargs: inspect(
                self, request, **{**kwargs, "planned_ports": frozenset()}
            ),
        )
        retained = profiles.apply(profile.id, request_key=str(uuid4()), actor="admin")
    assert not _admitted(sessions, retained.id)

    for step in range(1, 7):
        # The retained load retries on its own schedule, minutes apart.
        profiles._clock = lambda step=step: NOW + timedelta(minutes=step)
        profiles.tick()
        if _admitted(sessions, retained.id):
            break

    assert _admitted(sessions, retained.id)
    assert {port for _, port in _promises(sessions, retained.id)} == {
        str(port) for port in ENDPOINT_HOST_PORTS
    }


def test_promise_left_by_ended_load_does_not_block_and_is_released(
    tmp_path, postgres_engine
):
    sessions, profiles, _, profile, api, headers, nodes, _ = _ready_profile(
        tmp_path, postgres_engine
    )
    ended = profiles.apply(profile.id, request_key=str(uuid4()), actor="admin")
    (left,) = _promises(sessions, ended.id)
    with sessions.begin() as session:
        # An older Controller ended the load without releasing its promise.
        session.execute(
            update(FleetProfileApplication)
            .where(FleetProfileApplication.id == ended.id)
            .values(state="cancelled")
        )
        # Every other endpoint port is bound by a live run.
        for port in ENDPOINT_HOST_PORTS:
            if str(port) != left[1]:
                session.add(
                    ResourceReservation(
                        node_id=nodes[0],
                        kind="port",
                        resource_key=str(port),
                        amount_bytes=0,
                        owner_kind="run",
                        owner_id=str(uuid4()),
                        state="active",
                        plan_digest="d" * 64,
                        created_at=datetime.now(UTC),
                    )
                )

    review = api.post(f"/api/profile/{profile.number}/preview", headers=headers)
    assert review.json()["allowed"], review.text
    loaded = profiles.apply(profile.id, request_key=str(uuid4()), actor="admin")

    assert _admitted(sessions, loaded.id)
    assert _promises(sessions, loaded.id) == {left}
    assert not _promises(sessions, ended.id)


def test_replacement_of_a_load_with_issued_work_is_admitted_on_its_port(
    tmp_path, postgres_engine
):
    sessions, profiles, planner, profile, _, _, nodes, installation_id = _ready_profile(
        tmp_path, postgres_engine
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    started_recipe(
        sessions,
        lifecycle,
        installation_id,
        nodes,
        request_id=str(uuid4()),
        alias="previous",
    )
    earlier = profiles.apply(profile.id, request_key=str(uuid4()), actor="admin")
    (port,) = _promises(sessions, earlier.id)
    # The earlier load has issued its replacement work.
    _drive_to_job(profiles, planner, sessions, "recipe.stop")

    replacement = profiles.apply(profile.id, request_key=str(uuid4()), actor="admin")

    assert _admitted(sessions, replacement.id)
    assert _promises(sessions, replacement.id) == {port}
    assert not _promises(sessions, earlier.id)


def test_profile_run_takes_its_own_promise_not_its_siblings(tmp_path, postgres_engine):
    sessions, profiles, planner, profile, _, _, nodes, installation_id = _ready_profile(
        tmp_path, postgres_engine
    )
    first, second = (str(port) for port in ENDPOINT_HOST_PORTS)
    with sessions.begin() as session:
        # A run still binds the first port while the load is reviewed.
        held = ResourceReservation(
            node_id=nodes[0],
            kind="port",
            resource_key=first,
            amount_bytes=0,
            owner_kind="run",
            owner_id=str(uuid4()),
            state="active",
            plan_digest="d" * 64,
            created_at=datetime.now(UTC),
        )
        session.add(held)
    alias = profiles.preview(profile.id).resolved_assignments[0].alias
    assert alias is not None
    loaded = profiles.apply(profile.id, request_key=str(uuid4()), actor="admin")
    assert _promises(sessions, loaded.id) == {(nodes[0], second)}
    with sessions.begin() as session:
        # That run ended, and the same load promised the first port to
        # another of its assignments on this Spark.
        ended = session.get(ResourceReservation, held.id)
        application = session.get(FleetProfileApplication, loaded.id)
        assert ended is not None and application is not None
        ended.state = "released"
        session.add(
            ResourceReservation(
                node_id=nodes[0],
                kind="port",
                resource_key=first,
                amount_bytes=0,
                owner_kind="fleet-profile",
                owner_id=loaded.id,
                state="promised",
                plan_digest=application.plan_digest,
                created_at=datetime.now(UTC),
            )
        )

    lifecycle = planner._lifecycle
    assert lifecycle is not None
    plan = lifecycle._run_admission.plan_run(
        installation_id, alias, now=NOW, profile_application_id=loaded.id
    )

    assert [str(node.port) for node in plan.nodes] == [second]
