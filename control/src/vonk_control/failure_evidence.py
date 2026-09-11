"""Automatic diagnostics from durable failures, isolated from execution.

No commands or network calls run here. Agent observations are already captured
at the failure; an offline node therefore never delays Controller collection.
"""

from __future__ import annotations

import hashlib
import re
import time
import unicodedata
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import quote

from pydantic import ConfigDict, Field, TypeAdapter
from sqlalchemy import and_, func, or_, select
from vonk_agent_protocol.failure_evidence import FailureDiagnostics, FailureLogTail

from .bounded_json import BoundedJSONError, mapping, require_integer, sequence
from .failure_evidence_models import FailureEvidenceCursor, FailureEvidenceRecord
from .logging import redact_text
from .models import (
    AgentOperation,
    AgentOperationAttempt,
    FleetProfileApplication,
    Job,
    ModelCacheOperation,
)
from .operation_contract import (
    OperationEvidenceDownload,
    OperationEvidenceProvenance,
    OperationFailureEvidence,
)
from .strict_json import StrictJSONModel

MAX_BUNDLE_BYTES = 32 * 1024
MAX_LOG_BYTES = 2048
MAX_LOG_LINES = 32
COLLECTION_SECONDS = 0.25
_SENSITIVE = re.compile(
    r"password|secret|token|authorization|cookie|credential|private.?key|api.?key|environment",
    re.IGNORECASE,
)
_SECRET_LINE = re.compile(
    r"password|secret|token|authorization|cookie|credential|private[ _-]?key|api[ _-]?key|-----BEGIN|-----END",
    re.IGNORECASE,
)
_OPAQUE_SECRET = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9+/=_-]{40,}(?![A-Za-z0-9])")

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
    omitted_node_count: int = Field(default=0, ge=0)
    authority_revision: str | None = None
    plan_digest: str | None = None
    payload_digest: str | None = None
    updated_at: str
    source: EvidenceSource
    rank: int | None = Field(default=None, ge=0)


class FailureEvidenceBundle(EvidenceModel):
    schema_version: Literal[2] = 2
    context: EvidenceContext
    collected_at: str
    summary: str = Field(max_length=512)
    diagnostics: FailureDiagnostics
    receipt: OperationFailureEvidence
    collector_errors: list[str] = Field(max_length=8)


class EvidenceRetention(EvidenceModel):
    days: int = Field(default=14, ge=1, le=365)
    max_entries: int = Field(default=2000, ge=1, le=10000)
    max_bytes: int = Field(default=64 * 1024**2, ge=MAX_BUNDLE_BYTES)


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


def failure_receipt(result: Mapping[str, object]) -> OperationFailureEvidence:
    """Reuse the fixed operation failure contract; never export a loose receipt."""
    code = result.get("error_code") or result.get("code") or "operation_failed"
    code = re.sub(r"[^a-z0-9_]", "_", str(code).lower())[:64]
    if not code or not code[0].isalpha():
        code = "operation_failed"
    summary = (
        safe_text(
            str(
                result.get("summary")
                or result.get("reason")
                or result.get("detail")
                or "Operation failed"
            )
        )[:256]
        or "Operation failed"
    )
    detail = (
        result.get("detail")
        or result.get("diagnostic")
        or result.get("helper_error_code")
    )
    return OperationFailureEvidence(
        error_code=code,
        summary=summary,
        detail=safe_text(str(detail))[:1024] if detail is not None else None,
        retryable=result.get("retryable") is True,
        uncertain=result.get("uncertain") is True,
    )


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


def _optional_text(value: object, detail: str) -> str | None:
    """Return a persisted optional string without coercing a wrong JSON type."""

    return None if value is None else _required_text(value, detail)


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
    diagnostics = FailureDiagnostics.model_validate(value)
    document = diagnostics.model_dump(mode="json")
    for field in ("stdout", "stderr"):
        document[field]["text"] = safe_text(document[field]["text"])[:MAX_LOG_BYTES]
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
    diagnostics = FailureDiagnostics.model_validate(document)
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
    receipt = failure_receipt(result)
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
            omitted_node_count=max(0, len(node_ids) - 128),
            authority_revision=_optional_text(
                item.get("authority_revision"), "operation authority revision"
            ),
            plan_digest=_optional_text(
                item.get("plan_digest"), "operation plan digest"
            ),
            payload_digest=_optional_text(
                item.get("payload_digest"), "operation payload digest"
            ),
            updated_at=_required_text(item["updated_at"], "operation updated_at"),
            source=source,
            rank=_optional_int(item.get("rank"), "operation rank"),
        ),
        collected_at=now.isoformat(),
        summary=safe_text(str(summary))[:512],
        diagnostics=diagnostics,
        receipt=receipt,
        collector_errors=errors,
    )


