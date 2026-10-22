"""A wait on a Spark's inventory is checked again when that inventory lands.

After a Stop, admission needs inventory the Spark collected after the stop.
The Spark reports on its own cadence, so the wait must not look again before a
newer collection exists (each look is one more retry attempt, which the stall
alarm counts), and must look again on the very next tick once one lands.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_agent_protocol import LifecycleState, canonical_message
from vonk_agent_protocol.agent_words import ProfileChildPhase
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.fleet_profile_contract import FleetProfileApplicationProgress
from vonk_control.inventory_repository import (
    MAX_INVENTORY_FUTURE_SKEW,
    InventoryRepository,
    NodeInventoryReport,
)
from vonk_control.models import RecipeRun, RunNode
from vonk_control.operation_blockers import STALL_RETRY_ATTEMPT, ProgressStall
from vonk_control.progress_stall import is_stalled, next_stall
from vonk_control.run_switch_contract import RunSwitchApplyRequest
from vonk_control.run_switch_operations import (
    RecipeLifecyclePhaseExecutor,
    RunSwitchInstallPreflightExpired,
)
from vonk_control.run_switch_operations.evidence_waits import (
    RunSwitchPostStopInventoryPending,
)

from .test_recipe_operations import NOW, installed_recipe, setup_services
from .test_run_switch_lifecycle import _Harness, _ScriptedExecutor
from .test_run_switch_operations import (
    RecordingArtifactExecutor,
    _request,
    _result,
    _service,
)

_PHASES = (
    ProfileChildPhase.PREPARE,
    ProfileChildPhase.TRANSFER,
    ProfileChildPhase.STOP,
    ProfileChildPhase.VERIFY,
)


def _report(sessions, node_id: str, observed_at: datetime, received_at: datetime):
    """The Spark reports its unchanged inventory again, collected at ``observed_at``."""

    inventory = InventoryRepository(sessions, clock=lambda: received_at)
    report = inventory.latest(node_id, now=received_at, maximum_age=10**9)
    inventory.record(
        NodeInventoryReport.model_validate_json(
            canonical_message(
                {
                    "node_id": node_id,
                    "observed_at": observed_at,
                    "disk_total_bytes": report.disk_total_bytes,
                    "disk_free_bytes": report.disk_free_bytes,
                    "host_memory_total_bytes": report.host_memory_total_bytes,
                    "host_memory_free_bytes": report.host_memory_free_bytes,
                    "gpu_memory_total_bytes": report.gpu_memory_total_bytes,
                    "gpu_memory_free_bytes": report.gpu_memory_free_bytes,
                    "gpu_count": report.gpu_count,
                    "artifact_store_read_only": report.artifact_store_read_only,
                    "capabilities": report.capabilities,
                    "memory_pool": report.memory_pool,
                    "fabric_address": report.fabric_address,
                    "fabric_bandwidth_mbps": report.fabric_bandwidth_mbps,
                    "nvidia_driver_version": report.nvidia_driver_version,
                    "container_runtime_version": report.container_runtime_version,
                    "network_interfaces": report.network_interfaces,
                    "nas_route_interface": report.nas_route_interface,
                    "schema_version": 1,
                }
            )
        )
    )


class _StallWatch:
    """The profile load's stall alarm reading this child after every tick."""

    def __init__(self, harness: _Harness) -> None:
        self.harness = harness
        self.stall: ProgressStall | None = None
        self.before: FleetProfileApplicationProgress | None = None

    def observe(self) -> None:
        view = self.harness.view()
        progress = FleetProfileApplicationProgress.model_validate(
            {
                "blockers": [item.model_dump(mode="json") for item in view.blockers],
                "child_progress": {
                    "phase": "prepare",
                    "retry_attempt": _result(view).retry_attempt,
                },
            }
        )
        self.stall = next_stall(
            self.stall,
            progress,
            before=self.before,
            state="running",
            current_step=0,
            current_operation_id=self.harness.id,
            observed_at=self.harness.now[0],
        )
        self.before = progress


def _held(tmp_path: Path, stopped_at: datetime) -> tuple[_Harness, _ScriptedExecutor]:
    executor = _ScriptedExecutor()
    harness = _Harness(tmp_path, executor)
    threshold = stopped_at + MAX_INVENTORY_FUTURE_SKEW
    pending = RunSwitchPostStopInventoryPending(
        f"Spark {harness.nodes[0]} needs inventory collected after stop",
        node_id=harness.nodes[0],
        collected_after=threshold,
        observed_after=NOW,
    )
    for kind in _PHASES:
        executor.faults[kind] = pending
    harness.service.tick()
    return harness, executor


