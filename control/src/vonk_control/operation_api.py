"""Strict, secret-free representations for routine administrative operations."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import canonical_message

from .auth import CursorCodec
from .models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Job,
    Reconciliation,
    ReconciliationOperation,
    RoutePublication,
    RoutePublicationOwner,
)
from .route_runtime import verify_active_route_bundle

COMMIT_PATTERN = r"^[0-9a-f]{40}$"
DIGEST_PATTERN = r"^[0-9a-f]{64}$"
IDENTIFIER_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,62}$"
NODE_PATTERN = r"^spk_[0-9a-f]{32}$"
_ACTIVE_PUBLICATION_STATES = frozenset({"completed"})
_ADMIN_OPERATION_IDS = {
    ("post", "/api/v1/agents/enrollments/grants"): "createEnrollmentGrant",
    ("post", "/api/v1/agents/nodes/{node_id}/migration-grant"): "createAgentMigrationGrant",
    ("get", "/api/v1/agents/enrollments"): "listAgentEnrollments",
    ("post", "/api/v1/agents/enrollments/{enrollment_id}/approve"): "approveAgentEnrollment",
    ("post", "/api/v1/agents/enrollments/{enrollment_id}/reject"): "rejectAgentEnrollment",
    ("post", "/api/v1/agents/nodes/{node_id}/revoke"): "revokeAgentNode",
    ("get", "/api/v1/fleet"): "getFleetStatus",
    ("get", "/api/v1/fleet/stream"): "streamFleetEvents",
    ("get", "/api/v1/library"): "listLibrary",
    (
        "get",
        "/api/v1/library/recipes/{recipe_id}",
    ): "getLibraryRecipe",
    ("get", "/api/v1/nodes/status"): "getNodeStatuses",
    (
        "get",
        "/api/v1/nodes/{node_id}/telemetry",
    ): "getNodeTelemetryHistory",
    ("get", "/api/v1/endpoints/{alias}"): "getPublishedEndpoint",
    ("get", "/api/v1/agents"): "listAgents",
    ("get", "/api/v1/repository"): "getRepository",
    ("post", "/api/v1/proposals"): "previewProposal",
    ("post", "/api/v1/changes"): "submitChange",
    ("get", "/api/v1/jobs"): "listJobs",
    ("get", "/api/v1/audit"): "listAuditEvents",
    ("get", "/api/v1/jobs/{job_id}"): "getJob",
    ("post", "/api/v1/jobs/{job_id}/resume"): "resumeJob",
    ("get", "/api/v1/jobs/{job_id}/logs"): "listJobLogs",
    ("get", "/api/v1/jobs/{job_id}/logs/{digest}"): "getJobLog",
    ("get", "/api/v1/updates/skew"): "getPlatformUpdateSkew",
    ("post", "/api/v1/updates/plan"): "planPlatformUpdate",
    ("post", "/api/v1/updates"): "applyPlatformUpdate",
    ("get", "/api/v1/updates/{rollout_id}"): "getPlatformUpdate",
    (
        "post",
        "/api/v1/updates/{rollout_id}/approve-resume",
    ): "approvePlatformUpdateRecovery",
    ("get", "/api/v1/catalog/recipes"): "listLocalRecipes",
    ("post", "/api/v1/catalog/recipes"): "createLocalRecipe",
    ("get", "/api/v1/catalog/recipes/{recipe_id}"): "getLocalRecipe",
    ("put", "/api/v1/catalog/recipes/{recipe_id}/draft"): "updateLocalRecipeDraft",
    ("post", "/api/v1/catalog/recipes/{recipe_id}/resolve"): "resolveLocalRecipe",
    ("post", "/api/v1/catalog/recipes/{recipe_id}/fork"): "forkLocalRecipe",
    ("post", "/api/v1/catalog/imports/workload_run/preview"): "previewWorkloadRunImport",
    ("post", "/api/v1/catalog/imports/workload_run"): "applyWorkloadRunImport",
    ("post", "/api/v1/catalog/recipes/{recipe_id}/resolve-import"): "resolveWorkloadRunImport",
}
_HTTP_METHODS = frozenset({"delete", "get", "patch", "post", "put"})
BoundedIdentifier = Annotated[str, Field(min_length=1, max_length=128)]
NodeIdentifier = Annotated[str, Field(pattern=NODE_PATTERN)]


class OperationProjectionError(RuntimeError):
    """Durable operation state cannot be safely projected."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyBody(StrictModel):
    """A body type used only where an explicit empty JSON object is allowed."""


