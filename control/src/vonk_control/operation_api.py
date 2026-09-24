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
from sqlalchemy import String, and_, cast, false, func, or_, select, true, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement, SQLColumnExpression
from vonk_agent_protocol import AgentOperation as ProtocolAgentOperation
from vonk_agent_protocol import (
    OperationMemberProgress,
    OperationProgress,
    RecipeJobRunResult,
    canonical_message,
    validate_result_for_operation,
)
from vonk_agent_protocol.contracts import AgentFailureResult
from vonk_agent_protocol.route_activation import ActivationMarker

from .agent_jobs import (
    AgentJobService,
    authorize_operator_resume_in_session,
    operator_resume_eligible_operations_in_session,
    retire_exhausted_operations_in_session,
)
from .agent_upgrade_status import (
    GENERIC_AGENT_UPGRADE_REASONS,
    RECOVERABLE_AGENT_UPGRADE_REASONS,
    agent_upgrade_next_action,
    operator_agent_upgrade_reason,
)
from .auth import CursorCodec, CursorError
from .bounded_json import BoundedJSONError, mapping, require_integer, require_sequence
from .endpoint_contract import EndpointResponse
from .fleet_profile_contract import (
    FleetProfileApplicationCancellationView,
    FleetProfileEndpointAssignmentView,
    FleetProfileEndpointIntent,
    FleetProfileEndpointState,
    FleetProfileEndpointsView,
)
from .logging import redact_text
from .models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    AuditEvent,
    FleetProfileApplication,
    Job,
    ModelCacheOperation,
    RecipeLibrarySyncRun,
    RecipeRouteAuthority,
    RoutePublication,
    RoutePublicationOwner,
)
from .operation_contract import (
    AvailabilityOperationFailure,
    OperationEvidenceDownload,
    OperationEvidenceProvenance,
    OperationFailure,
    OperationFailureEvidence,
    OperationRecovery,
    OperationRecoveryAction,
    recovery_for_operation,
    sanitize_failure_evidence,
)
from .operation_progress import aggregate_progress, project_progress
from .route_runtime import verify_active_route_bundle
from .strict_json import StrictJSONModel

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
    ("get", "/api/artifact-jobs/{job_id}/result"): "getArtifactJobResult",
    (
        "get",
        "/api/artifact-jobs/{job_id}/results/{name}/{sha256}",
    ): "downloadArtifactJobResult",
    ("get", "/api/endpoints/{alias}"): "getPublishedEndpoint",
    ("get", "/api/jobs"): "listJobs",
    ("get", "/api/operations"): "listOperations",
    ("get", "/api/audit"): "listAuditEvents",
    ("get", "/api/identity-history"): "listIdentityHistory",
    ("get", "/api/jobs/{job_id}"): "getJob",
    ("get", "/api/operations/{operation_id}"): "getOperation",
    ("post", "/api/jobs/{job_id}/resume"): "resumeJob",
    ("get", "/api/jobs/{job_id}/logs"): "listJobLogs",
    ("get", "/api/jobs/{job_id}/logs/{digest}"): "getJobLog",
}


def _stored_activation_marker(value: object) -> ActivationMarker:
    try:
        document = canonical_message(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError("durable activation marker is invalid") from error
    try:
        return ActivationMarker.model_validate_json(document)
    except ValidationError as error:
        raise RuntimeError("durable activation marker is invalid") from error


_HTTP_METHODS = frozenset({"delete", "get", "patch", "post", "put"})
BoundedIdentifier = Annotated[str, Field(min_length=1, max_length=128)]
NodeIdentifier = Annotated[str, Field(pattern=NODE_PATTERN)]
DigestIdentifier = Annotated[str, Field(pattern=DIGEST_PATTERN)]


class OperationProjectionError(RuntimeError):
    """Durable operation state cannot be safely projected."""


class EndpointPublicationExpired(RuntimeError):
    """The last durable route publication has reached its lease expiry."""


@dataclass(frozen=True)
class _ActiveRouteSnapshot:
    marker: Mapping[str, object]
    marker_digest: str
    route_digest: str
    evidence_digest: str | None
    litellm_digest: str | None
    bundle_digest: str
    lease_issued_at: datetime
    lease_expires_at: datetime
    authority_id: str
    owner_generation: int
    publication_generation: int
    plan_digest: str


class StrictModel(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)


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


class AuditEventResponse(StrictModel):
    request_id: str = Field(min_length=1, max_length=128)
    actor: str = Field(min_length=1, max_length=128)
    action: str = Field(min_length=1, max_length=128)
    authority_revision: str | None = Field(default=None, max_length=128)
    targets: list[BoundedIdentifier] = Field(max_length=64)
    occurred_at: str | None = Field(default=None, max_length=64)


class AuditResponse(StrictModel):
    events: list[AuditEventResponse] = Field(max_length=100)


class IdentityHistoryItem(StrictModel):
    node_id: str = Field(pattern=NODE_PATTERN)
    agent_state: str = Field(min_length=1, max_length=80)
    certificate_serial: str | None = Field(default=None, max_length=256)
    certificate_fingerprint: str | None = Field(default=None, max_length=256)
    certificate_generation: int | None = Field(default=None, ge=0)
    enrolled_at: datetime | None = None
    revoked_at: datetime | None = None


class IdentityHistoryResponse(StrictModel):
    identities: list[IdentityHistoryItem] = Field(max_length=100)


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
    capabilities: list[str] = Field(max_length=128)
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
    provenance: OperationEvidenceProvenance | None = None
    evidence_download: OperationEvidenceDownload | None = None
    recovery: OperationRecovery | None = None

    @model_serializer(mode="wrap")
    def _serialize_without_unset_evidence(self, handler):
        document = handler(self)
        for key in ("failure", "provenance", "evidence_download", "recovery"):
            if document.get(key) is None:
                document.pop(key, None)
        return document


class OperationOwnerReference(StrictModel):
    """Exact durable owner and original request identity for Activity."""

    kind: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    id: str = Field(min_length=1, max_length=128)
    request_id: str | None = Field(
        default=None,
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        ),
        max_length=36,
    )


