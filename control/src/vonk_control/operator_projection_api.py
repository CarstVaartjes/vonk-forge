"""Singular operator API for Fleet, Model and Recipe projections.

The machine-facing agent transport is installed separately.  This module only
exposes authenticated operator reads and thin action adapters; action adapters
must call the existing Controller authorities and may not manufacture remote
state or log evidence.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, Protocol

from fastapi import FastAPI, HTTPException, Path, Query, status
from pydantic import ConfigDict, Field, model_serializer
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from starlette.responses import Response
from vonk_agent_protocol import (
    ControllerErrorCode,
    ObservationCause,
    SecurityRefusalReason,
)

from . import agent_operation_states
from .agent_api import AgentApiServices, EnrollmentGrantResponse
from .agent_upgrade_contract import AgentUpgradePackage, AgentUpgradeRequestIntent
from .agent_upgrades import AgentUpgradeConflict, AgentUpgradeService
from .auth import MUTATION_ROLES, Actor, CursorError
from .bounded_json import BoundedJSONError
from .enrollment import (
    MAX_ENROLLMENT_GRANT_TTL_SECONDS,
    EnrollmentDenied,
    RemoteRevocationUncertain,
)
from .enrollment_bootstrap import accepted_installer_url
from .enrollment_contract import (
    ENROLLMENT_ID_PATTERN,
    EnrollmentGrantStatus,
    EnrollmentId,
)
from .failure_evidence import (
    AttemptPhase,
    FailedAttempt,
    FailureEvidenceBundle,
    collect_failure,
    failed_attempt_condition,
)
from .fleet_projection import (
    FleetNode,
    FleetNodeIdentity,
    FleetSnapshot,
)
from .library_projection import LibrarySelectorAmbiguous
from .logging import current_request_id, log_event, redact_text
from .models import AgentOperation, AgentOperationAttempt
from .observation_transfer import (
    ObservationTransferRecord,
    ObservationTransferResponse,
    observation_openapi,
    observation_response,
)
from .operation_api import bounded_error_responses
from .operation_item_contract import OperationResultFacts
from .platform_observation_errors import (
    ObservationCaptureUnavailable,
    observation_capture_unavailable_response,
)
from .request_fault import RequestFault
from .strict_json import StrictJSONModel, stored_document_detail

_LOGGER = logging.getLogger(__name__)

_NODE_PATTERN = r"^spk_[0-9a-f]{32}$"
LogSource = Literal["client", "monitor", "runtime", "job"]
LogLevel = Literal["debug", "info", "warning", "error"]
_SELECTOR_PATTERN = r"^[^\x00-\x1f\x7f]{1,256}$"

FLEET_OPERATION_IDS = {
    ("get", "/api/fleet"): "getFleetStatus",
    ("get", "/api/fleet/locks"): "getFleetAdmissionLocks",
    ("get", "/api/fleet/{selector}"): "getFleetNode",
    ("get", "/api/fleet/{selector}/loginfo"): "getFleetLogInfo",
    ("post", "/api/fleet/{selector}/rename"): "renameFleetNode",
    ("post", "/api/fleet/enroll"): "enrollFleetNode",
    ("post", "/api/fleet/{selector}/re-enroll"): "reenrollFleetNode",
    ("get", "/api/fleet/enrollments/{grant_id}"): "getFleetEnrollment",
    ("post", "/api/fleet/enrollments/{grant_id}/revoke"): "revokeFleetEnrollment",
    ("post", "/api/fleet/{selector}/remove"): "removeFleetNode",
    ("post", "/api/fleet/upgrade"): "upgradeFleet",
}


class FleetRenameRequest(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    display_name: str = Field(
        min_length=1, max_length=80, pattern=r"^[^\x00-\x1f\x7f]+$"
    )


class FleetEnrollRequest(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=80, pattern=r"^[^\x00-\x1f\x7f]+$")
    request_key: EnrollmentId


class FleetReenrollRequest(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_key: EnrollmentId


class FleetUpgradeRequest(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_key: EnrollmentId
    selectors: list[str] | None = Field(default=None, min_length=1, max_length=64)
    all: bool = False


class FleetActionResponse(StrictJSONModel):
    action: Literal["enroll", "re-enroll", "remove", "upgrade"]
    state: str = Field(min_length=1, max_length=32)
    operation_id: str | None = Field(default=None, max_length=128)
    node_id: str | None = Field(default=None, pattern=_NODE_PATTERN)
    display_name: str | None = Field(default=None, max_length=80)
    plan_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    request_key: EnrollmentId | None = None
    targets: list[str] = Field(default_factory=list, max_length=64)
    grant: EnrollmentGrantResponse | None = None
    detail: str | None = Field(default=None, max_length=256)

    @model_serializer(mode="wrap")
    def _omit_unused_request_key(self, handler):
        document = handler(self)
        if self.request_key is None:
            document.pop("request_key", None)
        return document


class FleetLockHolder(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    node_id: str | None = Field(default=None, max_length=128)
    namespace: str = Field(max_length=64)
    holder: str = Field(max_length=128)
    state: str | None = Field(default=None, max_length=64)
    transaction_age_seconds: float
    query: str | None = Field(default=None, max_length=256)


class FleetOpenTransaction(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    application_name: str | None = Field(default=None, max_length=128)
    state: str | None = Field(default=None, max_length=64)
    transaction_age_seconds: float
    query: str | None = Field(default=None, max_length=256)


class FleetLocksResponse(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    held: list[FleetLockHolder] = Field(max_length=1_000)
    open_transactions: list[FleetOpenTransaction] = Field(max_length=100)


class FleetLogEntry(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    observed_at: datetime
    source: Literal["client", "monitor", "runtime", "job"]
    level: Literal["debug", "info", "warning", "error"]
    message: str = Field(min_length=1, max_length=4_096)
    evidence_id: str = Field(min_length=1, max_length=128)


class FleetLogResponse(StrictJSONModel):
    node_id: str = Field(pattern=_NODE_PATTERN)
    since: datetime | None
    lines: int = Field(ge=1, le=1_000)
    entries: list[FleetLogEntry] = Field(max_length=1_000)
    retained: bool
    follow: bool


class FleetLogProvider(Protocol):
    def list(
        self,
        node_id: str,
        *,
        since: datetime | None,
        lines: int,
        recipe: str | None,
        source: str | None,
        follow: bool,
    ) -> FleetLogResponse: ...


class FleetEnrollmentProvider(Protocol):
    def create_named(
        self, *, name: str, actor: str, request_id: str
    ) -> FleetActionResponse: ...

    def create_reenrollment(
        self, node_id: str, actor: str, request_id: str
    ) -> FleetActionResponse: ...

    def revoke_node(self, node_id: str, actor: str) -> None: ...

    def grant_status(self, grant_id: str, *, actor: str) -> EnrollmentGrantStatus: ...

    def revoke_grant(self, grant_id: str, *, actor: str) -> EnrollmentGrantStatus: ...


class FleetUpgradeProvider(Protocol):
    def current_package(self) -> AgentUpgradePackage: ...

    def get_request(
        self,
        request_id: str,
        *,
        actor: str,
        request_intent: AgentUpgradeRequestIntent,
    ) -> Any | None: ...

    def preview(
        self,
        node_ids: Sequence[str],
        package: AgentUpgradePackage,
        *,
        request_intent: AgentUpgradeRequestIntent,
    ) -> Any: ...

    def apply(
        self,
        node_ids: Sequence[str],
        package: AgentUpgradePackage,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        request_intent: AgentUpgradeRequestIntent,
    ) -> Any: ...


class FleetOperatorServices:
    def __init__(
        self,
        *,
        enrollment: FleetEnrollmentProvider | None = None,
        upgrades: FleetUpgradeProvider | None = None,
        logs: FleetLogProvider | None = None,
        sessions: sessionmaker[Session] | None = None,
    ) -> None:
        self.sessions = sessions
        self.enrollment = enrollment
        self.upgrades = upgrades
        self.logs = logs


class _AgentEnrollmentAdapter:
    """Expose the existing enrollment authority in the operator contract."""

    def __init__(self, services: AgentApiServices) -> None:
        self._services = services

    def _required(self) -> AgentApiServices:
        if self._services.enrollment is None:
            raise RuntimeError("agent enrollment is unavailable")
        if self._services.bootstrap is None:
            raise RuntimeError("agent enrollment bootstrap is unavailable")
        return self._services

    def _response(self, grant: Any) -> EnrollmentGrantResponse:
        services = self._required()
        bootstrap = services.bootstrap
        assert bootstrap is not None
        return EnrollmentGrantResponse(
            id=grant.id,
            expires_at=grant.expires_at.isoformat(),
            purpose=grant.purpose,
            token=grant.token,
            controller_endpoint=bootstrap.controller_endpoint,
            enrollment_endpoint=bootstrap.enrollment_endpoint,
            ca_fingerprint=bootstrap.ca_fingerprint,
            controller_address=bootstrap.controller_address,
            service_hostnames=list(bootstrap.service_hostnames),
            installer_url=accepted_installer_url(bootstrap.installer_url),
        )

    def create_named(
        self, *, name: str, actor: str, request_id: str
    ) -> FleetActionResponse:
        services = self._required()
        assert services.enrollment is not None
        grant = services.enrollment.create_named(
            name, actor, MAX_ENROLLMENT_GRANT_TTL_SECONDS, request_key=request_id
        )
        return FleetActionResponse(
            action="enroll",
            display_name=name,
            state="pending",
            grant=self._response(grant),
        )

    def create_reenrollment(
        self, node_id: str, actor: str, request_id: str
    ) -> FleetActionResponse:
        services = self._required()
        assert services.enrollment is not None
        grant = services.enrollment.create_reenrollment(
            node_id, actor, MAX_ENROLLMENT_GRANT_TTL_SECONDS, request_key=request_id
        )
        return FleetActionResponse(
            action="re-enroll",
            node_id=node_id,
            state="pending",
            grant=self._response(grant),
        )

    def grant_status(self, grant_id: str, *, actor: str) -> EnrollmentGrantStatus:
        enrollment = self._services.enrollment
        if enrollment is None:
            raise RuntimeError("agent enrollment is unavailable")
        return enrollment.grant_status(grant_id, actor=actor)

    def revoke_grant(self, grant_id: str, *, actor: str) -> EnrollmentGrantStatus:
        enrollment = self._services.enrollment
        if enrollment is None:
            raise RuntimeError("agent enrollment is unavailable")
        return enrollment.revoke_grant(grant_id, actor=actor)

    def revoke_node(self, node_id: str, actor: str) -> None:
        services = self._required()
        assert services.enrollment is not None
        services.enrollment.revoke_node(node_id, actor)


#: How far back the log projection narrates failed agent attempts by default.
_AGENT_LOG_LOOKBACK = timedelta(days=14)
#: A fixed number of Controller job-log blobs per query, matching the previous
#: bounded read.
#: A fixed number of failed agent attempts per query.
_AGENT_LOG_SCAN_LIMIT = 128
#: A hard ceiling on projected agent entries before the caller's ``lines`` cut.
_AGENT_LOG_ENTRY_LIMIT = 4_096
#: Which attempts an operator must be able to read back is owned by
#: ``failed_attempt_condition`` in the failure-evidence module, so this
#: projection and the diagnostics download cannot disagree about it.
#: The headline and level one attempt state narrates.  A lapse and a wait are
#: things an operator must act on, not errors that claim the start died.
_ATTEMPT_OUTCOME: Mapping[str, tuple[str, LogLevel]] = {
    "failed": ("failed", "error"),
    ObservationCause.LEASE_LAPSED.value: ("lease expired", "warning"),
    ObservationCause.REPORTED_UNKNOWN.value: ("waiting for operator", "warning"),
}


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _agent_log_source(kind: str) -> LogSource:
    """Map an agent operation kind onto the log source an operator already uses.

    Every agent-executed operation narrates the runtime path except a recipe job
    run, which owns the ``job`` source.  ``client`` and ``monitor`` have no
    producer in this build, so a query for them reports absence truthfully
    instead of claiming an empty retained store.
    """
    return "job" if kind == "recipe.job.run.v1" else "runtime"


def _phase_start_deadline(operation: AgentOperation) -> str | None:
    """Return the immutable start deadline a two-phase start bound, if any.

    Only a distributed start persists one, and it is the immutable budget the
    start may not outlive.  It is reported verbatim: the repository preserves a
    formatted timestamp's spelling rather than rewriting it.
    """

    payload = operation.payload if isinstance(operation.payload, Mapping) else {}
    value = payload.get("start_deadline")
    return value if isinstance(value, str) and value else None


def _attempt_is_parked(
    operation: AgentOperation, attempt: AgentOperationAttempt
) -> bool:
    """Return whether this exact attempt is why the order is waiting.

    The park sets the operation's state and reason, and the attempt keeps no
    timestamp of its own, so a lapse can only be dated while its attempt is
    still the current one.
    """

    return (
        agent_operation_states.order_is_parked(operation.state)
        and attempt.attempt == operation.current_attempt
    )


def _lease_clock(
    operation: AgentOperation, attempt: AgentOperationAttempt
) -> str | None:
    """Name the clock that lapsed and the numbers that bound it.

    A lease lapse is the Controller's own outcome, so the projection reports the
    Controller's facts: which clock stopped authorising renewal, the deadline it
    stopped renewing before, the instant the lapse was recorded, how far into the
    operation that instant was, and the separate start deadline that had not
    elapsed.  The requested lease window is deliberately not among them, and no
    existing field dates the grant: the attempt row keeps no timestamp, and the
    node's ``last_seen_at`` dates its last accepted *contact* -- which may be a
    claim or a result, not the renewal that set this deadline -- so subtracting
    it yields a lower bound rather than the window.  A missing number is
    acceptable here; an invented one is not.
    """

    if not agent_operation_states.attempt_lapsed(attempt):
        return None
    parts = [
        "clock=operation-lease",
        f"lease_deadline={_aware(attempt.lease_deadline).isoformat()}",
    ]
    if _attempt_is_parked(operation, attempt):
        expired_at = _aware(operation.updated_at)
        parts.append(f"expired_at={expired_at.isoformat()}")
        parts.append(
            "elapsed_seconds="
            f"{int((expired_at - _aware(operation.created_at)).total_seconds())}"
        )
    start_deadline = _phase_start_deadline(operation)
    if start_deadline is not None:
        parts.append(f"start_deadline={start_deadline}")
    return " ".join(parts)


def _failure_log_entries(
    *,
    operation_id: str,
    kind: str,
    source: LogSource,
    bundle: FailureEvidenceBundle,
    observed_at: datetime,
    state: str,
    clock: str | None,
    controller_reason: str | None,
    has_receipt: bool,
) -> list[FleetLogEntry]:
    """Project one bounded attempt narrative into retrievable log entries.

    The agent's own reason already names the stable refusal code for the
    protocol causes, whose ``diagnostic()`` is deliberately ``None``; the entry
    records that reason and the operation error code rather than any unbounded
    or unredacted payload.  An attempt that stopped renewing is the Controller's
    wait rather than an agent refusal, so it is narrated as a wait, it names the
    clock that lapsed with its numbers, and it claims no agent error code it
    never received.
    """

    entries: list[FleetLogEntry] = []
    headline, default_level = _ATTEMPT_OUTCOME.get(state, _ATTEMPT_OUTCOME["failed"])

    def add(message: str, level: LogLevel | None = None) -> None:
        text = redact_text(message)[:4_096]
        if text:
            entries.append(
                FleetLogEntry(
                    observed_at=observed_at,
                    source=source,
                    level=level or default_level,
                    message=text,
                    evidence_id=operation_id,
                )
            )

    add(f"{kind} {headline}: {bundle.summary}")
    # An error code is an agent's own stable refusal.  A lapsed lease came with
    # no receipt at all, so the default "operation_failed" would name a refusal
    # that never happened.
    if has_receipt:
        add(f"error_code={bundle.error_code}")
    if bundle.detail:
        add(f"detail={bundle.detail}")
    # The Controller's own record of the wait, which the agent's receipt cannot
    # carry: it is what says the effect is unobserved rather than dead.
    if controller_reason is not None and controller_reason != bundle.summary:
        add(f"controller: {controller_reason}", level="warning")
    if clock is not None:
        add(f"wait: {clock}", level="warning")
    for line in bundle.diagnostics.stderr.text.splitlines():
        add(f"stderr: {line}")
    for line in bundle.diagnostics.stdout.text.splitlines():
        add(f"stdout: {line}")
    # The refusing rule and its measured bound, e.g. "rule=… limit=4096
    # observed=518", so an operator sees how far over the request was.
    for property in bundle.diagnostics.preflight:
        add(f"preflight: {property.name}={property.value}", level="warning")
    for item in bundle.diagnostics.collector_errors:
        add(f"collector={item}", level="warning")
    for item in bundle.collector_errors:
        add(f"evidence-collector={item}", level="warning")
    return entries


class AgentFailureLogProvider:
    """Project retained, redacted agent failure evidence for one Spark.

    The agent operation attempts keep the agent's own bounded failure result -- its reason
    (which names the stable refusal code), its operation error code and its
    sanitized process-log tails -- which is the narrative a failed
    ``recipe.start`` never reached the log surface with.  An attempt whose lease
    lapsed left no result at all, so the Controller's own record of the wait and
    the clock that lapsed are projected in its place.  This is not a live
    stream, nothing is written here, and it never becomes an authority for
    anything.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Any | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock or (lambda: datetime.now(UTC))

    def list(
        self,
        node_id: str,
        *,
        since: datetime | None,
        lines: int,
        recipe: str | None,
        source: str | None,
        follow: bool,
    ) -> FleetLogResponse:
        entries: list[FleetLogEntry] = []
        retained = False
        if source in (None, "job", "runtime"):
            entries.extend(self._agent_failure_entries(node_id, since=since))
            retained = True
        entries.sort(key=lambda item: item.observed_at, reverse=True)
        return FleetLogResponse(
            node_id=node_id,
            since=since,
            lines=lines,
            entries=entries[:lines],
            # ``retained`` names whether a durable store was actually consulted
            # for this query.  A source with no producer reports False rather
            # than an empty retained store.
            retained=retained,
            # Both projected stores are retained evidence, not live streams.
            follow=False,
        )

    def _agent_failure_entries(
        self, node_id: str, *, since: datetime | None
    ) -> list[FleetLogEntry]:
        now = _aware(self._clock())
        cutoff = _aware(since) if since is not None else now - _AGENT_LOG_LOOKBACK
        with self._sessions() as session:
            rows = list(
                session.execute(
                    select(AgentOperation, AgentOperationAttempt)
                    .join(
                        AgentOperationAttempt,
                        AgentOperationAttempt.operation_id == AgentOperation.id,
                    )
                    .where(
                        AgentOperation.node_id == node_id,
                        failed_attempt_condition(AgentOperation, AgentOperationAttempt),
                        AgentOperation.updated_at >= cutoff,
                    )
                    .order_by(
                        AgentOperation.updated_at.desc(),
                        AgentOperation.id.desc(),
                        AgentOperationAttempt.attempt.desc(),
                    )
                    .limit(_AGENT_LOG_SCAN_LIMIT)
                )
            )
        entries: list[FleetLogEntry] = []
        for operation, attempt in rows:
            payload = (
                operation.payload if isinstance(operation.payload, Mapping) else {}
            )
            # Only the agent's own receipt may name an error code; a lease lapse
            # arrived with no receipt, so the fallback narrative is the
            # Controller's reason for the wait.
            receipt = attempt.result or payload.get("failure")
            result = receipt or {
                "reason": operation.status_reason or "Operation failed"
            }
            parked = _attempt_is_parked(operation, attempt)
            clock = _lease_clock(operation, attempt)
            observed_at = _aware(operation.updated_at)
            source_name = _agent_log_source(operation.kind)
            try:
                item = FailedAttempt(
                    id=operation.id,
                    attempt=attempt.attempt,
                    kind=operation.kind,
                    node_ids=[operation.node_id],
                    updated_at=observed_at.isoformat(),
                    source="agent",
                    progress=AttemptPhase.model_validate(attempt.progress),
                    result=OperationResultFacts.model_validate(result),
                )
                bundle = collect_failure(item, now=now)
            except Exception:  # noqa: BLE001 - one malformed row must not hide the rest
                headline, level = _ATTEMPT_OUTCOME.get(
                    agent_operation_states.attempt_outcome_key(attempt),
                    _ATTEMPT_OUTCOME["failed"],
                )
                fallback = redact_text(
                    f"{operation.kind} {headline}: "
                    f"{operation.status_reason or 'Operation failed'}"
                )[:4_096]
                if fallback:
                    entries.append(
                        FleetLogEntry(
                            observed_at=observed_at,
                            source=source_name,
                            level=level,
                            message=fallback,
                            evidence_id=operation.id,
                        )
                    )
                continue
            entries.extend(
                _failure_log_entries(
                    operation_id=operation.id,
                    kind=operation.kind,
                    source=source_name,
                    bundle=bundle,
                    observed_at=observed_at,
                    state=agent_operation_states.attempt_outcome_key(attempt),
                    clock=clock,
                    controller_reason=(operation.status_reason if parked else None),
                    has_receipt=receipt is not None,
                )
            )
            if len(entries) >= _AGENT_LOG_ENTRY_LIMIT:
                break
        return entries


