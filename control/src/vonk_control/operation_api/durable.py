"""Operation Api: durable."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    EndpointState,
    GatewayRouteState,
    LifecycleState,
    LifecycleSubject,
)
from vonk_agent_protocol.route_activation import ActivationMarker

from .. import agent_operation_states, job_states
from ..agent_jobs import (
    AgentJobService,
    authorize_operator_resume_in_session,
    operator_resume_eligible_operations_in_session,
    retire_exhausted_operations_in_session,
)
from ..auth import CursorCodec, CursorError
from ..endpoint_contract import EndpointResponse
from ..fleet_profile_contract import (
    FleetProfileEndpointAssignmentView,
    FleetProfileEndpointIntent,
    FleetProfileEndpointState,
    FleetProfileEndpointsView,
)
from ..lifecycle.job import JobAdapter
from ..models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Job,
    RecipeRouteAuthority,
    RoutePublication,
    RoutePublicationOwner,
)
from ..operation_item_contract import OperationItem, OperationOwnerReference
from ..operation_progress import aggregate_progress, member_progress
from ..route_bundle_contract import RouteBundleDocument, RouteEndpointDocument
from ..route_runtime import recipe_route_run_id, verify_active_route_bundle
from ..state_filters import state_filter
from .constants import _ACTIVE_PUBLICATION_STATES, NODE_PATTERN
from .contracts import (
    AgentSummary,
    JobProgress,
    OperationApiServices,
    OperationListPage,
    OperationPage,
    OperationProvider,
    OperationProviderProtocol,
    OperationQuery,
)
from .diagnostics import _agent_upgrade_diagnostics, _aware
from .providers import _activity_keyset_filter, _StandaloneJobActivityProjection
from .responses import _advertised_actions, _operation_item, _progress_projection
from .route_snapshot import _ActiveRouteSnapshot, _stored_activation_marker


class _DurableOperationProjection:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        route_root: Path,
        *,
        clock: Callable[[], datetime],
        stale_after_seconds: int,
        cursors: CursorCodec,
        profile_endpoint_intent: (
            Callable[[Session, int], FleetProfileEndpointIntent] | None
        ) = None,
    ) -> None:
        if route_root.is_symlink() or stale_after_seconds <= 0:
            raise ValueError("operation projection configuration is invalid")
        self._sessions = sessions
        self._route_root = route_root
        self._clock = clock
        self._stale_after_seconds = stale_after_seconds
        self._cursors = cursors
        self._profile_endpoint_intent = profile_endpoint_intent

    def _publication_snapshot(self, session: Session) -> _ActiveRouteSnapshot:
        owner = session.get(RoutePublicationOwner, 1)
        publication = (
            None
            if owner is None or owner.authority_id is None
            else session.get(RoutePublication, owner.authority_id)
        )
        authority = (
            None
            if owner is None or owner.authority_id is None
            else session.get(RecipeRouteAuthority, owner.authority_id)
        )
        if (
            owner is None
            or publication is None
            or authority is None
            or owner.authority_id is None
            or publication.generation is None
            or publication.state not in _ACTIVE_PUBLICATION_STATES
            or publication.generation != owner.owner_generation
            or publication.activation_marker is None
            or publication.activation_marker_digest is None
            or publication.route_digest is None
            or publication.litellm_digest is None
            or publication.bundle_digest is None
        ):
            raise RuntimeError("active publication is unavailable")
        marker = _stored_activation_marker(publication.activation_marker)
        return _ActiveRouteSnapshot(
            marker=marker,
            marker_digest=publication.activation_marker_digest,
            route_digest=publication.route_digest,
            litellm_digest=publication.litellm_digest,
            bundle_digest=publication.bundle_digest,
            authority_id=owner.authority_id,
            owner_generation=owner.owner_generation,
            publication_generation=publication.generation,
            plan_digest=publication.plan_digest,
        )

    def _verified_routes(
        self, snapshot: _ActiveRouteSnapshot
    ) -> tuple[ActivationMarker, RouteBundleDocument]:
        bundle = verify_active_route_bundle(self._route_root)
        active_marker = bundle.marker
        if (
            active_marker != snapshot.marker
            or active_marker.digest != snapshot.marker_digest
            or active_marker.state != GatewayRouteState.PUBLISHED
            or active_marker.authority_id != snapshot.authority_id
            or active_marker.plan_digest != snapshot.plan_digest
            or active_marker.generation != snapshot.publication_generation
            or active_marker.generation != snapshot.owner_generation
            or active_marker.evidence_set_digest != snapshot.plan_digest
            or active_marker.routes_sha256 != snapshot.route_digest
            or active_marker.litellm_sha256 != snapshot.litellm_digest
            or active_marker.manifest_sha256 != snapshot.bundle_digest
        ):
            raise RuntimeError("activation marker does not match durable state")
        routes = bundle.routes
        if (
            routes is None
            or routes.generation != snapshot.publication_generation
            or routes.state != GatewayRouteState.PUBLISHED
        ):
            raise RuntimeError("active route state does not match publication")
        return active_marker, routes

    @staticmethod
    def _endpoint_payload(
        alias: str,
        raw: RouteEndpointDocument,
        active_marker: ActivationMarker,
        gateway_api_base: str,
    ) -> EndpointResponse:
        if re.fullmatch(NODE_PATTERN, raw.node_id) is None:
            raise RuntimeError("active endpoint is invalid")
        return EndpointResponse(
            alias=alias,
            api_base=gateway_api_base,
            backend_api_base=(
                f"{raw.scheme}://{raw.address}:{raw.port}{raw.path.rstrip('/')}"
            ),
            generation=active_marker.generation,
            node_id=raw.node_id,
            observed_at=raw.observed_at,
            plan_digest=active_marker.plan_digest,
        )

    @staticmethod
    def _route_run_id(raw: RouteEndpointDocument) -> str | None:
        return recipe_route_run_id(raw.operation_id)

    def profile_endpoint(
        self, number: int, alias: str | None, gateway_api_base: str
    ) -> FleetProfileEndpointsView:
        if self._profile_endpoint_intent is None:
            raise RuntimeError("profile endpoint ownership is unavailable")
        with self._sessions() as session:
            intent = self._profile_endpoint_intent(session, number)
            assignments = intent.assignments
            if assignments is None:
                if intent.projection_issue is None:
                    raise RuntimeError(
                        "profile endpoint membership is unavailable without a reason"
                    )
                return FleetProfileEndpointsView(
                    number=intent.number,
                    profile_id=intent.profile_id,
                    application_id=intent.application_id,
                    application_state=intent.application_state,
                    observed_at=_aware(self._clock()),
                    assignments=None,
                    projection_issue=intent.projection_issue,
                )
            if intent.projection_issue is not None:
                raise RuntimeError(
                    "profile endpoint issue conflicts with available membership"
                )
            if alias is not None:
                assignments = tuple(item for item in assignments if item.alias == alias)
                if not assignments:
                    raise KeyError(alias)
            snapshot: _ActiveRouteSnapshot | None = None
            unavailable = False
            if any(item.expected_run_id is not None for item in assignments):
                try:
                    snapshot = self._publication_snapshot(session)
                except (OSError, RuntimeError, TypeError, ValueError):
                    unavailable = True

        endpoints: dict[str, EndpointResponse] = {}
        states: dict[str, FleetProfileEndpointState] = {
            item.assignment_id: item.state for item in assignments
        }
        if unavailable:
            for item in assignments:
                if item.expected_run_id is not None:
                    states[item.assignment_id] = EndpointState.UNAVAILABLE
        elif snapshot is not None:
            try:
                active_marker, route_document = self._verified_routes(snapshot)
            except (OSError, RuntimeError, TypeError, ValueError):
                for item in assignments:
                    if item.expected_run_id is not None:
                        states[item.assignment_id] = EndpointState.UNAVAILABLE
            else:
                for item in assignments:
                    if item.expected_run_id is None or item.alias is None:
                        continue
                    raw = route_document.routes.get(item.alias)
                    if raw is None:
                        states[item.assignment_id] = EndpointState.WITHDRAWN
                        continue
                    route_run_id = self._route_run_id(raw)
                    if route_run_id is None:
                        states[item.assignment_id] = EndpointState.UNAVAILABLE
                        continue
                    if route_run_id != item.expected_run_id:
                        states[item.assignment_id] = EndpointState.WITHDRAWN
                        continue
                    try:
                        endpoints[item.assignment_id] = self._endpoint_payload(
                            item.alias, raw, active_marker, gateway_api_base
                        )
                    except (RuntimeError, TypeError, ValueError):
                        states[item.assignment_id] = EndpointState.UNAVAILABLE
                    else:
                        states[item.assignment_id] = EndpointState.PUBLISHED

                # Fence endpoint authorization with a fresh SQL read after route validation.
                try:
                    with self._sessions() as session:
                        current = self._profile_endpoint_intent(session, number)
                        current_snapshot = self._publication_snapshot(session)
                        unchanged = (
                            current.profile_id == intent.profile_id
                            and current.application_id == intent.application_id
                            and current.assignments == intent.assignments
                            and current_snapshot == snapshot
                        )
                except (OSError, RuntimeError, TypeError, ValueError):
                    unchanged = False
                if not unchanged:
                    # Retain the readable membership, but never present a
                    # stale endpoint as current after its ownership fence moved.
                    endpoints.clear()
                    for item in assignments:
                        if item.expected_run_id is not None:
                            states[item.assignment_id] = EndpointState.UNAVAILABLE

        return FleetProfileEndpointsView(
            number=intent.number,
            profile_id=intent.profile_id,
            application_id=intent.application_id,
            application_state=intent.application_state,
            observed_at=_aware(self._clock()),
            assignments=[
                FleetProfileEndpointAssignmentView(
                    assignment_id=item.assignment_id,
                    recipe_title=item.recipe_title,
                    desired_state=item.desired_state,
                    alias=item.alias,
                    state=states[item.assignment_id],
                    endpoint=endpoints.get(item.assignment_id),
                )
                for item in assignments
            ],
        )

    def agents(self) -> Sequence[AgentSummary]:
        now = _aware(self._clock())
        with self._sessions() as session:
            nodes = list(
                session.scalars(
                    select(AgentNode).order_by(AgentNode.node_id).limit(500)
                )
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
        projected: list[AgentSummary] = []
        for node in nodes:
            last_seen = None if node.last_seen_at is None else _aware(node.last_seen_at)
            age = (
                None
                if last_seen is None
                else max(0.0, (now - last_seen).total_seconds())
            )
            certificate = latest_certificates.get(node.node_id)
            not_after = None if certificate is None else _aware(certificate.not_after)
            projected.append(
                AgentSummary(
                    certificate_expires_at=(
                        None if not_after is None else not_after.isoformat()
                    ),
                    last_seen_age_seconds=age,
                    last_seen_at=None if last_seen is None else last_seen.isoformat(),
                    node_id=node.node_id,
                    protocol_version=node.protocol_version,
                    semantic_version=node.semantic_version,
                    build_digest=node.build_digest,
                    binary_digest=node.binary_digest,
                    stale=age is None or age > self._stale_after_seconds,
                    state=node.state,
                )
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
                raise CursorError("operation cursor is invalid") from None
        now = _aware(self._clock())
        with self._sessions() as session:
            agent_upgrade_diagnostics = _agent_upgrade_diagnostics(session, job_id)
            resume_operation_ids = {
                operation.id
                for operation in operator_resume_eligible_operations_in_session(
                    session, job_id, now
                )
            }
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
                    statement.order_by(
                        AgentOperation.created_at, AgentOperation.id
                    ).limit(limit + 1)
                )
            )
            has_more = len(operations) > limit
            operations = operations[:limit]
            # Aggregate the full job, independently of its displayed page.
            aggregate_members = []
            for operation, progress in session.execute(
                select(AgentOperation, AgentOperationAttempt.progress)
                .outerjoin(
                    AgentOperationAttempt,
                    (AgentOperationAttempt.operation_id == AgentOperation.id)
                    & (AgentOperationAttempt.attempt == AgentOperation.current_attempt),
                )
                .where(AgentOperation.parent_job_id == job_id)
                .order_by(AgentOperation.created_at, AgentOperation.id)
            ):
                projected = _progress_projection(progress, operation.state, now=now)
                # A node can own several steps: retain the exact operation ID.
                aggregate_members.append(
                    member_progress(
                        projected,
                        member_id=operation.id,
                        kind=operation.kind,
                        phase=operation.state,
                        state=operation.state,
                    )
                )
            aggregate = (
                aggregate_progress(aggregate_members) if aggregate_members else None
            )
            state_counts = {
                str(state): int(count)
                for state, count in session.execute(
                    select(AgentOperation.state, func.count())
                    .where(AgentOperation.parent_job_id == job_id)
                    .group_by(AgentOperation.state)
                )
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
            _operation_item(operation, attempts.get(operation.id), now=now).model_copy(
                update={
                    "node_ids": [],
                    "parent_id": None,
                    "status_reason": None,
                    "node_id": operation.node_id,
                    "supported_actions": self._activity_actions(
                        operation,
                        attempts.get(operation.id),
                        resume=operation.id in resume_operation_ids,
                    ),
                }
            )
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
        terminal = {
            "succeeded",
            "accepted",
            agent_operation_states.RETAINED_COMPENSATED,
        }
        failed = {"failed", "uncertain"}
        running = {"queued", "running", "planned", "compensating"}
        return OperationPage(
            projected_at=now,
            items=items,
            next_cursor=next_cursor,
            progress=JobProgress(
                operation=aggregate,
                completed=sum(state_counts.get(state, 0) for state in terminal),
                failed=sum(state_counts.get(state, 0) for state in failed),
                running=sum(state_counts.get(state, 0) for state in running),
                total=sum(state_counts.values()),
            ),
            agent_upgrade_diagnostics=agent_upgrade_diagnostics,
            recovery_actions=("resume",) if resume_operation_ids else (),
        )

    def list_operations(
        self,
        cursor: str | None,
        limit: int,
        state: str | None,
        node_id: str | None,
        request_id: str | None,
        *,
        now: datetime | None = None,
    ) -> OperationListPage:
        """List the same durable AgentOperation authority globally."""

        if not 1 <= limit <= 100:
            raise ValueError("operation page limit is invalid")
        context = {"state": state, "node_id": node_id, "request_id": request_id}
        boundary: tuple[datetime, str] | None = None
        if cursor is not None:
            try:
                decoded = self._cursors.decode(
                    cursor,
                    resource="operations",
                    order="created-at-desc/id-desc/v1",
                    context=context,
                )
                if (
                    not isinstance(decoded, list)
                    or len(decoded) != 2
                    or not all(isinstance(item, str) for item in decoded)
                ):
                    raise ValueError
                boundary = (datetime.fromisoformat(decoded[0]), decoded[1])
            except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
                raise CursorError("operation cursor is invalid") from None
        now = _aware(self._clock() if now is None else now)
        with self._sessions() as session:
            filters = []
            if state is not None:
                filters.append(
                    state_filter(
                        AgentOperation.state, LifecycleSubject.AGENT_OPERATION, state
                    )
                )
            if node_id is not None:
                filters.append(AgentOperation.node_id == node_id)
            if request_id is not None:
                filters.append(Job.request_id == request_id)
            keyset = _activity_keyset_filter(
                AgentOperation.created_at, AgentOperation.id, "", boundary
            )
            if keyset is not None:
                filters.append(keyset)
            rows = list(
                session.scalars(
                    select(AgentOperation)
                    .join(Job, AgentOperation.parent_job_id == Job.id)
                    .where(*filters)
                    .order_by(
                        AgentOperation.created_at.desc(), AgentOperation.id.desc()
                    )
                    .limit(limit + 1)
                )
            )
            has_more = len(rows) > limit
            rows = rows[:limit]
            total_filters = []
            if state is not None:
                total_filters.append(
                    state_filter(
                        AgentOperation.state, LifecycleSubject.AGENT_OPERATION, state
                    )
                )
            if node_id is not None:
                total_filters.append(AgentOperation.node_id == node_id)
            if request_id is not None:
                total_filters.append(Job.request_id == request_id)
            total = int(
                session.scalar(
                    select(func.count())
                    .select_from(AgentOperation)
                    .join(Job, AgentOperation.parent_job_id == Job.id)
                    .where(*total_filters)
                )
                or 0
            )
            attempts = {
                attempt.operation_id: attempt
                for attempt in session.scalars(
                    select(AgentOperationAttempt).where(
                        AgentOperationAttempt.operation_id.in_([row.id for row in rows])
                    )
                )
                if any(
                    row.id == attempt.operation_id
                    and row.current_attempt == attempt.attempt
                    for row in rows
                )
            }
            owners = {
                job.id: job.request_id
                for job in session.scalars(
                    select(Job).where(Job.id.in_([row.parent_job_id for row in rows]))
                )
            }
            resume_operation_ids = {
                operation.id
                for parent_job_id in {row.parent_job_id for row in rows}
                for operation in operator_resume_eligible_operations_in_session(
                    session, parent_job_id, now
                )
            }
        items = [
            self._activity_item(
                row,
                attempts.get(row.id),
                request_id=owners.get(row.parent_job_id),
                resume=row.id in resume_operation_ids,
                now=now,
            )
            for row in rows
        ]
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = self._cursors.encode(
                resource="operations",
                order="created-at-desc/id-desc/v1",
                context=context,
                boundary=[_aware(last.created_at).isoformat(), last.id],
            )
        return OperationListPage(items=items, next_cursor=next_cursor, total=total)

    def list_operation_provider(self, query: OperationQuery) -> OperationListPage:
        """Return AgentOperation rows after the shared global boundary."""

        if not 1 <= query.limit <= 101:
            raise ValueError("operation provider page limit is invalid")
        filters = []
        if query.state is not None:
            filters.append(
                state_filter(
                    AgentOperation.state, LifecycleSubject.AGENT_OPERATION, query.state
                )
            )
        if query.node_id is not None:
            filters.append(AgentOperation.node_id == query.node_id)
        if query.request_id is not None:
            filters.append(Job.request_id == query.request_id)
        keyset = _activity_keyset_filter(
            AgentOperation.created_at, AgentOperation.id, "", query.after
        )
        if keyset is not None:
            filters.append(keyset)
        now = _aware(
            self._clock() if query.projected_at is None else query.projected_at
        )
        with self._sessions() as session:
            rows = list(
                session.scalars(
                    select(AgentOperation)
                    .join(Job, AgentOperation.parent_job_id == Job.id)
                    .where(*filters)
                    .order_by(
                        AgentOperation.created_at.desc(), AgentOperation.id.desc()
                    )
                    .limit(query.limit)
                )
            )
            total_filters = []
            if query.state is not None:
                total_filters.append(
                    state_filter(
                        AgentOperation.state,
                        LifecycleSubject.AGENT_OPERATION,
                        query.state,
                    )
                )
            if query.node_id is not None:
                total_filters.append(AgentOperation.node_id == query.node_id)
            if query.request_id is not None:
                total_filters.append(Job.request_id == query.request_id)
            total = int(
                session.scalar(
                    select(func.count())
                    .select_from(AgentOperation)
                    .join(Job, AgentOperation.parent_job_id == Job.id)
                    .where(*total_filters)
                )
                or 0
            )
            attempts = {
                attempt.operation_id: attempt
                for attempt in session.scalars(
                    select(AgentOperationAttempt).where(
                        AgentOperationAttempt.operation_id.in_([row.id for row in rows])
                    )
                )
                if any(
                    row.id == attempt.operation_id
                    and row.current_attempt == attempt.attempt
                    for row in rows
                )
            }
            owners = {
                job.id: job.request_id
                for job in session.scalars(
                    select(Job).where(Job.id.in_([row.parent_job_id for row in rows]))
                )
            }
            resume_operation_ids = {
                operation.id
                for parent_job_id in {row.parent_job_id for row in rows}
                for operation in operator_resume_eligible_operations_in_session(
                    session, parent_job_id, now
                )
            }
        return OperationListPage(
            items=[
                self._activity_item(
                    row,
                    attempts.get(row.id),
                    request_id=owners.get(row.parent_job_id),
                    resume=row.id in resume_operation_ids,
                    now=now,
                )
                for row in rows
            ],
            next_cursor=None,
            total=total,
        )

    def get_operation(
        self, operation_id: str, *, now: datetime | None = None
    ) -> OperationItem:
        now = _aware(self._clock() if now is None else now)
        with self._sessions() as session:
            operation = session.get(AgentOperation, operation_id)
            if operation is None:
                raise KeyError(operation_id)
            attempt = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == operation.id,
                    AgentOperationAttempt.attempt == operation.current_attempt,
                )
            )
            resume = operation.id in {
                eligible.id
                for eligible in operator_resume_eligible_operations_in_session(
                    session, operation.parent_job_id, now
                )
            }
            job = session.get(Job, operation.parent_job_id)
            return self._activity_item(
                operation,
                attempt,
                request_id=None if job is None else job.request_id,
                resume=resume,
                now=now,
            )

    @classmethod
    def _activity_item(
        cls,
        operation: AgentOperation,
        attempt: AgentOperationAttempt | None,
        *,
        request_id: str | None,
        resume: bool,
        now: datetime,
    ) -> OperationItem:
        """One agent operation as a global Activity row, owned by its job."""

        return _operation_item(operation, attempt, now=now).model_copy(
            update={
                "created_at": _aware(operation.created_at).isoformat(),
                "job_id": operation.parent_job_id,
                "supported_actions": cls._activity_actions(
                    operation, attempt, resume=resume
                ),
                "owner": OperationOwnerReference(
                    kind="job", id=operation.parent_job_id, request_id=request_id
                ),
            }
        )

    @staticmethod
    def _activity_actions(
        operation: AgentOperation,
        attempt: AgentOperationAttempt | None,
        *,
        resume: bool,
    ) -> list[str] | None:
        raw = (
            _advertised_actions(operation.payload.get("supported_actions"))
            if isinstance(operation.payload, Mapping)
            else None
        )
        actions = (
            [action for action in raw if isinstance(action, str) and action != "resume"]
            if isinstance(raw, list)
            else []
        )
        if resume:
            actions.append("resume")
        return list(dict.fromkeys(actions)) or None

    def resume_job(self, job_id: str) -> None:
        with self._sessions.begin() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            if job.state not in job_states.words(LifecycleState.NEEDS_OPERATOR):
                raise ValueError("job is not waiting for operator")
            scope = AgentJobService._target_scope(job.targets)
            if scope is None or not AgentJobService._lock_target_scopes(
                session, {"resume": (job_id, scope)}, scope[0]
            ):
                raise ValueError("job target scope changed")
            if job.state not in job_states.words(LifecycleState.NEEDS_OPERATOR):
                raise ValueError("job is not waiting for operator")
            now = self._clock()
            # The parent transition alone does not release the parked child:
            # the claim predicate requires the operation's own retry
            # authorisation.  It is written in this same transaction so an
            # exhausted budget rolls the parent transition back and no
            # half-queued job that can never be claimed is committed.
            authorize_operator_resume_in_session(session, job_id, now)
            if not JobAdapter(session, clock=self._clock).resume(session, job, now):
                raise ValueError("job is not waiting for operator")

    def retire_job(self, job_id: str) -> None:
        """Retire an exhausted order and retain its effects for exact cleanup.

        The refusal is raised inside this transaction and rolls it back, so a
        live or still-recoverable operation is left exactly as it was.
        """

        with self._sessions.begin() as session:
            now = self._clock()
            retire_exhausted_operations_in_session(session, job_id, now)


def durable_operation_services(
    sessions: sessionmaker[Session],
    route_root: Path,
    *,
    clock: Callable[[], datetime],
    cursors: CursorCodec,
    stale_after_seconds: int = 150,
    resume_agent_upgrade: Callable[[str], None] | None = None,
    operation_providers: Sequence[OperationProviderProtocol] = (),
    profile_endpoint_intent: (
        Callable[[Session, int], FleetProfileEndpointIntent] | None
    ) = None,
) -> OperationApiServices:
    """Build bounded projections over database state and the active route bundle."""

    projection = _DurableOperationProjection(
        sessions,
        route_root,
        clock=clock,
        stale_after_seconds=stale_after_seconds,
        cursors=cursors,
        profile_endpoint_intent=profile_endpoint_intent,
    )
    standalone_jobs = _StandaloneJobActivityProjection(sessions, operation_providers)

    def resume_job(job_id: str) -> None:
        with sessions() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            kind = job.kind
        if kind == "agent-upgrade":
            if resume_agent_upgrade is None:
                raise ValueError("agent upgrade resume is unavailable")
            resume_agent_upgrade(job_id)
            return
        projection.resume_job(job_id)

    def retire_job(job_id: str) -> None:
        projection.retire_job(job_id)

    return OperationApiServices(
        clock=clock,
        agents=projection.agents,
        job_operations=projection.job_operations,
        resume_job=resume_job,
        list_operations=projection.list_operations,
        get_operation=projection.get_operation,
        operation_providers=(
            OperationProvider(
                family="job",
                list_operations=standalone_jobs.list_operations,
                get_operation=standalone_jobs.get_operation,
            ),
            OperationProvider(
                family="agent",
                list_operations=projection.list_operation_provider,
                get_operation=projection.get_operation,
                get_operation_at=lambda operation_id, now: projection.get_operation(
                    operation_id, now=now
                ),
            ),
            *operation_providers,
        ),
        cursor_codec=cursors,
        retire_job=retire_job,
        profile_endpoint=(
            projection.profile_endpoint if profile_endpoint_intent is not None else None
        ),
    )
