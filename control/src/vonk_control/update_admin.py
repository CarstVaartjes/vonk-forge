"""Thin, content-addressed administration over GPU node platform updates."""

from __future__ import annotations

import hashlib
import re
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import canonical_message

from .models import (
    AgentNode,
    AuditEvent,
    Reconciliation,
    RoutePublication,
    RoutePublicationOwner,
    UpdateRollout,
    UpdateRolloutNode,
)
from .update_grants import AdminActionGrantIssuer
from .updates import (
    AgentObservation,
    DistributedWorkload,
    PlatformAgentArtifact,
    RolloutPolicy,
    TargetPlatform,
    TopologyExclusion,
    UpdatePlan,
    UpdatePlanner,
    VersionSkewAnalyzer,
    WorkloadReplicaObservation,
    durable_recipe_workloads,
)

_MAX_PLANNED_UPDATES = 256
_ROUTE_ID = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_VONK_ID = re.compile(r"spk_[0-9a-f]{32}\Z")


@dataclass(frozen=True)
class RouteImpact:
    """One accepted route alias and the GPU node workload membership it exposes."""

    alias: str
    workload_id: str
    nodes: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.alias, str)
            or _ROUTE_ID.fullmatch(self.alias) is None
            or not isinstance(self.workload_id, str)
            or _ROUTE_ID.fullmatch(self.workload_id) is None
            or not isinstance(self.nodes, tuple)
            or not self.nodes
            or len(set(self.nodes)) != len(self.nodes)
            or any(_VONK_ID.fullmatch(node_id) is None for node_id in self.nodes)
        ):
            raise ValueError("platform update route impact is invalid")


class UpdateAdminOrchestrator(Protocol):
    def create(
        self,
        plan: UpdatePlan,
        actor: str,
        request_id: str,
        *,
        admin_grant_factory: Callable[..., dict[str, object]],
    ) -> str: ...


class UpdateAdminGrantRefresher(Protocol):
    def refresh_update_grant(
        self,
        rollout_id: str,
        batch_index: int,
        node_ids: tuple[str, ...],
        *,
        actor: str,
        request_id: str,
    ) -> dict[str, object]: ...

    def authorize_rollback(
        self,
        rollout_id: str,
        actor: str,
        request_id: str,
        *,
        admin_grant_factory: Callable[..., dict[str, object]],
    ) -> str: ...

    def approve_resume(
        self,
        rollout_id: str,
        actor: str,
        request_id: str,
        reason: str,
    ) -> str: ...


