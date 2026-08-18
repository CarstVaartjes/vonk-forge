from __future__ import annotations

from datetime import UTC, datetime

import pytest
from vonk_control.updates import (
    AgentObservation,
    DistributedWorkload,
    PlatformAgentArtifact,
    RolloutPolicy,
    TargetPlatform,
    TopologyExclusion,
    UpdatePlanner,
    VersionSkewAnalyzer,
    WorkloadReplicaObservation,
)


def target() -> TargetPlatform:
    return TargetPlatform(
        platform_version="2.0.0",
        build_digest="sha256:" + "a" * 64,
        release_digest="sha256:" + "b" * 64,
        base_commit="f" * 40,
        protocol_minimum=3,
        protocol_maximum=3,
        tuf_targets_version=7,
        artifacts=(
            PlatformAgentArtifact(
                architecture="linux-arm64",
                oci_manifest_digest="sha256:" + "1" * 64,
                payload_name="vonk-agent",
                payload_sha256="2" * 64,
                payload_size=4096,
            ),
        ),
    )


def agent(
    index: int,
    *,
    version: str = "1.0.0",
    build: str = "c" * 64,
    protocol: int = 3,
    online: bool = True,
    state: str = "active",
    capabilities: tuple[str, ...] = ("agent.rollback", "agent.update"),
) -> AgentObservation:
    return AgentObservation(
        node_id=f"spk_{index:032x}",
        state=state,
        online=online,
        architecture="linux-arm64",
        platform_version=version,
        build_digest="sha256:" + build,
        protocol_version=protocol,
        active_slot="A",
        agent_sha256="d" * 64,
        supervisor_generation=1,
        capabilities=capabilities,
        last_seen_at=None,
    )


def test_skew_reports_current_old_build_mismatch_offline_and_incompatible() -> None:
    observations = (
        agent(1, version="2.0.0", build="a" * 64),
        agent(2),
        agent(3, version="2.0.0", build="e" * 64),
        agent(4, online=False),
        agent(5, protocol=2),
        agent(6, state="retired"),
    )

    report = VersionSkewAnalyzer().compare(target(), observations)

    assert {item.node_id: item.status for item in report.nodes} == {
        agent(1).node_id: "current",
        agent(2).node_id: "update-available",
        agent(3).node_id: "build-mismatch",
        agent(4).node_id: "offline-pending",
        agent(5).node_id: "incompatible",
        agent(6).node_id: "retired",
    }
    assert report.prompt_required is True
    assert report.affected_nodes == tuple(agent(index).node_id for index in (2, 3, 4))
    assert report.incompatible_nodes == (agent(5).node_id,)


def test_planner_supports_one_two_and_sixteen_without_a_fleet_limit() -> None:
    planner = UpdatePlanner()
    policy = RolloutPolicy(batch_size=3, soak_seconds=60)

    for count in (1, 2, 16):
        observations = tuple(agent(index + 1) for index in range(count))
        plan = planner.plan(target(), observations, (), policy)

        assert plan.canary_node == observations[0].node_id
        assert plan.batches[0] == (observations[0].node_id,)
        assert {node for batch in plan.batches for node in batch} == {
            item.node_id for item in observations
        }
        assert all(len(batch) <= 3 for batch in plan.batches[1:])
        assert plan.plan_digest.startswith("sha256:")


def test_single_node_workload_has_zero_minimum_available_during_its_update() -> None:
    observation = agent(1)
    workload = DistributedWorkload(
        workload_id="single-model",
        members=(observation.node_id,),
        minimum_available=0,
    )

    plan = UpdatePlanner().plan(
        target(),
        (observation,),
        (workload,),
        RolloutPolicy(batch_size=1, soak_seconds=0),
    )

    assert plan.batches == ((observation.node_id,),)
    assert plan.workloads == (workload,)


def test_planner_separates_distributed_peers_and_honors_preferred_canary() -> None:
    observations = tuple(agent(index) for index in range(1, 7))
    workload = DistributedWorkload(
        workload_id="distributed-a",
        members=tuple(item.node_id for item in observations[:3]),
        minimum_available=2,
    )
    policy = RolloutPolicy(
        batch_size=3,
        soak_seconds=120,
        preferred_canary=observations[2].node_id,
    )

    plan = UpdatePlanner().plan(target(), observations, (workload,), policy)

    assert plan.canary_node == observations[2].node_id
    for batch in plan.batches:
        assert len(set(batch) & set(workload.members)) <= 1
    assert plan.soak_seconds == 120


def test_planner_counts_serving_healthy_workload_replicas_not_agent_presence() -> None:
    observations = (agent(1), agent(2))
    replicas = tuple(
        WorkloadReplicaObservation(
            node_id=observation.node_id,
            healthy=index == 0,
            serving=True,
            observed_at=datetime(2026, 8, 6, tzinfo=UTC),
            evidence_digest=str(index + 1) * 64,
        )
        for index, observation in enumerate(observations)
    )
    workload = DistributedWorkload(
        workload_id="distributed-a",
        members=tuple(item.node_id for item in observations),
        minimum_available=1,
        replicas=replicas,
    )

    with pytest.raises(ValueError, match="current update capacity"):
        UpdatePlanner().plan(
            target(),
            observations,
            (workload,),
            RolloutPolicy(batch_size=1, soak_seconds=0),
        )


