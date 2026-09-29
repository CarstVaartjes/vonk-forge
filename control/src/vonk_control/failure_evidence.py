"""Failure diagnostics rendered on request from durable failure rows.

No commands or network calls run here, and nothing is stored: the bundle is a
redacted view of the attempt result, job result or progress the Controller
already keeps, so an offline node never delays it and a later read never
disagrees with the row it describes.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import quote

from pydantic import ConfigDict, Field, TypeAdapter
from sqlalchemy import String, and_, cast, or_, select
from vonk_agent_protocol.failure_evidence import FailureDiagnostics, FailureLogTail

from .bounded_json import BoundedJSONError, mapping, require_integer, sequence
from .logging import redact_text
from .models import (
    AgentOperation,
    AgentOperationAttempt,
    FleetProfileApplication,
    Job,
    ModelCacheOperation,
)
from .operation_contract import OperationEvidenceDownload
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


#: The attempt states whose own terminal failure receipt must stay readable.
#: ``expired`` is not among them by itself: a lease lapse or a supersession owns
#: no receipt, and its operation-level reason may already describe a later
#: attempt.  Only a receipt the attempt actually kept, or the park that still
#: names it, qualifies.
FAILED_ATTEMPT_STATES = ("failed", "waiting-for-operator")
#: The one state a superseded or lapsed attempt keeps while owning no receipt.
EXPIRED_ATTEMPT_STATE = "expired"


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


def failure_code(result: Mapping[str, object]) -> tuple[str, str | None]:
    """Return the stable error code and redacted detail a failure result names."""
    code = result.get("error_code") or result.get("code") or "operation_failed"
    code = re.sub(r"[^a-z0-9_]", "_", str(code).lower())[:64]
    if not code or not code[0].isalpha():
        code = "operation_failed"
    detail = (
        result.get("detail")
        or result.get("diagnostic")
        or result.get("helper_error_code")
    )
    return code, safe_text(str(detail))[:1024] if detail is not None else None


def classification(kind: str, result: Mapping[str, object]) -> FailureCategory:
    # Phase/state comes from typed fields. Classification uses stable emitted
    # error/diagnostic codes, never searches logs to invent operation progress.
    code = " ".join(
        str(result.get(key, ""))
        for key in ("error_code", "code", "diagnostic", "helper_error_code")
    ).casefold()
    for category, markers in _CATEGORY_MARKERS:
        if any(marker in code for marker in markers):
            return category
    return "runtime" if kind.startswith(("recipe.", "agent.upgrade")) else "unknown"


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


def collect_failure(
    item: Mapping[str, object], *, now: datetime
) -> FailureEvidenceBundle:
    result = mapping(item.get("result")) or {}
    progress = mapping(item.get("progress")) or {}
    errors: list[str] = []
    raw = result.get("diagnostics")
    if raw is not None:
        try:
            diagnostics = sanitize_diagnostics(raw)
        except (ValueError, TypeError):
            errors.append("agent-diagnostics-invalid")
            raw = None
    if raw is None:
        phase = progress.get("phase") or result.get("stage") or "unknown"
        diagnostics = FailureDiagnostics(
            collected_at=now.isoformat(),
            phase=safe_text(str(phase))[:80],
            category=classification(str(item["kind"]), result),
            stdout=log_tail(str(result.get("stdout", ""))),
            stderr=log_tail(str(result.get("stderr", result.get("log_excerpt", "")))),
            versions=[],
            sandbox=[],
            storage=[],
            preflight=[],
            collector_errors=["agent-observations-unavailable"]
            if item.get("node_ids")
            else [],
        )
    error_code, detail = failure_code(result)
    summary = (
        result.get("summary")
        or result.get("reason")
        or result.get("detail")
        or item.get("failure")
        or "Operation failed"
    )
    node_ids = _required_node_ids(item.get("node_ids"))
    source: EvidenceSource
    if "source" in item:
        source = _EVIDENCE_SOURCE.validate_python(item["source"], strict=True)
    else:
        source = "agent" if item.get("node_ids") else "controller"
    return FailureEvidenceBundle(
        context=EvidenceContext(
            operation_id=_required_text(item["id"], "operation id"),
            attempt=require_integer(item.get("attempt"), "operation attempt"),
            kind=_required_text(item["kind"], "operation kind"),
            node_ids=node_ids[:128],
            updated_at=_required_text(item["updated_at"], "operation updated_at"),
            source=source,
            rank=_optional_int(item.get("rank"), "operation rank"),
        ),
        collected_at=now.isoformat(),
        summary=safe_text(str(summary))[:512],
        error_code=error_code,
        detail=detail,
        diagnostics=diagnostics,
        collector_errors=errors,
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
        attempt.state.in_(FAILED_ATTEMPT_STATES),
        and_(
            attempt.state == EXPIRED_ATTEMPT_STATE,
            # A receipt is a JSON object.  An absent one is stored as the JSON
            # ``null`` value rather than SQL NULL, so the plain ``IS NOT NULL``
            # test would read every bare lapse as if it had kept a receipt.
            cast(attempt.result, String) != "null",
        ),
        and_(
            operation.state == "waiting-for-operator",
            attempt.attempt == operation.current_attempt,
        ),
    )


def _fallback_bundle(
    item: Mapping[str, object], *, now: datetime
) -> FailureEvidenceBundle:
    """A bounded bundle for a row the collector could not read.

    The download reports the collector failure separately and never echoes the
    exception, which may quote the unredacted value that broke it.
    """
    empty = FailureLogTail(text="", truncated=False, dropped_bytes=0, dropped_lines=0)
    result = mapping(item.get("result")) or {}
    error_code, _ = failure_code(result)
    return FailureEvidenceBundle(
        context=EvidenceContext(
            operation_id=str(item["id"]),
            attempt=require_integer(item["attempt"], "operation attempt"),
            kind=str(item["kind"]),
            node_ids=_required_node_ids(item.get("node_ids"))[:128],
            updated_at=str(item["updated_at"]),
            source="agent" if item.get("node_ids") else "controller",
        ),
        collected_at=now.isoformat(),
        summary=safe_text(
            str(result.get("reason") or result.get("summary") or "Operation failed")
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

    def __init__(self, sessions, *, clock=None):
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

    def decorate(self, item: Mapping[str, object]) -> dict[str, object]:
        """Name the diagnostics download when this failed attempt has one."""
        if item.get("state") not in FAILED_ATTEMPT_STATES:
            return dict(item)
        operation_id = _required_text(item["id"], "operation id")
        attempt = require_integer(item["attempt"], "operation attempt")
        if self._failed_item(operation_id, attempt) is None:
            return dict(item)
        return dict(
            item,
            evidence_download=OperationEvidenceDownload(
                href=evidence_href(operation_id, attempt)
            ).model_dump(mode="json"),
        )

    def _failed_item(self, operation_id: str, attempt: int) -> dict[str, object] | None:
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

                item = FleetProfileService._operation_item(application)
                if item["attempt"] != attempt:
                    return None
                item["source"] = "controller"
                item["result"] = item["result"] or item["failure"]
                return item
        return None

    @staticmethod
    def _item(
        operation,
        *,
        attempt: int,
        kind: str,
        node_ids: object,
        source: EvidenceSource,
        progress: object,
        result: object,
    ) -> dict[str, object]:
        payload = getattr(operation, "payload", None) or {}
        return {
            "id": operation.id,
            "attempt": attempt,
            "kind": kind,
            "node_ids": node_ids,
            "updated_at": _aware(operation.updated_at).isoformat(),
            "source": source,
            "rank": payload.get("rank") if type(payload.get("rank")) is int else None,
            "progress": progress,
            "result": result
            or payload.get("failure")
            or {
                "reason": getattr(operation, "last_error", None)
                or getattr(operation, "status_reason", None)
                or "Operation failed"
            },
        }