class PlatformUpdateAdminService:
    """Plan against fresh evidence and dispatch only an exact confirmed digest."""

    def __init__(
        self,
        *,
        target_source: Callable[[], TargetPlatform],
        observation_source: Callable[[], tuple[AgentObservation, ...]],
        workload_source: Callable[[], tuple[DistributedWorkload, ...]],
        orchestrator: UpdateAdminOrchestrator,
        grant_issuer: AdminActionGrantIssuer,
        status_source: Callable[[str], Mapping[str, object]],
        clock: Callable[[], datetime],
        grant_refresher: UpdateAdminGrantRefresher | None = None,
        topology_source: Callable[[], tuple[TopologyExclusion, ...]] | None = None,
        route_source: Callable[[], tuple[RouteImpact, ...]] | None = None,
    ) -> None:
        for dependency in (
            target_source,
            observation_source,
            workload_source,
            status_source,
            clock,
        ):
            if not callable(dependency):
                raise TypeError("platform update administration dependency is invalid")
        if not isinstance(grant_issuer, AdminActionGrantIssuer):
            raise TypeError("platform update admin grant issuer is invalid")
        for method in ("create", "authorize_rollback", "approve_resume"):
            if not callable(getattr(orchestrator, method, None)):
                raise TypeError("platform update orchestrator is invalid")
        self._target_source = target_source
        self._observation_source = observation_source
        self._workload_source = workload_source
        self._orchestrator = orchestrator
        self._grant_issuer = grant_issuer
        self._status_source = status_source
        self._clock = clock
        self._topology_source = topology_source or (lambda: ())
        self._route_source = route_source or (lambda: ())
        if grant_refresher is not None and not callable(
            getattr(grant_refresher, "refresh_update_grant", None)
        ):
            raise TypeError("platform update grant refresher is invalid")
        self._grant_refresher = grant_refresher
        self._plans: OrderedDict[str, UpdatePlan] = OrderedDict()
        self._plan_routes: OrderedDict[str, tuple[RouteImpact, ...]] = OrderedDict()
        self._plans_lock = threading.RLock()

    def refresh_update_grant(
        self,
        rollout_id: str,
        batch_index: int,
        node_ids: tuple[str, ...],
        *,
        actor: str,
        request_id: str,
    ) -> dict[str, object]:
        """Issue the exact next-batch grant through API-only key custody."""

        if self._grant_refresher is None:
            raise RuntimeError("platform update grant refresher is unavailable")
        return self._grant_refresher.refresh_update_grant(
            rollout_id,
            batch_index,
            node_ids,
            actor=actor,
            request_id=request_id,
        )

    def skew(self) -> dict[str, object]:
        target, observations, workloads = self._inputs()
        routes = self._routes()
        report = VersionSkewAnalyzer().compare(target, observations)
        by_node = {item.node_id: item for item in observations}
        workloads_by_node = {
            node_id: sorted(
                workload.workload_id
                for workload in workloads
                if node_id in workload.members
            )
            for node_id in by_node
        }
        routes_by_node = {
            node_id: sorted(route.alias for route in routes if node_id in route.nodes)
            for node_id in by_node
        }
        content: dict[str, object] = {
            "affected_nodes": list(report.affected_nodes),
            "incompatible_nodes": list(report.incompatible_nodes),
            "nodes": [
                {
                    "active_routes": routes_by_node[node.node_id],
                    "active_slot": by_node[node.node_id].active_slot,
                    "active_workloads": workloads_by_node[node.node_id],
                    "build_digest": by_node[node.node_id].build_digest,
                    "compatible": node.compatible,
                    "display_name": node.node_id,
                    "node_id": node.node_id,
                    "platform_version": by_node[node.node_id].platform_version,
                    "protocol_version": by_node[node.node_id].protocol_version,
                    "reasons": list(node.reasons),
                    "rollback_slot": _rollback_slot(by_node[node.node_id].active_slot),
                    "status": node.status,
                    "update_required": node.update_required,
                }
                for node in report.nodes
            ],
            "offline_pending": [
                node.node_id
                for node in report.nodes
                if node.status == "offline-pending"
            ],
            "prompt_required": report.prompt_required,
            "target": _target_document(target),
        }
        content["digest"] = _digest(content)
        return content

    def plan(
        self,
        *,
        release: str,
    ) -> dict[str, object]:
        target, observations, workloads = self._inputs()
        routes = self._routes()
        if release != _release_reference(target):
            raise ValueError("platform update release is unavailable")
        policy = RolloutPolicy(
            batch_size=1,
            soak_seconds=300,
            preferred_canary=None,
        )
        plan = UpdatePlanner().plan(
            target,
            observations,
            workloads,
            policy,
            topology=self._topology(),
        )
        with self._plans_lock:
            self._plans[plan.plan_digest] = plan
            self._plan_routes[plan.plan_digest] = routes
            self._plans.move_to_end(plan.plan_digest)
            self._plan_routes.move_to_end(plan.plan_digest)
            while len(self._plans) > _MAX_PLANNED_UPDATES:
                expired, _plan = self._plans.popitem(last=False)
                self._plan_routes.pop(expired, None)
        return _plan_document(plan, routes)

    def apply(
        self,
        plan_digest: str,
        actor: str,
        request_id: str,
    ) -> dict[str, object]:
        with self._plans_lock:
            planned = self._plans.get(plan_digest)
            planned_routes = self._plan_routes.get(plan_digest)
        target, observations, workloads = self._inputs()
        if planned is None or planned_routes is None:
            raise KeyError(plan_digest)
        if self._routes() != planned_routes:
            raise ValueError("platform update route impact evidence is stale")
        current = UpdatePlanner().plan(
            target,
            observations,
            workloads,
            planned.policy,
            topology=self._topology(),
        )
        if current.plan_digest != planned.plan_digest:
            raise ValueError("platform update plan digest is stale")

        def update_grant(**bindings: object) -> dict[str, object]:
            return self._issue_grant("agent.update", **bindings)

        rollout_id = self._orchestrator.create(
            current,
            actor,
            request_id,
            admin_grant_factory=update_grant,
        )
        projection = self.status(rollout_id)
        if projection.get("plan_digest") != current.plan_digest:
            raise RuntimeError("persisted update rollout plan digest disagrees")
        return projection

    def status(self, rollout_id: str) -> dict[str, object]:
        return dict(self._status_source(rollout_id))

    def approve_resume(
        self,
        rollout_id: str,
        actor: str,
        request_id: str,
        reason: str,
    ) -> dict[str, object]:
        current = self.status(rollout_id)
        state = current.get("state")
        if state == "paused":

            def rollback_grant(**bindings: object) -> dict[str, object]:
                return self._issue_grant("agent.rollback", **bindings)

            state = self._orchestrator.authorize_rollback(
                rollout_id,
                actor,
                request_id,
                admin_grant_factory=rollback_grant,
            )
        elif state == "waiting-for-approval":
            state = self._orchestrator.approve_resume(
                rollout_id,
                actor,
                request_id,
                reason,
            )
        else:
            raise ValueError("update rollout is not waiting for recovery approval")
        result = dict(current)
        result["state"] = state
        result["failure_reason"] = (
            None if state == "planned" else result.get("failure_reason")
        )
        result["resume_required"] = state == "waiting-for-approval"
        result["can_approve_resume"] = state == "waiting-for-approval"
        result["required_action"] = (
            "approve-resume" if state == "waiting-for-approval" else None
        )
        return result

    def _inputs(
        self,
    ) -> tuple[
        TargetPlatform,
        tuple[AgentObservation, ...],
        tuple[DistributedWorkload, ...],
    ]:
        target = self._target_source()
        observations = self._observation_source()
        workloads = self._workload_source()
        if (
            not isinstance(target, TargetPlatform)
            or not isinstance(observations, tuple)
            or any(not isinstance(item, AgentObservation) for item in observations)
            or not isinstance(workloads, tuple)
            or any(not isinstance(item, DistributedWorkload) for item in workloads)
        ):
            raise TypeError("platform update evidence is invalid")
        return target, observations, workloads

    def _topology(self) -> tuple[TopologyExclusion, ...]:
        topology = self._topology_source()
        if not isinstance(topology, tuple) or any(
            not isinstance(item, TopologyExclusion) for item in topology
        ):
            raise TypeError("platform update topology evidence is invalid")
        return topology

    def _routes(self) -> tuple[RouteImpact, ...]:
        routes = self._route_source()
        if (
            not isinstance(routes, tuple)
            or any(not isinstance(item, RouteImpact) for item in routes)
            or tuple(sorted(routes, key=lambda item: item.alias)) != routes
            or len({item.alias for item in routes}) != len(routes)
        ):
            raise TypeError("platform update route impact evidence is invalid")
        return routes

    def _issue_grant(
        self,
        action: str,
        *,
        rollout_id: object,
        parent_job_id: object,
        node_ids: object,
        target_release_digest: object,
    ) -> dict[str, object]:
        now = self._clock()
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise RuntimeError("platform update grant clock is invalid")
        return self._grant_issuer.issue(
            action=action,
            rollout_id=rollout_id,
            parent_job_id=parent_job_id,
            node_ids=node_ids,
            target_release_digest=target_release_digest,
            expires_at=int(now.astimezone(UTC).timestamp()) + 3600,
        )