class OperationDetailResponse(StrictModel):
    schema_version: Literal[2] = 2
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
    provenance: OperationEvidenceProvenance | None = None
    evidence_download: OperationEvidenceDownload | None = None
    cancellation: FleetProfileApplicationCancellationView | None = None
    recovery: OperationRecovery | None = None
    owner: OperationOwnerReference | None = None
    #: Why this operation is not currently progressing.  A refused claim
    #: records the refusing check here so an operator can tell "no work" apart
    #: from "work this node may not execute, and why".
    status_reason: str | None = Field(default=None, max_length=1024)

    @model_serializer(mode="wrap")
    def _serialize_without_unset_evidence(self, handler):
        document = handler(self)
        for key in (
            "failure",
            "provenance",
            "evidence_download",
            "cancellation",
            "recovery",
            "owner",
            "status_reason",
        ):
            if document.get(key) is None:
                document.pop(key, None)
        return document


class OperationsResponse(StrictModel):
    schema_version: Literal[2] = 2
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


class JobSummary(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    state: str = Field(min_length=1, max_length=80)
    kind: str = Field(min_length=1, max_length=80)
    created_at: datetime


class JobsResponse(StrictModel):
    jobs: list[JobSummary] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=512)
    total: int = Field(ge=0)


class JobLogsResponse(StrictModel):
    job_id: str = Field(min_length=1, max_length=128)
    digests: list[DigestIdentifier] = Field(max_length=100)


@dataclass(frozen=True)
class OperationApiServices:
    """Optional projections backed by accepted durable control state only."""

    endpoint: Callable[[str], Mapping[str, object]]
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
    get_operation: Callable[[str], Mapping[str, object]] | None = None
    operation_providers: tuple[OperationProviderProtocol, ...] = ()
    cursor_codec: CursorCodec | None = None
    retire_job: Callable[[str], None] | None = None
    profile_endpoint: Callable[[int, str | None], FleetProfileEndpointsView] | None = (
        None
    )


@dataclass(frozen=True)
class OperationPage:
    items: Sequence[Mapping[str, object]]
    next_cursor: str | None
    progress: JobProgress
    agent_upgrade_diagnostics: Mapping[str, object] | None = None
    recovery_actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class OperationListPage:
    items: Sequence[Mapping[str, object]]
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
    get_operation: Callable[[str], Mapping[str, object]]
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
    def get_operation(self) -> Callable[[str], Mapping[str, object]]: ...

    @property
    def represented_job_kinds(self) -> frozenset[str]: ...


