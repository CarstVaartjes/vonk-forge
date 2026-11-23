"""Shared helpers for the agent_jobs tests."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import AgentOperation as ProtocolAgentOperation
from vonk_agent_protocol import (
    AgentResult,
    RecipeOperationRequest,
    canonical_message,
)
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.recipe_operations import RecipeStopResult
from vonk_control.agent_jobs.service import AgentJobService
from vonk_control.model_primitives import Base
from vonk_control.models.fleet import AgentCertificate, AgentNode
from vonk_control.models.jobs import Job
from vonk_control.models.operations import AgentOperation, AgentOperationAttempt
from vonk_control.recipe_operations.start_payloads import (
    RecipeStartPlacement,
    bind_compiled_execution_plan,
)

from .agent_fences import fenced_attempt, fenced_operation
from .recipe_stop_fixtures import recipe_stop_payload
from .runtime_identity_support import (
    PACKAGED_RUNTIME_IDENTITY,
    claim_agent,
    inventory_items_fixture,
)

NODE_A = "spk_" + "a" * 32


NODE_B = "spk_" + "b" * 32


COMMIT = "a" * 64


STOP_PAYLOAD = recipe_stop_payload(NODE_A, plan_digest=COMMIT)


STOP_RESULT = RecipeStopResult()


def distribution_payload(plan_digest: str = COMMIT):
    from vonk_agent_protocol import ArtifactDistributionPayload

    payload = ArtifactDistributionPayload.model_validate_json(
        (
            Path(__file__).parents[2]
            / "agent_protocol/fixtures/artifact-distribution-payload.json"
        ).read_bytes()
    )
    return payload.model_copy(update={"plan_digest": plan_digest}).model_dump(
        mode="json"
    )


def canonical_start_payload(*, start_deadline: datetime) -> dict[str, object]:
    """One wire-valid distributed rank-launch document.

    Built through the production binder, because the payload model cross-checks
    every placement field against the compiled plan: a hand-written document
    would prove nothing about the shape a queued start actually carries.  What
    this fixture pins is the field the renewal allowance consumes; the real
    producer that writes it onto a queued start operation keeps its own witness
    in ``test_recipe_operations``.
    """

    plan = copy.deepcopy(
        json.loads(
            (
                Path(__file__).parents[2]
                / "agent_protocol"
                / "tests"
                / "fixtures"
                / "compiled-execution-plan-v2.json"
            ).read_text(encoding="utf-8")
        )
    )
    plan["topology"].update(name="dual", node_count=2)
    compiled = bind_compiled_execution_plan(
        plan,
        placement=RecipeStartPlacement(
            node_id=NODE_A,
            rank=0,
            role="entrypoint",
            port=8000,
            reserved_memory_bytes=4096,
            memory_floor_bytes=2048,
            memory_kind="unified",
            fabric_address="192.168.100.10",
        ),
        endpoint_address="192.168.100.10",
        master_address="192.168.100.10",
        master_port=29500,
        world_size=2,
        peer_addresses=(),
    )
    payload: dict[str, object] = {
        "run_id": "00000000-0000-4000-8000-0000000000aa",
        "installation_id": "00000000-0000-4000-8000-000000000001",
        "recipe_revision_id": "00000000-0000-4000-8000-0000000000bb",
        "mapping_id": "00000000-0000-4000-8000-0000000000cc",
        "run_generation": 1,
        "plan_digest": "b" * 64,
        "compiled_execution_plan": compiled,
        "phase": "rank-launch",
        "start_deadline": start_deadline.isoformat(),
    }
    RecipeOperationRequest.parse(ProtocolAgentOperation.RECIPE_START, payload)
    return payload


def canonical_install_payload() -> dict[str, object]:
    compiled_plan = json.loads(
        (
            Path(__file__).parents[2]
            / "agent_protocol"
            / "tests"
            / "fixtures"
            / "compiled-execution-plan-v2.json"
        ).read_text(encoding="utf-8")
    )
    payload = {
        "installation_id": "00000000-0000-4000-8000-000000000001",
        "plan_digest": "b" * 64,
        "expected_bytes": compiled_plan["identity"]["model_artifact_bytes"],
        "compiled_execution_plan": compiled_plan,
    }
    RecipeOperationRequest.parse(ProtocolAgentOperation.RECIPE_INSTALL, payload)
    return payload


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 3, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def service(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'agent-jobs.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    clock = Clock()
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        for node_id, serial in ((NODE_A, "serial-a"), (NODE_B, "serial-b")):
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
            session.add(
                AgentCertificate(
                    serial=serial,
                    node_id=node_id,
                    not_before=clock.now - timedelta(seconds=1),
                    not_after=clock.now + timedelta(hours=3),
                    fingerprint=f"fingerprint-{serial}",
                )
            )
    return AgentJobService(sessions, clock=clock), sessions, clock


def parent(sessions, clock) -> Job:
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
    return job


def superseded_order_payload(clock) -> dict[str, object]:
    """A rank launch of the Stop fixture's run: an order a newer intent supersedes.

    Supersession is shown on a launch, never on a Stop: a cleanup order keeps the
    intent it was admitted under (``test_own_cleanup``).
    """

    return {
        # A launch budget no longer than the lease, so a lapsed lease ends it.
        **canonical_start_payload(start_deadline=clock.now + timedelta(seconds=30)),
        "run_id": recipe_stop_payload(NODE_A, plan_digest=COMMIT)["run_id"],
    }


def job_state(sessions, job_id: str) -> Job:
    with sessions() as session:
        job = session.get(Job, job_id)
        assert job is not None
        session.expunge(job)
        return job


def exercise_upgrade_reconnect(service, older_work) -> None:
    jobs, sessions, clock = service
    from .package_upgrade_fixtures import activation_receipt, source_transport

    target = {
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
    }
    old_identity = {
        "architecture": "linux-arm64",
        "binary_digest": "f" * 64,
        "build_digest": "sha256:" + "f" * 64,
        "semantic_version": "0.1.0",
    }
    older = first = None
    if older_work is not None:
        older = jobs.enqueue(
            parent(sessions, clock).id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
        )
        first = claim_agent(
            jobs,
            NODE_A,
            "serial-a",
            runtime_identity=old_identity,
        )
        assert first is not None and fenced_operation(sessions, first).id == older.id
        clock.advance(seconds=1)

    job = parent(sessions, clock)
    target.update(source_transport())
    operation = jobs.enqueue(job.id, NODE_A, "agent.upgrade.v1", COMMIT, target)
    if older_work == "running":
        # Independent recovery must still wait for an actually running mutation.
        assert (
            claim_agent(
                jobs,
                NODE_A,
                "serial-a",
                runtime_identity=old_identity,
            )
            is None
        )
    if first is not None:
        jobs.record_result(
            AgentResult.model_validate_json(
                canonical_message(
                    {
                        "fence": first.fence,
                        "state": "waiting-for-operator",
                        "result": {
                            "error_code": "agent_restart_interrupted",
                            "failure_kind": "uncertain-effect",
                            "uncertain": True,
                            "reason": "agent process restarted",
                        },
                    }
                )
            )
        )
        with sessions() as session:
            stored = session.get(AgentOperation, fenced_operation(sessions, first).id)
            assert stored is not None and stored.next_action_at is not None
            clock.now = stored.next_action_at.replace(tzinfo=UTC)
        # Retry and upgrade ordering must survive a Controller restart.
        jobs = AgentJobService(sessions, clock=clock)
        # The older exact retry was queued first, so it resumes first.
        resumed = claim_agent(
            jobs,
            NODE_A,
            "serial-a",
            runtime_identity=old_identity,
        )
        assert older is not None
        assert resumed is not None and fenced_operation(sessions, resumed).id == (
            older.id
        )
        assert (
            fenced_attempt(sessions, resumed).attempt
            == fenced_attempt(sessions, first).attempt + 1
        )
        assert resumed.payload == first.payload
        jobs.succeed(resumed, STOP_RESULT)
        assert job_state(sessions, older.parent_job_id).state == "succeeded"

    claim = claim_agent(
        jobs,
        NODE_A,
        "serial-a",
        runtime_identity=old_identity,
    )
    assert claim is not None
    assert fenced_operation(sessions, claim).id == operation.id
    assert job_state(sessions, job.id).state == "queued"

    new_identity = {
        **old_identity,
        "binary_digest": target["target_binary_digest"],
        "build_digest": target["target_build_digest"],
        "package_activation": activation_receipt(
            claim.payload.model_dump(mode="json"),
            NODE_A,
            now=int(clock.now.timestamp()),
        ),
    }
    assert (
        claim_agent(
            jobs,
            NODE_A,
            "serial-a",
            runtime_identity=new_identity,
        )
        is None
    )

    assert job_state(sessions, job.id).state == "succeeded"
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id
            )
        )
        assert stored is not None and stored.state == "succeeded"
        assert attempt is not None and attempt.state == "succeeded"
        assert attempt.result == {
            "architecture": "linux-arm64",
            "binary_digest": "a" * 64,
            "build_digest": "sha256:" + "b" * 64,
            "package_sha256": "d" * 64,
            "package_version": "0.1.0~dev.330+g0123456789ab",
            "status": "upgraded",
            "activation_receipt": new_identity["package_activation"],
        }


_RESTART_INTERRUPTED = {
    "error_code": "agent_restart_interrupted",
    "failure_kind": "uncertain-effect",
    "reason": "agent restarted with an operation in progress",
    "uncertain": True,
}


def _restart_interrupted_result(claim, operation_kind: str):
    from vonk_agent_protocol import AgentResult

    return AgentResult.model_validate_json(
        json.dumps(
            {
                "fence": claim.fence,
                "state": "waiting-for-operator",
                "result": {**_RESTART_INTERRUPTED, "operation": operation_kind},
            }
        )
    )


def _claim_until_due(jobs, sessions, clock, operation_id: str, *, rounds: int = 4):
    """Poll as the agent does, advancing to each scheduled retry time."""
    for _ in range(rounds):
        claim = claim_agent(jobs, NODE_A, "serial-a")
        if claim is not None:
            return claim
        with sessions() as session:
            stored = session.get(AgentOperation, operation_id)
            if stored.next_action_at is None:
                return None
            clock.now = max(clock.now, stored.next_action_at.replace(tzinfo=UTC))
    return None


def _enqueue_successor_mutation(jobs, sessions, clock):
    with sessions.begin() as session:
        session.get(AgentNode, NODE_A).workload_intent_ordinal = 2
    successor_parent = parent(sessions, clock)
    with sessions.begin() as session:
        session.get(Job, successor_parent.id).payload = {"workload_intent_ordinal": 2}
    return jobs.enqueue(
        successor_parent.id,
        NODE_A,
        ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value,
        COMMIT,
        distribution_payload(),
    )


def _assert_parent_ending_admit_fresh_request(jobs, sessions, clock, owner):
    from types import SimpleNamespace

    from .non_blocking import assert_ended_without_blocking

    def fresh(_):
        successor = parent(sessions, clock)
        order = jobs.enqueue(successor.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
        claim = claim_agent(jobs, NODE_A, "serial-a")
        assert claim is not None
        assert fenced_operation(sessions, claim).id == order.id
        return job_state(sessions, successor.id)

    def reason(row):
        assert row.status_reason

    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        owner,
        end=lambda row: job_state(sessions, row.id),
        fresh=fresh,
        assert_reason=reason,
    )