class DurableUpdateGrantRefresher:
    """Refresh one exact pre-dispatch batch grant under the rollout row lock."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        issuer: AdminActionGrantIssuer,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(issuer, AdminActionGrantIssuer) or not callable(clock):
            raise TypeError("update grant refresher dependency is invalid")
        self._sessions = sessions
        self._issuer = issuer
        self._clock = clock
        public = issuer.public_key_document()
        try:
            raw_public = bytes.fromhex(str(public["public_key"]))
            self._public_key = ed25519.Ed25519PublicKey.from_public_bytes(raw_public)
        except (KeyError, TypeError, ValueError) as error:
            raise TypeError("update grant issuer public key is invalid") from error
        self._key_id = issuer.key_id

    def refresh_update_grant(
        self,
        rollout_id: str,
        batch_index: int,
        node_ids: tuple[str, ...],
        *,
        actor: str,
        request_id: str,
    ) -> dict[str, object]:
        _uuid4(rollout_id, "update rollout ID")
        _uuid4(request_id, "update grant request ID")
        if (
            isinstance(batch_index, bool)
            or not isinstance(batch_index, int)
            or batch_index < 0
            or not isinstance(node_ids, tuple)
            or not node_ids
            or tuple(sorted(node_ids)) != node_ids
            or len(set(node_ids)) != len(node_ids)
            or any(
                not isinstance(node_id, str)
                or not node_id.startswith("spk_")
                or len(node_id) != 36
                for node_id in node_ids
            )
            or not isinstance(actor, str)
            or not actor.strip()
            or len(actor) > 200
        ):
            raise ValueError("update grant refresh input is invalid")
        now = _aware(self._clock())
        now_epoch = int(now.astimezone(UTC).timestamp())
        with self._sessions.begin() as session:
            rollout = session.scalar(
                select(UpdateRollout)
                .where(UpdateRollout.id == rollout_id)
                .with_for_update(of=UpdateRollout)
            )
            if rollout is None:
                raise KeyError(rollout_id)
            if (
                rollout.state != "planned"
                or rollout.current_batch != batch_index
                or rollout.job_id is None
            ):
                raise ValueError("update rollout is not awaiting this batch")
            nodes = tuple(
                session.scalars(
                    select(UpdateRolloutNode)
                    .where(
                        UpdateRolloutNode.rollout_id == rollout.id,
                        UpdateRolloutNode.batch_index == batch_index,
                    )
                    .order_by(UpdateRolloutNode.node_order)
                    .with_for_update(of=UpdateRolloutNode)
                )
            )
            expected_nodes = tuple(node.node_id for node in nodes)
            if expected_nodes != node_ids or any(
                node.state != "pending" for node in nodes
            ):
                raise ValueError("update rollout batch nodes are stale")
            existing = rollout.update_admin_grant
            if self._reusable(
                existing,
                rollout_id=rollout.id,
                parent_job_id=rollout.job_id,
                node_ids=node_ids,
                target_release_digest="sha256:" + rollout.release_digest,
                now_epoch=now_epoch,
            ):
                assert isinstance(existing, dict)
                return dict(existing)
            grant = self._issuer.issue(
                action="agent.update",
                rollout_id=rollout.id,
                parent_job_id=rollout.job_id,
                node_ids=node_ids,
                target_release_digest="sha256:" + rollout.release_digest,
                expires_at=now_epoch + 3600,
            )
            rollout.update_admin_grant = dict(grant)
            rollout.updated_at = now
            session.add(
                AuditEvent(
                    request_id=request_id,
                    actor=actor,
                    action="platform.update.grant-refresh",
                    base_commit=rollout.base_commit,
                    targets=list(node_ids),
                    occurred_at=now,
                )
            )
            return grant

    def _reusable(
        self,
        envelope: object,
        *,
        rollout_id: str,
        parent_job_id: str,
        node_ids: tuple[str, ...],
        target_release_digest: str,
        now_epoch: int,
    ) -> bool:
        if not isinstance(envelope, dict) or set(envelope) != {
            "claims",
            "signature",
        }:
            return False
        claims = envelope.get("claims")
        signature = envelope.get("signature")
        if not isinstance(claims, dict) or not isinstance(signature, dict):
            return False
        expiry = claims.get("expires_at")
        if (
            set(claims)
            != {
                "action",
                "expires_at",
                "nonce",
                "node_ids",
                "parent_job_id",
                "rollout_id",
                "schema_version",
                "target_release_digest",
            }
            or claims.get("action") != "agent.update"
            or claims.get("schema_version") != 1
            or claims.get("rollout_id") != rollout_id
            or claims.get("parent_job_id") != parent_job_id
            or claims.get("node_ids") != list(node_ids)
            or claims.get("target_release_digest") != target_release_digest
            or isinstance(expiry, bool)
            or not isinstance(expiry, int)
            or expiry < now_epoch + 600
            or set(signature) != {"algorithm", "key_id", "value"}
            or signature.get("algorithm") != "ed25519"
            or signature.get("key_id") != self._key_id
            or not isinstance(signature.get("value"), str)
        ):
            return False
        try:
            from .update_grants import _canonical

            self._public_key.verify(
                bytes.fromhex(signature["value"]),
                _canonical(claims),
            )
        except (InvalidSignature, TypeError, ValueError):
            return False
        return True


def durable_update_status(
    sessions: sessionmaker[Session],
    identifier: str,
) -> dict[str, object]:
    """Project one rollout by rollout UUID or its durable platform-update job UUID."""
    try:
        parsed = uuid.UUID(identifier)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("update rollout ID is invalid") from None
    if str(parsed) != identifier:
        raise ValueError("update rollout ID is invalid")
    with sessions() as session:
        rollout = session.scalar(
            select(UpdateRollout).where(
                or_(
                    UpdateRollout.id == identifier,
                    UpdateRollout.job_id == identifier,
                )
            )
        )
        if rollout is None:
            raise KeyError(identifier)
        if rollout.job_id is None:
            raise RuntimeError("update rollout job binding is absent")
        nodes = tuple(
            session.scalars(
                select(UpdateRolloutNode)
                .where(UpdateRolloutNode.rollout_id == rollout.id)
                .order_by(
                    UpdateRolloutNode.batch_index,
                    UpdateRolloutNode.node_order,
                )
            )
        )
        raw_batches = rollout.plan.get("batches")
        if not isinstance(raw_batches, list) or any(
            not isinstance(batch, list)
            or any(not isinstance(node_id, str) for node_id in batch)
            for batch in raw_batches
        ):
            raise RuntimeError("persisted update rollout batches are invalid")
        recovery = rollout.state in {"paused", "waiting-for-approval"}
        rollback_authorized = rollout.state == "paused" and isinstance(
            rollout.rollback_admin_grant, dict
        )
        approval_required = recovery and not rollback_authorized
        required_action = (
            "authorize-rollback"
            if rollout.state == "paused" and not rollback_authorized
            else "approve-resume"
            if rollout.state == "waiting-for-approval"
            else None
        )
        return {
            "batches": [list(batch) for batch in raw_batches],
            "can_approve_resume": approval_required,
            "current_batch": rollout.current_batch,
            "failure_reason": rollout.failure_reason,
            "id": rollout.id,
            "job_id": rollout.job_id,
            "nodes": [{"node_id": node.node_id, "state": node.state} for node in nodes],
            "plan_digest": "sha256:" + rollout.plan_digest,
            "required_action": required_action,
            "resume_required": approval_required,
            "state": rollout.state,
        }


def durable_agent_observations(
    sessions: sessionmaker[Session],
    clock: Callable[[], datetime],
) -> tuple[AgentObservation, ...]:
    """Project authenticated agent heartbeats into exact version-skew evidence."""
    now = _aware(clock())
    with sessions() as session:
        nodes = tuple(session.scalars(select(AgentNode).order_by(AgentNode.node_id)))
    observations: list[AgentObservation] = []
    for node in nodes:
        if node.architecture not in {"linux-arm64", "linux-x86_64"}:
            raise RuntimeError(
                f"agent architecture evidence is unavailable for {node.node_id}"
            )
        last_seen = _aware(node.last_seen_at) if node.last_seen_at is not None else None
        age = (now - last_seen).total_seconds() if last_seen is not None else None
        observations.append(
            AgentObservation(
                node_id=node.node_id,
                state=node.state,
                online=(node.state == "active" and age is not None and 0 <= age <= 300),
                architecture=node.architecture,
                platform_version=node.platform_version,
                build_digest=node.build_digest,
                protocol_version=node.protocol_version,
                active_slot=node.active_slot,
                agent_sha256=node.agent_sha256,
                supervisor_generation=node.supervisor_generation,
                capabilities=tuple(sorted(node.capabilities)),
                last_seen_at=last_seen,
            )
        )
    return tuple(observations)


def durable_route_impacts(
    sessions: sessionmaker[Session],
) -> tuple[RouteImpact, ...]:
    """Load aliases only from the completed reconciliation owning publication."""

    with sessions() as session:
        owner = session.get(RoutePublicationOwner, 1)
        if owner is None or owner.reconciliation_id is None:
            return ()
        reconciliation = session.get(Reconciliation, owner.reconciliation_id)
        publication = session.get(RoutePublication, owner.reconciliation_id)
        if (
            reconciliation is None
            or reconciliation.status != "succeeded"
            or reconciliation.current_phase != "completed"
            or publication is None
            or publication.state != "completed"
            or publication.generation != owner.owner_generation
            or publication.plan_digest != reconciliation.plan_digest
            or not isinstance(reconciliation.resolved_plan, dict)
        ):
            raise RuntimeError("accepted route publication evidence is unavailable")
        routes = reconciliation.resolved_plan.get("routes")
        if routes is None:
            return ()
        if not isinstance(routes, Mapping):
            raise TypeError("accepted route publication evidence is invalid")
        result: list[RouteImpact] = []
        for alias, value in sorted(routes.items()):
            if not isinstance(alias, str) or not isinstance(value, Mapping):
                raise TypeError("accepted route publication evidence is invalid")
            workload_id = value.get("workload_id")
            nodes = value.get("nodes")
            entrypoint = value.get("entrypoint_node_id")
            if (
                not isinstance(workload_id, str)
                or not isinstance(nodes, list)
                or not nodes
                or any(not isinstance(node_id, str) for node_id in nodes)
                or not isinstance(entrypoint, str)
                or entrypoint not in nodes
            ):
                raise RuntimeError("accepted route publication evidence is invalid")
            result.append(RouteImpact(alias, workload_id, tuple(nodes)))
        return tuple(result)


def durable_distributed_workloads(
    sessions: sessionmaker[Session],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[DistributedWorkload, ...]:
    """Project running v1 recipe runs into rollout availability bounds."""

    return durable_recipe_workloads(sessions, clock)


def topology_exclusions_from_document(
    document: object,
) -> tuple[TopologyExclusion, ...]:
    """Project accepted Git topology links into independent update exclusions."""

    if not isinstance(document, Mapping):
        raise TypeError("platform update topology document is invalid")
    nodes = document.get("nodes")
    links = document.get("links")
    if (
        document.get("schema_version") != 1
        or not isinstance(nodes, Sequence)
        or isinstance(nodes, (str, bytes))
        or not isinstance(links, Sequence)
        or isinstance(links, (str, bytes))
        or any(not isinstance(node_id, str) for node_id in nodes)
    ):
        raise ValueError("platform update topology document is invalid")
    known = set(nodes)
    exclusions: list[TopologyExclusion] = []
    for raw in links:
        if not isinstance(raw, Mapping):
            raise TypeError("platform update topology link is invalid")
        link_id = raw.get("id")
        kind = raw.get("kind")
        accepted = raw.get("accepted")
        endpoints = raw.get("endpoints")
        if (
            not isinstance(link_id, str)
            or kind not in {"management", "direct-rdma", "switched-rdma"}
            or not isinstance(accepted, bool)
            or not isinstance(endpoints, Sequence)
            or isinstance(endpoints, (str, bytes))
        ):
            raise ValueError("platform update topology link is invalid")
        members: list[str] = []
        for endpoint in endpoints:
            if not isinstance(endpoint, Mapping):
                raise TypeError("platform update topology endpoint is invalid")
            node_id = endpoint.get("node_id")
            if not isinstance(node_id, str) or node_id not in known:
                raise ValueError("platform update topology endpoint is invalid")
            members.append(node_id)
        if len(members) < 2 or len(set(members)) != len(members):
            raise ValueError("platform update topology link is invalid")
        if accepted and kind != "management":
            exclusions.append(
                TopologyExclusion(
                    exclusion_id=link_id,
                    members=tuple(sorted(members)),
                    maximum_unavailable=1,
                )
            )
    return tuple(sorted(exclusions, key=lambda item: item.exclusion_id))


def target_platform_from_release(
    release: Any,
    *,
    base_commit: str,
    tuf_targets_version: int,
) -> TargetPlatform:
    """Map one validated platform manifest into the planner's immutable target."""
    from cluster_profiles.platform_release import PlatformRelease

    if not isinstance(release, PlatformRelease):
        raise TypeError("platform release target is invalid")
    protocols = {
        (agent.protocol.minimum, agent.protocol.maximum)
        for agent in release.agents
        if agent.protocol is not None
    }
    if (
        not release.agents
        or any(agent.protocol is None for agent in release.agents)
        or len(protocols) != 1
    ):
        raise ValueError("platform agent protocol ranges disagree")
    protocol_minimum, protocol_maximum = protocols.pop()
    return TargetPlatform(
        platform_version=release.platform_version,
        build_digest=release.build_digest,
        release_digest=release.digest,
        base_commit=base_commit,
        protocol_minimum=protocol_minimum,
        protocol_maximum=protocol_maximum,
        tuf_targets_version=tuf_targets_version,
        artifacts=tuple(
            PlatformAgentArtifact(
                architecture=agent.architecture,
                oci_manifest_digest=agent.artifact.reference.rsplit("@", 1)[1],
                payload_name=agent.payload_name,
                payload_sha256=agent.payload_sha256,
                payload_size=agent.payload_size,
            )
            for agent in release.agents
        ),
    )