def build_fleet_operator_services(
    *,
    agent_services: AgentApiServices | None,
    upgrades: AgentUpgradeService | None,
    logs: FleetLogProvider | None = None,
    sessions: sessionmaker[Session] | None = None,
) -> FleetOperatorServices:
    """Build Fleet action adapters from existing Controller authorities.

    ``logs`` must return retained authenticated evidence when configured.  If
    ``sessions`` is supplied, the helper builds the retained Controller
    evidence provider itself.  Neither
    path synthesizes remote log entries or falls back to SSH.
    """

    enrollment = (
        None if agent_services is None else _AgentEnrollmentAdapter(agent_services)
    )
    retained_logs = logs
    if retained_logs is None and sessions is not None:
        retained_logs = AgentFailureLogProvider(sessions)
    return FleetOperatorServices(
        enrollment=enrollment,
        upgrades=upgrades,
        logs=retained_logs,
        sessions=sessions,
    )


def _node(snapshot: FleetSnapshot, selector: str) -> FleetNode:
    wanted = selector.casefold()
    exact = [value for value in snapshot.nodes if value.id.casefold() == wanted]
    matches = exact or [
        value
        for value in snapshot.nodes
        if wanted
        in {
            value.display_name.casefold(),
            value.hostname.casefold(),
        }
    ]
    if not matches:
        raise KeyError(selector)
    if len(matches) > 1:
        raise LibrarySelectorAmbiguous(selector, sorted(value.id for value in matches))
    return matches[0]


