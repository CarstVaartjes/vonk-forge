"""Operation Api: contracts."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from types import UnionType
from typing import Annotated, Literal, Protocol

from pydantic import Field, model_serializer
from vonk_agent_protocol import OperationProgress, UnknownOutcomeError
from vonk_agent_protocol.http_failure import HttpFailureResponse

from ..auth import CursorCodec
from ..fleet_profile_contract import (
    FleetProfileApplicationCancellationView,
    FleetProfileEndpointsView,
)
from ..integer_domains import MAX_DATABASE_INTEGER
from ..model_cache_contract import ModelCacheCancellation
from ..operation_blockers import OperationBlocker
from ..operation_contract import (
    OperationEvidenceDownload,
    OperationFailure,
    OperationRecovery,
)
from ..operation_item_contract import OperationOwnerReference, OperationRow
from ..strict_json import StrictModel
from .constants import DIGEST_PATTERN, NODE_PATTERN, BoundedIdentifier, NodeIdentifier


class OperationProjectionError(UnknownOutcomeError):
    """Durable operation state cannot be safely projected."""


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
    outcome: HttpFailureResponse | None = None


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


def bounded_error_responses(
    *status_codes: int,
) -> dict[
    int | str, dict[str, type[StrictModel] | UnionType | dict[str, dict[str, str]]]
]:
    from ..capability_contract import CapabilityUnavailableReply

    return {
        status_code: {
            "model": RequestValidationProblem
            if status_code == 422
            else BoundedErrorResponse | CapabilityUnavailableReply
            if status_code == 503
            else BoundedErrorResponse,
            "content": {"application/json": {}},
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
    attempt: int = Field(le=MAX_DATABASE_INTEGER, ge=0)
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


class OperationProjectionIssue(StrictModel):
    """One optional fact unavailable within this response's reader allocation."""

    field: Literal["progress", "cancellation"]
    reason: Literal["response-budget-exceeded"] = "response-budget-exceeded"
    observed_bytes: int = Field(ge=1)
    budget_bytes: int = Field(ge=1)


class OperationDetailResponse(StrictModel):
    # One issue per optional fact: progress and cancellation are the two owners.
    projection_issues: list[OperationProjectionIssue] | None = Field(
        default=None, max_length=2
    )
    id: str = Field(min_length=1, max_length=128)
    parent_id: str | None = Field(default=None, max_length=128)
    node_ids: list[NodeIdentifier] = Field(max_length=1024)
    kind: str = Field(min_length=1, max_length=80)
    state: str = Field(min_length=1, max_length=80)
    attempt: int = Field(le=MAX_DATABASE_INTEGER, ge=0)
    progress: JobOperationProgress | None = None
    created_at: str | None = Field(default=None, min_length=1, max_length=64)
    observation_unavailable: bool = False
    updated_at: str | None = None
    failure: OperationFailure | None = None
    evidence_download: OperationEvidenceDownload | None = None
    cancellation: FleetProfileApplicationCancellationView | None = None
    model_cache_cancellation: ModelCacheCancellation | None = None
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
            "projection_issues",
            "failure",
            "evidence_download",
            "cancellation",
            "model_cache_cancellation",
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
    operations: list[OperationDetailResponse] | None = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=512)
    total: int | None = Field(ge=0)
    projection_issue: str | None = Field(default=None, max_length=256)
    continuation_unavailable: bool = False


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
    current_attempt: int = Field(le=MAX_DATABASE_INTEGER, ge=0)
    status_reason: str | None = Field(default=None, max_length=1024)
    operations: list[JobOperationResponse] | None = Field(max_length=100)
    operation_next_cursor: str | None = Field(default=None, max_length=512)
    operation_total: int | None = Field(ge=0)
    progress: JobProgress | None
    projection_issue: str | None = Field(default=None, max_length=256)
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

    agents: Callable[[], Sequence[AgentSummary]]
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
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


@dataclass(frozen=True)
class OperationPage:
    items: Sequence[OperationRow]
    next_cursor: str | None
    progress: JobProgress
    agent_upgrade_diagnostics: AgentUpgradeDiagnosticsResponse | None = None
    recovery_actions: tuple[str, ...] = ()
    # Request-local freshness cutoff, not persisted state or response JSON.
    projected_at: datetime = dataclass_field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class OperationListPage:
    items: Sequence[OperationRow]
    next_cursor: str | None
    total: int | None
    projection_issue: str | None = None
    continuation_unavailable: bool = False


@dataclass(frozen=True)
class OperationQuery:
    """Shared boundary query understood by every global activity provider."""

    after: tuple[datetime, str] | None
    limit: int
    state: str | None
    node_id: str | None
    request_id: str | None = None
    projected_at: datetime | None = None


@dataclass(frozen=True)
class OperationProvider:
    """Composable typed operation family for the global activity projection."""

    family: str
    list_operations: Callable[[OperationQuery], OperationListPage]
    get_operation: Callable[[str], OperationRow]
    represented_job_kinds: frozenset[str] = frozenset()
    # Optional explicit read context; existing non-Agent getters stay one-argument.
    get_operation_at: Callable[[str, datetime], OperationRow] | None = None


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
