"""Transactional durable job queue with lease fencing."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import InvalidRequestReason, SecurityRefusalError

from .categorized_errors import InvalidType, InvalidValue, MissingRecord
from .compiled_execution_plan import MAX_COMPILED_EXECUTION_PLAN_BYTES
from .lifecycle.job import JobAdapter
from .models import AgentOperation, Job, JobAttempt

_SENSITIVE = re.compile(r"(?i)(password|secret|token|private.?key|authorization)")
_MAX_PAYLOAD = 65_536
_TARGETS = TypeAdapter(list[str])


class StaleAttempt(SecurityRefusalError, RuntimeError):
    """The attempt's lease or fence is not current: it has no authority to write."""


@dataclass(frozen=True)
class AttemptFence:
    job_id: str
    attempt: int
    fence: str
    worker_id: str
    lease_deadline: datetime
    kind: str
    payload: Mapping[str, object]
    authority_revision: str
    targets: tuple[str, ...]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _validated_quota_fields(
    kind: str | None, payload: Mapping[str, object]
) -> frozenset[tuple[str, ...]]:
    if kind != "reconcile":
        return frozenset()
    routes = payload.get("routes")
    if not isinstance(routes, Mapping):
        return frozenset()
    accepted: set[tuple[str, ...]] = set()
    route_fields = {
        "workload_id",
        "nodes",
        "entrypoint_node_id",
        "scheme",
        "port",
        "path",
        "quota",
        "quota_digest",
    }
    for alias, raw_route in routes.items():
        if not isinstance(alias, str) or not isinstance(raw_route, Mapping):
            continue
        quota = raw_route.get("quota")
        nodes = raw_route.get("nodes")
        if (
            set(raw_route) != route_fields
            or not isinstance(raw_route.get("workload_id"), str)
            or not isinstance(nodes, list)
            or not nodes
            or len(nodes) != len(set(nodes))
            or not all(isinstance(node_id, str) for node_id in nodes)
            or raw_route.get("entrypoint_node_id") not in nodes
            or raw_route.get("scheme") not in {"http", "https"}
            or not isinstance(raw_route.get("port"), int)
            or isinstance(raw_route.get("port"), bool)
            or not 1 <= raw_route["port"] <= 65535
            or not isinstance(raw_route.get("path"), str)
            or not raw_route["path"].startswith("/")
            or not isinstance(quota, Mapping)
            or set(quota) != {"requests_per_minute", "tokens_per_minute"}
        ):
            continue
        rpm = quota.get("requests_per_minute")
        tpm = quota.get("tokens_per_minute")
        quota_digest = hashlib.sha256(
            json.dumps(quota, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if (
            not isinstance(rpm, int)
            or isinstance(rpm, bool)
            or not 1 <= rpm <= 100_000
            or not isinstance(tpm, int)
            or isinstance(tpm, bool)
            or not 1 <= tpm <= 100_000_000
            or raw_route.get("quota_digest") != quota_digest
        ):
            continue
        accepted.add(("routes", alias, "quota", "tokens_per_minute"))
    return frozenset(accepted)


def _canonical_payload(
    payload: Mapping[str, object], *, kind: str | None = None
) -> tuple[dict[str, object], bytes]:
    safe_quota_fields = _validated_quota_fields(kind, payload)

    def inspect(value: object, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise InvalidType(
                        "job payload keys must be strings",
                        reason=InvalidRequestReason.MALFORMED,
                    )
                child_path = path + (key,)
                if _SENSITIVE.search(key) and child_path not in safe_quota_fields:
                    raise InvalidValue(
                        "job payload contains a sensitive field",
                        reason=InvalidRequestReason.MALFORMED,
                    )
                inspect(child, child_path)
        elif isinstance(value, list):
            for child in value:
                inspect(child, path)

    inspect(payload)
    copied = json.loads(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )
    encoded = json.dumps(
        copied,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    maximum = (
        MAX_COMPILED_EXECUTION_PLAN_BYTES
        if kind in {"recipe.install", "recipe.start"}
        else _MAX_PAYLOAD
    )
    if len(encoded) > maximum:
        raise InvalidValue(
            "job payload is too large", reason=InvalidRequestReason.LIMIT_EXCEEDED
        )
    return copied, encoded


def _canonical_targets(value: object) -> list[str]:
    """Validate one target sequence through the JSON contract."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        return _TARGETS.validate_json(encoded, strict=True)
    except (TypeError, ValueError, ValidationError) as error:
        raise InvalidValue(
            "job targets must be a JSON array of strings",
            reason=InvalidRequestReason.MALFORMED,
        ) from error


_ADAPTER = JobAdapter()


class JobService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._claim_lock = threading.RLock()

    def enqueue(
        self,
        kind: str,
        actor: str,
        authority_revision: str,
        targets: Sequence[str],
        payload: Mapping[str, object],
        *,
        request_id: str | None = None,
    ) -> Job:
        if not all(value.strip() for value in (kind, actor, authority_revision)):
            raise InvalidValue(
                "job kind, actor, and authority revision are required",
                reason=InvalidRequestReason.INCOMPLETE,
            )
        clean_targets = _canonical_targets(targets)
        clean, encoded = _canonical_payload(payload, kind=kind)
        now = self._clock()
        job = _ADAPTER.new_job(
            request_id=request_id or str(uuid.uuid4()),
            kind=kind,
            actor=actor,
            authority_revision=authority_revision,
            targets=clean_targets,
            payload_digest=hashlib.sha256(encoded).hexdigest(),
            payload=clean,
            current_attempt=0,
            created_at=now,
            updated_at=now,
        )
        try:
            with self._sessions.begin() as session:
                existing = session.scalar(
                    select(Job).where(Job.request_id == job.request_id)
                )
                if existing is not None:
                    _canonical_targets(existing.targets)
                    if not self._same_request(existing, job):
                        raise InvalidValue(
                            "request key was already used differently",
                            reason=InvalidRequestReason.CONFLICT,
                        )
                    session.expunge(existing)
                    return existing
                session.add(job)
            return job
        except IntegrityError:
            with self._sessions() as session:
                existing = session.scalar(
                    select(Job).where(Job.request_id == job.request_id)
                )
                if existing is None or not self._same_request(existing, job):
                    raise InvalidValue(
                        "request key was already used differently",
                        reason=InvalidRequestReason.CONFLICT,
                    ) from None
                session.expunge(existing)
                return existing

    def get(self, job_id: str) -> Job:
        with self._sessions() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise MissingRecord(job_id, reason=InvalidRequestReason.NOT_FOUND)
            job.targets = _canonical_targets(job.targets)
            session.expunge(job)
            return job

    def enqueue_guarded(
        self,
        kind: str,
        actor: str,
        authority_revision: str,
        targets: Sequence[str],
        payload: Mapping[str, object],
        *,
        authority_check: Callable[[], bool],
        request_id: str | None = None,
    ) -> Job:
        """Create a job only while its external acceptance evidence stays current."""

        if not callable(authority_check):
            raise InvalidType(
                "job enqueue authority check is invalid",
                reason=InvalidRequestReason.MALFORMED,
            )
        if not all(value.strip() for value in (kind, actor, authority_revision)):
            raise InvalidValue(
                "job kind, actor, and authority revision are required",
                reason=InvalidRequestReason.INCOMPLETE,
            )
        clean_targets = _canonical_targets(targets)
        clean, encoded = _canonical_payload(payload, kind=kind)
        now = self._clock()
        job = _ADAPTER.new_job(
            request_id=request_id or str(uuid.uuid4()),
            kind=kind,
            actor=actor,
            authority_revision=authority_revision,
            targets=clean_targets,
            payload_digest=hashlib.sha256(encoded).hexdigest(),
            payload=clean,
            current_attempt=0,
            created_at=now,
            updated_at=now,
        )
        try:
            with self._sessions.begin() as session:
                existing = session.scalar(
                    select(Job).where(Job.request_id == job.request_id)
                )
                if existing is not None:
                    _canonical_targets(existing.targets)
                    if not self._same_request(existing, job):
                        raise InvalidValue(
                            "request key was already used differently",
                            reason=InvalidRequestReason.CONFLICT,
                        )
                    session.expunge(existing)
                    return existing
                if authority_check() is not True:
                    raise InvalidValue(
                        "fleet acceptance evidence is stale",
                        reason=InvalidRequestReason.SUPERSEDED,
                    )
                session.add(job)
                session.flush()
                if authority_check() is not True:
                    raise InvalidValue(
                        "fleet acceptance evidence is stale",
                        reason=InvalidRequestReason.SUPERSEDED,
                    )
            return job
        except IntegrityError:
            with self._sessions() as session:
                existing = session.scalar(
                    select(Job).where(Job.request_id == job.request_id)
                )
                if existing is None or not self._same_request(existing, job):
                    raise InvalidValue(
                        "request key was already used differently",
                        reason=InvalidRequestReason.CONFLICT,
                    ) from None
                session.expunge(existing)
                return existing

    @staticmethod
    def _same_request(existing: Job, requested: Job) -> bool:
        """Compare immutable request semantics for safe idempotent replay."""

        return (
            existing.kind == requested.kind
            and existing.actor == requested.actor
            and existing.authority_revision == requested.authority_revision
            and existing.targets == requested.targets
            and existing.payload_digest == requested.payload_digest
        )

    def claim(
        self, worker_id: str, lease_seconds: int, *, kinds: Sequence[str]
    ) -> AttemptFence | None:
        if not worker_id.strip() or lease_seconds <= 0:
            raise InvalidValue(
                "worker and positive lease are required",
                reason=InvalidRequestReason.INCOMPLETE,
            )
        now = self._clock()
        with self._claim_lock, self._sessions.begin() as session:
            statement = (
                select(Job)
                .where(
                    # Claim only work this executor handles. Coordinators may
                    # be queued between turns without an AgentOperation yet.
                    Job.kind.in_(kinds),
                    ~select(AgentOperation.id)
                    .where(AgentOperation.parent_job_id == Job.id)
                    .exists(),
                    or_(
                        Job.state == "queued",
                        Job.id.in_(
                            select(JobAttempt.job_id).where(
                                JobAttempt.state == "running",
                                JobAttempt.lease_deadline < now,
                            )
                        ),
                    ),
                )
                .order_by(Job.created_at, Job.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            job = session.scalars(statement).first()
            if job is None:
                return None
            clean_targets = _canonical_targets(job.targets)
            deadline = now + timedelta(seconds=lease_seconds)
            fence = str(uuid.uuid4())
            claimed = _ADAPTER.claim(
                session,
                job,
                worker_id=worker_id,
                fence=fence,
                lease_deadline=deadline,
                now=now,
            )
            if claimed is None:
                return None
            return AttemptFence(
                job.id,
                job.current_attempt,
                fence,
                worker_id,
                deadline,
                job.kind,
                dict(job.payload),
                job.authority_revision,
                tuple(clean_targets),
            )

    def _active(self, session: Session, fence: AttemptFence) -> tuple[Job, JobAttempt]:
        job = session.get(Job, fence.job_id)
        attempt = session.scalar(
            select(JobAttempt).where(JobAttempt.fence == fence.fence)
        )
        if (
            job is None
            or attempt is None
            or job.current_attempt != fence.attempt
            or attempt.state != "running"
            or attempt.worker_id != fence.worker_id
            or _aware(attempt.lease_deadline) <= _aware(self._clock())
        ):
            raise StaleAttempt("job attempt lease or fence is stale")
        _canonical_targets(job.targets)
        return job, attempt

    def heartbeat(self, fence: AttemptFence, lease_seconds: int) -> AttemptFence:
        if lease_seconds <= 0:
            raise InvalidValue(
                "lease must be positive", reason=InvalidRequestReason.OUT_OF_RANGE
            )
        with self._sessions.begin() as session:
            job, attempt = self._active(session, fence)
            deadline = self._clock() + timedelta(seconds=lease_seconds)
            _ADAPTER.heartbeat(job, attempt, deadline, self._clock())
            return AttemptFence(
                fence.job_id,
                fence.attempt,
                fence.fence,
                fence.worker_id,
                deadline,
                fence.kind,
                fence.payload,
                fence.authority_revision,
                fence.targets,
            )

    def _finish(
        self,
        fence: AttemptFence,
        state: str,
        result: Mapping[str, object] | None,
        reason: str | None,
    ) -> None:
        with self._sessions.begin() as session:
            job, attempt = self._active(session, fence)
            _ADAPTER.finish(
                job,
                attempt,
                succeeded=state == "succeeded",
                result=result,
                reason=reason,
                now=self._clock(),
            )

    def succeed(self, fence: AttemptFence, result: Mapping[str, object]) -> None:
        clean, _ = _canonical_payload(result)
        self._finish(fence, "succeeded", clean, None)

    def fail(self, fence: AttemptFence, reason: str) -> None:
        self._finish(fence, "failed", None, reason)
