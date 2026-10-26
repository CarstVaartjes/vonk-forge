"""Durable catch-all custody, using the canonical disposition and wait writers."""

from collections.abc import Callable
from datetime import datetime, timedelta

from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    Disposition,
    FailureCode,
    InvalidRequestReason,
    OperationReason,
    WaitOwnerKind,
    WaitReason,
    WaitResumeCondition,
    canonical_message,
)

from .agent_operation_facts import aware
from .coordinator_contract import CoordinatorFailureEvidence
from .failure_classification import disposition, error_code, error_detail
from .logging import redact_text
from .models import CoordinatorFailure
from .operation_blockers import BlockingOperation, make_blocker
from .persisted_waits import read_wait, write_wait

_RETRY_FIRST_SECONDS = 5
_RETRY_CAP_SECONDS = 300


class CoordinatorFailures:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        clock: Callable[[], datetime],
        source_content_sha256: str,
    ):
        self._sessions = sessions
        self._clock = clock
        self._source_content_sha256 = source_content_sha256

    def ready(self, source: str) -> bool:
        now = self._clock()
        with self._sessions.begin() as session:
            row = session.get(CoordinatorFailure, source)
            if row is None:
                return True
            failure = row.failure
            if not isinstance(failure, CoordinatorFailureEvidence):
                failure = CoordinatorFailureEvidence(
                    source_content_sha256=self._source_content_sha256,
                    disposition=Disposition.FAIL,
                    reason=InvalidRequestReason.MALFORMED,
                    detail="Stored coordinator failure evidence is unreadable; repair the coordinator before resuming.",
                )
                row.failure = failure
                self._write(row, failure, now)
                return False
            if failure.source_content_sha256 != self._source_content_sha256:
                # New executable content is new evidence: its backoff restarts.
                session.delete(row)
                return True
            # Once due, run the source again. A cleared fault heals the lane; a
            # persisting one fails again with a longer backoff, and a security
            # refusal is re-decided by the real check, never assumed.
            return row.next_action_at is None or aware(row.next_action_at) <= now

    def failed(self, source: str, error: Exception, verdict: Disposition) -> None:
        # The catch site supplies the one classifier's verdict; no prose classifier.
        assert verdict == disposition(error)
        failure = CoordinatorFailureEvidence(
            source_content_sha256=self._source_content_sha256,
            disposition=verdict,
            reason=_reason(error, verdict),
            detail=redact_text(error_detail(error)),
        )
        with self._sessions.begin() as session:
            row = session.get(CoordinatorFailure, source)
            if row is None:
                row = CoordinatorFailure(id=source, failure=failure)
                session.add(row)
            row.failure = failure
            self._write(row, failure, self._clock())

    def _write(
        self,
        row: CoordinatorFailure,
        failure: CoordinatorFailureEvidence,
        now: datetime,
    ) -> None:
        definite = failure.disposition in {Disposition.FAIL, Disposition.SECURITY}
        reason = failure.reason
        retry_in = float(_RETRY_FIRST_SECONDS)
        previous = read_wait(row)
        if definite and previous is not None and previous.reason == reason:
            # Deterministic faults back off by how long the same fault has
            # persisted (first_blocked_at survives while the cause is unchanged):
            # 5 s doubling to the cap. No second counter.
            persisted = (now - previous.first_blocked_at).total_seconds()
            retry_in = min(_RETRY_CAP_SECONDS, max(retry_in, persisted))
        write_wait(
            row,
            identity=BlockingOperation(id=row.id, kind=WaitOwnerKind.COORDINATOR.value),
            kind=WaitOwnerKind.COORDINATOR,
            reason=reason,
            now=now,
            next_check=now + timedelta(seconds=retry_in),
            blockers=[make_blocker(reason, failure.detail)],
            resume_condition=WaitResumeCondition.RETRY_DUE,
        )

    def succeeded(self, source: str) -> None:
        with self._sessions.begin() as session:
            row = session.get(CoordinatorFailure, source)
            if row is not None:
                session.delete(row)


def _reason(error: Exception, verdict: Disposition) -> OperationReason:
    try:
        return TypeAdapter(OperationReason).validate_json(
            canonical_message(error_code(error))
        )
    except ValidationError:
        return (
            FailureCode.OPERATION_FAILED
            if verdict in {Disposition.FAIL, Disposition.SECURITY}
            else WaitReason.OBSERVATION_UNAVAILABLE
        )
