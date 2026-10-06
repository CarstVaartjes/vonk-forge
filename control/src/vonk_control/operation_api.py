"""Strict, secret-free representations for routine administrative operations."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

from pydantic import (
    ConfigDict,
    Field,
    StrictStr,
    TypeAdapter,
    ValidationError,
    model_serializer,
)
from sqlalchemy import String, and_, cast, false, func, or_, select, true
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement, SQLColumnExpression
from vonk_agent_protocol import (
    EndpointState,
    GatewayRouteState,
    LifecycleState,
    LifecycleSubject,
    OperationFailureCode,
    OperationMemberProgress,
    OperationProgress,
    canonical_message,
)
from vonk_agent_protocol.contracts import AgentFailureResult
from vonk_agent_protocol.route_activation import ActivationMarker

from . import agent_operation_states, job_states
from .agent_jobs import (
    AgentJobService,
    authorize_operator_resume_in_session,
    operator_resume_eligible_operations_in_session,
    retire_exhausted_operations_in_session,
)
from .agent_upgrade_contract import AgentUpgradePackage
from .agent_upgrade_status import (
    GENERIC_AGENT_UPGRADE_REASONS,
    RECOVERABLE_AGENT_UPGRADE_REASONS,
    agent_upgrade_next_action,
    operator_agent_upgrade_reason,
)
from .auth import CursorCodec, CursorError
from .bounded_json import BoundedJSONError, mapping, require_sequence
from .endpoint_contract import EndpointResponse
from .fleet_profile_contract import (
    FleetProfileApplicationCancellationView,
    FleetProfileEndpointAssignmentView,
    FleetProfileEndpointIntent,
    FleetProfileEndpointState,
    FleetProfileEndpointsView,
)
from .lifecycle.agent_operation import retry_scheduled
from .lifecycle.job import JobAdapter
from .logging import redact_text
from .models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Job,
    RecipeRouteAuthority,
    RoutePublication,
    RoutePublicationOwner,
)
from .operation_blockers import OperationBlocker, read_blockers
from .operation_contract import (
    OperationEvidenceDownload,
    OperationFailure,
    OperationFailureEvidence,
    OperationRecovery,
    OperationRecoveryAction,
    recovery_for_operation,
)
from .operation_item_contract import (
    AGENT_OPERATION_KINDS,
    OperationItem,
    OperationOwnerReference,
    OperationResultFacts,
    OperationRow,
    agent_receipt_for,
    operation_item,
)
from .operation_progress import aggregate_progress, project_progress
from .route_bundle_contract import RouteBundleDocument, RouteEndpointDocument
from .route_runtime import verify_active_route_bundle
from .state_filters import state_filter
from .strict_json import (
    StrictModel,
    read_stored_model,
    warn_unreadable_once,
)

COMMIT_PATTERN = r"^[0-9a-f]{40}$"
DIGEST_PATTERN = r"^[0-9a-f]{64}$"
IDENTIFIER_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,62}$"
NODE_PATTERN = r"^spk_[0-9a-f]{32}$"
_ACTIVE_PUBLICATION_STATES = frozenset({"completed"})
_ADMIN_OPERATION_IDS = {
    ("get", "/api/fleet"): "getFleetStatus",
    ("get", "/api/operations/{operation_id}/evidence"): "getOperationEvidence",
    ("get", "/api/fleet/stream"): "streamFleetEvents",
    (
        "get",
        "/api/recipe/runs/{run_id}/artifact-jobs",
    ): "listArtifactJobsForRun",
    (
        "post",
        "/api/recipe/runs/{run_id}/artifact-jobs",
    ): "createArtifactJob",
    ("get", "/api/artifact-jobs/capabilities"): "getArtifactJobCapabilities",
    ("get", "/api/artifact-jobs/requests/{request_id}"): "getArtifactJobByRequestId",
    ("get", "/api/artifact-jobs/{job_id}"): "getArtifactJobStatus",
    ("put", "/api/artifact-jobs/{job_id}/inputs/{name}"): "uploadArtifactJobInput",
    ("post", "/api/artifact-jobs/{job_id}/finalize"): "finalizeArtifactJob",
    ("post", "/api/artifact-jobs/{job_id}/submit"): "submitArtifactJob",
    ("post", "/api/artifact-jobs/{job_id}/cancel"): "cancelArtifactJob",
    (
        "get",
        "/api/artifact-jobs/{job_id}/results/{name}/{sha256}",
    ): "downloadArtifactJobResult",
    ("get", "/api/operations"): "listOperations",
    ("get", "/api/jobs/{job_id}"): "getJob",
    ("get", "/api/operations/{operation_id}"): "getOperation",
    ("post", "/api/jobs/{job_id}/resume"): "resumeJob",
}


def _stored_activation_marker(value: object) -> ActivationMarker:
    try:
        document = canonical_message(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError("durable activation marker is invalid") from error
    try:
        return read_stored_model(ActivationMarker, document, from_json=True)
    except ValidationError as error:
        raise RuntimeError("durable activation marker is invalid") from error


_HTTP_METHODS = frozenset({"delete", "get", "patch", "post", "put"})
BoundedIdentifier = Annotated[str, Field(min_length=1, max_length=128)]
NodeIdentifier = Annotated[str, Field(pattern=NODE_PATTERN)]
DigestIdentifier = Annotated[str, Field(pattern=DIGEST_PATTERN)]


class OperationProjectionError(RuntimeError):
    """Durable operation state cannot be safely projected."""


@dataclass(frozen=True)
class _ActiveRouteSnapshot:
    marker: ActivationMarker
    marker_digest: str
    route_digest: str
    litellm_digest: str | None
    bundle_digest: str
    authority_id: str
    owner_generation: int
    publication_generation: int
    plan_digest: str


class EmptyBody(StrictModel):
    """A body type used only where an explicit empty JSON object is allowed."""


class ErrorContextResponse(StrictModel):
    """Safe context shared by public errors and generated clients."""

    operation: str = Field(min_length=1, max_length=160)
    endpoint: str | None = Field(
        default=None,
        pattern=r"^/[^?#\x00\r\n]{0,511}$",
        max_length=512,
    )
    http_status: int | None = Field(default=None, ge=100, le=599)
    code: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,95}$")
    request_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    source: Literal["remote_rejection", "transport", "local_io", "protocol", "unknown"]
    decision: Literal["retry", "defer", "exit"]
    retryable: bool = False


class BoundedErrorResponse(StrictModel):
    detail: str = Field(min_length=1, max_length=256)
    context: ErrorContextResponse | None = None


class RequestValidationIssue(StrictModel):
    """A structural input error without the submitted input or validator context."""

    type: str
    loc: list[str | int]
    msg: str


class RequestValidationProblem(BoundedErrorResponse):
    issues: list[RequestValidationIssue]
    candidates: (
        list[
            Annotated[
                str,
                Field(
                    pattern=r"^(?:spk_[0-9a-f]{32}|[a-z0-9][a-z0-9._-]{0,62}/[a-z0-9][a-z0-9._-]{0,62})$",
                    max_length=127,
                ),
            ]
        ]
        | None
    ) = None


class HealthzResponse(StrictModel):
    status: Literal["ok"]


class ReadyzResponse(StrictModel):
    status: Literal["ready"]


class JobResponse(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    state: str = Field(min_length=1, max_length=80)


def bounded_error_responses(*status_codes: int) -> dict[int | str, dict[str, Any]]:
    """Describe stable JSON errors for generated clients."""

    return {
        status_code: {
            "model": RequestValidationProblem
            if status_code == 422
            else BoundedErrorResponse
        }
        for status_code in status_codes
    }


class AgentSummary(StrictModel):
    node_id: str = Field(pattern=NODE_PATTERN)
    state: str = Field(min_length=1, max_length=80)
    protocol_version: int | None = Field(default=None, ge=1)
    semantic_version: str | None = Field(
        default=None,
        pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$",
    )
    build_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    binary_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    last_seen_at: str | None = Field(default=None, max_length=64)
    last_seen_age_seconds: float | None = Field(default=None, ge=0)
    stale: bool
    certificate_expires_at: str | None = Field(default=None, max_length=64)


class AgentsResponse(StrictModel):
    agents: list[AgentSummary]


JobOperationProgress = OperationProgress


class JobOperationResponse(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    node_id: str = Field(pattern=NODE_PATTERN)
    kind: str = Field(min_length=1, max_length=80)
    state: str = Field(min_length=1, max_length=80)
    attempt: int = Field(ge=0)
    progress: JobOperationProgress | None = None
    updated_at: str | None = None
    failure: OperationFailure | None = None
    evidence_download: OperationEvidenceDownload | None = None
    recovery: OperationRecovery | None = None

    @model_serializer(mode="wrap")
    def _serialize_without_unset_evidence(self, handler):
        document = handler(self)
        for key in ("failure", "evidence_download", "recovery"):
            if document.get(key) is None:
                document.pop(key, None)
        return document


class OperationDetailResponse(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    parent_id: str | None = Field(default=None, max_length=128)
    node_ids: list[NodeIdentifier] = Field(max_length=1024)
    kind: str = Field(min_length=1, max_length=80)
    state: str = Field(min_length=1, max_length=80)
    attempt: int = Field(ge=0)
    progress: JobOperationProgress | None = None
    created_at: str = Field(min_length=1, max_length=64)
    updated_at: str | None = None
    failure: OperationFailure | None = None
    evidence_download: OperationEvidenceDownload | None = None
    cancellation: FleetProfileApplicationCancellationView | None = None
    recovery: OperationRecovery | None = None
    owner: OperationOwnerReference | None = None
    #: Why this operation is not currently progressing.  A refused claim
    #: records the refusing check here so an operator can tell "no work" apart
    #: from "work this node may not execute, and why".
    status_reason: str | None = Field(default=None, max_length=1024)
    #: What a queued or blocked operation is waiting for, as of its latest check.
    blockers: list[OperationBlocker] = Field(default_factory=list, max_length=16)
    #: When the Controller will check again, for an operation that will retry.
    next_attempt_at: str | None = Field(default=None, max_length=64)

    @model_serializer(mode="wrap")
    def _serialize_without_unset_evidence(self, handler):
        document = handler(self)
        for key in (
            "failure",
            "evidence_download",
            "cancellation",
            "recovery",
            "owner",
            "status_reason",
            "next_attempt_at",
        ):
            if document.get(key) is None:
                document.pop(key, None)
        if not document.get("blockers"):
            document.pop("blockers", None)
        return document


class OperationsResponse(StrictModel):
    operations: list[OperationDetailResponse] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=512)
    total: int = Field(ge=0)


class JobProgress(StrictModel):
    operation: OperationProgress | None = None
    completed: int = Field(ge=0)
    failed: int = Field(ge=0)
    running: int = Field(ge=0)
    total: int = Field(ge=0)


class AgentUpgradeIdentityResponse(StrictModel):
    version: str | None = Field(default=None, max_length=128)
    binary_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    build_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")


class AgentUpgradeTargetDiagnosticsResponse(StrictModel):
    node_id: str = Field(pattern=NODE_PATTERN)
    state: str = Field(min_length=1, max_length=80)
    attempts: int = Field(ge=0)
    target_proven: bool
    observed_identity: AgentUpgradeIdentityResponse
    raw_reason: str | None = Field(default=None, max_length=1024)
    retry_not_before: str | None = None
    retry_queued: bool


class AgentUpgradeDiagnosticsResponse(StrictModel):
    expected_identity: AgentUpgradeIdentityResponse
    targets: list[AgentUpgradeTargetDiagnosticsResponse] = Field(max_length=64)
    failure_details_unavailable: bool
    next_action: str | None = Field(default=None, max_length=512)
    operator_summary: str | None = Field(default=None, max_length=1024)


class JobDetailResponse(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    state: str = Field(min_length=1, max_length=80)
    kind: str = Field(min_length=1, max_length=80)
    authority_revision: str = Field(min_length=1, max_length=128)
    targets: list[BoundedIdentifier] = Field(max_length=100)
    target_next_cursor: str | None = Field(default=None, max_length=512)
    target_total: int = Field(ge=0)
    current_attempt: int = Field(ge=0)
    status_reason: str | None = Field(default=None, max_length=1024)
    operations: list[JobOperationResponse] = Field(max_length=100)
    operation_next_cursor: str | None = Field(default=None, max_length=512)
    operation_total: int = Field(ge=0)
    progress: JobProgress
    agent_upgrade_diagnostics: AgentUpgradeDiagnosticsResponse | None = None
    recovery: OperationRecovery | None = None


class JobResumeRequest(StrictModel):
    """What the operator wants the parked job's bounded authorisation to do.

    ``resume`` is the default and preserves the historic request shape: an
    omitted body or an omitted field authorises one more claim.  ``retire`` is
    the terminal disposition for work whose retry budget is already spent.
    """

    disposition: Literal["resume", "retire"] = "resume"


class JobResumeResponse(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    state: str = Field(pattern=r"^(queued|failed)$")


@dataclass(frozen=True)
class OperationApiServices:
    """Optional projections backed by accepted durable control state only."""

    agents: Callable[[], Sequence[Mapping[str, object]]]
    job_operations: Callable[[str, str | None, int], OperationPage]
    resume_job: Callable[[str], None]
    list_operations: (
        Callable[
            [str | None, int, str | None, str | None, str | None],
            OperationListPage,
        ]
        | None
    ) = None
    get_operation: Callable[[str], OperationRow] | None = None
    operation_providers: tuple[OperationProviderProtocol, ...] = ()
    cursor_codec: CursorCodec | None = None
    retire_job: Callable[[str], None] | None = None
    profile_endpoint: (
        Callable[[int, str | None, str], FleetProfileEndpointsView] | None
    ) = None


@dataclass(frozen=True)
class OperationPage:
    items: Sequence[OperationRow]
    next_cursor: str | None
    progress: JobProgress
    agent_upgrade_diagnostics: AgentUpgradeDiagnosticsResponse | None = None
    recovery_actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class OperationListPage:
    items: Sequence[OperationRow]
    next_cursor: str | None
    total: int


@dataclass(frozen=True)
class OperationQuery:
    """Shared boundary query understood by every global activity provider."""

    after: tuple[datetime, str] | None
    limit: int
    state: str | None
    node_id: str | None
    request_id: str | None = None


@dataclass(frozen=True)
class OperationProvider:
    """Composable typed operation family for the global activity projection."""

    family: str
    list_operations: Callable[[OperationQuery], OperationListPage]
    get_operation: Callable[[str], OperationRow]
    represented_job_kinds: frozenset[str] = frozenset()


class OperationProviderProtocol(Protocol):
    """Structural surface of one global activity operation family.

    :class:`OperationProvider` is the canonical value and stays an instantiable
    dataclass; implementations such as ``RunSwitchOperationProvider`` expose the
    same surface as methods.  Annotating the merge boundary with this protocol
    keeps both usable without a nominal base class.
    """

    @property
    def family(self) -> str: ...

    @property
    def list_operations(self) -> Callable[[OperationQuery], OperationListPage]: ...

    @property
    def get_operation(self) -> Callable[[str], OperationRow]: ...

    @property
    def represented_job_kinds(self) -> frozenset[str]: ...


def _operation_boundary(item: OperationItem) -> tuple[datetime, str]:
    if item.created_at is None:
        raise OperationProjectionError("operation created_at is invalid")
    try:
        parsed = datetime.fromisoformat(item.created_at)
    except ValueError:
        raise OperationProjectionError("operation created_at is invalid") from None
    if not item.id:
        raise OperationProjectionError("operation id is invalid")
    return _aware(parsed), item.id


def merge_operation_providers(
    providers: Sequence[OperationProviderProtocol],
    *,
    cursor: str | None,
    limit: int,
    state: str | None,
    node_id: str | None,
    request_id: str | None = None,
    cursors: CursorCodec,
) -> OperationListPage:
    """Merge provider rows using one deterministic newest-first cursor."""

    if not 1 <= limit <= 100:
        raise ValueError("operation page limit is invalid")
    context = {"state": state, "node_id": node_id, "request_id": request_id}
    after: tuple[datetime, str] | None = None
    if cursor is not None:
        try:
            decoded = cursors.decode(
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
            after = (_aware(datetime.fromisoformat(decoded[0])), decoded[1])
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            raise CursorError("operation cursor is invalid") from None
    query = OperationQuery(
        after=after,
        limit=limit + 1,
        state=state,
        node_id=node_id,
        request_id=request_id,
    )
    rows: list[OperationItem] = []
    total = 0
    seen: set[str] = set()
    for provider in providers:
        page = provider.list_operations(query)
        total += page.total
        for row in page.items:
            item = operation_item(row)
            node_ids = item.node_ids
            if not all(re.fullmatch(NODE_PATTERN, node) for node in node_ids):
                raise OperationProjectionError(
                    f"{provider.family} provider returned invalid node_ids"
                )
            if node_id is not None and node_id not in node_ids:
                continue
            boundary = _operation_boundary(item)
            if after is not None and boundary >= after:
                raise OperationProjectionError(
                    f"{provider.family} provider returned a stale operation row"
                )
            operation_id = boundary[1]
            if operation_id in seen:
                raise OperationProjectionError("operation ids are not globally unique")
            seen.add(operation_id)
            rows.append(item)
    rows.sort(key=_operation_boundary, reverse=True)
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        created_at, operation_id = _operation_boundary(rows[-1])
        next_cursor = cursors.encode(
            resource="operations",
            order="created-at-desc/id-desc/v1",
            context=context,
            boundary=[created_at.isoformat(), operation_id],
        )
    return OperationListPage(items=rows, next_cursor=next_cursor, total=total)


def get_operation_from_providers(
    providers: Sequence[OperationProviderProtocol], operation_id: str
) -> OperationItem:
    """Resolve one operation without coupling the Controller to provider modules."""

    match: OperationItem | None = None
    for provider in providers:
        try:
            item = provider.get_operation(operation_id)
        except KeyError:
            continue
        if match is not None:
            raise OperationProjectionError("operation ids are not globally unique")
        match = operation_item(item)
    if match is None:
        raise KeyError(operation_id)
    _operation_boundary(match)
    return match


_ACTIVITY_REQUEST_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_ACTIVITY_TARGET = re.compile(
    r"^(?:[a-z0-9][a-z0-9._-]{0,62}|"
    r"[a-z0-9][a-z0-9._-]{0,62}/[a-z0-9][a-z0-9._-]{0,62})$"
)
_ACTIVITY_STRINGS = TypeAdapter(list[StrictStr], config=ConfigDict(strict=True))
_JOB_ACTIVITY_PREFIX = "job:"


def _activity_keyset_filter(
    created_at_column: SQLColumnExpression[datetime],
    id_column: SQLColumnExpression[str],
    id_prefix: str,
    after: tuple[datetime, str] | None,
) -> ColumnElement[bool] | None:
    """Build the provider-local half of the shared prefixed ID boundary."""

    if after is None:
        return None
    created_at, activity_id = after
    if activity_id.startswith(id_prefix):
        same_time_ids = id_column < activity_id[len(id_prefix) :]
    elif id_prefix < activity_id:
        same_time_ids = true()
    else:
        same_time_ids = false()
    return or_(
        created_at_column < created_at,
        and_(created_at_column == created_at, same_time_ids),
    )


def _activity_node_ids(value: object, *, limit: int) -> list[str]:
    """Validate decoded JSON like stored JSON, then select exact Spark targets."""

    if (
        not isinstance(value, (list, tuple))
        or len(value) > limit
        or any(not isinstance(target, str) or len(target) > 127 for target in value)
    ):
        raise ValueError("stored activity targets are malformed")
    try:
        values = _ACTIVITY_STRINGS.validate_json(canonical_message(value))
    except (TypeError, ValueError) as error:
        raise ValueError("stored activity targets are malformed") from error
    if len(values) > limit or any(
        len(target) > 127 or _ACTIVITY_TARGET.fullmatch(target) is None
        for target in values
    ):
        raise ValueError("stored activity targets are malformed")
    return [
        target for target in values if re.fullmatch(NODE_PATTERN, target) is not None
    ]


def _activity_owner_request_id(value: object) -> str | None:
    if isinstance(value, str) and _ACTIVITY_REQUEST_ID.fullmatch(value) is not None:
        return value
    return None


class _StandaloneJobActivityProjection:
    """Project Jobs not already represented by an exact operation owner."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        providers: Sequence[OperationProviderProtocol],
    ) -> None:
        self._sessions = sessions
        self._represented_job_kinds = frozenset(
            kind
            for provider in providers
            for kind in getattr(provider, "represented_job_kinds", frozenset())
        )

    def _base_filters(self, query: OperationQuery) -> list[ColumnElement[bool]]:
        filters: list[ColumnElement[bool]] = []
        if self._represented_job_kinds:
            filters.append(Job.kind.not_in(self._represented_job_kinds))
        filters.append(
            ~select(AgentOperation.id)
            .where(AgentOperation.parent_job_id == Job.id)
            .exists()
        )
        if query.state is not None:
            filters.append(state_filter(Job.state, LifecycleSubject.JOB, query.state))
        if query.request_id is not None:
            filters.append(Job.request_id == query.request_id)
        if query.node_id is not None:
            filters.append(cast(Job.targets, String).contains(f'"{query.node_id}"'))
        return filters

    def list_operations(self, query: OperationQuery) -> OperationListPage:
        if not 1 <= query.limit <= 101:
            raise ValueError("operation provider page limit is invalid")
        base_filters = self._base_filters(query)
        page_filters = list(base_filters)
        boundary = _activity_keyset_filter(
            Job.created_at, Job.id, _JOB_ACTIVITY_PREFIX, query.after
        )
        if boundary is not None:
            page_filters.append(boundary)
        with self._sessions() as session:
            total = int(
                session.scalar(
                    select(func.count()).select_from(Job).where(*base_filters)
                )
                or 0
            )
            jobs = session.scalars(
                select(Job)
                .where(*page_filters)
                .order_by(Job.created_at.desc(), Job.id.desc())
                .limit(query.limit)
            )
            return OperationListPage(
                tuple(self._item(job) for job in jobs), None, total
            )

    def get_operation(self, operation_id: str) -> OperationItem:
        if not operation_id.startswith(_JOB_ACTIVITY_PREFIX):
            raise KeyError(operation_id)
        owner_id = operation_id[len(_JOB_ACTIVITY_PREFIX) :]
        with self._sessions() as session:
            job = session.get(Job, owner_id)
            if job is None or not self._is_standalone(session, job):
                raise KeyError(operation_id)
            return self._item(job)

    def _is_standalone(self, session: Session, job: Job) -> bool:
        if job.kind in self._represented_job_kinds:
            return False
        return (
            session.scalar(
                select(AgentOperation.id)
                .where(AgentOperation.parent_job_id == job.id)
                .limit(1)
            )
            is None
        )

    def _item(self, job: Job) -> OperationItem:
        activity_id = f"{_JOB_ACTIVITY_PREFIX}{job.id}"
        request_id = _activity_owner_request_id(job.request_id)
        try:
            if (
                not isinstance(job.id, str)
                or not 1 <= len(activity_id) <= 128
                or request_id is None
                or not isinstance(job.kind, str)
                or not 1 <= len(job.kind) <= 80
                or not isinstance(job.state, str)
                or not 1 <= len(job.state) <= 80
                or type(job.current_attempt) is not int
                or job.current_attempt < 0
                or not isinstance(job.created_at, datetime)
                or not isinstance(job.updated_at, datetime)
            ):
                raise ValueError("stored job activity is malformed")
            node_ids = _activity_node_ids(job.targets, limit=100)
            status_reason = job.status_reason
            if status_reason is not None and (
                not isinstance(status_reason, str) or len(status_reason) > 1024
            ):
                raise ValueError("stored job status reason is malformed")
        except (AttributeError, TypeError, ValueError, ValidationError):
            warn_unreadable_once("job", job.id)
            return self._unreadable_item(job, activity_id, request_id)
        return OperationItem(
            id=activity_id,
            job_id=job.id,
            owner=OperationOwnerReference(kind="job", id=job.id, request_id=request_id),
            node_ids=node_ids,
            kind=job.kind,
            state=job.state,
            attempt=job.current_attempt,
            created_at=_aware(job.created_at).isoformat(),
            updated_at=_aware(job.updated_at).isoformat(),
            supported_actions=[],
            status_reason=status_reason,
            blockers=(
                read_blockers(job.payload.get("blockers"))
                if isinstance(job.payload, Mapping)
                else None
            ),
            next_attempt_at=(
                _text_or_none(job.payload.get("retry_after_at"))
                if isinstance(job.payload, Mapping) and job.state == "queued"
                else None
            ),
        )

    def _unreadable_item(
        self, job: Job, activity_id: str, request_id: str | None
    ) -> OperationItem:
        return OperationItem(
            id=activity_id,
            job_id=job.id,
            owner=OperationOwnerReference(kind="job", id=job.id, request_id=request_id),
            kind="job-history-unreadable",
            state="unavailable",
            attempt=0,
            created_at=_aware(job.created_at).isoformat(),
            updated_at=_aware(job.updated_at).isoformat(),
            supported_actions=[],
            status_reason="Stored job history is unreadable.",
        )


