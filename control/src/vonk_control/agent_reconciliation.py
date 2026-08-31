"""Durable, evidence-gated execution of persisted reconciliation graphs."""

from __future__ import annotations

import hashlib
import re
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import AgentResult, canonical_message

from .logging import redact_text
from .models import (
    AgentNode,
    AgentOperationAttempt,
    Job,
    NodeMutationLease,
    Reconciliation,
    ReconciliationCancellation,
    ReconciliationOperation,
    RoutePublication,
    RoutePublicationOwner,
)
from .models import (
    AgentOperation as StoredAgentOperation,
)
from .node_leases import NodeLeaseConflict, NodeLeaseGrant, NodeLeaseService
from .orchestration import (
    OperationGraph,
    OperationNode,
    ReconciliationOrchestrator,
    validate_persisted_resolved_plan,
)
from .route_runtime import (
    RECIPE_ROUTE_AUTHORITY_ID,
    AcceptedEndpointEvidence,
    ActivationMarker,
    PublishedRoute,
    RouteBundleRequest,
    build_published_route,
    endpoint_evidence_digest,
)

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_OCI_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_WORKLOAD_ACTIONS = {
    "workload.prepare": "prepare",
    "workload.start": "start",
    "workload.stop": "stop",
    "workload.health": "health",
    "workload.verify": "verify",
}
_MUTATIONS = frozenset(
    {
        "release.install",
        "workload.prepare",
        "workload.start",
        "workload.stop",
    }
)
_REQUIRED_AGENT_CAPABILITIES = frozenset(
    {
        "node.probe",
        "release.install",
        "workload.health",
        "workload.prepare",
        "workload.start",
        "workload.stop",
        "workload.verify",
    }
)
_NEXT_AGENT_CAPABILITIES = _REQUIRED_AGENT_CAPABILITIES | frozenset(
    {
        "agent.runtime.rust.v1",
        "runtime.vonk.v1",
        "recipe.build.v1",
        "recipe.image.import.v1",
        "recipe.install",
        "recipe.job.run.v1",
        "recipe.start",
        "recipe.stop",
        "recipe.uninstall",
        "recipe.model-uninstall.v1",
    }
)
_ACTIVE_CANCELLATION_STATES = frozenset(
    {
        "requested",
        "withdrawal-pending",
        "withdrawn",
        "processing",
        "compensating",
    }
)


@dataclass(frozen=True)
class ReconciliationAuthorityInput:
    """Immutable identity and route-policy input safe to attest before locking."""

    reconciliation_id: str
    authority_revision: str
    plan_digest: str
    fleet_evidence_digest: str
    routes: tuple[PublishedRoute, ...]


def _published_authority_routes(
    session: Session,
    document: Mapping[str, object],
    endpoint_resolver: Callable[[Session, str], tuple[str, datetime]],
) -> tuple[PublishedRoute, ...]:
    routes = document.get("routes")
    if not isinstance(routes, Mapping):
        raise TypeError("reconciliation routes are invalid")
    parsed_routes: list[tuple[str, Mapping[str, object], str]] = []
    for alias, raw in sorted(routes.items()):
        if not isinstance(alias, str):
            raise TypeError("reconciliation route alias is invalid")
        if not isinstance(raw, Mapping):
            raise TypeError("reconciliation route is invalid")
        node_id = raw.get("entrypoint_node_id")
        if not isinstance(node_id, str):
            raise TypeError("reconciliation route entrypoint is invalid")
        parsed_routes.append((alias, raw, node_id))
    addresses = {
        node_id: endpoint_resolver(session, node_id)[0]
        for node_id in sorted({item[2] for item in parsed_routes})
    }
    published: list[PublishedRoute] = []
    for alias, raw, node_id in parsed_routes:
        address = addresses[node_id]
        published.append(build_published_route(alias, raw, address))
    return tuple(published)


def load_reconciliation_authority_input(
    session: Session,
    reconciliation_id: str,
    endpoint_resolver: Callable[[Session, str], tuple[str, datetime]],
) -> ReconciliationAuthorityInput:
    """Load one snapshot in a DB transaction that must close before HTTP."""

    reconciliation = session.get(Reconciliation, reconciliation_id)
    if reconciliation is None:
        raise ValueError("reconciliation does not exist")
    _graph, document = AgentReconciliationService._validated_plan(reconciliation)
    published = _published_authority_routes(
        session,
        document,
        endpoint_resolver,
    )
    return ReconciliationAuthorityInput(
        reconciliation_id=reconciliation.id,
        authority_revision=reconciliation.authority_revision,
        plan_digest=AgentReconciliationService._plan_digest(reconciliation),
        fleet_evidence_digest=str(document["fleet_evidence_digest"]),
        routes=published,
    )


def _digest(document: object) -> str:
    return hashlib.sha256(canonical_message(document)).hexdigest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def ready_operation_ids(
    nodes: Sequence[OperationNode], states: Mapping[str, str]
) -> tuple[str, ...]:
    """Return the deterministic next wave using accepted projections only."""

    accepted = {
        operation_id for operation_id, state in states.items() if state == "accepted"
    }
    pending = {
        node.operation_id
        for node in nodes
        if states.get(node.operation_id, "planned") == "planned"
        and all(dependency in accepted for dependency in node.dependencies)
    }
    return tuple(sorted(pending))


def compensation_order(
    nodes: Sequence[OperationNode], states: Mapping[str, str]
) -> tuple[str, ...]:
    """Return accepted compensatable mutations in reverse graph order."""

    return tuple(
        node.operation_id
        for node in reversed(tuple(nodes))
        if states.get(node.operation_id) == "accepted"
        and node.compensation_kind is not None
    )


def accepted_result_digests(
    kind: str,
    payload: Mapping[str, object],
    result: object,
) -> tuple[str, str]:
    """Authenticate bounded agent evidence against the exact dispatched request."""

    if not isinstance(result, Mapping) or set(result) != {"status", "evidence"}:
        raise ValueError("accepted agent result is invalid")
    if result.get("status") != "ok":
        raise ValueError("accepted agent result status is invalid")
    evidence = result.get("evidence")
    if not isinstance(evidence, Mapping):
        raise TypeError("accepted agent result evidence is invalid")
    if kind == "release.install":
        _release_evidence(payload, evidence)
    elif kind in _WORKLOAD_ACTIONS:
        _workload_evidence(kind, payload, evidence)
    elif kind == "node.probe":
        _probe_evidence(payload, evidence)
    else:
        raise ValueError("accepted agent result operation is invalid")
    return _digest(result), _digest(evidence)


def _release_evidence(
    payload: Mapping[str, object], evidence: Mapping[str, object]
) -> None:
    if set(evidence) != {
        "status",
        "release_digest",
        "manifest_digest",
        "adapter_id",
    }:
        raise ValueError("release evidence is invalid")
    if evidence.get("status") not in {"installed", "already-installed"}:
        raise ValueError("release evidence status is invalid")
    if (
        evidence.get("release_digest") != payload.get("target_digest")
        or evidence.get("manifest_digest") != payload.get("oci_manifest_digest")
        or evidence.get("adapter_id") != payload.get("adapter_id")
        or not isinstance(evidence.get("release_digest"), str)
        or _DIGEST.fullmatch(evidence["release_digest"]) is None
        or not isinstance(evidence.get("manifest_digest"), str)
        or _OCI_DIGEST.fullmatch(evidence["manifest_digest"]) is None
        or not isinstance(evidence.get("adapter_id"), str)
        or not evidence["adapter_id"]
    ):
        raise ValueError("release evidence does not match the request")


def _workload_evidence(
    kind: str,
    payload: Mapping[str, object],
    evidence: Mapping[str, object],
) -> None:
    if set(evidence) != {
        "status",
        "action",
        "workload_id",
        "release_digest",
        "evidence_digest",
    }:
        raise ValueError("workload evidence is invalid")
    evidence_digest = evidence.get("evidence_digest")
    if (
        not isinstance(evidence.get("status"), str)
        or not evidence["status"]
        or evidence.get("action") != _WORKLOAD_ACTIONS[kind]
        or evidence.get("workload_id") != payload.get("workload_id")
        or evidence.get("release_digest") != payload.get("release_digest")
        or not isinstance(evidence_digest, str)
        or _DIGEST.fullmatch(evidence_digest) is None
    ):
        raise ValueError("workload evidence does not match the request")
    if kind == "workload.verify" and evidence_digest != payload.get("expected_digest"):
        raise ValueError("workload verify evidence digest does not match the request")


