"""Failure diagnostics rendered on request from durable failure rows.

No commands or network calls run here, and nothing is stored: the bundle is a
redacted view of the attempt result, job result or progress the Controller
already keeps, so an offline node never delays it and a later read never
disagrees with the row it describes.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import quote

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)
from sqlalchemy import String, and_, cast, or_, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import FailureCode, LifecycleState, StateAlias
from vonk_agent_protocol.failure_evidence import FailureDiagnostics, FailureLogTail

from . import agent_operation_states
from .bounded_json import BoundedJSONError, require_integer, sequence
from .logging import redact_text
from .models import (
    AgentOperation,
    AgentOperationAttempt,
    FleetProfileApplication,
    Job,
    ModelCacheOperation,
)
from .operation_blockers import OperationBlocker, read_blockers
from .operation_contract import OperationEvidenceDownload
from .operation_item_contract import OperationItem, OperationResultFacts, operation_item
from .strict_json import StrictJSONModel, read_stored_model

MAX_LOG_BYTES = 2048
MAX_LOG_LINES = 32
_SENSITIVE = re.compile(
    r"password|secret|token|authorization|cookie|credential|private.?key|api.?key|environment",
    re.IGNORECASE,
)
# A line-level filter, so every alternative must name a credential *value*, not
# merely a topic.  A bare ``authorization`` alternative redacted any line that
# mentioned a table such as ``runtime_image_authorizations``, and a bare
# ``token`` alternative redacted any line that mentioned
# ``num_speculative_tokens`` -- both are exactly the configuration and history a
# failed workload is diagnosed from.  Assignment and authentication shapes still
# match, and ``redact_text`` removes the credential inside them.
_SECRET_LINE = re.compile(
    r"(?:password|passwd|secret|token|authorization|cookie|credential)\s*[:=]"
    r"|private[ _-]?key|api[ _-]?key"
    r"|(?:bearer|basic)\s+\S"
    r"|-----BEGIN|-----END",
    re.IGNORECASE,
)
# An opaque value is a whole token.  Matching a run inside a longer word cut the
# module out of every Python frame path, so a traceback arrived as
# ``python3.[redacted opaque value].py`` and named no frame at all.
_OPAQUE_SECRET = re.compile(
    r"(?<![A-Za-z0-9/.])([A-Za-z0-9+/=_-]{40,})(?![A-Za-z0-9/.])"
)

# The failure-evidence closed sets are named once here so the bundle fields and
# the helpers that build them cannot disagree. ``FailureCategory`` mirrors the
# protocol's ``FailureDiagnostics.category`` literal set, which is defined in
# the shared ``vonk_agent_protocol`` package rather than this module.
EvidenceSource = Literal["agent", "controller"]
FailureCategory = Literal[
    "platform-policy",
    "capacity",
    "network",
    "digest",
    "timeout",
    "runtime",
    "unknown",
]
_EVIDENCE_SOURCE = TypeAdapter(EvidenceSource)
_CATEGORY_MARKERS: tuple[tuple[FailureCategory, tuple[str, ...]], ...] = (
    (
        "platform-policy",
        (
            "permission",
            "denied",
            "namespace",
            "sandbox",
            "policy",
            "mapping",
            "proc-mount",
        ),
    ),
    ("capacity", ("space", "storage", "capacity", "memory-limit")),
    ("digest", ("digest", "integrity", "verification", "checksum")),
    ("timeout", ("timeout", "deadline")),
    ("network", ("network", "connection", "download", "upstream", "rate_limit")),
)


class EvidenceModel(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class EvidenceContext(EvidenceModel):
    operation_id: str = Field(min_length=1, max_length=128)
    attempt: int = Field(ge=0)
    kind: str = Field(min_length=1, max_length=80)
    node_ids: list[str] = Field(max_length=128)
    updated_at: str
    source: EvidenceSource
    rank: int | None = Field(default=None, ge=0)


class FailureEvidenceBundle(EvidenceModel):
    schema_version: Literal[2] = 2
    context: EvidenceContext
    collected_at: str
    summary: str = Field(max_length=512)
    error_code: str = Field(min_length=1, max_length=64)
    detail: str | None = Field(default=None, max_length=1024)
    diagnostics: FailureDiagnostics
    collector_errors: list[str] = Field(max_length=8)
    #: What the operation was waiting for when it stopped, when it recorded that.
    blockers: list[OperationBlocker] = Field(default_factory=list, max_length=16)


#: The states of an operation (of any kind) whose failure evidence is downloadable:
#: a failure, or a wait for a person in either spelling.
FAILED_ATTEMPT_STATES = (
    LifecycleState.FAILED.value,
    LifecycleState.NEEDS_OPERATOR.value,
    StateAlias.WAITING_FOR_OPERATOR.value,
)

# The attempts whose own terminal failure receipt must stay readable are the failed
# ones and those whose executor reported the effect unknown.  A lapsed lease or a
# supersession is not among them by itself: it owns no receipt, and its
# operation-level reason may already describe a later attempt.  Only a receipt the
# attempt actually kept, or the park that still names it, qualifies.  The typed
# ``observation_cause`` tells the two kinds of observed attempt apart.


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _plain_text(value: str) -> str:
    return "".join(
        character
        for character in value
        if (character in "\n\t" or ord(character) >= 32)
        and ord(character) != 127
        and unicodedata.category(character) != "Cf"
    )


def safe_text(value: str) -> str:
    value = _plain_text(value)
    # Dropping sensitive lines handles quoted multiword assignments, partial
    # PEM blocks and encoded values without trying to understand credentials.
    lines = []
    for line in value.splitlines():
        if _SECRET_LINE.search(line):
            lines.append("[redacted diagnostic line]")
        else:
            lines.append(
                _OPAQUE_SECRET.sub("[redacted opaque value]", redact_text(line))
            )
    return "\n".join(lines)


def _keep_end(text: str, limit: int) -> tuple[str, int]:
    """Keep the end of ``text`` within ``limit`` bytes, and report what it dropped.

    The end is what happened last, so a bound is applied from the front.  Cutting
    the end of a tail keeps stale lines and discards the failure that ended it.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, 0
    cut = len(encoded) - limit
    candidate = encoded[cut:]
    boundary = candidate.find(b"\n")
    if boundary >= 0:
        cut += boundary + 1
    return encoded[cut:].decode("utf-8", errors="ignore"), cut