def _domain_refusal_detail(error: Exception) -> str:
    """Bound and redact a domain authority's own refusal text.

    The refusal is built by the owning domain from policy copy, so the boundary
    only has to keep it bounded and redacted; it never carries a stored document.
    """

    return redact_text(str(error))[:256]


def _operator_error(error: Exception) -> HTTPException:
    if isinstance(error, LibrarySelectorAmbiguous):
        from .library_api import SelectorAmbiguityHTTPError

        return SelectorAmbiguityHTTPError(error)
    if isinstance(error, KeyError):
        return HTTPException(status_code=404, detail="operator object not found")
    if isinstance(error, (CursorError, RequestFault)):
        # An explicit request fault keeps 422. Everything else, including a
        # stored document that no longer validates, is the Controller's state
        # and answers the declared 503 rather than blaming the request.
        return HTTPException(status_code=422, detail=str(error)[:256])
    if isinstance(error, AgentUpgradeConflict):
        # The upgrade authority refused the plan. That is a conflict between the
        # request and current Fleet state, not a projection fault, so name the
        # layer and keep the authority's bounded reason instead of the generic
        # "operator projection unavailable" tail.
        return HTTPException(
            status_code=409,
            detail=_domain_refusal_detail(error),
            headers={"x-vonk-error-code": ControllerErrorCode.FLEET_UPGRADE_CONFLICT},
        )
    if isinstance(error, RemoteRevocationUncertain):
        # Local revocation is durable; only the CA confirmation is pending. Stay
        # retryable but name the uncertain authority rather than the projection.
        return HTTPException(
            status_code=503,
            detail=_domain_refusal_detail(error),
            headers={
                "x-vonk-error-code": ControllerErrorCode.FLEET_REVOCATION_UNCERTAIN
            },
        )
    if isinstance(error, EnrollmentDenied):
        return HTTPException(
            status_code=409,
            detail=_domain_refusal_detail(error),
            headers={
                "x-vonk-error-code": SecurityRefusalReason.CONTROLLER_FLEET_ENROLLMENT_DENIED.value
            },
        )
    detail = stored_document_detail(error)
    if detail is not None:
        # Name the failing field path so the corrupt row can be found, without
        # echoing the stored value.
        return HTTPException(status_code=503, detail=detail[:256])
    if isinstance(error, BoundedJSONError):
        return HTTPException(status_code=503, detail=str(error)[:256])
    # Nothing above explains this failure, so the caller only learns that the
    # projection is unavailable. Record the redacted traceback against the
    # request id the caller sees, so the 503 can be diagnosed from the log.
    log_event(
        _LOGGER,
        "api.operator_projection_failed",
        service="controller",
        request_id=current_request_id.get(),
        failure_type=type(error).__name__,
        traceback=traceback.format_exception(error)[-64:],
    )
    return HTTPException(status_code=503, detail="operator projection unavailable")


