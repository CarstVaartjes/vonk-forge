"""Durable, recipe-bound admission probes without replaying expensive phases."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Annotated

from pydantic import (
    AwareDatetime,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)
from sqlalchemy import select
from vonk_agent_protocol.runtime_preflight import RuntimePreflightResult

from .models import AgentNode, AgentOperation, AgentOperationAttempt, Job
from .recovery_policy import RecoveryPolicy
from .runtime_preflight import (
    admission_blockers,
    latest_result,
    node_fingerprint,
    recipe_requirements,
    request_digest,
)
from .strict_json import StrictJSONModel

NodeId = Annotated[str, StringConstraints(pattern=r"^spk_[0-9a-f]{32}$")]
# The agent bounds probe execution. After its normal delivery/reporting window,
# observe less frequently while that same fenced operation owns recovery.
PENDING_PROBE_NORMAL_WINDOW = timedelta(seconds=180)
PROBE_OBSERVATION_INTERVAL = timedelta(seconds=5)
DELAYED_PROBE_OBSERVATION_INTERVAL = timedelta(seconds=60)
UuidId = Annotated[
    str,
    StringConstraints(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
]


class LifecyclePreflightCheckpoint(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    phase_index: int = Field(ge=0, le=31)
    pending_job_id: UuidId | None = None
    pending_node_id: NodeId | None = None
    next_check_at: AwareDatetime | None = None
    last_failure_code: str | None = None
    last_failure_detail: str | None = None
    attempts: dict[NodeId, Annotated[int, Field(ge=1)]] = Field(
        default_factory=dict, max_length=33
    )
    receipts: dict[NodeId, RuntimePreflightResult] = Field(
        default_factory=dict, max_length=33
    )

    @model_validator(mode="after")
    def pending_identity_is_complete(self):
        if (self.pending_job_id is None) != (self.pending_node_id is None):
            raise ValueError(
                "preflight pending child and node identities must be paired"
            )
        return self


# Security refusals that park the preflight instead of retrying it. Each code is
# one a producer actually emits: the agent's `HostRuntimeError::preflight_code()`
# (`helper_<code>` for codes on `stable_runtime_error_code`), the helper's own
# un-prefixed wire rejection codes (`vonk-agent-helper` `HelperRejection`,
# carried verbatim by image-import failures), the agent's `runtime_helper_<cause>`
# receipt causes, and the controller/agent identity, enrollment and certificate
# codes. Everything else is transient and retried with backoff.
_SECURITY_FAILURE_CODES = frozenset(
    {
        # Helper grant, peer identity, replay and integrity refusals.
        "helper_grant_invalid",
        "helper_grant_unauthorized",
        "helper_grant_node_mismatch",
        "helper_peer_identity_invalid",
        "helper_request_replayed",
        "helper_request_installation_identity_invalid",
        "helper_request_plan_binding_invalid",
        "helper_inspection_receipt_invalid",
        "helper_observation_receipt_invalid",
        "helper_operation_invalid_artifact",
        "helper_runtime_image_identity_invalid",
        "grant_invalid",
        "grant_unauthorized",
        "grant_node_mismatch",
        "peer_identity_invalid",
        "request_replayed",
        "operation_invalid_artifact",
        "runtime_image_identity_invalid",
        "runtime_helper_inspection_receipt_invalid",
        "runtime_helper_observation_receipt_invalid",
        # Controller authentication, enrollment, identity and certificate codes.
        "controller.authentication_required",
        "controller.request_rejected",
        "controller.fleet.enrollment_denied",
        "agent.enrollment.submit.rejected",
        "agent.certificate.rotation.conflict",
        "local.identity_expired",
    }
)


def _is_security_failure(code: str | None) -> bool:
    """Return whether a child's typed error code is a real security refusal."""
    return code in _SECURITY_FAILURE_CODES


def _child_error_code(result: object) -> str | None:
    """Read the typed failure code from a child attempt result, never free text."""
    if not isinstance(result, Mapping):
        return None
    for key in ("error_code", "helper_error_code"):
        value = result.get(key)
        if isinstance(value, str) and _is_security_failure(value):
            return value
    value = result.get("error_code")
    return value if isinstance(value, str) else None


