from vonk_agent_protocol import (
    ResourceBlockerCode,
    RunAdmissionCode,
    RunState,
    canonical_message,
)
from vonk_agent_protocol.swap import GIB

"""Admission bounds a recipe's load by what its runs really used."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, update
from vonk_control.models import (
    CatalogDocumentRevision,
    InstallationNode,
    MeasuredRolePeak,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from vonk_control.observed_run_memory import observed_role_peaks
from vonk_control.run_admission import RunAdmissionService
from vonk_control.telemetry import TelemetryRepository, TelemetrySample

from .test_load_phase_swap_admission import _FREE as _LIVE_FREE
from .test_load_phase_swap_admission import (
    _accept,
    _codes,
    _inventory,
    _observe_rise,
    _swap,
    _swap_wait,
    _world,
)
from .test_run_admission import setup

# The fixture Spark: 300 bytes free at admission, a declared peak of 225 and a
# 50-byte platform floor, so the declaration alone fits with 25 to spare.
_FREE = 300
_FLOOR = 50


def _report(sessions, node, observed_at, lowest_available):
    """One telemetry sample from the Spark while its run serves."""
    TelemetryRepository(sessions, clock=lambda: observed_at).record_batch(
        node,
        (
            TelemetrySample.model_validate_json(
                canonical_message(
                    {
                        "boot_id": str(uuid4()),
                        "observed_at": observed_at,
                        "memory_total_bytes": 1000,
                        "memory_available_bytes": lowest_available,
                        "disk_total_bytes": None,
                        "disk_free_bytes": None,
                        "gpu_utilization_percent": None,
                        "gpu_memory_total_bytes": None,
                        "gpu_memory_free_bytes": None,
                    }
                )
            ),
        ),
    )


def _serve(sessions, now, node, installation, *, lowest_available, other_claim=None):
    """Admit one run, report telemetry while it serves, then stop it."""
    service = RunAdmissionService(
        sessions, inventory_max_age=300, memory_floor_bytes=_FLOOR
    )
    run_id = service.accept_run(
        service.plan_run(installation, alias="qwen", now=now), actor="admin", now=now
    )
    if other_claim is not None:
        owner_kind, state = other_claim
        with sessions.begin() as session:
            session.add(
                ResourceReservation(
                    node_id=node,
                    kind="unified-memory",
                    resource_key="other",
                    amount_bytes=1,
                    owner_kind=owner_kind,
                    owner_id=str(uuid4()),
                    state=state,
                    plan_digest="f" * 64,
                    created_at=now,
                )
            )
    _report(sessions, node, now + timedelta(seconds=1), lowest_available)
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run.state = "stopped"
        run.stopped_at = now
        rank = session.scalar(select(RunNode).where(RunNode.run_id == run_id))
        assert rank is not None
        rank.state = "stopped"
        observed = rank.observed_memory_bytes
        for claim in session.scalars(
            select(ResourceReservation).where(ResourceReservation.state == "active")
        ):
            claim.state = "released"
            claim.released_at = now
    return service, observed


def test_a_run_over_its_declaration_is_bounded_by_held_swap_then_live_memory(
    tmp_path, postgres_engine
) -> None:
    """Catches admission trusting a declaration that real use already exceeded.

    The 2026-10-11 freeze: a recipe whose declared peak fit with a thin margin
    really used more, and the Spark thrashed. The whole-run peak no longer
    raises the reservation (a swap-backed load poisoned its content that way,
    2026-10-28). What protects a second load beside it today: while the first
    still loads, its admitted rise holds the swap; once it runs, its real use
    is in the Spark's live occupied memory.
    """
    steady, real = 50 * GIB, 80 * GIB
    sessions, now, node, installation, service = _world(
        tmp_path, postgres_engine, steady
    )
    at = _observe_rise(
        sessions, service, installation, node, now, steady=steady, rise=real - steady
    )
    _inventory(sessions, at, node, swap=_swap(64))
    first = _accept(
        sessions, service, service.plan_run(installation, alias="a", now=at), at
    )
    with sessions.begin() as session:
        session.execute(
            update(RunNode)
            .where(RunNode.run_id == first)
            .values(state=RunState.STARTING)
        )
    _swap_wait(service.plan_run(installation, alias="b", now=at))

    running = at + timedelta(seconds=1)
    with sessions.begin() as session:
        session.execute(
            update(RunNode)
            .where(RunNode.run_id == first)
            .values(state=RunState.RUNNING)
        )
    _inventory(sessions, running, node, free=_LIVE_FREE - real, swap=_swap(64))
    second = service.plan_run(installation, alias="b", now=running)
    assert not second.allowed
    assert RunAdmissionCode.INSUFFICIENT_MEMORY in _codes(second)
    assert ResourceBlockerCode.DEMAND_EXCEEDS_CAPACITY not in _codes(second)


def test_real_use_below_the_declaration_never_loosens_admission(tmp_path) -> None:
    """Catches a served run's lighter use shrinking the creator's contract."""
    sessions, now, node, installation = setup(tmp_path, free_memory=_FREE)
    service, observed = _serve(
        sessions, now, node, installation, lowest_available=_FREE - 100
    )
    assert observed == 100

    plan = service.plan_run(installation, alias="qwen", now=now)
    assert plan.allowed and plan.nodes[0].required_memory_bytes == 225


@pytest.mark.parametrize(
    ("other_claim", "attributed"),
    [(("recipe-build", "active"), False), (("fleet-profile", "promised"), True)],
)
def test_only_a_sole_active_claim_owner_is_charged_with_the_use(
    tmp_path, other_claim, attributed
) -> None:
    """Catches charging a builder's memory to the run, or ignoring a run whose
    neighbour holds only a promise (no memory until its own run starts)."""
    sessions, now, node, installation = setup(tmp_path, free_memory=_FREE)
    _, observed = _serve(
        sessions,
        now,
        node,
        installation,
        lowest_available=_FREE - 280,
        other_claim=other_claim,
    )
    assert observed == (280 if attributed else None)


def test_a_runs_real_use_outlives_its_collected_history(tmp_path) -> None:
    """Catches the observed peak leaving with the run's or installation's rows.

    Terminal history is collected a day after a run ends (its rank rows go
    with it), and an uninstalled installation's with its catalog revision
    (as catalog_revision_collection deletes them). The peak belongs to the
    recipe content, so it survives both.
    """
    sessions, now, node, installation = setup(tmp_path, free_memory=_FREE)
    service, observed = _serve(
        sessions, now, node, installation, lowest_available=_FREE - 280
    )
    assert observed == 280
    role = service.plan_run(installation, alias="qwen", now=now).nodes[0].role
    with sessions() as session:
        digest = session.scalars(
            select(CatalogDocumentRevision.content_digest).where(
                CatalogDocumentRevision.kind == "recipe"
            )
        ).one()
    with sessions.begin() as session:
        session.execute(delete(RunNode))
        session.execute(delete(RecipeRun))
        session.execute(delete(InstallationNode))
        session.execute(delete(RecipeInstallation))

    with sessions() as session:
        assert observed_role_peaks(session, digest) == {role: 280}


def test_a_running_run_without_a_row_gets_it_from_its_next_sample(tmp_path) -> None:
    """Catches a missing measured row staying missing while its run serves.

    A table that is empty (a fresh deploy) is a miss, not a gap to fill from
    history: the run's next qualifying sample writes the row.
    """
    sessions, now, node, installation = setup(tmp_path, free_memory=_FREE)
    service = RunAdmissionService(
        sessions, inventory_max_age=300, memory_floor_bytes=_FLOOR
    )
    service.accept_run(
        service.plan_run(installation, alias="qwen", now=now), actor="admin", now=now
    )
    with sessions() as session:
        digest = session.scalars(
            select(CatalogDocumentRevision.content_digest).where(
                CatalogDocumentRevision.kind == "recipe"
            )
        ).one()
        assert observed_role_peaks(session, digest) == {}
    _report(sessions, node, now + timedelta(seconds=1), _FREE - 120)
    with sessions() as session:
        assert list(observed_role_peaks(session, digest).values()) == [120]
    with sessions.begin() as session:
        session.execute(delete(MeasuredRolePeak))
    _report(sessions, node, now + timedelta(seconds=2), _FREE - 120)
    with sessions() as session:
        assert list(observed_role_peaks(session, digest).values()) == [120]


def _run_node_events(sessions) -> list[int]:
    from vonk_control.models import FleetStreamEvent

    with sessions() as session:
        return [
            event.payload["observed_memory_bytes"]
            for event in session.scalars(
                select(FleetStreamEvent).order_by(FleetStreamEvent.id)
            )
            if event.entity_kind == "run-node"
        ]


def _admit_run(sessions, now, node, installation) -> str:
    service = RunAdmissionService(
        sessions, inventory_max_age=300, memory_floor_bytes=_FLOOR
    )
    return service.accept_run(
        service.plan_run(installation, alias="qwen", now=now), actor="admin", now=now
    )


def test_a_raised_peak_emits_one_run_node_event_and_a_lower_one_none(
    tmp_path,
) -> None:
    """Catches the conditional peak UPDATE losing its Fleet event (or doubling it)."""
    sessions, now, node, installation = setup(tmp_path, free_memory=_FREE)
    _admit_run(sessions, now, node, installation)
    _report(sessions, node, now + timedelta(seconds=1), _FREE - 100)
    assert _run_node_events(sessions) == [100]
    _report(sessions, node, now + timedelta(seconds=2), _FREE - 100)
    _report(sessions, node, now + timedelta(seconds=3), _FREE - 40)
    assert _run_node_events(sessions) == [100]
    _report(sessions, node, now + timedelta(seconds=4), _FREE - 150)
    assert _run_node_events(sessions) == [100, 150]


def test_an_unrenderable_run_node_event_is_a_cursor_hole_not_a_refusal(
    tmp_path,
) -> None:
    """Catches a rank whose stored diagnostics break the payload refusing telemetry."""
    from vonk_agent_protocol.failure_evidence import FailureLogTail
    from vonk_control.models import (
        FleetEventCursor,
        FleetStreamEvent,
        NodeTelemetrySample,
    )

    sessions, now, node, installation = setup(tmp_path, free_memory=_FREE)
    run_id = _admit_run(sessions, now, node, installation)
    empty = FailureLogTail(text="", truncated=False, dropped_bytes=0, dropped_lines=0)
    from vonk_agent_protocol.failure_evidence import FailureDiagnostics

    diagnostics = FailureDiagnostics(
        collected_at=now.isoformat(),
        phase="unknown",
        category="unknown",
        stdout=empty,
        stderr=empty,
        versions=[],
        sandbox=[],
        storage=[],
        preflight=[],
        collector_errors=[],
    ).model_dump(mode="json")
    with sessions.begin() as session:
        rank = session.scalar(select(RunNode).where(RunNode.run_id == run_id))
        assert rank is not None
        rank.observation_failure_diagnostics = diagnostics

    _report(sessions, node, now + timedelta(seconds=1), _FREE - 100)

    with sessions() as session:
        assert session.scalar(select(NodeTelemetrySample.id)) is not None
        rank = session.scalar(select(RunNode).where(RunNode.run_id == run_id))
        assert rank is not None and rank.observed_memory_bytes == 100
        events = list(session.scalars(select(FleetStreamEvent.id)))
        last_id = session.scalar(select(FleetEventCursor.last_id))
    assert _run_node_events(sessions) == []
    assert last_id is not None and last_id > max(events)  # the hole
