from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.models import (
    AgentNode,
    Base,
    RecipeRun,
    Reconciliation,
    RoutePublication,
    RoutePublicationOwner,
    RunNode,
)
from vonk_control.update_admin import (
    PlatformUpdateAdminService,
    RouteImpact,
    durable_agent_observations,
    durable_distributed_workloads,
    durable_route_impacts,
    selected_platform_target,
    topology_exclusions_from_document,
)
from vonk_control.update_grants import AdminActionGrantIssuer
from vonk_control.updates import (
    AgentObservation,
    DistributedWorkload,
    PlatformAgentArtifact,
    TargetPlatform,
)

NODE_A = "spk_" + "1" * 32
NODE_B = "spk_" + "2" * 32
RELEASE = "platform/releases/2.0.0/" + "b" * 64 + ".json"
ROLLOUT_ID = "10000000-0000-4000-8000-000000000001"
JOB_ID = "20000000-0000-4000-8000-000000000002"


def test_accepted_fabric_topology_becomes_independent_update_exclusions() -> None:
    exclusions = topology_exclusions_from_document(
        {
            "schema_version": 1,
            "nodes": [NODE_A, NODE_B],
            "links": [
                {
                    "id": "management-a",
                    "kind": "management",
                    "accepted": True,
                    "endpoints": [
                        {"node_id": NODE_A, "interface": "eth0"},
                        {"node_id": NODE_B, "interface": "eth0"},
                    ],
                },
                {
                    "id": "fabric-a",
                    "kind": "direct-rdma",
                    "accepted": True,
                    "endpoints": [
                        {"node_id": NODE_B, "interface": "enp2s0"},
                        {"node_id": NODE_A, "interface": "enp2s0"},
                    ],
                },
            ],
        }
    )

    assert [(item.exclusion_id, item.members) for item in exclusions] == [
        ("fabric-a", (NODE_A, NODE_B))
    ]


def _target() -> TargetPlatform:
    return TargetPlatform(
        platform_version="2.0.0",
        build_digest="sha256:" + "a" * 64,
        release_digest="sha256:" + "b" * 64,
        base_commit="c" * 40,
        protocol_minimum=3,
        protocol_maximum=3,
        tuf_targets_version=9,
        artifacts=(
            PlatformAgentArtifact(
                architecture="linux-arm64",
                oci_manifest_digest="sha256:" + "d" * 64,
                payload_name="vonk-agent",
                payload_sha256="e" * 64,
                payload_size=4096,
            ),
        ),
    )


def _agent(node_id: str, *, online: bool = True) -> AgentObservation:
    return AgentObservation(
        node_id=node_id,
        state="active",
        online=online,
        architecture="linux-arm64",
        platform_version="1.0.0",
        build_digest="sha256:" + "f" * 64,
        protocol_version=3,
        active_slot="A",
        agent_sha256="1" * 64,
        supervisor_generation=1,
        capabilities=("agent.rollback", "agent.update"),
        last_seen_at=datetime(2026, 8, 6, tzinfo=UTC),
    )


@dataclass
class Inputs:
    target: TargetPlatform = field(default_factory=_target)
    observations: tuple[AgentObservation, ...] = (
        _agent(NODE_A),
        _agent(NODE_B, online=False),
    )
    workloads: tuple[DistributedWorkload, ...] = ()
    routes: tuple[RouteImpact, ...] = ()


class Orchestrator:
    def __init__(self) -> None:
        self.grant: dict[str, object] | None = None
        self.plan = None
        self.rollback_grant: dict[str, object] | None = None
        self.resume_calls: list[tuple[str, str, str, str]] = []

    def create(self, plan, actor, request_id, *, admin_grant_factory):
        assert actor and request_id
        nodes = tuple(node for batch in plan.batches for node in batch)
        self.grant = admin_grant_factory(
            rollout_id=ROLLOUT_ID,
            parent_job_id=JOB_ID,
            node_ids=nodes,
            target_release_digest=plan.target.release_digest,
        )
        self.plan = plan
        return ROLLOUT_ID

    def authorize_rollback(self, rollout_id, actor, request_id, *, admin_grant_factory):
        assert (rollout_id, actor, request_id) == (ROLLOUT_ID, "admin", "request")
        self.rollback_grant = admin_grant_factory(
            rollout_id=ROLLOUT_ID,
            parent_job_id=JOB_ID,
            node_ids=(NODE_A,),
            target_release_digest=None,
        )
        return "paused"

    def approve_resume(self, rollout_id, actor, request_id, reason):
        self.resume_calls.append((rollout_id, actor, request_id, reason))
        return "planned"