def _operation_boundary(item: Mapping[str, object]) -> tuple[datetime, str]:
    created_at = item.get("created_at")
    if not isinstance(created_at, str):
        raise OperationProjectionError("operation created_at is invalid")
    try:
        parsed = datetime.fromisoformat(created_at)
    except ValueError:
        raise OperationProjectionError("operation created_at is invalid") from None
    operation_id = item.get("id")
    if not isinstance(operation_id, str) or not operation_id:
        raise OperationProjectionError("operation id is invalid")
    return _aware(parsed), operation_id


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
    rows: list[Mapping[str, object]] = []
    total = 0
    seen: set[str] = set()
    for provider in providers:
        page = provider.list_operations(query)
        total += page.total
        for item in page.items:
            node_ids = item.get("node_ids")
            if not isinstance(node_ids, (list, tuple)) or not all(
                isinstance(node, str) and re.fullmatch(NODE_PATTERN, node)
                for node in node_ids
            ):
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
) -> Mapping[str, object]:
    """Resolve one operation without coupling the Controller to provider modules."""

    match: Mapping[str, object] | None = None
    for provider in providers:
        try:
            item = provider.get_operation(operation_id)
        except KeyError:
            continue
        if match is not None:
            raise OperationProjectionError("operation ids are not globally unique")
        match = item
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
_AUDIT_ACTIVITY_PREFIX = "audit:"


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
            filters.append(Job.state == query.state)
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

    def get_operation(self, operation_id: str) -> Mapping[str, object]:
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

    def _item(self, job: Job) -> Mapping[str, object]:
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
            return self._unreadable_item(job, activity_id, request_id)
        return {
            "id": activity_id,
            "job_id": job.id,
            "parent_id": None,
            "owner": {"kind": "job", "id": job.id, "request_id": request_id},
            "node_ids": node_ids,
            "kind": job.kind,
            "state": job.state,
            "attempt": job.current_attempt,
            "progress": None,
            "created_at": _aware(job.created_at).isoformat(),
            "updated_at": _aware(job.updated_at).isoformat(),
            "supported_actions": [],
            "status_reason": status_reason,
        }

    def _unreadable_item(
        self, job: Job, activity_id: str, request_id: str | None
    ) -> Mapping[str, object]:
        return {
            "id": activity_id,
            "job_id": job.id,
            "parent_id": None,
            "owner": {"kind": "job", "id": job.id, "request_id": request_id},
            "node_ids": [],
            "kind": "job-history-unreadable",
            "state": "unavailable",
            "attempt": 0,
            "progress": None,
            "created_at": _aware(job.created_at).isoformat(),
            "updated_at": _aware(job.updated_at).isoformat(),
            "supported_actions": [],
            "failure": {
                "error_code": "operation_history_unreadable",
                "summary": "Stored job history is malformed",
                "retryable": False,
            },
            "status_reason": "Stored job history is malformed.",
        }