class LifecyclePreflight:
    def __init__(self, sessions, queue, clock, minimum_free_bytes: int) -> None:
        self._sessions = sessions
        self._queue = queue
        self._clock = clock
        self._minimum_free_bytes = minimum_free_bytes
        self._recovery = RecoveryPolicy()

    def ensure(
        self,
        *,
        document: Mapping[str, object],
        nodes: Mapping[str, bool],
        phase_index: int,
        request_key: str,
        actor: str,
        previous: LifecyclePreflightCheckpoint | None,
    ) -> tuple[LifecyclePreflightCheckpoint, str | None]:
        """Return one bounded checkpoint and an optional blocking reason.

        Successful probe execution means observations exist; each mandatory
        finding must also pass for the current request, fingerprint and age.
        """
        now: datetime = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        checkpoint = (
            previous.model_copy(deep=True)
            if previous
            else LifecyclePreflightCheckpoint(phase_index=phase_index)
        )
        if checkpoint.phase_index != phase_index:
            checkpoint = LifecyclePreflightCheckpoint(
                phase_index=phase_index, receipts=checkpoint.receipts
            )

        def retry_probe(
            node_id: str, reason: str, error_code: str | None = None
        ) -> tuple[LifecyclePreflightCheckpoint, str | None]:
            checkpoint.pending_job_id = None
            checkpoint.pending_node_id = None
            checkpoint.receipts.pop(node_id, None)
            checkpoint.next_check_at = None
            if _is_security_failure(error_code):
                return checkpoint, reason
            checkpoint.next_check_at = self._recovery.next_attempt(
                f"{request_key}:{phase_index}:{node_id}",
                checkpoint.attempts.get(node_id, 1),
                now,
                ongoing_intent=True,
            )
            return checkpoint, None

        if checkpoint.pending_job_id is None:
            if checkpoint.next_check_at is not None and now < checkpoint.next_check_at:
                return checkpoint, None
            checkpoint.next_check_at = None
        with self._sessions.begin() as session:
            # Consume the outstanding probe first, then recheck every other node.
            # A previously checked rank can change while its peer is probing.
            ordered_nodes = sorted(
                nodes.items(),
                key=lambda item: (item[0] != checkpoint.pending_node_id, item[0]),
            )
            for node_id, source_build in ordered_nodes:
                node = session.get(AgentNode, node_id, with_for_update=True)
                if node is None:
                    checkpoint.pending_job_id = None
                    checkpoint.pending_node_id = None
                    checkpoint.receipts.pop(node_id, None)
                    checkpoint.next_check_at = None
                    return (
                        checkpoint,
                        (
                            "runtime_preflight.node_missing: node was removed from "
                            "Controller authority"
                        ),
                    )
                if node.revoked_at is not None:
                    return checkpoint, "runtime_preflight.node_revoked"
                request = recipe_requirements(
                    document,
                    source_build=source_build,
                    minimum_free_bytes=self._minimum_free_bytes,
                )
                fingerprint = node_fingerprint(node.capabilities)
                result = checkpoint.receipts.get(node_id) or latest_result(
                    session, node_id, requirements_sha256=request_digest(request)
                )
                blockers = admission_blockers(
                    request,
                    result,
                    current_fingerprint=fingerprint,
                    now=int(now.timestamp()),
                )
                # admission_blockers always reports a missing result, so the
                # explicit check only states that a clean result is the one
                # being receipted.
                if (
                    result is not None
                    and not blockers
                    and checkpoint.pending_node_id != node_id
                ):
                    checkpoint.receipts[node_id] = result
                    continue
                if checkpoint.pending_job_id:
                    if checkpoint.pending_node_id != node_id:
                        continue
                    child = session.get(Job, checkpoint.pending_job_id)
                    if child is None:
                        return retry_probe(node_id, "runtime_preflight.child_missing")
                    operation = session.scalar(
                        select(AgentOperation).where(
                            AgentOperation.parent_job_id == child.id,
                            AgentOperation.node_id == node_id,
                        )
                    )
                    if operation is None:
                        return retry_probe(
                            node_id, "runtime_preflight.operation_missing"
                        )
                    if child.state in {"queued", "running"}:
                        created_at = child.created_at
                        if created_at.tzinfo is None:
                            created_at = created_at.replace(tzinfo=UTC)
                        if (
                            checkpoint.next_check_at is None
                            or now >= checkpoint.next_check_at
                        ):
                            interval = (
                                DELAYED_PROBE_OBSERVATION_INTERVAL
                                if now >= created_at + PENDING_PROBE_NORMAL_WINDOW
                                else PROBE_OBSERVATION_INTERVAL
                            )
                            checkpoint.next_check_at = now + interval
                        return checkpoint, None
                    raw = session.scalar(
                        select(AgentOperationAttempt.result).where(
                            AgentOperationAttempt.operation_id == operation.id,
                            AgentOperationAttempt.attempt == operation.current_attempt,
                        )
                    )
                    if child.state != "succeeded":
                        # Classify on the typed code only; status_reason is prose.
                        return retry_probe(
                            node_id,
                            child.status_reason or "runtime_preflight.execution_failed",
                            _child_error_code(raw),
                        )
                    if raw is None:
                        return retry_probe(node_id, "runtime_preflight.receipt_missing")
                    try:
                        result = RuntimePreflightResult.model_validate(raw)
                    except (TypeError, ValueError):
                        return retry_probe(node_id, "runtime_preflight.receipt_invalid")
                    checkpoint.receipts[node_id] = result
                    checkpoint.pending_job_id = None
                    checkpoint.pending_node_id = None
                    checkpoint.next_check_at = None
                    blockers = admission_blockers(
                        request,
                        result,
                        current_fingerprint=fingerprint,
                        now=int(now.timestamp()),
                    )
                    if not blockers:
                        checkpoint.last_failure_code = None
                        checkpoint.last_failure_detail = None
                        continue
                    if any(
                        item.code
                        not in {
                            "runtime_preflight.host_changed",
                            "runtime_preflight.requirements_changed",
                            "runtime_preflight.stale",
                        }
                        for item in blockers
                    ):
                        return checkpoint, "; ".join(item.detail for item in blockers)[
                            :512
                        ]
                    stale_cause = "; ".join(
                        f"{item.code}: {item.detail}" for item in blockers
                    )
                    checkpoint.next_check_at = self._recovery.next_attempt(
                        f"{request_key}:{phase_index}:{node_id}",
                        checkpoint.attempts[node_id],
                        now,
                        ongoing_intent=True,
                    )
                    checkpoint.last_failure_code = next(
                        (
                            item.code
                            for item in blockers
                            if item.code
                            in {
                                "runtime_preflight.host_changed",
                                "runtime_preflight.requirements_changed",
                                "runtime_preflight.stale",
                            }
                        ),
                        "runtime_preflight.stale",
                    )
                    checkpoint.last_failure_detail = stale_cause[:512]
                    return checkpoint, None
                attempt = checkpoint.attempts.get(node_id, 0) + 1
                checkpoint.attempts[node_id] = attempt
                key = str(
                    uuid.uuid5(
                        uuid.UUID(request_key),
                        f"runtime-preflight:{phase_index}:{node_id}:{attempt}",
                    )
                )
                child = session.scalar(select(Job).where(Job.request_id == key))
                if child is None:
                    digest = request_digest(request)
                    payload = request.model_dump(mode="json")
                    child = Job(
                        id=str(uuid.uuid4()),
                        request_id=key,
                        kind="runtime.preflight.v1",
                        state="running",
                        actor=actor,
                        authority_revision=digest,
                        targets=[node_id],
                        payload_digest=digest,
                        payload=payload,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(child)
                    session.flush()
                    self._queue.enqueue_in_session(
                        session,
                        child.id,
                        node_id,
                        "runtime.preflight.v1",
                        digest,
                        payload,
                        operation_id=str(uuid.uuid4()),
                    )
                checkpoint.pending_job_id = child.id
                checkpoint.pending_node_id = node_id
                checkpoint.next_check_at = now + PROBE_OBSERVATION_INTERVAL
                break
        if checkpoint.pending_job_id:
            self._queue.notify_available()
        return checkpoint, None
