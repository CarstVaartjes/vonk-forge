"""Operator resume/retire acceptance belongs to the existing Job row."""

from __future__ import annotations

from vonk_agent_protocol import InvalidRequestReason, canonical_message

from cluster_profiles.control_limits import MAX_CONTROL_DOCUMENT_BYTES

from .categorized_errors import InvalidValue
from .models import Job
from .operator_receipt_contract import (
    JobOperatorDecision,
    JobOperatorDecisions,
    JobOperatorReceipt,
)


def _decisions(job: Job) -> JobOperatorDecisions:
    if job.operator_decisions is None:
        return JobOperatorDecisions(decisions=[])
    return JobOperatorDecisions.model_validate_json(job.operator_decisions)


def read_job_operator_receipt(job: Job, request_id: str) -> JobOperatorReceipt | None:
    for decision in _decisions(job).decisions:
        if decision.request_id == request_id:
            return JobOperatorReceipt(
                job_id=job.id,
                content_digest=job.payload_digest,
                request_id=decision.request_id,
                actor=decision.actor,
                action=decision.action,
            )
    return None


def replay_job_operator_receipt(job: Job, receipt: JobOperatorReceipt | None) -> bool:
    if receipt is None:
        return False
    previous = read_job_operator_receipt(job, receipt.request_id)
    if previous is None:
        return False
    if previous != receipt:
        raise InvalidValue(
            "job operator request identity was used differently",
            reason=InvalidRequestReason.CONFLICT,
        )
    return True


def record_job_operator_receipt(job: Job, receipt: JobOperatorReceipt | None) -> None:
    if receipt is None or replay_job_operator_receipt(job, receipt):
        return
    if job.id != receipt.job_id or job.payload_digest != receipt.content_digest:
        raise InvalidValue(
            "job operator request content changed", reason=InvalidRequestReason.CONFLICT
        )
    history = _decisions(job)
    history.decisions.append(
        JobOperatorDecision(
            request_id=receipt.request_id, actor=receipt.actor, action=receipt.action
        )
    )
    document = canonical_message(history)
    observed = len(document)
    if observed > MAX_CONTROL_DOCUMENT_BYTES:
        raise InvalidValue(
            f"job operator decisions require {observed} bytes; reader budget is {MAX_CONTROL_DOCUMENT_BYTES} bytes",
            reason=InvalidRequestReason.LIMIT_EXCEEDED,
        )
    job.operator_decisions = document.decode("utf-8")