class FailureEvidenceService:
    def __init__(
        self, sessions, *, clock=None, retention: EvidenceRetention | None = None
    ):
        self.sessions = sessions
        self.clock = clock or (lambda: datetime.now(UTC))
        self.retention = retention or EvidenceRetention()
        self.last_collection_error: str | None = None
        self._next_family = 0

    def capture(self, item: Mapping[str, object]) -> bool:
        now = _aware(self.clock())
        try:
            bundle = collect_failure(item, now=now)
        except Exception:  # noqa: BLE001 - diagnostics cannot replace the original operation result
            # Keep a separately typed collector failure without re-running the
            # failed collector or altering the original durable result.
            empty = FailureLogTail(
                text="", truncated=False, dropped_bytes=0, dropped_lines=0
            )
            result = mapping(item.get("result")) or {}
            node_ids = _required_node_ids(item.get("node_ids"))
            bundle = FailureEvidenceBundle(
                context=EvidenceContext(
                    operation_id=str(item["id"]),
                    attempt=require_integer(item["attempt"], "operation attempt"),
                    kind=str(item["kind"]),
                    node_ids=node_ids[:128],
                    omitted_node_count=max(0, len(node_ids) - 128),
                    updated_at=str(item["updated_at"]),
                    source="agent" if item.get("node_ids") else "controller",
                ),
                collected_at=now.isoformat(),
                summary=safe_text(
                    str(
                        result.get("reason")
                        or result.get("summary")
                        or "Operation failed"
                    )
                )[:512],
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
                receipt=failure_receipt(result),
                collector_errors=["collector-failed"],
            )
        content = bundle.model_dump_json().encode()
        if len(content) > MAX_BUNDLE_BYTES:
            raise ValueError("failure evidence exceeds its storage bound")
        digest = hashlib.sha256(content).hexdigest()
        with self.sessions.begin() as session:
            key = (bundle.context.operation_id, bundle.context.attempt)
            if session.get(FailureEvidenceRecord, key) is not None:
                return False
            session.add(
                FailureEvidenceRecord(
                    operation_id=key[0],
                    attempt=key[1],
                    sha256=digest,
                    content=content,
                    collected_at=now,
                )
            )
        self.prune()
        return True

    def read(
        self, operation_id: str, attempt: int | None = None
    ) -> tuple[bytes, str, FailureEvidenceBundle]:
        with self.sessions() as session:
            query = select(FailureEvidenceRecord).where(
                FailureEvidenceRecord.operation_id == operation_id
            )
            if attempt is not None:
                query = query.where(FailureEvidenceRecord.attempt == attempt)
            row = session.scalar(
                query.order_by(FailureEvidenceRecord.attempt.desc()).limit(1)
            )
            if row is None or _aware(row.collected_at) < _aware(
                self.clock()
            ) - timedelta(days=self.retention.days):
                raise KeyError(operation_id)
            content, digest = row.content, row.sha256
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("failure evidence digest mismatch")
        bundle = FailureEvidenceBundle.model_validate_json(content)
        return content, digest, bundle

    def decorate(self, item: Mapping[str, object]) -> dict[str, object]:
        result: dict[str, object] = dict(mapping(item.get("result")) or {})
        try:
            content, digest, bundle = self.read(
                _required_text(item["id"], "operation id"),
                require_integer(item["attempt"], "operation attempt"),
            )
        except (KeyError, ValueError, OSError):
            return dict(item)
        result["evidence_download"] = OperationEvidenceDownload(
            media_type="application/json",
            size_bytes=len(content),
            sha256=digest,
            href=f"/api/operations/{quote(str(item['id']), safe='')}/evidence?attempt={item['attempt']}",
        ).model_dump(mode="json")
        result["provenance"] = OperationEvidenceProvenance(
            source=bundle.context.source,
            collected_at=bundle.collected_at,
            evidence_digest=digest,
            authority_revision=bundle.context.authority_revision,
        ).model_dump(mode="json")
        return dict(item, result=result)

    def prune(self) -> int:
        with self.sessions.begin() as session:
            rows = session.execute(
                select(
                    FailureEvidenceRecord.operation_id,
                    FailureEvidenceRecord.attempt,
                    FailureEvidenceRecord.collected_at,
                    func.length(FailureEvidenceRecord.content).label("size_bytes"),
                ).order_by(
                    FailureEvidenceRecord.collected_at.desc(),
                    FailureEvidenceRecord.operation_id,
                )
            ).all()
            total_bytes = 0
            removed = 0
            cutoff = _aware(self.clock()) - timedelta(days=self.retention.days)
            for index, row in enumerate(rows):
                total_bytes += row.size_bytes
                if (
                    index >= self.retention.max_entries
                    or total_bytes > self.retention.max_bytes
                    or _aware(row.collected_at) < cutoff
                ):
                    record = session.get(
                        FailureEvidenceRecord, (row.operation_id, row.attempt)
                    )
                    if record is not None:
                        session.delete(record)
                    removed += 1
            return removed

    def tick(self, *, limit: int = 32) -> bool:
        """Incrementally snapshot failed attempts; cursor survives retention/restart."""
        deadline = time.monotonic() + COLLECTION_SECONDS
        captured = False
        try:
            self.prune()
            families = ("agent", "job", "model-cache", "fleet-profile")
            ordered = families[self._next_family :] + families[: self._next_family]
            self._next_family = (self._next_family + 1) % len(families)
            for family in ordered:
                if time.monotonic() >= deadline:
                    break
                for item in self._page(family, max(1, min(limit, 32))):
                    if time.monotonic() >= deadline:
                        break
                    captured = self.capture(item) or captured
                    with self.sessions.begin() as session:
                        cursor = session.get(FailureEvidenceCursor, family)
                        if cursor is None:
                            cursor = FailureEvidenceCursor(family=family)
                            session.add(cursor)
                        updated_at = _required_text(
                            item["updated_at"], "operation updated_at"
                        )
                        cursor.updated_at = datetime.fromisoformat(updated_at)
                        cursor.operation_id = _required_text(item["id"], "operation id")
                        cursor.attempt = require_integer(
                            item["attempt"], "operation attempt"
                        )
            self.last_collection_error = None
        except Exception:  # noqa: BLE001 - worker diagnostics must not interrupt execution
            self.last_collection_error = "failure-evidence-collection-unavailable"
        return captured

    def _page(self, family: str, limit: int) -> list[dict[str, object]]:
        with self.sessions() as session:
            cursor = session.get(FailureEvidenceCursor, family)
            cutoff = _aware(self.clock()) - timedelta(days=self.retention.days)
            after = (
                max(cutoff, _aware(cursor.updated_at)) if cursor is not None else cutoff
            )
            last_id = cursor.operation_id if cursor is not None else ""
            last_attempt = cursor.attempt if cursor is not None else -1
            model = {
                "agent": AgentOperation,
                "job": Job,
                "model-cache": ModelCacheOperation,
                "fleet-profile": FleetProfileApplication,
            }[family]
            attempt = (
                AgentOperationAttempt.attempt
                if family == "agent"
                else model.attempt
                if family == "model-cache"
                else model.progress["attempt"].as_integer()
                if family == "fleet-profile"
                else model.current_attempt
            )
            state = AgentOperationAttempt.state if family == "agent" else model.state
            query = (
                select(model, AgentOperationAttempt)
                if family == "agent"
                else select(model)
            )
            if family == "agent":
                query = query.join(
                    AgentOperationAttempt,
                    AgentOperationAttempt.operation_id == model.id,
                )
            query = (
                query.where(
                    state.in_(["failed", "waiting-for-operator"]),
                    or_(
                        model.updated_at > after,
                        and_(model.updated_at == after, model.id > last_id),
                        and_(
                            model.updated_at == after,
                            model.id == last_id,
                            attempt > last_attempt,
                        ),
                    ),
                )
                .order_by(model.updated_at, model.id, attempt)
                .limit(limit)
            )
            result = []
            for row in session.execute(query):
                operation = row[0]
                member = row[1] if family == "agent" else operation
                if family == "fleet-profile":
                    from .fleet_profiles import FleetProfileService

                    item = FleetProfileService._operation_item(operation)
                    item["source"] = "controller"
                    item["plan_digest"] = operation.plan_digest
                    item["result"] = item["result"] or item["failure"]
                    result.append(item)
                    continue
                number = (
                    member.attempt
                    if family in {"agent", "model-cache"}
                    else member.current_attempt
                )
                payload = getattr(operation, "payload", {}) or {}
                result.append(
                    {
                        "id": operation.id,
                        "attempt": number,
                        "kind": "fleet-profile.apply"
                        if family == "fleet-profile"
                        else operation.kind,
                        "node_ids": [operation.node_id]
                        if family == "agent"
                        else getattr(operation, "targets", []),
                        "authority_revision": getattr(
                            operation, "authority_revision", None
                        ),
                        "payload_digest": getattr(operation, "payload_digest", None),
                        "plan_digest": getattr(operation, "plan_digest", None)
                        or payload.get("plan_digest"),
                        "updated_at": _aware(operation.updated_at).isoformat(),
                        "source": "agent" if family == "agent" else "controller",
                        "rank": payload.get("rank")
                        if type(payload.get("rank")) is int
                        else None,
                        "progress": getattr(member, "progress", None),
                        "result": getattr(member, "result", None)
                        or payload.get("failure")
                        or {
                            "reason": getattr(operation, "last_error", None)
                            or getattr(operation, "status_reason", None)
                            or "Operation failed"
                        },
                    }
                )
            return result