class Status:
    state = "planned"
    plan_digest = "sha256:" + "3" * 64

    def __call__(self, rollout_id: str) -> dict[str, object]:
        if rollout_id != ROLLOUT_ID:
            raise KeyError(rollout_id)
        return {
            "batches": [[NODE_A]],
            "can_approve_resume": self.state == "waiting-for-approval",
            "current_batch": 0,
            "failure_reason": "failed" if self.state == "paused" else None,
            "id": rollout_id,
            "job_id": JOB_ID,
            "nodes": [{"node_id": NODE_A, "state": "failed"}],
            "plan_digest": self.plan_digest,
            "required_action": (
                "authorize-rollback"
                if self.state == "paused"
                else "approve-resume"
                if self.state == "waiting-for-approval"
                else None
            ),
            "resume_required": self.state == "waiting-for-approval",
            "state": self.state,
        }


def _service(inputs: Inputs, orchestrator: Orchestrator, status: Status):
    clock = lambda: datetime(2026, 8, 6, tzinfo=UTC)
    return PlatformUpdateAdminService(
        target_source=lambda: inputs.target,
        observation_source=lambda: inputs.observations,
        workload_source=lambda: inputs.workloads,
        orchestrator=orchestrator,
        grant_issuer=AdminActionGrantIssuer(
            ed25519.Ed25519PrivateKey.generate(),
            clock=clock,
            nonce_factory=lambda: uuid.UUID(
                "30000000-0000-4000-8000-000000000003"
            ),
        ),
        status_source=status,
        clock=clock,
        route_source=lambda: inputs.routes,
    )


def test_skew_and_plan_expose_exact_nodes_topology_and_content_digests() -> None:
    inputs = Inputs(
        workloads=(
            DistributedWorkload(
                "distributed-model", (NODE_A, NODE_B), minimum_available=1
            ),
        )
    )
    service = _service(inputs, Orchestrator(), Status())

    skew = service.skew()
    inputs.observations = (_agent(NODE_A), _agent(NODE_B))
    plan = service.plan(release=RELEASE)

    assert skew["affected_nodes"] == [NODE_A, NODE_B]
    assert skew["offline_pending"] == [NODE_B]
    assert skew["digest"].startswith("sha256:")
    assert skew["target"]["release"] == RELEASE
    assert skew["target"]["target_sha256"] == "b" * 64
    assert skew["target"]["tuf_targets_version"] == 9
    assert plan["batches"] == [[NODE_A], [NODE_B]]
    assert plan["offline_pending"] == []
    assert plan["workloads"] == [
        {
            "members": [NODE_A, NODE_B],
            "minimum_available": 1,
            "workload_id": "distributed-model",
        }
    ]
    assert plan["plan_digest"].startswith("sha256:")
    assert plan["target"] == {
        "build_digest": "sha256:" + "a" * 64,
        "platform_version": "2.0.0",
        "protocol_maximum": 3,
        "protocol_minimum": 3,
        "release": RELEASE,
        "release_digest": "sha256:" + "b" * 64,
        "target_sha256": "b" * 64,
        "tuf_targets_version": 9,
    }


def test_skew_and_plan_use_authoritative_route_aliases_not_workload_ids() -> None:
    inputs = Inputs(
        observations=(_agent(NODE_A),),
        workloads=(DistributedWorkload("model-a", (NODE_A,), minimum_available=0),),
        routes=(
            RouteImpact("chat", "model-a", (NODE_A,)),
            RouteImpact("embeddings", "model-b", (NODE_B,)),
        ),
    )
    service = _service(inputs, Orchestrator(), Status())

    skew = service.skew()
    plan = service.plan(release=RELEASE)

    assert skew["nodes"][0]["active_workloads"] == ["model-a"]
    assert skew["nodes"][0]["active_routes"] == ["chat"]
    assert plan["affected_routes"] == ["chat"]


def test_apply_rejects_route_impact_changed_since_exact_review() -> None:
    inputs = Inputs(
        observations=(_agent(NODE_A),),
        routes=(RouteImpact("chat", "model-a", (NODE_A,)),),
    )
    orchestrator = Orchestrator()
    status = Status()
    service = _service(inputs, orchestrator, status)
    plan = service.plan(release=RELEASE)
    status.plan_digest = plan["plan_digest"]

    inputs.routes = (RouteImpact("chat-v2", "model-a", (NODE_A,)),)

    with pytest.raises(ValueError, match="route impact.*stale"):
        service.apply(plan["plan_digest"], "operator", "request")
    assert orchestrator.plan is None