def log_tail(value: str) -> FailureLogTail:
    original = value.encode("utf-8")
    truncated = len(original) > MAX_LOG_BYTES or len(value.splitlines()) > MAX_LOG_LINES
    # Discard an incomplete leading line before sanitizing a tail: a cut
    # Authorization/PEM prefix must not turn its remainder into visible text.
    candidate = original[-MAX_LOG_BYTES:].decode("utf-8", errors="replace")
    if len(original) > MAX_LOG_BYTES:
        candidate = candidate.partition("\n")[2]
    selected = candidate.splitlines()[-MAX_LOG_LINES:]
    cleaned = (
        safe_text("\n".join(selected))
        .encode()[-MAX_LOG_BYTES:]
        .decode("utf-8", errors="ignore")
    )
    return FailureLogTail(
        text=cleaned,
        truncated=truncated,
        dropped_bytes=max(0, len(original) - len(candidate.encode())),
        dropped_lines=max(0, len(value.splitlines()) - len(selected)),
    )


def failure_code(result: OperationResultFacts) -> tuple[str, str | None]:
    """Return the stable error code and redacted detail a failure result names."""
    code = result.error_code or result.code or FailureCode.OPERATION_FAILED.value
    code = re.sub(r"[^a-z0-9_]", "_", str(code).lower())[:64]
    if not code or not code[0].isalpha():
        code = FailureCode.OPERATION_FAILED.value
    detail = result.detail or result.diagnostic or result.helper_error_code
    return code, safe_text(str(detail))[:1024] if detail is not None else None


def classification(kind: str, result: OperationResultFacts) -> FailureCategory:
    # Phase/state comes from typed fields. Classification uses stable emitted
    # error/diagnostic codes, never searches logs to invent operation progress.
    code = " ".join(
        value or ""
        for value in (
            result.error_code,
            result.code,
            result.diagnostic,
            result.helper_error_code,
        )
    ).casefold()
    for category, markers in _CATEGORY_MARKERS:
        if any(marker in code for marker in markers):
            return category
    return "runtime" if kind.startswith(("recipe.", "agent.upgrade")) else "unknown"


