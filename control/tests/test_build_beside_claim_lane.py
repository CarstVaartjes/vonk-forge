"""A Spark keeps taking a load's work while a build compiles beside it.

Incident 2026-10-26: a 2.5 h source build held spark-2297's only claim lane.
The dual load's 1 s probe, an uninstall and a transfer queued behind it for
hours. The owner decided that builds run off the claim lane: the agent keeps
claiming while one compiles, and the Controller must hand that work out.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from vonk_agent_protocol import (
    AgentClaim,
    AgentOperation,
    RecipeBuildEvidence,
    WaitReason,
    canonical_message,
)
from vonk_agent_protocol.recipe_operations import RecipeStopResult
from vonk_agent_protocol.runtime_preflight import RuntimePreflightResult
from vonk_control.agent_jobs.contracts import OFF_LANE_OPERATIONS
from vonk_control.agent_jobs.waits import refresh_queue_waits
from vonk_control.lifecycle.agent_operation import AgentOperationAdapter
from vonk_control.models import AgentOperation as StoredOperation
from vonk_control.models import Job
from vonk_control.operation_blockers import BlockingOperation
from vonk_control.persisted_waits import read_wait
from vonk_control.wait_contract import QueueWait

from .agent_fences import fenced_operation
from .package_upgrade_fixtures import source_transport
from .recipe_stop_fixtures import recipe_stop_payload
from .runtime_identity_support import claim_agent
from .test_agent_jobs import COMMIT, NODE_A, NODE_B, parent
from .test_agent_jobs import service as service  # noqa: PLC0414 -- fixture export

_PROTOCOL = Path(__file__).parents[2] / "agent_protocol"
_PREFLIGHT = {
    "source_build": False,
    "minimum_free_bytes": 0,
    "fabric_connectivity": "none",
    "fabric_minimum_mbps": 0,
}
_SERIAL = {NODE_A: "serial-a", NODE_B: "serial-b"}
#: Claim polls the Spark may need for one order: work is never left behind.
_BOUNDED_POLLS = 3


def _build_payload() -> dict[str, object]:
    vector = json.loads(
        (
            _PROTOCOL / "src/vonk_agent_protocol/vectors/recipe-build-claim-v1.json"
        ).read_text()
    )
    return vector["base_payload"]


def _compiling_build(jobs, sessions, clock) -> tuple[StoredOperation, AgentClaim]:
    """Node B compiles: an accepted build attempt whose agent heartbeats."""
    owner = parent(sessions, clock)
    payload = _build_payload()
    build = jobs.enqueue(
        owner.id, NODE_B, AgentOperation.RECIPE_BUILD.value, COMMIT, payload
    )
    claim = AgentClaim.model_validate(
        {
            "fence": str(uuid.uuid4()),
            "operation": AgentOperation.RECIPE_BUILD,
            "payload": payload,
            "deadline": clock.now + timedelta(seconds=30),
        }
    )
    with sessions.begin() as session:
        row = session.get(StoredOperation, build.id)
        job = session.get(Job, owner.id)
        assert row is not None and job is not None
        attempt = AgentOperationAdapter(session).start_attempt(
            row,
            job,
            _SERIAL[NODE_B],
            claim.fence,
            claim.deadline,
            clock.now,
            progress=None,
        )
        assert attempt is not None
    return build, claim


def _poll(jobs, clock, build_claim, node_id: str, wanted: str) -> AgentClaim:
    """Claim on ``node_id`` until ``wanted`` is handed out, the build heartbeating."""
    for _ in range(_BOUNDED_POLLS):
        clock.advance(seconds=10)
        jobs.heartbeat(build_claim, None, 30)
        claim = claim_agent(jobs, node_id, _SERIAL[node_id])
        if claim is not None:
            assert claim.operation == wanted, claim.operation
            return claim
    raise AssertionError(f"{wanted} on {node_id} was not claimed beside the build")


def _admit_a_fresh_probe(jobs, sessions, clock, build_claim) -> None:
    """A fresh request on node B is still claimed beside the build."""
    fresh = parent(sessions, clock)
    jobs.enqueue(
        fresh.id, NODE_B, AgentOperation.RUNTIME_PREFLIGHT.value, COMMIT, _PREFLIGHT
    )
    _poll(jobs, clock, build_claim, NODE_B, AgentOperation.RUNTIME_PREFLIGHT)


def test_dual_load_progresses_on_the_spark_whose_build_compiles(service) -> None:
    """Catches a build still holding its Spark's mutations as if it ran on the lane.

    On main the probes are handed out, but the load's mutating step on node B
    is refused ``live-mutation-in-progress`` for as long as the build runs.
    """
    jobs, sessions, clock = service
    build, build_claim = _compiling_build(jobs, sessions, clock)
    load = parent(sessions, clock)
    for node_id in (NODE_A, NODE_B):
        jobs.enqueue(
            load.id,
            node_id,
            AgentOperation.RUNTIME_PREFLIGHT.value,
            COMMIT,
            _PREFLIGHT,
        )
    # The switch then stops the old workload on node B.
    clock.advance(seconds=1)
    stop = jobs.enqueue(
        load.id,
        NODE_B,
        AgentOperation.RECIPE_STOP.value,
        COMMIT,
        recipe_stop_payload(NODE_B, plan_digest=COMMIT),
    )
    for node_id in (NODE_A, NODE_B):
        probe = _poll(
            jobs, clock, build_claim, node_id, AgentOperation.RUNTIME_PREFLIGHT
        )
        jobs.succeed(
            probe,
            RuntimePreflightResult(fingerprint="a" * 64, observed_at=1, findings=[]),
        )
    with sessions.begin() as session:
        refresh_queue_waits(session, clock.now, node_id=NODE_B)
    with sessions() as session:
        queued = session.get(StoredOperation, stop.id)
        assert queued is not None
        wait = read_wait(queued)
    # Queued beside a build it waits only for the next poll, never "for" it.
    assert wait is not None and wait.reason == WaitReason.OBSERVATION_UNAVAILABLE
    assert all(blocker.operation is None for blocker in wait.blockers)

    claimed = _poll(jobs, clock, build_claim, NODE_B, AgentOperation.RECIPE_STOP)
    assert fenced_operation(sessions, claimed).id == stop.id
    jobs.succeed(claimed, RecipeStopResult())
    with sessions() as session:
        compiling = session.get(StoredOperation, build.id)
        assert compiling is not None and compiling.state == "running"
    _admit_a_fresh_probe(jobs, sessions, clock, build_claim)


#: The agent build the upgrade fixture rolls back to: the build that may install it.
_ROLLBACK_SOURCE = {
    "architecture": "linux-arm64",
    "binary_digest": "f" * 64,
    "build_digest": "sha256:" + "f" * 64,
    "semantic_version": "0.1.0",
}


def _upgrade_payload() -> dict[str, object]:
    return {
        "architecture": "linux-arm64",
        "package_bytes": 1234,
        "package_sha256": "d" * 64,
        "package_signature": "e" * 128,
        "package_url": (
            "https://install.vonkforge.ai/dev/releases/example/"
            "spark/current/linux-arm64/vonk-forge-agent.deb"
        ),
        "package_version": "0.1.0~dev.330+g0123456789ab",
        "schema_version": 1,
        "target_binary_digest": "a" * 64,
        "target_build_digest": "sha256:" + "b" * 64,
        **source_transport(),
    }


@pytest.mark.parametrize(
    "kind",
    [AgentOperation.RECIPE_BUILD_CLEANUP, AgentOperation.AGENT_UPGRADE],
)
def test_work_that_would_end_a_build_waits_for_it_with_a_named_reason(
    service, kind
) -> None:
    """Catches the build's own cleanup or an agent restart overlapping the build."""
    jobs, sessions, clock = service
    build, build_claim = _compiling_build(jobs, sessions, clock)
    owner = parent(sessions, clock)
    payload = (
        _upgrade_payload()
        if kind is AgentOperation.AGENT_UPGRADE
        else {
            "build_id": _build_payload()["build_id"],
            "operation_id": str(uuid.uuid4()),
        }
    )
    queued_order = jobs.enqueue(owner.id, NODE_B, kind.value, COMMIT, payload)
    for _ in range(_BOUNDED_POLLS):
        clock.advance(seconds=10)
        jobs.heartbeat(build_claim, None, 30)
        assert claim_agent(jobs, NODE_B, _SERIAL[NODE_B]) is None

    def waits_for() -> QueueWait:
        with sessions.begin() as session:
            refresh_queue_waits(session, clock.now, node_id=NODE_B)
            queued = session.get(StoredOperation, queued_order.id)
            assert queued is not None
            wait = read_wait(queued)
        assert wait is not None
        return wait

    wait = waits_for()
    assert wait.reason == WaitReason.SPARK_BUSY
    assert wait.blockers[0].operation == BlockingOperation(
        id=build.id, kind=AgentOperation.RECIPE_BUILD.value
    )
    assert wait.blockers[0].node_ids == [NODE_B]
    # The build's end, not time, releases it: it then awaits the next poll.
    jobs.succeed(
        build_claim,
        RecipeBuildEvidence(
            image_bytes=1, image_digest="sha256:" + "a" * 64, oci_layout_sha256="b" * 64
        ),
    )
    assert waits_for().reason == WaitReason.OBSERVATION_UNAVAILABLE
    released = claim_agent(
        jobs,
        NODE_B,
        _SERIAL[NODE_B],
        **(
            {"runtime_identity": _ROLLBACK_SOURCE}
            if kind is AgentOperation.AGENT_UPGRADE
            else {}
        ),
    )
    assert released is not None and released.operation == kind


