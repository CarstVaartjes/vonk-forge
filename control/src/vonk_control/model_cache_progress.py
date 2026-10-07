"""Current cache counters with one canonical, durable progress measurement."""

import json
from datetime import datetime
from typing import Any

from vonk_agent_protocol import (
    OperationCheckpoint,
    OperationMemberProgress,
    OperationProgress,
    ProgressPhase,
    canonical_message,
)

from .model_cache_contract import (
    ModelCacheCounters,
    ModelCacheOperationPhase,
    ModelCacheOperationProgress,
)
from .operation_progress import STALE_AFTER_SECONDS, project_progress, sample_progress
from .strict_json import read_stored_model

#: The measured phase of each operation phase.  Both are words of the contract's
#: ``ProgressPhase``; the measurement used to say ``download``, ``verify`` and
#: ``cleanup``, which are read through ``adopt_progress_phase``.
PHASES: dict[str, ProgressPhase] = {
    "queued": ProgressPhase.QUEUED,
    "downloading": ProgressPhase.DOWNLOADING,
    "verifying": ProgressPhase.VERIFYING,
    "reclaiming": ProgressPhase.RECLAIMING,
    "cancelling": ProgressPhase.WAITING,
    "completed": ProgressPhase.COMPLETED,
    "failed": ProgressPhase.FAILED,
}


def progress_document(progress: ModelCacheOperationProgress) -> Any:
    """The JSON document a progress column stores for ``progress``."""

    return json.loads(canonical_message(progress))


def _sample(
    previous: OperationProgress | None, current: OperationProgress, now: datetime
) -> OperationProgress:
    if previous is not None and previous.observed_at:
        interval = (now - datetime.fromisoformat(previous.observed_at)).total_seconds()
        if interval >= STALE_AFTER_SECONDS or interval < 0:
            # A disconnected worker is not a throughput sample. Preserve elapsed
            # work already measured, but begin a fresh adjacent receipt window.
            previous = previous.model_copy(
                update={
                    "observed_at": now.isoformat(),
                    "smoothed_bytes_per_second": None,
                }
            )
    return sample_progress(previous, current, now)


def _member_as_progress(member: OperationMemberProgress) -> OperationProgress:
    """A member's own counters as a progress sample (its identity left out)."""

    return read_stored_model(
        OperationProgress,
        {
            **member.model_dump(
                mode="json", exclude={"member_id", "state"}, exclude_none=True
            ),
            "total_bytes_known": member.total_bytes is not None,
        },
    )


def _member_progress(
    measurement: OperationProgress, identity: OperationMemberProgress
) -> OperationMemberProgress:
    """Project a validated operation sample into its canonical member fields."""
    shared_fields = (
        OperationProgress.model_fields.keys()
        & OperationMemberProgress.model_fields.keys()
    )
    return read_stored_model(
        OperationMemberProgress,
        {
            **measurement.model_dump(mode="json", include=shared_fields),
            "member_id": identity.member_id,
            "state": identity.state,
        },
    )


def cache_progress(
    counters: ModelCacheCounters,
    *,
    previous: ModelCacheOperationProgress | None,
    now: datetime,
    members: list[OperationMemberProgress] | None = None,
) -> ModelCacheOperationProgress:
    total = counters.expected_bytes
    measurement = OperationProgress(
        phase=PHASES[counters.phase],
        completed_bytes=counters.downloaded_bytes,
        total_bytes=total,
        total_bytes_known=total is not None,
        completed_items=counters.completed_artifacts,
        total_items=counters.total_artifacts,
        checkpoint=OperationCheckpoint(
            key="artifact-set",
            sequence=counters.completed_artifacts,
            cursor=counters.current_artifact_key,
        ),
    )
    sampled = _sample(previous.measurement if previous else None, measurement, now)
    prior_members = (
        {member.member_id: member for member in previous.measurement.members}
        if previous
        else {}
    )
    sampled_members = []
    for member in members or []:
        prior = prior_members.get(member.member_id)
        sample = _sample(
            _member_as_progress(prior) if prior is not None else None,
            _member_as_progress(member),
            now,
        )
        sampled_members.append(_member_progress(sample, member))
    return ModelCacheOperationProgress(
        phase=counters.phase,
        completed_artifacts=counters.completed_artifacts,
        total_artifacts=counters.total_artifacts,
        downloaded_bytes=counters.downloaded_bytes,
        expected_bytes=total,
        current_artifact_key=counters.current_artifact_key,
        total_bytes_known=total is not None,
        measurement=sampled.model_copy(update={"members": sampled_members}),
    )


def cache_phase(
    current: ModelCacheOperationProgress,
    phase: ModelCacheOperationPhase,
    now: datetime,
    *,
    waiting: bool = False,
) -> ModelCacheOperationProgress:
    members = [
        member.model_copy(update={"phase": PHASES[phase]})
        for member in current.measurement.members
    ]
    result = cache_progress(
        ModelCacheCounters(
            phase=phase,
            completed_artifacts=current.completed_artifacts,
            total_artifacts=current.total_artifacts,
            downloaded_bytes=current.downloaded_bytes,
            expected_bytes=current.expected_bytes,
            current_artifact_key=current.current_artifact_key,
        ),
        previous=current,
        now=now,
        members=members,
    )
    if waiting:
        measurement = result.measurement
        result = result.model_copy(
            update={
                "measurement": sample_progress(
                    measurement,
                    measurement.model_copy(update={"phase": ProgressPhase.WAITING}),
                    now,
                )
            }
        )
    return read_stored_model(
        ModelCacheOperationProgress, json.loads(canonical_message(result))
    )


def project_cache_progress(
    value: object, now: datetime | None = None
) -> dict[str, object]:
    """The advisory projection of a stored progress document (the API edge)."""

    parsed = read_stored_model(ModelCacheOperationProgress, value)
    measurement = project_progress(parsed.measurement, now)
    members = []
    for member in measurement.members:
        projected = project_progress(_member_as_progress(member), now)
        members.append(_member_progress(projected, member))
    return json.loads(
        canonical_message(measurement.model_copy(update={"members": members}))
    )