class AttemptPhase(BaseModel):
    """The one thing the evidence reads of a family's progress document."""

    model_config = ConfigDict(extra="ignore")

    phase: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _tolerate(cls, value: object) -> object:
        phase = (
            value.get("phase")
            if isinstance(value, Mapping)
            else getattr(value, "phase", None)
        )
        return {"phase": phase if isinstance(phase, str) else None}


class FailedAttempt(BaseModel):
    """One failed attempt of any family: what the evidence bundle is built from."""

    model_config = ConfigDict(extra="ignore")

    id: str
    attempt: int
    kind: str
    node_ids: list[str] = Field(default_factory=list)
    updated_at: str
    source: EvidenceSource | None = None
    rank: int | None = None
    #: A Controller-owned record has no Spark operation, so no agent observations.
    agent_operation: bool = True
    progress: AttemptPhase | None = None
    result: OperationResultFacts = Field(default_factory=OperationResultFacts)
    blockers: list[OperationBlocker] | None = None

    @field_validator("node_ids", mode="before")
    @classmethod
    def _node_ids(cls, value: object) -> object:
        return [] if value is None else value

    @field_validator("blockers", mode="before")
    @classmethod
    def _readable_blockers(cls, value: object) -> object:
        return None if value is None else read_blockers(value)

    @field_validator("result", mode="before")
    @classmethod
    def _result(cls, value: object) -> object:
        return {} if value is None else value

    @property
    def evidence_source(self) -> EvidenceSource:
        return self.source or ("agent" if self.node_ids else "controller")


def _required_text(value: object, detail: str) -> str:
    """Return a persisted string or fail loudly; the field is required."""

    if not isinstance(value, str):
        raise BoundedJSONError(f"{detail} is invalid")
    return value


def _optional_int(value: object, detail: str) -> int | None:
    """Return a persisted optional integer without defaulting a wrong type."""

    return None if value is None else require_integer(value, detail)


def _required_node_ids(value: object) -> list[str]:
    """Read the persisted node list, failing on a non-string member.

    A value that is not a JSON array keeps the existing omission policy: the
    previous ``sequence(...) or ()`` read treated it as an empty fleet.
    """

    members = sequence(value)
    if members is None:
        return []
    return [_required_text(member, "operation node id") for member in members]


def sanitize_diagnostics(value: object) -> FailureDiagnostics:
    diagnostics = read_stored_model(FailureDiagnostics, value)
    document = diagnostics.model_dump(mode="json")
    for field in ("stdout", "stderr"):
        # The agent already retained this tail from the whole stream, so the
        # Controller only redacts it.  Re-selecting the window here would keep
        # the head of a tail and discard the failure that ended it, and it would
        # report a bound the agent had already reported.
        cleaned, dropped = _keep_end(safe_text(document[field]["text"]), MAX_LOG_BYTES)
        document[field]["text"] = cleaned
        if dropped:
            document[field]["truncated"] = True
            document[field]["dropped_bytes"] = (
                document[field]["dropped_bytes"] or 0
            ) + dropped
    for field in ("versions", "sandbox", "storage", "preflight"):
        document[field] = [
            {
                "name": safe_text(prop["name"])[:64],
                "value": "[redacted]"
                if _SENSITIVE.search(_plain_text(prop["name"]))
                else safe_text(prop["value"])[:256],
            }
            for prop in document[field]
        ]
    document["collector_errors"] = [
        safe_text(error)[:256] for error in document["collector_errors"]
    ]
    diagnostics = read_stored_model(FailureDiagnostics, document)
    return diagnostics