def _global_list_operations(
    services: OperationApiServices,
    cursor: str | None,
    limit: int,
    state: str | None,
    node_id: str | None,
    request_id: str | None,
) -> OperationListPage:
    if services.operation_providers:
        if services.cursor_codec is None:
            raise OperationProjectionError("operation cursor projection unavailable")
        return merge_operation_providers(
            services.operation_providers,
            cursor=cursor,
            limit=limit,
            state=state,
            node_id=node_id,
            request_id=request_id,
            cursors=services.cursor_codec,
        )
    if services.list_operations is None:
        raise OperationProjectionError("operation projection unavailable")
    return services.list_operations(cursor, limit, state, node_id, request_id)


def _global_get_operation(
    services: OperationApiServices, operation_id: str
) -> OperationItem:
    if services.operation_providers:
        return get_operation_from_providers(services.operation_providers, operation_id)
    if services.get_operation is None:
        raise OperationProjectionError("operation projection unavailable")
    return operation_item(services.get_operation(operation_id))


def _required_text(value: object, detail: str) -> str:
    """Read a required persisted string, failing closed on a wrong JSON type."""

    if not isinstance(value, str):
        raise BoundedJSONError(detail)
    return value


def _optional_text(value: object, detail: str) -> str | None:
    """Read an optional persisted string without coercing a wrong JSON type."""

    return None if value is None else _required_text(value, detail)