def test_a_post_stop_wait_looks_again_only_when_newer_inventory_lands(
    tmp_path: Path,
) -> None:
    """Wrong implementation this catches: the hold fenced only its first retry to
    the stop; later retries followed the core backoff (2 to 60 s) while the Spark
    reports about every two minutes, so one ordinary wait spent five to seven
    retry attempts before the inventory it waits for could exist, and the stall
    alarm fired on a healthy Switch."""

    harness, executor = _held(tmp_path, stopped_at=NOW)
    node = harness.nodes[0]
    held = _result(harness.view())
    assert held.awaited_inventory is not None
    assert held.awaited_inventory.node_id == node
    attempt, calls = held.retry_attempt, len(executor.calls)
    alarm = _StallWatch(harness)
    alarm.observe()

    # Inventory collected before the threshold cannot settle the wait.
    early = NOW + timedelta(seconds=20)
    for seconds in range(5, 125, 5):
        harness.now[0] = NOW + timedelta(seconds=seconds)
        if harness.now[0] == early:
            _report(harness.sessions, node, early, early)
        harness.service.tick()
        alarm.observe()
    assert len(executor.calls) == calls, executor.calls
    assert _result(harness.view()).retry_attempt == attempt
    # The profile load sees one wait, never a stall; the child no phase stall.
    assert alarm.stall is not None, alarm.stall
    assert not is_stalled(alarm.stall, now=harness.now[0]), alarm.stall
    assert (attempt or 0) < STALL_RETRY_ATTEMPT

    # The Spark's next collection lands: the very next tick looks again, and
    # the wait is gone.
    executor.faults.clear()
    landed = NOW + timedelta(seconds=126)
    harness.now[0] = landed
    _report(harness.sessions, node, landed, landed)
    harness.service.tick()
    assert len(executor.calls) == calls + 1, executor.calls
    after = _result(harness.view())
    assert after.awaited_inventory is None
    assert after.retry_reason is None
    alarm.observe()
    assert alarm.stall is None


def test_a_silent_spark_is_looked_at_again_once_its_inventory_is_too_old(
    tmp_path: Path,
) -> None:
    """No report never parks the wait: it is checked again once the inventory it
    judged can no longer admit anything, and it never fails for waiting."""

    harness, executor = _held(tmp_path, stopped_at=NOW)
    calls = len(executor.calls)
    due = harness.view().next_attempt_at
    assert due is not None
    assert due >= NOW + timedelta(seconds=harness.service._inventory_max_age)
    harness.advance_to_due()
    harness.service.tick()
    assert len(executor.calls) == calls + 1
    view = harness.view()
    assert view.state not in {"failed", "cancelled"}, view.status_reason
    assert _result(view).awaited_inventory is not None


def test_the_post_stop_gate_names_the_spark_and_the_collection_it_judged(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    node_id = nodes[0]
    installation_id = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    ).owner_id
    run_plan = lifecycle._run_admission.plan_run(
        installation_id, "old", now=lifecycle._clock()
    )
    run_id = lifecycle._run_admission.accept_run(
        run_plan, actor="admin", now=lifecycle._clock()
    )
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run.state, run.route_state = "running", "published"
        for item in session.scalars(select(RunNode).where(RunNode.run_id == run_id)):
            item.state = "running"
    service = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
    plan = service.preview(_request(sessions, node_id, action="switch"), actor="admin")
    assert [stop.run_id for stop in plan.stops] == [run_id]

    stopped_at = NOW + timedelta(seconds=60)
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run.state, run.stopped_at = "stopped", stopped_at
        for item in session.scalars(select(RunNode).where(RunNode.run_id == run_id)):
            item.state = "stopped"
    now = [stopped_at + timedelta(seconds=10)]
    gate = RecipeLifecyclePhaseExecutor(
        lifecycle, sessions, ClusterMappingService(sessions), lambda: now[0]
    )
    threshold = stopped_at + MAX_INVENTORY_FUTURE_SKEW

    # Only pre-stop inventory: the next collection after the threshold settles it.
    with pytest.raises(RunSwitchPostStopInventoryPending) as pending:
        gate._require_post_stop_inventory(plan)
    assert pending.value.node_id == node_id
    assert pending.value.observed_after == threshold

    # A collection inside the skew allowance is judged and cannot settle it.
    _report(sessions, node_id, now[0], now[0])
    with pytest.raises(RunSwitchPostStopInventoryPending) as pending:
        gate._require_post_stop_inventory(plan)
    assert pending.value.observed_after == threshold

    # A collection past the threshold that went stale is judged too: the wait
    # needs a newer one, so it is never woken again by the same evidence.
    stale = threshold + timedelta(seconds=1)
    _report(sessions, node_id, stale, stale)
    now[0] = stale + timedelta(seconds=gate._inventory_max_age + 1)
    with pytest.raises(RunSwitchPostStopInventoryPending) as pending:
        gate._require_post_stop_inventory(plan)
    assert pending.value.observed_after == stale

    _report(sessions, node_id, now[0], now[0])
    gate._require_post_stop_inventory(plan)

    # Nothing of the wait outlives it: a fresh Switch for the Spark is admitted.
    service._clock = lambda: now[0]
    fresh = service.apply(
        RunSwitchApplyRequest(
            **_request(sessions, node_id, action="switch").model_dump(),
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    assert fresh.state in {LifecycleState.QUEUED, LifecycleState.RUNNING}


def test_a_repeated_preflight_refresh_keeps_one_progress_signature(
    tmp_path: Path,
) -> None:
    """Wrong implementation this catches: the hold wrote its attempt into
    ``completed_items``, so every attempt looked like progress and a refresh that
    never succeeds could never be seen as stalled."""

    executor = _ScriptedExecutor()
    for kind in _PHASES:
        executor.faults[kind] = RunSwitchInstallPreflightExpired("receipt expired")
    harness = _Harness(tmp_path, executor)
    seen = []
    attempts = []
    for _ in range(4):
        harness.service.tick()
        result = _result(harness.view())
        assert result.operation is not None
        seen.append(
            (
                result.operation.phase,
                result.operation.completed_items,
                result.operation.completed_bytes,
                result.operation.last_progress_at,
            )
        )
        attempts.append(result.retry_attempt)
        harness.advance_to_due()
    assert len(set(seen)) == 1, seen
    assert attempts == sorted(attempts) and attempts[0] != attempts[-1], attempts