def test_selected_platform_target_reopens_projection_and_rejects_process_drift() -> None:
    selected = SimpleNamespace(
        generation_id="gen-" + "b" * 24,
        platform_target_name=RELEASE,
        platform_target_sha256="b" * 64,
        tuf_targets_version=9,
        release_digest="sha256:" + "b" * 64,
        build_digest="sha256:" + "a" * 64,
        platform_version="2.0.0",
    )

    class Projections:
        calls = 0

        def load_active_projection(self):
            self.calls += 1
            return selected

    projections = Projections()
    calls: list[dict[str, object]] = []

    def load(**kwargs):
        calls.append(kwargs)
        return _target()

    first = selected_platform_target(
        projections=projections,
        running_generation_id=selected.generation_id,
        running_platform_version=selected.platform_version,
        running_release_digest=selected.release_digest,
        running_build_digest=selected.build_digest,
        metadata_root=SimpleNamespace(),
        target_root=SimpleNamespace(),
        base_commit="c" * 40,
        loader=load,
    )
    second = selected_platform_target(
        projections=projections,
        running_generation_id=selected.generation_id,
        running_platform_version=selected.platform_version,
        running_release_digest=selected.release_digest,
        running_build_digest=selected.build_digest,
        metadata_root=SimpleNamespace(),
        target_root=SimpleNamespace(),
        base_commit="c" * 40,
        loader=load,
    )

    assert (first, second) == (_target(), _target())
    assert projections.calls == 2
    assert calls[0]["platform_target_name"] == RELEASE
    assert calls[0]["platform_target_sha256"] == "b" * 64
    assert calls[0]["minimum_tuf_targets_version"] == 9

    with pytest.raises(RuntimeError, match="selected control generation"):
        selected_platform_target(
            projections=projections,
            running_generation_id="gen-" + "c" * 24,
            running_platform_version=selected.platform_version,
            running_release_digest=selected.release_digest,
            running_build_digest=selected.build_digest,
            metadata_root=SimpleNamespace(),
            target_root=SimpleNamespace(),
            base_commit="c" * 40,
            loader=load,
        )


