"""Generic workload-package desired state and rollout orchestration.

The package lane deliberately sits on top of the existing reconciliation
graph and :class:`AgentJobService`.  It does not introduce a package-specific
transport, adapter registry, or SSH escape hatch.  All identities crossing the
boundary are immutable release/deployment digests.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Protocol, cast

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    AgentOperation,
    PackageOperationRequest,
    PackageReleaseLock,
    canonical_message,
)

from cluster_profiles.workload_packages import WorkloadDeployment

from .models import AgentOperation as StoredAgentOperation
from .models import AgentOperationAttempt, Job
from .orchestration import OperationGraph, OperationNode
from .reconcile import ReconciliationPlan, resolved_reconciliation_plan

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_NODE = re.compile(r"spk_[0-9a-f]{32}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_PACKAGE_KINDS = (
    AgentOperation.PACKAGE_PREPARE.value,
    AgentOperation.PACKAGE_ACTIVATE.value,
    AgentOperation.PACKAGE_HEALTH.value,
)
_PACKAGE_MUTATIONS = frozenset(
    {
        AgentOperation.PACKAGE_PREPARE.value,
        AgentOperation.PACKAGE_ACTIVATE.value,
        AgentOperation.PACKAGE_STOP.value,
        AgentOperation.PACKAGE_ROLLBACK.value,
        AgentOperation.PACKAGE_REMOVE.value,
        AgentOperation.PACKAGE_REPAIR.value,
    }
)


class PackageRolloutError(ValueError):
    """A package desired-state or rollout input is not safe to execute."""


class PackageTrust(Protocol):
    """Minimal trust projection required by the resolver.

    Implementations may be the NAS workload-TUF delivery service or a test
    double.  A release must be checked against both its digest and the exact
    Git commit before it is accepted.
    """

    def authorize_release(
        self, release_digest: str, lock_bytes: bytes, commit: str
    ) -> bool: ...


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_message(value)).hexdigest()


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _raw_digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise PackageRolloutError("package digest is invalid")
    return value


def _package_digest(deployment: WorkloadDeployment) -> str:
    return hashlib.sha256(deployment.canonical_bytes).hexdigest()


def package_operation_payload(
    deployment: WorkloadDeployment | Mapping[str, object], operation: str
) -> dict[str, object]:
    """Build the exact protocol payload for one package lifecycle operation."""

    if not isinstance(deployment, WorkloadDeployment):
        try:
            deployment = WorkloadDeployment.load(deployment)
        except Exception as error:
            raise PackageRolloutError("workload deployment is invalid") from error
    try:
        kind = AgentOperation(operation)
    except ValueError as error:
        raise PackageRolloutError("package operation is unsupported") from error
    if kind not in {
        AgentOperation.PACKAGE_PREPARE,
        AgentOperation.PACKAGE_ACTIVATE,
        AgentOperation.PACKAGE_HEALTH,
        AgentOperation.PACKAGE_STOP,
        AgentOperation.PACKAGE_ROLLBACK,
        AgentOperation.PACKAGE_REMOVE,
        AgentOperation.PACKAGE_REPAIR,
    }:
        raise PackageRolloutError("package operation is not release-bound")
    payload = {
        "schema_version": 1,
        "deployment_id": deployment.deployment_id,
        "release_digest": _raw_digest(deployment.release_digest),
        "deployment_digest": _package_digest(deployment),
        # Carry the exact Git-authored deployment projection through the
        # fenced operation.  The GPU node validates the digest/identity again
        # before constructing a backend invocation; it must not synthesize
        # execution policy from a compiled model catalog.
        "deployment": json.loads(deployment.canonical_bytes),
        "deployment_config_digest": _package_digest(deployment),
    }
    # Parse through the shared protocol constructor so control and agent have
    # one ABI, including exact field sets and digest validation.
    PackageOperationRequest.parse(kind, payload)
    return payload


def _package_payload_for_identity(
    deployment: WorkloadDeployment,
    operation: str,
    release_digest: str,
    deployment_digest: str | None = None,
) -> dict[str, object]:
    """Build a request for a retained predecessor generation."""

    kind = AgentOperation(operation)
    payload = {
        "schema_version": 1,
        "deployment_id": deployment.deployment_id,
        "release_digest": _raw_digest(release_digest),
        "deployment_digest": _raw_digest(
            deployment_digest if deployment_digest is not None else _package_digest(deployment)
        ),
        "deployment": json.loads(deployment.canonical_bytes),
        "deployment_config_digest": _package_digest(deployment),
    }
    PackageOperationRequest.parse(kind, payload)
    return payload


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise PackageRolloutError(f"{label} must be an object")
    return cast(Mapping[str, object], value)


def _node_id(value: object) -> str:
    if not isinstance(value, str) or _NODE.fullmatch(value) is None:
        raise PackageRolloutError("package target node ID is invalid")
    return value


def _observation_value(
    observation: object, name: str, default: object = None
) -> object:
    if isinstance(observation, Mapping):
        return observation.get(name, default)
    return getattr(observation, name, default)


def _labels(observation: object) -> Mapping[str, object]:
    value = _observation_value(observation, "labels", {})
    if isinstance(value, Mapping):
        return value
    # Fleet observations historically expose labels through a nested node
    # record.  Treat absent labels as an empty set, never as an implicit match.
    record = _observation_value(observation, "node", None)
    nested = _observation_value(record, "labels", {}) if record is not None else {}
    return nested if isinstance(nested, Mapping) else {}


def _current_release(observation: object, deployment_id: str) -> tuple[str, str] | None:
    """Project an accepted package generation from flexible observation forms."""

    candidates = (
        _observation_value(observation, "current_packages", {}),
        _observation_value(observation, "packages", {}),
        _observation_value(observation, "current_workloads", {}),
    )
    for value in candidates:
        if isinstance(value, Mapping):
            raw = value.get(deployment_id)
            if isinstance(raw, Mapping):
                release = raw.get("release_digest") or raw.get("release")
                deployment = raw.get("deployment_digest")
                if isinstance(release, str) and _DIGEST.fullmatch(release):
                    return release, deployment if isinstance(
                        deployment, str
                    ) else "0" * 64
            elif isinstance(raw, str) and _DIGEST.fullmatch(raw):
                return raw, "0" * 64
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for item in value:
                if (
                    isinstance(item, Mapping)
                    and item.get("deployment_id") == deployment_id
                ):
                    release = item.get("release_digest")
                    if isinstance(release, str) and _DIGEST.fullmatch(release):
                        dep = item.get("deployment_digest")
                        return release, dep if isinstance(dep, str) else "0" * 64
                elif getattr(item, "workload_id", None) == deployment_id:
                    release = getattr(item, "release_digest", None)
                    if isinstance(release, str) and _DIGEST.fullmatch(release):
                        return release, "0" * 64
    direct = (
        _observation_value(observation, "current_release_digest"),
        _observation_value(observation, "package_release_digest"),
    )
    for release in direct:
        if isinstance(release, str) and _DIGEST.fullmatch(release):
            return release, "0" * 64
    return None


def _select_nodes(
    deployment: WorkloadDeployment,
    observations: Iterable[object],
    lock: PackageReleaseLock | None = None,
) -> tuple[object, ...]:
    selector = _mapping(deployment.selector, "deployment selector")
    count = selector.get("node_count")
    labels = _mapping(selector.get("required_labels"), "deployment labels")
    preferred = selector.get("preferred_node_ids")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise PackageRolloutError("deployment selector node_count is invalid")
    preferred_ids = (
        tuple(preferred)
        if isinstance(preferred, Sequence) and not isinstance(preferred, (str, bytes))
        else ()
    )
    candidates: list[object] = []
    resources = _mapping(deployment.resources, "deployment resources")
    minimum_memory = resources.get("memory_bytes", 0)
    minimum_storage = resources.get("storage_bytes", 0)
    minimum_gpu_memory = 0
    if lock is not None:
        envelope = _mapping(lock.resource_envelope, "release resource envelope")
        required_nodes = envelope.get("required_nodes")
        if required_nodes != count:
            raise PackageRolloutError(
                "deployment GPU node count does not match release resource envelope"
            )
        per_node = _mapping(envelope.get("per_node"), "release per-node resources")
        envelope_memory = per_node.get("host_memory_bytes")
        envelope_storage = per_node.get("installed_bytes")
        envelope_transient = per_node.get("transient_bytes")
        envelope_gpu_memory = per_node.get("gpu_memory_bytes")
        if not all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in (
                envelope_memory,
                envelope_storage,
                envelope_transient,
                envelope_gpu_memory,
            )
        ):
            raise PackageRolloutError("release resource envelope is incomplete")
        minimum_memory = max(minimum_memory, envelope_memory)
        minimum_storage = max(
            minimum_storage, envelope_storage + envelope_transient
        )
        minimum_gpu_memory = envelope_gpu_memory
    compatibility = (
        _mapping(lock.compatibility, "release compatibility") if lock is not None else {}
    )
    required_capabilities = set(compatibility.get("required_capabilities", ()))
    architectures = set(compatibility.get("architectures", ()))
    operating_systems = set(compatibility.get("operating_systems", ()))
    for observation in observations:
        _node_id(_observation_value(observation, "node_id"))
        if _observation_value(observation, "healthy", True) is False:
            continue
        if _observation_value(observation, "agent_state", "active") not in {
            "active",
            "ready",
            "online",
        }:
            continue
        available_memory = _observation_value(
            observation, "memory_available_bytes", None
        )
        available_storage = _observation_value(
            observation, "disk_available_bytes", None
        )
        available_gpu_memory = _observation_value(
            observation,
            "gpu_memory_available_bytes",
            _observation_value(observation, "gpu_memory_free_bytes", None),
        )
        if (
            isinstance(minimum_memory, int)
            and (not isinstance(available_memory, int) or available_memory < minimum_memory)
        ) or (
            isinstance(minimum_storage, int)
            and (not isinstance(available_storage, int) or available_storage < minimum_storage)
        ) or (
            isinstance(minimum_gpu_memory, int)
            and minimum_gpu_memory > 0
            and (
                not isinstance(available_gpu_memory, int)
                or available_gpu_memory < minimum_gpu_memory
            )
        ):
            continue
        observed_capabilities = set(
            _observation_value(observation, "capabilities", ()) or ()
        )
        if required_capabilities and not required_capabilities <= observed_capabilities:
            continue
        architecture = _observation_value(observation, "architecture")
        operating_system = _observation_value(observation, "operating_system")
        if architectures and architecture is not None and architecture not in architectures:
            continue
        if operating_systems and operating_system is not None and operating_system not in operating_systems:
            continue
        observed_labels = _labels(observation)
        if any(observed_labels.get(key) != value for key, value in labels.items()):
            continue
        candidates.append(observation)
    candidates.sort(
        key=lambda item: (
            0 if _observation_value(item, "node_id") in preferred_ids else 1,
            _observation_value(item, "node_id"),
        )
    )
    if len(candidates) < count:
        raise PackageRolloutError("deployment has no compatible node placement")
    return tuple(candidates[:count])


def _load_lock(
    repository: object, commit: str, deployment: WorkloadDeployment
) -> tuple[PackageReleaseLock, bytes, str]:
    path = (
        f"manifests/workload-releases/{deployment.family_id}/"
        f"{deployment.release_digest}.json"
    )
    try:
        document = repository.read_document(commit, path)
    except Exception as error:
        raise PackageRolloutError("promoted workload release is unavailable") from error
    raw = document.content
    try:
        lock = PackageReleaseLock.parse(raw)
    except Exception as error:
        raise PackageRolloutError(
            "promoted workload release lock is invalid"
        ) from error
    if (
        lock.family_id != deployment.family_id
        or lock.digest != deployment.release_digest
    ):
        raise PackageRolloutError("deployment release identity does not match lock")
    if lock.resource_envelope is None:
        raise PackageRolloutError("promoted workload release resource envelope is missing")
    # Repository JSON documents carry the normal terminal newline; the signed
    # lock identity itself is the canonical JSON bytes without that transport
    # newline.  Any other formatting variation remains fail-closed.
    if raw not in {lock.canonical_bytes, lock.canonical_bytes + b"\n"}:
        raise PackageRolloutError("workload release lock is not canonical")
    return lock, lock.canonical_bytes, document.sha256


class PackageDesiredStateResolver:
    """Resolve Git/TUF workload deployments into digest-driven graphs."""

    def __init__(
        self,
        repository: object,
        *,
        trust: PackageTrust | Callable[[str, bytes, str], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._trust = trust
        self._clock = clock or (lambda: datetime.now(UTC))

    def resolve(
        self,
        commit: str,
        deployment_ids: Sequence[str],
        observations: Iterable[object],
    ) -> ReconciliationPlan:
        if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
            raise PackageRolloutError("base Git commit is invalid")
        ids = tuple(sorted(set(deployment_ids)))
        if not ids or any(not isinstance(item, str) or not item for item in ids):
            raise PackageRolloutError("deployment IDs are invalid")
        evidence = tuple(observations)
        if not evidence:
            raise PackageRolloutError("fleet observations are required")
        plans: dict[
            str, tuple[WorkloadDeployment, PackageReleaseLock, tuple[object, ...], str]
        ] = {}
        input_digests: dict[str, str] = {}
        for deployment_id in ids:
            path = f"config/workload-deployments/{deployment_id}.toml"
            try:
                document = self._repository.read_document(commit, path)
                deployment = WorkloadDeployment.load(
                    _mapping(document.parsed, "workload deployment")
                )
            except Exception as error:
                raise PackageRolloutError(
                    "workload deployment document is invalid"
                ) from error
            if deployment.deployment_id != deployment_id:
                raise PackageRolloutError(
                    "deployment ID does not match repository path"
                )
            lock, raw_lock, lock_blob_digest = _load_lock(
                self._repository, commit, deployment
            )
            authorized = True
            if self._trust is not None:
                if callable(self._trust):
                    authorized = bool(
                        self._trust(deployment.release_digest, raw_lock, commit)
                    )
                else:
                    authorized = bool(
                        self._trust.authorize_release(
                            deployment.release_digest, raw_lock, commit
                        )
                    )
            if not authorized:
                raise PackageRolloutError("workload release is not TUF-authorized")
            selected = _select_nodes(deployment, evidence, lock)
            plans[deployment_id] = (deployment, lock, selected, lock_blob_digest)
            input_digests[path] = document.sha256
            input_digests[
                f"manifests/workload-releases/{deployment.family_id}/{deployment.release_digest}.json"
            ] = lock_blob_digest
        graph, payloads, placements, routes, releases, groups = _package_graph(
            commit, plans
        )
        target_set = {node.node_id for node in graph.nodes}
        for _, _, selected, _ in plans.values():
            target_set.update(
                _node_id(_observation_value(item, "node_id")) for item in selected
            )
        target_nodes = tuple(sorted(target_set))
        fleet_digest = _digest(
            sorted(
                {
                    "node_id": _node_id(_observation_value(item, "node_id")),
                    "healthy": bool(_observation_value(item, "healthy", True)),
                }
                for item in evidence
            )
        )
        return resolved_reconciliation_plan(
            commit=commit,
            targets=target_nodes,
            placements=placements,
            routes=routes,
            releases=releases,
            workload_groups=groups,
            input_digests=input_digests,
            operation_graph=graph,
            operation_payloads=payloads,
            # Package operations are part of the v2 agent ABI.  Keep legacy
            # workload reconciliation on the v1 range, but never enqueue a
            # package graph that a v1 GPU node can claim: AgentJobService
            # rejects package claims below protocol v2.
            agent_protocol_range=(2, 2),
            fleet_evidence_digest=fleet_digest,
        )


def _package_graph(
    commit: str,
    plans: Mapping[
        str, tuple[WorkloadDeployment, PackageReleaseLock, tuple[object, ...], str]
    ],
) -> tuple[
    OperationGraph,
    Mapping[str, Mapping[str, object]],
    Mapping[str, object],
    Mapping[str, object],
    Mapping[str, object],
    Mapping[str, object],
]:
    nodes: dict[str, OperationNode] = {}
    payloads: dict[str, Mapping[str, object]] = {}
    placements: dict[str, object] = {}
    routes: dict[str, object] = {}
    releases: dict[str, object] = {}
    groups: dict[str, object] = {}
    for deployment_id, (deployment, lock, selected, _) in sorted(plans.items()):
        node_ids = tuple(
            _node_id(_observation_value(item, "node_id")) for item in selected
        )
        placements[deployment_id] = list(node_ids)
        release_payload = {
            "family_id": deployment.family_id,
            "release_digest": deployment.release_digest,
            "deployment_digest": _package_digest(deployment),
            "lock_digest": lock.digest,
            "resource_envelope": lock.resource_envelope,
            "rollback_payloads": {},
        }
        releases[deployment_id] = release_payload
        groups[deployment_id] = {
            "nodes": list(node_ids),
            "release_digest": deployment.release_digest,
            "deployment_digest": _package_digest(deployment),
        }
        routes[cast(str, deployment.routing["alias"])] = {
            "deployment_id": deployment_id,
            "entrypoint_node_id": node_ids[0],
            "port": deployment.ports[deployment.routing["port"]],
        }
        for node_id in node_ids:
            previous = _current_release(
                dict(selected)[node_id]
                if False
                else next(
                    item
                    for item in selected
                    if _observation_value(item, "node_id") == node_id
                ),
                deployment_id,
            )
            previous_release = previous[0] if previous else None
            if previous_release is not None and previous_release != deployment.release_digest:
                rollback_payload = _package_payload_for_identity(
                    deployment,
                    AgentOperation.PACKAGE_ROLLBACK.value,
                    previous_release,
                    previous[1],
                )
                cast(dict[str, object], release_payload["rollback_payloads"])[node_id] = rollback_payload
                release_payload.setdefault("previous_release_digest", previous_release)
                release_payload.setdefault("previous_deployment_digest", previous[1])
            kinds = list(_PACKAGE_KINDS)
            if previous_release == deployment.release_digest:
                kinds = [AgentOperation.PACKAGE_HEALTH.value]
            elif previous_release is not None:
                kinds.insert(0, AgentOperation.PACKAGE_STOP.value)
            operation_ids: dict[str, str] = {}
            for kind in kinds:
                operation_id = f"{deployment_id}:{node_id}:{kind}"
                operation_ids[kind] = operation_id
                payload = (
                    _package_payload_for_identity(
                        deployment,
                        kind,
                        previous_release,
                        previous[1],
                    )
                    if kind == AgentOperation.PACKAGE_STOP.value and previous_release is not None
                    else package_operation_payload(deployment, kind)
                )
                payloads[operation_id] = MappingProxyType(payload)
                if kind == AgentOperation.PACKAGE_STOP.value:
                    # Never withdraw the currently serving generation merely
                    # because a replacement download is planned.  The stop
                    # transition is explicitly fenced behind prepare so a
                    # fetch, capacity, trust, or materialization failure
                    # leaves the old process and route untouched even if a
                    # scheduler changes operation ordering.
                    dependencies = (
                        f"{deployment_id}:{node_id}:{AgentOperation.PACKAGE_PREPARE.value}",
                    )
                    compensation = None
                elif kind == AgentOperation.PACKAGE_PREPARE.value:
                    dependencies = ()
                    compensation = None
                elif kind == AgentOperation.PACKAGE_ACTIVATE.value:
                    dependencies = (
                        operation_ids[AgentOperation.PACKAGE_PREPARE.value],
                    )
                    if AgentOperation.PACKAGE_STOP.value in operation_ids:
                        dependencies += (operation_ids[AgentOperation.PACKAGE_STOP.value],)
                        dependencies = tuple(sorted(dependencies))
                    compensation = AgentOperation.PACKAGE_ROLLBACK.value
                else:
                    dependencies = (
                        operation_ids.get(AgentOperation.PACKAGE_ACTIVATE.value)
                        or operation_ids.get(AgentOperation.PACKAGE_PREPARE.value)
                        or "",
                    )
                    dependencies = tuple(item for item in dependencies if item)
                    compensation = None
                nodes[operation_id] = OperationNode(
                    operation_id=operation_id,
                    node_id=node_id,
                    workload_id=deployment_id,
                    kind=kind,
                    dependencies=dependencies,
                    compensation_kind=compensation,
                    payload_digest=_digest(payload),
                )
    ordered = _topological(nodes)
    graph_document = {
        "schema_version": 1,
        "base_commit": commit,
        "targets": sorted({node.node_id for node in ordered}),
        "nodes": [node.to_document() for node in ordered],
    }
    graph_digest = hashlib.sha256(
        json.dumps(graph_document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return (
        OperationGraph(
            f"pending:{graph_digest}",
            commit,
            tuple(graph_document["targets"]),
            ordered,
            graph_digest,
        ),
        MappingProxyType(dict(sorted(payloads.items()))),
        MappingProxyType(placements),
        MappingProxyType(routes),
        MappingProxyType(releases),
        MappingProxyType(groups),
    )


def _topological(nodes: Mapping[str, OperationNode]) -> tuple[OperationNode, ...]:
    unresolved = {key: set(value.dependencies) for key, value in nodes.items()}
    ordered: list[OperationNode] = []
    while unresolved:
        ready = sorted(key for key, deps in unresolved.items() if not deps)
        if not ready:
            raise PackageRolloutError("package operation graph contains a cycle")
        for key in ready:
            ordered.append(nodes[key])
            del unresolved[key]
        for deps in unresolved.values():
            deps.difference_update(ready)
    return tuple(ordered)


class PackageRolloutOrchestrator:
    """Persist and advance package rollout batches through AgentJobService.

    The implementation intentionally keeps route publication and leases behind
    injected boundaries.  In production these are the existing node-lease and
    route services; tests can provide deterministic doubles without opening an
    alternate execution path.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        agent_jobs: Any,
        *,
        clock: Callable[[], datetime] | None = None,
        leases: Any | None = None,
        routes: Any | None = None,
    ) -> None:
        self._sessions = sessions
        self._agent_jobs = agent_jobs
        self._clock = clock or (lambda: datetime.now(UTC))
        self._leases = leases
        self._routes = routes

    def create(
        self,
        plan: ReconciliationPlan,
        deployment_id: str | None = None,
        *,
        actor: str,
        request_id: str,
    ) -> str:
        """Persist a package rollout and its canary/stable node projections."""

        from .models import PackageRollout, PackageRolloutNode

        if not isinstance(plan, ReconciliationPlan):
            raise PackageRolloutError("package rollout plan is invalid")
        if deployment_id is None and len(plan.releases) == 1:
            deployment_id = next(iter(plan.releases))
        if not isinstance(deployment_id, str) or not deployment_id:
            raise PackageRolloutError("package deployment ID is invalid")
        graph = plan.operation_graph
        if graph is None:
            raise PackageRolloutError("package rollout plan has no operation graph")
        release = _mapping(plan.releases.get(deployment_id), "package release")
        release_digest = _raw_digest(release.get("release_digest"))
        deployment_digest = _raw_digest(release.get("deployment_digest"))
        lock_digest = _raw_digest(release.get("lock_digest", release_digest))
        package_nodes = tuple(
            node
            for node in graph.nodes
            if node.workload_id == deployment_id and node.kind.startswith("package.")
        )
        if not package_nodes:
            raise PackageRolloutError("package rollout has no target operations")
        now = self._clock()
        plan_document = {
            "schema_version": 1,
            "deployment_id": deployment_id,
            "operation_graph": _jsonable(graph.document),
            "operation_payloads": _jsonable(plan.operation_payloads),
            "release": _jsonable(release),
            "targets": list(plan.targets),
        }
        plan_digest = _digest(plan_document)
        fleet_digest = plan.fleet_evidence_digest or _digest(plan.targets)
        topology_digest = _digest(plan.placements)
        policy_digest = _digest(
            {"deployment_id": deployment_id, "release_digest": release_digest}
        )
        # A Job is the existing fenced queue parent.  PackageRollout has no
        # separate transport/job foreign key by design; the rollout ID is
        # carried in its bounded payload for restart-safe lookup.
        job_id = str(uuid.uuid4())
        rollout_id = str(uuid.uuid4())
        job = Job(
            id=job_id,
            request_id=request_id,
            kind="package.rollout",
            state="running",
            actor=actor,
            base_commit=plan.commit,
            targets=list(plan.targets),
            payload_digest=_digest(
                {"rollout_id": rollout_id, "plan_digest": plan_digest}
            ),
            payload={"rollout_id": rollout_id, "plan_digest": plan_digest},
            created_at=now,
            updated_at=now,
        )
        rollout = PackageRollout(
            id=rollout_id,
            job_id=job_id,
            deployment_id=deployment_id,
            deployment_digest=deployment_digest,
            release_digest=release_digest,
            previous_release_digest=(
                release.get("previous_release_digest")
                if isinstance(release.get("previous_release_digest"), str)
                else None
            ),
            base_commit=plan.commit,
            policy_digest=policy_digest,
            tuf_target_digest=lock_digest,
            fleet_digest=fleet_digest,
            topology_digest=topology_digest,
            plan_digest=plan_digest,
            state="planned",
            actor=actor,
            plan=cast(dict[str, object], _jsonable(plan_document)),
            progress={
                "accepted": 0,
                "total": len({node.node_id for node in package_nodes}),
            },
            current_batch=0,
            created_at=now,
            updated_at=now,
        )
        with self._sessions.begin() as session:
            existing = session.scalar(
                select(PackageRollout).where(PackageRollout.plan_digest == plan_digest)
            )
            if existing is not None:
                return existing.id
            session.add_all([job, rollout])
            for order, node_id in enumerate(
                sorted({node.node_id for node in package_nodes})
            ):
                # One projection per GPU node advances through prepare/activate/
                # health; operation_history records each exact graph node.
                first = next(node for node in package_nodes if node.node_id == node_id)
                payload = plan.operation_payloads[first.operation_id]
                session.add(
                    PackageRolloutNode(
                        rollout_id=rollout_id,
                        node_id=node_id,
                        batch_index=0 if order == 0 else 1,
                        node_order=order,
                        is_canary=order == 0,
                        state="pending",
                        operation_kind=first.kind,
                        graph_operation_id=first.operation_id,
                        operation_key=first.operation_id,
                        operation_id=None,
                        rollback_operation_id=None,
                        operation_history=[],
                        expected_payload_digest=_digest(payload),
                        created_at=now,
                        updated_at=now,
                    )
                )
            session.flush()
        return rollout_id

    def advance(self, rollout_id: str) -> str:
        """Advance one persisted package rollout and enqueue ready operations."""

        from .models import PackageRollout, PackageRolloutNode

        with self._sessions.begin() as session:
            rollout = session.get(PackageRollout, rollout_id)
            if rollout is None:
                raise KeyError(rollout_id)
            if rollout.state in {"completed", "failed", "cancelled", "partial"}:
                return rollout.state
            nodes = tuple(
                session.scalars(
                    select(PackageRolloutNode)
                    .where(PackageRolloutNode.rollout_id == rollout.id)
                    .order_by(
                        PackageRolloutNode.batch_index, PackageRolloutNode.node_order
                    )
                    .with_for_update(of=PackageRolloutNode)
                )
            )
            if not nodes:
                rollout.state = "failed"
                rollout.failure_reason = "package rollout has no target nodes"
                rollout.updated_at = self._clock()
                return rollout.state
            job = self._job_in_session(session, rollout)
            now = self._clock()
            current_batch = int(getattr(rollout, "current_batch", 0))
            batch = tuple(node for node in nodes if node.batch_index == current_batch)
            if not batch:
                rollout.state = "completed"
                rollout.completed_at = now
                rollout.updated_at = now
                job.state = "succeeded"
                job.updated_at = now
                return rollout.state
            queued = False
            for node in batch:
                history = list(node.operation_history or [])
                active = self._active_operation(session, node)
                if active is not None:
                    if active.state in {"queued", "running"}:
                        continue
                    if active.state == "succeeded":
                        active_kind = next(
                            (
                                item.get("kind")
                                for item in history
                                if item.get("operation_id") == active.id
                            ),
                            None,
                        )
                        self._accept_operation(session, node, history, active, now)
                        if active_kind == AgentOperation.PACKAGE_ROLLBACK.value:
                            node.state = "rolled-back"
                            node.completed_at = now
                            node.updated_at = now
                            rollout.state = "rolled-back"
                            rollout.rollback_evidence_digest = node.evidence_digest
                            job.state = "failed"
                            job.status_reason = "package rollout rolled back"
                            job.updated_at = now
                            continue
                    elif active.state in {"failed", "waiting-for-operator", "expired"}:
                        active_kind = next(
                            (
                                item.get("kind")
                                for item in history
                                if item.get("operation_id") == active.id
                            ),
                            None,
                        )
                        release = (
                            rollout.plan.get("release")
                            if isinstance(rollout.plan, Mapping)
                            else None
                        )
                        rollback_payloads = (
                            release.get("rollback_payloads")
                            if isinstance(release, Mapping)
                            else None
                        )
                        rollback_payload = (
                            rollback_payloads.get(node.node_id)
                            if isinstance(rollback_payloads, Mapping)
                            else None
                        )
                        if active_kind in {
                            AgentOperation.PACKAGE_ACTIVATE.value,
                            AgentOperation.PACKAGE_HEALTH.value,
                        } and isinstance(rollback_payload, Mapping):
                            rollback_id = str(uuid.uuid4())
                            stored = self._agent_jobs.enqueue_in_session(
                                session,
                                job.id,
                                node.node_id,
                                AgentOperation.PACKAGE_ROLLBACK.value,
                                rollout.base_commit,
                                rollback_payload,
                                operation_id=rollback_id,
                            )
                            node.operation_id = stored.id
                            node.operation_kind = AgentOperation.PACKAGE_ROLLBACK.value
                            node.rollback_operation_id = stored.id
                            node.expected_payload_digest = _digest(rollback_payload)
                            node.operation_history = history + [
                                {
                                    "kind": AgentOperation.PACKAGE_ROLLBACK.value,
                                    "payload_digest": _digest(rollback_payload),
                                    "operation_id": stored.id,
                                    "state": "queued",
                                }
                            ]
                            node.state = "rolling-back"
                            rollout.state = "rolling-back"
                            node.updated_at = now
                            queued = True
                            continue
                        node.state = "failed"
                        node.failure_reason = "package operation did not complete"
                        node.failure_evidence_digest = _digest(
                            {"operation_id": active.id, "state": active.state}
                        )
                        node.updated_at = now
                        rollout.state = "waiting-for-operator"
                        rollout.failure_reason = node.failure_reason
                        rollout.failure_evidence_digest = node.failure_evidence_digest
                        job.state = "waiting-for-operator"
                        job.status_reason = rollout.failure_reason
                        job.updated_at = now
                        continue
                if node.state in {"accepted", "cancelled", "failed", "rolled-back"}:
                    continue
                if node.state == "offline-pending":
                    continue
                history = list(node.operation_history or [])
                next_item = self._next_operation(rollout, node, history)
                if next_item is None:
                    node.state = "accepted"
                    node.completed_at = now
                    node.updated_at = now
                    continue
                graph_operation_id, kind, payload = next_item
                operation_id = str(uuid.uuid4())
                stored = self._agent_jobs.enqueue_in_session(
                    session,
                    job.id,
                    node.node_id,
                    kind,
                    rollout.base_commit,
                    payload,
                    operation_id=operation_id,
                )
                node.operation_id = stored.id
                node.operation_kind = kind
                node.graph_operation_id = graph_operation_id
                node.operation_key = graph_operation_id
                node.expected_payload_digest = _digest(payload)
                node.operation_history = history + [
                    {
                        "graph_operation_id": graph_operation_id,
                        "kind": kind,
                        "payload_digest": _digest(payload),
                        "operation_id": stored.id,
                        "state": "queued",
                    }
                ]
                node.state = {
                    AgentOperation.PACKAGE_PREPARE.value: "preparing",
                    AgentOperation.PACKAGE_ACTIVATE.value: "activating",
                    AgentOperation.PACKAGE_HEALTH.value: "health-checking",
                }.get(kind, "preparing")
                node.updated_at = now
                queued = True
            if queued:
                if rollout.state != "rolling-back":
                    rollout.state = "running"
                rollout.updated_at = now
                self._agent_jobs.notify_available()
                return rollout.state
            if all(node.state == "accepted" for node in batch):
                if any(node.batch_index > current_batch for node in nodes):
                    rollout.current_batch = current_batch + 1
                    rollout.state = "planned"
                else:
                    rollout.state = "completed"
                    rollout.completed_at = now
                    job.state = "succeeded"
                rollout.updated_at = now
            return rollout.state

    @staticmethod
    def _job_in_session(session: Session, rollout: object) -> Job:
        rollout_id = rollout.id
        linked = getattr(rollout, "job_id", None)
        if isinstance(linked, str):
            job = session.get(Job, linked)
            if job is not None:
                return job
        jobs = tuple(session.scalars(select(Job).where(Job.kind == "package.rollout")))
        for job in jobs:
            if (
                isinstance(job.payload, Mapping)
                and job.payload.get("rollout_id") == rollout_id
            ):
                return job
        now = datetime.now(UTC)
        base_commit = cast(str, rollout.base_commit)
        job = Job(
            id=str(uuid.uuid4()),
            request_id=str(uuid.uuid4()),
            kind="package.rollout",
            state="running",
            actor=cast(str, getattr(rollout, "actor", "package-orchestrator")),
            base_commit=base_commit,
            targets=[],
            payload_digest=_digest({"rollout_id": rollout_id}),
            payload={"rollout_id": rollout_id},
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        session.flush()
        return job

    @staticmethod
    def _active_operation(
        session: Session, node: object
    ) -> StoredAgentOperation | None:
        operation_id = getattr(node, "operation_id", None)
        if not isinstance(operation_id, str):
            return None
        return session.get(StoredAgentOperation, operation_id)

    @staticmethod
    def _accept_operation(
        session: Session,
        node: object,
        history: list[dict[str, object]],
        operation: StoredAgentOperation,
        now: datetime,
    ) -> None:
        for item in history:
            if item.get("operation_id") == operation.id:
                item["state"] = "accepted"
        node.operation_history = history
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == operation.current_attempt,
            )
        )
        result = attempt.result if attempt is not None else None
        node.evidence_digest = _digest(
            result
            if isinstance(result, Mapping)
            else {"operation_id": operation.id, "state": operation.state}
        )
        node.observed_release_digest = operation.payload.get("release_digest")
        node.updated_at = now

    @staticmethod
    def _next_operation(
        rollout: object,
        node: object,
        history: Sequence[Mapping[str, object]],
    ) -> tuple[str, str, Mapping[str, object]] | None:
        plan = getattr(rollout, "plan", None)
        if not isinstance(plan, Mapping):
            raise PackageRolloutError("package rollout plan is unavailable")
        graph = plan.get("operation_graph")
        payloads = plan.get("operation_payloads")
        if not isinstance(graph, Mapping) or not isinstance(payloads, Mapping):
            raise PackageRolloutError("package rollout graph/payloads are unavailable")
        completed = {
            cast(str, item.get("graph_operation_id"))
            for item in history
            if item.get("state") == "accepted"
        }
        raw_nodes = graph.get("nodes")
        if not isinstance(raw_nodes, list):
            raise PackageRolloutError("package rollout graph nodes are invalid")
        for raw in raw_nodes:
            if not isinstance(raw, Mapping) or raw.get("node_id") != node.node_id:
                continue
            operation_id = raw.get("operation_id")
            dependencies = raw.get("dependencies")
            kind = raw.get("kind")
            if (
                not isinstance(operation_id, str)
                or not isinstance(kind, str)
                or not isinstance(dependencies, list)
                or operation_id in completed
                or any(dependency not in completed for dependency in dependencies)
            ):
                continue
            payload = payloads.get(operation_id)
            if isinstance(payload, Mapping):
                return operation_id, kind, payload
        return None

    @staticmethod
    def _payload_for_node(rollout: object, node: object) -> Mapping[str, object]:
        plan = getattr(rollout, "plan", None)
        if not isinstance(plan, Mapping):
            raise PackageRolloutError("package rollout plan is unavailable")
        payloads = plan.get("operation_payloads")
        if not isinstance(payloads, Mapping):
            payloads = plan.get("payloads")
        if not isinstance(payloads, Mapping):
            raise PackageRolloutError(
                "package rollout operation payloads are unavailable"
            )
        operation_id = getattr(node, "graph_operation_id", None) or getattr(
            node, "operation_key", None
        )
        payload = payloads.get(operation_id)
        if not isinstance(payload, Mapping):
            payload = payloads.get(getattr(node, "operation_id", None))
        if not isinstance(payload, Mapping):
            raise PackageRolloutError("package rollout node payload is unavailable")
        try:
            PackageOperationRequest.parse(AgentOperation(node.operation_kind), payload)
        except Exception as error:
            raise PackageRolloutError(
                "package rollout node payload is invalid"
            ) from error
        return payload


__all__ = [
    "PackageDesiredStateResolver",
    "PackageRolloutError",
    "PackageRolloutOrchestrator",
    "PackageTrust",
    "package_operation_payload",
]
