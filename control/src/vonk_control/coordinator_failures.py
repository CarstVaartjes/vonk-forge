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
from .persisted_waits import write_wait

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
                return True
            # Once due, run the source again. A cleared fault heals the lane; a
            # persisting one fails again with a longer backoff, and a security
            # refusal is re-decided by the real check, never assumed.
            return row.next_action_at is None or aware(row.next_action_at) <= now

    def failed(self, source: str, error: Exception, verdict: Disposition) -> None:
        # The catch site supplies the one classifier's verdict; no prose classifier.
        assert verdict == disposition(error)
        with self._sessions.begin() as session:
            row = session.get(CoordinatorFailure, source)
            previous = row.failure if row is not None else None
            attempts = (
                previous.attempts + 1
                if isinstance(previous, CoordinatorFailureEvidence)
                and previous.source_content_sha256 == self._source_content_sha256
                else 1
            )
            failure = CoordinatorFailureEvidence(
                source_content_sha256=self._source_content_sha256,
                disposition=verdict,
                reason=_reason(error, verdict),
                detail=redact_text(error_detail(error)),
                attempts=attempts,
            )
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
        write_wait(
            row,
            identity=BlockingOperation(id=row.id, kind=WaitOwnerKind.COORDINATOR.value),
            kind=WaitOwnerKind.COORDINATOR,
            reason=reason,
            now=now,
            next_check=now + timedelta(seconds=_backoff_seconds(failure, definite)),
            blockers=[make_blocker(reason, failure.detail)],
            resume_condition=WaitResumeCondition.DEPENDENCY_CHANGED
            if definite
            else WaitResumeCondition.RETRY_DUE,
        )

    def succeeded(self, source: str) -> None:
        with self._sessions.begin() as session:
            row = session.get(CoordinatorFailure, source)
            if row is not None:
                session.delete(row)


def _backoff_seconds(failure: CoordinatorFailureEvidence, definite: bool) -> int:
    if not definite:
        return _RETRY_FIRST_SECONDS
    return min(
        _RETRY_CAP_SECONDS, _RETRY_FIRST_SECONDS * 2 ** min(failure.attempts - 1, 6)
    )


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
