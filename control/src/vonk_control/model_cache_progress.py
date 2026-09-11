"""Current cache counters with one canonical, durable progress measurement."""

import json
from collections.abc import Mapping
from datetime import datetime

from vonk_agent_protocol import (
    OperationCheckpoint,
    OperationMemberProgress,
    OperationProgress,
    canonical_message,
)

from .bounded_json import integer, require_integer, require_mapping, text
from .model_cache_contract import ModelCacheOperationPhase, ModelCacheOperationProgress
from .operation_progress import STALE_AFTER_SECONDS, observe_progress, project_progress

PHASES = {
    "queued": "queued",
    "downloading": "download",
    "verifying": "verify",
    "reclaiming": "cleanup",
    "completed": "completed",
    "failed": "failed",
}


def _sample(
    previous: Mapping[str, object] | None, current: dict[str, object], now: datetime
) -> dict[str, object]:
    observed_at = previous.get("observed_at") if previous else None
    if isinstance(observed_at, str) and observed_at:
        interval = (now - datetime.fromisoformat(observed_at)).total_seconds()
        if (
            interval >= STALE_AFTER_SECONDS or interval < 0
        ) and previous is not None:
            # A disconnected worker is not a throughput sample. Preserve elapsed
            # work already measured, but begin a fresh adjacent receipt window.
            previous = dict(previous, observed_at=now.isoformat())
            previous.pop("smoothed_bytes_per_second", None)
    return observe_progress(previous, current, now)


def _member_progress(
    measurement: OperationProgress, identity: OperationMemberProgress
) -> OperationMemberProgress:
    """Project a validated operation sample into its canonical member fields."""
    shared_fields = (
        OperationProgress.model_fields.keys()
        & OperationMemberProgress.model_fields.keys()
    )
    return OperationMemberProgress.model_validate(
        {
            **measurement.model_dump(mode="json", include=shared_fields),
            "member_id": identity.member_id,
            "state": identity.state,
        }
    )


def cache_progress(
    document: Mapping[str, object],
    *,
    previous: Mapping[str, object] | None,
    now: datetime,
    members: list[OperationMemberProgress] | None = None,
) -> dict[str, object]:
    old = (
        ModelCacheOperationProgress.model_validate(previous)
        if previous is not None
        else None
    )
    value = dict(document)
    total = integer(value.get("expected_bytes"))
    value["total_bytes_known"] = total is not None
    phase_key = value["phase"]
    if not isinstance(phase_key, str) or phase_key not in PHASES:
        raise ValueError("cache progress phase is invalid")
    measurement = OperationProgress(
        phase=PHASES[phase_key],
        completed_bytes=require_integer(value["downloaded_bytes"], "downloaded bytes"),
        total_bytes=total,
        total_bytes_known=total is not None,
        completed_items=require_integer(value["completed_artifacts"], "completed artifacts"),
        total_items=require_integer(value["total_artifacts"], "total artifacts"),
        checkpoint=OperationCheckpoint(
            key="artifact-set",
            sequence=require_integer(value["completed_artifacts"], "completed artifacts"),
            cursor=text(value.get("current_artifact_key")),
        ),
    )
    sampled = _sample(
        old.measurement.model_dump(mode="json") if old else None,
        measurement.model_dump(mode="json"),
        now,
    )
    prior_members = (
        {member.member_id: member for member in old.measurement.members} if old else {}
    )
    sampled_members = []
    for member in members or []:
        prior = prior_members.get(member.member_id)
        data = member.model_dump(mode="json", exclude_none=True)
        for key in ("member_id", "state"):
            data.pop(key)
        data["total_bytes_known"] = member.total_bytes is not None
        prior_data = None
        if prior is not None:
            prior_data = prior.model_dump(
                mode="json", exclude={"member_id", "state"}, exclude_none=True
            )
        sample = _sample(prior_data, data, now)
        sampled_members.append(
            _member_progress(OperationProgress.model_validate(sample), member)
        )
    sampled["members"] = [
        member.model_dump(mode="json", exclude_none=True) for member in sampled_members
    ]
    value["measurement"] = sampled
    return json.loads(
        canonical_message(ModelCacheOperationProgress.model_validate(value))
    )


def cache_phase(
    value: Mapping[str, object],
    phase: ModelCacheOperationPhase,
    now: datetime,
    *,
    waiting: bool = False,
) -> dict[str, object]:
    current = ModelCacheOperationProgress.model_validate(value)
    document = current.model_dump(mode="json", exclude={"measurement"})
    document["phase"] = phase
    members = [
        member.model_copy(update={"phase": PHASES[phase]})
        for member in current.measurement.members
    ]
    result = cache_progress(document, previous=value, now=now, members=members)
    if waiting:
        measurement_document = require_mapping(
            result["measurement"], "cache measurement"
        )
        result["measurement"] = observe_progress(
            measurement_document,
            dict(measurement_document, phase="waiting"),
            now,
        )
    return json.loads(
        canonical_message(ModelCacheOperationProgress.model_validate(result))
    )


def project_cache_progress(
    value: Mapping[str, object], now: datetime | None = None
) -> dict[str, object]:
    parsed = ModelCacheOperationProgress.model_validate(value)
    measurement = project_progress(parsed.measurement, now)
    members = []
    for member in measurement.members:
        raw = member.model_dump(
            mode="json", exclude={"member_id", "state"}, exclude_none=True
        )
        raw["total_bytes_known"] = member.total_bytes is not None
        projected = project_progress(OperationProgress.model_validate(raw), now)
        members.append(_member_progress(projected, member))
    return json.loads(
        canonical_message(measurement.model_copy(update={"members": members}))
    )