def selected_platform_target(
    *,
    projections: object,
    running_generation_id: str,
    running_platform_version: str,
    running_release_digest: str,
    running_build_digest: str,
    metadata_root: Path,
    target_root: Path,
    base_commit: str,
    loader: Callable[..., TargetPlatform] | None = None,
) -> TargetPlatform:
    """Bind one admin read or mutation to the freshly selected host projection."""

    load_active = getattr(projections, "load_active_projection", None)
    if not callable(load_active):
        raise TypeError("selected control projection source is invalid")
    selected = load_active()
    if selected is None:
        raise RuntimeError("selected control generation is unavailable")
    target_name = getattr(selected, "platform_target_name", None)
    target_sha256 = getattr(selected, "platform_target_sha256", None)
    targets_version = getattr(selected, "tuf_targets_version", None)
    platform_version = getattr(selected, "platform_version", None)
    release_digest = getattr(selected, "release_digest", None)
    build_digest = getattr(selected, "build_digest", None)
    generation_id = getattr(selected, "generation_id", None)
    expected_name = (
        f"platform/releases/{platform_version}/{target_sha256}.json"
        if isinstance(platform_version, str) and isinstance(target_sha256, str)
        else None
    )
    if (
        generation_id != running_generation_id
        or platform_version != running_platform_version
        or release_digest != running_release_digest
        or build_digest != running_build_digest
        or target_name != expected_name
        or release_digest != f"sha256:{target_sha256}"
        or isinstance(targets_version, bool)
        or not isinstance(targets_version, int)
        or targets_version < 1
    ):
        raise RuntimeError("selected control generation does not match running process")
    load = published_platform_target if loader is None else loader
    return load(
        metadata_root=metadata_root,
        target_root=target_root,
        platform_version=platform_version,
        release_digest=release_digest,
        build_digest=build_digest,
        base_commit=base_commit,
        platform_target_name=target_name,
        platform_target_sha256=target_sha256,
        minimum_tuf_targets_version=targets_version,
    )