class _AuditActivityProjection:
    """Project orphan audit records without duplicating a durable owner."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @staticmethod
    def _unowned_request_filter() -> ColumnElement[bool]:
        return ~or_(
            select(Job.id).where(Job.request_id == AuditEvent.request_id).exists(),
            select(FleetProfileApplication.id)
            .where(FleetProfileApplication.request_key == AuditEvent.request_id)
            .exists(),
            select(ModelCacheOperation.id)
            .where(ModelCacheOperation.request_key == AuditEvent.request_id)
            .exists(),
            select(RecipeLibrarySyncRun.id)
            .where(RecipeLibrarySyncRun.request_key == AuditEvent.request_id)
            .exists(),
        )

    def _base_filters(self, query: OperationQuery) -> list[ColumnElement[bool]]:
        filters: list[ColumnElement[bool]] = [self._unowned_request_filter()]
        # Audit references describe completed recorded actions.
        if query.state is not None and query.state != "completed":
            filters.append(false())
        if query.request_id is not None:
            filters.append(AuditEvent.request_id == query.request_id)
        if query.node_id is not None:
            filters.append(
                cast(AuditEvent.targets, String).contains(f'"{query.node_id}"')
            )
        return filters

    def list_operations(self, query: OperationQuery) -> OperationListPage:
        if not 1 <= query.limit <= 101:
            raise ValueError("operation provider page limit is invalid")
        base_filters = self._base_filters(query)
        page_filters = list(base_filters)
        boundary = _activity_keyset_filter(
            AuditEvent.occurred_at, AuditEvent.id, _AUDIT_ACTIVITY_PREFIX, query.after
        )
        if boundary is not None:
            page_filters.append(boundary)
        with self._sessions() as session:
            total = int(
                session.scalar(
                    select(func.count()).select_from(AuditEvent).where(*base_filters)
                )
                or 0
            )
            events = session.scalars(
                select(AuditEvent)
                .where(*page_filters)
                .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
                .limit(query.limit)
            )
            return OperationListPage(
                tuple(self._item(event) for event in events), None, total
            )

    def get_operation(self, operation_id: str) -> Mapping[str, object]:
        if not operation_id.startswith(_AUDIT_ACTIVITY_PREFIX):
            raise KeyError(operation_id)
        event_id = operation_id[len(_AUDIT_ACTIVITY_PREFIX) :]
        with self._sessions() as session:
            event = session.get(AuditEvent, event_id)
            if event is None:
                raise KeyError(operation_id)
            # A request with a durable operation owner is shown through that
            # exact owner and is not repeated as an audit-only Activity row.
            if not session.scalar(
                select(AuditEvent.id)
                .where(
                    AuditEvent.id == event_id,
                    self._unowned_request_filter(),
                )
                .limit(1)
            ):
                raise KeyError(operation_id)
            return self._item(event)

    def _item(self, event: AuditEvent) -> Mapping[str, object]:
        activity_id = f"{_AUDIT_ACTIVITY_PREFIX}{event.id}"
        request_id = _activity_owner_request_id(event.request_id)
        try:
            if (
                not isinstance(event.id, str)
                or not 1 <= len(activity_id) <= 128
                or request_id is None
                or not isinstance(event.actor, str)
                or not 1 <= len(event.actor) <= 200
                or not isinstance(event.action, str)
                or not 1 <= len(event.action) <= 120
                or not isinstance(event.occurred_at, datetime)
            ):
                raise ValueError("stored audit activity is malformed")
            node_ids = _activity_node_ids(event.targets, limit=64)
        except (AttributeError, TypeError, ValueError, ValidationError):
            return self._unreadable_item(event, activity_id, request_id)
        action = (
            event.action
            if re.fullmatch(r"[a-z][a-z0-9._-]{0,73}", event.action)
            else "event"
        )
        return {
            "id": activity_id,
            "job_id": None,
            "parent_id": None,
            "owner": {
                "kind": "audit-event",
                "id": event.id,
                "request_id": request_id,
            },
            "node_ids": node_ids,
            "kind": f"audit.{action}",
            "state": "completed",
            "attempt": 0,
            "progress": None,
            "created_at": _aware(event.occurred_at).isoformat(),
            "updated_at": _aware(event.occurred_at).isoformat(),
            "supported_actions": [],
            "status_reason": f"Audited request {request_id}",
        }

    def _unreadable_item(
        self, event: AuditEvent, activity_id: str, request_id: str | None
    ) -> Mapping[str, object]:
        return {
            "id": activity_id,
            "job_id": None,
            "parent_id": None,
            "owner": {
                "kind": "audit-event",
                "id": event.id,
                "request_id": request_id,
            },
            "node_ids": [],
            "kind": "audit-history-unreadable",
            "state": "unavailable",
            "attempt": 0,
            "progress": None,
            "created_at": _aware(event.occurred_at).isoformat(),
            "updated_at": _aware(event.occurred_at).isoformat(),
            "supported_actions": [],
            "failure": {
                "error_code": "operation_history_unreadable",
                "summary": "Stored audit history is malformed",
                "retryable": False,
            },
            "status_reason": "Stored audit history is malformed.",
        }


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
) -> Mapping[str, object]:
    if services.operation_providers:
        return get_operation_from_providers(services.operation_providers, operation_id)
    if services.get_operation is None:
        raise OperationProjectionError("operation projection unavailable")
    return services.get_operation(operation_id)


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


def _job_operation_response(item: Mapping[str, object]) -> JobOperationResponse:
    """Project one durable job operation member from its decoded JSON row."""

    state = _required_text(item["state"], "operation state is invalid")
    result = item.get("result")
    operation_id = _required_text(item["id"], "operation id is invalid")
    return JobOperationResponse(
        id=operation_id,
        node_id=_required_text(item["node_id"], "operation node id is invalid"),
        kind=_required_text(item["kind"], "operation kind is invalid"),
        state=state,
        attempt=require_integer(item["attempt"], "operation attempt is invalid"),
        progress=_progress_projection(item.get("progress"), state),
        updated_at=_optional_text(
            item.get("updated_at"), "operation updated_at is invalid"
        ),
        failure=_item_failure(item),
        provenance=_provenance_projection(result, operation_id),
        evidence_download=_evidence_download_projection(result, operation_id),
        recovery=recovery_for_operation(
            state,
            supported_actions=item.get("supported_actions"),
            available_actions=(OperationRecoveryAction.RESUME,),
            uncertain=bool(
                isinstance(result, Mapping) and result.get("uncertain") is True
            ),
        ),
    )


def job_response(
    job: Any,
    operation_page: OperationPage,
    *,
    target_cursor: int,
    limit: int,
    cursors: CursorCodec,
    evidence_decorator: Callable[[Mapping[str, object]], Mapping[str, object]]
    | None = None,
) -> JobDetailResponse:
    items = (
        [evidence_decorator(item) for item in operation_page.items]
        if evidence_decorator is not None
        else operation_page.items
    )
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
    operator_summary = (
        None if diagnostics is None else diagnostics.get("operator_summary")
    )
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
        agent_upgrade_diagnostics=(
            None
            if diagnostics is None
            else AgentUpgradeDiagnosticsResponse.model_validate(diagnostics)
        ),
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
        JobOperationProgress.model_validate(value, strict=True)
    )
    if state in {
        "succeeded",
        "accepted",
        "compensated",
        "failed",
        "cancelled",
        "waiting-for-operator",
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


def _progress_document(value: object, state: object = None) -> dict[str, object] | None:
    """Project one durable progress value with a single canonical parse."""

    projected = _progress_projection(value, state)
    return None if projected is None else projected.model_dump(mode="json")


def _failure_projection(value: object) -> OperationFailureEvidence | None:
    """Project Controller-owned result metadata into its bounded failure model."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("operation result must be a JSON object")
    raw = value.get("failure", value)
    if not isinstance(raw, Mapping):
        raise TypeError("operation failure must be a JSON object")
    if "error_code" not in raw:
        return None
    safe = sanitize_failure_evidence(raw)
    summary = safe.get("summary") or safe.get("reason") or safe["error_code"]
    return OperationFailureEvidence(
        error_code=_required_text(safe["error_code"], "failure error code is invalid"),
        summary=_required_text(summary, "failure summary is invalid")[:256],
        detail=_optional_text(safe.get("detail"), "failure detail is invalid"),
        retryable=_required_bool(
            safe.get("retryable", False), "failure retryable is invalid"
        ),
        uncertain=_required_bool(
            safe.get("uncertain", False), "failure uncertain is invalid"
        ),
    )