def test_durable_route_impacts_use_only_the_accepted_publication_owner(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'routes.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    reconciliation_id = "40000000-0000-4000-8000-000000000004"
    with sessions.begin() as session:
        session.add(
            Reconciliation(
                id=reconciliation_id,
                base_commit="c" * 40,
                status="succeeded",
                summary={},
                plan_digest="d" * 64,
                resolved_plan={
                    "routes": {
                        "chat": {
                            "entrypoint_node_id": NODE_A,
                            "nodes": [NODE_A, NODE_B],
                            "workload_id": "model-a",
                        }
                    }
                },
                current_phase="completed",
                created_at=datetime(2026, 8, 6, tzinfo=UTC),
            )
        )
        session.add(
            RoutePublication(
                reconciliation_id=reconciliation_id,
                state="completed",
                generation=7,
                plan_digest="d" * 64,
            )
        )
        session.add(
            RoutePublicationOwner(
                singleton_id=1,
                reconciliation_id=reconciliation_id,
                owner_generation=7,
            )
        )

    assert durable_route_impacts(sessions) == (
        RouteImpact("chat", "model-a", (NODE_A, NODE_B)),
    )


def test_durable_update_workloads_follow_running_v1_recipe_runs(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'recipe-runs.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 8, 6, tzinfo=UTC)
    run_id = "50000000-0000-4000-8000-000000000005"
    with sessions.begin() as session:
        session.add(
            RecipeRun(
                id=run_id,
                installation_id="60000000-0000-4000-8000-000000000006",
                mapping_id="70000000-0000-4000-8000-000000000007",
                mapping_generation=1,
                alias="mia-flash",
                plan_digest="a" * 64,
                plan={
                    "schema_version": 1,
                    "nodes": [
                        {"node_id": NODE_A, "rank": 0, "role": "entrypoint"},
                        {"node_id": NODE_B, "rank": 1, "role": "worker"},
                    ],
                },
                state="running",
                route_state="published",
                actor="admin",
                created_at=now,
                updated_at=now,
            )
        )
        session.add_all(
            RunNode(
                run_id=run_id,
                node_id=node_id,
                rank=rank,
                role=role,
                state="running",
                port=8000,
                reserved_memory_bytes=1,
                evidence_digest=str(rank + 1) * 64,
                updated_at=now,
            )
            for rank, (node_id, role) in enumerate(
                ((NODE_A, "entrypoint"), (NODE_B, "worker"))
            )
        )

    workloads = durable_distributed_workloads(sessions, lambda: now)

    assert len(workloads) == 1
    assert workloads[0].workload_id == "mia-flash"
    assert workloads[0].members == (NODE_A, NODE_B)
    assert workloads[0].minimum_available == 1
    assert [(item.node_id, item.healthy, item.serving) for item in workloads[0].replicas] == [
        (NODE_A, True, True),
        (NODE_B, True, True),
    ]


def test_apply_revalidates_exact_plan_and_persists_api_grant_before_visibility() -> None:
    inputs = Inputs(observations=(_agent(NODE_A),))
    orchestrator = Orchestrator()
    status = Status()
    service = _service(inputs, orchestrator, status)
    plan = service.plan(release=RELEASE)
    status.plan_digest = plan["plan_digest"]

    result = service.apply(plan["plan_digest"], "operator", "request")

    assert result["id"] == ROLLOUT_ID
    assert result["job_id"] == JOB_ID
    assert result["plan_digest"] == plan["plan_digest"]
    assert orchestrator.grant is not None
    assert orchestrator.grant["claims"] == {
        "action": "agent.update",
        "expires_at": 1785978000,
        "nonce": "30000000-0000-4000-8000-000000000003",
        "node_ids": [NODE_A],
        "parent_job_id": JOB_ID,
        "rollout_id": ROLLOUT_ID,
        "schema_version": 1,
        "target_release_digest": "sha256:" + "b" * 64,
    }


def test_apply_rejects_unknown_or_changed_plan_digest_before_dispatch() -> None:
    inputs = Inputs(observations=(_agent(NODE_A),))
    orchestrator = Orchestrator()
    service = _service(inputs, orchestrator, Status())
    plan = service.plan(release=RELEASE)

    with pytest.raises(KeyError):
        service.apply("sha256:" + "9" * 64, "operator", "request")
    inputs.observations = (_agent(NODE_A, online=False),)
    with pytest.raises(ValueError, match="stale"):
        service.apply(plan["plan_digest"], "operator", "request")
    assert orchestrator.plan is None


def test_approve_resume_first_authorizes_rollback_then_resumes_after_rollback() -> None:
    inputs = Inputs(observations=(_agent(NODE_A),))
    orchestrator = Orchestrator()
    status = Status()
    service = _service(inputs, orchestrator, status)

    status.state = "paused"
    rollback = service.approve_resume(
        ROLLOUT_ID, "admin", "request", "recover"
    )
    assert (rollback["id"], rollback["state"]) == (ROLLOUT_ID, "paused")
    assert rollback["can_approve_resume"] is False
    assert orchestrator.rollback_grant is not None
    assert orchestrator.rollback_grant["claims"]["action"] == "agent.rollback"
    assert orchestrator.rollback_grant["claims"]["target_release_digest"] is None

    status.state = "waiting-for-approval"
    resumed = service.approve_resume(
        ROLLOUT_ID, "admin", "request", "verified"
    )
    assert (resumed["id"], resumed["state"]) == (ROLLOUT_ID, "planned")
    assert orchestrator.resume_calls == [
        (ROLLOUT_ID, "admin", "request", "verified")
    ]


def test_durable_agent_projection_uses_authenticated_freshness_and_exact_identity(
    tmp_path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'agents.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 8, 6, tzinfo=UTC)
    with sessions.begin() as session:
        session.add_all(
            [
                AgentNode(
                    node_id=NODE_A,
                    state="active",
                    architecture="linux-arm64",
                    platform_version="1.0.0",
                    build_digest="sha256:" + "f" * 64,
                    protocol_version=3,
                    active_slot="A",
                    agent_sha256="1" * 64,
                    supervisor_generation=1,
                    capabilities=["agent.rollback", "agent.update"],
                    last_seen_at=now,
                ),
                AgentNode(
                    node_id=NODE_B,
                    state="active",
                    architecture="linux-arm64",
                    platform_version="1.0.0",
                    build_digest="sha256:" + "f" * 64,
                    protocol_version=3,
                    active_slot="B",
                    agent_sha256="2" * 64,
                    supervisor_generation=2,
                    capabilities=["agent.rollback", "agent.update"],
                    last_seen_at=now - timedelta(hours=1),
                ),
            ]
        )

    observations = durable_agent_observations(sessions, lambda: now)

    assert [(item.node_id, item.online) for item in observations] == [
        (NODE_A, True),
        (NODE_B, False),
    ]
    assert observations[0].active_slot == "A"
    assert observations[1].supervisor_generation == 2