class BoundedErrorResponse(StrictModel):
    detail: str = Field(min_length=1, max_length=256)


def bounded_error_responses(*status_codes: int) -> dict[int, dict[str, object]]:
    """Describe stable JSON errors for generated clients."""

    return {
        status_code: {"model": BoundedErrorResponse}
        for status_code in status_codes
    }


class EndpointResponse(StrictModel):
    alias: str = Field(pattern=IDENTIFIER_PATTERN)
    api_base: str
    expires_at: str
    generation: int = Field(ge=1)
    node_id: str = Field(pattern=NODE_PATTERN)
    observed_at: str
    plan_digest: str = Field(pattern=DIGEST_PATTERN)
    state: str = Field(pattern=r"^published$")


class AgentSummary(StrictModel):
    node_id: str = Field(pattern=NODE_PATTERN)
    state: str
    agent_implementation: str = Field(pattern=r"^(pending|python|rust)$")
    migration_state: str = Field(pattern=r"^(required|complete)$")
    protocol_version: int | None = Field(default=None, ge=1)
    platform_version: str | None = Field(
        default=None,
        pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$",
    )
    build_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    active_slot: str | None = Field(default=None, pattern=r"^[AB]$")
    agent_sha256: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    supervisor_generation: int | None = Field(
        default=None, ge=1, le=999_999_999, strict=True
    )
    capabilities: list[str]
    last_seen_at: str | None
    last_seen_age_seconds: float | None = Field(default=None, ge=0)
    stale: bool
    certificate_expires_at: str | None


class AgentsResponse(StrictModel):
    agents: list[AgentSummary]


class JobOperationProgress(StrictModel):
    phase: str = Field(min_length=1, max_length=80)