def _item_failure(item: Mapping[str, object]) -> OperationFailure | None:
    """Select the authoritative contract by producer, before union egress."""
    if "failure" in item:
        value = item["failure"]
        if value is None:
            return None
        kind = _required_text(item["kind"], "operation kind is invalid")
        if kind.startswith("model-cache.") or kind == "recipe.cache.update.v2":
            return AvailabilityOperationFailure.model_validate(value)
        return OperationFailureEvidence.model_validate(value, strict=True)
    kind = _required_text(item["kind"], "operation kind is invalid")
    state = _required_text(item["state"], "operation state is invalid")
    if kind in {operation.value for operation in ProtocolAgentOperation}:
        if (
            state not in {"failed", "waiting-for-operator"}
            or item.get("result") is None
        ):
            return None
        value = item["result"]
        if not isinstance(value, Mapping):
            raise ValueError("agent result must be a JSON object")
        # The evidence collector adds these separate, typed read decorations.
        result = {
            key: child
            for key, child in value.items()
            if key not in {"provenance", "evidence_download"}
        }
        parsed = validate_result_for_operation(kind, result, state=state)
        if isinstance(parsed, AgentFailureResult):
            return parsed
        # A job process receipt has its own canonical result contract. Its
        # complete manifest remains on the artifact-job result endpoint.
        if not isinstance(parsed, RecipeJobRunResult):
            raise ValueError("agent result is not a failure receipt")
        reason = (
            parsed.reason or f"Artifact process exited with code {parsed.exit_code}"
        )
        return OperationFailureEvidence(
            error_code="artifact_process_failed", summary=reason[:256], detail=reason
        )
    return _failure_projection(item.get("result"))


def _provenance_projection(
    value: object, operation_id: str
) -> OperationEvidenceProvenance | None:
    """Project stored evidence provenance, keeping absence distinct.

    A missing key or an explicit ``null`` means no provenance was attached.
    A present value that is not the canonical document is corruption and must
    not be reported as absent. The failure names the operation, because the
    route that reports it serves a whole list.
    """

    if not isinstance(value, Mapping) or "provenance" not in value:
        return None
    stored = value["provenance"]
    if stored is None:
        return None
    detail = f"stored provenance for operation {operation_id} is invalid"
    if not isinstance(stored, Mapping):
        raise BoundedJSONError(detail)
    try:
        return OperationEvidenceProvenance.model_validate(stored, strict=True)
    except ValidationError as error:
        raise BoundedJSONError(detail) from error


def _evidence_download_projection(
    value: object, operation_id: str
) -> OperationEvidenceDownload | None:
    """Project the stored evidence download, keeping absence distinct.

    A missing key or an explicit ``null`` means no download was attached. A
    present value that is not the canonical document is corruption and must
    not be reported as absent. As above, the failure names the operation.
    """

    if not isinstance(value, Mapping) or "evidence_download" not in value:
        return None
    stored = value["evidence_download"]
    if stored is None:
        return None
    detail = f"stored evidence download for operation {operation_id} is invalid"
    if not isinstance(stored, Mapping):
        raise BoundedJSONError(detail)
    try:
        return OperationEvidenceDownload.model_validate(stored, strict=True)
    except ValidationError as error:
        raise BoundedJSONError(detail) from error


def _operation_item(
    operation: AgentOperation, attempt: AgentOperationAttempt | None
) -> dict[str, object]:
    """Project one durable operation without exposing its unbounded payload."""

    progress = None
    result = None
    if attempt is not None:
        projected = _progress_projection(attempt.progress, operation.state)
        progress = None if projected is None else projected.model_dump(mode="json")
        result = attempt.result
    return {
        "attempt": operation.current_attempt,
        "id": operation.id,
        "kind": operation.kind,
        "node_ids": [operation.node_id],
        "parent_id": operation.parent_job_id,
        "progress": progress,
        "result": result,
        "supported_actions": (
            operation.payload.get("supported_actions")
            if isinstance(operation.payload, Mapping)
            else None
        ),
        "state": operation.state,
        "status_reason": operation.status_reason,
        "updated_at": _aware(operation.updated_at).isoformat(),
    }