def collect_failure(item: FailedAttempt, *, now: datetime) -> FailureEvidenceBundle:
    result = item.result
    errors: list[str] = []
    diagnostics: FailureDiagnostics | None = None
    if result.diagnostics_invalid:
        errors.append("agent-diagnostics-invalid")
    elif result.diagnostics is not None:
        try:
            diagnostics = sanitize_diagnostics(result.diagnostics)
        except (ValueError, TypeError):
            errors.append("agent-diagnostics-invalid")
    if diagnostics is None:
        phase = (
            (item.progress.phase if item.progress is not None else None)
            or result.stage
            or "unknown"
        )
        diagnostics = FailureDiagnostics(
            collected_at=now.isoformat(),
            phase=safe_text(str(phase))[:80],
            category=classification(item.kind, result),
            stdout=log_tail(result.stdout or ""),
            stderr=log_tail(result.stderr or result.log_excerpt or ""),
            versions=[],
            sandbox=[],
            storage=[],
            preflight=[],
            collector_errors=["agent-observations-unavailable"]
            if item.node_ids and item.agent_operation
            else [],
        )
    error_code, detail = failure_code(result)
    summary = result.summary or result.reason or result.detail or "Operation failed"
    return FailureEvidenceBundle(
        context=EvidenceContext(
            operation_id=item.id,
            attempt=item.attempt,
            kind=item.kind,
            node_ids=item.node_ids[:128],
            updated_at=item.updated_at,
            source=item.evidence_source,
            rank=item.rank,
        ),
        collected_at=now.isoformat(),
        summary=safe_text(str(summary))[:512],
        error_code=error_code,
        detail=detail,
        diagnostics=diagnostics,
        collector_errors=errors,
        blockers=[
            blocker.model_copy(
                update={"detail": safe_text(blocker.detail)[:512] or "-"}
            )
            for blocker in item.blockers or []
        ],
    )


def failed_attempt_condition(operation, attempt):
    """SQL predicate: this attempt's failure evidence must stay readable.

    One owner for "which attempt is a failure attempt": the diagnostics
    download and the operator log projection both select attempts through this
    predicate, so neither can drift into showing or hiding an attempt the other
    disagrees about.  An attempt qualifies when its own state is a terminal
    failure, when it lapsed its lease but kept the agent's late failure receipt,
    or when it is the current attempt of a parked operation -- the one lapse
    whose narrative the operation's own reason still owns.  A bare superseded
    lapse qualifies on none of them: it kept no receipt, and the operation's
    reason already describes a different attempt, so neither may be invented.
    """

    return or_(
        agent_operation_states.sql_attempt_failed_or_unknown(attempt),
        and_(
            agent_operation_states.sql_attempt_lapsed(attempt),
            # A receipt is a JSON object.  An absent one is stored as the JSON
            # ``null`` value rather than SQL NULL, so the plain ``IS NOT NULL``
            # test would read every bare lapse as if it had kept a receipt.
            cast(attempt.result, String) != "null",
        ),
        and_(
            operation.state.in_(agent_operation_states.PARKED),
            attempt.attempt == operation.current_attempt,
        ),
    )


def _fallback_bundle(item: FailedAttempt, *, now: datetime) -> FailureEvidenceBundle:
    """A bounded bundle for a row the collector could not read.

    The download reports the collector failure separately and never echoes the
    exception, which may quote the unredacted value that broke it.
    """
    empty = FailureLogTail(text="", truncated=False, dropped_bytes=0, dropped_lines=0)
    error_code, _ = failure_code(item.result)
    return FailureEvidenceBundle(
        context=EvidenceContext(
            operation_id=item.id,
            attempt=item.attempt,
            kind=item.kind,
            node_ids=item.node_ids[:128],
            updated_at=item.updated_at,
            source="agent" if item.node_ids else "controller",
        ),
        collected_at=now.isoformat(),
        summary=safe_text(
            item.result.reason or item.result.summary or "Operation failed"
        )[:512],
        error_code=error_code,
        diagnostics=FailureDiagnostics(
            collected_at=now.isoformat(),
            phase="unknown",
            category="unknown",
            stdout=empty,
            stderr=empty,
            versions=[],
            sandbox=[],
            storage=[],
            preflight=[],
            collector_errors=[],
        ),
        collector_errors=["collector-failed"],
    )


def evidence_href(operation_id: str, attempt: int) -> str:
    return f"/api/operations/{quote(operation_id, safe='')}/evidence?attempt={attempt}"


