"""A load's transient peak rides on swap; the steady demand stays the reservation.

Every test drives the real ``plan_run`` / ``accept_run_in_session`` and the
real inventory repository against PostgreSQL.
"""

import json
from datetime import timedelta
from importlib.resources import files
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from vonk_agent_protocol import (
    ResourceBlockerCode,
    RunAdmissionCode,
    RunState,
    RunSwitchCode,
    canonical_message,
)
from vonk_agent_protocol.inventory import HostSettings
from vonk_agent_protocol.swap import GIB, SWAP_POLICY, usable_swap_bytes
from vonk_control.fleet_profiles.dependencies import ENDING_BLOCKER_CODES
from vonk_control.inventory_repository import InventoryRepository, NodeInventoryReport
from vonk_control.models import (
    InstallationNode,
    NodeInventorySnapshot,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from vonk_control.recipe_execution_contract import parse_stored_run_plan
from vonk_control.run_admission import (
    RETRYABLE_PLAN_BLOCKERS,
    RunAdmissionBusy,
    RunAdmissionService,
    require_admissible,
)
from vonk_control.telemetry import TelemetryRepository, TelemetrySample

from .test_run_admission import setup

_TOTAL = 120 * GIB
_FREE = 118 * GIB
_STEADY = 100 * GIB
_FLOOR = 4 * GIB


def _inventory(sessions, at, node, *, free=_FREE, swap=None):
    """Record one realistic Spark report through the real inventory validator."""
    report = {
        "node_id": node,
        "observed_at": at,
        "disk_total_bytes": 1000,
        "disk_free_bytes": 500,
        "host_memory_total_bytes": _TOTAL,
        "host_memory_free_bytes": free,
        "gpu_memory_total_bytes": _TOTAL,
        "gpu_memory_free_bytes": free,
        "gpu_count": 1,
        "artifact_store_read_only": False,
        "capabilities": ("runtime.vonk.v1", "recipe.image.pull.v1"),
        "memory_pool": "shared",
        "schema_version": 1,
        "nvidia_driver_version": "unknown",
        "container_runtime_version": "unknown",
    }
    if swap is not None:
        report["host_settings"] = {
            "display_manager_active": False,
            "graphical_default_target": False,
            "swap": swap,
        }
    InventoryRepository(sessions, clock=lambda: at).record(
        NodeInventoryReport.model_validate_json(canonical_message(report))
    )


_PAGE = 4096
#: What both Sparks report at the 64 GiB default (2026-10-29): /swap.img's
#: 16777212 kB plus the helper's 50331652 kB area, sized to make up both
#: areas' header pages.
_FLEET_64_GIB = (16777212 + 50331652) * 1024


def _swap(total_gib, *, used=0, header_pages=0):
    """SwapTotal as the kernel reports a Spark set to ``total_gib``.

    The helper sizes its area to make up the header pages (the 64 GiB default
    reads exactly as the fleet reports it). ``header_pages`` models a layout
    that does not: SwapTotal leaves out one page per swap area.
    """
    total = (
        _FLEET_64_GIB
        if total_gib == 64 and not header_pages
        else total_gib * GIB - _PAGE * header_pages
    )
    return {
        "total_bytes": total,
        "free_bytes": total - used,
        "receipt_detail": "test",
    }


def _telemetry(sessions, at, node, available):
    TelemetryRepository(sessions, clock=lambda: at).record_batch(
        node,
        (
            TelemetrySample.model_validate_json(
                canonical_message(
                    {
                        "boot_id": str(uuid4()),
                        "observed_at": at,
                        "memory_total_bytes": _TOTAL,
                        "memory_available_bytes": available,
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


def _stop(sessions, run_id, at):
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run.state = "stopped"
        run.stopped_at = at
        for rank in session.scalars(select(RunNode).where(RunNode.run_id == run_id)):
            rank.state = "stopped"
        for claim in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_id == run_id,
                ResourceReservation.state == "active",
            )
        ):
            claim.state = "released"
            claim.released_at = at


def _world(tmp_path, postgres_engine, steady=_STEADY, **declared):
    sessions, now, node, installation = setup(
        tmp_path, peak_bytes=steady, engine=postgres_engine, **declared
    )
    service = RunAdmissionService(
        sessions, inventory_max_age=300, memory_floor_bytes=_FLOOR
    )
    return sessions, now, node, installation, service


def _codes(plan):
    return {reason.code for node in plan.nodes for reason in node.blockers}


def _accept(sessions, service, plan, now):
    with sessions.begin() as session:
        return service.accept_run_in_session(session, plan, actor="admin", now=now)


def _observe_rise(sessions, service, installation, node, at, *, steady, rise):
    """One earlier run of this content whose load rose ``rise`` above ``steady``."""
    at += timedelta(seconds=1)
    _inventory(sessions, at, node, swap=_swap(128))
    run_id = _accept(
        sessions,
        service,
        service.plan_run(installation, alias="observed", now=at),
        at,
    )
    _telemetry(sessions, at + timedelta(seconds=1), node, _FREE - steady - rise)
    _stop(sessions, run_id, at + timedelta(seconds=2))
    return at + timedelta(seconds=3)


def _swap_wait(plan):
    """The plan waits on swap alone, and accepting it is the same typed wait."""
    assert not plan.allowed
    assert _codes(plan) == {RunAdmissionCode.INSUFFICIENT_SWAP}
    assert RunAdmissionCode.INSUFFICIENT_SWAP in RETRYABLE_PLAN_BLOCKERS
    with pytest.raises(RunAdmissionBusy) as waited:
        require_admissible(plan)
    assert waited.value.code == RunAdmissionCode.INSUFFICIENT_SWAP
    return " ".join(reason.detail for node in plan.nodes for reason in node.blockers)


def _claim_bytes(sessions, run_id):
    with sessions() as session:
        claim = session.scalar(
            select(ResourceReservation).where(
                ResourceReservation.owner_id == run_id,
                ResourceReservation.kind == "unified-memory",
            )
        )
        assert claim is not None
        return claim.amount_bytes


def _stored_load(sessions, run_id):
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        return parse_stored_run_plan(run.plan).nodes[0].load_memory_bytes


def test_f_a_swap_backed_load_never_poisons_its_content(
    tmp_path, postgres_engine
) -> None:
    """Catches the whole-run peak replacing the steady demand (2026-10-28).

    DSV4 loads past physical memory on swap; MemAvailable (which leaves swap
    out) fell to 0.7 GiB, so the run recorded nearly the whole pool as its
    peak. Re-admission then charged that as the steady demand, found it plus
    the floor above the Spark's pool and ended the load for good
    (demand_exceeds_capacity, stored by content digest).
    """
    sessions, now, node, installation, service = _world(tmp_path, postgres_engine)
    _inventory(sessions, now + timedelta(seconds=1), node, swap=_swap(64))
    at = now + timedelta(seconds=1)
    run_id = _accept(
        sessions, service, service.plan_run(installation, alias="qwen", now=at), at
    )
    _telemetry(sessions, now + timedelta(seconds=2), node, int(0.7 * GIB))
    with sessions() as session:
        rank = session.scalar(select(RunNode).where(RunNode.run_id == run_id))
        assert rank is not None and rank.observed_memory_bytes == _FREE - int(0.7 * GIB)
    _stop(sessions, run_id, now + timedelta(seconds=3))

    later = now + timedelta(seconds=4)
    _inventory(sessions, later, node, swap=_swap(64))
    plan = service.plan_run(installation, alias="qwen", now=later)

    assert ResourceBlockerCode.DEMAND_EXCEEDS_CAPACITY not in _codes(plan)
    assert plan.allowed, _codes(plan)
    assert plan.nodes[0].required_memory_bytes == _STEADY
    readmitted = _accept(sessions, service, plan, later)
    assert _claim_bytes(sessions, readmitted) == _STEADY
    assert _stored_load(sessions, readmitted) == _FREE - int(0.7 * GIB)


def test_a_a_load_waits_on_16_gib_swap_and_starts_on_64_reserving_its_steady_demand(
    tmp_path, postgres_engine
) -> None:
    """Catches a load whose rise swap cannot carry being admitted (no check)."""
    steady, rise = 60 * GIB, 20 * GIB
    sessions, now, node, installation, service = _world(
        tmp_path, postgres_engine, steady
    )
    at = _observe_rise(
        sessions, service, installation, node, now, steady=steady, rise=rise
    )

    _inventory(sessions, at, node, swap=_swap(16))
    detail = _swap_wait(service.plan_run(installation, alias="qwen", now=at))
    # 20 GiB needs 23 GiB of swap within the guard's margins.
    assert "at least 23 GiB" not in detail and "reaches its setting" in detail

    later = at + timedelta(seconds=1)
    _inventory(sessions, later, node, swap=_swap(64))
    plan = service.plan_run(installation, alias="qwen", now=later)
    assert plan.allowed, _codes(plan)
    run_id = _accept(sessions, service, plan, later)
    assert _claim_bytes(sessions, run_id) == steady
    assert _stored_load(sessions, run_id) == steady + rise


def test_b_unknown_swap_waits_then_the_load_starts_once_swap_is_reported(
    tmp_path, postgres_engine
) -> None:
    """Catches an agent that reports no swap being treated as having some."""
    steady, rise = 60 * GIB, 20 * GIB
    sessions, now, node, installation, service = _world(
        tmp_path, postgres_engine, steady
    )
    at = _observe_rise(
        sessions, service, installation, node, now, steady=steady, rise=rise
    )

    _inventory(sessions, at, node)
    _swap_wait(service.plan_run(installation, alias="qwen", now=at))

    later = at + timedelta(seconds=1)
    _inventory(sessions, later, node, swap=_swap(64))
    assert service.plan_run(installation, alias="qwen", now=later).allowed


def test_c_a_second_load_waits_while_the_first_still_loads_and_no_rise_never_waits(
    tmp_path, postgres_engine
) -> None:
    """Catches two loads on one Spark each counting on the same free swap."""
    steady, rise = 30 * GIB, 30 * GIB
    sessions, now, node, installation, service = _world(
        tmp_path, postgres_engine, steady
    )
    at = _observe_rise(
        sessions, service, installation, node, now, steady=steady, rise=rise
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

    detail = _swap_wait(service.plan_run(installation, alias="b", now=at))
    assert f"{rise} held by loads still starting" in detail
    assert "other loads here finish loading" in detail

    with sessions.begin() as session:
        session.execute(
            update(RunNode)
            .where(RunNode.run_id == first)
            .values(state=RunState.RUNNING)
        )
    assert service.plan_run(installation, alias="b", now=at).allowed

    # A load that does not rise above its steady demand needs no swap: it
    # never waits on another load's.
    with sessions.begin() as session:
        session.execute(
            update(RunNode)
            .where(RunNode.run_id == first)
            .values(state=RunState.STARTING)
        )
        session.execute(update(RunNode).values(observed_memory_bytes=None))
        session.execute(update(InstallationNode).values(observed_memory_bytes=None))
    plan = service.plan_run(installation, alias="c", now=at)
    assert plan.allowed, _codes(plan)
    assert plan.nodes[0].load_memory_bytes is None


def test_d_a_rise_past_the_guard_margin_waits_with_the_setting_to_raise(
    tmp_path, postgres_engine
) -> None:
    """Catches admitting a load the host memory guard would stop at once.

    63 GiB fits under a 64 GiB swap total but not within the guard's 10%
    margin (57.6 GiB usable): admitting it would invite R1/R2.
    """
    steady, rise = 30 * GIB, 63 * GIB
    sessions, now, node, installation, service = _world(
        tmp_path, postgres_engine, steady
    )
    at = _observe_rise(
        sessions, service, installation, node, now, steady=steady, rise=rise
    )

    _inventory(sessions, at, node, swap=_swap(64))
    detail = _swap_wait(service.plan_run(installation, alias="qwen", now=at))
    # 70 GiB leaves a few bytes under 63 GiB usable (the guard's integer
    # percent): 71 GiB is the smallest setting that holds it.
    assert "Raise the Spark swap setting to at least 71 GiB" in detail


def test_e_only_a_rise_beyond_the_largest_swap_setting_never_fits(
    tmp_path, postgres_engine
) -> None:
    """Catches a large rise ending the load although a bigger setting holds it."""
    steady = GIB
    largest = usable_swap_bytes(SWAP_POLICY.maximum_gib * GIB)
    sessions, now, node, installation, service = _world(
        tmp_path, postgres_engine, steady
    )
    at = _observe_rise(
        sessions, service, installation, node, now, steady=steady, rise=largest
    )
    _inventory(sessions, at, node, swap=_swap(64))
    detail = _swap_wait(service.plan_run(installation, alias="qwen", now=at))
    assert (
        f"Raise the Spark swap setting to at least {SWAP_POLICY.maximum_gib} GiB"
        in detail
    )

    beyond = _observe_rise(
        sessions, service, installation, node, at, steady=steady, rise=largest + 1
    )
    _inventory(sessions, beyond, node, swap=_swap(SWAP_POLICY.maximum_gib))
    plan = service.plan_run(installation, alias="qwen", now=beyond)
    assert _codes(plan) == {ResourceBlockerCode.DEMAND_EXCEEDS_CAPACITY}
    assert ResourceBlockerCode.DEMAND_EXCEEDS_CAPACITY in ENDING_BLOCKER_CODES
    assert ResourceBlockerCode.INSUFFICIENT_SWAP not in ENDING_BLOCKER_CODES


def test_g_the_shared_swap_vector_admits_exactly_its_usable_bound(
    tmp_path, postgres_engine
) -> None:
    """Catches the Controller's usable swap drifting from the guard's vectors.

    The Spark reports the shared vector's observation through the real
    inventory validator; a rise of exactly its usable swap less the swap in
    use starts, one byte more waits.
    """
    vectors = json.loads(
        files("vonk_agent_protocol")
        .joinpath("vectors/swap-v1.json")
        .read_text(encoding="utf-8")
    )
    observation = vectors["observation"]
    usable = next(
        pair["usable_bytes"]
        for pair in vectors["usable"]
        if pair["total_bytes"] == observation["total_bytes"]
    )
    bound = usable - (observation["total_bytes"] - observation["free_bytes"])
    steady = 30 * GIB
    sessions, now, node, installation, service = _world(
        tmp_path, postgres_engine, steady
    )
    at = _observe_rise(
        sessions, service, installation, node, now, steady=steady, rise=bound
    )
    _inventory(sessions, at, node, swap=observation)
    assert service.plan_run(installation, alias="qwen", now=at).allowed

    past = _observe_rise(
        sessions, service, installation, node, at, steady=steady, rise=bound + 1
    )
    _inventory(sessions, past, node, swap=observation)
    _swap_wait(service.plan_run(installation, alias="qwen", now=past))


def _host_swap(total_gib, *, used=0):
    return HostSettings.model_validate_json(
        canonical_message(
            {
                "display_manager_active": False,
                "graphical_default_target": False,
                "swap": _swap(total_gib, used=used),
            }
        )
    )


def _set_swap(sessions, *, total_gib, used=0):
    with sessions.begin() as session:
        for snapshot in session.scalars(select(NodeInventorySnapshot)):
            snapshot.host_settings = (
                None if total_gib is None else _host_swap(total_gib, used=used)
            )


def _switch_world(tmp_path, postgres_engine, *, rise, total_gib, used, loading=0):
    """A serving run on one Spark, then a switch to the same content rising ``rise``."""
    from .test_recipe_operations import installed_recipe, setup_services
    from .test_run_switch_operations import (
        RecordingArtifactExecutor,
        SynchronousPhaseExecutor,
        _request,
        _service,
    )

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    installation_id = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid4())
    ).owner_id
    admission = lifecycle._run_admission
    old_plan = admission.plan_run(installation_id, "old", now=lifecycle._clock())
    old = admission.accept_run(old_plan, actor="admin", now=lifecycle._clock())
    steady = old_plan.nodes[0].required_memory_bytes
    if loading:
        # Another load of this content on the Spark is still starting.
        with sessions.begin() as session:
            session.execute(
                update(InstallationNode).values(observed_memory_bytes=steady + rise)
            )
        _set_swap(sessions, total_gib=SWAP_POLICY.maximum_gib)
        other = admission.accept_run(
            admission.plan_run(installation_id, "other", now=lifecycle._clock()),
            actor="admin",
            now=lifecycle._clock(),
        )
        with sessions.begin() as session:
            session.execute(
                update(RunNode)
                .where(RunNode.run_id == other)
                .values(state=RunState.STARTING)
            )
    with sessions.begin() as session:
        installation = session.get(RecipeInstallation, installation_id)
        run = session.get(RecipeRun, old)
        assert installation is not None and run is not None
        installation.state = "partial"
        run.state = "running"
        run.route_state = "published"
        for rank in session.scalars(select(RunNode).where(RunNode.run_id == old)):
            rank.state = "running"
        # This content's load rises ``rise`` above its steady demand.
        session.execute(
            update(InstallationNode).values(observed_memory_bytes=steady + rise)
        )
    _set_swap(sessions, total_gib=total_gib, used=used)
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
        phase_executor=SynchronousPhaseExecutor(),
    )
    plan = service.preview(_request(sessions, nodes[0], action="switch"), actor="admin")
    return sessions, lifecycle, installation_id, old, plan


def test_h_a_switch_away_from_a_swapping_run_stops_it_first_then_starts_once_swap_falls(
    tmp_path, postgres_engine
) -> None:
    """Catches a switch deadlocking on swap the run it replaces holds.

    Swap in use is a live reading that includes the old run's pages, so the
    after-stop fit was short too; swap was no stop-conditional shortfall, the
    stop never went first and nothing freed the swap.
    """
    sessions, lifecycle, installation_id, old, plan = _switch_world(
        tmp_path, postgres_engine, rise=35 * GIB, total_gib=64, used=25 * GIB
    )

    assert plan.allowed, plan.blockers
    assert plan.post_stop_memory_check is not None
    assert plan.post_stop_memory_check.stop_run_ids == [old]
    assert plan.stop_before_prepare is True

    # Preparation reinstalled and the stop ran; the start re-checks swap on
    # the post-stop inventory.
    with sessions.begin() as session:
        installation = session.get(RecipeInstallation, installation_id)
        assert installation is not None
        installation.state = "installed"
    _stop(sessions, old, lifecycle._clock())
    admission = lifecycle._run_admission
    _swap_wait(admission.plan_run(installation_id, "qwen", now=lifecycle._clock()))
    _set_swap(sessions, total_gib=64)
    started = admission.plan_run(installation_id, "qwen", now=lifecycle._clock())
    assert started.allowed, _codes(started)
    lifecycle.start(
        started,
        plan_digest=started.plan_digest,
        actor="admin",
        request_id=str(uuid4()),
    )


@pytest.mark.parametrize(
    ("rise", "total_gib", "used", "loading"),
    [
        # The old run uses no swap; the rise alone exceeds 64 GiB's usable.
        (60 * GIB, 64, 0, 0),
        # No swap reported: nothing to free.
        (20 * GIB, None, 0, 0),
        # Another load still starting holds the swap the rise needs.
        (35 * GIB, 64, 0, 1),
    ],
    ids=["rise-beyond-usable", "no-swap", "held-by-another-load"],
)
def test_h2_a_swap_shortfall_no_stop_clears_waits_before_stopping_anything(
    tmp_path, postgres_engine, rise, total_gib, used, loading
) -> None:
    """Catches a switch stopping a serving run that frees no swap it needs.

    The stop went first whenever the stopped run held any claim on the
    Spark: the Spark then served nothing while the start waited out the
    stall window.
    """
    _, _, _, _, plan = _switch_world(
        tmp_path,
        postgres_engine,
        rise=rise,
        total_gib=total_gib,
        used=used,
        loading=loading,
    )

    assert not plan.allowed
    assert plan.post_stop_memory_check is None
    assert plan.stop_before_prepare is False
    assert RunSwitchCode.RESOURCE_INSUFFICIENT_SWAP in {
        reason.code for reason in plan.blockers
    }


def test_i_a_job_run_holds_its_rise_while_its_ranks_run_jobs(
    tmp_path, postgres_engine
) -> None:
    """Catches one-shot job runs never holding their rise.

    Activation marks a job run's ranks running in the same transaction, and
    each job loads again while they run; two such runs counted on the same
    free swap.
    """
    from .test_artifact_jobs import _configure_artifact_recipe
    from .test_recipe_operations import NOW, installed_recipe, setup_services

    sessions, operations, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, recipe_transform=_configure_artifact_recipe, engine=postgres_engine
    )
    installation_id = installed_recipe(
        operations, mapping_id, build_id, nodes, request_id=str(uuid4())
    ).owner_id
    steady = operations.preview_run(installation_id, "image-job").nodes[0]
    with sessions.begin() as session:
        session.execute(
            update(InstallationNode).values(
                observed_memory_bytes=steady.required_memory_bytes + 30 * GIB
            )
        )
    _set_swap(sessions, total_gib=64)
    plan = operations.preview_run(installation_id, "image-job")
    assert plan.allowed, _codes(plan)
    operations.activate_job_run(
        plan, plan_digest=plan.plan_digest, actor="operator", request_id=str(uuid4())
    )
    with sessions() as session:
        assert set(session.scalars(select(RunNode.state))) == {RunState.RUNNING}

    with sessions() as session:
        first = session.scalars(select(RecipeRun.id)).one()

    second = operations.preview_run(installation_id, "image-job-b")
    assert RunAdmissionCode.INSUFFICIENT_SWAP in _codes(second)
    detail = " ".join(r.detail for node in second.nodes for r in node.blockers)
    assert f"once job run {first} here stops" in detail

    # The wait ends when the first job run stops: a fresh activation starts.
    _stop(sessions, first, NOW)
    fresh = operations.preview_run(installation_id, "image-job")
    assert fresh.allowed, _codes(fresh)
    operations.activate_job_run(
        fresh, plan_digest=fresh.plan_digest, actor="operator", request_id=str(uuid4())
    )


def test_j_a_declared_load_peak_rides_on_swap_without_any_earlier_run(
    tmp_path, postgres_engine
) -> None:
    """Catches the recipe's declared load peak being ignored (contracts 2.7.0)."""
    steady, load = 60 * GIB, 80 * GIB
    sessions, now, node, installation, service = _world(
        tmp_path, postgres_engine, steady, load_peak_bytes=load
    )
    at = now + timedelta(seconds=1)
    _inventory(sessions, at, node, swap=_swap(16))
    _swap_wait(service.plan_run(installation, alias="qwen", now=at))

    later = at + timedelta(seconds=1)
    _inventory(sessions, later, node, swap=_swap(64))
    plan = service.plan_run(installation, alias="qwen", now=later)
    assert plan.allowed, _codes(plan)
    run_id = _accept(sessions, service, plan, later)
    assert _claim_bytes(sessions, run_id) == steady
    assert _stored_load(sessions, run_id) == load


@pytest.mark.parametrize(
    ("minimum_gib", "remedy"),
    [
        (48, "reaches its setting"),
        # The minimum equals the 64 GiB setting.
        (64, "reaches its setting"),
        (96, "Raise the Spark swap setting to at least 96 GiB"),
        (128, "Raise the Spark swap setting to at least 128 GiB"),
    ],
)
def test_k_a_load_waits_until_the_spark_has_its_minimum_swap(
    tmp_path, postgres_engine, minimum_gib, remedy
) -> None:
    """Catches a load starting on less swap than its creator requires.

    And one never starting at all: a Spark set to exactly the minimum reports
    a header page per swap area less, which an exact byte comparison read
    as short forever (128 GiB passes the recipe check, so it must fit).
    """
    steady = 30 * GIB
    sessions, now, node, installation, service = _world(
        tmp_path,
        postgres_engine,
        steady,
        load_peak_bytes=steady + GIB,
        minimum_swap_bytes=minimum_gib * GIB,
    )
    at = now + timedelta(seconds=1)
    _inventory(sessions, at, node, swap=_swap(32, header_pages=2))
    assert remedy in _swap_wait(service.plan_run(installation, alias="qwen", now=at))

    # /swap.img and the helper's area, each less its header page.
    later = at + timedelta(seconds=1)
    _inventory(sessions, later, node, swap=_swap(minimum_gib, header_pages=2))
    plan = service.plan_run(installation, alias="qwen", now=later)
    assert plan.allowed, _codes(plan)


def test_l_only_a_minimum_swap_beyond_the_largest_setting_never_fits(
    tmp_path, postgres_engine
) -> None:
    """Catches a requirement no swap setting can meet waiting forever."""
    steady = 30 * GIB
    sessions, now, node, installation, service = _world(
        tmp_path,
        postgres_engine,
        steady,
        load_peak_bytes=steady + GIB,
        minimum_swap_bytes=(SWAP_POLICY.maximum_gib + 1) * GIB,
    )
    at = now + timedelta(seconds=1)
    _inventory(sessions, at, node, swap=_swap(SWAP_POLICY.maximum_gib))
    plan = service.plan_run(installation, alias="qwen", now=at)
    assert _codes(plan) == {ResourceBlockerCode.DEMAND_EXCEEDS_CAPACITY}
