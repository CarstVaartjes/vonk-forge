"""Worker-owned execution boundary for generic workload validation."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from sqlalchemy import select
from vonk_agent_protocol import (
    AgentOperation,
    PackageOperationRequest,
    canonical_message,
)

from .models import AgentOperation as StoredAgentOperation
from .models import AgentOperationAttempt, Job, PackageValidationRun

_VALIDATION_OPERATIONS = (
    AgentOperation.PACKAGE_PREPARE.value,
    AgentOperation.PACKAGE_HEALTH.value,
)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_message(value)).hexdigest()


class PackageValidationRunner:
    """Stage validation operations without executing package code locally.

    The API process may call the object to persist a parent validation job and
    exact agent operations.  The worker calls :meth:`tick` to project terminal
    agent evidence into the durable validation run.
    """

    def __init__(
        self,
        sessions,
        agent_jobs,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(getattr(agent_jobs, "enqueue_in_session", None)):
            raise TypeError("validation agent job service is invalid")
        self._sessions = sessions
        self._agent_jobs = agent_jobs
        self._clock = clock or (lambda: datetime.now(UTC))

    def __call__(self, request: Mapping[str, object]) -> Mapping[str, object]:
        if not isinstance(request, Mapping):
            raise TypeError("validation request is invalid")
        validation_id = request.get("validation_id")
        candidate_id = request.get("candidate_id")
        base_commit = request.get("base_commit")
        release_digest = request.get("release_digest")
        node_ids = request.get("node_ids")
        operations = request.get("operations")
        required_evidence = request.get("required_evidence", [])
        if (
            not isinstance(validation_id, str)
            or not isinstance(candidate_id, str)
            or not isinstance(base_commit, str)
            or not isinstance(release_digest, str)
            or len(release_digest) != 64
            or any(character not in "0123456789abcdef" for character in release_digest)
            or not isinstance(node_ids, list)
            or not node_ids
            or not all(isinstance(node_id, str) for node_id in node_ids)
            or not isinstance(operations, list)
            or len(operations) != len(_VALIDATION_OPERATIONS)
            or not isinstance(required_evidence, list)
            or len(required_evidence) > 64
            or not all(isinstance(item, str) and item for item in required_evidence)
            or any(len(item) > 128 for item in required_evidence)
        ):
            raise ValueError("validation request identity is invalid")
        try:
            uuid.UUID(validation_id)
        except ValueError as error:
            raise ValueError("validation ID is invalid") from error
        now = self._clock()
        with self._sessions.begin() as session:
            run = session.get(PackageValidationRun, validation_id)
            if (
                run is None
                or run.candidate_id != candidate_id
                or run.release_digest != release_digest
            ):
                raise ValueError("validation release identity is unavailable")
            progress = dict(run.progress or {})
            existing_job_id = progress.get("job_id")
            if isinstance(existing_job_id, str):
                return {"status": "running", "evidence": {}}
            parsed_operations: list[tuple[str, Mapping[str, object]]] = []
            for expected, raw in zip(_VALIDATION_OPERATIONS, operations, strict=True):
                if not isinstance(raw, Mapping) or raw.get("kind") != expected:
                    raise ValueError("validation operation order is invalid")
                payload = raw.get("payload")
                if not isinstance(payload, Mapping):
                    raise TypeError("validation operation payload is invalid")
                agent_payload = {
                    key: payload[key]
                    for key in (
                        "schema_version",
                        "deployment_id",
                        "release_digest",
                        "deployment_digest",
                        "deployment",
                        "deployment_config_digest",
                    )
                    if key in payload
                }
                PackageOperationRequest.parse(AgentOperation(expected), agent_payload)
                if agent_payload.get("release_digest") != release_digest:
                    raise ValueError("validation operation release identity is invalid")
                deployment = agent_payload.get("deployment")
                if (
                    not isinstance(deployment, Mapping)
                    or deployment.get("release_digest") != release_digest
                ):
                    raise ValueError("validation deployment release identity is invalid")
                parsed_operations.append((expected, agent_payload))
            job_id = str(uuid.uuid4())
            parent_payload = {
                "schema_version": 1,
                "validation_id": validation_id,
                "candidate_id": candidate_id,
                "release_digest": release_digest,
                "required_evidence": list(dict.fromkeys(required_evidence)),
            }
            session.add(
                Job(
                    id=job_id,
                    request_id=validation_id,
                    kind="package.validation",
                    state="running",
                    actor=run.actor,
                    base_commit=base_commit,
                    targets=list(node_ids),
                    payload_digest=_digest(parent_payload),
                    payload=parent_payload,
                    created_at=now,
                    updated_at=now,
                )
            )
            operation_ids: list[str] = []
            for node_id in node_ids:
                for kind, payload in parsed_operations:
                    operation_id = str(uuid.uuid4())
                    stored = self._agent_jobs.enqueue_in_session(
                        session,
                        job_id,
                        node_id,
                        kind,
                        base_commit,
                        payload,
                        operation_id=operation_id,
                    )
                    operation_ids.append(stored.id)
            run.progress = {
                "completed": 0,
                "failed": 0,
                "running": len(operation_ids),
                "total": len(operation_ids),
                "job_id": job_id,
                "operation_ids": operation_ids,
                "operation_kinds": [kind for kind, _payload in parsed_operations],
                "required_evidence": list(dict.fromkeys(required_evidence)),
            }
            run.updated_at = now
        self._agent_jobs.notify_available()
        return {"status": "running", "evidence": {}}

    def tick(self) -> bool:
        """Project terminal agent results into validation runs."""

        with self._sessions.begin() as session:
            runs = tuple(
                session.scalars(
                    select(PackageValidationRun)
                    .where(PackageValidationRun.state == "running")
                    .order_by(PackageValidationRun.updated_at, PackageValidationRun.id)
                )
            )
            for run in runs:
                progress = run.progress if isinstance(run.progress, Mapping) else {}
                operation_ids = progress.get("operation_ids")
                if not isinstance(operation_ids, list) or not operation_ids:
                    continue
                operations = [session.get(StoredAgentOperation, item) for item in operation_ids]
                if any(operation is None for operation in operations):
                    continue
                concrete = [operation for operation in operations if operation is not None]
                if any(operation.state in {"queued", "running"} for operation in concrete):
                    continue
                failed = next(
                    (operation for operation in concrete if operation.state != "succeeded"),
                    None,
                )
                if failed is not None:
                    run.state = "retryable" if failed.retry_disposition == "retry" else "failed"
                    run.reason_code = "agent-validation-failed"
                    run.progress = {
                        **dict(progress),
                        "completed": 0,
                        "failed": 1,
                        "running": 0,
                    }
                    run.updated_at = self._clock()
                    return True
                evidence: dict[str, object] = {}
                for operation in concrete:
                    attempt = session.scalar(
                        select(AgentOperationAttempt)
                        .where(AgentOperationAttempt.operation_id == operation.id)
                        .order_by(AgentOperationAttempt.attempt.desc())
                    )
                    if attempt is None or not isinstance(attempt.result, Mapping):
                        continue
                    result = dict(attempt.result)
                    nested = result.get("evidence")
                    if isinstance(nested, Mapping):
                        evidence.update(nested)
                    else:
                        key = operation.kind.removeprefix("package.")
                        evidence[key] = result
                try:
                    evidence_size = len(canonical_message(evidence))
                except (TypeError, ValueError):
                    evidence_size = 16_385
                if evidence_size > 16_384:
                    run.state = "failed"
                    run.reason_code = "validation-evidence-too-large"
                    run.failure_detail = {"max_bytes": 16_384}
                    run.progress = {
                        **dict(progress),
                        "completed": 0,
                        "failed": 1,
                        "running": 0,
                    }
                    run.updated_at = self._clock()
                    return True
                required = progress.get("required_evidence", [])
                if isinstance(required, list):
                    missing = [item for item in required if item not in evidence]
                else:
                    missing = []
                if missing:
                    run.state = "failed"
                    run.reason_code = "validation-evidence-missing"
                    run.failure_detail = {"missing": missing[:32]}
                    run.progress = {
                        **dict(progress),
                        "completed": 0,
                        "failed": 1,
                        "running": 0,
                    }
                    run.updated_at = self._clock()
                    return True
                run.state = "passed"
                run.reason_code = None
                run.evidence = evidence
                run.progress = {
                    **dict(progress),
                    "completed": len(concrete),
                    "failed": 0,
                    "running": 0,
                }
                run.completed_at = self._clock()
                run.updated_at = self._clock()
                return True
        return False


__all__ = ["PackageValidationRunner"]
