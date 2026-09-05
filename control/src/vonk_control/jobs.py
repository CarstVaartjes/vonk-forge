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

from sqlalchemy import String, cast, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .auth import CursorCodec
from .logging import redact_text
from .models import Job, JobAttempt

_SENSITIVE = re.compile(r"(?i)(password|secret|token|private.?key|authorization)")
_MAX_PAYLOAD = 65_536


class StaleAttempt(RuntimeError):
    pass


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
                    raise TypeError("job payload keys must be strings")
                child_path = path + (key,)
                if _SENSITIVE.search(key) and child_path not in safe_quota_fields:
                    raise ValueError("job payload contains a sensitive field")
                inspect(child, child_path)
        elif isinstance(value, list):
            for child in value:
                inspect(child, path)

    inspect(payload)
    copied = json.loads(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    encoded = json.dumps(copied, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > _MAX_PAYLOAD:
        raise ValueError("job payload is too large")
    return copied, encoded


class JobService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        cursors: CursorCodec | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._cursors = cursors
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
        reconciliation_id: str | None = None,
    ) -> Job:
        if not all(value.strip() for value in (kind, actor, authority_revision)):
            raise ValueError("job kind, actor, and authority revision are required")
        if reconciliation_id is not None:
            try:
                reconciliation_id = str(uuid.UUID(reconciliation_id))
            except (AttributeError, TypeError, ValueError) as error:
                raise ValueError("reconciliation identity is invalid") from error
        clean, encoded = _canonical_payload(payload, kind=kind)
        now = self._clock()
        job = Job(
            request_id=request_id or str(uuid.uuid4()),
            kind=kind,
            state="queued",
            actor=actor,
            authority_revision=authority_revision,
            targets=list(targets),
            payload_digest=hashlib.sha256(encoded).hexdigest(),
            payload=clean,
            current_attempt=0,
            created_at=now,
            updated_at=now,
            reconciliation_id=reconciliation_id,
        )
        try:
            with self._sessions.begin() as session:
                existing = session.scalar(
                    select(Job).where(Job.request_id == job.request_id)
                )
                if existing is not None:
                    if not self._same_request(existing, job):
                        raise ValueError("request key was already used differently")
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
                    raise ValueError(
                        "request key was already used differently"
                    ) from None
                session.expunge(existing)
                return existing

    def get(self, job_id: str) -> Job:
        with self._sessions() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            session.expunge(job)
            return job

    def list(self, *, limit: int = 100) -> list[Job]:
        page, _, _ = self.list_page(limit=limit)
        return page

    def list_page(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
        status: str | None = None,
        target: str | None = None,
    ) -> tuple[list[Job], str | None, int]:
        """Return a stable newest-first keyset page and authoritative total."""

        if not 1 <= limit <= 100:
            raise ValueError("job list limit is invalid")
        if status is not None and not status.strip():
            raise ValueError("job status is invalid")
        if target is not None and not target.strip():
            raise ValueError("job target is invalid")
        normalized_status = None if status is None else status.strip()
        normalized_target = None if target is None else target.strip()
        context = {"status": normalized_status, "target": normalized_target}
        boundary: tuple[datetime, str] | None = None
        if cursor is not None:
            try:
                if self._cursors is None:
                    raise ValueError
                decoded = self._cursors.decode(
                    cursor,
                    resource="jobs",
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
                raise ValueError("job list cursor is invalid") from None
        with self._sessions() as session:
            filters = []
            if normalized_status is not None:
                filters.append(Job.state == normalized_status)
            if normalized_target is not None:
                filters.append(
                    cast(Job.targets, String).contains(f'"{normalized_target}"')
                )
            statement = select(Job).where(*filters)
            if boundary is not None:
                created_at, job_id = boundary
                statement = statement.where(
                    or_(
                        Job.created_at < created_at,
                        (Job.created_at == created_at) & (Job.id < job_id),
                    )
                )
            jobs = list(
                session.scalars(
                    statement.order_by(Job.created_at.desc(), Job.id.desc()).limit(
                        limit + 1
                    )
                )
            )
            total = int(
                session.scalar(select(func.count()).select_from(Job).where(*filters))
                or 0
            )
            has_more = len(jobs) > limit
            jobs = jobs[:limit]
            for job in jobs:
                session.expunge(job)
        next_cursor = None
        if has_more and jobs:
            if self._cursors is None:
                raise RuntimeError("job cursor signer is unavailable")
            last = jobs[-1]
            next_cursor = self._cursors.encode(
                resource="jobs",
                order="created-at-desc/id-desc/v1",
                context=context,
                boundary=[_aware(last.created_at).isoformat(), last.id],
            )
        return jobs, next_cursor, total

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
        reconciliation_id: str | None = None,
    ) -> Job:
        """Create a job only while its external acceptance evidence stays current."""

        if not callable(authority_check):
            raise TypeError("job enqueue authority check is invalid")
        if not all(value.strip() for value in (kind, actor, authority_revision)):
            raise ValueError("job kind, actor, and authority revision are required")
        if reconciliation_id is not None:
            try:
                reconciliation_id = str(uuid.UUID(reconciliation_id))
            except (AttributeError, TypeError, ValueError) as error:
                raise ValueError("reconciliation identity is invalid") from error
        clean, encoded = _canonical_payload(payload, kind=kind)
        now = self._clock()
        job = Job(
            request_id=request_id or str(uuid.uuid4()),
            kind=kind,
            state="queued",
            actor=actor,
            authority_revision=authority_revision,
            targets=list(targets),
            payload_digest=hashlib.sha256(encoded).hexdigest(),
            payload=clean,
            current_attempt=0,
            created_at=now,
            updated_at=now,
            reconciliation_id=reconciliation_id,
        )
        try:
            with self._sessions.begin() as session:
                existing = session.scalar(
                    select(Job).where(Job.request_id == job.request_id)
                )
                if existing is not None:
                    if not self._same_request(existing, job):
                        raise ValueError("request key was already used differently")
                    session.expunge(existing)
                    return existing
                if authority_check() is not True:
                    raise ValueError("fleet acceptance evidence is stale")
                session.add(job)
                session.flush()
                if authority_check() is not True:
                    raise ValueError("fleet acceptance evidence is stale")
            return job
        except IntegrityError:
            with self._sessions() as session:
                existing = session.scalar(
                    select(Job).where(Job.request_id == job.request_id)
                )
                if existing is None or not self._same_request(existing, job):
                    raise ValueError(
                        "request key was already used differently"
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

    def claim(self, worker_id: str, lease_seconds: int) -> AttemptFence | None:
        if not worker_id.strip() or lease_seconds <= 0:
            raise ValueError("worker and positive lease are required")
        now = self._clock()
        with self._claim_lock, self._sessions.begin() as session:
            statement = (
                select(Job)
                .where(
                    Job.reconciliation_id.is_(None),
                    Job.kind != "agent-upgrade",
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
            if job.current_attempt:
                old = session.scalar(
                    select(JobAttempt).where(
                        JobAttempt.job_id == job.id,
                        JobAttempt.attempt == job.current_attempt,
                    )
                )
                if old is not None:
                    old.state = "expired"
            job.current_attempt += 1
            job.state = "running"
            job.updated_at = now
            deadline = now + timedelta(seconds=lease_seconds)
            fence = str(uuid.uuid4())
            session.add(
                JobAttempt(
                    job_id=job.id,
                    attempt=job.current_attempt,
                    fence=fence,
                    worker_id=worker_id,
                    lease_deadline=deadline,
                    state="running",
                )
            )
            return AttemptFence(
                job.id,
                job.current_attempt,
                fence,
                worker_id,
                deadline,
                job.kind,
                dict(job.payload),
                job.authority_revision,
                tuple(job.targets),
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
        return job, attempt

    def heartbeat(self, fence: AttemptFence, lease_seconds: int) -> AttemptFence:
        if lease_seconds <= 0:
            raise ValueError("lease must be positive")
        with self._sessions.begin() as session:
            job, attempt = self._active(session, fence)
            deadline = self._clock() + timedelta(seconds=lease_seconds)
            attempt.lease_deadline = deadline
            job.updated_at = self._clock()
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
            attempt.state = state
            job.state = state
            job.result = dict(result) if result is not None else None
            job.status_reason = redact_text(reason)[:1024] if reason else None
            job.updated_at = self._clock()

    def succeed(self, fence: AttemptFence, result: Mapping[str, object]) -> None:
        clean, _ = _canonical_payload(result)
        self._finish(fence, "succeeded", clean, None)

    def fail(self, fence: AttemptFence, reason: str) -> None:
        self._finish(fence, "failed", None, reason)

    def wait_for_operator(self, fence: AttemptFence, reason: str) -> None:
        self._finish(fence, "waiting-for-operator", None, reason)

    def resume(self, job_id: str) -> None:
        with self._sessions.begin() as session:
            job = session.get(Job, job_id)
            if job is None or job.state != "waiting-for-operator":
                raise ValueError("job is not waiting for operator")
            now = self._clock()
            result = session.execute(
                update(Job)
                .where(Job.id == job_id, Job.state == "waiting-for-operator")
                .values(
                    state="queued",
                    status_reason=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise ValueError("job is not waiting for operator")
            job.state = "queued"
            job.status_reason = None
            job.updated_at = now