def _probe_evidence(
    payload: Mapping[str, object], evidence: Mapping[str, object]
) -> None:
    if payload != {"require_active_nvidia_compute_processes": 0}:
        raise ValueError("node probe request is not an authenticated compute gate")
    health = evidence.get("vonk_forge")
    nvidia = evidence.get("nvidia")
    accelerator = health.get("accelerator") if isinstance(health, Mapping) else None
    if (
        set(evidence) != {"vonk_forge", "nvidia"}
        or not isinstance(health, Mapping)
        or health.get("schema_version") != 1
        or not isinstance(accelerator, Mapping)
        or accelerator.get("active_nvidia_compute_processes") != 0
        or not isinstance(nvidia, Mapping)
    ):
        raise ValueError("node probe compute gate evidence is invalid")


class AgentReconciliationService:
    """Advance one immutable graph using only durable, fenced agent evidence."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        agent_jobs: Any,
        publisher: Any,
        endpoint_resolver: Callable[[Session, str], tuple[str, datetime]],
        clock: Callable[[], datetime],
        publication_lease_seconds: int = 60,
        revision_eligible: Callable[[str], bool] | None = None,
        current_revision: Callable[[], str] | None = None,
        authority_prefetch: Callable[[str, str, str, tuple[PublishedRoute, ...]], None]
        | None = None,
        authority_check: Callable[
            [str, str, str, tuple[PublishedRoute, ...]], bool | str
        ]
        | None = None,
        authority_clear: Callable[[], None] | None = None,
    ) -> None:
        if not 1 <= publication_lease_seconds <= 300:
            raise ValueError("reconciliation publication lease is invalid")
        if (revision_eligible is None) != (current_revision is None):
            raise ValueError("reconciliation authority is incomplete")
        if any(
            callback is not None
            for callback in (authority_prefetch, authority_check, authority_clear)
        ) and any(
            callback is None
            for callback in (authority_prefetch, authority_check, authority_clear)
        ):
            raise ValueError("reconciliation prefetched authority is incomplete")
        if authority_check is not None and revision_eligible is not None:
            raise ValueError("reconciliation authority modes are ambiguous")
        self._sessions = sessions
        self._agent_jobs = agent_jobs
        self._publisher = publisher
        self._endpoint_resolver = endpoint_resolver
        self._clock = clock
        self._publication_lease_seconds = publication_lease_seconds
        self._revision_eligible = revision_eligible
        self._current_revision = current_revision
        self._authority_prefetch = authority_prefetch
        self._authority_check = authority_check
        self._authority_clear = authority_clear
        self._node_leases = NodeLeaseService(clock=clock)
        # SQLite ignores row locks; PostgreSQL remains the production arbiter.
        self._tick_lock = threading.RLock()

    def attach_job(self, reconciliation_id: str, job_id: str) -> None:
        """Bind the sole durable parent job; JSON fields never grant authority."""

        with self._sessions.begin() as session:
            reconciliation, job, graph, _document = self._locked_context(
                session, reconciliation_id, expected_job_id=job_id
            )
            if job.reconciliation_id not in {None, reconciliation.id}:
                raise ValueError("job is attached to another reconciliation")
            if job.authority_revision != reconciliation.authority_revision:
                raise ValueError("reconciliation job authority revision does not match")
            self._require_active_targets(session, graph)
            authority_reason = self._continuous_authority_reason(
                session, reconciliation, graph, _document
            )
            if authority_reason is not None:
                raise ValueError(authority_reason)
            job.reconciliation_id = reconciliation.id
            job.state = "running"
            job.updated_at = self._clock()

    def tick(self, reconciliation_id: str | None = None) -> bool:
        """Advance one durable phase and return whether work was available."""

        with self._tick_lock:
            if reconciliation_id is not None:
                self._prepare_authority(reconciliation_id)
                return self._tick_candidate(
                    reconciliation_id,
                    automatically_selected=False,
                )
            completed_owner = self._completed_owner_id()
            if completed_owner is not None:
                self._prepare_authority(completed_owner)
                if self._tick_candidate(
                    completed_owner,
                    automatically_selected=True,
                ):
                    return True
            candidate = self._candidate_id()
            if candidate is None:
                return False
            self._prepare_authority(candidate)
            return self._tick_candidate(
                candidate,
                automatically_selected=True,
            )

    def _prepare_authority(self, reconciliation_id: str) -> None:
        """Perform the only remote authority call before any locked context."""

        if (
            self._authority_prefetch is None
            or self._authority_check is None
            or self._authority_clear is None
        ):
            return
        self._authority_clear()
        try:
            with self._sessions() as session:
                snapshot = load_reconciliation_authority_input(
                    session,
                    reconciliation_id,
                    self._endpoint_resolver,
                )
            self._authority_prefetch(
                snapshot.reconciliation_id,
                snapshot.authority_revision,
                snapshot.plan_digest,
                snapshot.routes,
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            # The locked tick must still run so an existing publication is
            # withdrawn through its durable handoff path.
            return

    def _tick_candidate(
        self,
        candidate: str,
        *,
        automatically_selected: bool,
    ) -> bool:
        with self._tick_lock:
            notify = False
            advanced = False
            with self._sessions.begin() as session:
                reconciliation, job, graph, document = self._locked_context(
                    session, candidate
                )
                phase = reconciliation.current_phase
                released_node_lease = self._release_pending_node_lease(
                    session, reconciliation, graph
                )
                cancellation = self._cancellation(session, reconciliation.id)
                if (
                    released_node_lease
                    and phase != "completed"
                    and cancellation is None
                ):
                    return True
                if phase in {"failed", "cancelled"} and (
                    self._mark_node_lease_releasing(session, reconciliation, graph)
                ):
                    return True
                if (
                    phase
                    in {
                        "withdrawal-pending",
                        "routes-withdrawn",
                        "dispatching",
                        "accepting",
                        "publication-pending",
                        "compensating",
                    }
                    and not (
                        phase == "withdrawal-pending"
                        and not self._targets_are_active(session, graph)
                    )
                    and self._ensure_node_lease(session, reconciliation, graph) is None
                ):
                    return False
                if cancellation is not None:
                    if (
                        phase == "completed"
                        and cancellation.state in {"requested", "withdrawal-pending"}
                        and self._owns_publication(
                            session,
                            reconciliation,
                            may_supersede=False,
                        )
                        and self._ensure_node_lease(session, reconciliation, graph)
                        is None
                    ):
                        return False
                    cancellation_advanced = self._advance_cancellation(
                        session,
                        reconciliation,
                        job,
                        graph,
                        cancellation,
                    )
                    if cancellation_advanced is not None:
                        if reconciliation.current_phase in {"failed", "cancelled"}:
                            self._mark_node_lease_releasing(
                                session, reconciliation, graph
                            )
                        return cancellation_advanced
                if phase in {"failed", "cancelled", "waiting-for-operator"}:
                    return released_node_lease
                if self._sweep_expired_mutations(session, reconciliation, job, graph):
                    return True
                publication_owner = self._publication_owner(session)
                if (
                    phase == "withdrawal-pending"
                    and publication_owner.reconciliation_id is not None
                    and publication_owner.reconciliation_id != reconciliation.id
                ):
                    if not self._owns_publication(
                        session,
                        reconciliation,
                        may_supersede=True,
                    ):
                        return False
                    publication = self._publication(session, reconciliation.id)
                    marker = self._publisher.withdraw(
                        reconciliation_id=reconciliation.id,
                        plan_digest=self._plan_digest(reconciliation),
                        targets=graph.targets,
                        reason="reconciliation maintenance",
                    )
                    self._transfer_publication_owner(session, reconciliation)
                    self._store_marker(publication, marker, "routes-withdrawn")
                    reconciliation.current_phase = "routes-withdrawn"
                    return True
                authority_reason = self._continuous_authority_reason(
                    session, reconciliation, graph, document
                )
                if authority_reason is not None:
                    acknowledged_owner = (
                        self._publication_owner(session).reconciliation_id
                        == reconciliation.id
                    )
                    owns_publication = self._owns_publication(
                        session,
                        reconciliation,
                        may_supersede=False,
                    )
                    if (
                        (owns_publication or acknowledged_owner)
                        and phase in {"completed", "publication-pending"}
                        and self._publisher is not None
                    ):
                        if (
                            self._ensure_node_lease(session, reconciliation, graph)
                            is None
                        ):
                            return False
                        publication = self._publication(session, reconciliation.id)
                        marker = self._publisher.withdraw(
                            reconciliation_id=reconciliation.id,
                            plan_digest=self._plan_digest(reconciliation),
                            targets=graph.targets,
                            reason="reconciliation authority lost",
                        )
                        self._store_marker(publication, marker, "routes-withdrawn")
                    self._quiesce_for_unavailable_target(
                        session,
                        reconciliation,
                        job,
                        graph,
                        authority_reason,
                    )
                    return True
                if phase == "withdrawal-pending":
                    if not self._owns_publication(
                        session,
                        reconciliation,
                        may_supersede=True,
                    ):
                        return False
                    publication = self._publication(session, reconciliation.id)
                    marker = self._publisher.withdraw(
                        reconciliation_id=reconciliation.id,
                        plan_digest=self._plan_digest(reconciliation),
                        targets=graph.targets,
                        reason="reconciliation maintenance",
                    )
                    self._transfer_publication_owner(session, reconciliation)
                    self._store_marker(publication, marker, "routes-withdrawn")
                    reconciliation.current_phase = "routes-withdrawn"
                    return True
                if not self._owns_publication(
                    session,
                    reconciliation,
                    may_supersede=phase == "planned",
                ):
                    return False
                if phase == "planned":
                    if job.authority_revision != reconciliation.authority_revision:
                        raise ValueError(
                            "reconciliation job authority revision does not match"
                        )
                    owner_id = session.scalar(
                        select(RoutePublicationOwner.reconciliation_id).where(
                            RoutePublicationOwner.singleton_id == 1
                        )
                    )
                    owner = (
                        None
                        if owner_id is None
                        else session.get(Reconciliation, owner_id)
                    )
                    predecessor_handoff = (
                        owner is not None
                        and owner.id != reconciliation.id
                        and owner.current_phase
                        in {"completed", "failed", "cancelled", "waiting-for-operator"}
                    )
                    if (
                        not predecessor_handoff
                        and self._ensure_node_lease(session, reconciliation, graph)
                        is None
                    ):
                        return False
                    session.add(
                        RoutePublication(
                            reconciliation_id=reconciliation.id,
                            state="withdrawal-pending",
                            generation=None,
                            plan_digest=self._plan_digest(reconciliation),
                        )
                    )
                    reconciliation.current_phase = "withdrawal-pending"
                    reconciliation.status = "running"
                    job.state = "running"
                    job.status_reason = None
                    job.updated_at = self._clock()
                    return True
                publication = self._publication(session, reconciliation.id)
                if phase == "completed" and automatically_selected:
                    expires_at = publication.lease_expires_at
                    if expires_at is None or _aware(expires_at) > _aware(
                        self._clock()
                    ) + timedelta(seconds=30):
                        if not released_node_lease:
                            return False
                        owner_id = session.scalar(
                            select(RoutePublicationOwner.reconciliation_id).where(
                                RoutePublicationOwner.singleton_id == 1
                            )
                        )
                        return (
                            owner_id != reconciliation.id
                            or publication.generation is None
                        )
                if phase == "routes-withdrawn":
                    if not self._targets_are_active(session, graph):
                        self._quiesce_for_unavailable_target(
                            session,
                            reconciliation,
                            job,
                            graph,
                            "reconciliation target agent is unavailable",
                        )
                        return True
                    existing = {
                        row.graph_operation_id
                        for row in self._projections(
                            session, reconciliation.id, "primary"
                        )
                    }
                    for node in graph.nodes:
                        if node.operation_id not in existing:
                            session.add(
                                ReconciliationOperation(
                                    reconciliation_id=reconciliation.id,
                                    graph_operation_id=node.operation_id,
                                    role="primary",
                                    expected_payload_digest=node.payload_digest,
                                    state="planned",
                                )
                            )
                    reconciliation.current_phase = "dispatching"
                    return True
                if phase == "dispatching":
                    if not self._targets_are_active(session, graph):
                        self._quiesce_for_unavailable_target(
                            session,
                            reconciliation,
                            job,
                            graph,
                            "reconciliation target agent is unavailable",
                        )
                        return True
                    notify = self._dispatch_primary(
                        session, reconciliation, job, graph, document
                    )
                elif phase == "accepting":
                    if not self._targets_are_active(session, graph):
                        self._quiesce_for_unavailable_target(
                            session,
                            reconciliation,
                            job,
                            graph,
                            "reconciliation target agent is unavailable",
                        )
                        return True
                    evidence_digest = self._accepted_evidence_digest(
                        self._projections(session, reconciliation.id, "primary")
                    )
                    publication.state = "publication-pending"
                    publication.evidence_digest = evidence_digest
                    reconciliation.current_phase = "publication-pending"
                elif phase == "publication-pending":
                    if not self._targets_are_active(session, graph):
                        self._quiesce_for_unavailable_target(
                            session,
                            reconciliation,
                            job,
                            graph,
                            "reconciliation target agent is unavailable",
                        )
                        return True
                    request = self._publication_request(
                        session, reconciliation, document, publication
                    )
                    marker = self._publisher.publish(request)
                    self._store_marker(publication, marker, "completed")
                    reconciliation.current_phase = "completed"
                    reconciliation.status = "succeeded"
                    if reconciliation.completion_generation is None:
                        reconciliation.completion_generation = (
                            ReconciliationOrchestrator._next_completion_generation(
                                session
                            )
                        )
                    job.state = "succeeded"
                    job.status_reason = None
                    job.result = {
                        "reconciliation_id": reconciliation.id,
                        "plan_digest": self._plan_digest(reconciliation),
                        "bundle_digest": marker.manifest_sha256,
                    }
                    job.updated_at = self._clock()
                    self._mark_node_lease_releasing(session, reconciliation, graph)
                elif phase == "completed":
                    if self._ensure_node_lease(session, reconciliation, graph) is None:
                        return released_node_lease
                    marker = self._publisher.withdraw(
                        reconciliation_id=reconciliation.id,
                        plan_digest=self._plan_digest(reconciliation),
                        targets=graph.targets,
                        reason="route lease renewal",
                    )
                    self._store_marker(publication, marker, "routes-withdrawn")
                    reconciliation.current_phase = "accepting"
                    reconciliation.status = "running"
                    job.state = "running"
                    job.updated_at = self._clock()
                elif phase == "compensating":
                    if not self._targets_are_active(session, graph):
                        self._quiesce_for_unavailable_target(
                            session,
                            reconciliation,
                            job,
                            graph,
                            "reconciliation target agent is unavailable during compensation",
                        )
                        return True
                    notify = self._dispatch_compensation(
                        session, reconciliation, job, graph, document
                    )
                    if reconciliation.current_phase == "failed":
                        self._mark_node_lease_releasing(session, reconciliation, graph)
                elif phase in {
                    "failed",
                    "cancelled",
                    "waiting-for-operator",
                }:
                    return False
                else:
                    raise ValueError("reconciliation execution phase is invalid")
                advanced = notify or reconciliation.current_phase != phase
            if notify:
                self._agent_jobs.notify_available()
            return advanced

    def consume_result(
        self,
        session: Session,
        operation: StoredAgentOperation,
        attempt: AgentOperationAttempt,
        message: AgentResult,
    ) -> None:
        """Accept one exact result inside the agent result transaction."""

        hint = session.scalar(
            select(ReconciliationOperation).where(
                ReconciliationOperation.agent_operation_id == operation.id
            )
        )
        if hint is None:
            return
        reconciliation = session.scalar(
            select(Reconciliation)
            .where(Reconciliation.id == hint.reconciliation_id)
            .with_for_update(of=Reconciliation)
        )
        if reconciliation is None:
            raise KeyError(hint.reconciliation_id)
        job = session.scalar(
            select(Job)
            .where(
                Job.id == operation.parent_job_id,
                Job.reconciliation_id == reconciliation.id,
            )
            .with_for_update(of=Job)
        )
        projection = session.scalar(
            select(ReconciliationOperation)
            .where(ReconciliationOperation.id == hint.id)
            .with_for_update(of=ReconciliationOperation)
        )
        if job is None or projection is None:
            raise ValueError("agent result lacks its reconciliation projection")
        graph, document = self._validated_plan(reconciliation)
        node = self._graph_node(graph, projection.graph_operation_id)
        payload = self._operation_payload(document, node.operation_id)
        kind = node.kind
        if projection.role == "compensation":
            if node.compensation_kind is None:
                raise ValueError("reconciliation compensation is not graph-authorized")
            kind = node.compensation_kind
            payload = self._compensation_payload(payload)
        self._validate_operation_binding(
            operation,
            attempt,
            message,
            reconciliation,
            job,
            projection,
            node,
            kind,
            payload,
        )
        expected_phase = (
            "compensating" if projection.role == "compensation" else "dispatching"
        )
        if reconciliation.current_phase != expected_phase:
            raise ValueError("agent result is invalid for reconciliation phase")
        authority_reason = self._continuous_authority_reason(
            session, reconciliation, graph, document
        )
        if authority_reason is not None:
            terminal = (
                "waiting-for-operator" if operation.kind in _MUTATIONS else "failed"
            )
            attempt.state = terminal
            operation.state = terminal
            operation.updated_at = self._clock()
            projection.state = terminal
            self._quiesce_pending(
                session,
                reconciliation,
                graph,
                self._projections(session, reconciliation.id, "primary"),
            )
            self._quiesce_pending(
                session,
                reconciliation,
                graph,
                self._projections(session, reconciliation.id, "compensation"),
            )
            self._wait_for_operator(reconciliation, job, authority_reason)
            return
        now = self._clock()
        if message.state == "succeeded":
            result_digest, evidence_digest = accepted_result_digests(
                kind, payload, message.result
            )
            projection.result_digest = result_digest
            projection.evidence_digest = evidence_digest
            projection.accepted_at = now
            projection.state = (
                "compensated" if projection.role == "compensation" else "accepted"
            )
            return
        projection.state = (
            "waiting-for-operator"
            if message.state == "waiting-for-operator"
            else "failed"
        )
        projection.result_digest = _digest(message.result)
        reason = self._result_reason(message)
        if projection.role == "compensation" or message.state == "waiting-for-operator":
            self._quiesce_pending(
                session,
                reconciliation,
                graph,
                self._projections(session, reconciliation.id, "primary"),
            )
            self._quiesce_pending(
                session,
                reconciliation,
                graph,
                self._projections(session, reconciliation.id, "compensation"),
            )
            self._wait_for_operator(reconciliation, job, reason)
            return
        self._handle_primary_failure(session, reconciliation, job, graph, node, reason)
        if reconciliation.current_phase in {"failed", "cancelled"}:
            self._mark_node_lease_releasing(session, reconciliation, graph)

    def request_cancel(self, reconciliation_id: str, reason: str) -> None:
        self.enqueue_cancel(
            reconciliation_id,
            reason,
            actor="internal-cancellation-adapter",
            request_id=str(uuid.uuid4()),
        )
        for _ in range(3):
            if not self.tick(reconciliation_id):
                break

    def enqueue_cancel(
        self,
        reconciliation_id: str,
        reason: str,
        *,
        actor: str,
        request_id: str,
    ) -> ReconciliationCancellation:
        """Commit idempotent cancellation intent before any external effect."""

        if (
            not isinstance(reason, str)
            or not reason.strip()
            or not isinstance(actor, str)
            or not actor.strip()
        ):
            raise ValueError("cancellation reason and actor are required")
        try:
            canonical_request_id = str(uuid.UUID(request_id))
        except (AttributeError, TypeError, ValueError):
            raise ValueError("cancellation request ID is invalid") from None
        if canonical_request_id != request_id:
            raise ValueError("cancellation request ID is invalid")
        with self._sessions.begin() as session:
            existing = session.scalar(
                select(ReconciliationCancellation)
                .where(
                    ReconciliationCancellation.reconciliation_id == reconciliation_id
                )
                .with_for_update(of=ReconciliationCancellation)
            )
            if existing is not None:
                return existing
            reconciliation, _job, _graph, _document = self._locked_context(
                session, reconciliation_id
            )
            if reconciliation.current_phase in {"failed", "cancelled"}:
                raise ValueError("reconciliation is terminal")
            now = self._clock()
            cancellation = ReconciliationCancellation(
                reconciliation_id=reconciliation.id,
                state="requested",
                reason=self._safe_reason(reason.strip()),
                actor=actor.strip()[:200],
                request_id=request_id,
                requested_at=now,
                updated_at=now,
            )
            session.add(cancellation)
            session.flush()
            return cancellation

    def _advance_cancellation(
        self,
        session: Session,
        reconciliation: Reconciliation,
        job: Job,
        graph: OperationGraph,
        cancellation: ReconciliationCancellation,
    ) -> bool | None:
        now = self._clock()
        if cancellation.state == "requested":
            owns_publication = self._owns_publication(
                session,
                reconciliation,
                may_supersede=False,
            )
            if (
                not owns_publication
                and self._pending_publication_handoff(session) is not None
            ):
                return False
            cancellation.state = (
                "withdrawal-pending"
                if reconciliation.current_phase in {"completed", "publication-pending"}
                and owns_publication
                else "processing"
            )
            cancellation.updated_at = now
            return True
        if cancellation.state == "withdrawal-pending":
            owns_publication = self._owns_publication(
                session,
                reconciliation,
                may_supersede=False,
            )
            if (
                not owns_publication
                and self._pending_publication_handoff(session) is not None
            ):
                return False
            if owns_publication:
                if self._publisher is None:
                    raise RuntimeError("route publisher is unavailable")
                publication = self._publication(session, reconciliation.id)
                marker = self._publisher.withdraw(
                    reconciliation_id=reconciliation.id,
                    plan_digest=self._plan_digest(reconciliation),
                    targets=graph.targets,
                    reason="reconciliation cancellation",
                )
                self._store_marker(publication, marker, "routes-withdrawn")
                cancellation.state = "withdrawn"
            else:
                cancellation.state = "processing"
            cancellation.updated_at = now
            return True
        if cancellation.state == "withdrawn":
            cancellation.state = "processing"
            cancellation.updated_at = now
            self._apply_cancellation(session, reconciliation, job, graph, cancellation)
            return True
        if cancellation.state == "processing":
            self._apply_cancellation(session, reconciliation, job, graph, cancellation)
            return True
        if cancellation.state == "compensating":
            if reconciliation.current_phase == "failed":
                cancellation.state = "completed"
            elif reconciliation.current_phase == "waiting-for-operator":
                cancellation.state = "waiting-for-operator"
            else:
                return None
            cancellation.updated_at = now
            return True
        return None

    def _apply_cancellation(
        self,
        session: Session,
        reconciliation: Reconciliation,
        job: Job,
        graph: OperationGraph,
        cancellation: ReconciliationCancellation,
    ) -> None:
        projections = self._projections(session, reconciliation.id, "primary")
        compensations = self._projections(session, reconciliation.id, "compensation")
        if not self._owns_publication(
            session,
            reconciliation,
            may_supersede=False,
        ):
            uncertain = self._quiesce_pending(
                session,
                reconciliation,
                graph,
                projections,
            )
            uncertain = (
                self._quiesce_pending(
                    session,
                    reconciliation,
                    graph,
                    compensations,
                )
                or uncertain
            )
            reconciliation.terminal_reason = cancellation.reason
            if uncertain:
                self._wait_for_operator(
                    reconciliation,
                    job,
                    "historical cancellation interrupted a running mutation",
                )
                cancellation.state = "waiting-for-operator"
            else:
                reconciliation.current_phase = "cancelled"
                reconciliation.status = "cancelled"
                job.state = "failed"
                job.status_reason = "historical reconciliation cancelled"
                job.updated_at = self._clock()
                cancellation.state = "completed"
            cancellation.updated_at = self._clock()
            return
        if reconciliation.current_phase == "waiting-for-operator":
            self._quiesce_pending(session, reconciliation, graph, projections)
            self._quiesce_pending(session, reconciliation, graph, compensations)
            cancellation.state = "waiting-for-operator"
            cancellation.updated_at = self._clock()
            return
        unsafe_mutation = any(
            self._graph_node(graph, row.graph_operation_id).kind in _MUTATIONS
            and row.state
            in {
                "failed",
                "running",
                "succeeded",
                "uncertain",
                "waiting-for-operator",
            }
            for row in projections
        ) or any(
            row.state
            in {
                "failed",
                "running",
                "succeeded",
                "uncertain",
                "waiting-for-operator",
            }
            for row in compensations
        )
        uncertain = self._quiesce_pending(session, reconciliation, graph, projections)
        uncertain = (
            self._quiesce_pending(session, reconciliation, graph, compensations)
            or uncertain
        )
        mutated = [
            row
            for row in projections
            if row.state == "accepted"
            and self._graph_node(graph, row.graph_operation_id).kind in _MUTATIONS
        ]
        reconciliation.terminal_reason = cancellation.reason
        if uncertain or unsafe_mutation:
            self._wait_for_operator(
                reconciliation,
                job,
                "cancellation interrupted a running mutation",
            )
            cancellation.state = "waiting-for-operator"
        elif any(
            self._graph_node(graph, row.graph_operation_id).compensation_kind
            for row in mutated
        ):
            reconciliation.current_phase = "compensating"
            reconciliation.status = "running"
            job.state = "running"
            job.updated_at = self._clock()
            cancellation.state = "compensating"
        elif mutated:
            self._wait_for_operator(
                reconciliation, job, "cancellation requires operator recovery"
            )
            cancellation.state = "waiting-for-operator"
        else:
            reconciliation.current_phase = "cancelled"
            reconciliation.status = "cancelled"
            job.state = "failed"
            job.status_reason = "reconciliation cancelled before mutation"
            job.updated_at = self._clock()
            cancellation.state = "completed"
        cancellation.updated_at = self._clock()

    def _completed_owner_id(self) -> str | None:
        with self._sessions() as session:
            owner_id = session.scalar(
                select(RoutePublicationOwner.reconciliation_id).where(
                    RoutePublicationOwner.singleton_id == 1
                )
            )
            if owner_id == RECIPE_ROUTE_AUTHORITY_ID:
                return None
            if owner_id is None:
                owner_id = session.scalar(
                    select(Reconciliation.id)
                    .where(
                        Reconciliation.status == "succeeded",
                        Reconciliation.current_phase == "completed",
                        Reconciliation.completion_generation.is_not(None),
                    )
                    .order_by(
                        Reconciliation.completion_generation.desc(),
                        Reconciliation.id.desc(),
                    )
                    .limit(1)
                )
            if owner_id is None:
                return None
            return session.scalar(
                select(Reconciliation.id).where(
                    Reconciliation.id == owner_id,
                    Reconciliation.current_phase == "completed",
                )
            )

    def _candidate_id(self) -> str | None:
        with self._sessions() as session:
            releasing_owner = session.scalar(
                select(Reconciliation.id)
                .join(
                    NodeMutationLease,
                    and_(
                        NodeMutationLease.owner_kind == "reconciliation",
                        NodeMutationLease.owner_id == Reconciliation.id,
                    ),
                )
                .where(NodeMutationLease.state == "releasing")
                .order_by(NodeMutationLease.updated_at, Reconciliation.id)
                .limit(1)
            )
            pending = session.scalar(
                select(Reconciliation.id)
                .join(
                    RoutePublication,
                    RoutePublication.reconciliation_id == Reconciliation.id,
                )
                .where(
                    Reconciliation.current_phase == "withdrawal-pending",
                    RoutePublication.state == "withdrawal-pending",
                )
                .order_by(Reconciliation.created_at.desc(), Reconciliation.id.desc())
                .limit(1)
            )
            if releasing_owner is not None:
                releasing_phase = session.scalar(
                    select(Reconciliation.current_phase).where(
                        Reconciliation.id == releasing_owner
                    )
                )
                if releasing_phase != "waiting-for-operator" or pending is None:
                    return releasing_owner
            if pending is not None:
                return pending
            expired_mutation = session.scalar(
                select(Reconciliation.id)
                .join(Job, Job.reconciliation_id == Reconciliation.id)
                .join(
                    StoredAgentOperation,
                    StoredAgentOperation.parent_job_id == Job.id,
                )
                .join(
                    AgentOperationAttempt,
                    and_(
                        AgentOperationAttempt.operation_id == StoredAgentOperation.id,
                        AgentOperationAttempt.attempt
                        == StoredAgentOperation.current_attempt,
                    ),
                )
                .where(
                    StoredAgentOperation.state == "running",
                    StoredAgentOperation.kind.in_(_MUTATIONS),
                    AgentOperationAttempt.state == "running",
                    AgentOperationAttempt.lease_deadline <= self._clock(),
                )
                .order_by(Reconciliation.created_at, Reconciliation.id)
                .limit(1)
            )
            if expired_mutation is not None:
                return expired_mutation
            cancellation = session.scalar(
                select(Reconciliation.id)
                .join(
                    ReconciliationCancellation,
                    ReconciliationCancellation.reconciliation_id == Reconciliation.id,
                )
                .where(
                    ReconciliationCancellation.state.in_(_ACTIVE_CANCELLATION_STATES)
                )
                .order_by(Reconciliation.created_at, Reconciliation.id)
                .limit(1)
            )
            if cancellation is not None:
                return cancellation
            owner_id = session.scalar(
                select(RoutePublicationOwner.reconciliation_id).where(
                    RoutePublicationOwner.singleton_id == 1
                )
            )
            owner = None if owner_id is None else session.get(Reconciliation, owner_id)
            active_owner_phases = {
                "planned",
                "routes-withdrawn",
                "dispatching",
                "accepting",
                "publication-pending",
                "compensating",
            }
            if owner is not None and owner.current_phase in active_owner_phases:
                return owner.id
            planned = select(Reconciliation.id).where(
                Reconciliation.current_phase == "planned"
            )
            if owner is not None:
                planned = planned.where(
                    or_(
                        Reconciliation.created_at > owner.created_at,
                        and_(
                            Reconciliation.created_at == owner.created_at,
                            Reconciliation.id > owner.id,
                        ),
                    )
                )
            candidate = session.scalar(
                planned.order_by(Reconciliation.created_at, Reconciliation.id).limit(1)
            )
            if candidate is not None or owner is not None:
                return candidate
            return session.scalar(
                select(Reconciliation.id)
                .where(
                    Reconciliation.current_phase.in_(active_owner_phases),
                )
                .order_by(Reconciliation.created_at, Reconciliation.id)
                .limit(1)
            )

    def _owns_publication(
        self,
        session: Session,
        reconciliation: Reconciliation,
        *,
        may_supersede: bool,
    ) -> bool:
        """Authorize marker access without transferring unacknowledged ownership."""

        owner = self._publication_owner(session)
        pending_id = self._pending_publication_handoff(session)
        if owner.reconciliation_id == reconciliation.id:
            return pending_id in {None, reconciliation.id}
        current = (
            None
            if owner.reconciliation_id is None
            else session.get(Reconciliation, owner.reconciliation_id)
        )
        if current is not None and current.current_phase not in {
            "completed",
            "failed",
            "cancelled",
            "waiting-for-operator",
        }:
            return False
        candidate_order = (_aware(reconciliation.created_at), reconciliation.id)
        current_order = (
            None if current is None else (_aware(current.created_at), current.id)
        )
        if current_order is not None and (
            not may_supersede or candidate_order <= current_order
        ):
            return False
        if not may_supersede:
            return False
        return pending_id in {None, reconciliation.id}

    def _publication_owner(self, session: Session) -> RoutePublicationOwner:
        statement = (
            select(RoutePublicationOwner)
            .where(RoutePublicationOwner.singleton_id == 1)
            .with_for_update(of=RoutePublicationOwner)
        )
        owner = session.scalar(statement)
        if owner is None:
            try:
                with session.begin_nested():
                    session.add(
                        RoutePublicationOwner(
                            singleton_id=1,
                            reconciliation_id=None,
                            owner_generation=0,
                        )
                    )
                    session.flush()
            except IntegrityError:
                pass
            owner = session.scalar(statement)
        if owner is None:
            raise RuntimeError("route publication owner is unavailable")
        if owner.reconciliation_id is None:
            latest_completed = session.scalar(
                select(Reconciliation.id)
                .where(
                    Reconciliation.status == "succeeded",
                    Reconciliation.current_phase == "completed",
                    Reconciliation.completion_generation.is_not(None),
                )
                .order_by(
                    Reconciliation.completion_generation.desc(),
                    Reconciliation.id.desc(),
                )
                .limit(1)
            )
            owner.reconciliation_id = latest_completed
        return owner

    @staticmethod
    def _pending_publication_handoff(session: Session) -> str | None:
        return session.scalar(
            select(Reconciliation.id)
            .join(
                RoutePublication,
                RoutePublication.reconciliation_id == Reconciliation.id,
            )
            .where(
                Reconciliation.current_phase == "withdrawal-pending",
                RoutePublication.state == "withdrawal-pending",
            )
            .order_by(Reconciliation.created_at.desc(), Reconciliation.id.desc())
            .limit(1)
        )

    def _transfer_publication_owner(
        self,
        session: Session,
        reconciliation: Reconciliation,
    ) -> None:
        owner = self._publication_owner(session)
        if owner.reconciliation_id == reconciliation.id:
            return
        if self._pending_publication_handoff(session) != reconciliation.id:
            raise ValueError("route publication handoff is no longer authoritative")
        current = (
            None
            if owner.reconciliation_id is None
            else session.get(Reconciliation, owner.reconciliation_id)
        )
        if current is not None and (
            _aware(reconciliation.created_at),
            reconciliation.id,
        ) <= (_aware(current.created_at), current.id):
            raise ValueError("route publication handoff is stale")
        owner.reconciliation_id = reconciliation.id
        owner.owner_generation += 1
        owner.updated_at = self._clock()

    def _locked_context(
        self,
        session: Session,
        reconciliation_id: str,
        *,
        expected_job_id: str | None = None,
    ) -> tuple[Reconciliation, Job, OperationGraph, Mapping[str, object]]:
        preview = session.get(Reconciliation, reconciliation_id)
        if preview is None:
            raise KeyError(reconciliation_id)
        graph, _document = self._validated_plan(preview)
        _locked_nodes = tuple(
            session.scalars(
                select(AgentNode)
                .where(AgentNode.node_id.in_(graph.targets))
                .order_by(AgentNode.node_id)
                .with_for_update(of=AgentNode)
            )
        )
        reconciliation = session.scalar(
            select(Reconciliation)
            .where(Reconciliation.id == reconciliation_id)
            .with_for_update(of=Reconciliation)
            .execution_options(populate_existing=True)
        )
        assert reconciliation is not None
        graph, document = self._validated_plan(reconciliation)
        job_query = select(Job).where(
            Job.id == expected_job_id
            if expected_job_id is not None
            else Job.reconciliation_id == reconciliation.id
        )
        job = session.scalar(job_query.with_for_update(of=Job))
        if job is None:
            raise ValueError("reconciliation has no durable parent job")
        if expected_job_id is None and job.reconciliation_id != reconciliation.id:
            raise ValueError("reconciliation parent link is invalid")
        return reconciliation, job, graph, document

    @staticmethod
    def _targets_are_active(session: Session, graph: OperationGraph) -> bool:
        nodes = list(
            session.scalars(
                select(AgentNode)
                .where(AgentNode.node_id.in_(graph.targets))
                .order_by(AgentNode.node_id)
            )
        )
        return [node.node_id for node in nodes] == list(graph.targets) and all(
            node.state == "active" and node.revoked_at is None for node in nodes
        )

    def _ensure_node_lease(
        self,
        session: Session,
        reconciliation: Reconciliation,
        graph: OperationGraph,
    ) -> NodeLeaseGrant | None:
        try:
            grant = self._node_leases.owned_grant_in_session(
                session,
                graph.targets,
                owner_kind="reconciliation",
                owner_id=reconciliation.id,
            )
            if grant is not None:
                return grant if grant.state == "held" else None
            return self._node_leases.acquire_in_session(
                session,
                graph.targets,
                owner_kind="reconciliation",
                owner_id=reconciliation.id,
            )
        except NodeLeaseConflict:
            # A terminal predecessor can leave its fenced lease in the
            # release-pending state while a newer publication handoff is
            # withdrawing its route.  Finalize only that explicit terminal
            # handoff, then acquire the successor lease; held or active
            # predecessors remain a hard conflict.
            rows = self._node_leases._rows(session, tuple(sorted(graph.targets)))
            if not rows:
                return None
            owners = {(row.owner_kind, row.owner_id, row.fence) for row in rows}
            if len(owners) != 1:
                return None
            owner_kind, owner_id, fence = next(iter(owners))
            if owner_kind != "reconciliation":
                return None
            predecessor = session.get(Reconciliation, owner_id)
            if predecessor is None or predecessor.current_phase not in {
                "completed",
                "failed",
                "cancelled",
                "waiting-for-operator",
            }:
                return None
            predecessor_cancel = self._cancellation(session, predecessor.id)
            if any(row.state == "held" for row in rows) and not (
                predecessor.current_phase == "completed"
                and predecessor_cancel is not None
                and predecessor_cancel.state == "withdrawal-pending"
            ):
                return None
            predecessor_grant = NodeLeaseGrant(
                owner_kind,
                owner_id,
                fence,
                tuple(sorted(graph.targets)),
                "held" if any(row.state == "held" for row in rows) else "releasing",
            )
            if predecessor_grant.state == "held":
                self._node_leases.mark_releasing_in_session(session, predecessor_grant)
            self._node_leases.release_in_session(
                session,
                NodeLeaseGrant(
                    owner_kind,
                    owner_id,
                    fence,
                    tuple(sorted(graph.targets)),
                    "releasing",
                ),
            )
            session.flush()
            try:
                return self._node_leases.acquire_in_session(
                    session,
                    graph.targets,
                    owner_kind="reconciliation",
                    owner_id=reconciliation.id,
                )
            except NodeLeaseConflict:
                return None

    def _release_pending_node_lease(
        self,
        session: Session,
        reconciliation: Reconciliation,
        graph: OperationGraph,
    ) -> bool:
        try:
            grant = self._node_leases.owned_grant_in_session(
                session,
                graph.targets,
                owner_kind="reconciliation",
                owner_id=reconciliation.id,
            )
            if grant is None or grant.state != "releasing":
                return False
            self._node_leases.release_in_session(session, grant)
            session.flush()
            return True
        except NodeLeaseConflict:
            return False

    def _mark_node_lease_releasing(
        self,
        session: Session,
        reconciliation: Reconciliation,
        graph: OperationGraph,
    ) -> bool:
        try:
            grant = self._node_leases.owned_grant_in_session(
                session,
                graph.targets,
                owner_kind="reconciliation",
                owner_id=reconciliation.id,
            )
        except NodeLeaseConflict:
            return False
        if grant is not None and grant.state == "held":
            self._node_leases.mark_releasing_in_session(session, grant)
            return True
        return False

    def _continuous_authority_reason(
        self,
        session: Session,
        reconciliation: Reconciliation,
        graph: OperationGraph,
        document: Mapping[str, object],
    ) -> str | None:
        if self._authority_check is not None:
            try:
                decision = self._authority_check(
                    reconciliation.id,
                    reconciliation.authority_revision,
                    self._plan_digest(reconciliation),
                    _published_authority_routes(
                        session,
                        document,
                        self._endpoint_resolver,
                    ),
                )
                if isinstance(decision, str):
                    if decision not in {
                        "fleet acceptance evidence changed since planning",
                        "reconciliation authority revision is no longer current",
                        "reconciliation authority revision is no longer eligible",
                    }:
                        return "reconciliation authority is invalid"
                    return decision
                if decision is not True:
                    return "reconciliation authority revision is no longer eligible"
            except (OSError, RuntimeError, TypeError, ValueError):
                return "reconciliation authority revision eligibility is unavailable"
        elif self._revision_eligible is None or self._current_revision is None:
            return None
        else:
            try:
                if not self._revision_eligible(reconciliation.authority_revision):
                    return "reconciliation authority revision is no longer eligible"
                if self._current_revision() != reconciliation.authority_revision:
                    return "reconciliation authority revision is no longer current"
            except (OSError, RuntimeError, TypeError, ValueError):
                return "reconciliation authority revision eligibility is unavailable"
        protocol = document.get("agent_protocol_range")
        if (
            not isinstance(protocol, list)
            or len(protocol) != 2
            or not all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in protocol
            )
        ):
            return "reconciliation protocol authority is invalid"
        nodes = list(
            session.scalars(
                select(AgentNode)
                .where(AgentNode.node_id.in_(graph.targets))
                .order_by(AgentNode.node_id)
            )
        )
        if [node.node_id for node in nodes] != list(graph.targets):
            return "reconciliation target set is unavailable"
        if any(
            node.state != "active"
            or node.revoked_at is not None
            or not isinstance(node.protocol_version, int)
            or isinstance(node.protocol_version, bool)
            or not protocol[0] <= node.protocol_version <= protocol[1]
            or not isinstance(node.capabilities, list)
            or not _REQUIRED_AGENT_CAPABILITIES
            <= set(node.capabilities)
            <= _NEXT_AGENT_CAPABILITIES
            for node in nodes
        ):
            return "reconciliation target agent is incompatible"
        try:
            for node_id in graph.targets:
                address, observed_at = self._endpoint_resolver(session, node_id)
                if (
                    not isinstance(address, str)
                    or not address
                    or not isinstance(observed_at, datetime)
                ):
                    return "reconciliation management address is invalid"
        except (OSError, RuntimeError, TypeError, ValueError):
            return "reconciliation management address is unavailable"
        return None

    @classmethod
    def _require_active_targets(cls, session: Session, graph: OperationGraph) -> None:
        if not cls._targets_are_active(session, graph):
            raise ValueError("reconciliation target agent is unavailable")

    @staticmethod
    def _validated_plan(
        reconciliation: Reconciliation,
    ) -> tuple[OperationGraph, Mapping[str, object]]:
        return validate_persisted_resolved_plan(
            reconciliation_id=reconciliation.id,
            authority_revision=reconciliation.authority_revision,
            graph_document=reconciliation.graph,
            graph_digest=reconciliation.graph_digest,
            plan_digest=reconciliation.plan_digest,
            resolved_document=reconciliation.resolved_plan,
            route_withdrawal_generation=reconciliation.route_withdrawal_generation,
        )

    @staticmethod
    def _plan_digest(reconciliation: Reconciliation) -> str:
        if not isinstance(reconciliation.plan_digest, str):
            raise TypeError("reconciliation lacks an immutable plan digest")
        return reconciliation.plan_digest

    @staticmethod
    def _publication(session: Session, reconciliation_id: str) -> RoutePublication:
        publication = session.scalar(
            select(RoutePublication)
            .where(RoutePublication.reconciliation_id == reconciliation_id)
            .with_for_update(of=RoutePublication)
        )
        if publication is None:
            raise ValueError("reconciliation route withdrawal is not durable")
        return publication

    @staticmethod
    def _cancellation(
        session: Session, reconciliation_id: str
    ) -> ReconciliationCancellation | None:
        return session.scalar(
            select(ReconciliationCancellation)
            .where(ReconciliationCancellation.reconciliation_id == reconciliation_id)
            .with_for_update(of=ReconciliationCancellation)
        )

    @staticmethod
    def _projections(
        session: Session, reconciliation_id: str, role: str
    ) -> list[ReconciliationOperation]:
        return list(
            session.scalars(
                select(ReconciliationOperation)
                .where(
                    ReconciliationOperation.reconciliation_id == reconciliation_id,
                    ReconciliationOperation.role == role,
                )
                .order_by(ReconciliationOperation.graph_operation_id)
                .with_for_update(of=ReconciliationOperation)
            )
        )

    def _dispatch_primary(
        self,
        session: Session,
        reconciliation: Reconciliation,
        job: Job,
        graph: OperationGraph,
        document: Mapping[str, object],
    ) -> bool:
        projections = self._projections(session, reconciliation.id, "primary")
        states = {row.graph_operation_id: row.state for row in projections}
        if len(projections) != len(graph.nodes):
            raise ValueError("reconciliation execution projection is incomplete")
        if all(state == "accepted" for state in states.values()):
            reconciliation.current_phase = "accepting"
            return False
        ready = ready_operation_ids(graph.nodes, states)
        by_id = {row.graph_operation_id: row for row in projections}
        for operation_id in ready:
            node = self._graph_node(graph, operation_id)
            payload = self._operation_payload(document, operation_id)
            agent_operation_id = str(uuid.uuid4())
            stored = self._agent_jobs.enqueue_in_session(
                session,
                job.id,
                node.node_id,
                node.kind,
                reconciliation.authority_revision,
                payload,
                operation_id=agent_operation_id,
            )
            projection = by_id[operation_id]
            projection.agent_operation_id = stored.id
            projection.state = "queued"
        return bool(ready)

    def _dispatch_compensation(
        self,
        session: Session,
        reconciliation: Reconciliation,
        job: Job,
        graph: OperationGraph,
        document: Mapping[str, object],
    ) -> bool:
        primary = self._projections(session, reconciliation.id, "primary")
        primary_states = {row.graph_operation_id: row.state for row in primary}
        ordered = compensation_order(graph.nodes, primary_states)
        existing = {
            row.graph_operation_id: row
            for row in self._projections(session, reconciliation.id, "compensation")
        }
        if not ordered:
            self._finish_failed(reconciliation, job)
            return False
        for operation_id in ordered:
            row = existing.get(operation_id)
            if row is not None:
                if row.state in {"queued", "running"}:
                    return False
                if row.state == "compensated":
                    continue
                if row.state in {"failed", "uncertain", "waiting-for-operator"}:
                    self._wait_for_operator(
                        reconciliation, job, "reconciliation compensation is incomplete"
                    )
                    return False
            node = self._graph_node(graph, operation_id)
            payload = self._compensation_payload(
                self._operation_payload(document, operation_id)
            )
            expected = _digest(payload)
            row = ReconciliationOperation(
                reconciliation_id=reconciliation.id,
                graph_operation_id=operation_id,
                role="compensation",
                expected_payload_digest=expected,
                state="planned",
                compensated_graph_operation_id=operation_id,
            )
            session.add(row)
            session.flush()
            agent_operation_id = str(uuid.uuid4())
            stored = self._agent_jobs.enqueue_in_session(
                session,
                job.id,
                node.node_id,
                node.compensation_kind,
                reconciliation.authority_revision,
                payload,
                operation_id=agent_operation_id,
            )
            row.agent_operation_id = stored.id
            row.state = "queued"
            return True
        self._finish_failed(reconciliation, job)
        return False

    def _publication_request(
        self,
        session: Session,
        reconciliation: Reconciliation,
        document: Mapping[str, object],
        publication: RoutePublication,
    ) -> RouteBundleRequest:
        if not isinstance(publication.evidence_digest, str):
            raise TypeError("accepted reconciliation evidence set is unavailable")
        routes = document.get("routes")
        if not isinstance(routes, Mapping) or not routes:
            raise ValueError("accepted reconciliation routes are unavailable")
        projections = self._projections(session, reconciliation.id, "primary")
        by_operation = {row.graph_operation_id: row for row in projections}
        endpoints: dict[str, AcceptedEndpointEvidence] = {}
        for raw in routes.values():
            if not isinstance(raw, Mapping):
                raise TypeError("accepted route is invalid")
            node_id = raw.get("entrypoint_node_id")
            workload_id = raw.get("workload_id")
            if not isinstance(node_id, str) or not isinstance(workload_id, str):
                raise TypeError("accepted route entrypoint is invalid")
            operation_id = f"{workload_id}:{node_id}:workload.verify"
            projection = by_operation.get(operation_id)
            if (
                projection is None
                or projection.state != "accepted"
                or not isinstance(projection.evidence_digest, str)
            ):
                raise ValueError("accepted route lacks exact verify evidence")
            address, observed_at = self._endpoint_resolver(session, node_id)
            endpoint_digest = endpoint_evidence_digest(
                node_id=node_id,
                address=address,
                observed_at=observed_at,
                operation_id=operation_id,
                verify_evidence_digest=projection.evidence_digest,
            )
            endpoints[node_id] = AcceptedEndpointEvidence(
                node_id,
                address,
                observed_at,
                operation_id,
                projection.evidence_digest,
                endpoint_digest,
            )
        return RouteBundleRequest(
            reconciliation.id,
            self._plan_digest(reconciliation),
            publication.evidence_digest,
            routes,
            endpoints,
            self._clock() + timedelta(seconds=self._publication_lease_seconds),
            reconciliation.authority_revision,
        )

    @staticmethod
    def _accepted_evidence_digest(
        projections: Sequence[ReconciliationOperation],
    ) -> str:
        if not projections or any(
            row.state != "accepted"
            or not isinstance(row.result_digest, str)
            or not isinstance(row.evidence_digest, str)
            for row in projections
        ):
            raise ValueError("reconciliation operation evidence is incomplete")
        return _digest(
            [
                {
                    "operation_id": row.graph_operation_id,
                    "result_digest": row.result_digest,
                    "evidence_digest": row.evidence_digest,
                }
                for row in sorted(projections, key=lambda item: item.graph_operation_id)
            ]
        )

    @staticmethod
    def _store_marker(
        publication: RoutePublication, marker: ActivationMarker, state: str
    ) -> None:
        document = asdict(marker)
        publication.state = state
        publication.generation = marker.generation
        publication.plan_digest = marker.plan_digest
        publication.evidence_digest = marker.evidence_set_digest
        publication.route_digest = marker.routes_sha256
        publication.litellm_digest = marker.litellm_sha256
        publication.bundle_digest = marker.manifest_sha256
        publication.activation_marker = document
        publication.activation_marker_digest = marker.digest
        publication.lease_issued_at = datetime.fromisoformat(marker.issued_at)
        publication.lease_expires_at = datetime.fromisoformat(marker.expires_at)

    @staticmethod
    def _operation_payload(
        document: Mapping[str, object], operation_id: str
    ) -> Mapping[str, object]:
        payloads = document.get("operation_payloads")
        if not isinstance(payloads, Mapping):
            raise TypeError("reconciliation operation payloads are invalid")
        payload = payloads.get(operation_id)
        if not isinstance(payload, Mapping):
            raise TypeError("reconciliation operation payload is invalid")
        return payload

    @staticmethod
    def _graph_node(graph: OperationGraph, operation_id: str) -> OperationNode:
        for node in graph.nodes:
            if node.operation_id == operation_id:
                return node
        raise ValueError("execution projection operation is absent from the graph")

    @staticmethod
    def _compensation_payload(
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        required = {"schema_version", "workload_id", "release_digest", "adapter_id"}
        if not required.issubset(payload):
            raise ValueError("workload compensation payload is incomplete")
        return {key: payload[key] for key in sorted(required)}

    @staticmethod
    def _validate_operation_binding(
        operation: StoredAgentOperation,
        attempt: AgentOperationAttempt,
        message: AgentResult,
        reconciliation: Reconciliation,
        job: Job,
        projection: ReconciliationOperation,
        node: OperationNode,
        kind: str,
        payload: Mapping[str, object],
    ) -> None:
        if (
            projection.agent_operation_id != operation.id
            or projection.expected_payload_digest != _digest(payload)
            or operation.parent_job_id != job.id
            or operation.node_id != node.node_id
            or operation.kind != kind
            or operation.authority_revision != reconciliation.authority_revision
            or operation.payload_digest != projection.expected_payload_digest
            or operation.payload != payload
            or message.job_id != job.id
            or message.operation_id != operation.id
            or message.node_id != operation.node_id
            or message.attempt != attempt.attempt
            or message.fence != attempt.fence
            or message.state not in {"succeeded", "failed", "waiting-for-operator"}
        ):
            raise ValueError("agent result does not match its reconciliation operation")

    def _handle_primary_failure(
        self,
        session: Session,
        reconciliation: Reconciliation,
        job: Job,
        graph: OperationGraph,
        failed_node: OperationNode,
        reason: str,
    ) -> None:
        projections = self._projections(session, reconciliation.id, "primary")
        if self._quiesce_pending(session, reconciliation, graph, projections):
            self._wait_for_operator(
                reconciliation,
                job,
                "a sibling mutation was running when reconciliation failed",
            )
            return
        accepted = {row.graph_operation_id: row.state for row in projections}
        compensatable = compensation_order(graph.nodes, accepted)
        if (
            failed_node.kind
            in {
                "workload.start",
                "workload.health",
                "workload.verify",
            }
            and compensatable
        ):
            reconciliation.current_phase = "compensating"
            reconciliation.status = "running"
            reconciliation.terminal_reason = reason
        elif failed_node.kind in _MUTATIONS:
            self._wait_for_operator(reconciliation, job, reason)
        else:
            reconciliation.current_phase = "failed"
            reconciliation.status = "failed"
            reconciliation.terminal_reason = reason
            job.state = "failed"
            job.status_reason = reason
            job.updated_at = self._clock()

    def _quiesce_pending(
        self,
        session: Session,
        reconciliation: Reconciliation,
        graph: OperationGraph,
        projections: Sequence[ReconciliationOperation],
    ) -> bool:
        """Fence unresolved work while target Node locks serialize agent traffic."""

        uncertain_mutation = False
        now = self._clock()
        for projection in projections:
            if projection.state == "planned":
                projection.state = "failed"
                continue
            if projection.state != "queued" or projection.agent_operation_id is None:
                continue
            operation = session.scalar(
                select(StoredAgentOperation)
                .where(StoredAgentOperation.id == projection.agent_operation_id)
                .with_for_update(of=StoredAgentOperation)
            )
            if operation is None:
                raise ValueError("reconciliation operation projection is incomplete")
            node = self._graph_node(graph, projection.graph_operation_id)
            if operation.state == "queued":
                operation.state = "failed"
                operation.updated_at = now
                projection.state = "failed"
                continue
            if operation.state != "running":
                continue
            attempt = session.scalar(
                select(AgentOperationAttempt)
                .where(
                    AgentOperationAttempt.operation_id == operation.id,
                    AgentOperationAttempt.attempt == operation.current_attempt,
                )
                .with_for_update(of=AgentOperationAttempt)
            )
            if attempt is None or attempt.state != "running":
                raise ValueError("running reconciliation operation lacks its attempt")
            if node.kind in _MUTATIONS:
                operation.state = "waiting-for-operator"
                attempt.state = "waiting-for-operator"
                projection.state = "waiting-for-operator"
                uncertain_mutation = True
            else:
                operation.state = "failed"
                attempt.state = "failed"
                projection.state = "failed"
            operation.updated_at = now
        return uncertain_mutation

    def _sweep_expired_mutations(
        self,
        session: Session,
        reconciliation: Reconciliation,
        job: Job,
        graph: OperationGraph,
    ) -> bool:
        """Project expired mutation leases without requiring another claim."""

        primary = self._projections(session, reconciliation.id, "primary")
        compensation = self._projections(session, reconciliation.id, "compensation")
        now = self._clock()
        expired = False
        for projection in (*primary, *compensation):
            if projection.state != "queued" or projection.agent_operation_id is None:
                continue
            operation = session.scalar(
                select(StoredAgentOperation)
                .where(StoredAgentOperation.id == projection.agent_operation_id)
                .with_for_update(of=StoredAgentOperation)
            )
            if (
                operation is None
                or operation.state != "running"
                or operation.kind not in _MUTATIONS
            ):
                continue
            attempt = session.scalar(
                select(AgentOperationAttempt)
                .where(
                    AgentOperationAttempt.operation_id == operation.id,
                    AgentOperationAttempt.attempt == operation.current_attempt,
                )
                .with_for_update(of=AgentOperationAttempt)
            )
            if (
                attempt is None
                or attempt.state != "running"
                or _aware(attempt.lease_deadline) > _aware(now)
            ):
                continue
            attempt.state = "expired"
            operation.state = "waiting-for-operator"
            operation.retry_disposition = None
            operation.retry_disposition_attempt = None
            operation.updated_at = now
            projection.state = "waiting-for-operator"
            expired = True
        if not expired:
            return False
        self._quiesce_pending(session, reconciliation, graph, primary)
        self._quiesce_pending(session, reconciliation, graph, compensation)
        self._wait_for_operator(
            reconciliation,
            job,
            "mutating agent operation lease expired with uncertain outcome",
        )
        return True

    def _quiesce_for_unavailable_target(
        self,
        session: Session,
        reconciliation: Reconciliation,
        job: Job,
        graph: OperationGraph,
        reason: str,
    ) -> None:
        self._quiesce_pending(
            session,
            reconciliation,
            graph,
            self._projections(session, reconciliation.id, "primary"),
        )
        self._quiesce_pending(
            session,
            reconciliation,
            graph,
            self._projections(session, reconciliation.id, "compensation"),
        )
        self._wait_for_operator(reconciliation, job, reason)
        self._mark_node_lease_releasing(session, reconciliation, graph)

    def _finish_failed(self, reconciliation: Reconciliation, job: Job) -> None:
        reconciliation.current_phase = "failed"
        reconciliation.status = "failed"
        job.state = "failed"
        job.status_reason = reconciliation.terminal_reason or "reconciliation failed"
        job.updated_at = self._clock()

    def _wait_for_operator(
        self, reconciliation: Reconciliation, job: Job, reason: str
    ) -> None:
        safe_reason = self._safe_reason(reason)
        reconciliation.current_phase = "waiting-for-operator"
        reconciliation.status = "failed"
        reconciliation.terminal_reason = safe_reason
        job.state = "waiting-for-operator"
        job.status_reason = safe_reason
        job.updated_at = self._clock()

    @staticmethod
    def _safe_reason(reason: str) -> str:
        return redact_text(reason)[:1024]

    @classmethod
    def _result_reason(cls, message: AgentResult) -> str:
        reason = message.result.get("reason")
        if not isinstance(reason, str):
            reason = message.result.get("error_code")
        return cls._safe_reason(
            reason if isinstance(reason, str) and reason else "agent operation failed"
        )


def bind_reconciliation_result_consumer(
    sessions: sessionmaker[Session],
    *,
    operations: Any,
    presence: Any,
    clock: Callable[[], datetime],
    maximum_presence_age_seconds: int = 300,
    revision_eligible: Callable[[str], bool] | None = None,
    current_revision: Callable[[], str] | None = None,
    additional_result_consumer: Callable[[Session, Any, Any, AgentResult], None]
    | None = None,
) -> AgentReconciliationService:
    """Bind the API's result queue to the same durable execution projection."""

    if not 1 <= maximum_presence_age_seconds <= 300:
        raise ValueError("reconciliation presence age is invalid")

    def endpoint(session: Session, node_id: str) -> tuple[str, datetime]:
        observation = presence.latest_in_session(
            session,
            node_id,
            maximum_age_seconds=maximum_presence_age_seconds,
        )
        return observation.address, observation.observed_at

    service = AgentReconciliationService(
        sessions,
        agent_jobs=operations,
        publisher=None,
        endpoint_resolver=endpoint,
        clock=clock,
        revision_eligible=revision_eligible,
        current_revision=current_revision,
    )
    if additional_result_consumer is None:
        operations.set_result_consumer(service.consume_result)
    else:
        if not callable(additional_result_consumer):
            raise TypeError("additional agent result consumer must be callable")

        def consume(session, operation, attempt, message) -> None:
            service.consume_result(session, operation, attempt, message)
            additional_result_consumer(session, operation, attempt, message)

        operations.set_result_consumer(consume)
    return service