class FailureEvidenceService:
    """Render one failed attempt's diagnostics from its durable row on request."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self.sessions = sessions
        self.clock = clock or (lambda: datetime.now(UTC))

    def read(self, operation_id: str, attempt: int) -> FailureEvidenceBundle:
        """Return the redacted bundle, or raise ``KeyError`` for no failed attempt."""
        item = self._failed_item(operation_id, attempt)
        if item is None:
            raise KeyError(operation_id)
        now = _aware(self.clock())
        try:
            return collect_failure(item, now=now)
        except Exception:  # noqa: BLE001 - diagnostics cannot replace the original operation result
            return _fallback_bundle(item, now=now)

    def decorate(self, item: OperationItem) -> OperationItem:
        """Name the diagnostics download when this failed attempt has one."""
        if item.state not in FAILED_ATTEMPT_STATES and not item.blockers:
            return item
        if self._failed_item(item.id, item.attempt) is None:
            return item
        return item.with_evidence_download(
            OperationEvidenceDownload(href=evidence_href(item.id, item.attempt))
        )

    def _failed_item(self, operation_id: str, attempt: int) -> FailedAttempt | None:
        """Load one failed attempt from whichever durable family owns the id."""
        with self.sessions() as session:
            row = session.execute(
                select(AgentOperation, AgentOperationAttempt)
                .join(
                    AgentOperationAttempt,
                    AgentOperationAttempt.operation_id == AgentOperation.id,
                )
                .where(
                    AgentOperation.id == operation_id,
                    AgentOperationAttempt.attempt == attempt,
                    failed_attempt_condition(AgentOperation, AgentOperationAttempt),
                )
            ).first()
            if row is not None:
                operation, member = row
                return self._item(
                    operation,
                    attempt=member.attempt,
                    kind=operation.kind,
                    node_ids=[operation.node_id],
                    source="agent",
                    progress=member.progress,
                    result=member.result,
                )
            job = session.get(Job, operation_id)
            if (
                job is not None
                and job.state in FAILED_ATTEMPT_STATES
                and job.current_attempt == attempt
            ):
                return self._item(
                    job,
                    attempt=attempt,
                    kind=job.kind,
                    node_ids=job.targets,
                    source="controller",
                    progress=None,
                    result=job.result,
                )
            cache = session.get(ModelCacheOperation, operation_id)
            if (
                cache is not None
                and cache.state in FAILED_ATTEMPT_STATES
                and cache.attempt == attempt
            ):
                return self._item(
                    cache,
                    attempt=attempt,
                    kind=cache.kind,
                    node_ids=[],
                    source="controller",
                    progress=cache.progress,
                    result=None,
                )
            application = session.get(FleetProfileApplication, operation_id)
            if application is not None and application.state in FAILED_ATTEMPT_STATES:
                from .fleet_profiles import FleetProfileService

                projected = operation_item(
                    FleetProfileService._operation_item(application)
                )
                if projected.attempt != attempt:
                    return None
                failure = (
                    None
                    if projected.failure is None
                    else projected.failure.model_dump(mode="json")
                )
                return FailedAttempt(
                    id=projected.id,
                    attempt=projected.attempt,
                    kind=projected.kind,
                    node_ids=projected.node_ids,
                    updated_at=projected.updated_at or "",
                    source="controller",
                    # The Controller owns this record: no Spark operation ran, so
                    # there are no agent observations to be missing.
                    agent_operation=False,
                    progress=AttemptPhase.model_validate(projected.progress),
                    result=(
                        OperationResultFacts.model_validate(failure)
                        if projected.result is None
                        else projected.result
                    ),
                    blockers=projected.blockers,
                )
        return None

    @staticmethod
    def _item(
        operation,
        *,
        attempt: int,
        kind: str,
        node_ids: list[str],
        source: EvidenceSource,
        progress: object,
        result: object,
    ) -> FailedAttempt:
        payload = getattr(operation, "payload", None) or {}
        return FailedAttempt(
            id=operation.id,
            attempt=attempt,
            kind=kind,
            node_ids=node_ids,
            updated_at=_aware(operation.updated_at).isoformat(),
            source=source,
            rank=payload.get("rank") if type(payload.get("rank")) is int else None,
            progress=AttemptPhase.model_validate(progress),
            result=OperationResultFacts.model_validate(
                result
                or payload.get("failure")
                or {
                    "reason": getattr(operation, "last_error", None)
                    or getattr(operation, "status_reason", None)
                    or "Operation failed"
                }
            ),
        )
