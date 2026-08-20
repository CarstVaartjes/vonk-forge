"""Deterministic version-skew analysis and topology-safe GPU node rollout plans."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import canonical_message

from .models import (
    AgentCertificate,
    AgentNode,
    AgentOperationAttempt,
    AuditEvent,
    Job,
    NodeMutationLease,
    RecipeRun,
    RunNode,
    UpdateRollout,
    UpdateRolloutNode,
)
from .models import AgentOperation as StoredAgentOperation
from .node_leases import NodeLeaseConflict, NodeLeaseService
from .update_routes import RouteDrainReceipt

_NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
_SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_AUTHORITY_REVISION = re.compile(r"[0-9a-f]{64}\Z")
_AGENT_FRESHNESS_SECONDS = 300
_OPERATION_COMPLETION_SECONDS = 600
_POST_SUCCESS_RECONNECT_SECONDS = 180
_ROLLOUT_CREATE_LOCK = threading.RLock()
_ROLLOUT_ADVANCE_LOCK = threading.RLock()


class StaleAuthorizationResult(RuntimeError):
    """Internal control flow for a committed stale signer CAS."""


@dataclass(frozen=True)
class PlatformAgentArtifact:
    architecture: str
    oci_manifest_digest: str
    payload_name: str
    payload_sha256: str
    payload_size: int

    def __post_init__(self) -> None:
        if self.architecture not in {"linux-arm64", "linux-x86_64"}:
            raise ValueError("platform agent architecture is invalid")
        if _DIGEST.fullmatch(self.oci_manifest_digest) is None:
            raise ValueError("platform agent OCI digest is invalid")
        if re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?", self.payload_name) is None:
            raise ValueError("platform agent payload name is invalid")
        if _SHA256.fullmatch(self.payload_sha256) is None:
            raise ValueError("platform agent payload digest is invalid")
        if (
            isinstance(self.payload_size, bool)
            or not isinstance(self.payload_size, int)
            or not 64 <= self.payload_size <= 256 * 1024 * 1024
        ):
            raise ValueError("platform agent payload size is invalid")


@dataclass(frozen=True)
class TargetPlatform:
    platform_version: str
    build_digest: str
    release_digest: str
    authority_revision: str
    protocol_minimum: int
    protocol_maximum: int
    tuf_targets_version: int
    artifacts: tuple[PlatformAgentArtifact, ...]

    def __post_init__(self) -> None:
        if _SEMVER.fullmatch(self.platform_version) is None:
            raise ValueError("target platform version is invalid")
        if (
            _DIGEST.fullmatch(self.build_digest) is None
            or _DIGEST.fullmatch(self.release_digest) is None
        ):
            raise ValueError("target platform digest is invalid")
        if _AUTHORITY_REVISION.fullmatch(self.authority_revision) is None:
            raise ValueError("target platform authority revision is invalid")
        if (
            isinstance(self.protocol_minimum, bool)
            or isinstance(self.protocol_maximum, bool)
            or not isinstance(self.protocol_minimum, int)
            or not isinstance(self.protocol_maximum, int)
            or not 1 <= self.protocol_minimum <= self.protocol_maximum <= 65535
            or isinstance(self.tuf_targets_version, bool)
            or not isinstance(self.tuf_targets_version, int)
            or self.tuf_targets_version < 1
        ):
            raise ValueError("target protocol range is invalid")
        architectures = tuple(item.architecture for item in self.artifacts)
        if not architectures or len(set(architectures)) != len(architectures):
            raise ValueError("target platform agent artifacts are invalid")

    @property
    def target_sha256(self) -> str:
        return self.release_digest.removeprefix("sha256:")

    @property
    def target_name(self) -> str:
        return (
            f"platform/releases/{self.platform_version}/"
            f"{self.target_sha256}.json"
        )


@dataclass(frozen=True)
class AgentObservation:
    node_id: str
    state: str
    online: bool
    architecture: str
    platform_version: str | None
    build_digest: str | None
    protocol_version: int | None
    active_slot: str | None
    agent_sha256: str | None
    supervisor_generation: int | None
    capabilities: tuple[str, ...]
    last_seen_at: datetime | None
    supervisor_ready_generation: int | None = None
    self_test_passed: bool = False
    contact_certificate_serial: str | None = None
    contact_observation_digest: str | None = None

    def __post_init__(self) -> None:
        if _NODE_ID.fullmatch(self.node_id) is None:
            raise ValueError("agent node ID is invalid")
        if self.state not in {"active", "retired", "revoked", "pending"}:
            raise ValueError("agent lifecycle state is invalid")
        if not isinstance(self.online, bool):
            raise TypeError("agent online state is invalid")
        if self.architecture not in {"linux-arm64", "linux-x86_64"}:
            raise ValueError("agent architecture is invalid")
        if self.platform_version is not None and _SEMVER.fullmatch(
            self.platform_version
        ) is None:
            raise ValueError("agent platform version is invalid")
        if self.build_digest is not None and _DIGEST.fullmatch(
            self.build_digest
        ) is None:
            raise ValueError("agent build digest is invalid")
        if self.protocol_version is not None and (
            isinstance(self.protocol_version, bool)
            or not isinstance(self.protocol_version, int)
            or not 1 <= self.protocol_version <= 65535
        ):
            raise ValueError("agent protocol version is invalid")
        if self.active_slot is not None and self.active_slot not in {"A", "B"}:
            raise ValueError("agent active slot is invalid")
        if self.agent_sha256 is not None and _SHA256.fullmatch(
            self.agent_sha256
        ) is None:
            raise ValueError("agent executable digest is invalid")
        if self.supervisor_generation is not None and (
            isinstance(self.supervisor_generation, bool)
            or not isinstance(self.supervisor_generation, int)
            or not 1 <= self.supervisor_generation <= 999_999_999
        ):
            raise ValueError("agent supervisor generation is invalid")
        if (
            len(self.capabilities) != len(set(self.capabilities))
            or any(not isinstance(item, str) or not item for item in self.capabilities)
        ):
            raise ValueError("agent capabilities are invalid")
        if self.last_seen_at is not None and self.last_seen_at.tzinfo is None:
            raise ValueError("agent last-seen timestamp must be timezone-aware")
        if (
            not isinstance(self.self_test_passed, bool)
            or (
                self.supervisor_ready_generation is not None
                and (
                    isinstance(self.supervisor_ready_generation, bool)
                    or not isinstance(self.supervisor_ready_generation, int)
                    or not 1 <= self.supervisor_ready_generation <= 999_999_999
                )
            )
            or (self.self_test_passed and self.supervisor_ready_generation is None)
            or (
                self.contact_observation_digest is not None
                and _SHA256.fullmatch(self.contact_observation_digest) is None
            )
            or (
                self.contact_certificate_serial is not None
                and (
                    not self.contact_certificate_serial
                    or len(self.contact_certificate_serial) > 128
                )
            )
        ):
            raise ValueError("agent authenticated readiness evidence is invalid")


@dataclass(frozen=True)
class NodeSkew:
    node_id: str
    status: str
    compatible: bool
    update_required: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class VersionSkewReport:
    target: TargetPlatform
    nodes: tuple[NodeSkew, ...]
    affected_nodes: tuple[str, ...]
    incompatible_nodes: tuple[str, ...]
    prompt_required: bool


class VersionSkewAnalyzer:
    def compare(
        self,
        target: TargetPlatform,
        observations: tuple[AgentObservation, ...],
    ) -> VersionSkewReport:
        if len({item.node_id for item in observations}) != len(observations):
            raise ValueError("agent observations overlap")
        nodes = tuple(
            self._node(target, observation)
            for observation in sorted(observations, key=lambda item: item.node_id)
        )
        affected = tuple(
            item.node_id
            for item in nodes
            if item.update_required and item.status != "retired"
        )
        incompatible = tuple(
            item.node_id for item in nodes if not item.compatible and item.status != "retired"
        )
        return VersionSkewReport(
            target=target,
            nodes=nodes,
            affected_nodes=affected,
            incompatible_nodes=incompatible,
            prompt_required=bool(affected),
        )

    @staticmethod
    def _node(target: TargetPlatform, agent: AgentObservation) -> NodeSkew:
        if agent.state == "retired":
            return NodeSkew(agent.node_id, "retired", True, False, ())
        if agent.state != "active":
            return NodeSkew(
                agent.node_id,
                "incompatible",
                False,
                False,
                ("agent-not-active",),
            )
        missing = tuple(
            name
            for name, value in (
                ("platform-version", agent.platform_version),
                ("build-digest", agent.build_digest),
                ("protocol-version", agent.protocol_version),
                ("active-slot", agent.active_slot),
                ("agent-sha256", agent.agent_sha256),
                ("supervisor-generation", agent.supervisor_generation),
            )
            if value is None
        )
        if missing:
            return NodeSkew(
                agent.node_id,
                "incompatible",
                False,
                False,
                tuple(f"missing-{name}" for name in missing),
            )
        assert agent.protocol_version is not None
        assert agent.platform_version is not None
        assert agent.build_digest is not None
        if not target.protocol_minimum <= agent.protocol_version <= target.protocol_maximum:
            return NodeSkew(
                agent.node_id,
                "incompatible",
                False,
                False,
                ("protocol-outside-target-range",),
            )
        current_version = _version_tuple(agent.platform_version)
        target_version = _version_tuple(target.platform_version)
        if current_version > target_version:
            return NodeSkew(
                agent.node_id,
                "incompatible",
                False,
                False,
                ("agent-newer-than-control",),
            )
        update_required = (
            agent.platform_version != target.platform_version
            or agent.build_digest != target.build_digest
        )
        if not agent.online:
            return NodeSkew(
                agent.node_id,
                "offline-pending" if update_required else "offline-current",
                True,
                update_required,
                ("agent-offline",),
            )
        if not update_required:
            return NodeSkew(agent.node_id, "current", True, False, ())
        required_capabilities = {"agent.rollback", "agent.update"}
        if not required_capabilities <= set(agent.capabilities):
            return NodeSkew(
                agent.node_id,
                "incompatible",
                False,
                True,
                ("agent-update-capability-absent",),
            )
        if current_version == target_version:
            return NodeSkew(
                agent.node_id,
                "build-mismatch",
                True,
                True,
                ("build-digest-differs",),
            )
        return NodeSkew(
            agent.node_id,
            "update-available",
            True,
            True,
            ("control-release-newer",),
        )


@dataclass(frozen=True)
class WorkloadReplicaObservation:
    node_id: str
    healthy: bool
    serving: bool
    observed_at: datetime
    evidence_digest: str

    def __post_init__(self) -> None:
        if (
            _NODE_ID.fullmatch(self.node_id) is None
            or not isinstance(self.healthy, bool)
            or not isinstance(self.serving, bool)
            or not isinstance(self.observed_at, datetime)
            or self.observed_at.tzinfo is None
            or _SHA256.fullmatch(self.evidence_digest) is None
        ):
            raise ValueError("workload replica observation is invalid")


@dataclass(frozen=True)
class DistributedWorkload:
    workload_id: str
    members: tuple[str, ...]
    minimum_available: int
    replicas: tuple[WorkloadReplicaObservation, ...] = ()

    def __post_init__(self) -> None:
        if not self.workload_id or len(self.workload_id) > 128:
            raise ValueError("workload ID is invalid")
        if (
            not self.members
            or len(set(self.members)) != len(self.members)
            or any(_NODE_ID.fullmatch(node_id) is None for node_id in self.members)
        ):
            raise ValueError("workload members are invalid")
        if (
            isinstance(self.minimum_available, bool)
            or not isinstance(self.minimum_available, int)
            or not 0 <= self.minimum_available <= len(self.members)
        ):
            raise ValueError("workload availability bound is invalid")
        if self.replicas and (
            len({item.node_id for item in self.replicas}) != len(self.replicas)
            or {item.node_id for item in self.replicas} != set(self.members)
        ):
            raise ValueError("workload replica observations are incomplete")


def durable_recipe_workloads(
    sessions: sessionmaker[Session],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[DistributedWorkload, ...]:
    """Project running v1 recipe runs into update-safety workload bounds."""

    now = _aware(clock())
    with sessions() as session:
        runs = session.scalars(
            select(RecipeRun)
            .where(RecipeRun.state == "running")
            .order_by(RecipeRun.id)
        ).all()
        result: list[DistributedWorkload] = []
        aliases: set[str] = set()
        for run in runs:
            if run.alias in aliases:
                raise RuntimeError("running recipe aliases are not unique")
            aliases.add(run.alias)
            nodes = session.scalars(
                select(RunNode)
                .where(RunNode.run_id == run.id)
                .order_by(RunNode.rank, RunNode.node_id)
            ).all()
            if not nodes:
                raise RuntimeError("running recipe has no assigned nodes")
            members = tuple(node.node_id for node in nodes)
            replicas = tuple(
                WorkloadReplicaObservation(
                    node_id=node.node_id,
                    healthy=(
                        node.state == "running"
                        and _aware(node.updated_at)
                        >= now - timedelta(seconds=_AGENT_FRESHNESS_SECONDS)
                    ),
                    serving=(
                        node.state == "running"
                        and _aware(node.updated_at)
                        >= now - timedelta(seconds=_AGENT_FRESHNESS_SECONDS)
                    ),
                    observed_at=_aware(node.updated_at),
                    evidence_digest=hashlib.sha256(
                        canonical_message(
                            {
                                "alias": run.alias,
                                "healthy": node.state == "running",
                                "node_id": node.node_id,
                                "observed_at": _aware(node.updated_at).isoformat(),
                                "serving": node.state == "running",
                            }
                        )
                    ).hexdigest(),
                )
                for node in nodes
            )
            result.append(
                DistributedWorkload(
                    workload_id=run.alias,
                    members=members,
                    minimum_available=max(0, len(members) - 1),
                    replicas=replicas,
                )
            )
    return tuple(result)


@dataclass(frozen=True)
class TopologyExclusion:
    exclusion_id: str
    members: tuple[str, ...]
    maximum_unavailable: int = 1

    def __post_init__(self) -> None:
        if not self.exclusion_id or len(self.exclusion_id) > 128:
            raise ValueError("topology exclusion ID is invalid")
        if (
            len(self.members) < 2
            or len(set(self.members)) != len(self.members)
            or any(_NODE_ID.fullmatch(node_id) is None for node_id in self.members)
            or isinstance(self.maximum_unavailable, bool)
            or not isinstance(self.maximum_unavailable, int)
            or not 1 <= self.maximum_unavailable < len(self.members)
        ):
            raise ValueError("topology exclusion is invalid")


@dataclass(frozen=True)
class RolloutPolicy:
    batch_size: int = 1
    soak_seconds: int = 300
    preferred_canary: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.batch_size, bool)
            or not isinstance(self.batch_size, int)
            or not 1 <= self.batch_size <= 1024
            or isinstance(self.soak_seconds, bool)
            or not isinstance(self.soak_seconds, int)
            or not 0 <= self.soak_seconds <= 86_400
        ):
            raise ValueError("rollout policy bounds are invalid")
        if self.preferred_canary is not None and _NODE_ID.fullmatch(
            self.preferred_canary
        ) is None:
            raise ValueError("preferred canary is invalid")


@dataclass(frozen=True)
class UpdatePlan:
    target: TargetPlatform
    canary_node: str | None
    batches: tuple[tuple[str, ...], ...]
    offline_pending: tuple[str, ...]
    incompatible: tuple[str, ...]
    soak_seconds: int
    fleet_digest: str
    topology_digest: str
    agent_input_digest: str
    node_architectures: tuple[tuple[str, str], ...]
    source_observations: tuple[AgentObservation, ...]
    workloads: tuple[DistributedWorkload, ...]
    topology_exclusions: tuple[TopologyExclusion, ...]
    policy: RolloutPolicy
    plan_digest: str

    def payload_for(self, node_id: str) -> dict[str, object]:
        scheduled = {item for batch in self.batches for item in batch}
        if node_id not in scheduled:
            raise KeyError(node_id)
        architecture = self._node_architecture(node_id)
        artifact = next(
            item for item in self.target.artifacts if item.architecture == architecture
        )
        return {
            "artifact": {
                "architecture": artifact.architecture,
                "oci_manifest_digest": artifact.oci_manifest_digest,
                "payload_name": artifact.payload_name,
                "payload_sha256": artifact.payload_sha256,
                "payload_size": artifact.payload_size,
            },
            "release": {
                "build_digest": self.target.build_digest,
                "platform_version": self.target.platform_version,
                "protocol_maximum": self.target.protocol_maximum,
                "protocol_minimum": self.target.protocol_minimum,
            },
        }

    def _node_architecture(self, node_id: str) -> str:
        # Architecture bindings are encoded into the signed plan digest and
        # exposed separately to avoid putting arbitrary mappings on the wire.
        for encoded in self.node_architectures:
            if encoded[0] == node_id:
                return encoded[1]
        raise KeyError(node_id)

    def source_for(self, node_id: str) -> AgentObservation:
        for observation in self.source_observations:
            if observation.node_id == node_id:
                return observation
        raise KeyError(node_id)


class UpdatePlanner:
    def plan(
        self,
        target: TargetPlatform,
        observations: tuple[AgentObservation, ...],
        workloads: tuple[DistributedWorkload, ...],
        policy: RolloutPolicy,
        *,
        topology: tuple[TopologyExclusion, ...] = (),
    ) -> UpdatePlan:
        report = VersionSkewAnalyzer().compare(target, observations)
        eligible = sorted(
            item.node_id
            for item in report.nodes
            if item.status in {"update-available", "build-mismatch"}
        )
        offline = tuple(
            item.node_id for item in report.nodes if item.status == "offline-pending"
        )
        incompatible = report.incompatible_nodes
        if len({item.workload_id for item in workloads}) != len(workloads):
            raise ValueError("distributed workload IDs overlap")
        known = {item.node_id for item in observations}
        if any(node_id not in known for item in workloads for node_id in item.members):
            raise ValueError("workload references an unknown node")
        if (
            len({item.exclusion_id for item in topology}) != len(topology)
            or any(node_id not in known for item in topology for node_id in item.members)
        ):
            raise ValueError("topology exclusions are invalid")
        blocked = sorted(
            workload.workload_id
            for workload in workloads
            if workload.minimum_available == len(workload.members)
            and set(workload.members) & set(eligible)
        )
        if blocked:
            raise ValueError(
                "distributed workload has no update disruption budget: "
                + ", ".join(blocked)
            )
        by_node = {item.node_id: item for item in observations}
        unavailable = sorted(
            workload.workload_id
            for workload in workloads
            if sum(
                1
                for node_id in workload.members
                if by_node[node_id].state == "active"
                and by_node[node_id].online
                and (
                    not workload.replicas
                    or any(
                        replica.node_id == node_id
                        and replica.healthy
                        and replica.serving
                        for replica in workload.replicas
                    )
                )
            )
            <= workload.minimum_available
            and set(workload.members) & set(eligible)
        )
        if unavailable:
            raise ValueError(
                "distributed workload has no current update capacity: "
                + ", ".join(unavailable)
            )
        artifacts = {item.architecture for item in target.artifacts}
        missing_artifacts = sorted(
            item.architecture
            for item in observations
            if item.node_id in eligible and item.architecture not in artifacts
        )
        if missing_artifacts:
            raise ValueError(
                "target release lacks agent artifact: "
                + ", ".join(sorted(set(missing_artifacts)))
            )
        canary = None
        batches: list[tuple[str, ...]] = []
        if eligible:
            canary = (
                policy.preferred_canary
                if policy.preferred_canary in eligible
                else eligible[0]
            )
            batches.append((canary,))
            remaining = [node_id for node_id in eligible if node_id != canary]
            while remaining:
                batch: list[str] = []
                deferred: list[str] = []
                for node_id in remaining:
                    if len(batch) >= policy.batch_size or _conflicts(
                        node_id, batch, workloads, topology
                    ):
                        deferred.append(node_id)
                    else:
                        batch.append(node_id)
                if not batch:
                    batch.append(deferred.pop(0))
                batches.append(tuple(batch))
                remaining = deferred
        fleet_digest = _digest(
            [
                {
                    "architecture": item.architecture,
                    "node_id": item.node_id,
                    "state": item.state,
                }
                for item in sorted(observations, key=lambda item: item.node_id)
            ]
        )
        topology_digest = _digest(
            {
                "exclusions": [
                    asdict(item)
                    for item in sorted(topology, key=lambda item: item.exclusion_id)
                ],
                "workloads": [
                    _workload_document(item)
                    for item in sorted(workloads, key=lambda item: item.workload_id)
                ],
            }
        )
        agent_input_digest = _digest(
            [
                _observation_document(item)
                for item in sorted(observations, key=lambda item: item.node_id)
            ]
        )
        node_architectures = tuple(
            (item.node_id, item.architecture)
            for item in sorted(observations, key=lambda item: item.node_id)
            if item.node_id in eligible
        )
        content = {
            "agent_input_digest": agent_input_digest,
            "batches": batches,
            "canary_node": canary,
            "fleet_digest": fleet_digest,
            "incompatible": incompatible,
            "offline_pending": offline,
            "node_architectures": node_architectures,
            "policy": asdict(policy),
            "target": _target_document(target),
            "topology_digest": topology_digest,
        }
        digest = _digest(content)
        return UpdatePlan(
            target=target,
            canary_node=canary,
            batches=tuple(batches),
            offline_pending=offline,
            incompatible=incompatible,
            soak_seconds=policy.soak_seconds,
            fleet_digest=fleet_digest,
            topology_digest=topology_digest,
            agent_input_digest=agent_input_digest,
            node_architectures=node_architectures,
            source_observations=tuple(
                item
                for item in sorted(observations, key=lambda item: item.node_id)
            ),
            workloads=tuple(sorted(workloads, key=lambda item: item.workload_id)),
            topology_exclusions=tuple(
                sorted(topology, key=lambda item: item.exclusion_id)
            ),
            policy=policy,
            plan_digest=digest,
        )


class UpdateRouteBoundary(Protocol):
    def withdraw(
        self, rollout_id: str, batch_index: int, targets: tuple[str, ...]
    ) -> RouteDrainReceipt: ...

    def restore(
        self, rollout_id: str, batch_index: int, targets: tuple[str, ...]
    ) -> str: ...


class UpdateAgentQueue(Protocol):
    def reserve_update_authorization_in_session(
        self,
        session: Session,
        *,
        rollout_id: str,
        rollout_node_id: str,
        operation: str,
        payload: dict[str, object],
        target_release_digest: str | None,
    ) -> object: ...

    def sign_update_authorization(self, reserved: object) -> dict[str, object]: ...

    def finalize_update_authorization_in_session(
        self,
        session: Session,
        reserved: object,
        response: dict[str, object],
    ) -> object: ...

    def mark_update_authorizations_stale(self, reserved: list[object]) -> None: ...

    def enqueue_in_session(
        self,
        session: Session,
        parent_job_id: str,
        node_id: str,
        operation: str,
        authority_revision: str,
        payload: dict[str, object],
        *,
        operation_id: str,
        prepared_update: object | None = None,
    ) -> StoredAgentOperation: ...

    def notify_available(self) -> None: ...


class UpdateOrchestrator:
    """Persist and advance one explicit, administrator-approved GPU node rollout."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        agent_jobs: UpdateAgentQueue,
        routes: UpdateRouteBoundary,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._sessions = sessions
        self._agent_jobs = agent_jobs
        self._routes = routes
        self._clock = clock
        self._leases = NodeLeaseService(clock=clock)

    def create(
        self,
        plan: UpdatePlan,
        actor: str,
        request_id: str,
        *,
        admin_grant_factory: Callable[..., dict[str, object]] | None = None,
    ) -> str:
        with _ROLLOUT_CREATE_LOCK:
            return self._create_locked(
                plan,
                actor,
                request_id,
                admin_grant_factory=admin_grant_factory,
            )

    def _create_locked(
        self,
        plan: UpdatePlan,
        actor: str,
        request_id: str,
        *,
        admin_grant_factory: Callable[..., dict[str, object]] | None,
    ) -> str:
        if not plan.batches:
            raise ValueError("update plan has no online compatible targets")
        if plan.incompatible:
            raise ValueError("incompatible agent skew blocks platform update")
        _actor(actor)
        _request_id(request_id)
        now = _aware(self._clock())
        with self._sessions() as session:
            existing = session.scalar(
                select(UpdateRollout).where(
                    UpdateRollout.plan_digest == _raw_digest(plan.plan_digest)
                )
            )
            if existing is not None:
                return existing.id
        rollout_id = str(uuid.uuid4())
        job_id = str(uuid.uuid4())
        document = _plan_document(plan)
        targets = [node_id for batch in plan.batches for node_id in batch]
        update_admin_grant = None
        if admin_grant_factory is not None:
            update_admin_grant = admin_grant_factory(
                rollout_id=rollout_id,
                parent_job_id=job_id,
                node_ids=tuple(plan.batches[0]),
                target_release_digest=plan.target.release_digest,
            )
            if not isinstance(update_admin_grant, dict):
                raise TypeError("API-issued update authorization grant is invalid")
        with self._sessions.begin() as session:
            existing = session.scalar(
                select(UpdateRollout).where(
                    UpdateRollout.plan_digest == _raw_digest(plan.plan_digest)
                )
            )
            if existing is not None:
                return existing.id
            job = Job(
                id=job_id,
                request_id=request_id,
                kind="platform.update",
                state="running",
                actor=actor,
                authority_revision=plan.target.authority_revision,
                targets=targets,
                payload_digest=_raw_digest(_digest(document)),
                payload=document,
                current_attempt=0,
                created_at=now,
                updated_at=now,
            )
            rollout = UpdateRollout(
                id=rollout_id,
                job_id=job_id,
                state="planned",
                plan_digest=_raw_digest(plan.plan_digest),
                release_digest=_raw_digest(plan.target.release_digest),
                authority_revision=plan.target.authority_revision,
                fleet_digest=_raw_digest(plan.fleet_digest),
                topology_digest=_raw_digest(plan.topology_digest),
                agent_input_digest=_raw_digest(plan.agent_input_digest),
                target_platform_version=plan.target.platform_version,
                target_build_digest=plan.target.build_digest,
                tuf_targets_version=plan.target.tuf_targets_version,
                update_admin_grant=update_admin_grant,
                plan=document,
                current_batch=0,
                created_at=now,
                updated_at=now,
            )
            _validate_persisted_plan(rollout)
            try:
                with session.begin_nested():
                    session.add_all([job, rollout])
                    session.flush()
            except IntegrityError:
                existing = session.scalar(
                    select(UpdateRollout).where(
                        UpdateRollout.plan_digest == _raw_digest(plan.plan_digest)
                    )
                )
                if existing is not None:
                    return existing.id
                raise
            for batch_index, batch in enumerate(plan.batches):
                for node_order, node_id in enumerate(batch):
                    source = plan.source_for(node_id)
                    payload = plan.payload_for(node_id)["artifact"]
                    assert isinstance(payload, dict)
                    target_digest = payload["payload_sha256"]
                    assert isinstance(target_digest, str)
                    session.add(
                        UpdateRolloutNode(
                            rollout_id=rollout_id,
                            node_id=node_id,
                            batch_index=batch_index,
                            node_order=node_order,
                            is_canary=node_id == plan.canary_node,
                            state="pending",
                            source_identity_digest=_raw_digest(
                                _digest(_observation_document(source))
                            ),
                            target_artifact_digest=target_digest,
                            created_at=now,
                            updated_at=now,
                        )
                    )
            artifacts = {
                artifact.architecture: artifact for artifact in plan.target.artifacts
            }
            for node_order, node_id in enumerate(plan.offline_pending):
                source = plan.source_for(node_id)
                artifact = artifacts.get(source.architecture)
                if artifact is None:
                    raise ValueError("target release lacks offline agent artifact")
                session.add(
                    UpdateRolloutNode(
                        rollout_id=rollout_id,
                        node_id=node_id,
                        batch_index=-1,
                        node_order=node_order,
                        is_canary=False,
                        state="offline-pending",
                        source_identity_digest=_raw_digest(
                            _digest(_observation_document(source))
                        ),
                        target_artifact_digest=artifact.payload_sha256,
                        created_at=now,
                        updated_at=now,
                    )
                )
        return rollout_id

    def advance(self, rollout_id: str) -> str:
        with _ROLLOUT_ADVANCE_LOCK:
            return self._advance_locked(rollout_id)

    def _advance_locked(self, rollout_id: str) -> str:
        with self._sessions() as session:
            snapshot = session.get(UpdateRollout, rollout_id)
            if snapshot is None:
                raise KeyError(rollout_id)
            side_effect_state = snapshot.state
        if side_effect_state in {
            "compensating-withdrawal",
            "failure-publishing",
            "withdrawing",
            "publishing",
            "rollback-publishing",
        }:
            if side_effect_state == "compensating-withdrawal":
                return self._advance_publication_compensation(rollout_id)
            return self._advance_route_side_effect(rollout_id, side_effect_state)
        notify = False
        with self._sessions.begin() as session:
            rollout = self._rollout(session, rollout_id)
            now = _aware(self._clock())
            nodes = self._batch_nodes(session, rollout)
            if rollout.state == "planned":
                block_reason = self._execution_block_reason(
                    session, rollout, nodes, now
                )
                if block_reason is not None:
                    evidence = _raw_digest(
                        _digest(
                            {
                                "batch": rollout.current_batch,
                                "reason": block_reason,
                                "targets": [node.node_id for node in nodes],
                            }
                        )
                    )
                    rollout.state = "paused"
                    rollout.failure_reason = block_reason
                    rollout.failure_evidence_digest = evidence
                    rollout.updated_at = now
                    self._project_job(
                        session,
                        rollout,
                        "waiting-for-operator",
                        block_reason,
                        now,
                    )
                    return rollout.state
                if self._mutation_is_running(session, nodes):
                    reason = "node mutation overlap blocks platform update"
                    evidence = _raw_digest(
                        _digest(
                            {
                                "batch": rollout.current_batch,
                                "reason": reason,
                                "targets": [node.node_id for node in nodes],
                            }
                        )
                    )
                    rollout.state = "paused"
                    rollout.failure_reason = reason
                    rollout.failure_evidence_digest = evidence
                    rollout.updated_at = now
                    self._project_job(
                        session, rollout, "waiting-for-operator", reason, now
                    )
                    return rollout.state
                try:
                    self._leases.acquire_in_session(
                        session,
                        [node.node_id for node in nodes],
                        owner_kind="update-rollout",
                        owner_id=rollout.id,
                    )
                except NodeLeaseConflict:
                    reason = "node mutation lease blocks platform update"
                    evidence = _raw_digest(
                        _digest(
                            {
                                "batch": rollout.current_batch,
                                "reason": reason,
                                "targets": [node.node_id for node in nodes],
                            }
                        )
                    )
                    rollout.state = "paused"
                    rollout.failure_reason = reason
                    rollout.failure_evidence_digest = evidence
                    rollout.updated_at = now
                    self._project_job(
                        session, rollout, "waiting-for-operator", reason, now
                    )
                    return rollout.state
                rollout.state = "withdrawing"
                rollout.updated_at = now
            elif rollout.state == "updating":
                operations = [
                    self._operation(session, node.operation_id) for node in nodes
                ]
                for node, operation in zip(nodes, operations, strict=True):
                    if (
                        operation.state in {"queued", "running"}
                        and node.dispatch_at is not None
                        and now
                        >= _aware(node.dispatch_at)
                        + timedelta(seconds=_OPERATION_COMPLETION_SECONDS)
                    ):
                        if operation.state == "running":
                            self._expire_active_attempt(session, operation, now)
                            operation.state = "waiting-for-operator"
                        else:
                            operation.state = "failed"
                        operation.updated_at = now
                failed = next(
                    (
                        (node, operation)
                        for node, operation in zip(nodes, operations, strict=True)
                        if operation.state in {"failed", "waiting-for-operator"}
                    ),
                    None,
                )
                if failed is not None:
                    _node, operation = failed
                    original_states = {
                        peer.node_id: peer_operation.state
                        for peer, peer_operation in zip(nodes, operations, strict=True)
                    }
                    reason = self._operation_reason(session, operation)
                    evidence = _raw_digest(
                        _digest(
                            {
                                "operation_id": operation.id,
                                "reason": reason,
                                "state": operation.state,
                            }
                        )
                    )
                    for peer, peer_operation in zip(nodes, operations, strict=True):
                        if peer_operation.state == "queued":
                            peer_operation.state = "failed"
                        elif peer_operation.state == "running":
                            self._expire_active_attempt(session, peer_operation, now)
                            peer_operation.state = "waiting-for-operator"
                        peer.state = "failed"
                        peer.failure_reason = (
                            "batch peer update state uncertain"
                            if original_states[peer.node_id]
                            in {"running", "succeeded", "waiting-for-operator"}
                            else reason
                        )
                        peer.failure_evidence_digest = evidence
                        peer.updated_at = now
                    rollout.state = (
                        "failure-publishing"
                        if all(
                            original_states[peer.node_id] in {"failed", "queued"}
                            and self._source_is_unchanged(
                                session,
                                rollout,
                                peer,
                                now,
                                require_post_operation_contact=True,
                            )
                            for peer in nodes
                        )
                        else "paused"
                    )
                    rollout.failure_reason = reason
                    rollout.failure_evidence_digest = evidence
                    rollout.updated_at = now
                    self._project_job(
                        session, rollout, "waiting-for-operator", reason, now
                    )
                elif all(operation.state == "succeeded" for operation in operations):
                    for node, operation in zip(nodes, operations, strict=True):
                        node.activation_deadline = _aware(
                            operation.updated_at
                        ) + timedelta(seconds=_POST_SUCCESS_RECONNECT_SECONDS)
                    target_nodes = [
                        node
                        for node in nodes
                        if self._target_is_running(session, rollout, node, now)
                    ]
                    source_nodes = [
                        node
                        for node in nodes
                        if self._source_is_running(session, rollout, node, now)
                    ]
                    if len(target_nodes) == len(nodes):
                        soak_until = now + timedelta(seconds=_soak_seconds(rollout.plan))
                        for node in nodes:
                            identity = self._running_identity(session, node.node_id)
                            node.state = "soaking"
                            node.observed_platform_version = identity.platform_version
                            node.observed_build_digest = identity.build_digest
                            node.observed_protocol_version = identity.protocol_version
                            node.observed_active_slot = identity.active_slot
                            node.soak_until = soak_until
                            node.updated_at = now
                        rollout.state = "soaking"
                        rollout.soak_until = soak_until
                        rollout.updated_at = now
                    elif len(source_nodes) == len(nodes):
                        rollback_evidence = _raw_digest(
                            _digest(
                                {
                                    "operations": sorted(
                                        operation.id for operation in operations
                                    ),
                                    "state": "automatic-rollback-observed",
                                }
                            )
                        )
                        for node in nodes:
                            node.state = "rolling-back"
                            node.rollback_evidence_digest = rollback_evidence
                            node.updated_at = now
                        rollout.state = "rollback-publishing"
                        rollout.rollback_evidence_digest = rollback_evidence
                        rollout.updated_at = now
                    elif source_nodes:
                        mixed_evidence = _raw_digest(
                            _digest(
                                {
                                    "batch": rollout.current_batch,
                                    "source_nodes": sorted(
                                        node.node_id for node in source_nodes
                                    ),
                                    "state": "mixed-activation-outcome",
                                    "target_nodes": sorted(
                                        node.node_id for node in target_nodes
                                    ),
                                }
                            )
                        )
                        source_ids = {node.node_id for node in source_nodes}
                        for node in nodes:
                            if node.node_id in source_ids:
                                node.state = "rolled-back"
                                node.rollback_evidence_digest = mixed_evidence
                            else:
                                node.state = "failed"
                                node.failure_reason = (
                                    "batch peer update state uncertain"
                                )
                                node.failure_evidence_digest = mixed_evidence
                            node.updated_at = now
                        rollout.state = "paused"
                        rollout.failure_reason = "mixed agent activation outcome"
                        rollout.failure_evidence_digest = mixed_evidence
                        rollout.updated_at = now
                        self._project_job(
                            session,
                            rollout,
                            "waiting-for-operator",
                            rollout.failure_reason,
                            now,
                        )
                    elif any(
                        node.activation_deadline is not None
                        and now >= _aware(node.activation_deadline)
                        for node in nodes
                    ):
                        self._pause_for_identity_loss(
                            session,
                            rollout,
                            nodes,
                            now,
                            "agent activation reconnect timed out",
                        )
            elif rollout.state == "soaking":
                if rollout.soak_until is None:
                    raise ValueError("update rollout soak deadline is absent")
                if now >= _aware(rollout.soak_until):
                    if not all(
                        self._target_is_running(session, rollout, node, now)
                        for node in nodes
                    ):
                        self._pause_for_identity_loss(
                            session, rollout, nodes, now, "target identity lost during soak"
                        )
                    else:
                        rollout.state = "publishing"
                    rollout.updated_at = now
            elif rollout.state == "rolling-back":
                rollback_operations = [
                    self._operation(session, node.rollback_operation_id)
                    for node in nodes
                    if node.state == "rolling-back"
                ]
                if any(
                    operation.state in {"failed", "waiting-for-operator"}
                    for operation in rollback_operations
                ):
                    self._pause_for_identity_loss(
                        session,
                        rollout,
                        nodes,
                        now,
                        "agent rollback failed",
                    )
                elif rollback_operations and all(
                    operation.state == "succeeded"
                    for operation in rollback_operations
                ) and all(
                    self._source_is_running(session, rollout, node, now)
                    for node in nodes
                    if node.state == "rolling-back"
                ):
                    rollback_evidence = _raw_digest(
                        _digest(
                            {
                                "operations": sorted(
                                    operation.id for operation in rollback_operations
                                ),
                                "state": "rolled-back",
                            }
                        )
                    )
                    for node in nodes:
                        if node.state != "rolling-back":
                            continue
                        node.rollback_evidence_digest = rollback_evidence
                        node.updated_at = now
                    rollout.state = "rollback-publishing"
                    rollout.rollback_evidence_digest = rollback_evidence
                    rollout.updated_at = now
                elif any(
                    node.activation_deadline is not None
                    and now >= _aware(node.activation_deadline)
                    for node in nodes
                    if node.state == "rolling-back"
                ):
                    self._pause_for_identity_loss(
                        session,
                        rollout,
                        nodes,
                        now,
                        "agent rollback reconnect timed out",
                    )
            elif rollout.state == "paused" and not self._mutation_is_running(
                session, nodes
            ):
                automatically_retryable = (
                    rollout.failure_reason
                    == "node mutation overlap blocks platform update"
                    or (
                        rollout.failure_reason
                        == "node mutation lease blocks platform update"
                        and all(
                            session.get(NodeMutationLease, node.node_id) is None
                            for node in nodes
                        )
                    )
                )
                preflight_recovered = (
                    rollout.failure_reason
                    in {
                        "distributed workload quorum changed",
                        "planned source identity changed",
                    }
                    and self._execution_block_reason(
                        session, rollout, nodes, now
                    )
                    is None
                )
                if automatically_retryable or preflight_recovered:
                    rollout.state = "planned"
                    rollout.failure_reason = None
                    rollout.failure_evidence_digest = None
                    rollout.updated_at = now
                    self._project_job(session, rollout, "running", None, now)
            state = rollout.state
        if notify:
            self._agent_jobs.notify_available()
        return state

    def _advance_route_side_effect(self, rollout_id: str, state: str) -> str:
        releasing = state != "withdrawing"
        update_payloads: dict[str, dict[str, object]] = {}
        with self._sessions.begin() as session:
            rollout = self._rollout(session, rollout_id)
            if rollout.state != state:
                return rollout.state
            nodes = self._batch_nodes(session, rollout)
            batch_index = rollout.current_batch
            targets = tuple(node.node_id for node in nodes)
            if state == "withdrawing":
                update_payloads = {
                    node.node_id: _node_payload(rollout.plan, node.node_id)
                    for node in nodes
                }
            grant = self._leases.owned_grant_in_session(
                session,
                targets,
                owner_kind="update-rollout",
                owner_id=rollout.id,
            )
            if grant is None:
                raise RuntimeError("update rollout node mutation lease is absent")
            if releasing:
                block_reason = self._publication_block_reason(
                    session, rollout, nodes, _aware(self._clock()), state
                )
                if block_reason is not None:
                    if grant.state == "releasing":
                        self._rehold_lease_in_session(
                            session,
                            rollout,
                            targets,
                            _aware(self._clock()),
                        )
                    self._pause_for_identity_loss(
                        session,
                        rollout,
                        nodes,
                        _aware(self._clock()),
                        block_reason,
                    )
                    return rollout.state
                self._leases.mark_releasing_in_session(session, grant)
            elif grant.state != "held":
                raise RuntimeError("update rollout node mutation lease is releasing")
        if state == "withdrawing":
            drain = self._routes.withdraw(rollout_id, batch_index, targets)
            if (
                not isinstance(drain, RouteDrainReceipt)
                or drain.rollout_id != rollout_id
                or drain.batch_index != batch_index
                or drain.targets != tuple(sorted(targets))
                or not _aware(drain.drained_at)
                <= _aware(self._clock())
                < _aware(drain.expires_at)
            ):
                raise RuntimeError("route withdrawal drain receipt is invalid")
            evidence = _evidence(drain.evidence_digest)
        else:
            evidence = _evidence(
                self._routes.restore(rollout_id, batch_index, targets)
            )
        reserved_updates: dict[str, object] = {}
        if state == "withdrawing":
            with self._sessions.begin() as session:
                rollout = self._rollout(session, rollout_id)
                if rollout.state != state or rollout.current_batch != batch_index:
                    return rollout.state
                nodes = self._batch_nodes(session, rollout)
                now = _aware(self._clock())
                grant = self._leases.owned_grant_in_session(
                    session,
                    [node.node_id for node in nodes],
                    owner_kind="update-rollout",
                    owner_id=rollout.id,
                )
                if grant is None or grant.state != "held":
                    raise RuntimeError(
                        "update rollout held mutation lease is absent"
                    )
                for node in nodes:
                    node.state = "routes-withdrawn"
                    node.route_withdrawal_evidence_digest = evidence
                    node.updated_at = now
                    reserved_updates[node.node_id] = (
                        self._agent_jobs.reserve_update_authorization_in_session(
                            session,
                            rollout_id=rollout.id,
                            rollout_node_id=node.id,
                            operation="agent.update",
                            payload=update_payloads[node.node_id],
                            target_release_digest="sha256:" + rollout.release_digest,
                        )
                    )
            signed_updates = {
                node_id: self._agent_jobs.sign_update_authorization(reserved)
                for node_id, reserved in reserved_updates.items()
            }
        notify = False
        with self._sessions.begin() as session:
            rollout = self._rollout(session, rollout_id)
            if rollout.state != state or rollout.current_batch != batch_index:
                return rollout.state
            nodes = self._batch_nodes(session, rollout)
            now = _aware(self._clock())
            if releasing:
                grant = self._leases.owned_grant_in_session(
                    session,
                    [node.node_id for node in nodes],
                    owner_kind="update-rollout",
                    owner_id=rollout.id,
                )
                if grant is None or grant.state != "releasing":
                    raise RuntimeError(
                        "update rollout releasing mutation lease is absent"
                    )
                block_reason = self._publication_block_reason(
                    session, rollout, nodes, now, state
                )
                if block_reason is not None:
                    self._stage_publication_compensation(
                        session,
                        rollout,
                        nodes,
                        now,
                        block_reason,
                    )
                    return rollout.state
                self._leases.release_in_session(session, grant)
            if state == "withdrawing":
                for node in nodes:
                    try:
                        finalization = (
                            self._agent_jobs.finalize_update_authorization_in_session(
                                session,
                                reserved_updates[node.node_id],
                                signed_updates[node.node_id],
                            )
                        )
                        if getattr(finalization, "stale", None) is not False:
                            raise StaleAuthorizationResult
                        operation = getattr(finalization, "operation", None)
                        if not isinstance(operation, StoredAgentOperation):
                            raise TypeError("update finalization result is invalid")
                    except (StaleAuthorizationResult, ValueError):
                        session.rollback()
                        self._agent_jobs.mark_update_authorizations_stale(
                            list(reserved_updates.values())
                        )
                        raise ValueError(
                            "update authorization intent became stale"
                        )
                    node.operation_id = operation.id
                    node.state = "updating"
                    node.dispatch_at = now
                    node.activation_deadline = now + timedelta(
                        seconds=_OPERATION_COMPLETION_SECONDS
                    )
                    node.route_withdrawal_evidence_digest = evidence
                    node.updated_at = now
                rollout.state = "updating"
                rollout.updated_at = now
                notify = True
            elif state == "publishing":
                for node in nodes:
                    node.state = "accepted"
                    node.acceptance_evidence_digest = _raw_digest(
                        _digest(_identity_document(self._running_identity(session, node.node_id)))
                    )
                    node.completed_at = now
                    node.updated_at = now
                if self._has_next_batch(session, rollout):
                    rollout.current_batch += 1
                    rollout.state = "planned"
                    rollout.soak_until = None
                else:
                    offline_pending = session.scalar(
                        select(UpdateRolloutNode.id)
                        .where(
                            UpdateRolloutNode.rollout_id == rollout.id,
                            UpdateRolloutNode.state == "offline-pending",
                        )
                        .limit(1)
                    )
                    rollout.state = (
                        "partial" if offline_pending is not None else "completed"
                    )
                    rollout.completed_at = now
                    self._project_job(
                        session,
                        rollout,
                        "succeeded" if rollout.state == "completed" else "partial",
                        None if rollout.state == "completed" else "offline nodes remain pending",
                        now,
                    )
                rollout.updated_at = now
            elif state == "failure-publishing":
                rollout.state = "paused"
                rollout.updated_at = now
                self._project_job(
                    session,
                    rollout,
                    "waiting-for-operator",
                    rollout.failure_reason,
                    now,
                )
            else:
                for node in nodes:
                    if node.state != "rolling-back":
                        continue
                    failed_operation = self._operation(session, node.operation_id)
                    failed_operation.state = "compensated"
                    node.state = "rolled-back"
                    node.updated_at = now
                rollout.state = "waiting-for-approval"
                rollout.updated_at = now
                self._project_job(
                    session,
                    rollout,
                    "waiting-for-operator",
                    "rollout rollback requires administrator approval",
                    now,
                )
            result = rollout.state
        if notify:
            self._agent_jobs.notify_available()
        return result

    def _advance_publication_compensation(self, rollout_id: str) -> str:
        with self._sessions.begin() as session:
            rollout = self._rollout(session, rollout_id)
            if rollout.state != "compensating-withdrawal":
                return rollout.state
            nodes = self._batch_nodes(session, rollout)
            batch_index = rollout.current_batch
            targets = tuple(node.node_id for node in nodes)
            grant = self._leases.owned_grant_in_session(
                session,
                targets,
                owner_kind="update-rollout",
                owner_id=rollout.id,
            )
            if grant is None or grant.state != "releasing":
                raise RuntimeError(
                    "update rollout compensating mutation lease is absent"
                )

        self._routes.withdraw(rollout_id, batch_index, targets)

        with self._sessions.begin() as session:
            rollout = self._rollout(session, rollout_id)
            if (
                rollout.state != "compensating-withdrawal"
                or rollout.current_batch != batch_index
            ):
                return rollout.state
            nodes = self._batch_nodes(session, rollout)
            grant = self._leases.owned_grant_in_session(
                session,
                [node.node_id for node in nodes],
                owner_kind="update-rollout",
                owner_id=rollout.id,
            )
            if grant is None or grant.state != "releasing":
                raise RuntimeError(
                    "update rollout compensating mutation lease is absent"
                )
            now = _aware(self._clock())
            self._rehold_lease_in_session(session, rollout, targets, now)
            rollout.state = "paused"
            rollout.updated_at = now
            self._project_job(
                session,
                rollout,
                "waiting-for-operator",
                rollout.failure_reason,
                now,
            )
            return rollout.state

    @staticmethod
    def _project_job(
        session: Session,
        rollout: UpdateRollout,
        state: str,
        reason: str | None,
        now: datetime,
    ) -> None:
        job = session.get(Job, rollout.job_id)
        if job is None:
            raise ValueError("update rollout job binding is absent")
        job.state = state
        job.status_reason = reason
        job.updated_at = now
        if state == "succeeded":
            job.result = {
                "plan_digest": "sha256:" + rollout.plan_digest,
                "release_digest": "sha256:" + rollout.release_digest,
                "state": rollout.state,
            }

    def _pause_for_identity_loss(
        self,
        session: Session,
        rollout: UpdateRollout,
        nodes: list[UpdateRolloutNode],
        now: datetime,
        reason: str,
    ) -> None:
        evidence = _raw_digest(
            _digest(
                {
                    "batch": rollout.current_batch,
                    "reason": reason,
                    "targets": [node.node_id for node in nodes],
                }
            )
        )
        rollout.state = "paused"
        rollout.failure_reason = reason
        rollout.failure_evidence_digest = evidence
        rollout.updated_at = now
        for node in nodes:
            node.state = "failed"
            node.failure_reason = reason
            node.failure_evidence_digest = evidence
            node.updated_at = now
        self._project_job(session, rollout, "waiting-for-operator", reason, now)

    def _stage_publication_compensation(
        self,
        session: Session,
        rollout: UpdateRollout,
        nodes: list[UpdateRolloutNode],
        now: datetime,
        reason: str,
    ) -> None:
        evidence = _raw_digest(
            _digest(
                {
                    "batch": rollout.current_batch,
                    "reason": reason,
                    "state": "compensating-withdrawal",
                    "targets": [node.node_id for node in nodes],
                }
            )
        )
        rollout.state = "compensating-withdrawal"
        rollout.failure_reason = reason
        rollout.failure_evidence_digest = evidence
        rollout.updated_at = now
        for node in nodes:
            node.state = "failed"
            node.failure_reason = reason
            node.failure_evidence_digest = evidence
            node.updated_at = now
        self._project_job(session, rollout, "waiting-for-operator", reason, now)

    def authorize_rollback(
        self,
        rollout_id: str,
        actor: str,
        request_id: str,
        *,
        admin_grant_factory: Callable[..., dict[str, object]],
    ) -> str:
        """Persist API authorization; only the worker may consume it via signer IPC."""
        _actor(actor)
        _request_id(request_id)
        if not callable(admin_grant_factory):
            raise TypeError("rollback admin grant factory is invalid")
        now = _aware(self._clock())
        with self._sessions.begin() as session:
            rollout = self._rollout(session, rollout_id)
            if rollout.state != "paused":
                raise ValueError("update rollout is not paused")
            nodes = self._batch_nodes(session, rollout)
            failed = self._rollback_candidates(session, rollout, nodes, now)
            if not failed:
                raise ValueError("update rollout has no mutated node to roll back")
            if rollout.job_id is None:
                raise ValueError("update rollout job binding is absent")
            grant = admin_grant_factory(
                rollout_id=rollout.id,
                parent_job_id=rollout.job_id,
                node_ids=tuple(node.node_id for node in failed),
                target_release_digest=None,
            )
            if not isinstance(grant, dict):
                raise TypeError("API-issued rollback authorization grant is invalid")
            rollout.rollback_admin_grant = dict(grant)
            rollout.updated_at = now
        return "paused"

    def begin_rollback(
        self,
        rollout_id: str,
        actor: str,
        request_id: str,
        admin_grant: dict[str, object] | None = None,
        *,
        admin_grant_factory: Callable[..., dict[str, object]] | None = None,
    ) -> str:
        _actor(actor)
        _request_id(request_id)
        now = _aware(self._clock())
        reserved_rollbacks: dict[str, object] = {}
        with self._sessions.begin() as session:
            rollout = self._rollout(session, rollout_id)
            if rollout.state != "paused":
                raise ValueError("update rollout is not paused")
            nodes = self._batch_nodes(session, rollout)
            failed = self._rollback_candidates(session, rollout, nodes, now)
            if not failed:
                raise ValueError("update rollout has no mutated node to roll back")
            if admin_grant is not None and admin_grant_factory is not None:
                raise ValueError("rollback admin grant inputs conflict")
            if admin_grant_factory is not None:
                if rollout.job_id is None:
                    raise ValueError("update rollout job binding is absent")
                admin_grant = admin_grant_factory(
                    rollout_id=rollout.id,
                    parent_job_id=rollout.job_id,
                    node_ids=tuple(node.node_id for node in failed),
                    target_release_digest=None,
                )
                if not isinstance(admin_grant, dict):
                    raise TypeError("API-issued rollback authorization grant is invalid")
            if admin_grant is not None:
                rollout.rollback_admin_grant = dict(admin_grant)
            for node in failed:
                if node.rollback_operation_id is not None:
                    prior = self._operation(session, node.rollback_operation_id)
                    history = list(node.operation_history)
                    history.append(
                        {
                            "id": prior.id,
                            "kind": prior.kind,
                            "role": "rollback",
                            "state": prior.state,
                        }
                    )
                    node.operation_history = history
                    node.rollback_operation_id = None
                reserved_rollbacks[node.node_id] = (
                    self._agent_jobs.reserve_update_authorization_in_session(
                        session,
                        rollout_id=rollout.id,
                        rollout_node_id=node.id,
                        operation="agent.rollback",
                        payload={},
                        target_release_digest=None,
                    )
                )
        signed_rollbacks = {
            node_id: self._agent_jobs.sign_update_authorization(reserved)
            for node_id, reserved in reserved_rollbacks.items()
        }
        with self._sessions.begin() as session:
            rollout = self._rollout(session, rollout_id)
            if rollout.state != "paused":
                raise ValueError("update rollout is not paused")
            nodes = self._batch_nodes(session, rollout)
            failed = [node for node in nodes if node.node_id in reserved_rollbacks]
            for node in failed:
                try:
                    finalization = (
                        self._agent_jobs.finalize_update_authorization_in_session(
                            session,
                            reserved_rollbacks[node.node_id],
                            signed_rollbacks[node.node_id],
                        )
                    )
                    if getattr(finalization, "stale", None) is not False:
                        raise StaleAuthorizationResult
                    operation = getattr(finalization, "operation", None)
                    if not isinstance(operation, StoredAgentOperation):
                        raise TypeError("rollback finalization result is invalid")
                except (StaleAuthorizationResult, ValueError):
                    session.rollback()
                    self._agent_jobs.mark_update_authorizations_stale(
                        list(reserved_rollbacks.values())
                    )
                    raise ValueError(
                        "update authorization intent became stale"
                    )
                node.rollback_operation_id = operation.id
                node.state = "rolling-back"
                node.activation_deadline = now + timedelta(
                    seconds=_POST_SUCCESS_RECONNECT_SECONDS
                )
                node.updated_at = now
            rollout.state = "rolling-back"
            rollout.updated_at = now
            self._project_job(session, rollout, "running", None, now)
            session.add(
                AuditEvent(
                    request_id=request_id,
                    actor=actor,
                    action="platform.update.rollback",
                    authority_revision=rollout.authority_revision,
                    targets=[node.node_id for node in failed],
                    occurred_at=now,
                )
            )
        self._agent_jobs.notify_available()
        return "rolling-back"

    def _rollback_candidates(
        self,
        session: Session,
        rollout: UpdateRollout,
        nodes: list[UpdateRolloutNode],
        now: datetime,
    ) -> list[UpdateRolloutNode]:
        return [
            node
            for node in nodes
            if node.state == "failed"
            and (
                node.failure_reason == "batch peer update state uncertain"
                or not self._source_is_unchanged(session, rollout, node, now)
            )
            and not self._source_is_running(session, rollout, node, now)
        ]

    def approve_resume(
        self,
        rollout_id: str,
        actor: str,
        request_id: str,
        reason: str,
    ) -> str:
        _actor(actor)
        _request_id(request_id)
        if not reason.strip() or len(reason) > 1024:
            raise ValueError("update rollout approval reason is invalid")
        now = _aware(self._clock())
        with self._sessions.begin() as session:
            rollout = self._rollout(session, rollout_id)
            if rollout.state not in {"paused", "waiting-for-approval"}:
                raise ValueError("update rollout is not waiting for approval")
            nodes = self._batch_nodes(session, rollout)
            if rollout.state == "paused" and not all(
                node.state == "failed"
                and self._source_is_unchanged(session, rollout, node, now)
                for node in nodes
            ):
                raise ValueError("update rollout requires rollback before approval")
            evidence = _raw_digest(
                _digest(
                    {
                        "actor": actor,
                        "reason": reason,
                        "request_id": request_id,
                        "rollout_id": rollout_id,
                    }
                )
            )
            rollout.approval_actor = actor
            rollout.approval_request_id = request_id
            rollout.approval_reason = reason
            rollout.approval_at = now
            rollout.approval_evidence_digest = evidence
            rollout.state = "planned"
            rollout.failure_reason = None
            rollout.failure_evidence_digest = None
            rollout.updated_at = now
            self._project_job(session, rollout, "running", None, now)
            for node in nodes:
                if node.state not in {"failed", "rolled-back"}:
                    continue
                history = list(node.operation_history)
                for operation_id, role in (
                    (node.operation_id, "update"),
                    (node.rollback_operation_id, "rollback"),
                ):
                    if operation_id is None:
                        continue
                    operation = self._operation(session, operation_id)
                    history.append(
                        {
                            "id": operation.id,
                            "kind": operation.kind,
                            "role": role,
                            "state": operation.state,
                        }
                    )
                node.operation_history = history
                node.operation_id = None
                node.rollback_operation_id = None
                node.state = "pending"
                node.dispatch_at = None
                node.activation_deadline = None
                node.soak_until = None
                node.observed_platform_version = None
                node.observed_build_digest = None
                node.observed_protocol_version = None
                node.observed_active_slot = None
                node.route_withdrawal_evidence_digest = None
                node.acceptance_evidence_digest = None
                node.failure_reason = None
                node.failure_evidence_digest = None
                node.rollback_evidence_digest = None
                node.completed_at = None
                node.updated_at = now
            session.add(
                AuditEvent(
                    request_id=request_id,
                    actor=actor,
                    action="platform.update.resume-approved",
                    authority_revision=rollout.authority_revision,
                    targets=[node.node_id for node in self._batch_nodes(session, rollout)],
                    occurred_at=now,
                )
            )
        return "planned"

    @staticmethod
    def _rollout(session: Session, rollout_id: str) -> UpdateRollout:
        rollout = session.scalar(
            select(UpdateRollout)
            .where(UpdateRollout.id == rollout_id)
            .with_for_update(of=UpdateRollout)
        )
        if rollout is None:
            raise KeyError(rollout_id)
        _validate_persisted_plan(rollout)
        return rollout

    @staticmethod
    def _batch_nodes(
        session: Session, rollout: UpdateRollout
    ) -> list[UpdateRolloutNode]:
        nodes = list(
            session.scalars(
                select(UpdateRolloutNode)
                .where(
                    UpdateRolloutNode.rollout_id == rollout.id,
                    UpdateRolloutNode.batch_index == rollout.current_batch,
                )
                .order_by(UpdateRolloutNode.node_order)
                .with_for_update(of=UpdateRolloutNode)
            )
        )
        if not nodes:
            raise ValueError("update rollout batch is empty")
        batches = rollout.plan.get("batches")
        if (
            not isinstance(batches, list)
            or rollout.current_batch >= len(batches)
            or not isinstance(batches[rollout.current_batch], list)
            or [node.node_id for node in nodes] != batches[rollout.current_batch]
        ):
            raise ValueError("persisted update batch binding disagrees")
        for node in nodes:
            source_digest = _raw_digest(
                _digest(_node_source(rollout.plan, node.node_id))
            )
            payload = _node_payload(rollout.plan, node.node_id)
            artifact = payload.get("artifact")
            if (
                not isinstance(artifact, dict)
                or node.source_identity_digest != source_digest
                or node.target_artifact_digest != artifact.get("payload_sha256")
            ):
                raise ValueError("persisted update node binding disagrees")
        return nodes

    @staticmethod
    def _operation(
        session: Session, operation_id: str | None
    ) -> StoredAgentOperation:
        if operation_id is None:
            raise ValueError("update rollout operation binding is absent")
        operation = session.get(StoredAgentOperation, operation_id)
        if operation is None:
            raise KeyError(operation_id)
        return operation

    @staticmethod
    def _operation_reason(
        session: Session, operation: StoredAgentOperation
    ) -> str:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == operation.current_attempt,
            )
        )
        if attempt is not None and isinstance(attempt.result, dict):
            reason = attempt.result.get("reason")
            if isinstance(reason, str) and reason:
                return reason[:1024]
        return "agent update failed"

    @staticmethod
    def _running_identity(session: Session, node_id: str) -> AgentNode:
        node = session.get(AgentNode, node_id)
        if node is None:
            raise KeyError(node_id)
        return node

    def _target_is_running(
        self,
        session: Session,
        rollout: UpdateRollout,
        node: UpdateRolloutNode,
        now: datetime,
    ) -> bool:
        running = self._running_identity(session, node.node_id)
        source = _node_source(rollout.plan, node.node_id)
        target = rollout.plan.get("target")
        operation = self._operation(session, node.operation_id)
        if not isinstance(target, dict):
            raise TypeError("persisted update target is invalid")
        source_generation = source.get("supervisor_generation")
        source_slot = source.get("active_slot")
        last_seen = running.last_seen_at
        return (
            running.state == "active"
            and self._agent_is_current(running, now)
            and running.platform_version == rollout.target_platform_version
            and running.build_digest == rollout.target_build_digest
            and isinstance(running.protocol_version, int)
            and isinstance(target.get("protocol_minimum"), int)
            and isinstance(target.get("protocol_maximum"), int)
            and target["protocol_minimum"]
            <= running.protocol_version
            <= target["protocol_maximum"]
            and source_slot in {"A", "B"}
            and running.active_slot in {"A", "B"}
            and running.active_slot != source_slot
            and running.agent_sha256 == node.target_artifact_digest
            and isinstance(source_generation, int)
            and isinstance(running.supervisor_generation, int)
            and running.supervisor_generation > source_generation
            and running.self_test_passed is True
            and running.supervisor_ready_generation == running.supervisor_generation
            and isinstance(running.contact_certificate_serial, str)
            and bool(running.contact_certificate_serial)
            and isinstance(running.contact_observation_digest, str)
            and _SHA256.fullmatch(running.contact_observation_digest) is not None
            and self._authenticated_contact_is_current(session, running, now)
            and isinstance(running.capabilities, list)
            and {"agent.rollback", "agent.update"} <= set(running.capabilities)
            and last_seen is not None
            and _aware(last_seen) >= _aware(operation.updated_at)
        )

    @staticmethod
    def _authenticated_contact_is_current(
        session: Session,
        node: AgentNode,
        now: datetime,
    ) -> bool:
        serial = node.contact_certificate_serial
        observed_at = node.last_seen_at
        if not isinstance(serial, str) or observed_at is None:
            return False
        certificate = session.get(AgentCertificate, serial)
        if (
            certificate is None
            or certificate.node_id != node.node_id
            or certificate.state != "active"
            or certificate.revoked_at is not None
            or certificate.ca_revoked_at is not None
            or not _aware(certificate.not_before) <= _aware(now)
            <= _aware(certificate.not_after)
        ):
            return False
        runtime_identity = {
            "active_slot": node.active_slot,
            "architecture": node.architecture,
            "agent_sha256": node.agent_sha256,
            "build_digest": node.build_digest,
            "platform_version": node.platform_version,
            "self_test_passed": node.self_test_passed,
            "supervisor_generation": node.supervisor_generation,
            "supervisor_ready_generation": node.supervisor_ready_generation,
        }
        digest = hashlib.sha256(
            canonical_message(
                {
                    "certificate_fingerprint": certificate.fingerprint,
                    "certificate_serial": certificate.serial,
                    "node_id": node.node_id,
                    "observed_at": _aware(observed_at).isoformat(),
                    "runtime_identity": runtime_identity,
                }
            )
        ).hexdigest()
        return digest == node.contact_observation_digest

    def _source_is_running(
        self,
        session: Session,
        rollout: UpdateRollout,
        node: UpdateRolloutNode,
        now: datetime,
    ) -> bool:
        source = _node_source(rollout.plan, node.node_id)
        running = session.get(AgentNode, node.node_id)
        source_generation = source.get("supervisor_generation")
        operation_id = node.rollback_operation_id or node.operation_id
        operation = (
            self._operation(session, operation_id)
            if operation_id is not None
            else None
        )
        return (
            running is not None
            and self._agent_is_current(running, now)
            and isinstance(source_generation, int)
            and isinstance(running.supervisor_generation, int)
            and running.supervisor_generation > source_generation
            and running.self_test_passed is True
            and running.supervisor_ready_generation == running.supervisor_generation
            and isinstance(running.contact_certificate_serial, str)
            and bool(running.contact_certificate_serial)
            and isinstance(running.contact_observation_digest, str)
            and _SHA256.fullmatch(running.contact_observation_digest) is not None
            and self._authenticated_contact_is_current(session, running, now)
            and all(
                getattr(running, field) == source[field]
                for field in (
                    "platform_version",
                    "build_digest",
                    "protocol_version",
                    "active_slot",
                    "agent_sha256",
                )
            )
            and (
                operation is None
                or (
                    running.last_seen_at is not None
                    and _aware(running.last_seen_at)
                    >= _aware(operation.updated_at)
                )
            )
        )

    def _source_is_unchanged(
        self,
        session: Session,
        rollout: UpdateRollout,
        node: UpdateRolloutNode,
        now: datetime | None = None,
        *,
        require_post_operation_contact: bool = False,
    ) -> bool:
        source = _node_source(rollout.plan, node.node_id)
        running = session.get(AgentNode, node.node_id)
        operation = (
            self._operation(session, node.operation_id)
            if require_post_operation_contact and node.operation_id is not None
            else None
        )
        return (
            running is not None
            and (now is None or self._agent_is_current(running, now))
            and all(
                getattr(running, field) == source[field]
                for field in (
                    "platform_version",
                    "build_digest",
                    "protocol_version",
                    "active_slot",
                    "agent_sha256",
                    "supervisor_generation",
                )
            )
            and (
                operation is None
                or (
                    running.last_seen_at is not None
                    and _aware(running.last_seen_at)
                    >= _aware(operation.updated_at)
                )
            )
        )

    @staticmethod
    def _agent_is_current(running: AgentNode, now: datetime) -> bool:
        return (
            running.state == "active"
            and isinstance(running.capabilities, list)
            and {"agent.rollback", "agent.update"}
            <= set(running.capabilities)
            and running.last_seen_at is not None
            and _aware(running.last_seen_at)
            >= _aware(now) - timedelta(seconds=_AGENT_FRESHNESS_SECONDS)
        )

    def _publication_block_reason(
        self,
        session: Session,
        rollout: UpdateRollout,
        nodes: list[UpdateRolloutNode],
        now: datetime,
        state: str,
    ) -> str | None:
        if state == "publishing":
            if not all(
                self._target_is_running(session, rollout, node, now)
                for node in nodes
            ):
                return "target identity lost during route publication"
        elif state == "rollback-publishing":
            if not all(
                self._source_is_running(session, rollout, node, now)
                for node in nodes
            ):
                return "source identity lost during route publication"
        elif state == "failure-publishing" and not all(
            self._source_is_unchanged(
                session,
                rollout,
                node,
                now,
                require_post_operation_contact=True,
            )
            for node in nodes
        ):
            return "source identity lost during route publication"
        return None

    @staticmethod
    def _expire_active_attempt(
        session: Session,
        operation: StoredAgentOperation,
        now: datetime,
    ) -> None:
        if operation.current_attempt < 1:
            return
        attempt = session.scalar(
            select(AgentOperationAttempt)
            .where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == operation.current_attempt,
            )
            .with_for_update(of=AgentOperationAttempt)
        )
        if attempt is not None and attempt.state == "running":
            attempt.state = "expired"
            attempt.lease_deadline = now

    @staticmethod
    def _rehold_lease_in_session(
        session: Session,
        rollout: UpdateRollout,
        targets: tuple[str, ...],
        now: datetime,
    ) -> None:
        rows = tuple(
            session.scalars(
                select(NodeMutationLease)
                .where(NodeMutationLease.node_id.in_(targets))
                .order_by(NodeMutationLease.node_id)
                .with_for_update(of=NodeMutationLease)
            )
        )
        if (
            tuple(row.node_id for row in rows) != tuple(sorted(targets))
            or any(
                row.owner_kind != "update-rollout"
                or row.owner_id != rollout.id
                or row.state != "releasing"
                for row in rows
            )
        ):
            raise RuntimeError("update rollout releasing mutation lease is absent")
        for row in rows:
            row.state = "held"
            row.updated_at = now

    @staticmethod
    def _has_next_batch(session: Session, rollout: UpdateRollout) -> bool:
        return (
            session.scalar(
                select(UpdateRolloutNode.id)
                .where(
                    UpdateRolloutNode.rollout_id == rollout.id,
                    UpdateRolloutNode.batch_index > rollout.current_batch,
                )
                .limit(1)
            )
            is not None
        )

    @staticmethod
    def _mutation_is_running(
        session: Session, nodes: list[UpdateRolloutNode]
    ) -> bool:
        mutations = {
            "agent.rollback",
            "agent.update",
            "release.install",
            "workload.prepare",
            "workload.start",
            "workload.stop",
        }
        return (
            session.scalar(
                select(StoredAgentOperation.id)
                .where(
                    StoredAgentOperation.node_id.in_(
                        [node.node_id for node in nodes]
                    ),
                    StoredAgentOperation.kind.in_(mutations),
                    StoredAgentOperation.state == "running",
                )
                .limit(1)
            )
            is not None
        )

    def _execution_block_reason(
        self,
        session: Session,
        rollout: UpdateRollout,
        nodes: list[UpdateRolloutNode],
        now: datetime,
    ) -> str | None:
        required_capabilities = {"agent.rollback", "agent.update"}
        for node in nodes:
            running = session.get(AgentNode, node.node_id)
            if (
                running is None
                or running.state != "active"
                or not isinstance(running.capabilities, list)
                or not required_capabilities <= set(running.capabilities)
                or running.last_seen_at is None
                or _aware(running.last_seen_at)
                < now - timedelta(seconds=_AGENT_FRESHNESS_SECONDS)
                or not (
                    self._source_is_unchanged(
                        session, rollout, node, now
                    )
                    or self._source_is_running(
                        session, rollout, node, now
                    )
                )
            ):
                return "planned source identity changed"
        batch_targets = {node.node_id for node in nodes}
        workloads = rollout.plan.get("workloads")
        if not isinstance(workloads, list):
            raise TypeError("persisted update workloads are invalid")
        for workload in workloads:
            if not isinstance(workload, dict):
                raise TypeError("persisted update workload is invalid")
            members = workload.get("members")
            minimum = workload.get("minimum_available")
            replicas = workload.get("replicas", [])
            workload_id = workload.get("workload_id")
            if (
                not isinstance(members, list)
                or not isinstance(minimum, int)
                or not isinstance(replicas, list)
                or not isinstance(workload_id, str)
            ):
                raise TypeError("persisted update workload is invalid")
            current_workloads: dict[str, object] | None = None
            if replicas:
                current_workloads = {}
                for active_run in session.scalars(
                    select(RecipeRun).where(RecipeRun.state == "running")
                ):
                    for active_node in session.scalars(
                        select(RunNode).where(RunNode.run_id == active_run.id)
                    ):
                        current_workloads.setdefault(active_node.node_id, set()).add(
                            active_run.alias
                        )
            available = 0
            for node_id in members:
                if node_id in batch_targets:
                    continue
                running = session.get(AgentNode, node_id)
                lease = session.get(NodeMutationLease, node_id)
                workload_healthy = True
                if current_workloads is not None:
                    workload_healthy = (
                        node_id in current_workloads
                        and workload_id
                        in current_workloads.get(node_id, {})
                    )
                if (
                    running is not None
                    and self._agent_is_current(running, now)
                    and workload_healthy
                    and (
                        lease is None
                        or (
                            lease.owner_kind == "update-rollout"
                            and lease.owner_id == rollout.id
                        )
                    )
                ):
                    available += 1
            if available < minimum:
                return "distributed workload quorum changed"
        return None