def published_platform_target(
    *,
    metadata_root: Path,
    target_root: Path,
    platform_version: str,
    release_digest: str,
    build_digest: str,
    base_commit: str,
    platform_target_name: str,
    platform_target_sha256: str,
    minimum_tuf_targets_version: int,
) -> TargetPlatform:
    """Load the exact published target; the isolated signer re-verifies at dispatch."""
    from tuf.api.metadata import Metadata

    from cluster_profiles.platform_release import PlatformRelease

    target_sha256 = release_digest.removeprefix("sha256:")
    target_name = f"platform/releases/{platform_version}/{target_sha256}.json"
    if (
        platform_target_name != target_name
        or platform_target_sha256 != target_sha256
        or isinstance(minimum_tuf_targets_version, bool)
        or not isinstance(minimum_tuf_targets_version, int)
        or minimum_tuf_targets_version < 1
    ):
        raise RuntimeError("published platform release disagrees with selection")
    target_path = Path(target_root).joinpath(*target_name.split("/"))
    metadata_path = Path(metadata_root) / "targets.json"
    try:
        raw_target = target_path.read_bytes()
        raw_metadata = metadata_path.read_bytes()
    except OSError as error:
        raise RuntimeError(
            "published platform release evidence is unavailable"
        ) from error
    if not raw_target or len(raw_target) > 1024 * 1024:
        raise RuntimeError("published platform release target size is invalid")
    if not raw_metadata or len(raw_metadata) > 2 * 1024 * 1024:
        raise RuntimeError("published platform TUF metadata size is invalid")
    release = PlatformRelease.from_bytes(raw_target)
    actual_sha256 = hashlib.sha256(raw_target).hexdigest()
    release.validate_target_identity(target_name, actual_sha256)
    if (
        release.digest != release_digest
        or release.build_digest != build_digest
        or release.platform_version != platform_version
    ):
        raise RuntimeError("published platform release disagrees with active control")
    try:
        metadata = Metadata.from_bytes(raw_metadata)
        target = metadata.signed.targets[target_name]
        targets_version = metadata.signed.version
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError(
            "published platform TUF target binding is invalid"
        ) from error
    if (
        target.length != len(raw_target)
        or target.hashes.get("sha256") != actual_sha256
        or isinstance(targets_version, bool)
        or not isinstance(targets_version, int)
        or targets_version < minimum_tuf_targets_version
    ):
        raise RuntimeError("published platform TUF target binding is invalid")
    return target_platform_from_release(
        release,
        base_commit=base_commit,
        tuf_targets_version=targets_version,
    )


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("platform update clock is invalid")
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _uuid4(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} is invalid")  # noqa: TRY004
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ValueError(f"{label} is invalid") from None
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{label} is invalid")
    return value