def _require_mutation(actor: Actor, method: str, route: str) -> None:
    """Use the shared role table for both cookie and bearer actors."""

    allowed = MUTATION_ROLES.get((method, route))
    if allowed is None or actor.role not in allowed:
        raise HTTPException(status_code=403, detail="insufficient role")


def install_operator_projection_routes(
    app: FastAPI,
    *,
    actor_dependency: Any,
    fleet_projection: Any | None,
    library_projection: Any | None,
    fleet_services: FleetOperatorServices | None = None,
) -> None:
    """Install the singular operator route hierarchy.

    ``fleet_services`` is deliberately an adapter boundary.  The parent wires
    it to enrollment, signed upgrades and retained authenticated evidence; this
    module never falls back to SSH or synthetic values.
    """

    from .library_api import install_library_routes
    from .operation_api import _ADMIN_OPERATION_IDS

    _ADMIN_OPERATION_IDS.update(FLEET_OPERATION_IDS)
    install_library_routes(
        app, actor_dependency=actor_dependency, projection=library_projection
    )
    authenticated = actor_dependency

    def fleet() -> Any:
        if fleet_projection is None:
            raise HTTPException(status_code=503, detail="fleet projection unavailable")
        return fleet_projection

    def snapshot() -> FleetSnapshot:
        try:
            return fleet().read()
        except HTTPException:
            raise
        except SQLAlchemyError as error:
            raise ObservationCaptureUnavailable(phase="database-capture") from error
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
            raise _operator_error(error) from None

    def selected(selector: str) -> FleetNode:
        try:
            return _node(snapshot(), selector)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
            raise _operator_error(error) from None

    @app.get(
        "/api/fleet",
        response_class=ObservationTransferResponse,
        response_model=None,
        responses={
            200: {"model": ObservationTransferRecord},
            **bounded_error_responses(401, 503),
        },
        openapi_extra=observation_openapi("FleetSnapshot"),
        operation_id="getFleetStatus",
    )
    def fleet_status(
        _actor: Actor = authenticated,
    ) -> ObservationTransferResponse | Response:
        try:
            captured = snapshot()
        except ObservationCaptureUnavailable as error:
            return observation_capture_unavailable_response(
                error, operation="getFleetStatus", endpoint="/api/fleet"
            )
        return observation_response(captured, resource="fleet")

    @app.get(
        "/api/fleet/locks",
        response_model=FleetLocksResponse,
        responses=bounded_error_responses(401, 403, 503),
        operation_id="getFleetAdmissionLocks",
    )
    def fleet_locks(actor: Actor = authenticated) -> FleetLocksResponse:
        if actor.role != "administrator":
            raise HTTPException(status_code=403, detail="insufficient role")
        if fleet_services is None or fleet_services.sessions is None:
            raise HTTPException(status_code=503, detail="fleet locks unavailable")
        from .admission_locking import report_admission_locks

        try:
            with fleet_services.sessions() as session:
                report = report_admission_locks(session)
                session.rollback()
            return FleetLocksResponse.model_validate(
                {
                    "held": [asdict(item) for item in report.held],
                    "open_transactions": [
                        asdict(item) for item in report.open_transactions
                    ],
                }
            )
        except (OSError, RuntimeError, TypeError, ValueError, SQLAlchemyError) as error:
            raise _operator_error(error) from None

    @app.get(
        "/api/fleet/{selector}/loginfo",
        response_model=FleetLogResponse,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getFleetLogInfo",
    )
    def fleet_loginfo(
        selector: Annotated[str, Path(pattern=_SELECTOR_PATTERN)],
        since: Annotated[datetime | None, Query()] = None,
        lines: Annotated[int, Query(ge=1, le=1_000)] = 100,
        recipe: Annotated[str | None, Query(max_length=256)] = None,
        source: Annotated[
            Literal["client", "monitor", "runtime", "job"] | None, Query()
        ] = None,
        follow: Annotated[bool, Query()] = False,
        _actor: Actor = authenticated,
    ) -> FleetLogResponse:
        node = selected(selector)
        if fleet_services is None or fleet_services.logs is None:
            raise HTTPException(
                status_code=503, detail="fleet log evidence unavailable"
            )
        try:
            value = fleet_services.logs.list(
                node.id,
                since=since,
                lines=lines,
                recipe=recipe,
                source=source,
                follow=follow,
            )
            return value
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise _operator_error(error) from None

    @app.get(
        "/api/fleet/{selector}",
        response_model=FleetNode,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getFleetNode",
    )
    def fleet_detail(
        selector: Annotated[str, Path(pattern=_SELECTOR_PATTERN)],
        _actor: Actor = authenticated,
    ) -> FleetNode:
        return selected(selector)

    @app.post(
        "/api/fleet/{selector}/rename",
        response_model=FleetNodeIdentity,
        responses=bounded_error_responses(401, 403, 404, 422, 503),
        operation_id="renameFleetNode",
    )
    def fleet_rename(
        selector: Annotated[str, Path(pattern=_SELECTOR_PATTERN)],
        body: FleetRenameRequest,
        actor: Actor = authenticated,
    ) -> FleetNodeIdentity:
        _require_mutation(actor, "POST", "/api/fleet/{selector}/rename")
        node = selected(selector)
        try:
            result = fleet().update_display_name(node.id, body.display_name)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise _operator_error(error) from None
        return result

    @app.post(
        "/api/fleet/enroll",
        response_model=FleetActionResponse,
        status_code=status.HTTP_201_CREATED,
        responses=bounded_error_responses(401, 403, 409, 422, 503),
        operation_id="enrollFleetNode",
    )
    def fleet_enroll(
        body: FleetEnrollRequest,
        actor: Actor = authenticated,
    ) -> FleetActionResponse:
        _require_mutation(actor, "POST", "/api/fleet/enroll")
        if fleet_services is None or fleet_services.enrollment is None:
            raise HTTPException(status_code=503, detail="fleet enrollment unavailable")
        try:
            value = fleet_services.enrollment.create_named(
                name=body.name,
                actor=actor.subject,
                request_id=body.request_key,
            )
            return value
        except (OSError, RuntimeError, TypeError, ValueError, SQLAlchemyError) as error:
            raise _operator_error(error) from None

    @app.post(
        "/api/fleet/{selector}/re-enroll",
        response_model=FleetActionResponse,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="reenrollFleetNode",
    )
    def fleet_reenroll(
        selector: Annotated[str, Path(pattern=_SELECTOR_PATTERN)],
        body: FleetReenrollRequest,
        actor: Actor = authenticated,
    ) -> FleetActionResponse:
        _require_mutation(actor, "POST", "/api/fleet/{selector}/re-enroll")
        node = selected(selector)
        if fleet_services is None or fleet_services.enrollment is None:
            raise HTTPException(status_code=503, detail="fleet enrollment unavailable")
        try:
            value = fleet_services.enrollment.create_reenrollment(
                node.id, actor.subject, body.request_key
            )
            return value
        except (OSError, RuntimeError, TypeError, ValueError, SQLAlchemyError) as error:
            raise _operator_error(error) from None

    @app.get(
        "/api/fleet/enrollments/{grant_id}",
        response_model=EnrollmentGrantStatus,
        responses=bounded_error_responses(401, 403, 404, 422, 503),
        operation_id="getFleetEnrollment",
    )
    def get_enrollment(
        grant_id: Annotated[str, Path(pattern=ENROLLMENT_ID_PATTERN)],
        actor: Actor = authenticated,
    ) -> EnrollmentGrantStatus:
        _require_mutation(actor, "POST", "/api/fleet/enroll")
        if fleet_services is None or fleet_services.enrollment is None:
            raise HTTPException(status_code=503, detail="fleet enrollment unavailable")
        try:
            return fleet_services.enrollment.grant_status(grant_id, actor=actor.subject)
        except (
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            SQLAlchemyError,
        ) as error:
            raise _operator_error(error) from None

    @app.post(
        "/api/fleet/enrollments/{grant_id}/revoke",
        openapi_extra={"x-vonk-request-body": "none"},
        response_model=EnrollmentGrantStatus,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="revokeFleetEnrollment",
    )
    def revoke_enrollment(
        grant_id: Annotated[str, Path(pattern=ENROLLMENT_ID_PATTERN)],
        actor: Actor = authenticated,
    ) -> EnrollmentGrantStatus:
        _require_mutation(actor, "POST", "/api/fleet/enrollments/{grant_id}/revoke")
        if fleet_services is None or fleet_services.enrollment is None:
            raise HTTPException(status_code=503, detail="fleet enrollment unavailable")
        try:
            result = fleet_services.enrollment.revoke_grant(
                grant_id, actor=actor.subject
            )
            return result
        except (
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            SQLAlchemyError,
        ) as error:
            raise _operator_error(error) from None

    @app.post(
        "/api/fleet/{selector}/remove",
        openapi_extra={"x-vonk-request-body": "none"},
        response_model=FleetActionResponse,
        responses=bounded_error_responses(401, 403, 404, 409, 503),
        operation_id="removeFleetNode",
    )
    def fleet_remove(
        selector: Annotated[str, Path(pattern=_SELECTOR_PATTERN)],
        actor: Actor = authenticated,
    ) -> FleetActionResponse:
        _require_mutation(actor, "POST", "/api/fleet/{selector}/remove")
        node = selected(selector)
        if fleet_services is None or fleet_services.enrollment is None:
            raise HTTPException(status_code=503, detail="fleet removal unavailable")
        try:
            fleet_services.enrollment.revoke_node(node.id, actor.subject)
            result = FleetActionResponse(
                action="remove", state="accepted", node_id=node.id
            )
            return result
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise _operator_error(error) from None

    @app.post(
        "/api/fleet/upgrade",
        response_model=FleetActionResponse,
        status_code=status.HTTP_202_ACCEPTED,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="upgradeFleet",
    )
    def fleet_upgrade(
        body: FleetUpgradeRequest,
        actor: Actor = authenticated,
    ) -> FleetActionResponse:
        _require_mutation(actor, "POST", "/api/fleet/upgrade")
        if fleet_services is None or fleet_services.upgrades is None:
            raise HTTPException(status_code=503, detail="fleet upgrades unavailable")
        if body.all == (body.selectors is not None):
            raise HTTPException(status_code=422, detail="choose all or selectors")
        try:
            request_intent = AgentUpgradeRequestIntent(
                all=body.all, selectors=body.selectors
            )
            existing = fleet_services.upgrades.get_request(
                body.request_key,
                actor=actor.subject,
                request_intent=request_intent,
            )
            if existing is not None:
                result = FleetActionResponse(
                    action="upgrade",
                    state=str(getattr(existing, "state", "accepted")),
                    operation_id=str(getattr(existing, "id", "")) or None,
                    plan_digest=str(getattr(existing, "payload_digest", "")) or None,
                    request_key=body.request_key,
                    targets=list(getattr(existing, "targets", ())),
                )
                return result
            fleet_snapshot = snapshot()
            nodes = (
                list(fleet_snapshot.nodes)
                if body.all
                else [_node(fleet_snapshot, value) for value in body.selectors or []]
            )
            node_ids = list(dict.fromkeys(node.id for node in nodes))
            package = fleet_services.upgrades.current_package()
            plan = fleet_services.upgrades.preview(
                node_ids,
                package,
                request_intent=request_intent,
            )
            job = fleet_services.upgrades.apply(
                node_ids,
                package,
                plan_digest=plan.plan_digest,
                actor=actor.subject,
                request_id=body.request_key,
                request_intent=request_intent,
            )
            result = FleetActionResponse(
                action="upgrade",
                state=str(getattr(job, "state", "accepted")),
                operation_id=str(getattr(job, "id", "")) or None,
                plan_digest=str(getattr(job, "payload_digest", plan.plan_digest)),
                request_key=body.request_key,
                targets=list(getattr(job, "targets", node_ids)),
            )
            return result
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise _operator_error(error) from None


__all__ = [
    "FleetActionResponse",
    "FleetEnrollRequest",
    "FleetEnrollmentProvider",
    "FleetLogEntry",
    "FleetLogProvider",
    "FleetLogResponse",
    "FleetOperatorServices",
    "FleetRenameRequest",
    "FleetUpgradeProvider",
    "FleetUpgradeRequest",
    "build_fleet_operator_services",
    "install_operator_projection_routes",
]