def _conflicts(
    candidate: str,
    batch: list[str],
    workloads: tuple[DistributedWorkload, ...],
    topology: tuple[TopologyExclusion, ...],
) -> bool:
    for workload in workloads:
        if candidate not in workload.members:
            continue
        peers = set(batch) & set(workload.members)
        if peers:
            return True
    for exclusion in topology:
        if candidate not in exclusion.members:
            continue
        unavailable = len(set(batch).intersection(exclusion.members))
        if unavailable >= exclusion.maximum_unavailable:
            return True
    return False


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(value)
    if match is None:
        raise ValueError("semantic version is invalid")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _raw_digest(value: str) -> str:
    if _DIGEST.fullmatch(value) is None:
        raise ValueError("prefixed SHA-256 digest is invalid")
    return value.removeprefix("sha256:")


def _evidence(value: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError("rollout evidence digest is invalid")
    return value


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _actor(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError("update rollout actor is invalid")


def _request_id(value: str) -> None:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("update rollout request ID is invalid") from error
    if str(parsed) != value:
        raise ValueError("update rollout request ID is invalid")


def _target_document(target: TargetPlatform) -> dict[str, object]:
    return {
        **asdict(target),
        "target_name": target.target_name,
        "target_sha256": target.target_sha256,
    }


def _plan_document(plan: UpdatePlan) -> dict[str, object]:
    return {
        "agent_input_digest": plan.agent_input_digest,
        "batches": [list(batch) for batch in plan.batches],
        "canary_node": plan.canary_node,
        "fleet_digest": plan.fleet_digest,
        "incompatible": list(plan.incompatible),
        "node_payloads": {
            node_id: plan.payload_for(node_id)
            for batch in plan.batches
            for node_id in batch
        },
        "offline_pending": list(plan.offline_pending),
        "plan_digest": plan.plan_digest,
        "policy": asdict(plan.policy),
        "schema_version": 1,
        "soak_seconds": plan.soak_seconds,
        "source_observations": {
            item.node_id: _observation_document(item)
            for item in plan.source_observations
        },
        "target": _target_document(plan.target),
        "topology_digest": plan.topology_digest,
        "topology_exclusions": [asdict(item) for item in plan.topology_exclusions],
        "node_architectures": [list(item) for item in plan.node_architectures],
        "workloads": [_workload_document(item) for item in plan.workloads],
    }


def _validate_persisted_plan(rollout: UpdateRollout) -> None:
    document = rollout.plan
    required = {
        "agent_input_digest",
        "batches",
        "canary_node",
        "fleet_digest",
        "incompatible",
        "node_architectures",
        "node_payloads",
        "offline_pending",
        "plan_digest",
        "policy",
        "schema_version",
        "soak_seconds",
        "source_observations",
        "target",
        "topology_digest",
        "topology_exclusions",
        "workloads",
    }
    if not isinstance(document, dict) or set(document) != required:
        raise ValueError("persisted update plan fields are invalid")
    target = document.get("target")
    if not isinstance(target, dict):
        raise TypeError("persisted update target is invalid")
    if (
        document.get("schema_version") != 1
        or document.get("plan_digest") != "sha256:" + rollout.plan_digest
        or document.get("fleet_digest") != "sha256:" + rollout.fleet_digest
        or document.get("topology_digest") != "sha256:" + rollout.topology_digest
        or document.get("agent_input_digest")
        != "sha256:" + rollout.agent_input_digest
        or target.get("authority_revision") != rollout.authority_revision
        or target.get("platform_version") != rollout.target_platform_version
        or target.get("build_digest") != rollout.target_build_digest
        or target.get("release_digest") != "sha256:" + rollout.release_digest
        or target.get("target_name")
        != (
            f"platform/releases/{rollout.target_platform_version}/"
            f"{rollout.release_digest}.json"
        )
        or target.get("target_sha256") != rollout.release_digest
        or target.get("tuf_targets_version") != rollout.tuf_targets_version
    ):
        raise ValueError("persisted update plan pins disagree")
    policy = document.get("policy")
    if (
        not isinstance(policy, dict)
        or document.get("soak_seconds") != policy.get("soak_seconds")
    ):
        raise ValueError("persisted update plan policy disagrees")
    digest_content = {
        "agent_input_digest": document["agent_input_digest"],
        "batches": document["batches"],
        "canary_node": document["canary_node"],
        "fleet_digest": document["fleet_digest"],
        "incompatible": document["incompatible"],
        "node_architectures": document["node_architectures"],
        "offline_pending": document["offline_pending"],
        "policy": document["policy"],
        "target": target,
        "topology_digest": document["topology_digest"],
    }
    if _digest(digest_content) != document["plan_digest"]:
        raise ValueError("persisted update plan digest disagrees")
    observations = document.get("source_observations")
    workloads = document.get("workloads")
    exclusions = document.get("topology_exclusions")
    if (
        not isinstance(observations, dict)
        or not isinstance(workloads, list)
        or not isinstance(exclusions, list)
    ):
        raise TypeError("persisted update plan inputs are invalid")
    ordered_observations = [observations[key] for key in sorted(observations)]
    if (
        _digest(ordered_observations) != document["agent_input_digest"]
        or _digest(
            [
                {
                    "architecture": item.get("architecture"),
                    "node_id": item.get("node_id"),
                    "state": item.get("state"),
                }
                for item in ordered_observations
                if isinstance(item, dict)
            ]
        )
        != document["fleet_digest"]
        or _digest({"exclusions": exclusions, "workloads": workloads})
        != document["topology_digest"]
    ):
        raise ValueError("persisted update plan input digest disagrees")
    _validate_node_payloads(document)


def _soak_seconds(document: dict[str, object]) -> int:
    value = document.get("soak_seconds")
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 86_400:
        raise ValueError("persisted rollout soak interval is invalid")
    return value


def _validate_node_payloads(document: dict[str, object]) -> None:
    target = document.get("target")
    payloads = document.get("node_payloads")
    bindings = document.get("node_architectures")
    if (
        not isinstance(target, dict)
        or not isinstance(payloads, dict)
        or not isinstance(bindings, list)
        or not isinstance(target.get("artifacts"), (list, tuple))
    ):
        raise TypeError("persisted update payload authority is invalid")
    artifacts = {
        item.get("architecture"): item
        for item in target["artifacts"]
        if isinstance(item, dict)
    }
    architecture_by_node: dict[str, str] = {}
    for binding in bindings:
        if (
            not isinstance(binding, list)
            or len(binding) != 2
            or not all(isinstance(item, str) for item in binding)
        ):
            raise ValueError("persisted update architecture binding is invalid")
        architecture_by_node[binding[0]] = binding[1]
    if set(payloads) != set(architecture_by_node):
        raise ValueError("persisted update payload target set disagrees")
    for node_id, architecture in architecture_by_node.items():
        artifact = artifacts.get(architecture)
        expected = {
            "artifact": artifact,
            "release": {
                "build_digest": target.get("build_digest"),
                "platform_version": target.get("platform_version"),
                "protocol_maximum": target.get("protocol_maximum"),
                "protocol_minimum": target.get("protocol_minimum"),
            },
        }
        if artifact is None or payloads.get(node_id) != expected:
            raise ValueError("persisted update node payload disagrees")


def _node_payload(document: dict[str, object], node_id: str) -> dict[str, object]:
    payloads = document.get("node_payloads")
    if not isinstance(payloads, dict):
        raise TypeError("persisted rollout payloads are invalid")
    payload = payloads.get(node_id)
    if not isinstance(payload, dict):
        raise TypeError("persisted rollout node payload is invalid")
    return payload


def _node_source(document: dict[str, object], node_id: str) -> dict[str, object]:
    observations = document.get("source_observations")
    if not isinstance(observations, dict):
        raise TypeError("persisted rollout source observations are invalid")
    observation = observations.get(node_id)
    if not isinstance(observation, dict):
        raise TypeError("persisted rollout node source is invalid")
    return observation


def _identity_document(node: AgentNode) -> dict[str, object]:
    return {
        "active_slot": node.active_slot,
        "agent_sha256": node.agent_sha256,
        "build_digest": node.build_digest,
        "contact_certificate_serial": node.contact_certificate_serial,
        "contact_observation_digest": node.contact_observation_digest,
        "node_id": node.node_id,
        "platform_version": node.platform_version,
        "protocol_version": node.protocol_version,
        "supervisor_generation": node.supervisor_generation,
        "supervisor_ready_generation": node.supervisor_ready_generation,
        "self_test_passed": node.self_test_passed,
    }


def _observation_document(observation: AgentObservation) -> dict[str, object]:
    document = asdict(observation)
    if observation.last_seen_at is not None:
        document["last_seen_at"] = observation.last_seen_at.isoformat()
    return document


def _workload_document(workload: DistributedWorkload) -> dict[str, object]:
    return {
        "members": list(workload.members),
        "minimum_available": workload.minimum_available,
        "replicas": [
            {
                "evidence_digest": replica.evidence_digest,
                "healthy": replica.healthy,
                "node_id": replica.node_id,
                "observed_at": replica.observed_at.isoformat(),
                "serving": replica.serving,
            }
            for replica in workload.replicas
        ],
        "workload_id": workload.workload_id,
    }