def test_planner_honors_topology_exclusions_independent_of_workload_membership() -> None:
    observations = tuple(agent(index) for index in range(1, 7))
    exclusion = TopologyExclusion(
        exclusion_id="fabric-a",
        members=tuple(item.node_id for item in observations[1:4]),
        maximum_unavailable=1,
    )

    plan = UpdatePlanner().plan(
        target(),
        observations,
        (),
        RolloutPolicy(batch_size=3, soak_seconds=60),
        topology=(exclusion,),
    )

    assert plan.topology_exclusions == (exclusion,)
    assert all(
        len(set(batch).intersection(exclusion.members)) <= 1
        for batch in plan.batches
    )


def test_planner_keeps_offline_pending_and_rejects_incompatible_targets() -> None:
    observations = (agent(1), agent(2, online=False), agent(3, protocol=9))

    plan = UpdatePlanner().plan(
        target(), observations, (), RolloutPolicy(batch_size=1, soak_seconds=0)
    )

    assert plan.batches == ((agent(1).node_id,),)
    assert plan.offline_pending == (agent(2).node_id,)
    assert plan.incompatible == (agent(3).node_id,)


def test_skew_blocks_agent_newer_than_nas_control_generation() -> None:
    report = VersionSkewAnalyzer().compare(
        target(),
        (agent(1, version="3.0.0", build="e" * 64),),
    )

    assert report.nodes[0].status == "incompatible"
    assert report.nodes[0].reasons == ("agent-newer-than-control",)
    assert report.prompt_required is False


def test_skew_requires_exact_version_and_build_identity() -> None:
    report = VersionSkewAnalyzer().compare(
        target(),
        (agent(1, version="1.0.0", build="a" * 64),),
    )

    assert report.nodes[0].status == "update-available"
    assert report.prompt_required is True


def test_skew_rejects_agent_without_update_and_rollback_capabilities() -> None:
    report = VersionSkewAnalyzer().compare(
        target(),
        (agent(1, capabilities=("node.probe",)),),
    )

    assert report.nodes[0].status == "incompatible"
    assert report.nodes[0].update_required is True
    assert report.nodes[0].reasons == ("agent-update-capability-absent",)
    assert report.prompt_required is True


def test_planner_rejects_a_batch_when_workload_has_no_disruption_budget() -> None:
    observations = (agent(1), agent(2))
    workload = DistributedWorkload(
        workload_id="zero-disruption",
        members=tuple(item.node_id for item in observations),
        minimum_available=2,
    )

    with pytest.raises(ValueError, match="no update disruption budget"):
        UpdatePlanner().plan(
            target(), observations, (workload,), RolloutPolicy(batch_size=1)
        )


def test_planner_counts_offline_members_against_distributed_quorum() -> None:
    observations = (agent(1), agent(2), agent(3, online=False))
    workload = DistributedWorkload(
        workload_id="quorum-two",
        members=tuple(item.node_id for item in observations),
        minimum_available=2,
    )

    with pytest.raises(ValueError, match="no current update capacity"):
        UpdatePlanner().plan(
            target(), observations, (workload,), RolloutPolicy(batch_size=1)
        )


def test_plan_pins_commit_release_fleet_topology_and_agent_inputs() -> None:
    observations = (agent(1), agent(2))
    workload = DistributedWorkload(
        workload_id="pair",
        members=tuple(item.node_id for item in observations),
        minimum_available=1,
    )

    plan = UpdatePlanner().plan(
        target(), observations, (workload,), RolloutPolicy(batch_size=1)
    )
    changed_agent = UpdatePlanner().plan(
        target(),
        (agent(1), agent(2, build="e" * 64)),
        (workload,),
        RolloutPolicy(batch_size=1),
    )

    assert plan.target.base_commit == "f" * 40
    assert plan.target.release_digest == "sha256:" + "b" * 64
    assert plan.fleet_digest.startswith("sha256:")
    assert plan.topology_digest.startswith("sha256:")
    assert plan.agent_input_digest.startswith("sha256:")
    assert plan.agent_input_digest != changed_agent.agent_input_digest
    assert plan.plan_digest != changed_agent.plan_digest


def test_plan_emits_only_the_signed_architecture_update_payload() -> None:
    plan = UpdatePlanner().plan(
        target(), (agent(1),), (), RolloutPolicy(batch_size=1)
    )

    assert plan.payload_for(agent(1).node_id) == {
        "artifact": {
            "architecture": "linux-arm64",
            "oci_manifest_digest": "sha256:" + "1" * 64,
            "payload_name": "vonk-agent",
            "payload_sha256": "2" * 64,
            "payload_size": 4096,
        },
        "release": {
            "build_digest": "sha256:" + "a" * 64,
            "platform_version": "2.0.0",
            "protocol_maximum": 3,
            "protocol_minimum": 3,
        },
    }


def test_platform_agent_artifact_obeys_the_supervisor_256_mib_limit() -> None:
    with pytest.raises(ValueError, match="payload size"):
        PlatformAgentArtifact(
            architecture="linux-arm64",
            oci_manifest_digest="sha256:" + "1" * 64,
            payload_name="vonk-agent",
            payload_sha256="2" * 64,
            payload_size=256 * 1024 * 1024 + 1,
        )