def _digest(document: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_message(document)).hexdigest()


def _target_document(target: TargetPlatform) -> dict[str, object]:
    return {
        "build_digest": target.build_digest,
        "platform_version": target.platform_version,
        "protocol_maximum": target.protocol_maximum,
        "protocol_minimum": target.protocol_minimum,
        "release": _release_reference(target),
        "release_digest": target.release_digest,
        "target_sha256": target.target_sha256,
        "tuf_targets_version": target.tuf_targets_version,
    }


def _plan_document(
    plan: UpdatePlan, routes: tuple[RouteImpact, ...] = ()
) -> dict[str, object]:
    scheduled = [node_id for batch in plan.batches for node_id in batch]
    affected_workloads = [
        workload
        for workload in plan.workloads
        if set(workload.members).intersection(scheduled)
    ]
    return {
        "affected_routes": sorted(
            route.alias for route in routes if set(route.nodes).intersection(scheduled)
        ),
        "batches": [list(batch) for batch in plan.batches],
        "canary_node": plan.canary_node,
        "gates": [
            {
                "detail": (
                    "all scheduled agents are compatible"
                    if not plan.incompatible
                    else "incompatible agent skew blocks apply"
                ),
                "name": "agent-compatibility",
                "status": "passed" if not plan.incompatible else "blocked",
            }
        ],
        "incompatible": list(plan.incompatible),
        "offline_pending": list(plan.offline_pending),
        "plan_digest": plan.plan_digest,
        "rollback_slots": {
            node_id: _rollback_slot(plan.source_for(node_id).active_slot)
            for node_id in scheduled
        },
        "soak_seconds": plan.soak_seconds,
        "target": _target_document(plan.target),
        "workloads": [
            {
                "members": list(workload.members),
                "minimum_available": workload.minimum_available,
                "workload_id": workload.workload_id,
            }
            for workload in affected_workloads
        ],
    }


def _release_reference(target: TargetPlatform) -> str:
    return target.target_name


def _rollback_slot(active_slot: str | None) -> str | None:
    if active_slot == "A":
        return "B"
    if active_slot == "B":
        return "A"
    return None