class JobOperationResponse(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    graph_operation_id: str | None = Field(default=None, max_length=128)
    node_id: str = Field(pattern=NODE_PATTERN)
    kind: str = Field(min_length=1, max_length=80)
    state: str = Field(min_length=1, max_length=80)
    attempt: int = Field(ge=0)
    progress: JobOperationProgress | None = None
    updated_at: str | None = None


class JobProgress(StrictModel):
    completed: int = Field(ge=0)
    failed: int = Field(ge=0)
    running: int = Field(ge=0)
    total: int = Field(ge=0)


class JobDetailResponse(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    state: str = Field(min_length=1, max_length=80)
    kind: str = Field(min_length=1, max_length=80)
    base_commit: str = Field(min_length=1, max_length=128)
    targets: list[BoundedIdentifier] = Field(max_length=100)
    target_next_cursor: str | None = Field(default=None, max_length=512)
    target_total: int = Field(ge=0)
    current_attempt: int = Field(ge=0)
    status_reason: str | None = Field(default=None, max_length=1024)
    reconciliation_id: str | None = Field(default=None, max_length=128)
    operations: list[JobOperationResponse] = Field(max_length=100)
    operation_next_cursor: str | None = Field(default=None, max_length=512)
    operation_total: int = Field(ge=0)
    progress: JobProgress


class JobResumeResponse(StrictModel):
    id: str
    state: str = Field(pattern=r"^queued$")


class JobSummary(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    state: str = Field(min_length=1, max_length=80)
    kind: str = Field(min_length=1, max_length=80)


class JobsResponse(StrictModel):
    jobs: list[JobSummary] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=512)
    total: int = Field(ge=0)


class JobLogsResponse(StrictModel):
    job_id: str
    digests: list[str]


@dataclass(frozen=True)
class OperationApiServices:
    """Optional projections backed by accepted durable control state only."""

    endpoint: Callable[[str], Mapping[str, object]]
    agents: Callable[[], Sequence[Mapping[str, object]]]
    job_operations: Callable[[str, str | None, int], OperationPage]
    resume_job: Callable[[str], None]


@dataclass(frozen=True)
class OperationPage:
    items: Sequence[Mapping[str, object]]
    next_cursor: str | None
    progress: JobProgress


def job_response(
    job: Any,
    operation_page: OperationPage,
    *,
    target_cursor: int,
    limit: int,
    cursors: CursorCodec,
) -> JobDetailResponse:
    projected = [
        JobOperationResponse(
            id=str(item["id"]),
            graph_operation_id=(
                None
                if item.get("graph_operation_id") is None
                else str(item["graph_operation_id"])
            ),
            node_id=str(item["node_id"]),
            kind=str(item["kind"]),
            state=str(item["state"]),
            attempt=int(item["attempt"]),
            progress=_progress_projection(item.get("progress")),
            updated_at=(
                None
                if item.get("updated_at") is None
                else str(item["updated_at"])
            ),
        )
        for item in operation_page.items
    ]
    targets = list(job.targets)
    visible_targets = targets[target_cursor : target_cursor + limit]
    target_next_cursor = (
        _encode_offset(
            target_cursor + limit,
            job_id=str(job.id),
            cursors=cursors,
        )
        if target_cursor + limit < len(targets)
        else None
    )
    return JobDetailResponse(
        id=str(job.id),
        state=str(job.state),
        kind=str(job.kind),
        base_commit=str(job.base_commit),
        targets=visible_targets,
        target_next_cursor=target_next_cursor,
        target_total=len(targets),
        current_attempt=int(job.current_attempt),
        status_reason=job.status_reason,
        reconciliation_id=job.reconciliation_id,
        operations=projected,
        operation_next_cursor=operation_page.next_cursor,
        operation_total=operation_page.progress.total,
        progress=operation_page.progress,
    )


def _encode_offset(offset: int, *, job_id: str, cursors: CursorCodec) -> str:
    return cursors.encode(
        resource="job-targets",
        order="index-asc/v1",
        context={"job_id": job_id},
        boundary=offset,
    )


def decode_offset(
    cursor: str | None,
    *,
    job_id: str,
    cursors: CursorCodec,
) -> int:
    if cursor is None:
        return 0
    try:
        offset = cursors.decode(
            cursor,
            resource="job-targets",
            order="index-asc/v1",
            context={"job_id": job_id},
        )
    except (UnicodeError, ValueError):
        raise ValueError("target cursor is invalid") from None
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("target cursor is invalid")
    return offset


def _progress_projection(value: object) -> JobOperationProgress | None:
    if not isinstance(value, Mapping):
        return None
    phase = value.get("phase")
    if not isinstance(phase, str) or not phase.strip() or len(phase) > 80:
        return None
    return JobOperationProgress(phase=phase)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class _DurableOperationProjection:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        route_root: Path,
        *,
        clock: Callable[[], datetime],
        stale_after_seconds: int,
        cursors: CursorCodec,
    ) -> None:
        if route_root.is_symlink() or stale_after_seconds <= 0:
            raise ValueError("operation projection configuration is invalid")
        self._sessions = sessions
        self._route_root = route_root
        self._clock = clock
        self._stale_after_seconds = stale_after_seconds
        self._cursors = cursors

    def endpoint(self, alias: str) -> Mapping[str, object]:
        with self._sessions() as session:
            owner = session.get(RoutePublicationOwner, 1)
            publication = (
                None
                if owner is None or owner.reconciliation_id is None
                else session.get(RoutePublication, owner.reconciliation_id)
            )
            reconciliation = (
                None
                if owner is None or owner.reconciliation_id is None
                else session.get(Reconciliation, owner.reconciliation_id)
            )
            if (
                owner is None
                or publication is None
                or reconciliation is None
                or publication.state not in _ACTIVE_PUBLICATION_STATES
                or reconciliation.status != "succeeded"
                or reconciliation.current_phase != "completed"
                or publication.generation != owner.owner_generation
                or publication.activation_marker is None
                or publication.activation_marker_digest is None
                or publication.route_digest is None
                or publication.lease_expires_at is None
                or _aware(publication.lease_expires_at) <= _aware(self._clock())
            ):
                raise RuntimeError("active publication is unavailable")
            marker = dict(publication.activation_marker)
            marker_digest = publication.activation_marker_digest
            route_digest = publication.route_digest
            evidence_digest = publication.evidence_digest
            litellm_digest = publication.litellm_digest
            bundle_digest = publication.bundle_digest
            lease_issued_at = publication.lease_issued_at
            lease_expires_at = publication.lease_expires_at
            owner_reconciliation_id = owner.reconciliation_id
            owner_generation = owner.owner_generation
            publication_generation = publication.generation
            publication_plan_digest = publication.plan_digest

        bundle = verify_active_route_bundle(
            self._route_root,
            clock=self._clock,
        )
        active_marker = bundle.marker
        if (
            asdict(active_marker) != marker
            or active_marker.digest != marker_digest
            or active_marker.state != "published"
            or active_marker.reconciliation_id != owner_reconciliation_id
            or active_marker.plan_digest != publication_plan_digest
            or active_marker.generation != publication_generation
            or active_marker.generation != owner_generation
            or active_marker.evidence_set_digest != evidence_digest
            or active_marker.routes_sha256 != route_digest
            or active_marker.litellm_sha256 != litellm_digest
            or active_marker.manifest_sha256 != bundle_digest
            or lease_issued_at is None
            or lease_expires_at is None
            or _aware(lease_issued_at)
            != _aware(datetime.fromisoformat(active_marker.issued_at))
            or _aware(lease_expires_at)
            != _aware(datetime.fromisoformat(active_marker.expires_at))
        ):
            raise RuntimeError("activation marker does not match durable state")
        routes = bundle.routes
        if (
            routes.get("generation") != publication_generation
            or routes.get("state") != "published"
            or not isinstance(routes.get("routes"), Mapping)
        ):
            raise RuntimeError("active route state does not match publication")
        raw = routes["routes"].get(alias)
        if raw is None:
            raise KeyError(alias)
        if not isinstance(raw, Mapping):
            raise OperationProjectionError("active endpoint is invalid")
        scheme = raw.get("scheme")
        address = raw.get("address")
        port = raw.get("port")
        path = raw.get("path")
        node_id = raw.get("node_id")
        observed_at = raw.get("observed_at")
        if (
            scheme not in {"http", "https"}
            or not isinstance(address, str)
            or not isinstance(port, int)
            or isinstance(port, bool)
            or not 1 <= port <= 65535
            or not isinstance(path, str)
            or not path.startswith("/")
            or not isinstance(node_id, str)
            or re.fullmatch(NODE_PATTERN, node_id) is None
            or not isinstance(observed_at, str)
        ):
            raise RuntimeError("active endpoint is invalid")
        return {
            "alias": alias,
            "api_base": f"{scheme}://{address}:{port}{path.rstrip('/')}",
            "expires_at": active_marker.expires_at,
            "generation": active_marker.generation,
            "node_id": node_id,
            "observed_at": observed_at,
            "plan_digest": active_marker.plan_digest,
            "state": "published",
        }

    def agents(self) -> Sequence[Mapping[str, object]]:
        now = _aware(self._clock())
        with self._sessions() as session:
            nodes = list(
                session.scalars(select(AgentNode).order_by(AgentNode.node_id).limit(500))
            )
            certificates = list(
                session.scalars(
                    select(AgentCertificate)
                    .where(
                        AgentCertificate.state == "active",
                        AgentCertificate.revoked_at.is_(None),
                    )
                    .order_by(
                        AgentCertificate.node_id,
                        AgentCertificate.not_after.desc(),
                        AgentCertificate.generation.desc(),
                    )
                )
            )
        latest_certificates: dict[str, AgentCertificate] = {}
        for certificate in certificates:
            latest_certificates.setdefault(certificate.node_id, certificate)
        projected: list[Mapping[str, object]] = []
        for node in nodes:
            last_seen = None if node.last_seen_at is None else _aware(node.last_seen_at)
            age = None if last_seen is None else max(0.0, (now - last_seen).total_seconds())
            certificate = latest_certificates.get(node.node_id)
            not_after = (
                None if certificate is None else _aware(certificate.not_after)
            )
            projected.append(
                {
                    "capabilities": [
                        capability[:80]
                        for capability in node.capabilities[:64]
                        if isinstance(capability, str)
                    ],
                    "certificate_expires_at": (
                        None if not_after is None else not_after.isoformat()
                    ),
                    "last_seen_age_seconds": age,
                    "last_seen_at": None if last_seen is None else last_seen.isoformat(),
                    "node_id": node.node_id,
                    "agent_implementation": node.agent_implementation,
                    "migration_state": node.migration_state,
                    "protocol_version": node.protocol_version,
                    "platform_version": node.platform_version,
                    "build_digest": node.build_digest,
                    "active_slot": node.active_slot,
                    "agent_sha256": node.agent_sha256,
                    "supervisor_generation": node.supervisor_generation,
                    "stale": age is None or age > self._stale_after_seconds,
                    "state": node.state,
                }
            )
        return projected

    def job_operations(
        self, job_id: str, cursor: str | None, limit: int
    ) -> OperationPage:
        if not 1 <= limit <= 100:
            raise ValueError("operation page limit is invalid")
        boundary: tuple[datetime, str] | None = None
        if cursor is not None:
            try:
                decoded = self._cursors.decode(
                    cursor,
                    resource="job-operations",
                    order="created-at-asc/id-asc/v1",
                    context={"job_id": job_id},
                )
                if (
                    not isinstance(decoded, list)
                    or len(decoded) != 2
                    or not all(isinstance(item, str) for item in decoded)
                ):
                    raise ValueError
                boundary = (datetime.fromisoformat(decoded[0]), decoded[1])
            except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
                raise ValueError("operation cursor is invalid") from None
        with self._sessions() as session:
            statement = select(AgentOperation).where(
                AgentOperation.parent_job_id == job_id
            )
            if boundary is not None:
                created_at, operation_id = boundary
                statement = statement.where(
                    or_(
                        AgentOperation.created_at > created_at,
                        (AgentOperation.created_at == created_at)
                        & (AgentOperation.id > operation_id),
                    )
                )
            operations = list(
                session.scalars(
                    statement
                    .order_by(AgentOperation.created_at, AgentOperation.id)
                    .limit(limit + 1)
                )
            )
            has_more = len(operations) > limit
            operations = operations[:limit]
            state_counts = {
                str(state): int(count)
                for state, count in session.execute(
                    select(AgentOperation.state, func.count())
                    .where(AgentOperation.parent_job_id == job_id)
                    .group_by(AgentOperation.state)
                )
            }
            graph_ids = {
                row.agent_operation_id: row.graph_operation_id
                for row in session.scalars(
                    select(ReconciliationOperation).where(
                        ReconciliationOperation.agent_operation_id.in_(
                            [operation.id for operation in operations]
                        )
                    )
                )
                if row.agent_operation_id is not None
            }
            attempts = {
                attempt.operation_id: attempt
                for attempt in session.scalars(
                    select(AgentOperationAttempt).where(
                        AgentOperationAttempt.operation_id.in_(
                            [operation.id for operation in operations]
                        )
                    )
                )
                if any(
                    operation.id == attempt.operation_id
                    and operation.current_attempt == attempt.attempt
                    for operation in operations
                )
            }
        items = [
            {
                "attempt": operation.current_attempt,
                "graph_operation_id": graph_ids.get(operation.id),
                "id": operation.id,
                "kind": operation.kind,
                "node_id": operation.node_id,
                "progress": (
                    None
                    if attempts.get(operation.id) is None
                    else (
                        None
                        if _progress_projection(
                            attempts[operation.id].progress
                        )
                        is None
                        else _progress_projection(
                            attempts[operation.id].progress
                        ).model_dump(mode="json")
                    )
                ),
                "state": operation.state,
                "updated_at": _aware(operation.updated_at).isoformat(),
            }
            for operation in operations
        ]
        next_cursor = None
        if has_more and operations:
            last = operations[-1]
            next_cursor = self._cursors.encode(
                resource="job-operations",
                order="created-at-asc/id-asc/v1",
                context={"job_id": job_id},
                boundary=[_aware(last.created_at).isoformat(), last.id],
            )
        terminal = {"succeeded", "accepted", "compensated"}
        failed = {"failed", "uncertain"}
        running = {"queued", "running", "planned", "compensating"}
        return OperationPage(
            items=items,
            next_cursor=next_cursor,
            progress=JobProgress(
                completed=sum(state_counts.get(state, 0) for state in terminal),
                failed=sum(state_counts.get(state, 0) for state in failed),
                running=sum(state_counts.get(state, 0) for state in running),
                total=sum(state_counts.values()),
            ),
        )

    def resume_job(self, job_id: str) -> None:
        with self._sessions.begin() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            if job.state != "waiting-for-operator":
                raise ValueError("job is not waiting for operator")
            now = self._clock()
            result = session.execute(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.state == "waiting-for-operator",
                )
                .values(
                    state="queued",
                    status_reason=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount == 1:
                job.state = "queued"
                job.status_reason = None
                job.updated_at = now
                return
            raise ValueError("job is not waiting for operator")


def durable_operation_services(
    sessions: sessionmaker[Session],
    route_root: Path,
    *,
    clock: Callable[[], datetime],
    cursors: CursorCodec,
    stale_after_seconds: int = 150,
) -> OperationApiServices:
    """Build bounded projections over database state and the active route bundle."""

    projection = _DurableOperationProjection(
        sessions,
        route_root,
        clock=clock,
        stale_after_seconds=stale_after_seconds,
        cursors=cursors,
    )
    return OperationApiServices(
        endpoint=projection.endpoint,
        agents=projection.agents,
        job_operations=projection.job_operations,
        resume_job=projection.resume_job,
    )


def admin_openapi_schema(app: Any) -> dict[str, object]:
    """Return the deterministic authenticated admin surface without agent APIs."""

    source = deepcopy(app.openapi())
    paths: dict[str, object] = {}
    browser_auth_paths = {
        "/api/v1/auth/login",
        "/api/v1/auth/logout",
        "/api/v1/auth/session",
        "/api/v1/fleet/stream",
    }
    includes_browser_auth = any(
        path in source.get("paths", {}) for path in browser_auth_paths
    )
    for path, path_item in source.get("paths", {}).items():
        if path in {"/api/v1/healthz", "/api/v1/readyz"}:
            continue
        if not path.startswith("/api/v1/"):
            continue
        selected = deepcopy(path_item)
        for method, operation in selected.items():
            if method not in _HTTP_METHODS:
                continue
            try:
                operation["operationId"] = _ADMIN_OPERATION_IDS[(method, path)]
            except KeyError as error:
                raise RuntimeError(
                    f"admin operation ID is not explicit for {method.upper()} {path}"
                ) from error
            if path == "/api/v1/auth/login":
                operation["security"] = []
            elif path in browser_auth_paths:
                operation["security"] = [{"BrowserSession": []}]
            else:
                operation["security"] = [{"BearerAuth": []}]
        paths[path] = selected
    source["paths"] = paths
    components = source.setdefault("components", {})
    components["securitySchemes"] = {
        "BearerAuth": {"scheme": "bearer", "type": "http"}
    }
    if includes_browser_auth:
        components["securitySchemes"]["BrowserSession"] = {
            "in": "cookie",
            "name": "vonk_session",
            "type": "apiKey",
        }

    referenced: set[str] = set()

    def collect(value: object) -> None:
        if isinstance(value, Mapping):
            reference = value.get("$ref")
            if isinstance(reference, str) and reference.startswith(
                "#/components/schemas/"
            ):
                referenced.add(reference.rsplit("/", 1)[-1])
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(paths)
    schemas = components.get("schemas", {})
    pending = list(referenced)
    while pending:
        name = pending.pop()
        before = set(referenced)
        collect(schemas.get(name, {}))
        pending.extend(sorted(referenced - before))
    components["schemas"] = {
        name: schemas[name] for name in sorted(referenced) if name in schemas
    }
    return source


class NodeStatus(StrictModel):
    id: str = Field(pattern=NODE_PATTERN)
    display_name: str
    hostname: str
    lifecycle: str
    healthy: bool | None
    stale: bool
    labels: dict[str, str]
    profile: str | None
    memory_available_bytes: int = Field(ge=0)
    disk_available_bytes: int = Field(ge=0)
    probe_age_seconds: float | None = Field(default=None, ge=0)
    inventory_observed_at: str | None = None
    inventory_age_seconds: float | None = Field(default=None, ge=0)
    inventory_stale: bool = True
    inventory_capabilities: list[str] = Field(default_factory=list, max_length=64)
    agent_state: str = "unregistered"
    agent_implementation: str | None = None
    agent_migration_state: str | None = None
    last_seen_at: str | None = None
    last_seen_age_seconds: float | None = Field(default=None, ge=0)
    agent_last_seen_at: str | None = None
    agent_online: bool = False
    # Version-skew projection is deliberately nullable for pre-enrollment and
    # legacy observations.  Keeping these fields in the typed public model
    # prevents the dashboard from losing authenticated agent identity data.
    agent_platform_version: str | None = None
    agent_build_digest: str | None = None
    agent_active_slot: str | None = None
    agent_sha256: str | None = None
    agent_supervisor_generation: int | None = None
    certificate_expires_at: str | None = None
    certificate_expiry_seconds: float | None = Field(default=None, ge=0)
    compatibility: str = "unknown"


class FleetStatusResponse(StrictModel):
    commit: str = Field(pattern=COMMIT_PATTERN)
    nodes: list[NodeStatus]
    evidence_digest: str = Field(pattern=DIGEST_PATTERN)


class _FleetEvidence(StrictModel):
    commit: str = Field(pattern=COMMIT_PATTERN)
    nodes: list[NodeStatus]


def fleet_response(fleet_state: Mapping[str, object]) -> FleetStatusResponse:
    """Validate and digest the exact public live acceptance evidence."""

    evidence = _FleetEvidence.model_validate(fleet_state)
    public = evidence.model_dump(mode="json")
    digest = hashlib.sha256(canonical_message(public)).hexdigest()
    return FleetStatusResponse(**public, evidence_digest=digest)
