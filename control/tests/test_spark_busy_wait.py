"""A queued order behind a busy Spark says what it waits for, then proceeds.

The agent executes one claimed order at a time and does not poll meanwhile, so
no claim evaluates the orders queued behind it.  Their wait must still be
visible in every read (Activity, the operation, the job) and in vonkctl, and it
must clear by itself once the running order ends.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import AgentOperation as OperationKind
from vonk_agent_protocol import (
    AgentResultState,
    LifecycleState,
    OperationProgress,
    ProgressPhase,
    WaitReason,
    canonical_message,
)
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.recipe_operations import RecipeStopResult
from vonk_control.agent_jobs import AgentJobService
from vonk_control.agent_jobs.contracts import CLAIM_LEASE_SECONDS
from vonk_control.agent_jobs.waits import refresh_queue_waits
from vonk_control.auth import TokenCodec
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Base,
    Job,
)
from vonk_control.operation_api import (
    durable_operation_services,
    operation_detail_response,
)
from vonk_control.operation_api.responses import _job_operation_response
from vonk_control.operation_blockers import ProgressAlarmCode
from vonk_control.settings import PROFILE_STALL_WINDOW_SECONDS

from cluster_profiles.cli_render import progress_line

from .agent_fences import fenced_operation
from .package_upgrade_fixtures import source_transport
from .recipe_stop_fixtures import recipe_stop_payload
from .runtime_identity_support import (
    PACKAGED_RUNTIME_IDENTITY,
    claim_agent,
    inventory_items_fixture,
)

NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32
COMMIT = "a" * 64
OLD_IDENTITY = {
    "architecture": "linux-arm64",
    "binary_digest": "f" * 64,
    "build_digest": "sha256:" + "f" * 64,
    "semantic_version": "0.1.0",
}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 17, 9, 51, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def fleet(tmp_path, postgres_engine):
    engine = postgres_engine
    Base.metadata.create_all(engine)
    clock = Clock()
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        for node_id in (NODE_A, NODE_B):
            session.add(
                AgentNode(
                    node_id=node_id,
                    state="active",
                    workload_intent_ordinal=1,
                    inventory_snapshot=inventory_items_fixture(
                        node_id,
                        observed_at=None,
                        identity=AgentRuntimeIdentity.model_validate(
                            {
                                **PACKAGED_RUNTIME_IDENTITY,
                                "architecture": "linux-arm64",
                                "semantic_version": "1.0.0",
                                "build_digest": "sha256:" + "f" * 64,
                                "binary_digest": "f" * 64,
                            }
                        ),
                    ),
                )
            )
            session.flush()
            session.add(
                AgentCertificate(
                    serial=f"serial-{node_id}",
                    node_id=node_id,
                    not_before=clock.now - timedelta(seconds=1),
                    not_after=clock.now + timedelta(hours=3),
                    fingerprint=f"fingerprint-{node_id}",
                )
            )
    services = durable_operation_services(
        sessions,
        tmp_path / "routes",
        clock=clock,
        cursors=TokenCodec(b"k" * 32).cursor_codec(),
    )
    return AgentJobService(sessions, clock=clock), sessions, clock, services


def _parent(sessions, clock) -> str:
    payload = {"workload_intent_ordinal": 1}
    job = Job(
        request_id=str(uuid.uuid4()),
        kind="agent.operations",
        state="queued",
        actor="operator",
        authority_revision=COMMIT,
        targets=[NODE_A, NODE_B],
        payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
        payload=payload,
        current_attempt=0,
        created_at=clock.now,
        updated_at=clock.now,
    )
    with sessions.begin() as session:
        session.add(job)
    return job.id


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


def _busy(fleet):
    """Spark A executes a Stop: its claim lane is held by a live attempt."""

    jobs, sessions, clock, _services = fleet
    stop = jobs.enqueue(
        _parent(sessions, clock),
        NODE_A,
        "recipe.stop",
        COMMIT,
        recipe_stop_payload(NODE_A, plan_digest=COMMIT),
    )
    claim = claim_agent(jobs, NODE_A, f"serial-{NODE_A}", runtime_identity=OLD_IDENTITY)
    assert claim is not None and fenced_operation(sessions, claim).id == stop.id
    clock.now += timedelta(seconds=1)
    return stop, claim


def _waits(services, operation_id: str, job_id: str):
    """The wait as every read surface reports it, in API JSON form."""

    detail = operation_detail_response(services.get_operation(operation_id))
    listed = [
        operation_detail_response(item)
        for item in services.list_operations(None, 100, None, None, None).items
        if item.id == operation_id
    ]
    job_member = [
        _job_operation_response(item)
        for item in services.job_operations(job_id, None, 100).items
        if item.id == operation_id
    ]
    assert len(listed) == 1 and len(job_member) == 1
    return (
        detail.model_dump(mode="json"),
        listed[0].model_dump(mode="json"),
        job_member[0].model_dump(mode="json"),
    )


def test_queued_upgrade_reports_the_order_holding_its_spark_then_proceeds(
    fleet,
) -> None:
    jobs, sessions, clock, services = fleet
    stop, stop_claim = _busy(fleet)
    job_id = _parent(sessions, clock)
    upgrade = jobs.enqueue(
        job_id, NODE_A, OperationKind.AGENT_UPGRADE.value, COMMIT, _upgrade_payload()
    )

    for document in _waits(services, upgrade.id, job_id):
        assert document["state"] == "queued"
        assert document["wait_reason"] == WaitReason.SPARK_BUSY
        assert datetime.fromisoformat(document["next_attempt_at"]) > clock.now
        [blocker] = document["blockers"]
        assert blocker["code"] == WaitReason.SPARK_BUSY
        assert blocker["operation"] == {"id": stop.id, "kind": "recipe.stop"}
        assert blocker["node_ids"] == [NODE_A]
        assert blocker["run_id"] == stop.payload["run_id"]
        # vonkctl names the order and the Spark from the same response.
        assert f"recipe.stop {stop.id[:8]} on {NODE_A}" in progress_line(document)

    # A queue summary must not hide a claim refusal that carries stronger evidence.
    refusal = "claim refused: node identity inactive"
    with sessions.begin() as session:
        queued = session.get(AgentOperation, upgrade.id)
        assert queued is not None
        queued.status_reason = refusal
    assert (
        operation_detail_response(services.get_operation(upgrade.id)).status_reason
        == refusal
    )

    # The running Stop itself waits for nothing.
    assert not operation_detail_response(services.get_operation(stop.id)).blockers

    jobs.succeed(stop_claim, RecipeStopResult())

    for document in _waits(services, upgrade.id, job_id):
        assert document["wait_reason"] == WaitReason.OBSERVATION_UNAVAILABLE
        assert not document["blockers"][0].get("operation")
    claim = claim_agent(jobs, NODE_A, f"serial-{NODE_A}", runtime_identity=OLD_IDENTITY)
    assert claim is not None and fenced_operation(sessions, claim).id == upgrade.id


def test_any_queued_order_on_a_busy_spark_waits_visibly_and_only_there(
    fleet,
) -> None:
    jobs, sessions, clock, services = fleet
    stop, _stop_claim = _busy(fleet)
    job_id = _parent(sessions, clock)
    same_spark = recipe_stop_payload(NODE_A, plan_digest="c" * 64)
    other_spark = recipe_stop_payload(NODE_B, plan_digest=COMMIT)
    behind = jobs.enqueue(job_id, NODE_A, "recipe.stop", COMMIT, same_spark)
    elsewhere = jobs.enqueue(job_id, NODE_B, "recipe.stop", COMMIT, other_spark)

    for document in _waits(services, behind.id, job_id):
        assert document["blockers"][0]["operation"]["id"] == stop.id
    for document in _waits(services, elsewhere.id, job_id):
        assert document["wait_reason"] == WaitReason.OBSERVATION_UNAVAILABLE
        assert not document["blockers"][0].get("operation")

    # Lease loss must not hide the unconfirmed effect or imply capacity is free.
    clock.now += timedelta(seconds=CLAIM_LEASE_SECONDS + 1)
    with sessions.begin() as session:
        refresh_queue_waits(session, clock.now)
    for document in _waits(services, behind.id, job_id):
        assert document["wait_reason"] == WaitReason.JOB_STATE_UNCERTAIN
        assert document["blockers"][0]["operation"]["id"] == stop.id

    # A known failed attempt awaiting retry/operator action is not an unknown
    # effect and must not masquerade as the holder of another order's lane.
    with sessions.begin() as session:
        holder = session.get(AgentOperation, stop.id)
        assert holder is not None
        holder.state = LifecycleState.BACKOFF.value
        attempt = (
            session.query(AgentOperationAttempt).filter_by(operation_id=stop.id).one()
        )
        attempt.state = AgentResultState.FAILED.value
        refresh_queue_waits(session, clock.now)
    for document in _waits(services, behind.id, job_id):
        assert document["wait_reason"] == WaitReason.OBSERVATION_UNAVAILABLE
        assert not document["blockers"][0].get("operation")


def test_unconfirmed_blocker_stalls_visibly_then_clears_without_changing_intent(fleet):
    """Catches lease loss hiding a queue wait, and heartbeat/read resetting its alarm."""
    jobs, sessions, clock, services = fleet
    stop, _ = _busy(fleet)
    job_id = _parent(sessions, clock)
    upgrade = jobs.enqueue(
        job_id, NODE_A, OperationKind.AGENT_UPGRADE.value, COMMIT, _upgrade_payload()
    )
    recheck_at = clock.now + timedelta(seconds=2 * CLAIM_LEASE_SECONDS)
    with sessions.begin() as session:
        holder = session.get(AgentOperation, stop.id)
        assert holder is not None
        holder.state = LifecycleState.OBSERVING.value
        holder.next_action_at = recheck_at
    clock.now += timedelta(seconds=PROFILE_STALL_WINDOW_SECONDS + 1)
    with sessions.begin() as session:
        refresh_queue_waits(session, clock.now)
    for document in _waits(services, upgrade.id, job_id):
        assert document["wait_reason"] == WaitReason.JOB_STATE_UNCERTAIN
        assert document["blockers"][0]["operation"]["id"] == stop.id
        assert document["blockers"][0]["run_id"] == stop.payload["run_id"]
        assert document["blockers"][1]["code"] == ProgressAlarmCode.STALLED
        assert datetime.fromisoformat(document["next_attempt_at"]) == recheck_at
    # A read never changes the queue identity, attempt, or stored schedule.
    with sessions() as session:
        queued = session.get(AgentOperation, upgrade.id)
        assert queued is not None
        assert queued.current_attempt == 0 and queued.next_action_at == recheck_at
        assert queued.state == LifecycleState.QUEUED.value
    # Reconciliation of the exact old effect permits the original upgrade.
    with sessions.begin() as session:
        holder = session.get(AgentOperation, stop.id)
        assert holder is not None
        holder.state = LifecycleState.SUCCEEDED.value
    claim = claim_agent(jobs, NODE_A, f"serial-{NODE_A}", runtime_identity=OLD_IDENTITY)
    assert claim is not None and fenced_operation(sessions, claim).id == upgrade.id
    assert not operation_detail_response(services.get_operation(upgrade.id)).blockers


def test_live_blocker_progress_clears_alarm_but_renewal_does_not(fleet):
    """Catches treating a fresh lease or repeated read as useful queue progress."""
    jobs, sessions, clock, services = fleet
    stop, _ = _busy(fleet)
    job_id = _parent(sessions, clock)
    queued = jobs.enqueue(
        job_id, NODE_A, OperationKind.AGENT_UPGRADE.value, COMMIT, _upgrade_payload()
    )
    clock.now += timedelta(seconds=PROFILE_STALL_WINDOW_SECONDS + 1)
    with sessions.begin() as session:
        attempt = (
            session.query(AgentOperationAttempt).filter_by(operation_id=stop.id).one()
        )
        attempt.lease_deadline = clock.now + timedelta(seconds=CLAIM_LEASE_SECONDS)
        refresh_queue_waits(session, clock.now)
    for document in _waits(services, queued.id, job_id):
        assert document["wait_reason"] == WaitReason.SPARK_BUSY
        assert document["blockers"][1]["code"] == ProgressAlarmCode.STALLED
    with sessions.begin() as session:
        attempt = (
            session.query(AgentOperationAttempt).filter_by(operation_id=stop.id).one()
        )
        attempt.progress = OperationProgress(
            phase=ProgressPhase.STARTING, last_progress_at=clock.now.isoformat()
        ).model_dump(mode="json")
        refresh_queue_waits(session, clock.now)
    for document in _waits(services, queued.id, job_id):
        assert len(document["blockers"]) == 1
        assert document["blockers"][0]["operation"]["id"] == stop.id
