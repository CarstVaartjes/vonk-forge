"""Durable single-lane recovery checkpoints for paired qualification batches.

The batch runner owns pairing and Controller plans. This module only binds a
reviewed one-lane transition to its exact canary, observes the offline/changed
boot boundary, and records post-reboot serving and fixture evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypedDict

from .fleet_qualification import EvidenceLedger, QualificationError

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class _LoadedProjection(TypedDict):
    alias: str
    recipe_revision_id: str
    ranks: dict[str, int]
    route_states: set[str]


class _FleetNode(TypedDict):
    online_state: str
    telemetry_freshness: object
    boot_id: object
    telemetry_observed_at: object
    loaded_run_ids: set[str]
    published_route_aliases: set[str]
    reservations: dict[str, int]


class _FleetView(TypedDict):
    event_cursor: int
    generated_at: datetime
    authority_revision: str
    nodes: dict[str, _FleetNode]
    loaded_runs: dict[str, _LoadedProjection]
    published_route_aliases: set[str]


class _CleanupContext(TypedDict):
    cleanup_mode: str
    terminal_event: str
    terminal_record: Mapping[str, object]
    active_run_id: str
    active_assignment: dict[str, object]
    stop_assignments: dict[str, dict[str, int]]
    stop_aliases: list[str]
    recovery_profile_number: object
    recovery_profile_id: object
    recovery_profile_digest: object
    recovery_plan_digest: object


@dataclass(frozen=True, slots=True)
class CanaryReference:
    lane_id: int
    record_sha256: str
    recipe_key: str
    recipe_content_sha256: str
    package_sha256: str
    assigned_node_id: str
    assigned_rank: int
    run_id: str | None
    recipe_revision_id: str | None
    alias: str | None
    node_to_rank: Mapping[str, int] | None
    outcome_event: str = "canary.completed"


@dataclass(frozen=True, slots=True)
class LaneRecoveryTarget:
    campaign_id: str
    batch_id: str
    lane_id: int
    recipe_key: str
    recipe_content_sha256: str
    package_sha256: str
    original_run_id: str | None
    recipe_revision_id: str
    alias: str
    node_id: str
    node_to_rank: Mapping[str, int]
    smoke_case_ids: tuple[str, ...]
    fleet_node_ids: tuple[str, ...]
    partner_run_ids: tuple[str, ...]
    partner_aliases: tuple[str, ...]
    canaries: tuple[CanaryReference, ...]
    profile_number: int
    profile_id: str
    profile_digest: str
    plan_digest: str
    allow_reactivation: bool = False


@dataclass(frozen=True, slots=True)
class LaneRecoveryProgress:
    status: str
    target_digest: str
    active_run_id: str | None = None
    baseline_boot_id: str | None = None
    recovered_boot_id: str | None = None
    receipt_sha256: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class LaneTransitionReview:
    target_digest: str
    request_key: str
    review_digest: str
    profile_number: int
    profile_id: str
    profile_digest: str
    plan_digest: str
    receipt: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class LaneCleanupReview:
    target_digest: str
    request_key: str
    review_digest: str
    profile_number: int
    profile_id: str
    profile_digest: str
    plan_digest: str
    receipt: Mapping[str, object]


def review_lane_transition(
    target: LaneRecoveryTarget,
    ledger: EvidenceLedger,
    *,
    prepare_transition: Callable[[Mapping[str, object]], Mapping[str, object]],
) -> LaneTransitionReview:
    """Persist one exact, unapplied exclusive-profile review.

    The durable intent is written before the runner callback can save a profile
    or preview its Controller plan. This function never applies that plan.
    """

    binding = _target_binding(target)
    target_digest = _digest(binding)
    _validate_target(target)
    _require_canary_records(target, ledger)
    _reject_conflicting_lane_evidence(target, ledger, target_digest)
    request_key = _request_key("transition", target, target_digest)
    intent = _event(ledger, target, target_digest, "lane_recovery.intent")
    if intent is None:
        intent = ledger.append(
            "lane_recovery.intent",
            plan_digest=target.campaign_id,
            recipe=target.recipe_key,
            payload={
                **_envelope(target, target_digest),
                "transition_request_key": request_key,
                "canary_record_sha256s": [
                    item.record_sha256 for item in target.canaries
                ],
            },
        )
    intent_payload = _payload(intent)
    if intent_payload.get("transition_request_key") != request_key:
        raise QualificationError("lane recovery intent has an invalid transition key")

    receipt = prepare_transition(
        {
            **_envelope(target, target_digest),
            "request_key": request_key,
            "profile_number": target.profile_number,
            "profile_id": target.profile_id,
            "profile_digest": target.profile_digest,
            "plan_digest": target.plan_digest,
            "preferred_run_id": target.original_run_id,
            "partner_run_ids": list(target.partner_run_ids),
            "partner_aliases": list(target.partner_aliases),
            "fleet_node_ids": list(target.fleet_node_ids),
            "allow_reactivation": target.allow_reactivation,
        }
    )
    review = _validate_transition_review(target, request_key, receipt, ledger)
    review_digest = _digest(dict(review))
    previous = _event(ledger, target, target_digest, "lane_recovery.plan_reviewed")
    if previous is None:
        ledger.append(
            "lane_recovery.plan_reviewed",
            plan_digest=target.campaign_id,
            recipe=target.recipe_key,
            payload={
                **_envelope(target, target_digest),
                "request_key": request_key,
                "review_digest": review_digest,
                "review": dict(review),
                "profile_number": review["profile_number"],
                "profile_id": review["profile_id"],
                "profile_digest": review["profile_digest"],
                "plan_digest": review["plan_digest"],
            },
        )
    else:
        previous_payload = _payload(previous)
        if (
            previous_payload.get("request_key") != request_key
            or previous_payload.get("review_digest") != review_digest
            or previous_payload.get("review") != dict(review)
        ):
            raise QualificationError(
                "lane transition preview changed after durable review"
            )
    return LaneTransitionReview(
        target_digest=target_digest,
        request_key=request_key,
        review_digest=review_digest,
        profile_number=_positive_int(
            review.get("profile_number"), "reviewed profile number"
        ),
        profile_id=str(review["profile_id"]),
        profile_digest=str(review["profile_digest"]),
        plan_digest=str(review["plan_digest"]),
        receipt=review,
    )


def recover_single_lane(
    target: LaneRecoveryTarget,
    ledger: EvidenceLedger,
    *,
    prepare_transition: Callable[[Mapping[str, object]], Mapping[str, object]],
    apply_authorized: bool,
    transition_to_lane: Callable[[Mapping[str, object]], Mapping[str, object]],
    observe_fleet: Callable[[], Mapping[str, object]],
    verify_serving: Callable[[Mapping[str, object]], Mapping[str, object]],
    run_fixture_smoke: Callable[[Mapping[str, object]], Mapping[str, object]],
) -> LaneRecoveryProgress:
    """Apply a reviewed single-lane transition, then advance recovery evidence.

    A preview invocation passes ``apply_authorized=False`` and stops after the
    durable review. The apply invocation repeats the review callback so the
    saved plan must still match before effects are submitted.
    """

    review = review_lane_transition(
        target, ledger, prepare_transition=prepare_transition
    )
    target_digest = review.target_digest
    transition_key = review.request_key
    if type(apply_authorized) is not bool:
        raise QualificationError("lane recovery apply authorization must be explicit")
    if apply_authorized is not True:
        return LaneRecoveryProgress(
            "awaiting-explicit-apply",
            target_digest,
            reason="the exact one-lane transition is reviewed; rerun with explicit apply authorization",
        )

    transitioned = _event(ledger, target, target_digest, "lane_recovery.transitioned")
    if transitioned is None:
        receipt = transition_to_lane(
            {
                **_review_envelope(target, target_digest, review),
                "request_key": transition_key,
                "review_digest": review.review_digest,
                "review": dict(review.receipt),
                "profile_number": review.profile_number,
                "profile_id": review.profile_id,
                "profile_digest": review.profile_digest,
                "plan_digest": review.plan_digest,
                "keep_run_id": review.receipt.get("keep_run_id"),
                "stop_run_ids": _string_list(
                    review.receipt.get("stop_run_ids"), "reviewed partner runs"
                ),
                "stop_aliases": _string_list(
                    review.receipt.get("stop_aliases"), "reviewed partner routes"
                ),
                "allow_reactivation": target.allow_reactivation,
            }
        )
        active = _validate_transition(target, review, receipt)
        transitioned = ledger.append(
            "lane_recovery.transitioned",
            plan_digest=target.campaign_id,
            recipe=target.recipe_key,
            payload={
                **_review_envelope(target, target_digest, review),
                "request_key": transition_key,
                "review_digest": review.review_digest,
                "receipt": dict(receipt),
                "active_run_id": active["run_id"],
                "reactivated": active["reactivated"],
            },
        )
    else:
        transitioned_payload = _payload(transitioned)
        if transitioned_payload.get("review_digest") != review.review_digest:
            raise QualificationError(
                "applied transition differs from its durable review"
            )
        receipt_value = _payload(transitioned).get("receipt")
        active = _validate_transition(
            target,
            review,
            _mapping(receipt_value, "persisted lane transition receipt"),
        )

    return _advance_recovery(
        target,
        ledger,
        review,
        active,
        observe_fleet=observe_fleet,
        verify_serving=verify_serving,
        run_fixture_smoke=run_fixture_smoke,
    )


def observe_single_lane(
    target: LaneRecoveryTarget,
    ledger: EvidenceLedger,
    *,
    observe_fleet: Callable[[], Mapping[str, object]],
    verify_serving: Callable[[Mapping[str, object]], Mapping[str, object]],
    run_fixture_smoke: Callable[[Mapping[str, object]], Mapping[str, object]],
) -> LaneRecoveryProgress:
    """Advance recovery using only durable transition state and read callbacks."""

    _validate_target(target)
    _require_canary_records(target, ledger)
    review = _load_transition_review(target, ledger)
    transitioned = _require_event(
        ledger, target, review.target_digest, "lane_recovery.transitioned"
    )
    payload = _payload(transitioned)
    if payload.get("review_digest") != review.review_digest:
        raise QualificationError("applied transition differs from its durable review")
    receipt = _mapping(payload.get("receipt"), "persisted lane transition receipt")
    active = _validate_transition(target, review, receipt)
    return _advance_recovery(
        target,
        ledger,
        review,
        active,
        observe_fleet=observe_fleet,
        verify_serving=verify_serving,
        run_fixture_smoke=run_fixture_smoke,
    )


def _advance_recovery(
    target: LaneRecoveryTarget,
    ledger: EvidenceLedger,
    review: LaneTransitionReview,
    active: Mapping[str, object],
    *,
    observe_fleet: Callable[[], Mapping[str, object]],
    verify_serving: Callable[[Mapping[str, object]], Mapping[str, object]],
    run_fixture_smoke: Callable[[Mapping[str, object]], Mapping[str, object]],
) -> LaneRecoveryProgress:
    target_digest = review.target_digest
    transition_event = _require_event(
        ledger, target, target_digest, "lane_recovery.transitioned"
    )
    transition_payload = _payload(transition_event)
    if transition_payload.get("review_digest") != review.review_digest:
        raise QualificationError("lane transition checkpoint changed its reviewed plan")
    transition_receipt = _mapping(
        transition_payload.get("receipt"), "durable lane transition receipt"
    )
    transition_view = _fleet_view(
        transition_receipt.get("fleet_snapshot"), target.fleet_node_ids
    )
    transition_at = transition_view["generated_at"]
    transition_cursor = transition_view["event_cursor"]
    transition_authority = transition_view["authority_revision"]
    _check_recovery_event_reviews(target, ledger, review)
    active_run_id = _str(active.get("run_id"), "active recovery run ID")
    baseline = _event(ledger, target, target_digest, "lane_recovery.baseline")
    if baseline is None:
        observation = observe_fleet()
        state = _validate_observation(target, active, review, observation)
        if (
            _timestamp(state.get("generated_at"), "baseline Fleet generated_at")
            < transition_at
            or _cursor(state.get("event_cursor"), "baseline event cursor")
            < transition_cursor
        ):
            return LaneRecoveryProgress(
                "awaiting-fresh-fleet-observation",
                target_digest,
                active_run_id,
                reason="baseline Fleet snapshot must follow the reviewed transition application",
            )
        _require_same_authority(state, str(transition_authority))
        if state["online_state"] != "online":
            return LaneRecoveryProgress(
                "awaiting-host-online",
                target_digest,
                active_run_id,
                reason="the exact one-lane run must be online before its boot baseline is captured",
            )
        if state.get("telemetry_freshness") != "live" or not isinstance(
            state.get("boot_id"), str
        ):
            return LaneRecoveryProgress(
                "awaiting-live-telemetry",
                target_digest,
                active_run_id,
                reason="the selected Spark needs live Fleet telemetry before its boot baseline can be captured",
            )
        if state.get("serving_ready") is not True:
            return LaneRecoveryProgress(
                "awaiting-lane-serving",
                target_digest,
                active_run_id,
                reason="the exact lane is not serving yet; resume after Controller recovery settles",
            )
        boot_id = _str(state.get("boot_id"), "baseline boot ID")
        telemetry_observed_at = _str(
            state.get("telemetry_observed_at"), "baseline telemetry observation time"
        )
        baseline = ledger.append(
            "lane_recovery.baseline",
            plan_digest=target.campaign_id,
            recipe=target.recipe_key,
            payload={
                **_review_envelope(target, target_digest, review),
                "active_run_id": active_run_id,
                "boot_id": boot_id,
                "generated_at": state.get("generated_at"),
                "telemetry_observed_at": telemetry_observed_at,
                "authority_revision": state.get("authority_revision"),
                "event_cursor": state.get("event_cursor"),
            },
        )
    baseline_payload = _payload(baseline)
    baseline_boot_id = _str(baseline_payload.get("boot_id"), "baseline boot ID")
    baseline_cursor = _cursor(
        baseline_payload.get("event_cursor"), "baseline event cursor"
    )
    baseline_generated_at = _timestamp(
        baseline_payload.get("generated_at"), "baseline Fleet generated_at"
    )
    baseline_telemetry_at = _timestamp(
        baseline_payload.get("telemetry_observed_at"), "baseline telemetry observed_at"
    )
    baseline_authority = _str(
        baseline_payload.get("authority_revision"), "baseline authority revision"
    )
    if (
        baseline_generated_at < transition_at
        or baseline_cursor < transition_cursor
        or baseline_authority != transition_authority
    ):
        raise QualificationError(
            "durable boot baseline predates the reviewed lane transition"
        )

    offline = _event(ledger, target, target_digest, "lane_recovery.offline_observed")
    newly_observed_offline = False
    if offline is None:
        observation = observe_fleet()
        state = _validate_observation(target, active, review, observation)
        if state["online_state"] == "online":
            _require_same_authority(state, baseline_authority)
            if state.get("boot_id") != baseline_boot_id:
                return LaneRecoveryProgress(
                    "awaiting-host-offline",
                    target_digest,
                    active_run_id,
                    baseline_boot_id,
                    reason="boot changed before an offline observation was durably recorded",
                )
            return LaneRecoveryProgress(
                "awaiting-host-offline",
                target_digest,
                active_run_id,
                baseline_boot_id,
                reason="record the selected host offline, then resume this lane",
            )
        boot_id = state.get("boot_id")
        if boot_id not in (None, baseline_boot_id):
            return LaneRecoveryProgress(
                "awaiting-host-offline",
                target_digest,
                active_run_id,
                baseline_boot_id,
                reason="offline telemetry carries a changed boot ID without an ordered observation",
            )
        offline_cursor = _cursor(state.get("event_cursor"), "offline event cursor")
        offline_generated_at = _timestamp(
            state.get("generated_at"), "offline Fleet generated_at"
        )
        if (
            offline_cursor < baseline_cursor
            or offline_generated_at <= baseline_generated_at
        ):
            return LaneRecoveryProgress(
                "awaiting-host-offline",
                target_digest,
                active_run_id,
                baseline_boot_id,
                reason="offline Fleet observation is not newer than the baseline",
            )
        offline = ledger.append(
            "lane_recovery.offline_observed",
            plan_digest=target.campaign_id,
            recipe=target.recipe_key,
            payload={
                **_review_envelope(target, target_digest, review),
                "active_run_id": active_run_id,
                "node_id": target.node_id,
                "baseline_boot_id": baseline_boot_id,
                "online_state": "offline",
                "generated_at": state.get("generated_at"),
                "authority_revision": state.get("authority_revision"),
                "event_cursor": state.get("event_cursor"),
            },
        )
        newly_observed_offline = True

    offline_payload = _payload(offline)
    if (
        offline_payload.get("node_id") != target.node_id
        or offline_payload.get("active_run_id") != active_run_id
        or offline_payload.get("baseline_boot_id") != baseline_boot_id
        or offline_payload.get("online_state") != "offline"
    ):
        raise QualificationError("offline checkpoint differs from exact lane baseline")
    offline_cursor = _cursor(
        offline_payload.get("event_cursor"), "offline event cursor"
    )
    _require_same_authority(offline_payload, baseline_authority)
    offline_generated_at = _timestamp(
        offline_payload.get("generated_at"), "offline Fleet generated_at"
    )
    if (
        offline_cursor < baseline_cursor
        or offline_generated_at <= baseline_generated_at
    ):
        raise QualificationError(
            "offline Fleet observation is stale against its boot baseline"
        )
    if newly_observed_offline:
        return LaneRecoveryProgress(
            "awaiting-host-online",
            target_digest,
            active_run_id,
            baseline_boot_id,
            reason="offline state is durable; restart or power on the selected host, then resume",
        )

    recovered = _event(ledger, target, target_digest, "lane_recovery.recovered")
    if recovered is None:
        observation = observe_fleet()
        state = _validate_observation(target, active, review, observation)
        if state["online_state"] == "offline":
            return LaneRecoveryProgress(
                "awaiting-host-online",
                target_digest,
                active_run_id,
                baseline_boot_id,
                reason="offline state is durable; restart or power on the selected host, then resume",
            )
        _require_same_authority(state, baseline_authority)
        boot_id = state.get("boot_id")
        if state.get("telemetry_freshness") != "live" or not isinstance(boot_id, str):
            return LaneRecoveryProgress(
                "awaiting-live-telemetry",
                target_digest,
                active_run_id,
                baseline_boot_id,
                reason="the returning host needs a live Fleet boot ID",
            )
        if boot_id == baseline_boot_id:
            return LaneRecoveryProgress(
                "awaiting-changed-boot",
                target_digest,
                active_run_id,
                baseline_boot_id,
                reason="the selected host is online but its boot ID has not changed",
            )
        observation_cursor = _cursor(
            state.get("event_cursor"), "recovered event cursor"
        )
        observation_generated_at = _timestamp(
            state.get("generated_at"), "recovered Fleet generated_at"
        )
        if (
            observation_cursor < offline_cursor
            or observation_generated_at <= offline_generated_at
        ):
            return LaneRecoveryProgress(
                "awaiting-fresh-fleet-observation",
                target_digest,
                active_run_id,
                baseline_boot_id,
                reason="the returning Fleet snapshot predates the durable offline observation",
            )
        telemetry_observed_at = _str(
            state.get("telemetry_observed_at"), "recovered telemetry observed_at"
        )
        telemetry_observed_dt = _timestamp(
            telemetry_observed_at, "recovered telemetry observed_at"
        )
        if telemetry_observed_dt <= baseline_telemetry_at:
            return LaneRecoveryProgress(
                "awaiting-fresh-telemetry",
                target_digest,
                active_run_id,
                baseline_boot_id,
                reason="live telemetry must be sampled after the active-run baseline",
            )
        boot_observed = _event(
            ledger, target, target_digest, "lane_recovery.boot_observed"
        )
        if boot_observed is None:
            boot_observed = ledger.append(
                "lane_recovery.boot_observed",
                plan_digest=target.campaign_id,
                recipe=target.recipe_key,
                payload={
                    **_review_envelope(target, target_digest, review),
                    "node_id": target.node_id,
                    "active_run_id": active_run_id,
                    "baseline_boot_id": baseline_boot_id,
                    "observed_boot_id": boot_id,
                    "authority_revision": state.get("authority_revision"),
                    "event_cursor": observation_cursor,
                    "generated_at": state.get("generated_at"),
                    "telemetry_observed_at": telemetry_observed_at,
                },
            )
        else:
            boot_payload = _payload(boot_observed)
            if boot_payload.get("observed_boot_id") != boot_id:
                raise QualificationError(
                    "target host rebooted again before recovery completed"
                )
            if observation_cursor < _cursor(
                boot_payload.get("event_cursor"), "observed boot event cursor"
            ):
                return LaneRecoveryProgress(
                    "awaiting-fresh-fleet-observation",
                    target_digest,
                    active_run_id,
                    baseline_boot_id,
                    reason="the Fleet snapshot predates the durable changed-boot observation",
                )
            if observation_generated_at < _timestamp(
                boot_payload.get("generated_at"), "observed boot Fleet time"
            ):
                return LaneRecoveryProgress(
                    "awaiting-fresh-fleet-observation",
                    target_digest,
                    active_run_id,
                    baseline_boot_id,
                    reason="the Fleet snapshot predates the durable changed-boot observation",
                )
        if state.get("serving_ready") is not True:
            return LaneRecoveryProgress(
                "awaiting-lane-serving",
                target_digest,
                active_run_id,
                baseline_boot_id,
                reason="the changed boot is durable; wait for the exact run and route to recover",
            )
        serving = verify_serving(
            {
                **_review_envelope(target, target_digest, review),
                "active": dict(active),
                "boot_id": boot_id,
            }
        )
        _validate_serving_receipt(target, active, boot_id, serving)
        recovered = ledger.append(
            "lane_recovery.recovered",
            plan_digest=target.campaign_id,
            recipe=target.recipe_key,
            payload={
                **_review_envelope(target, target_digest, review),
                "active_run_id": active_run_id,
                "node_id": target.node_id,
                "baseline_boot_id": baseline_boot_id,
                "observed_boot_id": boot_id,
                "authority_revision": state.get("authority_revision"),
                "event_cursor": observation_cursor,
                "generated_at": state.get("generated_at"),
                "telemetry_observed_at": telemetry_observed_at,
                "boot_observed_record_sha256": boot_observed.get("record_sha256"),
                "serving_receipt": dict(serving),
            },
        )

    recovered_payload = _payload(recovered)
    recovered_boot_id = _str(
        recovered_payload.get("observed_boot_id"), "recovered boot ID"
    )
    if (
        recovered_payload.get("node_id") != target.node_id
        or recovered_payload.get("active_run_id") != active_run_id
        or recovered_payload.get("baseline_boot_id") != baseline_boot_id
        or recovered_boot_id == baseline_boot_id
    ):
        raise QualificationError(
            "recovered checkpoint differs from exact lane identity"
        )
    _require_same_authority(recovered_payload, baseline_authority)
    recovered_cursor = _cursor(
        recovered_payload.get("event_cursor"), "recovered event cursor"
    )
    recovered_generated_at = _timestamp(
        recovered_payload.get("generated_at"), "recovered Fleet generated_at"
    )
    recovered_telemetry_at = _timestamp(
        recovered_payload.get("telemetry_observed_at"),
        "recovered telemetry observed_at",
    )
    if (
        recovered_cursor < offline_cursor
        or recovered_generated_at <= offline_generated_at
        or recovered_telemetry_at <= baseline_telemetry_at
    ):
        raise QualificationError("recovered Fleet/telemetry observation is stale")
    current_observation = observe_fleet()
    current = _validate_observation(target, active, review, current_observation)
    _require_same_authority(current, baseline_authority)
    current_generated_at = _timestamp(
        current.get("generated_at"), "current Fleet generated_at"
    )
    current_cursor = _cursor(current.get("event_cursor"), "current Fleet cursor")
    current_telemetry_at = (
        _timestamp(
            current.get("telemetry_observed_at"), "current telemetry observed_at"
        )
        if current.get("telemetry_observed_at") is not None
        else recovered_telemetry_at
    )
    if (
        current["online_state"] != "online"
        or current.get("telemetry_freshness") != "live"
        or current.get("boot_id") != recovered_boot_id
        or current.get("serving_ready") is not True
        or current_generated_at < recovered_generated_at
        or current_cursor < recovered_cursor
        or current_telemetry_at < recovered_telemetry_at
    ):
        raise QualificationError("recovered lane changed after its serving receipt")

    smoke = _event(ledger, target, target_digest, "lane_recovery.smoke_completed")
    if smoke is None:
        smoke_request = _event(
            ledger, target, target_digest, "lane_recovery.smoke.requested"
        )
        smoke_key = _request_key("smoke", target, target_digest)
        if smoke_request is None:
            smoke_request = ledger.append(
                "lane_recovery.smoke.requested",
                plan_digest=target.campaign_id,
                recipe=target.recipe_key,
                payload={
                    **_review_envelope(target, target_digest, review),
                    "request_key": smoke_key,
                    "active_run_id": active_run_id,
                    "recovered_record_sha256": recovered.get("record_sha256"),
                    "case_ids": list(target.smoke_case_ids),
                },
            )
        if _payload(smoke_request).get("request_key") != smoke_key:
            raise QualificationError("durable fixture smoke request changed identity")
        result = run_fixture_smoke(
            {
                **_review_envelope(target, target_digest, review),
                "request_key": smoke_key,
                "active": dict(active),
                "boot_id": recovered_boot_id,
                "case_ids": list(target.smoke_case_ids),
            }
        )
        _validate_smoke_receipt(target, active, recovered_boot_id, result)
        smoke = ledger.append(
            "lane_recovery.smoke_completed",
            plan_digest=target.campaign_id,
            recipe=target.recipe_key,
            payload={
                **_review_envelope(target, target_digest, review),
                "request_key": smoke_key,
                "active_run_id": active_run_id,
                "recovered_boot_id": recovered_boot_id,
                "smoke_receipt": dict(result),
            },
        )
    else:
        _validate_smoke_receipt(
            target,
            active,
            recovered_boot_id,
            _mapping(_payload(smoke).get("smoke_receipt"), "persisted fixture smoke"),
        )

    completed = _event(ledger, target, target_digest, "lane_recovery.completed")
    if completed is None:
        references = {
            name: _require_event(ledger, target, target_digest, event).get(
                "record_sha256"
            )
            for name, event in (
                ("intent", "lane_recovery.intent"),
                ("transitioned", "lane_recovery.transitioned"),
                ("baseline", "lane_recovery.baseline"),
                ("offline", "lane_recovery.offline_observed"),
                ("boot_observed", "lane_recovery.boot_observed"),
                ("recovered", "lane_recovery.recovered"),
                ("smoke", "lane_recovery.smoke_completed"),
            )
        }
        receipt_base = {
            "schema_version": 1,
            **_review_envelope(target, target_digest, review),
            "active_run_id": active_run_id,
            "active_assignment": dict(active),
            "baseline_boot_id": baseline_boot_id,
            "recovered_boot_id": recovered_boot_id,
            "event_refs": references,
        }
        receipt_sha256 = _digest(receipt_base)
        completed = ledger.append(
            "lane_recovery.completed",
            plan_digest=target.campaign_id,
            recipe=target.recipe_key,
            payload={**receipt_base, "receipt_sha256": receipt_sha256},
        )
    completed_payload = _payload(completed)
    receipt_body = dict(completed_payload)
    recorded_digest = receipt_body.pop("receipt_sha256", None)
    if not isinstance(recorded_digest, str) or _digest(receipt_body) != recorded_digest:
        raise QualificationError("lane recovery completion digest is invalid")
    return LaneRecoveryProgress(
        "completed",
        target_digest,
        active_run_id,
        baseline_boot_id,
        recovered_boot_id,
        recorded_digest,
    )


def review_lane_cleanup(
    target: LaneRecoveryTarget,
    ledger: EvidenceLedger,
    *,
    prepare_cleanup: Callable[[Mapping[str, object]], Mapping[str, object]],
) -> LaneCleanupReview:
    """Persist an exact unapplied cleanup review for a terminal lane outcome."""

    target_digest = _digest(_target_binding(target))
    _validate_target(target, require_passing_lane=False)
    _require_canary_records(target, ledger)
    context = _cleanup_context(target, ledger, target_digest)
    terminal_record = context["terminal_record"]
    terminal_sha256 = _str(
        terminal_record.get("record_sha256"), "cleanup terminal record SHA-256"
    )
    request_key = _request_key("cleanup", target, target_digest)
    intent = _event(ledger, target, target_digest, "lane_recovery.cleanup.intent")
    intent_values = {
        "request_key": request_key,
        "cleanup_mode": context["cleanup_mode"],
        "terminal_event": context["terminal_event"],
        "terminal_record_sha256": terminal_sha256,
        "active_run_id": context["active_run_id"],
        "stop_run_ids": sorted(context["stop_assignments"]),
        "stop_aliases": context["stop_aliases"],
    }
    if intent is None:
        intent = ledger.append(
            "lane_recovery.cleanup.intent",
            plan_digest=target.campaign_id,
            recipe=target.recipe_key,
            payload={**_envelope(target, target_digest), **intent_values},
        )
    elif any(
        _payload(intent).get(key) != value for key, value in intent_values.items()
    ):
        raise QualificationError("lane cleanup intent changed its exact release set")
    receipt = prepare_cleanup(
        {
            **_envelope(target, target_digest),
            "request_key": request_key,
            "cleanup_mode": context["cleanup_mode"],
            "terminal_event": context["terminal_event"],
            "terminal_record_sha256": terminal_sha256,
            "active_run_id": context["active_run_id"],
            "expected_stop_run_ids": sorted(context["stop_assignments"]),
            "expected_stop_aliases": list(context["stop_aliases"]),
            "fleet_node_ids": list(target.fleet_node_ids),
            "recovery_profile_number": context.get("recovery_profile_number"),
            "recovery_profile_id": context.get("recovery_profile_id"),
            "recovery_profile_digest": context.get("recovery_profile_digest"),
            "recovery_plan_digest": context.get("recovery_plan_digest"),
        }
    )
    review = _validate_cleanup_review(
        target,
        request_key,
        str(context["cleanup_mode"]),
        str(context["terminal_event"]),
        terminal_sha256,
        str(context["active_run_id"]),
        context["stop_assignments"],
        context["stop_aliases"],
        receipt,
        ledger,
    )
    review_digest = _digest(dict(review))
    previous = _event(
        ledger, target, target_digest, "lane_recovery.cleanup.plan_reviewed"
    )
    if previous is None:
        ledger.append(
            "lane_recovery.cleanup.plan_reviewed",
            plan_digest=target.campaign_id,
            recipe=target.recipe_key,
            payload={
                **_envelope(target, target_digest),
                "request_key": request_key,
                "review_digest": review_digest,
                "review": dict(review),
            },
        )
    elif (
        _payload(previous).get("request_key") != request_key
        or _payload(previous).get("review_digest") != review_digest
        or _payload(previous).get("review") != dict(review)
    ):
        raise QualificationError("lane cleanup preview changed after durable review")
    return LaneCleanupReview(
        target_digest=target_digest,
        request_key=request_key,
        review_digest=review_digest,
        profile_number=_positive_int(
            review.get("profile_number"), "reviewed cleanup profile number"
        ),
        profile_id=str(review["profile_id"]),
        profile_digest=str(review["profile_digest"]),
        plan_digest=str(review["plan_digest"]),
        receipt=review,
    )


def record_lane_cleanup(
    target: LaneRecoveryTarget,
    ledger: EvidenceLedger,
    *,
    prepare_cleanup: Callable[[Mapping[str, object]], Mapping[str, object]],
    apply_authorized: bool,
    cleanup_to_idle: Callable[[Mapping[str, object]], Mapping[str, object]],
    reconcile_cleanup: Callable[[Mapping[str, object]], Mapping[str, object]]
    | None = None,
) -> LaneRecoveryProgress:
    """Apply reviewed cleanup and resume its receipt after a process restart."""

    if type(apply_authorized) is not bool:
        raise QualificationError("lane cleanup apply authorization must be explicit")
    target_digest = _digest(_target_binding(target))
    _validate_target(target, require_passing_lane=False)
    _require_canary_records(target, ledger)
    context = _cleanup_context(target, ledger, target_digest)
    cleanup = _event(ledger, target, target_digest, "lane_recovery.cleanup.completed")

    if cleanup is not None:
        review = _load_cleanup_review(target, ledger, target_digest, context)
        persisted_receipt = _validate_cleanup_completion(
            target, review, context, cleanup
        )
        active_run_id = str(context["active_run_id"])
        if apply_authorized is not True:
            return LaneRecoveryProgress(
                "awaiting-explicit-cleanup",
                review.target_digest,
                active_run_id=active_run_id,
                reason="cleanup already completed; explicit apply is required to reconcile its Fleet release",
            )
        if reconcile_cleanup is None:
            raise QualificationError(
                "completed cleanup resume requires a fresh Controller reconciliation callback"
            )
        persisted_snapshot = _fleet_view(
            persisted_receipt.get("fleet_snapshot"), target.fleet_node_ids
        )
        reconciliation = reconcile_cleanup(
            {
                **_envelope(target, review.target_digest),
                "request_key": review.request_key,
                "review_digest": review.review_digest,
                "review": dict(review.receipt),
                "resume_completed": True,
                "cleanup_mode": context["cleanup_mode"],
                "terminal_event": context["terminal_event"],
                "terminal_record_sha256": context["terminal_record"].get(
                    "record_sha256"
                ),
                "profile_number": review.profile_number,
                "profile_id": review.profile_id,
                "profile_digest": review.profile_digest,
                "plan_digest": review.plan_digest,
                "application_id": persisted_receipt.get("application_id"),
                "application_updated_at": persisted_receipt.get(
                    "application_updated_at"
                ),
                "stop_run_ids": _string_list(
                    review.receipt["stop_run_ids"], "reviewed stop run IDs"
                ),
                "stop_aliases": _string_list(
                    review.receipt["stop_aliases"], "reviewed stop aliases"
                ),
                "released_run_proofs": _object_list(
                    review.receipt["released_run_proofs"], "reviewed release proofs"
                ),
                "fleet_node_ids": list(target.fleet_node_ids),
                "persisted_receipt": dict(persisted_receipt),
            }
        )
        fresh_receipt = _validate_cleanup_application(
            target,
            review,
            context,
            reconciliation,
            expected_application_id=str(persisted_receipt["application_id"]),
            expected_application_updated_at=str(
                persisted_receipt["application_updated_at"]
            ),
        )
        if fresh_receipt.get("stop_receipts") != persisted_receipt.get("stop_receipts"):
            raise QualificationError(
                "cleanup resume changed the exact Controller stop receipts"
            )
        fresh_snapshot = _fleet_view(
            fresh_receipt.get("fleet_snapshot"), target.fleet_node_ids
        )
        if (
            fresh_snapshot["generated_at"] <= persisted_snapshot["generated_at"]
            or fresh_snapshot["event_cursor"] < persisted_snapshot["event_cursor"]
            or fresh_snapshot["authority_revision"]
            != persisted_snapshot["authority_revision"]
        ):
            raise QualificationError(
                "cleanup resume Fleet projection is not newer than its durable completion"
            )
        reconciliation_digest = _digest(dict(fresh_receipt))
        ledger.append(
            "lane_recovery.cleanup.reconciled",
            plan_digest=target.campaign_id,
            recipe=target.recipe_key,
            payload={
                **_envelope(target, review.target_digest),
                "request_key": review.request_key,
                "review_digest": review.review_digest,
                "cleanup_record_sha256": cleanup.get("record_sha256"),
                "application_id": fresh_receipt.get("application_id"),
                "application_updated_at": fresh_receipt.get("application_updated_at"),
                "receipt_sha256": reconciliation_digest,
                "receipt": dict(fresh_receipt),
            },
        )
        return LaneRecoveryProgress(
            "cleanup-completed",
            review.target_digest,
            active_run_id=active_run_id,
            receipt_sha256=reconciliation_digest,
        )

    target_digest = _digest(_target_binding(target))
    saved_review = _event(
        ledger, target, target_digest, "lane_recovery.cleanup.plan_reviewed"
    )
    resume_reviewed = saved_review is not None
    review = (
        _load_cleanup_review(target, ledger, target_digest, context)
        if resume_reviewed
        else review_lane_cleanup(target, ledger, prepare_cleanup=prepare_cleanup)
    )
    if apply_authorized is not True:
        return LaneRecoveryProgress(
            "awaiting-explicit-cleanup",
            review.target_digest,
            reason="the exact whole-Fleet cleanup is reviewed; rerun with explicit apply authorization",
        )
    reviewed_stop_ids = _string_list(
        review.receipt.get("stop_run_ids"), "reviewed cleanup stop run IDs"
    )
    active_run_id = str(context["active_run_id"])
    receipt = cleanup_to_idle(
        {
            **_envelope(target, review.target_digest),
            "request_key": review.request_key,
            "review_digest": review.review_digest,
            "review": dict(review.receipt),
            "resume_reviewed": resume_reviewed,
            "cleanup_mode": context["cleanup_mode"],
            "terminal_event": context["terminal_event"],
            "terminal_record_sha256": context["terminal_record"].get("record_sha256"),
            "profile_number": review.profile_number,
            "profile_id": review.profile_id,
            "profile_digest": review.profile_digest,
            "plan_digest": review.plan_digest,
            "stop_run_ids": reviewed_stop_ids,
            "stop_aliases": _string_list(
                review.receipt["stop_aliases"], "reviewed stop aliases"
            ),
            "released_run_proofs": _object_list(
                review.receipt["released_run_proofs"], "reviewed release proofs"
            ),
            "fleet_node_ids": list(target.fleet_node_ids),
        }
    )
    validated_receipt = _validate_cleanup_application(target, review, context, receipt)
    cleanup = ledger.append(
        "lane_recovery.cleanup.completed",
        plan_digest=target.campaign_id,
        recipe=target.recipe_key,
        payload={
            **_envelope(target, review.target_digest),
            "request_key": review.request_key,
            "review_digest": review.review_digest,
            "cleanup_mode": context["cleanup_mode"],
            "terminal_event": context["terminal_event"],
            "terminal_record_sha256": context["terminal_record"].get("record_sha256"),
            "receipt": dict(validated_receipt),
        },
    )
    receipt_body = _mapping(
        _payload(cleanup).get("receipt"), "completed cleanup receipt"
    )
    return LaneRecoveryProgress(
        "cleanup-completed",
        review.target_digest,
        active_run_id=active_run_id,
        receipt_sha256=_digest(dict(receipt_body)),
    )


def _validate_target(
    target: LaneRecoveryTarget, *, require_passing_lane: bool = True
) -> None:
    for label, value in (
        ("campaign ID", target.campaign_id),
        ("recipe content digest", target.recipe_content_sha256),
        ("package digest", target.package_sha256),
        ("profile digest", target.profile_digest),
        ("plan digest", target.plan_digest),
    ):
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            raise QualificationError(f"lane recovery {label} is invalid")
    for label, value in (
        ("batch ID", target.batch_id),
        ("recipe key", target.recipe_key),
        ("recipe revision ID", target.recipe_revision_id),
        ("route alias", target.alias),
        ("node ID", target.node_id),
        ("profile ID", target.profile_id),
    ):
        if not isinstance(value, str) or not value:
            raise QualificationError(f"lane recovery {label} is invalid")
    if type(target.lane_id) is not int or target.lane_id < 1:
        raise QualificationError("lane recovery lane ID must be a positive integer")
    if target.original_run_id is not None and (
        not isinstance(target.original_run_id, str) or not target.original_run_id
    ):
        raise QualificationError("lane recovery original run ID is invalid")
    if require_passing_lane and target.original_run_id is None:
        raise QualificationError(
            "lane recovery requires an exact passing canary run ID"
        )
    if type(target.profile_number) is not int or target.profile_number < 1:
        raise QualificationError("lane recovery profile number is invalid")
    if set(target.node_to_rank) != {target.node_id} or any(
        type(rank) is not int or rank < 0 for rank in target.node_to_rank.values()
    ):
        raise QualificationError("lane recovery target must bind its exact node/rank")
    if (
        not target.smoke_case_ids
        or any(
            not isinstance(case_id, str) or not case_id
            for case_id in target.smoke_case_ids
        )
        or len(set(target.smoke_case_ids)) != len(target.smoke_case_ids)
    ):
        raise QualificationError("lane recovery fixture case list is invalid")
    if (
        not target.fleet_node_ids
        or len(set(target.fleet_node_ids)) != len(target.fleet_node_ids)
        or target.node_id not in target.fleet_node_ids
    ):
        raise QualificationError("lane recovery whole-Fleet roster is invalid")
    if len(set(target.partner_run_ids)) != len(target.partner_run_ids) or len(
        set(target.partner_aliases)
    ) != len(target.partner_aliases):
        raise QualificationError("lane recovery partner identities are duplicated")
    if (
        target.original_run_id in target.partner_run_ids
        or target.alias in target.partner_aliases
    ):
        raise QualificationError("lane recovery lane overlaps a partner identity")
    lane_ids = [item.lane_id for item in target.canaries]
    if (
        not target.canaries
        or len(set(lane_ids)) != len(lane_ids)
        or any(type(lane_id) is not int or lane_id < 1 for lane_id in lane_ids)
    ):
        raise QualificationError("paired canary references are empty or duplicated")
    own = [item for item in target.canaries if item.lane_id == target.lane_id]
    if len(own) != 1:
        raise QualificationError("paired canaries lack this exact lane")
    own_ref = own[0]
    if (
        own_ref.recipe_key != target.recipe_key
        or own_ref.recipe_content_sha256 != target.recipe_content_sha256
        or own_ref.package_sha256 != target.package_sha256
        or own_ref.assigned_node_id != target.node_id
        or own_ref.assigned_rank != target.node_to_rank[target.node_id]
    ):
        raise QualificationError("this lane canary differs from the recovery target")
    if own_ref.run_id is not None and (
        own_ref.recipe_revision_id != target.recipe_revision_id
        or own_ref.alias != target.alias
    ):
        raise QualificationError(
            "this lane canary applied a different revision or route"
        )
    if require_passing_lane and (
        own_ref.outcome_event != "canary.completed"
        or own_ref.run_id != target.original_run_id
        or own_ref.recipe_revision_id != target.recipe_revision_id
        or own_ref.alias != target.alias
        or own_ref.node_to_rank is None
        or dict(own_ref.node_to_rank) != dict(target.node_to_rank)
    ):
        raise QualificationError("this lane canary differs from the recovery target")
    if not require_passing_lane and own_ref.run_id != target.original_run_id:
        raise QualificationError(
            "cleanup target run differs from its own canary outcome"
        )
    for reference in target.canaries:
        if reference.outcome_event not in {"canary.completed", "canary.failed"}:
            raise QualificationError("paired lane outcome event is not terminal")
        if (
            reference.assigned_node_id not in target.fleet_node_ids
            or type(reference.assigned_rank) is not int
            or reference.assigned_rank < 0
        ):
            raise QualificationError("paired lane Spark/rank assignment is invalid")
        if reference.outcome_event == "canary.completed" and any(
            value is None
            for value in (
                reference.run_id,
                reference.recipe_revision_id,
                reference.alias,
                reference.node_to_rank,
            )
        ):
            raise QualificationError(
                "passing paired canary lacks an exact run identity"
            )
        if reference.run_id is not None and any(
            value is None
            for value in (
                reference.recipe_revision_id,
                reference.alias,
                reference.node_to_rank,
            )
        ):
            raise QualificationError(
                "applied failed canary lacks an exact run identity"
            )
        if reference.node_to_rank is not None and dict(reference.node_to_rank) != {
            reference.assigned_node_id: reference.assigned_rank
        }:
            raise QualificationError(
                "paired canary rank receipt differs from its assignment"
            )
    assigned_nodes = [item.assigned_node_id for item in target.canaries]
    if len(set(assigned_nodes)) != len(assigned_nodes):
        raise QualificationError("paired lane assignments overlap the same Spark")
    if require_passing_lane and own_ref.outcome_event != "canary.completed":
        raise QualificationError("the recovering lane requires a passing canary")
    other_runs = {
        item.run_id
        for item in target.canaries
        if item.lane_id != target.lane_id and item.run_id is not None
    }
    other_aliases = {
        item.alias
        for item in target.canaries
        if item.lane_id != target.lane_id and item.alias is not None
    }
    if other_runs != set(target.partner_run_ids) or other_aliases != set(
        target.partner_aliases
    ):
        raise QualificationError(
            "partner canary references do not match stop identities"
        )


def _require_canary_records(target: LaneRecoveryTarget, ledger: EvidenceLedger) -> None:
    for reference in target.canaries:
        matches = [
            record
            for record in ledger.records
            if record.get("record_sha256") == reference.record_sha256
        ]
        if len(matches) != 1:
            raise QualificationError(
                "paired canary record reference is missing or ambiguous"
            )
        record = matches[0]
        payload = _payload(record)
        ranks = payload.get("node_to_rank")
        if (
            type(payload.get("lane_id")) is not int
            or payload.get("lane_id") != reference.lane_id
            or record.get("event") != reference.outcome_event
            or record.get("plan_digest") != target.campaign_id
            or record.get("recipe") != reference.recipe_key
            or payload.get("batch_id") != target.batch_id
            or payload.get("recipe_content_sha256") != reference.recipe_content_sha256
            or payload.get("package_sha256") != reference.package_sha256
            or payload.get("assigned_node_id") != reference.assigned_node_id
            or payload.get("assigned_rank") != reference.assigned_rank
            or payload.get("run_id") != reference.run_id
            or payload.get("recipe_revision_id") != reference.recipe_revision_id
            or payload.get("alias") != reference.alias
            or payload.get("node_to_rank")
            != (
                dict(reference.node_to_rank)
                if reference.node_to_rank is not None
                else None
            )
            or (
                reference.outcome_event == "canary.completed"
                and (
                    not isinstance(ranks, Mapping)
                    or dict(ranks) != dict(reference.node_to_rank or {})
                )
            )
        ):
            raise QualificationError(
                "paired canary reference is not exact recipe/lane evidence"
            )


def _validate_transition_review(
    target: LaneRecoveryTarget,
    request_key: str,
    receipt: Mapping[str, object],
    ledger: EvidenceLedger,
) -> Mapping[str, object]:
    expected_keys = {
        "request_key",
        "reviewed",
        "profile_number",
        "profile_id",
        "profile_digest",
        "plan_digest",
        "fleet_node_ids",
        "assignment",
        "keep_run_id",
        "stop_run_ids",
        "stop_aliases",
        "released_partner_proofs",
    }
    if set(receipt) != expected_keys:
        raise QualificationError("lane transition review has an unexpected shape")
    assignment = _mapping(receipt.get("assignment"), "reviewed lane assignment")
    if set(assignment) not in (
        {
            "recipe_key",
            "recipe_content_sha256",
            "package_sha256",
            "recipe_revision_id",
            "alias",
            "node_to_rank",
        },
        {
            "recipe_key",
            "recipe_content_sha256",
            "package_sha256",
            "recipe_revision_id",
            "alias",
            "node_to_rank",
            "run_id",
        },
    ):
        raise QualificationError("reviewed lane assignment has an unexpected shape")
    expected_keep = receipt.get("keep_run_id")
    if (expected_keep is not None and expected_keep != target.original_run_id) or (
        expected_keep is None and not target.allow_reactivation
    ):
        raise QualificationError(
            "reviewed transition does not preserve the exact lane run"
        )
    if receipt.get("request_key") != request_key or receipt.get("reviewed") is not True:
        raise QualificationError(
            "lane transition review lacks its exact request binding"
        )
    if (
        _positive_int(receipt.get("profile_number"), "reviewed profile number") < 1
        or not isinstance(receipt.get("profile_id"), str)
        or not receipt.get("profile_id")
        or not _is_sha256(receipt.get("profile_digest"))
        or not _is_sha256(receipt.get("plan_digest"))
    ):
        raise QualificationError(
            "lane transition review lacks an exact profile and plan identity"
        )
    if set(
        _string_list(receipt.get("fleet_node_ids"), "transition Fleet roster")
    ) != set(target.fleet_node_ids):
        raise QualificationError("lane transition changed the reviewed Fleet roster")
    if (
        assignment.get("recipe_key") != target.recipe_key
        or assignment.get("recipe_content_sha256") != target.recipe_content_sha256
        or assignment.get("package_sha256") != target.package_sha256
        or assignment.get("recipe_revision_id") != target.recipe_revision_id
        or assignment.get("alias") != target.alias
        or assignment.get("node_to_rank") != dict(target.node_to_rank)
    ):
        raise QualificationError(
            "reviewed profile assignment changed the exact recipe or target"
        )
    assignment_run_id = assignment.get("run_id")
    if assignment_run_id is not None and not isinstance(assignment_run_id, str):
        raise QualificationError("reviewed assignment run ID is invalid")
    if expected_keep == target.original_run_id:
        if assignment_run_id not in (None, target.original_run_id):
            raise QualificationError(
                "reviewed assignment replaced the preserved canary run"
            )
    elif assignment_run_id is not None:
        raise QualificationError(
            "reactivated lane review unexpectedly binds a prior run"
        )
    partner_assignments = _partner_assignments(target)
    stop_run_ids = _string_list(receipt.get("stop_run_ids"), "reviewed partner runs")
    stop_aliases = _string_list(receipt.get("stop_aliases"), "reviewed partner routes")
    if (
        stop_run_ids != sorted(set(stop_run_ids))
        or not set(stop_run_ids) <= set(partner_assignments)
        or stop_aliases != sorted(set(stop_aliases))
        or set(stop_aliases)
        != {
            _str(
                next(item.alias for item in target.canaries if item.run_id == run_id),
                "reviewed partner route",
            )
            for run_id in stop_run_ids
        }
    ):
        raise QualificationError(
            "reviewed transition changed its exact active partner stop set"
        )
    missing_partner_runs = sorted(set(partner_assignments) - set(stop_run_ids))
    raw_proofs = receipt.get("released_partner_proofs")
    proofs = _object_list(raw_proofs, "reviewed prior partner release proof")
    if [item.get("run_id") for item in proofs] != missing_partner_runs:
        raise QualificationError(
            "reviewed transition omits a prior proof for an absent partner"
        )
    references_by_run = {
        item.run_id: item
        for item in target.canaries
        if item.run_id is not None and item.lane_id != target.lane_id
    }
    for proof, run_id in zip(proofs, missing_partner_runs, strict=True):
        expected_proof = _prior_partner_release_proof(
            target, references_by_run[run_id], ledger
        )
        expected_proof.pop("source_fleet_snapshot")
        if dict(proof) != expected_proof:
            raise QualificationError(
                "reviewed transition prior release proof is misattributed"
            )
    return dict(receipt)


def _validate_cleanup_review(
    target: LaneRecoveryTarget,
    request_key: str,
    cleanup_mode: str,
    terminal_event: str,
    terminal_record_sha256: str,
    active_run_id: str,
    expected_stops: Mapping[str, Mapping[str, int]],
    expected_aliases: Sequence[str],
    receipt: Mapping[str, object],
    ledger: EvidenceLedger,
) -> Mapping[str, object]:
    expected_keys = {
        "request_key",
        "reviewed",
        "profile_number",
        "profile_id",
        "profile_digest",
        "plan_digest",
        "fleet_node_ids",
        "cleanup_mode",
        "terminal_event",
        "terminal_record_sha256",
        "active_run_id",
        "stop_run_ids",
        "stop_aliases",
        "released_run_proofs",
        "pre_cleanup_fleet_snapshot",
    }
    if set(receipt) != expected_keys:
        raise QualificationError("lane cleanup review has an unexpected shape")
    actual_stops = _string_list(receipt.get("stop_run_ids"), "cleanup stopped run IDs")
    actual_aliases = _string_list(
        receipt.get("stop_aliases"), "cleanup stopped aliases"
    )
    aliases_by_run: dict[str, str] = {}
    for run_id in actual_stops:
        reference = next(
            (item for item in target.canaries if item.run_id == run_id), None
        )
        if reference is not None and reference.alias is not None:
            aliases_by_run[run_id] = reference.alias
        elif run_id == active_run_id:
            aliases_by_run[run_id] = target.alias
        else:
            raise QualificationError(
                "cleanup stop names a run without exact route identity"
            )
    if (
        receipt.get("request_key") != request_key
        or receipt.get("reviewed") is not True
        or receipt.get("cleanup_mode") != cleanup_mode
        or receipt.get("terminal_event") != terminal_event
        or receipt.get("terminal_record_sha256") != terminal_record_sha256
        or receipt.get("active_run_id") != active_run_id
        or _positive_int(
            receipt.get("profile_number"), "reviewed cleanup profile number"
        )
        < 1
        or not isinstance(receipt.get("profile_id"), str)
        or not receipt.get("profile_id")
        or not _is_sha256(receipt.get("profile_digest"))
        or not _is_sha256(receipt.get("plan_digest"))
        or set(_string_list(receipt.get("fleet_node_ids"), "cleanup Fleet roster"))
        != set(target.fleet_node_ids)
        or actual_stops != sorted(set(actual_stops))
        or not set(actual_stops) <= set(expected_stops)
        or actual_aliases != sorted(set(actual_aliases))
        or set(actual_aliases) != set(aliases_by_run.values())
        or not set(actual_aliases) <= set(expected_aliases)
    ):
        raise QualificationError(
            "lane cleanup review changed its exact whole-Fleet effects"
        )
    missing_runs = sorted(set(expected_stops) - set(actual_stops))
    proofs = _object_list(
        receipt.get("released_run_proofs"), "reviewed prior cleanup release proof"
    )
    if [item.get("run_id") for item in proofs] != missing_runs:
        raise QualificationError(
            "cleanup review omits proof for a previously released run"
        )
    references_by_run = {
        item.run_id: item for item in target.canaries if item.run_id is not None
    }
    for proof, run_id in zip(proofs, missing_runs, strict=True):
        reference = references_by_run.get(run_id)
        if reference is None:
            raise QualificationError("cleanup prior release run is not a bound canary")
        expected_proof = _prior_partner_release_proof(target, reference, ledger)
        expected_proof.pop("source_fleet_snapshot")
        if dict(proof) != expected_proof:
            raise QualificationError("cleanup prior release proof is misattributed")
    view = _fleet_view(receipt.get("pre_cleanup_fleet_snapshot"), target.fleet_node_ids)
    _require_fresh_online_fleet(view)
    if set(view["loaded_runs"]) != set(actual_stops):
        raise QualificationError(
            "cleanup review stop set differs from fresh pre-cleanup Fleet"
        )
    references_by_run = {
        item.run_id: item for item in target.canaries if item.run_id is not None
    }
    for run_id in actual_stops:
        reference = references_by_run.get(run_id)
        expected_alias = aliases_by_run[run_id]
        if run_id == active_run_id:
            expected_revision = target.recipe_revision_id
        else:
            if reference is None:
                raise QualificationError("cleanup stop has no exact canary reference")
            expected_revision = _str(
                reference.recipe_revision_id, "cleanup canary revision"
            )
        expected_ranks = dict(expected_stops[run_id])
        projection = view["loaded_runs"][run_id]
        if (
            projection["alias"] != expected_alias
            or projection["recipe_revision_id"] != expected_revision
            or projection["ranks"] != expected_ranks
            or projection["route_states"] != {"published"}
        ):
            raise QualificationError(
                "cleanup pre-Fleet view changed an exact stop run or route identity"
            )
    for proof in proofs:
        run_id = _str(proof.get("run_id"), "released cleanup run ID")
        alias = _str(proof.get("alias"), "released cleanup route alias")
        source_generated_at = _timestamp(
            proof.get("source_fleet_generated_at"), "source cleanup Fleet time"
        )
        source_cursor = _cursor(
            proof.get("source_fleet_event_cursor"), "source cleanup Fleet cursor"
        )
        source_authority = _str(
            proof.get("source_fleet_authority_revision"),
            "source cleanup authority revision",
        )
        if (
            run_id in view["loaded_runs"]
            or alias in view["published_route_aliases"]
            or view["generated_at"] <= source_generated_at
            or view["event_cursor"] < source_cursor
            or view["authority_revision"] != source_authority
        ):
            raise QualificationError(
                "cleanup pre-Fleet view does not freshly confirm prior release"
            )
    return dict(receipt)


def _partner_assignments(
    target: LaneRecoveryTarget,
) -> dict[str, dict[str, int]]:
    return {
        reference.run_id: dict(
            reference.node_to_rank
            or {reference.assigned_node_id: reference.assigned_rank}
        )
        for reference in target.canaries
        if reference.lane_id != target.lane_id and reference.run_id is not None
    }


def prior_partner_release_proofs(
    target: LaneRecoveryTarget,
    ledger: EvidenceLedger,
    fleet_snapshot: Mapping[str, object],
    *,
    candidate_run_ids: Sequence[str] | None = None,
) -> tuple[dict[str, object], ...]:
    """Bind absent paired runs to earlier reviewed stops and a fresh Fleet view.

    The returned records are suitable for embedding in a transition or cleanup
    review. A missing run without a durable typed stop receipt remains a hard
    blocker; the current snapshot must also be newer than the source release.
    """

    _validate_target(target, require_passing_lane=False)
    _require_canary_records(target, ledger)
    all_canaries = {
        reference.run_id: dict(
            reference.node_to_rank
            or {reference.assigned_node_id: reference.assigned_rank}
        )
        for reference in target.canaries
        if reference.run_id is not None
    }
    all_partners = _partner_assignments(target)
    candidates = (
        sorted(all_partners) if candidate_run_ids is None else sorted(candidate_run_ids)
    )
    current = _fleet_view(fleet_snapshot, target.fleet_node_ids)
    _require_fresh_online_fleet(current)
    if len(candidates) != len(set(candidates)) or not set(candidates) <= (
        set(all_canaries) | set(current["loaded_runs"])
    ):
        raise QualificationError(
            "prior release proof request changed paired run identities"
        )
    proofs: list[dict[str, object]] = []
    reference_by_run = {
        reference.run_id: reference
        for reference in target.canaries
        if reference.run_id is not None
    }
    for run_id in candidates:
        if run_id in current["loaded_runs"]:
            continue
        reference = reference_by_run.get(run_id)
        if reference is None:
            raise QualificationError(
                "absent prior release run has no bound canary identity"
            )
        if reference.alias in current["published_route_aliases"]:
            raise QualificationError("absent partner run still has its published route")
        proof = _prior_partner_release_proof(target, reference, ledger)
        source_snapshot = _fleet_view(
            proof["source_fleet_snapshot"], target.fleet_node_ids
        )
        if (
            run_id in source_snapshot["loaded_runs"]
            or reference.alias in source_snapshot["published_route_aliases"]
            or current["generated_at"] <= source_snapshot["generated_at"]
            or current["event_cursor"] < source_snapshot["event_cursor"]
            or current["authority_revision"] != source_snapshot["authority_revision"]
        ):
            raise QualificationError(
                "absent partner lacks a fresh Fleet observation after its exact release"
            )
        proofs.append(
            {
                key: value
                for key, value in proof.items()
                if key != "source_fleet_snapshot"
            }
        )
    return tuple(proofs)


def _prior_partner_release_proof(
    target: LaneRecoveryTarget,
    reference: CanaryReference,
    ledger: EvidenceLedger,
) -> dict[str, object]:
    """Return the latest exact earlier transition/cleanup stop for one canary."""

    if (
        reference.run_id is None
        or reference.alias is None
        or reference.node_to_rank is None
    ):
        raise QualificationError(
            "prior partner release requires an exact applied canary"
        )
    by_lane = {item.lane_id: item for item in target.canaries}
    for source_record in reversed(ledger.records):
        event = source_record.get("event")
        if event not in {
            "lane_recovery.transitioned",
            "lane_recovery.cleanup.completed",
        }:
            continue
        if source_record.get("plan_digest") != target.campaign_id:
            continue
        source_payload = _payload(source_record)
        if source_payload.get("batch_id") != target.batch_id:
            continue
        source_lane = source_payload.get("lane_id")
        source_ref = by_lane.get(source_lane) if type(source_lane) is int else None
        if source_ref is None or source_record.get("recipe") != source_ref.recipe_key:
            continue
        source_target_digest = source_payload.get("target_digest")
        if not isinstance(source_target_digest, str):
            continue
        review_event_name = (
            "lane_recovery.plan_reviewed"
            if event == "lane_recovery.transitioned"
            else "lane_recovery.cleanup.plan_reviewed"
        )
        review_records = [
            item
            for item in ledger.records
            if item.get("plan_digest") == target.campaign_id
            and item.get("recipe") == source_ref.recipe_key
            and item.get("event") == review_event_name
            and _payload(item).get("batch_id") == target.batch_id
            and _payload(item).get("lane_id") == source_lane
            and _payload(item).get("target_digest") == source_target_digest
        ]
        if len(review_records) != 1:
            continue
        review_payload = _payload(review_records[0])
        review = _mapping(review_payload.get("review"), "prior stop plan review")
        review_digest = _digest(dict(review))
        if (
            source_payload.get("review_digest") != review_digest
            or review_payload.get("review_digest") != review_digest
            or source_payload.get("request_key") != review_payload.get("request_key")
            or review_payload.get("request_key") != review.get("request_key")
        ):
            continue
        stop_ids = _string_list(
            review.get("stop_run_ids"), "prior reviewed stop run IDs"
        )
        if reference.run_id not in stop_ids:
            continue
        stop_aliases = _string_list(
            review.get("stop_aliases"), "prior reviewed stop aliases"
        )
        if reference.alias not in stop_aliases:
            raise QualificationError(
                "prior reviewed stop omitted the exact partner route"
            )
        receipt = _mapping(
            source_payload.get("receipt"), "prior stop application receipt"
        )
        if (
            receipt.get("request_key") != review.get("request_key")
            or receipt.get("review_digest") != review_digest
            or receipt.get("application_state") != "succeeded"
            or receipt.get("profile_number") != review.get("profile_number")
            or receipt.get("profile_id") != review.get("profile_id")
            or receipt.get("profile_digest") != review.get("profile_digest")
            or receipt.get("plan_digest") != review.get("plan_digest")
            or not isinstance(receipt.get("application_id"), str)
            or not receipt.get("application_id")
        ):
            raise QualificationError(
                "prior stop application differs from its reviewed plan"
            )
        source_stop_ids = sorted(set(stop_ids))
        if source_stop_ids != sorted(stop_ids) or len(stop_ids) != len(set(stop_ids)):
            raise QualificationError("prior reviewed stop identities are not canonical")
        canary_by_run = {
            item.run_id: item for item in target.canaries if item.run_id is not None
        }
        expected_assignments = {
            run: dict(canary_by_run[run].node_to_rank or {})
            for run in stop_ids
            if run in canary_by_run
        }
        if set(expected_assignments) != set(stop_ids):
            raise QualificationError("prior stop review names an unbound paired run")
        stop_receipt_key = (
            "partner_stop_receipts"
            if event == "lane_recovery.transitioned"
            else "stop_receipts"
        )
        raw_stop_receipts = receipt.get(stop_receipt_key)
        _validate_stopped_runs(target, expected_assignments, raw_stop_receipts)
        matching_stop = next(
            item
            for item in _object_list(raw_stop_receipts, "prior typed stop receipt")
            if item.get("run_id") == reference.run_id
        )
        fleet_value = receipt.get("fleet_snapshot")
        source_fleet = _fleet_view(fleet_value, target.fleet_node_ids)
        _require_fresh_online_fleet(source_fleet)
        application_updated_at = _timestamp(
            receipt.get("application_updated_at"), "prior stop application update time"
        )
        if (
            source_fleet["generated_at"] < application_updated_at
            or reference.run_id in source_fleet["loaded_runs"]
            or reference.alias in source_fleet["published_route_aliases"]
        ):
            raise QualificationError("prior stop lacks fresh whole-Fleet release proof")
        return {
            "run_id": reference.run_id,
            "recipe_key": reference.recipe_key,
            "recipe_content_sha256": reference.recipe_content_sha256,
            "package_sha256": reference.package_sha256,
            "lane_id": reference.lane_id,
            "canary_record_sha256": reference.record_sha256,
            "assigned_node_id": reference.assigned_node_id,
            "assigned_rank": reference.assigned_rank,
            "alias": reference.alias,
            "node_to_rank": dict(reference.node_to_rank),
            "source_event": str(event),
            "source_lane_id": source_lane,
            "source_record_sha256": _str(
                source_record.get("record_sha256"), "prior stop ledger record digest"
            ),
            "source_application_id": receipt.get("application_id"),
            "source_application_updated_at": receipt.get("application_updated_at"),
            "source_stop_receipt_sha256": _digest(dict(matching_stop)),
            "source_fleet_snapshot_sha256": _digest(
                dict(_mapping(fleet_value, "prior stop FleetSnapshot"))
            ),
            "source_fleet_generated_at": source_fleet["generated_at"].isoformat(),
            "source_fleet_event_cursor": source_fleet["event_cursor"],
            "source_fleet_authority_revision": source_fleet["authority_revision"],
            "source_fleet_snapshot": dict(
                _mapping(fleet_value, "prior stop FleetSnapshot")
            ),
        }
    raise QualificationError(
        "absent partner canary has no exact same-batch typed stop and release receipt"
    )


def _cleanup_context(
    target: LaneRecoveryTarget,
    ledger: EvidenceLedger,
    target_digest: str,
) -> _CleanupContext:
    completed = _event(ledger, target, target_digest, "lane_recovery.completed")
    own_reference = next(
        reference
        for reference in target.canaries
        if reference.lane_id == target.lane_id
    )
    for reference in target.canaries:
        if reference.outcome_event == "canary.failed" and reference.run_id is None:
            raise QualificationError(
                "failed canary lacks its applied run identity; reconcile before cleanup"
            )
    if completed is not None:
        if own_reference.outcome_event != "canary.completed":
            raise QualificationError(
                "recovery completion is not tied to a passing own canary"
            )
        payload = _payload(completed)
        active_run_id = _str(payload.get("active_run_id"), "completed lane run ID")
        active_assignment = _mapping(
            payload.get("active_assignment"), "completed lane assignment"
        )
        _validate_active_identity(target, active_assignment)
        if active_assignment.get("run_id") != active_run_id:
            raise QualificationError(
                "completed lane assignment changed its run identity"
            )
        terminal_record: Mapping[str, object] = completed
        terminal_event = "lane_recovery.completed"
        cleanup_mode = "recovered-lane"
        recovery_profile = {
            "recovery_profile_number": payload.get("recovery_profile_number"),
            "recovery_profile_id": payload.get("recovery_profile_id"),
            "recovery_profile_digest": payload.get("recovery_profile_digest"),
            "recovery_plan_digest": payload.get("recovery_plan_digest"),
        }
    else:
        if own_reference.outcome_event != "canary.failed":
            raise QualificationError(
                "failed-lane cleanup requires this lane's durable canary.failed outcome"
            )
        active_run_id = _str(own_reference.run_id, "failed canary applied run ID")
        terminal_event = "canary.failed"
        cleanup_mode = "failed-canary-lane"
        terminal_matches = [
            record
            for record in ledger.records
            if record.get("record_sha256") == own_reference.record_sha256
        ]
        if len(terminal_matches) != 1:
            raise QualificationError(
                "failed canary terminal record is absent or ambiguous"
            )
        terminal_record = terminal_matches[0]
        terminal_payload = _payload(terminal_record)
        if (
            terminal_record.get("event") != "canary.failed"
            or terminal_record.get("plan_digest") != target.campaign_id
            or terminal_record.get("recipe") != target.recipe_key
            or terminal_payload.get("run_id") != active_run_id
        ):
            raise QualificationError(
                "failed canary cleanup terminal attribution is invalid"
            )
        recovery_profile = {
            "recovery_profile_number": None,
            "recovery_profile_id": None,
            "recovery_profile_digest": None,
            "recovery_plan_digest": None,
        }
    stop_assignments: dict[str, dict[str, int]] = {
        **_partner_assignments(target),
        active_run_id: dict(target.node_to_rank),
    }
    stop_aliases = sorted(
        {
            _str(reference.alias, "applied canary route alias")
            for reference in target.canaries
            if reference.run_id is not None
        }
    )
    if target.alias not in stop_aliases:
        raise QualificationError("cleanup release set omits the recovering lane route")
    terminal_sha256 = _str(
        terminal_record.get("record_sha256"), "cleanup terminal record SHA-256"
    )
    if not _is_sha256(terminal_sha256):
        raise QualificationError("cleanup terminal record digest is invalid")
    return {
        "cleanup_mode": cleanup_mode,
        "terminal_event": terminal_event,
        "terminal_record": terminal_record,
        "active_run_id": active_run_id,
        "active_assignment": (
            dict(active_assignment)
            if completed is not None
            else {
                "run_id": active_run_id,
                "recipe_key": target.recipe_key,
                "recipe_content_sha256": target.recipe_content_sha256,
                "package_sha256": target.package_sha256,
                "recipe_revision_id": target.recipe_revision_id,
                "alias": target.alias,
                "node_to_rank": dict(target.node_to_rank),
            }
        ),
        "stop_assignments": stop_assignments,
        "stop_aliases": stop_aliases,
        "recovery_profile_number": recovery_profile["recovery_profile_number"],
        "recovery_profile_id": recovery_profile["recovery_profile_id"],
        "recovery_profile_digest": recovery_profile["recovery_profile_digest"],
        "recovery_plan_digest": recovery_profile["recovery_plan_digest"],
    }


def _load_cleanup_review(
    target: LaneRecoveryTarget,
    ledger: EvidenceLedger,
    target_digest: str,
    context: _CleanupContext,
) -> LaneCleanupReview:
    terminal_sha256 = _str(
        context["terminal_record"].get("record_sha256"),
        "cleanup terminal record SHA-256",
    )
    request_key = _request_key("cleanup", target, target_digest)
    expected_intent = {
        **_envelope(target, target_digest),
        "request_key": request_key,
        "cleanup_mode": context["cleanup_mode"],
        "terminal_event": context["terminal_event"],
        "terminal_record_sha256": terminal_sha256,
        "active_run_id": context["active_run_id"],
        "stop_run_ids": sorted(context["stop_assignments"]),
        "stop_aliases": context["stop_aliases"],
    }
    intent = _require_event(
        ledger, target, target_digest, "lane_recovery.cleanup.intent"
    )
    if _payload(intent) != expected_intent:
        raise QualificationError(
            "durable cleanup intent changed its exact release identities"
        )
    reviewed = _require_event(
        ledger, target, target_digest, "lane_recovery.cleanup.plan_reviewed"
    )
    reviewed_payload = _payload(reviewed)
    receipt = _mapping(reviewed_payload.get("review"), "durable cleanup review")
    review_digest = _digest(dict(receipt))
    if (
        set(reviewed_payload)
        != set(_envelope(target, target_digest))
        | {"request_key", "review_digest", "review"}
        or reviewed_payload.get("request_key") != request_key
        or reviewed_payload.get("review_digest") != review_digest
    ):
        raise QualificationError(
            "durable cleanup review digest or attribution is invalid"
        )
    valid = _validate_cleanup_review(
        target,
        request_key,
        str(context["cleanup_mode"]),
        str(context["terminal_event"]),
        terminal_sha256,
        str(context["active_run_id"]),
        context["stop_assignments"],
        context["stop_aliases"],
        receipt,
        ledger,
    )
    return LaneCleanupReview(
        target_digest=target_digest,
        request_key=request_key,
        review_digest=review_digest,
        profile_number=_positive_int(
            valid.get("profile_number"), "durable cleanup profile number"
        ),
        profile_id=str(valid["profile_id"]),
        profile_digest=str(valid["profile_digest"]),
        plan_digest=str(valid["plan_digest"]),
        receipt=valid,
    )


def _validate_cleanup_application(
    target: LaneRecoveryTarget,
    review: LaneCleanupReview,
    context: _CleanupContext,
    value: Mapping[str, object],
    *,
    expected_application_id: str | None = None,
    expected_application_updated_at: str | None = None,
) -> dict[str, object]:
    receipt = _mapping(value, "cleanup application receipt")
    application_id = receipt.get("application_id")
    application_updated_at = receipt.get("application_updated_at")
    if (
        receipt.get("request_key") != review.request_key
        or receipt.get("review_digest") != review.review_digest
        or receipt.get("application_state") != "succeeded"
        or receipt.get("profile_number") != review.profile_number
        or receipt.get("profile_id") != review.profile_id
        or receipt.get("profile_digest") != review.profile_digest
        or receipt.get("plan_digest") != review.plan_digest
        or not isinstance(application_id, str)
        or not application_id
        or (
            expected_application_id is not None
            and application_id != expected_application_id
        )
        or not isinstance(application_updated_at, str)
        or (
            expected_application_updated_at is not None
            and application_updated_at != expected_application_updated_at
        )
    ):
        raise QualificationError(
            "lane cleanup application differs from its reviewed plan"
        )
    _timestamp(application_updated_at, "completed cleanup application time")
    stop_run_ids = _string_list(
        review.receipt.get("stop_run_ids"), "reviewed cleanup stopped run IDs"
    )
    stop_assignments = {
        run_id: context["stop_assignments"][run_id] for run_id in stop_run_ids
    }
    _validate_stopped_runs(target, stop_assignments, receipt.get("stop_receipts"))
    if receipt.get("released_run_proofs") != review.receipt.get("released_run_proofs"):
        raise QualificationError("cleanup application changed its prior release proofs")
    fleet = _fleet_view(receipt.get("fleet_snapshot"), target.fleet_node_ids)
    pre_cleanup = _fleet_view(
        review.receipt.get("pre_cleanup_fleet_snapshot"), target.fleet_node_ids
    )
    _require_fresh_online_fleet(fleet)
    _require_fresh_online_fleet(pre_cleanup)
    if fleet["generated_at"] < _timestamp(
        application_updated_at, "completed cleanup application time"
    ):
        raise QualificationError(
            "cleanup Fleet projection predates completed application"
        )
    if (
        fleet["generated_at"] <= pre_cleanup["generated_at"]
        or fleet["event_cursor"] < pre_cleanup["event_cursor"]
        or fleet["authority_revision"] != pre_cleanup["authority_revision"]
    ):
        raise QualificationError(
            "cleanup Fleet projection is not fresh after its reviewed pre-state"
        )
    if fleet.get("loaded_runs") or fleet.get("published_route_aliases"):
        raise QualificationError(
            "cleanup FleetSnapshot still contains a workload or route"
        )
    return dict(receipt)


def _validate_cleanup_completion(
    target: LaneRecoveryTarget,
    review: LaneCleanupReview,
    context: _CleanupContext,
    record: Mapping[str, object],
) -> dict[str, object]:
    payload = _payload(record)
    expected_keys = set(_envelope(target, review.target_digest)) | {
        "request_key",
        "review_digest",
        "cleanup_mode",
        "terminal_event",
        "terminal_record_sha256",
        "receipt",
    }
    if (
        set(payload) != expected_keys
        or payload.get("request_key") != review.request_key
        or payload.get("review_digest") != review.review_digest
        or payload.get("cleanup_mode") != context["cleanup_mode"]
        or payload.get("terminal_event") != context["terminal_event"]
        or payload.get("terminal_record_sha256")
        != context["terminal_record"].get("record_sha256")
    ):
        raise QualificationError(
            "completed cleanup differs from its durable reviewed intent"
        )
    return _validate_cleanup_application(
        target,
        review,
        context,
        _mapping(payload.get("receipt"), "persisted cleanup receipt"),
    )


def _validate_transition(
    target: LaneRecoveryTarget,
    review: LaneTransitionReview,
    receipt: Mapping[str, object],
) -> dict[str, object]:
    if (
        receipt.get("request_key") != review.request_key
        or receipt.get("review_digest") != review.review_digest
        or receipt.get("application_state") != "succeeded"
        or receipt.get("profile_number") != review.profile_number
        or receipt.get("profile_id") != review.profile_id
        or receipt.get("profile_digest") != review.profile_digest
        or receipt.get("plan_digest") != review.plan_digest
    ):
        raise QualificationError(
            "lane transition differs from its reviewed profile plan"
        )
    if not isinstance(receipt.get("application_id"), str) or not receipt.get(
        "application_id"
    ):
        raise QualificationError("lane transition lacks a durable application identity")
    active = _mapping(receipt.get("active"), "active lane receipt")
    _validate_active_identity(target, active)
    run_id = _str(active.get("run_id"), "active lane run ID")
    reactivated = active.get("reactivated") is True
    if review.receipt.get("keep_run_id") == target.original_run_id:
        if run_id != target.original_run_id or reactivated:
            raise QualificationError(
                "lane transition replaced the exact retained canary run"
            )
    elif run_id == target.original_run_id or not (
        target.allow_reactivation and reactivated
    ):
        raise QualificationError(
            "lane transition replaced the exact canary run without authority"
        )
    if reactivated:
        smoke = _mapping(active.get("reactivation_smoke"), "lane reactivation smoke")
        _validate_smoke_receipt(
            target,
            active,
            _active_boot_id(target, receipt),
            smoke,
            application_id=str(receipt["application_id"]),
            require_record_sha256=True,
        )
    elif run_id != target.original_run_id:
        raise QualificationError("lane run identity is invalid")
    stop_receipts = receipt.get("partner_stop_receipts")
    reviewed_stop_ids = _string_list(
        review.receipt.get("stop_run_ids"), "reviewed transition stop run IDs"
    )
    all_partner_assignments = _partner_assignments(target)
    partner_assignments = {
        run_id: all_partner_assignments[run_id] for run_id in reviewed_stop_ids
    }
    _validate_stopped_runs(target, partner_assignments, stop_receipts)
    pre = _fleet_view(
        receipt.get("pre_transition_fleet_snapshot"), target.fleet_node_ids
    )
    post = _fleet_view(receipt.get("fleet_snapshot"), target.fleet_node_ids)
    _require_fresh_online_fleet(pre)
    _require_fresh_online_fleet(post)
    keep_run_id = review.receipt.get("keep_run_id")
    expected_pre_runs = set(reviewed_stop_ids)
    if isinstance(keep_run_id, str):
        expected_pre_runs.add(keep_run_id)
    if set(pre["loaded_runs"]) != expected_pre_runs:
        raise QualificationError(
            "transition pre-Fleet view changed its exact active and released runs"
        )
    raw_released = _object_list(
        review.receipt.get("released_partner_proofs"),
        "reviewed prior partner release proof",
    )
    for proof in raw_released:
        run_id = _str(proof.get("run_id"), "released partner run ID")
        alias = _str(proof.get("alias"), "released partner route alias")
        source_generated_at = _timestamp(
            proof.get("source_fleet_generated_at"), "source release Fleet time"
        )
        source_cursor = _cursor(
            proof.get("source_fleet_event_cursor"), "source release Fleet cursor"
        )
        source_authority = _str(
            proof.get("source_fleet_authority_revision"),
            "source release authority revision",
        )
        if (
            run_id in pre["loaded_runs"]
            or alias in pre["published_route_aliases"]
            or pre["generated_at"] <= source_generated_at
            or pre["event_cursor"] < source_cursor
            or pre["authority_revision"] != source_authority
        ):
            raise QualificationError(
                "transition pre-Fleet view does not freshly confirm prior partner release"
            )
    application_updated_at = _timestamp(
        receipt.get("application_updated_at"), "completed transition application time"
    )
    if (
        post["generated_at"] < pre["generated_at"]
        or post["event_cursor"] < pre["event_cursor"]
        or post["authority_revision"] != pre["authority_revision"]
    ):
        raise QualificationError(
            "transition Fleet projection predates its pre-transition view"
        )
    if post["generated_at"] < application_updated_at:
        raise QualificationError(
            "transition Fleet projection predates completed application"
        )
    _validate_lane_projection(target, active, post, require_serving=True)
    return dict(active)


def _validate_active_identity(
    target: LaneRecoveryTarget, active: Mapping[str, object]
) -> None:
    if (
        active.get("recipe_key") != target.recipe_key
        or active.get("recipe_content_sha256") != target.recipe_content_sha256
        or active.get("package_sha256") != target.package_sha256
        or active.get("recipe_revision_id") != target.recipe_revision_id
        or active.get("alias") != target.alias
        or active.get("node_to_rank") != dict(target.node_to_rank)
        or not isinstance(active.get("run_id"), str)
        or not active.get("run_id")
    ):
        raise QualificationError(
            "active lane run changed recipe, node, rank, or route identity"
        )


def _validate_observation(
    target: LaneRecoveryTarget,
    active: Mapping[str, object],
    review: LaneTransitionReview,
    observation: Mapping[str, object],
) -> Mapping[str, object]:
    if (
        observation.get("snapshot_freshness") != "live"
        or observation.get("profile_number") != review.profile_number
        or observation.get("profile_id") != review.profile_id
        or observation.get("profile_digest") != review.profile_digest
        or observation.get("plan_digest") != review.plan_digest
    ):
        raise QualificationError("Fleet observation changed the intended lane profile")
    view = _fleet_view(observation.get("fleet_snapshot"), target.fleet_node_ids)
    _validate_lane_projection(target, active, view, require_serving=False)
    target_node = _mapping(view["nodes"].get(target.node_id), "target Fleet node")
    loaded = target_node["loaded_run_ids"]
    routes = target_node["published_route_aliases"]
    active_run_id = _str(active.get("run_id"), "active run ID")
    online = target_node["online_state"]
    serving_ready = (
        online == "online"
        and loaded == {active_run_id}
        and routes == {target.alias}
        and target_node.get("telemetry_freshness") == "live"
        and target_node.get("boot_id") is not None
    )
    return {
        **target_node,
        "serving_ready": serving_ready,
        "generated_at": view["generated_at"].isoformat(),
        "event_cursor": view["event_cursor"],
        "authority_revision": view["authority_revision"],
    }


def _fleet_view(value: object, expected_node_ids: Sequence[str]) -> _FleetView:
    """Validate and normalize one serialized Controller FleetSnapshot."""

    snapshot = _mapping(value, "serialized FleetSnapshot")
    if snapshot.get("schema_version") != 1:
        raise QualificationError("FleetSnapshot schema version is invalid")
    event_cursor = _cursor(snapshot.get("event_cursor"), "FleetSnapshot event cursor")
    generated_at = _timestamp(
        snapshot.get("generated_at"), "FleetSnapshot generated_at"
    )
    authority_revision = _str(
        snapshot.get("authority_revision"), "FleetSnapshot authority revision"
    )
    raw_nodes = snapshot.get("nodes")
    if not isinstance(raw_nodes, list):
        raise QualificationError("FleetSnapshot nodes are invalid")
    nodes: dict[str, _FleetNode] = {}
    all_loaded: dict[str, _LoadedProjection] = {}
    all_published_aliases: set[str] = set()
    for raw_node in raw_nodes:
        node = _mapping(raw_node, "FleetSnapshot node")
        node_id = _str(node.get("id"), "FleetSnapshot node ID")
        if node_id in nodes:
            raise QualificationError("FleetSnapshot contains duplicate node IDs")
        connection = _mapping(node.get("connection"), "Fleet node connection")
        online_state = connection.get("online_state")
        if online_state not in {"online", "offline", "unregistered"}:
            raise QualificationError("Fleet node online state is invalid")
        online_state = _str(online_state, "Fleet node online state")
        raw_telemetry = node.get("telemetry")
        telemetry: Mapping[str, object] | None
        if raw_telemetry is None:
            telemetry = None
        else:
            telemetry = _mapping(raw_telemetry, "Fleet node telemetry")
        sample: Mapping[str, object] = {}
        freshness: object = None
        if telemetry is not None:
            freshness = telemetry.get("freshness")
            sample_value = telemetry.get("sample")
            if sample_value is not None:
                sample = _mapping(sample_value, "Fleet telemetry sample")
        raw_loaded = node.get("loaded")
        if not isinstance(raw_loaded, list):
            raise QualificationError("Fleet node loaded runs are invalid")
        loaded_run_ids: set[str] = set()
        published_aliases: set[str] = set()
        for raw_presence in raw_loaded:
            presence = _mapping(raw_presence, "Fleet loaded run")
            run_id = _str(presence.get("run_id"), "Fleet loaded run ID")
            alias = _str(presence.get("alias"), "Fleet loaded run alias")
            revision = _str(
                presence.get("recipe_revision_id"), "Fleet loaded recipe revision"
            )
            rank = presence.get("rank")
            if type(rank) is not int or rank < 0:
                raise QualificationError("Fleet loaded run rank is invalid")
            route_state = presence.get("route_state")
            if not isinstance(route_state, str) or not route_state:
                raise QualificationError("Fleet loaded route state is invalid")
            if run_id in loaded_run_ids:
                raise QualificationError("Fleet node repeats a loaded run")
            loaded_run_ids.add(run_id)
            prior = all_loaded.get(run_id)
            if prior is None:
                all_loaded[run_id] = {
                    "alias": alias,
                    "recipe_revision_id": revision,
                    "ranks": {node_id: rank},
                    "route_states": {route_state},
                }
            else:
                if (
                    prior["alias"] != alias
                    or prior["recipe_revision_id"] != revision
                    or node_id in prior["ranks"]
                ):
                    raise QualificationError("Fleet run rank presence changed identity")
                ranks = dict(prior["ranks"])
                ranks[node_id] = rank
                states = set(prior["route_states"])
                states.add(route_state)
                all_loaded[run_id] = {
                    "alias": alias,
                    "recipe_revision_id": revision,
                    "ranks": ranks,
                    "route_states": states,
                }
            if route_state == "published":
                published_aliases.add(alias)
                all_published_aliases.add(alias)
        reservations = _mapping(node.get("reservations"), "Fleet node reservations")
        expected_reservations = {
            "disk_bytes",
            "unified_memory_bytes",
            "host_memory_bytes",
            "gpu_memory_bytes",
            "port_count",
        }
        if set(reservations) != expected_reservations:
            raise QualificationError(
                "Fleet reservation snapshot is incomplete or invalid"
            )
        reservation_values: dict[str, int] = {}
        for key in expected_reservations:
            reservation_values[key] = _nonnegative_int(
                reservations[key], f"Fleet reservation {key}"
            )
        nodes[node_id] = {
            "online_state": online_state,
            "telemetry_freshness": freshness,
            "boot_id": sample.get("boot_id"),
            "telemetry_observed_at": sample.get("observed_at"),
            "loaded_run_ids": loaded_run_ids,
            "published_route_aliases": published_aliases,
            "reservations": reservation_values,
        }
    if set(nodes) != set(expected_node_ids):
        raise QualificationError("FleetSnapshot changed the locked whole-Fleet roster")
    return {
        "event_cursor": event_cursor,
        "generated_at": generated_at,
        "authority_revision": authority_revision,
        "nodes": nodes,
        "loaded_runs": all_loaded,
        "published_route_aliases": all_published_aliases,
    }


def _require_fresh_online_fleet(view: _FleetView) -> None:
    """Require current live telemetry before treating Fleet absence as proof."""

    for node_id, node in view["nodes"].items():
        if (
            node["online_state"] != "online"
            or node["telemetry_freshness"] != "live"
            or not isinstance(node["boot_id"], str)
            or not node["boot_id"]
        ):
            raise QualificationError(
                "fresh Fleet observation requires every locked Fleet node to be online with live telemetry"
            )
        observed_at = _timestamp(
            node["telemetry_observed_at"],
            f"Fleet telemetry observed_at for {node_id}",
        )
        if observed_at > view["generated_at"]:
            raise QualificationError(
                "fresh Fleet observation contains telemetry newer than its snapshot"
            )


def _validate_lane_projection(
    target: LaneRecoveryTarget,
    active: Mapping[str, object],
    view: _FleetView,
    *,
    require_serving: bool,
) -> None:
    run_id = _str(active.get("run_id"), "active lane run ID")
    loaded_runs = view["loaded_runs"]
    permitted_runs = {run_id, *target.partner_run_ids}
    if target.original_run_id is not None:
        permitted_runs.add(target.original_run_id)
    unexpected = set(loaded_runs) - permitted_runs
    if unexpected:
        raise QualificationError("whole-Fleet snapshot contains a foreign workload")
    for partner_run in target.partner_run_ids:
        if partner_run in loaded_runs:
            raise QualificationError("partner workload remains in the Fleet projection")
    candidate_runs = [run_id]
    if target.original_run_id is not None:
        candidate_runs.append(target.original_run_id)
    for candidate_run in candidate_runs:
        presence = loaded_runs.get(candidate_run)
        if presence is not None and candidate_run != run_id:
            raise QualificationError(
                "a replaced canary run remains loaded in the Fleet"
            )
        if presence is not None:
            expected_alias = target.alias
            expected_revision = target.recipe_revision_id
            expected_node_to_rank = dict(target.node_to_rank)
            if (
                presence.get("alias") != expected_alias
                or presence.get("recipe_revision_id") != expected_revision
                or dict(presence.get("ranks", {})) != expected_node_to_rank
            ):
                raise QualificationError(
                    "Fleet run projection changed the exact lane identity"
                )
    aliases = view["published_route_aliases"]
    if aliases - {target.alias}:
        raise QualificationError(
            "whole-Fleet snapshot contains a foreign published route"
        )
    target_node = view["nodes"][target.node_id]
    if require_serving and (
        set(loaded_runs) != {run_id}
        or aliases != {target.alias}
        or target_node.get("online_state") != "online"
        or target_node.get("telemetry_freshness") != "live"
        or target_node.get("boot_id") is None
    ):
        raise QualificationError(
            "post-transition Fleet projection does not serve the exact lane"
        )


def _validate_stopped_runs(
    target: LaneRecoveryTarget,
    expected_assignments: Mapping[str, Mapping[str, int]],
    value: object,
) -> None:
    receipts = value
    if not isinstance(receipts, list):
        raise QualificationError("typed final stop receipts are absent")
    by_run: dict[str, Mapping[str, object]] = {}
    for raw_receipt in receipts:
        receipt = _mapping(raw_receipt, "typed run stop receipt")
        run_id = _str(receipt.get("run_id"), "stopped run ID")
        if run_id in by_run:
            raise QualificationError("duplicate final stop receipt for a run")
        if receipt.get("operation_state") != "succeeded":
            raise QualificationError(
                "run stop operation has no successful terminal receipt"
            )
        final = _mapping(receipt.get("final_observation"), "run final observation")
        ranks = final.get("ranks")
        if (
            final.get("phase") != "final_verify"
            or final.get("final_verified") is not True
            or final.get("run_id") != run_id
            or final.get("state") != "stopped"
            or final.get("route_state") != "withdrawn"
            or not isinstance(ranks, list)
            or not ranks
        ):
            raise QualificationError(
                "run stop lacks exact final stopped-and-withdrawn proof"
            )
        rank_keys: set[tuple[str, int]] = set()
        for raw_rank in ranks:
            rank = _mapping(raw_rank, "stopped rank receipt")
            node_id = _str(rank.get("node_id"), "stopped rank node ID")
            rank_number = rank.get("rank")
            if (
                node_id not in target.fleet_node_ids
                or type(rank_number) is not int
                or rank_number < 0
                or rank.get("state") != "stopped"
                or (node_id, rank_number) in rank_keys
            ):
                raise QualificationError("run final stop receipt has an invalid rank")
            rank_keys.add((node_id, rank_number))
        expected = expected_assignments.get(run_id)
        if expected is None or rank_keys != set(expected.items()):
            raise QualificationError(
                "run final stop receipt changed its canary node/rank assignment"
            )
        by_run[run_id] = receipt
    if set(by_run) != set(expected_assignments):
        raise QualificationError("final stop receipts do not cover exact required runs")


def _validate_serving_receipt(
    target: LaneRecoveryTarget,
    active: Mapping[str, object],
    boot_id: str,
    receipt: Mapping[str, object],
) -> None:
    if (
        receipt.get("state") != "succeeded"
        or receipt.get("run_id") != active.get("run_id")
        or receipt.get("recipe_key") != target.recipe_key
        or receipt.get("recipe_content_sha256") != target.recipe_content_sha256
        or receipt.get("package_sha256") != target.package_sha256
        or receipt.get("recipe_revision_id") != target.recipe_revision_id
        or receipt.get("alias") != target.alias
        or receipt.get("node_to_rank") != dict(target.node_to_rank)
        or receipt.get("boot_id") != boot_id
        or receipt.get("route_state") != "published"
        or receipt.get("healthy") is not True
    ):
        raise QualificationError(
            "post-reboot serving receipt changed exact lane identity"
        )


def _validate_smoke_receipt(
    target: LaneRecoveryTarget,
    active: Mapping[str, object],
    boot_id: str,
    receipt: Mapping[str, object],
    *,
    application_id: str | None = None,
    require_record_sha256: bool = False,
) -> None:
    cases = receipt.get("cases")
    observed_ids: list[object] = []
    valid_cases = isinstance(cases, list)
    if isinstance(cases, list):
        for case in cases:
            item = _mapping(case, "post-reboot fixture case")
            observed_ids.append(item.get("case_id"))
            valid_cases = valid_cases and item.get("passed") is True
    if (
        receipt.get("run_id") != active.get("run_id")
        or receipt.get("recipe_key") != target.recipe_key
        or receipt.get("recipe_content_sha256") != target.recipe_content_sha256
        or receipt.get("package_sha256") != target.package_sha256
        or receipt.get("recipe_revision_id") != target.recipe_revision_id
        or receipt.get("alias") != target.alias
        or receipt.get("node_to_rank") != dict(target.node_to_rank)
        or receipt.get("boot_id") != boot_id
        or (
            application_id is not None
            and receipt.get("application_id") != application_id
        )
        or (require_record_sha256 and not _is_sha256(receipt.get("record_sha256")))
        or not valid_cases
        or observed_ids != list(target.smoke_case_ids)
    ):
        raise QualificationError(
            "post-reboot fixture smoke is incomplete or misattributed"
        )


def _active_boot_id(
    target: LaneRecoveryTarget, transition_receipt: Mapping[str, object]
) -> str:
    fleet = _fleet_view(transition_receipt.get("fleet_snapshot"), target.fleet_node_ids)
    nodes = _mapping(fleet.get("nodes"), "post-transition Fleet nodes")
    node = _mapping(nodes.get(target.node_id), "post-transition target Fleet node")
    return _str(node.get("boot_id"), "post-transition target boot ID")


def _reject_conflicting_lane_evidence(
    target: LaneRecoveryTarget, ledger: EvidenceLedger, target_digest: str
) -> None:
    for record in ledger.records:
        if (
            record.get("plan_digest") == target.campaign_id
            and record.get("recipe") == target.recipe_key
            and str(record.get("event", "")).startswith("lane_recovery.")
        ):
            payload = _payload(record)
            if (
                payload.get("batch_id") == target.batch_id
                and payload.get("lane_id") == target.lane_id
                and payload.get("target_digest") != target_digest
            ):
                raise QualificationError(
                    "durable lane recovery evidence has a changed target"
                )


def _event(
    ledger: EvidenceLedger,
    target: LaneRecoveryTarget,
    target_digest: str,
    name: str,
) -> Mapping[str, object] | None:
    matches = [
        record
        for record in ledger.records
        if record.get("plan_digest") == target.campaign_id
        and record.get("recipe") == target.recipe_key
        and record.get("event") == name
        and _payload(record).get("batch_id") == target.batch_id
        and _payload(record).get("lane_id") == target.lane_id
        and _payload(record).get("target_digest") == target_digest
    ]
    if len(matches) > 1:
        raise QualificationError(f"duplicate durable {name} events exist")
    return matches[0] if matches else None


def _require_event(
    ledger: EvidenceLedger,
    target: LaneRecoveryTarget,
    target_digest: str,
    name: str,
) -> Mapping[str, object]:
    event = _event(ledger, target, target_digest, name)
    if event is None:
        raise QualificationError(f"durable {name} evidence is absent")
    return event


def _check_recovery_event_reviews(
    target: LaneRecoveryTarget,
    ledger: EvidenceLedger,
    review: LaneTransitionReview,
) -> None:
    for name in (
        "lane_recovery.transitioned",
        "lane_recovery.baseline",
        "lane_recovery.offline_observed",
        "lane_recovery.boot_observed",
        "lane_recovery.recovered",
        "lane_recovery.smoke.requested",
        "lane_recovery.smoke_completed",
        "lane_recovery.completed",
    ):
        record = _event(ledger, target, review.target_digest, name)
        if record is None:
            continue
        payload = _payload(record)
        if (
            payload.get("review_digest") != review.review_digest
            or payload.get("recovery_profile_number") != review.profile_number
            or payload.get("recovery_profile_id") != review.profile_id
            or payload.get("recovery_profile_digest") != review.profile_digest
            or payload.get("recovery_plan_digest") != review.plan_digest
        ):
            raise QualificationError(
                f"durable {name} event changed lane review identity"
            )


def _load_transition_review(
    target: LaneRecoveryTarget, ledger: EvidenceLedger
) -> LaneTransitionReview:
    target_digest = _digest(_target_binding(target))
    request_key = _request_key("transition", target, target_digest)
    intent = _require_event(ledger, target, target_digest, "lane_recovery.intent")
    if _payload(intent).get("transition_request_key") != request_key:
        raise QualificationError("lane recovery intent has an invalid transition key")
    reviewed = _require_event(
        ledger, target, target_digest, "lane_recovery.plan_reviewed"
    )
    payload = _payload(reviewed)
    receipt = _mapping(payload.get("review"), "durable lane transition review")
    digest = _digest(dict(receipt))
    if (
        payload.get("request_key") != request_key
        or payload.get("review_digest") != digest
    ):
        raise QualificationError("durable lane transition review digest is invalid")
    valid = _validate_transition_review(target, request_key, receipt, ledger)
    return LaneTransitionReview(
        target_digest=target_digest,
        request_key=request_key,
        review_digest=digest,
        profile_number=_positive_int(
            valid.get("profile_number"), "durable reviewed profile number"
        ),
        profile_id=str(valid["profile_id"]),
        profile_digest=str(valid["profile_digest"]),
        plan_digest=str(valid["plan_digest"]),
        receipt=valid,
    )


def _target_binding(target: LaneRecoveryTarget) -> dict[str, object]:
    return {
        "schema_version": 1,
        "campaign_id": target.campaign_id,
        "batch_id": target.batch_id,
        "lane_id": target.lane_id,
        "recipe_key": target.recipe_key,
        "recipe_content_sha256": target.recipe_content_sha256,
        "package_sha256": target.package_sha256,
        "original_run_id": target.original_run_id,
        "recipe_revision_id": target.recipe_revision_id,
        "alias": target.alias,
        "node_id": target.node_id,
        "node_to_rank": dict(target.node_to_rank),
        "smoke_case_ids": list(target.smoke_case_ids),
        "fleet_node_ids": list(target.fleet_node_ids),
        "partner_run_ids": list(target.partner_run_ids),
        "partner_aliases": list(target.partner_aliases),
        "canaries": [
            {
                "lane_id": item.lane_id,
                "record_sha256": item.record_sha256,
                "recipe_key": item.recipe_key,
                "recipe_content_sha256": item.recipe_content_sha256,
                "package_sha256": item.package_sha256,
                "assigned_node_id": item.assigned_node_id,
                "assigned_rank": item.assigned_rank,
                "run_id": item.run_id,
                "recipe_revision_id": item.recipe_revision_id,
                "alias": item.alias,
                "node_to_rank": (
                    None if item.node_to_rank is None else dict(item.node_to_rank)
                ),
                "outcome_event": item.outcome_event,
            }
            for item in target.canaries
        ],
        "profile_number": target.profile_number,
        "profile_id": target.profile_id,
        "profile_digest": target.profile_digest,
        "plan_digest": target.plan_digest,
        "allow_reactivation": target.allow_reactivation,
    }


def _envelope(target: LaneRecoveryTarget, target_digest: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "campaign_id": target.campaign_id,
        "batch_id": target.batch_id,
        "lane_id": target.lane_id,
        "recipe_key": target.recipe_key,
        "recipe_content_sha256": target.recipe_content_sha256,
        "package_sha256": target.package_sha256,
        "target_digest": target_digest,
    }


def _review_envelope(
    target: LaneRecoveryTarget,
    target_digest: str,
    review: LaneTransitionReview,
) -> dict[str, object]:
    return {
        **_envelope(target, target_digest),
        "review_digest": review.review_digest,
        "recovery_profile_number": review.profile_number,
        "recovery_profile_id": review.profile_id,
        "recovery_profile_digest": review.profile_digest,
        "recovery_plan_digest": review.plan_digest,
    }


def _request_key(action: str, target: LaneRecoveryTarget, target_digest: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"vonk-lane-recovery:{target.campaign_id}:{target.recipe_key}:"
            f"{target.batch_id}:{target.lane_id}:{action}:{target_digest}",
        )
    )


def _digest(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _payload(record: Mapping[str, object]) -> Mapping[str, object]:
    return _mapping(record.get("payload"), "qualification event payload")


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise QualificationError(f"{label} is invalid")
    return value


def _str(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise QualificationError(f"{label} is invalid")
    return value


def _string_list(value: object, label: str) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise QualificationError(f"{label} is invalid")
    result = list(value)
    if any(not isinstance(item, str) or not item for item in result):
        raise QualificationError(f"{label} is invalid")
    if len(set(result)) != len(result):
        raise QualificationError(f"{label} contains duplicates")
    return result


def _object_list(value: object, label: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        raise QualificationError(f"{label} list is invalid")
    return [_mapping(item, label) for item in value]


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _cursor(value: object, label: str) -> int:
    if type(value) is not int or not 0 <= value <= 9_223_372_036_854_775_807:
        raise QualificationError(f"{label} is invalid")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise QualificationError(f"{label} is invalid")
    return value


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise QualificationError(f"{label} is invalid")
    return value


def _timestamp(value: object, label: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value)
        except ValueError as error:
            raise QualificationError(f"{label} is invalid") from error
    else:
        raise QualificationError(f"{label} is invalid")
    if result.tzinfo is None or result.utcoffset() is None:
        raise QualificationError(f"{label} must include a timezone")
    return result.astimezone(UTC)


def _require_same_authority(value: Mapping[str, object], expected: str) -> None:
    if value.get("authority_revision") != expected:
        raise QualificationError(
            "Fleet authority revision changed during lane recovery"
        )
