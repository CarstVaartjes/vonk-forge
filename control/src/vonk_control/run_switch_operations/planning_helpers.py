"""Planning helpers."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import (
    Literal,
    TypeGuard,
)

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    RunSwitchCode,
    canonical_message,
    run_switch_code,
)

from ..categorized_errors import (
    InvalidValue,
)
from ..job_documents import (
    RecipeStartParent,
    RunSwitchJobPayload,
)
from ..memory_reservations import (
    MEMORY_RESERVATION_KINDS,
    reviewed_run_memory_reservations,
)
from ..models import (
    Job,
    NodeInventorySnapshot,
)
from ..resource_planning import (
    ResourceReason,
    memory_reservation_kinds,
    resolve_effective_settings,
)
from ..run_switch_contract import (
    ConditionalPostStopMemoryCheck,
    EffectiveParallelism,
    EffectiveSettingsSelection,
    FreshnessEvidence,
    RunSwitchOperationResult,
    RunSwitchPlan,
    RunSwitchReason,
    RunSwitchReasonScope,
    RunSwitchReasonSeverity,
    SparkFit,
    StopImpact,
)
from ..stored_json import read_row_column
from .constants import (
    _CHANGE_EFFECTS_ADAPTER,
    _KNOBS_ADAPTER,
    _MEMORY_STOP_CONDITIONAL_REFUSALS,
)
from .interfaces import ArtifactInspection


def _refused_retries(progress: RunSwitchOperationResult) -> int:
    """How many times in a row admission was refused for capacity (0 if not)."""

    reason = progress.retry_reason
    attempt = progress.retry_attempt
    if (
        isinstance(reason, str)
        and reason.endswith(".capacity_busy")
        and type(attempt) is int
    ):
        return attempt
    return 0


def _now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise InvalidValue("run/switch clock must be timezone-aware")
    return value.astimezone(UTC)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_message(value)).hexdigest()


_PLAN_VOLATILE_KEYS = frozenset(
    {
        "generated_at",
        "invocation",
        "plan_digest",
        "age_seconds",
        "verified_at",
        # Node-keyed breakdowns of byte totals that are already bound.
        "missing_spark_bytes_by_node",
        "missing_image_distribution_bytes_by_node",
    }
)


def _plan_identity(value: object) -> object:
    """Remove presentation and wall-clock fields before digesting a plan.

    The plan still binds observed freshness state, timestamps, and evidence
    digests.  Relative age and locally generated verification timestamps are
    omitted so a preview can be applied a moment later without changing its
    authority merely because the clock advanced.
    """

    if isinstance(value, Mapping):
        return {
            str(key): _plan_identity(item)
            for key, item in value.items()
            if str(key) not in _PLAN_VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [_plan_identity(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_plan_identity(item) for item in value)
    return value


def _as_reason(
    code: str,
    detail: str,
    *,
    scope: RunSwitchReasonScope,
    severity: RunSwitchReasonSeverity = "blocker",
    node_ids: Sequence[str] = (),
    stale: bool = False,
) -> RunSwitchReason:
    return RunSwitchReason(
        code=code,
        detail=detail[:512],
        severity=severity,
        scope=scope,
        node_ids=list(node_ids),
        stale=stale,
    )


def _run_switch_payload(job: Job) -> RunSwitchJobPayload | None:
    payload = read_row_column(job, "payload")
    return payload if isinstance(payload, RunSwitchJobPayload) else None


def _stored_job_plan(job: Job) -> RunSwitchPlan | None:
    payload = _run_switch_payload(job)
    return payload.plan if payload is not None else None


def _start_parent(job: Job | None) -> RecipeStartParent | None:
    payload = read_row_column(job, "payload") if job is not None else None
    return payload if isinstance(payload, RecipeStartParent) else None


def _required_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _resource_reason(
    reason: ResourceReason, *, node_ids: Sequence[str] = ()
) -> RunSwitchReason:
    node_id = reason.node_id
    return _as_reason(
        run_switch_code(reason.code),
        reason.detail,
        scope="node" if isinstance(node_id, str) else "operation",
        severity=reason.severity,
        node_ids=(node_id,) if isinstance(node_id, str) else node_ids,
    )


def _is_memory_reservation_kind(
    value: str,
) -> TypeGuard[Literal["host-memory", "gpu-memory", "unified-memory"]]:
    return value in MEMORY_RESERVATION_KINDS


def _conditional_post_stop_memory_check(
    session: Session,
    stops: Sequence[StopImpact],
    fit: SparkFit,
    blockers: Sequence[RunSwitchReason],
    insufficient_components: Mapping[str, frozenset[str]],
) -> ConditionalPostStopMemoryCheck | None:
    """Permit only a known-capacity memory shortfall covered by exact stop claims."""

    if (
        not stops
        or not blockers
        or any(
            reason.code not in _MEMORY_STOP_CONDITIONAL_REFUSALS for reason in blockers
        )
    ):
        return None
    blocked_nodes = {node_id for reason in blockers for node_id in reason.node_ids}
    nodes = {node.node_id: node for node in fit.nodes}
    if not blocked_nodes or not blocked_nodes <= nodes.keys():
        return None
    for node in fit.nodes:
        if (
            node.memory_required_bytes is None
            or node.memory_kind is None
            or node.memory_pool is None
            or node.memory_floor_bytes is None
            or node.memory_capacity_bytes is None
            or node.memory_available_bytes is None
            or node.resource_demand is None
            or node.memory_capacity_bytes
            < node.memory_required_bytes + node.memory_floor_bytes
        ):
            return None
    for node_id in blocked_nodes:
        node = nodes[node_id]
        components = insufficient_components.get(node_id, frozenset())
        if not components or node.memory_pool is None:
            return None
        for component in components:
            reservation_kind = {
                "host": "host-memory",
                "accelerator": "gpu-memory",
                "shared": "unified-memory",
            }.get(component)
            if reservation_kind is None:
                return None
            claim_kinds = memory_reservation_kinds(reservation_kind, node.memory_pool)
            if not any(
                node_id in stop.node_ids
                and any(
                    claim.kind in claim_kinds
                    for claim in reviewed_run_memory_reservations(
                        session, node_id, stop
                    )
                )
                for stop in stops
            ):
                return None
    return ConditionalPostStopMemoryCheck(
        stop_run_ids=sorted(stop.run_id for stop in stops)
    )


def _settings_view(settings: object) -> EffectiveSettingsSelection:
    resolution = resolve_effective_settings(settings)
    if resolution.settings is None:
        raise InvalidValue("effective settings are invalid")
    resolved = resolution.settings
    return EffectiveSettingsSelection(
        kind=resolved.kind,
        context_tokens=resolved.context_tokens,
        concurrency=resolved.concurrency,
        max_batch_tokens=resolved.batch_tokens,
        parallelism=EffectiveParallelism(
            world_size=resolved.parallelism.world_size,
            tensor=resolved.parallelism.tensor,
            pipeline=resolved.parallelism.pipeline,
            data=resolved.parallelism.data,
            backend=resolved.parallelism.backend,
        ),
        knobs=_KNOBS_ADAPTER.validate_python(dict(resolved.knobs), strict=True),
        change_effects=_CHANGE_EFFECTS_ADAPTER.validate_python(
            dict(resolved.change_effects), strict=True
        ),
        identity_sha256=resolved.identity_digest,
    )


def _resource_evidence_digest(revision_digest: str | None) -> str | None:
    return (
        revision_digest
        if isinstance(revision_digest, str) and len(revision_digest) == 64
        else None
    )


def _latest_inventory(
    session: Session,
    node_id: str,
    *,
    now: datetime,
    maximum_age_seconds: int,
) -> tuple[NodeInventorySnapshot | None, FreshnessEvidence]:
    snapshot = session.scalar(
        select(NodeInventorySnapshot)
        .where(NodeInventorySnapshot.node_id == node_id)
        .order_by(NodeInventorySnapshot.observed_at.desc())
        .limit(1)
    )
    if snapshot is None:
        return None, FreshnessEvidence(
            source=f"spark:{node_id}:inventory",
            state="unknown",
            maximum_age_seconds=maximum_age_seconds,
        )
    observed = snapshot.observed_at
    if observed.tzinfo is None or observed.utcoffset() is None:
        observed = observed.replace(tzinfo=UTC)
    age = max(0.0, (now - observed.astimezone(UTC)).total_seconds())
    fresh = age <= maximum_age_seconds
    return snapshot, FreshnessEvidence(
        source=f"spark:{node_id}:inventory",
        state="fresh" if fresh else "stale",
        observed_at=observed.astimezone(UTC),
        age_seconds=age,
        maximum_age_seconds=maximum_age_seconds,
        evidence_digest=(
            snapshot.evidence_digest
            if isinstance(snapshot.evidence_digest, str)
            and len(snapshot.evidence_digest) == 64
            else None
        ),
    )


def _inspection_unavailable(detail: str) -> ArtifactInspection:
    """What a coverage inspection that could not be made says: coverage unknown,
    with a blocker the plan observes again (never a refusal of the request)."""

    return ArtifactInspection(
        required_bytes=None,
        reused_bytes=0,
        copied_bytes=0,
        missing_nas_bytes=None,
        missing_spark_bytes=None,
        reclaimable_bytes=0,
        nas_coverage="unknown",
        spark_coverage="unknown",
        artifact_digests=(),
        reclaimable_digests=(),
        blockers=(
            _as_reason(
                RunSwitchCode.ARTIFACT_INSPECTION_UNAVAILABLE,
                f"Artifact coverage could not be inspected: {detail}",
                scope="artifact",
            ),
        ),
    )