def test_work_held_by_the_build_never_holds_back_the_load_behind_it(service) -> None:
    """Catches a conflicting order at the queue head starving every later order.

    The claim picks the oldest claimable order; refusing it as conflicting with
    the build must not hand out nothing while a probe and a stop wait behind.
    """
    jobs, sessions, clock = service
    build, build_claim = _compiling_build(jobs, sessions, clock)
    rollout = parent(sessions, clock)
    upgrade = jobs.enqueue(
        rollout.id,
        NODE_B,
        AgentOperation.AGENT_UPGRADE.value,
        COMMIT,
        _upgrade_payload(),
    )
    clock.advance(seconds=1)
    load = parent(sessions, clock)
    jobs.enqueue(
        load.id, NODE_B, AgentOperation.RUNTIME_PREFLIGHT.value, COMMIT, _PREFLIGHT
    )
    clock.advance(seconds=1)
    jobs.enqueue(
        load.id,
        NODE_B,
        AgentOperation.RECIPE_STOP.value,
        COMMIT,
        recipe_stop_payload(NODE_B, plan_digest=COMMIT),
    )
    probe = _poll(jobs, clock, build_claim, NODE_B, AgentOperation.RUNTIME_PREFLIGHT)
    jobs.succeed(
        probe, RuntimePreflightResult(fingerprint="a" * 64, observed_at=1, findings=[])
    )
    stop = _poll(jobs, clock, build_claim, NODE_B, AgentOperation.RECIPE_STOP)
    jobs.succeed(stop, RecipeStopResult())
    with sessions.begin() as session:
        refresh_queue_waits(session, clock.now, node_id=NODE_B)
        queued = session.get(StoredOperation, upgrade.id)
        assert queued is not None and queued.state == "queued"
        wait = read_wait(queued)
    assert wait is not None and wait.reason == WaitReason.SPARK_BUSY
    assert wait.blockers[0].operation == BlockingOperation(
        id=build.id, kind=AgentOperation.RECIPE_BUILD.value
    )
    _admit_a_fresh_probe(jobs, sessions, clock, build_claim)


