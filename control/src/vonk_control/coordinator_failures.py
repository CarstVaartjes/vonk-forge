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
            if row.next_action_at is not None and aware(row.next_action_at) > now:
                return False
            if failure.disposition in {Disposition.FAIL, Disposition.SECURITY}:
                # Observe the resume condition, never execute the unchanged decision.
                self._write(row, failure, now)
                return False
            return True

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
        write_wait(
            row,
            identity=BlockingOperation(id=row.id, kind=WaitOwnerKind.COORDINATOR.value),
            kind=WaitOwnerKind.COORDINATOR,
            reason=reason,
            now=now,
            next_check=now + timedelta(seconds=300 if definite else 5),
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