def _required_bool(value: object, detail: str) -> bool:
    """Read a required persisted boolean, rejecting Python truthiness."""

    if not isinstance(value, bool):
        raise BoundedJSONError(detail)
    return value


def _required_node_ids(value: object) -> list[NodeIdentifier]:
    """Read a persisted node-id array, failing closed on a wrong shape."""

    return [
        _required_text(member, "operation node id is invalid")
        for member in require_sequence(value, "operation node ids are invalid")
    ]


def _result_uncertain(item: OperationItem) -> bool:
    return item.result is not None and item.result.is_uncertain


def _job_operation_response(item: OperationItem) -> JobOperationResponse:
    """Project one durable job operation member."""

    return JobOperationResponse(
        id=item.id,
        node_id=_required_text(item.node_id, "operation node id is invalid"),
        kind=item.kind,
        state=item.state,
        attempt=item.attempt,
        progress=_progress_projection(item.progress, item.state),
        updated_at=item.updated_at,
        failure=_item_failure(item),
        evidence_download=item.evidence_download,
        recovery=recovery_for_operation(
            item.state,
            supported_actions=item.supported_actions,
            available_actions=(OperationRecoveryAction.RESUME,),
            uncertain=_result_uncertain(item),
        ),
    )


def job_response(
    job: Any,
    operation_page: OperationPage,
    *,
    target_cursor: int,
    limit: int,
    cursors: CursorCodec,
    evidence_decorator: Callable[[OperationItem], OperationItem] | None = None,
) -> JobDetailResponse:
    items = [operation_item(row) for row in operation_page.items]
    if evidence_decorator is not None:
        items = [evidence_decorator(item) for item in items]
    projected = [_job_operation_response(item) for item in items]
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
    diagnostics = operation_page.agent_upgrade_diagnostics
    operator_summary = None if diagnostics is None else diagnostics.operator_summary
    return JobDetailResponse(
        id=job.id,
        state=job.state,
        kind=job.kind,
        authority_revision=job.authority_revision,
        targets=visible_targets,
        target_next_cursor=target_next_cursor,
        target_total=len(targets),
        current_attempt=job.current_attempt,
        status_reason=(
            operator_summary if isinstance(operator_summary, str) else job.status_reason
        ),
        operations=projected,
        operation_next_cursor=operation_page.next_cursor,
        operation_total=operation_page.progress.total,
        progress=operation_page.progress,
        agent_upgrade_diagnostics=diagnostics,
        recovery=recovery_for_operation(
            job.state,
            supported_actions=operation_page.recovery_actions,
            available_actions=(OperationRecoveryAction.RESUME,),
        ),
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
        raise CursorError("target cursor is invalid") from None
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise CursorError("target cursor is invalid")
    return offset


def _progress_projection(
    value: object, state: object = None
) -> JobOperationProgress | None:
    if value is None:
        return None
    projected = project_progress(
        value
        if isinstance(value, JobOperationProgress)
        else read_stored_model(JobOperationProgress, value, strict=True)
    )
    if state in {
        "succeeded",
        "accepted",
        "compensated",
        "failed",
        "cancelled",
        *agent_operation_states.PARKED,
    }:
        return projected.model_copy(
            update={
                "activity": None,
                "bytes_per_second": None,
                "smoothed_bytes_per_second": None,
                "eta_seconds": None,
            }
        )
    return projected


def _failure_projection(
    result: OperationResultFacts | None,
) -> OperationFailureEvidence | None:
    """Project a family's stored result into its bounded failure model."""

    return None if result is None else result.failure_evidence()


def _item_failure(item: OperationItem) -> OperationFailure | None:
    """Select the authoritative contract by producer, before union egress."""
    if item.failure_recorded:
        return item.failure
    if item.kind in AGENT_OPERATION_KINDS:
        if item.state not in {"failed", *agent_operation_states.PARKED}:
            return None
        if item.result_unreadable:
            raise ValueError("agent result is not a valid failure receipt")
        parsed = item.agent_receipt
        if parsed is None:
            return None
        if isinstance(parsed, AgentFailureResult):
            return parsed
        # A job process receipt has its own canonical result contract. Its
        # complete manifest remains on the artifact-job result endpoint.
        reason = (
            parsed.reason or f"Artifact process exited with code {parsed.exit_code}"
        )
        return OperationFailureEvidence(
            error_code=OperationFailureCode.ARTIFACT_PROCESS_FAILED,
            summary=reason[:256],
            detail=reason,
        )
    return _failure_projection(item.result)


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _advertised_actions(value: object) -> list[str] | None:
    """The actions a worker advertised; anything but a list of words is none."""

    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return None


def _operation_item(
    operation: AgentOperation, attempt: AgentOperationAttempt | None
) -> OperationItem:
    """Project one durable operation without exposing its unbounded payload."""

    progress = None
    result = None
    if attempt is not None:
        progress = _progress_projection(attempt.progress, operation.state)
        result = attempt.result
    receipt, unreadable = agent_receipt_for(operation.kind, operation.state, result)
    return OperationItem(
        attempt=operation.current_attempt,
        id=operation.id,
        kind=operation.kind,
        node_ids=[operation.node_id],
        parent_id=operation.parent_job_id,
        progress=progress,
        result=None if result is None else OperationResultFacts.model_validate(result),
        agent_receipt=receipt,
        result_unreadable=unreadable,
        supported_actions=(
            _advertised_actions(operation.payload.get("supported_actions"))
            if isinstance(operation.payload, Mapping)
            else None
        ),
        state=operation.state,
        status_reason=operation.status_reason,
        updated_at=_aware(operation.updated_at).isoformat(),
    )


def operation_detail_response(
    row: OperationRow, *, available_actions: object = ()
) -> OperationDetailResponse:
    """Build the bounded generic read representation from a durable projection."""

    item = operation_item(row)
    failure = _item_failure(item)
    return OperationDetailResponse(
        id=item.id,
        parent_id=item.parent_id,
        node_ids=item.node_ids,
        kind=item.kind,
        state=item.state,
        attempt=item.attempt,
        progress=_progress_projection(item.progress, item.state),
        created_at=_required_text(item.created_at, "operation created_at is invalid"),
        updated_at=item.updated_at,
        failure=failure,
        evidence_download=item.evidence_download,
        cancellation=item.cancellation,
        status_reason=item.status_reason,
        recovery=recovery_for_operation(
            item.state,
            supported_actions=item.supported_actions,
            available_actions=available_actions,
            uncertain=bool(failure is not None and getattr(failure, "uncertain", False))
            or _result_uncertain(item),
        ),
        owner=item.owner,
        blockers=item.blockers or [],
        next_attempt_at=item.next_attempt_at,
    )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _agent_upgrade_diagnostics(
    session: Session, job_id: str
) -> AgentUpgradeDiagnosticsResponse | None:
    job = session.get(Job, job_id)
    if job is None or job.kind != "agent-upgrade":
        return None
    payload = mapping(job.payload)
    if payload is None:
        raise BoundedJSONError(
            f"agent upgrade job {job_id} has no package payload document"
        )
    if "package" not in payload or payload["package"] is None:
        # An upgrade that carries no package document simply has no
        # diagnostics to project; only a present but unreadable one fails.
        return None
    try:
        package = read_stored_model(AgentUpgradePackage, payload["package"])
    except (TypeError, ValueError):
        raise BoundedJSONError(
            f"agent upgrade job {job_id} package payload is invalid"
        ) from None
    operations = list(
        session.scalars(
            select(AgentOperation)
            .where(
                AgentOperation.parent_job_id == job_id,
                AgentOperation.node_id.in_(job.targets),
            )
            .order_by(AgentOperation.created_at, AgentOperation.id)
            .limit(64)
        )
    )
    operations_by_node = {operation.node_id: operation for operation in operations}
    attempts = {
        attempt.operation_id: attempt
        for attempt in session.scalars(
            select(AgentOperationAttempt)
            .join(
                AgentOperation,
                AgentOperationAttempt.operation_id == AgentOperation.id,
            )
            .where(
                AgentOperation.parent_job_id == job_id,
                AgentOperation.node_id.in_(job.targets),
                AgentOperationAttempt.attempt == AgentOperation.current_attempt,
            )
            .limit(64)
        )
    }
    nodes = {
        node.node_id: node
        for node in session.scalars(
            select(AgentNode).where(AgentNode.node_id.in_(job.targets))
        )
    }
    targets: list[AgentUpgradeTargetDiagnosticsResponse] = []
    failure_details_unavailable = False
    retry_queued_any = False
    operator_summary = None
    for node_id in job.targets:
        operation = operations_by_node.get(node_id)
        attempt = None if operation is None else attempts.get(operation.id)
        result = None if attempt is None else attempt.result
        raw_reason = None
        if isinstance(result, Mapping):
            candidate = result.get("reason")
            if not isinstance(candidate, str):
                candidate = result.get("error_code")
            if isinstance(candidate, str):
                raw_reason = redact_text(candidate)[:1024]
        node = nodes.get(node_id)
        # The controller only transitions an upgrade operation to succeeded
        # after _contact_proves_target accepts authenticated contact plus the
        # complete runtime and result evidence. Matching digests alone is not
        # the success gate and must never be projected as proof here.
        target_proven = bool(operation is not None and operation.state == "succeeded")
        unresolved_generic = bool(
            not target_proven and raw_reason in GENERIC_AGENT_UPGRADE_REASONS
        )
        failure_details_unavailable = failure_details_unavailable or unresolved_generic
        retry_queued = bool(
            operation is not None and retry_scheduled(operation) is not None
        )
        retry_queued_any = retry_queued_any or retry_queued
        retry_not_before = (
            _aware(attempt.lease_deadline).isoformat()
            if attempt is not None and retry_queued
            else None
        )
        if unresolved_generic and operator_summary is None and raw_reason is not None:
            operator_summary = operator_agent_upgrade_reason(
                node_id=node_id,
                attempt_count=(0 if operation is None else operation.current_attempt),
                package=package.model_dump(mode="json"),
                observed_semantic_version=(
                    None if node is None else node.semantic_version
                ),
                observed_binary_digest=(None if node is None else node.binary_digest),
                observed_build_digest=None if node is None else node.build_digest,
                raw_reason=raw_reason,
                retry_queued=retry_queued,
            )
        targets.append(
            AgentUpgradeTargetDiagnosticsResponse(
                node_id=node_id,
                state="not-started" if operation is None else operation.state,
                attempts=0 if operation is None else operation.current_attempt,
                target_proven=target_proven,
                observed_identity=AgentUpgradeIdentityResponse(
                    version=None if node is None else node.semantic_version,
                    binary_digest=None if node is None else node.binary_digest,
                    build_digest=None if node is None else node.build_digest,
                ),
                raw_reason=raw_reason,
                retry_not_before=retry_not_before,
                retry_queued=retry_queued,
            )
        )
    return AgentUpgradeDiagnosticsResponse(
        expected_identity=AgentUpgradeIdentityResponse(
            version=package.package_version,
            binary_digest=package.target_binary_digest,
            build_digest=package.target_build_digest,
        ),
        targets=targets,
        failure_details_unavailable=failure_details_unavailable,
        next_action=(
            agent_upgrade_next_action(retry_queued=retry_queued_any)
            if any(
                not target.target_proven
                and target.attempts
                and (
                    target.state in agent_operation_states.PARKED
                    or target.raw_reason in RECOVERABLE_AGENT_UPGRADE_REASONS
                )
                for target in targets
            )
            else None
        ),
        operator_summary=operator_summary,
    )


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
        operation_id = raw.operation_id
        match = re.fullmatch(
            r"recipe:([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}):rank:(?:0|[1-9][0-9]*)",
            operation_id,
        )
        return None if match is None else match.group(1)

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

                # A new application or route generation between membership
                # lookup and bundle verification must never authorize a stale
                # endpoint. Fence the projection with a fresh SQL read.
                with self._sessions() as session:
                    current = self._profile_endpoint_intent(session, number)
                    current_snapshot = self._publication_snapshot(session)
                    if (
                        current.profile_id != intent.profile_id
                        or current.application_id != intent.application_id
                        or current.assignments != intent.assignments
                        or current_snapshot != snapshot
                    ):
                        raise RuntimeError(
                            "profile endpoint ownership changed during projection"
                        )

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

    def agents(self) -> Sequence[Mapping[str, object]]:
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
        projected: list[Mapping[str, object]] = []
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
                {
                    "certificate_expires_at": (
                        None if not_after is None else not_after.isoformat()
                    ),
                    "last_seen_age_seconds": age,
                    "last_seen_at": None
                    if last_seen is None
                    else last_seen.isoformat(),
                    "node_id": node.node_id,
                    "protocol_version": node.protocol_version,
                    "semantic_version": node.semantic_version,
                    "build_digest": node.build_digest,
                    "binary_digest": node.binary_digest,
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
                raise CursorError("operation cursor is invalid") from None
        with self._sessions() as session:
            agent_upgrade_diagnostics = _agent_upgrade_diagnostics(session, job_id)
            resume_operation_ids = {
                operation.id
                for operation in operator_resume_eligible_operations_in_session(
                    session, job_id, self._clock()
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
                projected = _progress_projection(progress, operation.state)
                document = (
                    {}
                    if projected is None
                    else projected.model_dump(mode="json", exclude_none=True)
                )
                # A member is one independent node operation. Its identity is
                # the durable operation ID, since a node can have several steps.
                for key in ("checkpoint", "members", "total_bytes_known"):
                    document.pop(key, None)
                document.update(
                    member_id=operation.id,
                    kind=operation.kind,
                    phase=document.get("phase", operation.state),
                    state=operation.state,
                )
                aggregate_members.append(
                    read_stored_model(OperationMemberProgress, document)
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
            _operation_item(operation, attempts.get(operation.id)).model_copy(
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
        terminal = {"succeeded", "accepted", "compensated"}
        failed = {"failed", "uncertain"}
        running = {"queued", "running", "planned", "compensating"}
        return OperationPage(
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
                    session, parent_job_id, self._clock()
                )
            }
        items = [
            self._activity_item(
                row,
                attempts.get(row.id),
                request_id=owners.get(row.parent_job_id),
                resume=row.id in resume_operation_ids,
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
                    session, parent_job_id, self._clock()
                )
            }
        return OperationListPage(
            items=[
                self._activity_item(
                    row,
                    attempts.get(row.id),
                    request_id=owners.get(row.parent_job_id),
                    resume=row.id in resume_operation_ids,
                )
                for row in rows
            ],
            next_cursor=None,
            total=total,
        )

    def get_operation(self, operation_id: str) -> OperationItem:
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
                    session, operation.parent_job_id, self._clock()
                )
            }
            job = session.get(Job, operation.parent_job_id)
            return self._activity_item(
                operation,
                attempt,
                request_id=None if job is None else job.request_id,
                resume=resume,
            )

    @classmethod
    def _activity_item(
        cls,
        operation: AgentOperation,
        attempt: AgentOperationAttempt | None,
        *,
        request_id: str | None,
        resume: bool,
    ) -> OperationItem:
        """One agent operation as a global Activity row, owned by its job."""

        return _operation_item(operation, attempt).model_copy(
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
        raw = _operation_item(operation, attempt).supported_actions
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
            ),
            *operation_providers,
        ),
        cursor_codec=cursors,
        retire_job=retire_job,
        profile_endpoint=(
            projection.profile_endpoint if profile_endpoint_intent is not None else None
        ),
    )


def admin_openapi_schema(app: Any) -> dict[str, object]:
    """Return the deterministic authenticated admin surface without agent APIs."""

    source = deepcopy(app.openapi())
    paths: dict[str, object] = {}
    browser_auth_paths = {
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/session",
        "/api/auth/cli-token",
    }
    for path, path_item in source.get("paths", {}).items():
        if path in {"/api/healthz", "/api/readyz"}:
            continue
        if not path.startswith("/api/"):
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
            if path == "/api/auth/login":
                operation["security"] = []
            elif path in browser_auth_paths:
                operation["security"] = [{"BrowserSession": []}]
            else:
                operation["security"] = [{"BearerAuth": []}, {"BrowserSession": []}]
        paths[path] = selected
    source["paths"] = paths
    components = source.setdefault("components", {})
    components["securitySchemes"] = {"BearerAuth": {"scheme": "bearer", "type": "http"}}
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
    # The lifecycle and error vocabulary is one closed set shared with the agent
    # and the generated clients; no route has to mention a word for it to exist.
    for name, schema in _contract_component_schemas().items():
        if components["schemas"].setdefault(name, schema) != schema:
            raise RuntimeError(
                f"contract component {name} conflicts with a route model"
            )
    stored_schemas, json_columns = _stored_component_schemas()
    for name, schema in stored_schemas.items():
        # A model a route also exposes keeps the route's description of it.
        components["schemas"].setdefault(name, schema)
    components["schemas"] = dict(sorted(components["schemas"].items()))
    source["x-vonk-json-columns"] = json_columns
    return source


def _contract_component_schemas() -> dict[str, dict[str, object]]:
    from pydantic.json_schema import models_json_schema
    from vonk_agent_protocol import (
        ErrorCatalog,
        LifecycleVocabulary,
        ReasonCodeVocabulary,
    )

    from .auth_api import CliTokenDownload

    _references, document = models_json_schema(
        [
            (LifecycleVocabulary, "validation"),
            (ReasonCodeVocabulary, "validation"),
            (ErrorCatalog, "validation"),
            (CliTokenDownload, "validation"),
        ],
        ref_template="#/components/schemas/{model}",
    )
    return deepcopy(document["$defs"])


def _stored_component_schemas() -> tuple[
    dict[str, dict[str, object]], dict[str, list[str]]
]:
    """The contracts of the JSON columns, and where each column's contract is.

    Every contract the JSON-column registry binds is published, so a generated
    client (TypeScript, Python) reads a stored document exactly as the
    Controller does.  They are described as the Controller writes them
    (serialization mode).  The second answer maps ``table.column`` to the
    component names of its contract's models (one per kind for a column that
    stores several document families).
    """

    from pydantic import BaseModel
    from pydantic.json_schema import JsonSchemaMode, models_json_schema

    from .stored_json import bindings, contract_models

    stored = {binding.key: contract_models(binding) for binding in bindings().values()}
    members: list[tuple[type[BaseModel], JsonSchemaMode]] = [
        (model, "serialization") for models in stored.values() for model in models
    ]
    references, document = models_json_schema(
        members, ref_template="#/components/schemas/{model}"
    )
    columns = {
        key: sorted(
            {
                str(references[(model, "serialization")]["$ref"]).rsplit("/", 1)[-1]
                for model in models
            }
        )
        for key, models in sorted(stored.items())
        if models
    }
    return deepcopy(document["$defs"]), columns