def test_a_compiling_build_is_never_labelled_a_refused_claim(service) -> None:
    """Catches the idle poll explaining an executing build as refused work.

    The agent polls throughout a build now; the refusal note named the build
    "running-lease-live" and rewrote it with every renewed lease.
    """
    jobs, sessions, clock = service
    build, build_claim = _compiling_build(jobs, sessions, clock)
    for _ in range(_BOUNDED_POLLS):
        clock.advance(seconds=10)
        jobs.heartbeat(build_claim, None, 30)
        assert claim_agent(jobs, NODE_B, _SERIAL[NODE_B]) is None
    with sessions() as session:
        compiling = session.get(StoredOperation, build.id)
        assert compiling is not None
        owner = session.get(Job, compiling.parent_job_id)
        assert owner is not None
        assert compiling.status_reason is None
        assert owner.status_reason is None
    _admit_a_fresh_probe(jobs, sessions, clock, build_claim)


def test_off_lane_operations_are_the_agents_shared_vector() -> None:
    """Catches the Controller and the agent disagreeing on what holds the lane."""
    vector = json.loads(
        (_PROTOCOL / "fixtures" / "off-lane-operations.json").read_text()
    )
    assert canonical_message(sorted(OFF_LANE_OPERATIONS)) == canonical_message(
        vector["off_lane_operations"]
    )