def operation_detail_response(
    item: Mapping[str, object], *, available_actions: object = ()
) -> OperationDetailResponse:
    """Build the bounded generic read representation from a durable projection."""

    failure = _item_failure(item)
    state = _required_text(item["state"], "operation state is invalid")
    result = item.get("result")
    operation_id = _required_text(item["id"], "operation id is invalid")
    cancellation = None
    raw_cancellation = item.get("cancellation")
    if raw_cancellation is not None:
        try:
            cancellation = FleetProfileApplicationCancellationView.model_validate_json(
                canonical_message(raw_cancellation), strict=True
            )
        except (TypeError, ValueError, ValidationError) as error:
            raise BoundedJSONError(
                f"profile cancellation receipt for operation {operation_id} is invalid"
            ) from error
    owner: OperationOwnerReference | None = None
    raw_owner = item.get("owner")
    if isinstance(raw_owner, Mapping):
        try:
            owner = OperationOwnerReference.model_validate(raw_owner)
        except ValidationError:
            # Keep one damaged historical owner visible without turning the
            # rest of the canonical page into an unavailable response.
            owner = None
    return OperationDetailResponse(
        id=operation_id,
        parent_id=_optional_text(
            item.get("parent_id"), "operation parent id is invalid"
        ),
        node_ids=_required_node_ids(item["node_ids"]),
        kind=_required_text(item["kind"], "operation kind is invalid"),
        state=state,
        attempt=require_integer(item["attempt"], "operation attempt is invalid"),
        progress=_progress_projection(item.get("progress"), state),
        created_at=_required_text(
            item["created_at"], "operation created_at is invalid"
        ),
        updated_at=_optional_text(
            item.get("updated_at"), "operation updated_at is invalid"
        ),
        failure=failure,
        provenance=_provenance_projection(result, operation_id),
        evidence_download=_evidence_download_projection(result, operation_id),
        cancellation=cancellation,
        status_reason=_optional_text(
            item.get("status_reason"), "operation status reason is invalid"
        ),
        recovery=recovery_for_operation(
            state,
            supported_actions=item.get("supported_actions"),
            available_actions=available_actions,
            uncertain=bool(failure is not None and getattr(failure, "uncertain", False))
            or bool(isinstance(result, Mapping) and result.get("uncertain") is True),
        ),
        owner=owner,
    )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _agent_upgrade_diagnostics(
    session: Session, job_id: str
) -> Mapping[str, object] | None:
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
    package = payload["package"]
    if not isinstance(package, Mapping):
        raise BoundedJSONError(f"agent upgrade job {job_id} package payload is invalid")
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
    expected_binary = package.get("target_binary_digest")
    expected_build = package.get("target_build_digest")
    targets: list[dict[str, object]] = []
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
            operation is not None
            and operation.retry_disposition == "retry"
            and operation.retry_disposition_attempt == operation.current_attempt
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
                package=package,
                observed_semantic_version=(
                    None if node is None else node.semantic_version
                ),
                observed_binary_digest=(None if node is None else node.binary_digest),
                observed_build_digest=None if node is None else node.build_digest,
                raw_reason=raw_reason,
                retry_queued=retry_queued,
            )
        targets.append(
            {
                "node_id": node_id,
                "state": "not-started" if operation is None else operation.state,
                "attempts": 0 if operation is None else operation.current_attempt,
                "target_proven": target_proven,
                "observed_identity": {
                    "version": None if node is None else node.semantic_version,
                    "binary_digest": None if node is None else node.binary_digest,
                    "build_digest": None if node is None else node.build_digest,
                },
                "raw_reason": raw_reason,
                "retry_not_before": retry_not_before,
                "retry_queued": retry_queued,
            }
        )
    return {
        "expected_identity": {
            "version": package.get("package_version"),
            "binary_digest": expected_binary,
            "build_digest": expected_build,
        },
        "targets": targets,
        "failure_details_unavailable": failure_details_unavailable,
        "next_action": (
            agent_upgrade_next_action(retry_queued=retry_queued_any)
            if any(
                not target["target_proven"]
                and target["attempts"]
                and (
                    target["state"] == "waiting-for-operator"
                    or target["raw_reason"] in RECOVERABLE_AGENT_UPGRADE_REASONS
                )
                for target in targets
            )
            else None
        ),
        "operator_summary": operator_summary,
    }


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
            owner is not None
            and publication is not None
            and publication.lease_expires_at is not None
            and _aware(publication.lease_expires_at) <= _aware(self._clock())
        ):
            raise EndpointPublicationExpired("active route lease expired")
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
            or publication.lease_issued_at is None
            or publication.lease_expires_at is None
            or publication.evidence_digest is None
            or publication.litellm_digest is None
            or publication.bundle_digest is None
        ):
            raise RuntimeError("active publication is unavailable")
        marker = _stored_activation_marker(publication.activation_marker).model_dump()
        return _ActiveRouteSnapshot(
            marker=marker,
            marker_digest=publication.activation_marker_digest,
            route_digest=publication.route_digest,
            evidence_digest=publication.evidence_digest,
            litellm_digest=publication.litellm_digest,
            bundle_digest=publication.bundle_digest,
            lease_issued_at=_aware(publication.lease_issued_at),
            lease_expires_at=_aware(publication.lease_expires_at),
            authority_id=owner.authority_id,
            owner_generation=owner.owner_generation,
            publication_generation=publication.generation,
            plan_digest=publication.plan_digest,
        )

    def _verified_routes(
        self, snapshot: _ActiveRouteSnapshot
    ) -> tuple[Mapping[str, object], Mapping[str, object]]:
        bundle = verify_active_route_bundle(self._route_root, clock=self._clock)
        active_marker = bundle.marker
        if (
            active_marker.model_dump() != snapshot.marker
            or active_marker.digest != snapshot.marker_digest
            or active_marker.state != "published"
            or active_marker.authority_id != snapshot.authority_id
            or active_marker.plan_digest != snapshot.plan_digest
            or active_marker.generation != snapshot.publication_generation
            or active_marker.generation != snapshot.owner_generation
            or active_marker.evidence_set_digest != snapshot.evidence_digest
            or active_marker.routes_sha256 != snapshot.route_digest
            or active_marker.litellm_sha256 != snapshot.litellm_digest
            or active_marker.manifest_sha256 != snapshot.bundle_digest
            or _aware(snapshot.lease_issued_at)
            != _aware(datetime.fromisoformat(active_marker.issued_at))
            or _aware(snapshot.lease_expires_at)
            != _aware(datetime.fromisoformat(active_marker.expires_at))
        ):
            raise RuntimeError("activation marker does not match durable state")
        routes = bundle.routes
        route_document = routes.get("routes")
        if (
            routes.get("generation") != snapshot.publication_generation
            or routes.get("state") != "published"
            or not isinstance(route_document, Mapping)
        ):
            raise RuntimeError("active route state does not match publication")
        return active_marker.model_dump(), route_document

    @staticmethod
    def _endpoint_payload(
        alias: str,
        raw: Mapping[str, object],
        active_marker: Mapping[str, object],
    ) -> EndpointResponse:
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
        expires_at = active_marker.get("expires_at")
        generation = active_marker.get("generation")
        plan_digest = active_marker.get("plan_digest")
        if (
            not isinstance(expires_at, str)
            or not isinstance(generation, int)
            or not isinstance(plan_digest, str)
        ):
            raise TypeError("active endpoint marker is invalid")
        return EndpointResponse(
            alias=alias,
            api_base=f"{scheme}://{address}:{port}{path.rstrip('/')}",
            expires_at=expires_at,
            generation=generation,
            node_id=node_id,
            observed_at=observed_at,
            plan_digest=plan_digest,
            state="published",
        )

    @staticmethod
    def _route_run_id(raw: Mapping[str, object]) -> str | None:
        operation_id = raw.get("operation_id")
        if not isinstance(operation_id, str):
            return None
        match = re.fullmatch(
            r"recipe:([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}):rank:(?:0|[1-9][0-9]*)",
            operation_id,
        )
        return None if match is None else match.group(1)

    def endpoint(self, alias: str) -> Mapping[str, object]:
        with self._sessions() as session:
            snapshot = self._publication_snapshot(session)
        active_marker, route_document = self._verified_routes(snapshot)
        raw = route_document.get(alias)
        if raw is None:
            raise KeyError(alias)
        if not isinstance(raw, Mapping):
            raise OperationProjectionError("active endpoint is invalid")
        return self._endpoint_payload(alias, raw, active_marker).model_dump(mode="json")

    def profile_endpoint(
        self, number: int, alias: str | None
    ) -> FleetProfileEndpointsView:
        if self._profile_endpoint_intent is None:
            raise RuntimeError("profile endpoint ownership is unavailable")
        with self._sessions() as session:
            intent = self._profile_endpoint_intent(session, number)
            assignments = intent.assignments
            if alias is not None:
                assignments = tuple(item for item in assignments if item.alias == alias)
                if not assignments:
                    raise KeyError(alias)
            snapshot: _ActiveRouteSnapshot | None = None
            expired = False
            unavailable = False
            if any(item.expected_run_id is not None for item in assignments):
                try:
                    snapshot = self._publication_snapshot(session)
                except EndpointPublicationExpired:
                    expired = True
                except (OSError, RuntimeError, TypeError, ValueError):
                    unavailable = True

        endpoints: dict[str, EndpointResponse] = {}
        states: dict[str, FleetProfileEndpointState] = {
            item.assignment_id: item.state for item in assignments
        }
        if expired:
            for item in assignments:
                if item.expected_run_id is not None:
                    states[item.assignment_id] = "expired"
        elif unavailable:
            for item in assignments:
                if item.expected_run_id is not None:
                    states[item.assignment_id] = "unavailable"
        elif snapshot is not None:
            try:
                active_marker, route_document = self._verified_routes(snapshot)
            except (OSError, RuntimeError, TypeError, ValueError):
                for item in assignments:
                    if item.expected_run_id is not None:
                        states[item.assignment_id] = "unavailable"
            else:
                for item in assignments:
                    if item.expected_run_id is None or item.alias is None:
                        continue
                    raw = route_document.get(item.alias)
                    if not isinstance(raw, Mapping):
                        states[item.assignment_id] = "withdrawn"
                        continue
                    route_run_id = self._route_run_id(raw)
                    if route_run_id is None:
                        states[item.assignment_id] = "unavailable"
                        continue
                    if route_run_id != item.expected_run_id:
                        states[item.assignment_id] = "withdrawn"
                        continue
                    try:
                        endpoints[item.assignment_id] = self._endpoint_payload(
                            item.alias, raw, active_marker
                        )
                    except (RuntimeError, TypeError, ValueError):
                        states[item.assignment_id] = "unavailable"
                    else:
                        states[item.assignment_id] = "published"

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
                    "capabilities": [
                        capability[:80]
                        for capability in node.capabilities[:64]
                        if isinstance(capability, str)
                    ],
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
                    OperationMemberProgress.model_validate(document)
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
            {
                "attempt": operation.current_attempt,
                "id": operation.id,
                "kind": operation.kind,
                "node_id": operation.node_id,
                "progress": (
                    None
                    if attempts.get(operation.id) is None
                    else _progress_document(
                        attempts[operation.id].progress, operation.state
                    )
                ),
                "result": (
                    None
                    if attempts.get(operation.id) is None
                    else attempts[operation.id].result
                ),
                "supported_actions": self._activity_actions(
                    operation,
                    attempts.get(operation.id),
                    resume=operation.id in resume_operation_ids,
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
                filters.append(AgentOperation.state == state)
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
                total_filters.append(AgentOperation.state == state)
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
            {
                **_operation_item(row, attempts.get(row.id)),
                "created_at": _aware(row.created_at).isoformat(),
                "job_id": row.parent_job_id,
                "supported_actions": self._activity_actions(
                    row,
                    attempts.get(row.id),
                    resume=row.id in resume_operation_ids,
                ),
                "owner": {
                    "kind": "job",
                    "id": row.parent_job_id,
                    "request_id": owners.get(row.parent_job_id),
                },
            }
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
            filters.append(AgentOperation.state == query.state)
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
                total_filters.append(AgentOperation.state == query.state)
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
                {
                    **_operation_item(row, attempts.get(row.id)),
                    "created_at": _aware(row.created_at).isoformat(),
                    "job_id": row.parent_job_id,
                    "supported_actions": self._activity_actions(
                        row,
                        attempts.get(row.id),
                        resume=row.id in resume_operation_ids,
                    ),
                    "owner": {
                        "kind": "job",
                        "id": row.parent_job_id,
                        "request_id": owners.get(row.parent_job_id),
                    },
                }
                for row in rows
            ],
            next_cursor=None,
            total=total,
        )

    def get_operation(self, operation_id: str) -> Mapping[str, object]:
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
            return {
                **_operation_item(operation, attempt),
                "created_at": _aware(operation.created_at).isoformat(),
                "job_id": operation.parent_job_id,
                "supported_actions": self._activity_actions(
                    operation, attempt, resume=resume
                ),
                "owner": {
                    "kind": "job",
                    "id": operation.parent_job_id,
                    "request_id": (
                        None
                        if (job := session.get(Job, operation.parent_job_id)) is None
                        else job.request_id
                    ),
                },
            }

    @staticmethod
    def _activity_actions(
        operation: AgentOperation,
        attempt: AgentOperationAttempt | None,
        *,
        resume: bool,
    ) -> list[str] | None:
        raw = _operation_item(operation, attempt).get("supported_actions")
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
            if job.state != "waiting-for-operator":
                raise ValueError("job is not waiting for operator")
            scope = AgentJobService._target_scope(job.targets)
            if scope is None or not AgentJobService._lock_target_scopes(
                session, {"resume": (job_id, scope)}, scope[0]
            ):
                raise ValueError("job target scope changed")
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
            if result.rowcount != 1:
                raise ValueError("job is not waiting for operator")
            # The parent transition alone does not release the parked child:
            # the claim predicate requires the operation's own retry
            # authorisation.  It is written in this same transaction so an
            # exhausted budget rolls the parent transition back and no
            # half-queued job that can never be claimed is committed.
            authorize_operator_resume_in_session(session, job_id, now)
            job.state = "queued"
            job.status_reason = None
            job.updated_at = now

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
    audit_events = _AuditActivityProjection(sessions)

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
        endpoint=projection.endpoint,
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
                family="audit-event",
                list_operations=audit_events.list_operations,
                get_operation=audit_events.get_operation,
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
    return source
