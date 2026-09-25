"""Durable, exact-evidence checkpoints for exclusive dual-Spark recovery.

The campaign runner owns recipe selection, Controller profile plans, and
operator authorization. This module binds physical observations and smoke
receipts to one successful dual canary. Observation never causes rank or host
faults and never applies cleanup; cleanup has its own reviewed/apply boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from .fleet_qualification import EvidenceLedger, QualificationError

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_RANK_LOSS_EVENT = "rank-loss.observed"
_ROUTE_WITHDRAWAL_EVENT = "route-withdrawal.observed"
_RANK_RECOVERY_EVENT = "rank-recovery.smoke-completed"
_CLEANUP_TERMINAL_EVENT = _RANK_RECOVERY_EVENT
_FAILED_CLEANUP_PREFIX = "dual_recovery.failed_cleanup"


@dataclass(frozen=True, slots=True)
class DualRecoveryTarget:
    """Immutable identities for one exclusive two-node recipe assignment."""

    campaign_id: str
    batch_id: str
    lane_id: int
    recipe_key: str
    recipe_content_sha256: str
    package_sha256: str
    canary_record_sha256: str
    run_id: str
    recipe_revision_id: str
    alias: str
    node_ids: tuple[str, str]
    node_to_rank: Mapping[str, int]
    failure_node_id: str
    smoke_case_ids: tuple[str, ...]
    fleet_node_ids: tuple[str, ...]
    profile_number: int
    profile_id: str
    profile_digest: str
    plan_digest: str


@dataclass(frozen=True, slots=True)
class FailedDualCanaryTarget:
    """Exact identity of a failed smoke after an accepted dual canary load."""

    campaign_id: str
    batch_id: str
    lane_id: int
    recipe_key: str
    recipe_content_sha256: str
    package_sha256: str
    canary_record_sha256: str
    application_id: str
    application_request_key: str
    application_updated_at: str
    run_id: str | None
    recipe_revision_id: str
    alias: str
    node_ids: tuple[str, str]
    node_to_rank: Mapping[str, int]
    assigned_node_id: str
    assigned_rank: int
    fleet_node_ids: tuple[str, ...]
    profile_number: int
    profile_id: str
    profile_digest: str
    plan_digest: str


@dataclass(frozen=True, slots=True)
class DualRecoveryProgress:
    status: str
    target_digest: str
    checkpoint: str | None = None
    node_id: str | None = None
    reason: str | None = None
    receipt_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class DualCleanupReview:
    target_digest: str
    request_key: str
    review_digest: str
    profile_number: int
    profile_id: str
    profile_digest: str
    plan_digest: str
    receipt: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _FleetNode:
    online_state: str
    telemetry_freshness: object
    boot_id: object
    telemetry_observed_at: object
    loaded: tuple[Mapping[str, object], ...]
    reservations: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class _FleetView:
    generated_at: datetime
    event_cursor: int
    authority_revision: str
    nodes: Mapping[str, _FleetNode]
    loaded: tuple[tuple[str, Mapping[str, object]], ...]
    published_aliases: frozenset[str]


def observe_dual_batch(
    target: DualRecoveryTarget,
    ledger: EvidenceLedger,
    *,
    observe_fleet: Callable[[], Mapping[str, object]],
    endpoint_exists: Callable[[str], bool],
    verify_serving: Callable[[Mapping[str, object]], Mapping[str, object]],
    run_fixture_smoke: Callable[[Mapping[str, object]], Mapping[str, object]],
) -> DualRecoveryProgress:
    """Advance one durable dual-recovery checkpoint from read-only evidence.

    The ordered ladder is rank loss and route withdrawal, full dual-rank
    recovery with per-rank fixture receipts, reviewed cleanup, then sequential
    idle-host restart checks. A call performs at most one inference smoke. Host
    power and rank-loss actions are external and are never performed here.
    """

    _validate_target(target)
    target_digest = _digest(_target_binding(target))
    _require_canary(target, ledger)
    _require_pending_rank_loss(target, ledger)
    _reject_conflicting_evidence(target, ledger, target_digest)

    rank_loss = _event(ledger, target, target_digest, _RANK_LOSS_EVENT)
    if rank_loss is None:
        raw_fleet = observe_fleet()
        view = _fleet_view(raw_fleet, target.fleet_node_ids)
        lost, proof = _rank_loss_proof(target, view)
        if not lost:
            return DualRecoveryProgress(
                "awaiting-rank-loss",
                target_digest,
                checkpoint="rank-loss-and-route-withdrawal",
                reason="the exact selected Spark rank is not yet durably visible as lost",
            )
        record = _append(
            target,
            ledger,
            target_digest,
            _RANK_LOSS_EVENT,
            {
                **_snapshot_identity(view),
                "canary_record_sha256": target.canary_record_sha256,
                "proof": proof,
            },
        )
        return DualRecoveryProgress(
            "checkpoint-observed",
            target_digest,
            checkpoint="route-withdrawal",
            receipt_sha256=str(record["record_sha256"]),
        )

    _validate_rank_loss_event(target, target_digest, rank_loss)
    route_withdrawal = _event(ledger, target, target_digest, _ROUTE_WITHDRAWAL_EVENT)
    if route_withdrawal is None:
        raw_fleet = observe_fleet()
        view = _fleet_view(raw_fleet, target.fleet_node_ids)
        _require_snapshot_after(view, _payload(rank_loss), "route withdrawal")
        lost, proof = _rank_loss_proof(target, view)
        if not lost:
            return DualRecoveryProgress(
                "awaiting-route-withdrawal",
                target_digest,
                checkpoint="route-withdrawal",
                reason="the rank-loss record is durable; waiting to observe its route withdrawn",
            )
        if _endpoint_presence(endpoint_exists, target.alias):
            return DualRecoveryProgress(
                "awaiting-route-withdrawal",
                target_digest,
                checkpoint="route-withdrawal",
                reason="the exact failed-rank route is still published",
            )
        presences = _run_presences(view, target)
        if not presences or any(
            presence.get("route_state") != "withdrawn"
            for _node_id, presence in presences
        ):
            return DualRecoveryProgress(
                "awaiting-route-withdrawal",
                target_digest,
                checkpoint="route-withdrawal",
                reason="the endpoint is absent but Fleet has not confirmed the exact route withdrawn",
            )
        route_withdrawal = _append(
            target,
            ledger,
            target_digest,
            _ROUTE_WITHDRAWAL_EVENT,
            {
                "rank_loss_record_sha256": rank_loss["record_sha256"],
                "endpoint_not_found": True,
                "fleet_rank_presence": [
                    {"node_id": node_id, **dict(presence)}
                    for node_id, presence in presences
                ],
                "proof": proof,
                "fleet_snapshot": dict(raw_fleet),
                **_snapshot_identity(view),
            },
        )
        return DualRecoveryProgress(
            "checkpoint-observed",
            target_digest,
            checkpoint="rank-recovery-and-smoke",
            receipt_sha256=str(route_withdrawal["record_sha256"]),
        )
    _validate_route_withdrawal_event(target, target_digest, rank_loss, route_withdrawal)
    recovery = _event(ledger, target, target_digest, _RANK_RECOVERY_EVENT)
    if recovery is None:
        raw_fleet = observe_fleet()
        view = _fleet_view(raw_fleet, target.fleet_node_ids)
        _require_snapshot_after(view, _payload(route_withdrawal), "rank recovery")
        if not _serving_dual_run(target, view):
            return DualRecoveryProgress(
                "awaiting-rank-recovery",
                target_digest,
                checkpoint="rank-recovery-and-smoke",
                reason="waiting for both exact ranks, fresh telemetry, and the published healthy route",
            )
        if not _endpoint_presence(endpoint_exists, target.alias):
            return DualRecoveryProgress(
                "awaiting-route-publication",
                target_digest,
                checkpoint="rank-recovery-and-smoke",
                reason="Fleet ranks recovered; waiting for the exact endpoint route",
            )
        serving_receipt = verify_serving(
            {
                **_envelope(target, target_digest),
                "fleet_snapshot": raw_fleet,
            }
        )
        _validate_serving_receipt(target, serving_receipt)
        completed_nodes = _smoke_nodes(ledger, target, target_digest)
        next_node = next(
            (
                node_id
                for node_id, _rank in _rank_order(target)
                if node_id not in completed_nodes
            ),
            None,
        )
        if next_node is not None:
            request_key = _smoke_request_key(target, target_digest, next_node)
            requested = _node_event(
                ledger,
                target,
                target_digest,
                "rank-recovery.smoke-requested",
                next_node,
            )
            if requested is None:
                _append(
                    target,
                    ledger,
                    target_digest,
                    "rank-recovery.smoke-requested",
                    {"node_id": next_node, "request_key": request_key},
                )
            elif _payload(requested).get("request_key") != request_key:
                raise QualificationError(
                    "rank-recovery smoke intent changed its stable request identity"
                )
            rank = target.node_to_rank[next_node]
            smoke = run_fixture_smoke(
                {
                    **_envelope(target, target_digest),
                    "phase": "rank-recovery",
                    "request_key": request_key,
                    "node_id": next_node,
                    "rank": rank,
                    "run_id": target.run_id,
                    "alias": target.alias,
                    "case_ids": list(target.smoke_case_ids),
                }
            )
            _validate_smoke_receipt(
                target,
                smoke,
                node_id=next_node,
                rank=rank,
            )
            _append(
                target,
                ledger,
                target_digest,
                "rank-recovery.smoke-node-completed",
                {
                    "node_id": next_node,
                    "rank": rank,
                    "request_key": request_key,
                    "record_sha256": smoke["record_sha256"],
                    "fixture_receipt": dict(
                        _mapping(
                            smoke.get("fixture_receipt"),
                            "rank-recovery fixture receipt",
                        )
                    ),
                    **_snapshot_identity(view),
                },
            )
            completed_nodes.add(next_node)
            if completed_nodes != set(target.node_ids):
                return DualRecoveryProgress(
                    "rank-recovery-smoke-in-progress",
                    target_digest,
                    checkpoint="rank-recovery-and-smoke",
                    node_id=next_node,
                )
        if completed_nodes != set(target.node_ids):
            raise QualificationError("dual recovery smoke did not cover both ranks")
        node_receipts = _smoke_receipts(target, ledger, target_digest)
        case_receipt = _mapping(
            node_receipts[_rank_order(target)[0][0]].get("fixture_receipt"),
            "rank-recovery smoke fixture receipt",
        )
        payload = {
            "canary_record_sha256": target.canary_record_sha256,
            "fleet_rank_presence": [
                {"node_id": node_id, **dict(presence)}
                for node_id, presence in _run_presences(view, target)
            ],
            "smoke": {
                **dict(case_receipt),
                "endpoint_alias": target.alias,
                "recipe_content_sha256": target.recipe_content_sha256,
                "cases": [{"case_id": case_id} for case_id in target.smoke_case_ids],
                "rank_receipts": [
                    {
                        "node_id": node_id,
                        "rank": target.node_to_rank[node_id],
                        "record_sha256": node_receipts[node_id]["record_sha256"],
                    }
                    for node_id, _rank in _rank_order(target)
                ],
            },
            **_snapshot_identity(view),
            "rank_presence_verified": dict(serving_receipt),
            "node_smoke_record_sha256s": {
                node_id: str(node_receipts[node_id]["record_sha256"])
                for node_id, _rank in _rank_order(target)
            },
        }
        record = _append(
            target,
            ledger,
            target_digest,
            _RANK_RECOVERY_EVENT,
            payload,
        )
        return DualRecoveryProgress(
            "rank-recovery-smoke-completed",
            target_digest,
            checkpoint="reviewed-cleanup",
            receipt_sha256=str(record["record_sha256"]),
        )

    _validate_rank_recovery_event(target, target_digest, ledger, recovery)
    cleanup = _event(ledger, target, target_digest, "dual_recovery.cleanup.completed")
    if cleanup is None:
        return DualRecoveryProgress(
            "awaiting-reviewed-cleanup",
            target_digest,
            checkpoint="reviewed-cleanup",
            reason="rank recovery is proven; review and apply the exact whole-profile stop plan",
        )
    _cleanup_receipt, cleanup_view = _validate_cleanup_event(
        target, target_digest, ledger, cleanup
    )
    completion = _event(ledger, target, target_digest, "dual_recovery.completed")
    if completion is not None:
        _validate_completion(target, target_digest, ledger, completion, cleanup)
        return DualRecoveryProgress(
            "complete",
            target_digest,
            receipt_sha256=str(completion["record_sha256"]),
        )
    if _endpoint_presence(endpoint_exists, target.alias):
        raise QualificationError("dual cleanup left the exact recipe route published")
    baseline = _event(ledger, target, target_digest, "host-restart.baseline")
    if baseline is None:
        if not _all_selected_live(cleanup_view, target):
            return DualRecoveryProgress(
                "awaiting-host-online",
                target_digest,
                checkpoint="host-restart-baseline",
                reason="both selected Sparks need live telemetry before idle restart observations",
            )
        record = _append(
            target,
            ledger,
            target_digest,
            "host-restart.baseline",
            {
                "run_id": target.run_id,
                "cleanup_record_sha256": cleanup.get("record_sha256"),
                "rank_recovery_record_sha256": _rank_recovery_sha(
                    target, ledger, target_digest
                ),
                "rank_smoke_refs": _rank_smoke_refs(target, ledger, target_digest),
                "nodes": {
                    node_id: {
                        "boot_id": cleanup_view.nodes[node_id].boot_id,
                        "telemetry_observed_at": cleanup_view.nodes[
                            node_id
                        ].telemetry_observed_at,
                    }
                    for node_id in target.node_ids
                },
                **_snapshot_identity(cleanup_view),
            },
        )
        return DualRecoveryProgress(
            "awaiting-host-offline",
            target_digest,
            checkpoint="host-offline",
            node_id=_rank_order(target)[0][0],
            receipt_sha256=str(record["record_sha256"]),
        )

    baseline_payload = _payload(baseline)
    baseline_view_identity = _snapshot_identity_from_payload(
        baseline_payload, "host-restart baseline"
    )
    _validate_baseline(
        target, target_digest, ledger, cleanup, cleanup_view, baseline_payload
    )
    recovered_nodes = _recovered_nodes(ledger, target, target_digest)
    expected_node = next(
        (
            node_id
            for node_id, _rank in _rank_order(target)
            if node_id not in recovered_nodes
        ),
        None,
    )
    if expected_node is None:
        completion = _event(ledger, target, target_digest, "dual_recovery.completed")
        if completion is None:
            final_snapshot = observe_fleet()
            final_view = _fleet_view(final_snapshot, target.fleet_node_ids)
            prior_recovery_record = next(
                (
                    recovered
                    for node_id, _rank in reversed(_rank_order(target))
                    if (
                        recovered := _node_event(
                            ledger,
                            target,
                            target_digest,
                            "host-restart.recovered",
                            node_id,
                        )
                    )
                    is not None
                ),
                None,
            )
            if prior_recovery_record is None:
                raise QualificationError(
                    "dual recovery completion lacks a host restart receipt"
                )
            prior_recovery = _payload(prior_recovery_record)
            _require_snapshot_after(final_view, prior_recovery, "final idle Fleet")
            _require_idle_fleet(target, final_view)
            _require_endpoint_state(endpoint_exists, target.alias, expected=False)
            _require_same_authority(
                final_view, str(baseline_view_identity["authority_revision"])
            )
            if not _all_selected_live(final_view, target):
                return DualRecoveryProgress(
                    "awaiting-host-online",
                    target_digest,
                    checkpoint="final-idle-verification",
                    reason="both restarted Sparks need live telemetry before final acceptance",
                )
            for node_id, _rank in _rank_order(target):
                node = final_view.nodes[node_id]
                recovered_record = _node_event(
                    ledger,
                    target,
                    target_digest,
                    "host-restart.recovered",
                    node_id,
                )
                if recovered_record is None:
                    raise QualificationError(
                        "dual recovery completion lacks a host restart receipt"
                    )
                recovered = _payload(recovered_record)
                if node.boot_id != recovered.get("observed_boot_id"):
                    raise QualificationError(
                        "final Fleet boot identity changed after its restart receipt"
                    )
            refs = _completion_refs(ledger, target, target_digest)
            record = _append(
                target,
                ledger,
                target_digest,
                "dual_recovery.completed",
                {
                    "canary_record_sha256": target.canary_record_sha256,
                    "cleanup_record_sha256": cleanup.get("record_sha256"),
                    "event_refs": refs,
                    "endpoint_not_found": True,
                    "final_fleet_snapshot": dict(final_snapshot),
                },
            )
            return DualRecoveryProgress(
                "complete",
                target_digest,
                receipt_sha256=str(record["record_sha256"]),
            )
        _validate_completion(target, target_digest, ledger, completion, cleanup)
        return DualRecoveryProgress(
            "complete",
            target_digest,
            receipt_sha256=str(completion["record_sha256"]),
        )

    current = _fleet_view(observe_fleet(), target.fleet_node_ids)
    offline_event = _node_event(
        ledger,
        target,
        target_digest,
        "host-restart.offline",
        expected_node,
    )
    prior_payload: Mapping[str, object] = baseline_payload
    if offline_event is not None:
        prior_payload = _payload(offline_event)
    else:
        previous_node = next(
            (
                node_id
                for node_id, _rank in _rank_order(target)
                if node_id != expected_node and node_id in recovered_nodes
            ),
            None,
        )
        if previous_node is not None:
            previous_recovered = _node_event(
                ledger,
                target,
                target_digest,
                "host-restart.recovered",
                previous_node,
            )
            if previous_recovered is not None:
                prior_payload = _payload(previous_recovered)
    _require_snapshot_after(current, prior_payload, "idle host restart")
    _require_idle_fleet(target, current)
    if _endpoint_presence(endpoint_exists, target.alias):
        raise QualificationError("dual cleanup left the exact recipe route published")
    _require_same_authority(
        current,
        _required_string(
            baseline_view_identity.get("authority_revision"),
            "restart baseline authority revision",
        ),
    )
    selected_nodes = set(target.node_ids)
    peers = selected_nodes - {expected_node}
    if any(current.nodes[node_id].online_state != "online" for node_id in peers):
        return DualRecoveryProgress(
            "awaiting-sequential-host-restart",
            target_digest,
            checkpoint="host-offline",
            node_id=expected_node,
            reason="the other selected Spark must remain online during this host restart",
        )

    node = current.nodes[expected_node]
    baseline_nodes = _mapping(baseline_payload.get("nodes"), "restart baseline nodes")
    baseline_node = _mapping(baseline_nodes.get(expected_node), "restart baseline node")
    baseline_boot = _required_string(baseline_node.get("boot_id"), "baseline boot ID")
    if offline_event is None:
        if node.online_state != "offline":
            if node.online_state != "online":
                return DualRecoveryProgress(
                    "awaiting-host-offline",
                    target_digest,
                    checkpoint="host-offline",
                    node_id=expected_node,
                    reason="Fleet has not confirmed the selected Spark offline",
                )
            if isinstance(node.boot_id, str) and node.boot_id != baseline_boot:
                return DualRecoveryProgress(
                    "awaiting-host-offline",
                    target_digest,
                    checkpoint="host-offline",
                    node_id=expected_node,
                    reason="boot changed before an offline observation was durably recorded",
                )
            return DualRecoveryProgress(
                "awaiting-host-offline",
                target_digest,
                checkpoint="host-offline",
                node_id=expected_node,
                reason="take this idle Spark offline, then resume observation",
            )
        if node.boot_id not in (None, baseline_boot):
            return DualRecoveryProgress(
                "awaiting-host-offline",
                target_digest,
                checkpoint="host-offline",
                node_id=expected_node,
                reason="offline telemetry carries a changed boot ID without an ordered observation",
            )
        if current.generated_at <= _timestamp(
            baseline_payload.get("generated_at"), "baseline generated_at"
        ):
            return DualRecoveryProgress(
                "awaiting-host-offline",
                target_digest,
                checkpoint="host-offline",
                node_id=expected_node,
                reason="offline Fleet observation is not newer than the idle baseline",
            )
        record = _append(
            target,
            ledger,
            target_digest,
            "host-restart.offline",
            {
                "node_id": expected_node,
                "rank": target.node_to_rank[expected_node],
                **_rank_smoke_binding(target, ledger, target_digest, expected_node),
                "baseline_boot_id": baseline_boot,
                "online_state": "offline",
                "online_peer_node_ids": sorted(peers),
                **_snapshot_identity(current),
            },
        )
        return DualRecoveryProgress(
            "awaiting-host-online",
            target_digest,
            checkpoint="host-online",
            node_id=expected_node,
            receipt_sha256=str(record["record_sha256"]),
        )

    offline_payload = _payload(offline_event)
    _validate_offline_event(
        target,
        target_digest,
        ledger,
        expected_node,
        baseline_boot,
        offline_payload,
        baseline_payload,
    )
    _require_snapshot_after(current, offline_payload, "host return")
    if node.online_state != "online":
        return DualRecoveryProgress(
            "awaiting-host-online",
            target_digest,
            checkpoint="host-online",
            node_id=expected_node,
            reason="selected Spark remains offline",
        )
    if node.telemetry_freshness != "live" or not isinstance(node.boot_id, str):
        return DualRecoveryProgress(
            "awaiting-live-telemetry",
            target_digest,
            checkpoint="host-online",
            node_id=expected_node,
            reason="the returning Spark needs live telemetry with a boot ID",
        )
    if node.boot_id == baseline_boot:
        return DualRecoveryProgress(
            "awaiting-changed-boot",
            target_digest,
            checkpoint="host-online",
            node_id=expected_node,
            reason="the selected Spark returned with its baseline boot ID",
        )
    telemetry_at = _timestamp(node.telemetry_observed_at, "recovered telemetry time")
    offline_at = _timestamp(offline_payload.get("generated_at"), "offline generated_at")
    if telemetry_at <= offline_at:
        return DualRecoveryProgress(
            "awaiting-fresh-telemetry",
            target_digest,
            checkpoint="host-online",
            node_id=expected_node,
            reason="live boot telemetry must be sampled after the offline observation",
        )
    record = _append(
        target,
        ledger,
        target_digest,
        "host-restart.recovered",
        {
            "node_id": expected_node,
            "rank": target.node_to_rank[expected_node],
            **_rank_smoke_binding(target, ledger, target_digest, expected_node),
            "baseline_boot_id": baseline_boot,
            "observed_boot_id": node.boot_id,
            "online_state": "online",
            "online_peer_node_ids": sorted(peers),
            "telemetry_freshness": "live",
            "telemetry_observed_at": telemetry_at.isoformat(),
            **_snapshot_identity(current),
        },
    )
    recovered_nodes.add(expected_node)
    return DualRecoveryProgress(
        "awaiting-host-offline"
        if recovered_nodes != selected_nodes
        else "host-restarts-complete",
        target_digest,
        checkpoint=("host-offline" if recovered_nodes != selected_nodes else None),
        node_id=next(
            (
                node_id
                for node_id, _rank in _rank_order(target)
                if node_id not in recovered_nodes
            ),
            None,
        ),
        receipt_sha256=str(record["record_sha256"]),
    )


def review_dual_cleanup(
    target: DualRecoveryTarget,
    ledger: EvidenceLedger,
    *,
    prepare_cleanup: Callable[[Mapping[str, object]], Mapping[str, object]],
) -> DualCleanupReview:
    """Persist an exact whole-profile cleanup review after dual smoke passes."""

    _validate_target(target)
    _require_canary(target, ledger)
    target_digest = _digest(_target_binding(target))
    _require_rank_recovery_complete(target, ledger, target_digest)
    _reject_conflicting_evidence(target, ledger, target_digest)
    completed = _event(ledger, target, target_digest, "dual_recovery.cleanup.completed")
    if completed is not None:
        _validate_cleanup_event(target, target_digest, ledger, completed)
        return _load_cleanup_review(target, ledger, target_digest)
    request_key = _cleanup_request_key(target, target_digest)
    intent = _event(ledger, target, target_digest, "dual_recovery.cleanup.intent")
    if intent is None:
        _append(
            target,
            ledger,
            target_digest,
            "dual_recovery.cleanup.intent",
            {
                "request_key": request_key,
                "terminal_event": _CLEANUP_TERMINAL_EVENT,
                "terminal_record_sha256": _rank_recovery_sha(
                    target, ledger, target_digest
                ),
                "stop_run_ids": [target.run_id],
                "stop_aliases": [target.alias],
                "fleet_node_ids": sorted(target.fleet_node_ids),
            },
        )
    elif _payload(intent).get("request_key") != request_key:
        raise QualificationError("dual cleanup intent changed its stable request key")
    request = {
        **_envelope(target, target_digest),
        "request_key": request_key,
        "cleanup_mode": "dual-lane",
        "terminal_event": _CLEANUP_TERMINAL_EVENT,
        "terminal_record_sha256": _rank_recovery_sha(target, ledger, target_digest),
        "active_run_id": target.run_id,
        "stop_run_ids": [target.run_id],
        "stop_aliases": [target.alias],
        "fleet_node_ids": sorted(target.fleet_node_ids),
    }
    receipt = prepare_cleanup(request)
    valid = _validate_cleanup_review(target, request_key, request, receipt)
    review_digest = _digest(dict(valid))
    prior = _event(ledger, target, target_digest, "dual_recovery.cleanup.plan_reviewed")
    if prior is None:
        _append(
            target,
            ledger,
            target_digest,
            "dual_recovery.cleanup.plan_reviewed",
            {
                "request_key": request_key,
                "review_digest": review_digest,
                "review": dict(valid),
            },
        )
    else:
        previous = _payload(prior)
        if (
            previous.get("request_key") != request_key
            or previous.get("review_digest") != review_digest
            or previous.get("review") != dict(valid)
        ):
            raise QualificationError(
                "dual cleanup preview changed after its durable review"
            )
    return DualCleanupReview(
        target_digest=target_digest,
        request_key=request_key,
        review_digest=review_digest,
        profile_number=_positive_int(
            valid.get("profile_number"), "cleanup profile number"
        ),
        profile_id=_required_string(valid.get("profile_id"), "cleanup profile ID"),
        profile_digest=_sha(valid.get("profile_digest"), "cleanup profile digest"),
        plan_digest=_sha(valid.get("plan_digest"), "cleanup plan digest"),
        receipt=valid,
    )


def apply_dual_cleanup(
    target: DualRecoveryTarget,
    ledger: EvidenceLedger,
    *,
    prepare_cleanup: Callable[[Mapping[str, object]], Mapping[str, object]],
    apply_authorized: bool,
    cleanup_to_idle: Callable[[Mapping[str, object]], Mapping[str, object]],
    reconcile_cleanup: Callable[[Mapping[str, object]], Mapping[str, object]],
    endpoint_exists: Callable[[str], bool],
) -> DualRecoveryProgress:
    """Apply a reviewed stop once; reconcile its original receipt on resume."""

    if type(apply_authorized) is not bool:
        raise QualificationError("dual cleanup apply authorization must be explicit")
    _validate_target(target)
    target_digest = _digest(_target_binding(target))
    _require_canary(target, ledger)
    _require_rank_recovery_complete(target, ledger, target_digest)
    _reject_conflicting_evidence(target, ledger, target_digest)
    completed = _event(ledger, target, target_digest, "dual_recovery.cleanup.completed")
    if completed is not None:
        review = _load_cleanup_review(target, ledger, target_digest)
        receipt, _stored_view = _validate_cleanup_event(
            target, target_digest, ledger, completed
        )
        if apply_authorized is not True:
            return DualRecoveryProgress(
                "awaiting-explicit-cleanup",
                target_digest,
                checkpoint="cleanup-reconciliation",
                reason="the stop already succeeded; explicit apply is required to reconcile its original receipt",
            )
        reconciliation_request = {
            **_envelope(target, target_digest),
            "resume_completed": True,
            "request_key": review.request_key,
            "review_digest": review.review_digest,
            "application_id": _required_string(
                receipt.get("application_id"), "completed cleanup application ID"
            ),
            "application_updated_at": receipt.get("application_updated_at"),
            "profile_number": review.profile_number,
            "profile_id": review.profile_id,
            "profile_digest": review.profile_digest,
            "plan_digest": review.plan_digest,
            "cleanup_mode": "dual-lane",
            "terminal_event": _CLEANUP_TERMINAL_EVENT,
            "terminal_record_sha256": _rank_recovery_sha(target, ledger, target_digest),
            "stop_run_ids": [target.run_id],
            "stop_aliases": [target.alias],
            "fleet_node_ids": sorted(target.fleet_node_ids),
            "prior_receipt": dict(receipt),
        }
        reconciled = reconcile_cleanup(reconciliation_request)
        reconciled_view = _validate_cleanup_reconciliation(
            target,
            ledger,
            review,
            receipt,
            reconciled,
            endpoint_exists=endpoint_exists,
        )
        reconciliation = _event(
            ledger, target, target_digest, "dual_recovery.cleanup.reconciled"
        )
        if reconciliation is None:
            reconciliation_record = _append(
                target,
                ledger,
                target_digest,
                "dual_recovery.cleanup.reconciled",
                {
                    "request_key": review.request_key,
                    "review_digest": review.review_digest,
                    "application_id": receipt["application_id"],
                    "application_updated_at": receipt["application_updated_at"],
                    "prior_cleanup_record_sha256": completed["record_sha256"],
                    "reconciled_fleet_snapshot": dict(
                        _mapping(
                            reconciled.get("fleet_snapshot"),
                            "reconciled FleetSnapshot",
                        )
                    ),
                },
            )
        else:
            prior_reconciliation = _payload(reconciliation)
            if (
                prior_reconciliation.get("request_key") != review.request_key
                or prior_reconciliation.get("review_digest") != review.review_digest
                or prior_reconciliation.get("application_id")
                != receipt.get("application_id")
                or prior_reconciliation.get("prior_cleanup_record_sha256")
                != completed.get("record_sha256")
            ):
                raise QualificationError(
                    "cleanup reconciliation changed its original application identity"
                )
            prior_reconciled_view = _fleet_view(
                prior_reconciliation.get("reconciled_fleet_snapshot"),
                target.fleet_node_ids,
            )
            _require_idle_fleet(target, prior_reconciled_view)
            _require_snapshot_after(
                reconciled_view,
                _snapshot_identity(prior_reconciled_view),
                "repeated cleanup reconciliation",
            )
            reconciliation_record = reconciliation
        return DualRecoveryProgress(
            "cleanup-completed",
            target_digest,
            checkpoint="host-restart-baseline",
            receipt_sha256=str(reconciliation_record["record_sha256"]),
        )
    review = review_dual_cleanup(target, ledger, prepare_cleanup=prepare_cleanup)
    if review.target_digest != target_digest:
        raise QualificationError("dual cleanup review changed its exact target")
    if apply_authorized is not True:
        return DualRecoveryProgress(
            "awaiting-explicit-cleanup",
            target_digest,
            checkpoint="reviewed-cleanup",
            reason="the exact whole-profile stop is reviewed; explicit cleanup apply is required",
        )
    request = {
        **_envelope(target, target_digest),
        "request_key": review.request_key,
        "review_digest": review.review_digest,
        "review": dict(review.receipt),
        "cleanup_mode": "dual-lane",
        "terminal_event": _CLEANUP_TERMINAL_EVENT,
        "terminal_record_sha256": _rank_recovery_sha(target, ledger, target_digest),
        "profile_number": review.profile_number,
        "profile_id": review.profile_id,
        "profile_digest": review.profile_digest,
        "plan_digest": review.plan_digest,
        "stop_run_ids": [target.run_id],
        "stop_aliases": [target.alias],
        "fleet_node_ids": sorted(target.fleet_node_ids),
    }
    _event_or_append(
        target,
        ledger,
        target_digest,
        "dual_recovery.cleanup.apply-requested",
        {
            "request_key": review.request_key,
            "review_digest": review.review_digest,
        },
    )
    receipt = cleanup_to_idle(request)
    _validate_cleanup_receipt(
        target, ledger, review, receipt, endpoint_exists=endpoint_exists
    )
    record = _append(
        target,
        ledger,
        target_digest,
        "dual_recovery.cleanup.completed",
        {
            "request_key": review.request_key,
            "review_digest": review.review_digest,
            "terminal_event": _CLEANUP_TERMINAL_EVENT,
            "terminal_record_sha256": _rank_recovery_sha(target, ledger, target_digest),
            "receipt": dict(receipt),
        },
    )
    return DualRecoveryProgress(
        "cleanup-completed",
        target_digest,
        checkpoint="host-restart-baseline",
        receipt_sha256=str(record["record_sha256"]),
    )


def review_failed_dual_cleanup(
    target: FailedDualCanaryTarget,
    ledger: EvidenceLedger,
    *,
    prepare_cleanup: Callable[[Mapping[str, object]], Mapping[str, object]],
) -> DualCleanupReview:
    """Review release of a dual run whose fixture canary failed."""

    _validate_failed_target(target)
    _require_failed_canary(target, ledger)
    target_digest = _digest(_failed_target_binding(target))
    _reject_failed_cleanup_conflicts(target, ledger, target_digest)
    completed = _failed_cleanup_event(
        ledger, target, target_digest, f"{_FAILED_CLEANUP_PREFIX}.completed"
    )
    if completed is not None:
        _validate_failed_cleanup_event(target, target_digest, ledger, completed)
        return _load_failed_cleanup_review(target, ledger, target_digest)

    request_key = _failed_cleanup_request_key(target, target_digest)
    intent = _failed_cleanup_event(
        ledger, target, target_digest, f"{_FAILED_CLEANUP_PREFIX}.intent"
    )
    if intent is None:
        _append_failed_cleanup(
            target,
            ledger,
            target_digest,
            f"{_FAILED_CLEANUP_PREFIX}.intent",
            {"request_key": request_key},
        )
    elif _payload(intent).get("request_key") != request_key:
        raise QualificationError(
            "failed-canary cleanup changed its stable request identity"
        )

    request = _failed_cleanup_request(target, target_digest, request_key)
    value = prepare_cleanup(request)
    valid = _validate_failed_cleanup_review(target, request, value)
    review_digest = _digest(valid)
    prior = _failed_cleanup_event(
        ledger, target, target_digest, f"{_FAILED_CLEANUP_PREFIX}.plan_reviewed"
    )
    if prior is None:
        _append_failed_cleanup(
            target,
            ledger,
            target_digest,
            f"{_FAILED_CLEANUP_PREFIX}.plan_reviewed",
            {
                "request_key": request_key,
                "review_digest": review_digest,
                "review": valid,
            },
        )
    else:
        prior_payload = _payload(prior)
        if (
            prior_payload.get("request_key") != request_key
            or prior_payload.get("review_digest") != review_digest
            or prior_payload.get("review") != valid
        ):
            raise QualificationError(
                "failed-canary cleanup preview changed after its durable review"
            )
    return DualCleanupReview(
        target_digest,
        request_key,
        review_digest,
        target.profile_number,
        target.profile_id,
        str(valid["profile_digest"]),
        str(valid["plan_digest"]),
        valid,
    )


def apply_failed_dual_cleanup(
    target: FailedDualCanaryTarget,
    ledger: EvidenceLedger,
    *,
    prepare_cleanup: Callable[[Mapping[str, object]], Mapping[str, object]],
    apply_authorized: bool,
    cleanup_to_idle: Callable[[Mapping[str, object]], Mapping[str, object]],
    reconcile_cleanup: Callable[[Mapping[str, object]], Mapping[str, object]],
    endpoint_exists: Callable[[str], bool],
) -> DualRecoveryProgress:
    """Release a failed canary run under explicit reviewed cleanup only."""

    if type(apply_authorized) is not bool:
        raise QualificationError("failed-canary cleanup authorization must be explicit")
    _validate_failed_target(target)
    failure = _require_failed_canary(target, ledger)
    target_digest = _digest(_failed_target_binding(target))
    _reject_failed_cleanup_conflicts(target, ledger, target_digest)
    completed = _failed_cleanup_event(
        ledger, target, target_digest, f"{_FAILED_CLEANUP_PREFIX}.completed"
    )
    if completed is not None:
        review = _load_failed_cleanup_review(target, ledger, target_digest)
        receipt, _stored_view = _validate_failed_cleanup_event(
            target, target_digest, ledger, completed
        )
        if not apply_authorized:
            return DualRecoveryProgress(
                "awaiting-explicit-failed-cleanup",
                target_digest,
                checkpoint="failed-cleanup-reconciliation",
                reason="the failed-canary cleanup already succeeded; explicit apply is required to reconcile its original application",
            )
        request = {
            **_failed_cleanup_envelope(target, target_digest),
            "resume_completed": True,
            "request_key": review.request_key,
            "review_digest": review.review_digest,
            "application_id": receipt["application_id"],
            "application_updated_at": receipt["application_updated_at"],
            "profile_number": review.profile_number,
            "profile_id": review.profile_id,
            "profile_digest": review.profile_digest,
            "plan_digest": review.plan_digest,
            "terminal_event": "canary.failed",
            "terminal_record_sha256": target.canary_record_sha256,
            "source_application_id": target.application_id,
            "source_application_request_key": target.application_request_key,
            "stop_run_ids": [_failed_run_id(target)],
            "stop_aliases": [target.alias],
            "fleet_node_ids": sorted(target.fleet_node_ids),
            "prior_receipt": dict(receipt),
        }
        reconciled = reconcile_cleanup(request)
        view = _validate_failed_cleanup_reconciliation(
            target,
            review,
            failure,
            receipt,
            reconciled,
            endpoint_exists=endpoint_exists,
        )
        reconciliation = _failed_cleanup_event(
            ledger,
            target,
            target_digest,
            f"{_FAILED_CLEANUP_PREFIX}.reconciled",
        )
        if reconciliation is None:
            record = _append_failed_cleanup(
                target,
                ledger,
                target_digest,
                f"{_FAILED_CLEANUP_PREFIX}.reconciled",
                {
                    "request_key": review.request_key,
                    "review_digest": review.review_digest,
                    "application_id": receipt["application_id"],
                    "application_updated_at": receipt["application_updated_at"],
                    "prior_cleanup_record_sha256": completed["record_sha256"],
                    "reconciled_fleet_snapshot": dict(
                        _mapping(
                            reconciled.get("fleet_snapshot"), "reconciled FleetSnapshot"
                        )
                    ),
                },
            )
        else:
            prior = _payload(reconciliation)
            if (
                prior.get("request_key") != review.request_key
                or prior.get("review_digest") != review.review_digest
                or prior.get("application_id") != receipt.get("application_id")
                or prior.get("prior_cleanup_record_sha256")
                != completed.get("record_sha256")
            ):
                raise QualificationError(
                    "failed-canary cleanup reconciliation changed its original application"
                )
            prior_view = _fleet_view(
                prior.get("reconciled_fleet_snapshot"), target.fleet_node_ids
            )
            _require_idle_fleet(target, prior_view)
            if not _all_selected_live(prior_view, target):
                raise QualificationError(
                    "persisted failed cleanup reconciliation lacks live selected Sparks"
                )
            _require_snapshot_after(
                view,
                _snapshot_identity(prior_view),
                "repeated failed cleanup reconciliation",
            )
            record = reconciliation
        return DualRecoveryProgress(
            "failed-cleanup-completed",
            target_digest,
            checkpoint="recipe-failure-finalization",
            receipt_sha256=str(record["record_sha256"]),
        )

    apply_request = _failed_cleanup_event(
        ledger,
        target,
        target_digest,
        f"{_FAILED_CLEANUP_PREFIX}.apply_requested",
    )
    if apply_request is not None:
        review = _load_failed_cleanup_review(target, ledger, target_digest)
    else:
        review = review_failed_dual_cleanup(
            target, ledger, prepare_cleanup=prepare_cleanup
        )
    if not apply_authorized:
        return DualRecoveryProgress(
            "awaiting-explicit-failed-cleanup",
            target_digest,
            checkpoint="failed-cleanup-reviewed",
            reason="the exact failed-canary whole-profile stop is reviewed; explicit cleanup apply is required",
        )
    request = {
        **_failed_cleanup_envelope(target, target_digest),
        "request_key": review.request_key,
        "review_digest": review.review_digest,
        "review": dict(review.receipt),
        "cleanup_mode": "failed-dual-lane",
        "terminal_event": "canary.failed",
        "terminal_record_sha256": target.canary_record_sha256,
        "source_application_id": target.application_id,
        "source_application_request_key": target.application_request_key,
        "source_application_updated_at": target.application_updated_at,
        "profile_number": review.profile_number,
        "profile_id": review.profile_id,
        "profile_digest": review.profile_digest,
        "plan_digest": review.plan_digest,
        "stop_run_ids": [_failed_run_id(target)],
        "stop_aliases": [target.alias],
        "node_to_rank": dict(target.node_to_rank),
        "assigned_node_id": target.assigned_node_id,
        "assigned_rank": target.assigned_rank,
        "fleet_node_ids": sorted(target.fleet_node_ids),
        "resume_requested": apply_request is not None,
    }
    if apply_request is None:
        _append_failed_cleanup(
            target,
            ledger,
            target_digest,
            f"{_FAILED_CLEANUP_PREFIX}.apply_requested",
            {"request_key": review.request_key, "review_digest": review.review_digest},
        )
    elif (
        _payload(apply_request).get("request_key") != review.request_key
        or _payload(apply_request).get("review_digest") != review.review_digest
    ):
        raise QualificationError(
            "failed-canary cleanup retry changed its reviewed identity"
        )
    receipt = cleanup_to_idle(request)
    _validate_failed_cleanup_receipt(
        target,
        review,
        failure,
        receipt,
        endpoint_exists=endpoint_exists,
    )
    record = _append_failed_cleanup(
        target,
        ledger,
        target_digest,
        f"{_FAILED_CLEANUP_PREFIX}.completed",
        {
            "request_key": review.request_key,
            "review_digest": review.review_digest,
            "terminal_event": "canary.failed",
            "terminal_record_sha256": target.canary_record_sha256,
            "receipt": dict(receipt),
        },
    )
    return DualRecoveryProgress(
        "failed-cleanup-completed",
        target_digest,
        checkpoint="recipe-failure-finalization",
        receipt_sha256=str(record["record_sha256"]),
    )


def _validate_failed_target(target: FailedDualCanaryTarget) -> None:
    for label, value in (
        ("campaign ID", target.campaign_id),
        ("recipe content digest", target.recipe_content_sha256),
        ("package digest", target.package_sha256),
        ("failed canary record digest", target.canary_record_sha256),
        ("profile digest", target.profile_digest),
        ("load plan digest", target.plan_digest),
    ):
        _sha(value, f"failed dual cleanup {label}")
    if not isinstance(target.batch_id, str) or not target.batch_id:
        raise QualificationError("failed dual cleanup batch ID is invalid")
    if type(target.lane_id) is not int or target.lane_id < 1:
        raise QualificationError("failed dual cleanup lane ID is invalid")
    if not isinstance(target.recipe_key, str) or "/" not in target.recipe_key:
        raise QualificationError("failed dual cleanup recipe key is invalid")
    try:
        _required_string(target.run_id, "failed dual canary run ID")
    except QualificationError as error:
        raise QualificationError(
            "failed dual canary is missing its exact run ID; reconcile the original Controller profile application and require typed no-effects proof before cleanup"
        ) from error
    for label, value in (
        ("application ID", target.application_id),
        ("application request key", target.application_request_key),
        ("recipe revision ID", target.recipe_revision_id),
        ("route alias", target.alias),
        ("profile ID", target.profile_id),
        ("assigned Spark ID", target.assigned_node_id),
    ):
        _required_string(value, f"failed dual cleanup {label}")
    _timestamp(target.application_updated_at, "failed canary application update time")
    if type(target.profile_number) is not int or target.profile_number < 1:
        raise QualificationError("failed dual cleanup profile number is invalid")
    if (
        not isinstance(target.node_ids, tuple)
        or len(target.node_ids) != 2
        or any(
            not isinstance(node_id, str) or not node_id for node_id in target.node_ids
        )
        or len(set(target.node_ids)) != 2
        or not isinstance(target.node_to_rank, Mapping)
        or set(target.node_to_rank) != set(target.node_ids)
        or any(type(rank) is not int for rank in target.node_to_rank.values())
        or set(target.node_to_rank.values()) != {0, 1}
    ):
        raise QualificationError("failed dual cleanup must bind exactly both ranks")
    if (
        target.assigned_node_id not in target.node_to_rank
        or type(target.assigned_rank) is not int
        or target.node_to_rank[target.assigned_node_id] != target.assigned_rank
    ):
        raise QualificationError("failed canary assignment node and rank do not match")
    if (
        not isinstance(target.fleet_node_ids, tuple)
        or not target.fleet_node_ids
        or any(
            not isinstance(node_id, str) or not node_id
            for node_id in target.fleet_node_ids
        )
        or len(set(target.fleet_node_ids)) != len(target.fleet_node_ids)
        or not set(target.node_ids).issubset(target.fleet_node_ids)
    ):
        raise QualificationError("failed dual cleanup whole-Fleet roster is invalid")


def _failed_run_id(target: FailedDualCanaryTarget) -> str:
    return _required_string(target.run_id, "failed dual canary run ID")


def _failed_target_binding(target: FailedDualCanaryTarget) -> dict[str, object]:
    return {
        "schema_version": 1,
        "campaign_id": target.campaign_id,
        "batch_id": target.batch_id,
        "lane_id": target.lane_id,
        "recipe_key": target.recipe_key,
        "recipe_content_sha256": target.recipe_content_sha256,
        "package_sha256": target.package_sha256,
        "canary_record_sha256": target.canary_record_sha256,
        "application_id": target.application_id,
        "application_request_key": target.application_request_key,
        "application_updated_at": target.application_updated_at,
        "run_id": _failed_run_id(target),
        "recipe_revision_id": target.recipe_revision_id,
        "alias": target.alias,
        "node_ids": list(target.node_ids),
        "node_to_rank": dict(target.node_to_rank),
        "assigned_node_id": target.assigned_node_id,
        "assigned_rank": target.assigned_rank,
        "fleet_node_ids": sorted(target.fleet_node_ids),
        "profile_number": target.profile_number,
        "profile_id": target.profile_id,
        "profile_digest": target.profile_digest,
        "plan_digest": target.plan_digest,
    }


def _failed_cleanup_envelope(
    target: FailedDualCanaryTarget, target_digest: str
) -> dict[str, object]:
    return {
        **_failed_target_binding(target),
        "target_digest": target_digest,
    }


def _failed_cleanup_request(
    target: FailedDualCanaryTarget, target_digest: str, request_key: str
) -> dict[str, object]:
    return {
        **_failed_cleanup_envelope(target, target_digest),
        "request_key": request_key,
        "cleanup_mode": "failed-dual-lane",
        "terminal_event": "canary.failed",
        "terminal_record_sha256": target.canary_record_sha256,
        "source_application_id": target.application_id,
        "source_application_request_key": target.application_request_key,
        "source_application_updated_at": target.application_updated_at,
        "active_run_id": _failed_run_id(target),
        "stop_run_ids": [_failed_run_id(target)],
        "stop_aliases": [target.alias],
        "node_to_rank": dict(target.node_to_rank),
        "assigned_node_id": target.assigned_node_id,
        "assigned_rank": target.assigned_rank,
        "fleet_node_ids": sorted(target.fleet_node_ids),
    }


def _require_failed_canary(
    target: FailedDualCanaryTarget, ledger: EvidenceLedger
) -> Mapping[str, object]:
    matches = [
        record
        for record in ledger.records
        if record.get("record_sha256") == target.canary_record_sha256
    ]
    if len(matches) != 1:
        raise QualificationError(
            "failed canary record reference is missing or ambiguous"
        )
    record = matches[0]
    payload = _payload(record)
    payload_ranks = payload.get("node_to_rank")
    lane_outcomes = [
        item
        for item in ledger.records
        if item.get("plan_digest") == target.campaign_id
        and item.get("recipe") == target.recipe_key
        and item.get("event") in {"canary.completed", "canary.failed"}
        and _payload(item).get("batch_id") == target.batch_id
        and _payload(item).get("lane_id") == target.lane_id
    ]
    if (
        len(lane_outcomes) != 1
        or lane_outcomes[0].get("record_sha256") != target.canary_record_sha256
        or record.get("event") != "canary.failed"
        or record.get("plan_digest") != target.campaign_id
        or record.get("recipe") != target.recipe_key
        or payload.get("batch_id") != target.batch_id
        or payload.get("lane_id") != target.lane_id
        or payload.get("recipe_content_sha256") != target.recipe_content_sha256
        or payload.get("package_sha256") != target.package_sha256
        or payload.get("application_id") != target.application_id
        or payload.get("application_request_key") != target.application_request_key
        or payload.get("run_id") != _failed_run_id(target)
        or payload.get("alias") != target.alias
        or payload.get("recipe_revision_id") != target.recipe_revision_id
        or payload.get("node_ids") != list(target.node_ids)
        or not isinstance(payload_ranks, Mapping)
        or dict(payload_ranks) != dict(target.node_to_rank)
        or payload.get("assigned_node_id") != target.assigned_node_id
        or payload.get("assigned_rank") != target.assigned_rank
        or payload.get("smoke_status") != "failed"
        or not isinstance(payload.get("error"), str)
        or not payload.get("error")
    ):
        raise QualificationError(
            "failed canary record changed exact run or rank identity"
        )
    application = _mapping(
        payload.get("application"), "failed canary profile application"
    )
    expected_application_plan_digest = _digest(
        {
            "schema_version": 2,
            "reconciliation_digest": target.plan_digest,
            "retry_of_application_id": None,
            "request_key": target.application_request_key,
        }
    )
    if (
        application.get("id") != target.application_id
        or application.get("request_key") != target.application_request_key
        or application.get("state") != "succeeded"
        or application.get("profile_id") != target.profile_id
        or application.get("profile_digest") != target.profile_digest
        or application.get("plan_digest") != expected_application_plan_digest
        or application.get("updated_at") != target.application_updated_at
    ):
        raise QualificationError(
            "failed canary does not bind its succeeded source application"
        )
    _validate_failed_rank_presence(target, payload.get("fleet_rank_presence"))
    return record


def _validate_failed_rank_presence(target: FailedDualCanaryTarget, raw: object) -> None:
    if not isinstance(raw, list) or len(raw) != 2:
        raise QualificationError("failed canary lacks exact two-rank serving evidence")
    observed: dict[str, int] = {}
    for item in raw:
        row = _mapping(item, "failed canary rank presence")
        node_id = _required_string(row.get("node_id"), "failed canary rank node ID")
        rank = row.get("rank")
        if type(rank) is not int or node_id in observed:
            raise QualificationError("failed canary repeats or mis-types a rank")
        observed[node_id] = rank
        if (
            row.get("run_id") != target.run_id
            or row.get("alias") != target.alias
            or row.get("recipe_revision_id") != target.recipe_revision_id
            or row.get("expected_rank_count") != 2
            or row.get("run_state") != "running"
            or row.get("route_state") != "published"
            or row.get("healthy") is not True
            or row.get("rank_state") != "running"
            or row.get("rank_fresh") is not True
            or row.get("group_state") != "healthy"
            or not _has_exact_rank_members(row.get("present_ranks"), (0, 1))
            or not _same_string_set(row.get("member_node_ids"), target.node_ids)
        ):
            raise QualificationError(
                "failed canary serving evidence changed its exact dual run"
            )
    if observed != dict(target.node_to_rank):
        raise QualificationError(
            "failed canary serving evidence crossed Spark/rank mapping"
        )


def _failed_cleanup_request_key(
    target: FailedDualCanaryTarget, target_digest: str
) -> str:
    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"vonk-failed-dual-cleanup:{target_digest}")
    )


def _failed_cleanup_event(
    ledger: EvidenceLedger,
    target: FailedDualCanaryTarget,
    target_digest: str,
    event: str,
) -> Mapping[str, object] | None:
    matches = [
        record
        for record in ledger.records
        if record.get("plan_digest") == target.campaign_id
        and record.get("recipe") == target.recipe_key
        and record.get("event") == event
        and _payload(record).get("batch_id") == target.batch_id
        and _payload(record).get("lane_id") == target.lane_id
        and _payload(record).get("target_digest") == target_digest
    ]
    if len(matches) > 1:
        raise QualificationError(f"duplicate durable {event} records exist")
    if not matches:
        return None
    expected = _failed_cleanup_envelope(target, target_digest)
    if any(_payload(matches[0]).get(key) != value for key, value in expected.items()):
        raise QualificationError(
            f"durable {event} changed its exact failed-canary target"
        )
    return matches[0]


def _append_failed_cleanup(
    target: FailedDualCanaryTarget,
    ledger: EvidenceLedger,
    target_digest: str,
    event: str,
    details: Mapping[str, object],
) -> Mapping[str, object]:
    return ledger.append(
        event,
        plan_digest=target.campaign_id,
        recipe=target.recipe_key,
        payload={
            **_failed_cleanup_envelope(target, target_digest),
            **dict(details),
        },
    )


def _reject_failed_cleanup_conflicts(
    target: FailedDualCanaryTarget,
    ledger: EvidenceLedger,
    target_digest: str,
) -> None:
    relevant = {
        f"{_FAILED_CLEANUP_PREFIX}.intent",
        f"{_FAILED_CLEANUP_PREFIX}.plan_reviewed",
        f"{_FAILED_CLEANUP_PREFIX}.apply_requested",
        f"{_FAILED_CLEANUP_PREFIX}.completed",
        f"{_FAILED_CLEANUP_PREFIX}.reconciled",
    }
    for record in ledger.records:
        if (
            record.get("plan_digest") != target.campaign_id
            or record.get("recipe") != target.recipe_key
            or record.get("event") not in relevant
        ):
            continue
        payload = _payload(record)
        if (
            payload.get("batch_id") == target.batch_id
            and payload.get("lane_id") == target.lane_id
            and payload.get("target_digest") != target_digest
        ):
            raise QualificationError(
                "failed-canary cleanup evidence changed its exact target"
            )


def _validate_failed_cleanup_review(
    target: FailedDualCanaryTarget,
    request: Mapping[str, object],
    value: Mapping[str, object],
) -> dict[str, object]:
    expected_keys = {
        "request_key",
        "reviewed",
        "canary_record_sha256",
        "terminal_event",
        "terminal_record_sha256",
        "source_application_id",
        "source_application_request_key",
        "source_application_updated_at",
        "profile_number",
        "profile_id",
        "profile_digest",
        "plan_digest",
        "cleanup_mode",
        "active_run_id",
        "stop_run_ids",
        "stop_aliases",
        "node_ids",
        "node_to_rank",
        "assigned_node_id",
        "assigned_rank",
        "fleet_node_ids",
    }
    if set(value) != expected_keys:
        raise QualificationError("failed-canary cleanup review has an unexpected shape")
    if (
        value.get("request_key") != request.get("request_key")
        or value.get("reviewed") is not True
        or value.get("canary_record_sha256") != target.canary_record_sha256
        or value.get("terminal_event") != "canary.failed"
        or value.get("terminal_record_sha256") != target.canary_record_sha256
        or value.get("source_application_id") != target.application_id
        or value.get("source_application_request_key") != target.application_request_key
        or value.get("source_application_updated_at") != target.application_updated_at
        or value.get("profile_number") != target.profile_number
        or value.get("profile_id") != target.profile_id
        or value.get("cleanup_mode") != "failed-dual-lane"
        or value.get("active_run_id") != target.run_id
        or value.get("stop_run_ids") != [target.run_id]
        or value.get("stop_aliases") != [target.alias]
        or value.get("node_ids") != list(target.node_ids)
        or value.get("node_to_rank") != dict(target.node_to_rank)
        or value.get("assigned_node_id") != target.assigned_node_id
        or value.get("assigned_rank") != target.assigned_rank
        or value.get("fleet_node_ids") != sorted(target.fleet_node_ids)
        or type(value.get("profile_number")) is not int
        or not _is_sha(value.get("profile_digest"))
        or not _is_sha(value.get("plan_digest"))
    ):
        raise QualificationError(
            "failed-canary cleanup review changed its exact stop effects"
        )
    return dict(value)


def _load_failed_cleanup_review(
    target: FailedDualCanaryTarget,
    ledger: EvidenceLedger,
    target_digest: str,
) -> DualCleanupReview:
    request_key = _failed_cleanup_request_key(target, target_digest)
    intent = _failed_cleanup_event(
        ledger, target, target_digest, f"{_FAILED_CLEANUP_PREFIX}.intent"
    )
    if intent is None or _payload(intent).get("request_key") != request_key:
        raise QualificationError("failed-canary cleanup intent is absent or changed")
    reviewed = _failed_cleanup_event(
        ledger, target, target_digest, f"{_FAILED_CLEANUP_PREFIX}.plan_reviewed"
    )
    if reviewed is None:
        raise QualificationError("failed-canary cleanup has no durable review")
    payload = _payload(reviewed)
    receipt = _mapping(payload.get("review"), "durable failed-canary cleanup review")
    valid = _validate_failed_cleanup_review(
        target,
        _failed_cleanup_request(target, target_digest, request_key),
        receipt,
    )
    review_digest = _digest(valid)
    if (
        payload.get("request_key") != request_key
        or payload.get("review_digest") != review_digest
    ):
        raise QualificationError("failed-canary cleanup review digest is invalid")
    return DualCleanupReview(
        target_digest,
        request_key,
        review_digest,
        target.profile_number,
        target.profile_id,
        str(valid["profile_digest"]),
        str(valid["plan_digest"]),
        valid,
    )


def _validate_failed_stop_receipts(target: FailedDualCanaryTarget, raw: object) -> None:
    if not isinstance(raw, list) or len(raw) != 1:
        raise QualificationError(
            "failed-canary cleanup must contain one exact run stop receipt"
        )
    receipt = _mapping(raw[0], "failed-canary run stop receipt")
    observation = _mapping(
        receipt.get("final_observation"), "failed-canary final stop observation"
    )
    expected_ranks = [
        {"node_id": node_id, "rank": rank, "state": "stopped"}
        for node_id, rank in sorted(
            target.node_to_rank.items(), key=lambda item: item[1]
        )
    ]
    if (
        receipt.get("run_id") != target.run_id
        or receipt.get("operation_state") != "succeeded"
        or observation.get("phase") != "final_verify"
        or observation.get("final_verified") is not True
        or observation.get("run_id") != target.run_id
        or observation.get("state") != "stopped"
        or observation.get("route_state") != "withdrawn"
        or observation.get("ranks") != expected_ranks
    ):
        raise QualificationError(
            "failed-canary cleanup stop lacks exact final rank verification"
        )


def _validate_failed_cleanup_receipt(
    target: FailedDualCanaryTarget,
    review: DualCleanupReview,
    failure_record: Mapping[str, object],
    value: Mapping[str, object],
    *,
    endpoint_exists: Callable[[str], bool],
) -> tuple[Mapping[str, object], _FleetView]:
    if (
        value.get("request_key") != review.request_key
        or value.get("review_digest") != review.review_digest
        or value.get("application_state") != "succeeded"
        or not isinstance(value.get("application_id"), str)
        or not value.get("application_id")
        or value.get("profile_number") != review.profile_number
        or value.get("profile_id") != review.profile_id
        or value.get("profile_digest") != review.profile_digest
        or value.get("plan_digest") != review.plan_digest
        or value.get("cleanup_mode") != "failed-dual-lane"
        or value.get("terminal_event") != "canary.failed"
        or value.get("terminal_record_sha256") != target.canary_record_sha256
        or value.get("canary_record_sha256") != target.canary_record_sha256
        or value.get("source_application_id") != target.application_id
        or value.get("source_application_request_key") != target.application_request_key
        or value.get("source_application_updated_at") != target.application_updated_at
        or value.get("active_run_id") != target.run_id
        or value.get("stop_run_ids") != [target.run_id]
        or value.get("stop_aliases") != [target.alias]
        or value.get("node_ids") != list(target.node_ids)
        or value.get("node_to_rank") != dict(target.node_to_rank)
        or value.get("assigned_node_id") != target.assigned_node_id
        or value.get("assigned_rank") != target.assigned_rank
        or value.get("fleet_node_ids") != sorted(target.fleet_node_ids)
    ):
        raise QualificationError(
            "failed-canary cleanup receipt changed its reviewed identity"
        )
    _timestamp(
        value.get("application_updated_at"), "failed cleanup application update time"
    )
    _validate_failed_stop_receipts(target, value.get("stop_receipts"))
    view = _fleet_view(value.get("fleet_snapshot"), target.fleet_node_ids)
    source_app_updated_at = _timestamp(
        target.application_updated_at, "failed canary source application update time"
    )
    cleanup_app_updated_at = _timestamp(
        value.get("application_updated_at"), "failed cleanup application update time"
    )
    if view.generated_at < max(source_app_updated_at, cleanup_app_updated_at):
        raise QualificationError(
            "failed cleanup FleetSnapshot predates its application"
        )
    _require_idle_fleet(target, view)
    if not _all_selected_live(view, target):
        raise QualificationError(
            "failed cleanup Fleet lacks live telemetry from both selected Sparks"
        )
    if _endpoint_presence(endpoint_exists, target.alias):
        raise QualificationError(
            "failed dual cleanup left the exact recipe route published"
        )
    if failure_record.get("record_sha256") != target.canary_record_sha256:
        raise QualificationError(
            "failed cleanup receipt lost its canary failure source"
        )
    return value, view


def _validate_failed_cleanup_event(
    target: FailedDualCanaryTarget,
    target_digest: str,
    ledger: EvidenceLedger,
    record: Mapping[str, object],
) -> tuple[Mapping[str, object], _FleetView]:
    review = _load_failed_cleanup_review(target, ledger, target_digest)
    payload = _payload(record)
    if (
        payload.get("request_key") != review.request_key
        or payload.get("review_digest") != review.review_digest
        or payload.get("terminal_event") != "canary.failed"
        or payload.get("terminal_record_sha256") != target.canary_record_sha256
    ):
        raise QualificationError("durable failed cleanup changed its reviewed identity")
    receipt = _mapping(payload.get("receipt"), "durable failed-canary cleanup receipt")
    failure = _require_failed_canary(target, ledger)
    return _validate_failed_cleanup_receipt(
        target,
        review,
        failure,
        receipt,
        endpoint_exists=lambda _alias: False,
    )


def _validate_failed_cleanup_reconciliation(
    target: FailedDualCanaryTarget,
    review: DualCleanupReview,
    failure_record: Mapping[str, object],
    prior: Mapping[str, object],
    value: Mapping[str, object],
    *,
    endpoint_exists: Callable[[str], bool],
) -> _FleetView:
    if (
        value.get("request_key") != review.request_key
        or value.get("review_digest") != review.review_digest
        or value.get("application_id") != prior.get("application_id")
        or _timestamp(
            value.get("application_updated_at"), "reconciled cleanup update time"
        )
        != _timestamp(
            prior.get("application_updated_at"), "completed cleanup update time"
        )
        or value.get("stop_receipts") != prior.get("stop_receipts")
    ):
        raise QualificationError(
            "failed cleanup reconciliation changed the original application"
        )
    _current_receipt, current_view = _validate_failed_cleanup_receipt(
        target,
        review,
        failure_record,
        value,
        endpoint_exists=endpoint_exists,
    )
    _require_snapshot_after(
        current_view,
        _mapping(prior.get("fleet_snapshot"), "prior failed cleanup FleetSnapshot"),
        "failed cleanup reconciliation",
    )
    _require_idle_fleet(target, current_view)
    if not _all_selected_live(current_view, target):
        raise QualificationError(
            "failed cleanup reconciliation lacks live selected Sparks"
        )
    _require_same_authority(
        current_view,
        _required_string(
            _mapping(
                prior.get("fleet_snapshot"), "prior failed cleanup FleetSnapshot"
            ).get("authority_revision"),
            "prior failed cleanup Fleet authority",
        ),
    )
    if _endpoint_presence(endpoint_exists, target.alias):
        raise QualificationError(
            "reconciled failed cleanup left the recipe route published"
        )
    return current_view


def _validate_target(target: DualRecoveryTarget) -> None:
    for label, value in (
        ("campaign ID", target.campaign_id),
        ("recipe content digest", target.recipe_content_sha256),
        ("package digest", target.package_sha256),
        ("canary record digest", target.canary_record_sha256),
        ("profile digest", target.profile_digest),
        ("plan digest", target.plan_digest),
    ):
        _sha(value, f"dual recovery {label}")
    if not isinstance(target.batch_id, str) or not target.batch_id:
        raise QualificationError("dual recovery batch ID is invalid")
    if type(target.lane_id) is not int or target.lane_id < 1:
        raise QualificationError("dual recovery lane ID is invalid")
    if not isinstance(target.recipe_key, str) or "/" not in target.recipe_key:
        raise QualificationError("dual recovery recipe key is invalid")
    for label, value in (
        ("run ID", target.run_id),
        ("recipe revision ID", target.recipe_revision_id),
        ("route alias", target.alias),
        ("profile ID", target.profile_id),
    ):
        _required_string(value, f"dual recovery {label}")
    if type(target.profile_number) is not int or target.profile_number < 1:
        raise QualificationError("dual recovery profile number is invalid")
    _required_string(target.failure_node_id, "dual recovery failure Spark ID")
    if (
        not isinstance(target.node_ids, tuple)
        or len(target.node_ids) != 2
        or any(not isinstance(item, str) or not item for item in target.node_ids)
        or len(set(target.node_ids)) != 2
    ):
        raise QualificationError(
            "dual recovery must bind exactly two distinct Spark IDs"
        )
    if (
        not isinstance(target.node_to_rank, Mapping)
        or set(target.node_to_rank) != set(target.node_ids)
        or any(type(rank) is not int for rank in target.node_to_rank.values())
        or set(target.node_to_rank.values()) != {0, 1}
    ):
        raise QualificationError(
            "dual recovery rank map must bind both nodes to ranks 0 and 1"
        )
    if target.failure_node_id not in target.node_to_rank:
        raise QualificationError(
            "dual recovery failure node is outside the exact rank map"
        )
    if (
        not isinstance(target.fleet_node_ids, tuple)
        or not target.fleet_node_ids
        or any(not isinstance(item, str) or not item for item in target.fleet_node_ids)
        or len(set(target.fleet_node_ids)) != len(target.fleet_node_ids)
        or not set(target.node_ids).issubset(target.fleet_node_ids)
    ):
        raise QualificationError("dual recovery whole-Fleet roster is invalid")
    if (
        not isinstance(target.smoke_case_ids, tuple)
        or not target.smoke_case_ids
        or any(not isinstance(item, str) or not item for item in target.smoke_case_ids)
        or len(set(target.smoke_case_ids)) != len(target.smoke_case_ids)
    ):
        raise QualificationError("dual recovery smoke cases must be exact and nonempty")


def _target_binding(target: DualRecoveryTarget) -> dict[str, object]:
    return {
        "schema_version": 1,
        "campaign_id": target.campaign_id,
        "batch_id": target.batch_id,
        "lane_id": target.lane_id,
        "recipe_key": target.recipe_key,
        "recipe_content_sha256": target.recipe_content_sha256,
        "package_sha256": target.package_sha256,
        "canary_record_sha256": target.canary_record_sha256,
        "run_id": target.run_id,
        "recipe_revision_id": target.recipe_revision_id,
        "alias": target.alias,
        "node_ids": list(target.node_ids),
        "node_to_rank": dict(target.node_to_rank),
        "failure_node_id": target.failure_node_id,
        "smoke_case_ids": list(target.smoke_case_ids),
        "fleet_node_ids": sorted(target.fleet_node_ids),
        "profile_number": target.profile_number,
        "profile_id": target.profile_id,
        "profile_digest": target.profile_digest,
        "plan_digest": target.plan_digest,
    }


def _envelope(target: DualRecoveryTarget, target_digest: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "campaign_id": target.campaign_id,
        "batch_id": target.batch_id,
        "lane_id": target.lane_id,
        "target_digest": target_digest,
        "recipe_content_sha256": target.recipe_content_sha256,
        "package_sha256": target.package_sha256,
        "canary_record_sha256": target.canary_record_sha256,
        "run_id": target.run_id,
        "recipe_revision_id": target.recipe_revision_id,
        "alias": target.alias,
        "node_ids": list(target.node_ids),
        "node_to_rank": dict(target.node_to_rank),
        "failure_node_id": target.failure_node_id,
        "smoke_case_ids": list(target.smoke_case_ids),
        "fleet_node_ids": sorted(target.fleet_node_ids),
        "profile_number": target.profile_number,
        "profile_id": target.profile_id,
        "profile_digest": target.profile_digest,
        "plan_digest": target.plan_digest,
    }


def _require_canary(
    target: DualRecoveryTarget, ledger: EvidenceLedger | None
) -> Mapping[str, object]:
    if ledger is None:
        raise QualificationError("dual recovery requires its durable canary ledger")
    matches = [
        record
        for record in ledger.records
        if record.get("record_sha256") == target.canary_record_sha256
    ]
    if len(matches) != 1:
        raise QualificationError("dual canary record reference is missing or ambiguous")
    record = matches[0]
    payload = _payload(record)
    ranks = payload.get("node_to_rank")
    node_ids = payload.get("node_ids")
    if (
        record.get("event") != "canary.completed"
        or record.get("plan_digest") != target.campaign_id
        or record.get("recipe") != target.recipe_key
        or payload.get("batch_id") != target.batch_id
        or payload.get("lane_id") != target.lane_id
        or payload.get("recipe_content_sha256") != target.recipe_content_sha256
        or payload.get("package_sha256") != target.package_sha256
        or payload.get("run_id") != target.run_id
        or payload.get("recipe_revision_id") != target.recipe_revision_id
        or payload.get("alias") != target.alias
        or node_ids != list(target.node_ids)
        or not isinstance(ranks, Mapping)
        or dict(ranks) != dict(target.node_to_rank)
    ):
        raise QualificationError(
            "dual canary reference changed its recipe, lane, run, alias, or Spark ranks"
        )
    smoke = _mapping(payload.get("smoke"), "dual canary smoke receipt")
    cases = smoke.get("cases")
    if isinstance(cases, list):
        case_ids = [
            _required_string(
                _mapping(item, "canary smoke case").get("case_id"),
                "canary smoke case ID",
            )
            for item in cases
        ]
    elif isinstance(smoke.get("case_id"), str):
        case_ids = [str(smoke["case_id"])]
    else:
        case_ids = []
    if case_ids != list(target.smoke_case_ids):
        raise QualificationError(
            "dual canary does not prove its exact reviewed smoke cases"
        )
    return record


def _require_pending_rank_loss(
    target: DualRecoveryTarget, ledger: EvidenceLedger
) -> Mapping[str, object]:
    matches = [
        record
        for record in ledger.records
        if record.get("plan_digest") == target.campaign_id
        and record.get("recipe") == target.recipe_key
        and record.get("event") == "rank-loss.pending"
        and _payload(record).get("batch_id") == target.batch_id
    ]
    if len(matches) != 1:
        raise QualificationError("dual rank-loss target is missing or ambiguous")
    record = matches[0]
    payload = _payload(record)
    payload_ranks = payload.get("node_to_rank")
    if (
        payload.get("lane_id") != target.lane_id
        or payload.get("failure_spark") != target.failure_node_id
        or payload.get("run_id") != target.run_id
        or payload.get("revision_id") != target.recipe_revision_id
        or payload.get("alias") != target.alias
        or payload.get("node_ids") != list(target.node_ids)
        or not isinstance(payload_ranks, Mapping)
        or dict(payload_ranks) != dict(target.node_to_rank)
    ):
        raise QualificationError(
            "durable dual rank-loss target changed exact run or ranks"
        )
    return record


def _rank_loss_proof(
    target: DualRecoveryTarget, view: _FleetView
) -> tuple[bool, dict[str, object]]:
    _require_exclusive_run(target, view)
    records = _run_presences(view, target)
    failed_node = view.nodes[target.failure_node_id]
    failed_rank = target.node_to_rank[target.failure_node_id]
    surviving_rank = 1 - failed_rank
    survivors = [
        (node_id, presence)
        for node_id, presence in records
        if node_id != target.failure_node_id
    ]
    failures = [
        presence for node_id, presence in records if node_id == target.failure_node_id
    ]
    if len(survivors) != 1 or len(failures) > 1:
        return False, {}
    node_id, presence = survivors[0]
    if (
        not _node_live(view.nodes[node_id])
        or presence.get("recipe_revision_id") != target.recipe_revision_id
        or presence.get("alias") != target.alias
        or presence.get("rank") != surviving_rank
        or presence.get("expected_rank_count") != 2
        or presence.get("group_state") != "degraded"
        or presence.get("healthy") is not False
        or presence.get("route_state") not in {"published", "withdrawn"}
        or not _contains_exact_rank_sets(
            presence.get("present_ranks"),
            surviving_rank,
            failed_rank,
            failed_state=bool(failures) or failed_node.online_state == "offline",
        )
    ):
        return False, {}
    if failures:
        failed = failures[0]
        if (
            failed.get("recipe_revision_id") != target.recipe_revision_id
            or failed.get("alias") != target.alias
            or failed.get("rank") != failed_rank
            or failed.get("expected_rank_count") != 2
            or failed.get("group_state") != "degraded"
            or failed.get("healthy") is not False
            or failed.get("route_state") not in {"published", "withdrawn"}
            or failed.get("rank_state") not in {"lost", "failed", "stopped"}
        ):
            return False, {}
    elif failed_node.online_state != "offline":
        return False, {}
    if failed_node.online_state not in {"offline", "online"}:
        return False, {}
    proof = {
        "failure_node_id": target.failure_node_id,
        "failure_rank": failed_rank,
        "expected_present_ranks": [surviving_rank],
        "survivors": [{"node_id": node_id, **dict(presence)}],
        "failed_rank_presence": [dict(item) for item in failures],
        "failed_node_online_state": failed_node.online_state,
    }
    return True, proof


def _contains_exact_rank_sets(
    raw: object,
    surviving_rank: int,
    failed_rank: int,
    *,
    failed_state: bool,
) -> bool:
    if (
        not isinstance(raw, list)
        or any(type(item) is not int for item in raw)
        or len(raw) != len(set(raw))
    ):
        return False
    observed = set(raw)
    return observed == {surviving_rank} or (
        failed_state and observed == {surviving_rank, failed_rank}
    )


def _has_exact_rank_members(raw: object, expected: tuple[int, ...]) -> bool:
    return (
        isinstance(raw, list)
        and all(type(item) is int for item in raw)
        and len(raw) == len(expected)
        and set(raw) == set(expected)
    )


def _same_string_set(raw: object, expected: Sequence[str]) -> bool:
    return (
        isinstance(raw, list)
        and all(isinstance(item, str) and item for item in raw)
        and len(raw) == len(expected)
        and len(set(raw)) == len(raw)
        and set(raw) == set(expected)
    )


def _validate_rank_loss_event(
    target: DualRecoveryTarget,
    target_digest: str,
    record: Mapping[str, object],
) -> None:
    payload = _payload(record)
    proof = _mapping(payload.get("proof"), "rank-loss proof")
    if (
        payload.get("target_digest") != target_digest
        or payload.get("canary_record_sha256") != target.canary_record_sha256
        or proof.get("failure_node_id") != target.failure_node_id
        or proof.get("failure_rank") != target.node_to_rank[target.failure_node_id]
        or proof.get("expected_present_ranks")
        != [1 - target.node_to_rank[target.failure_node_id]]
        or proof.get("failed_node_online_state") not in {"offline", "online"}
    ):
        raise QualificationError(
            "durable rank-loss proof changed exact failure identity"
        )
    survivors = proof.get("survivors")
    failed = proof.get("failed_rank_presence")
    if (
        not isinstance(survivors, list)
        or len(survivors) != 1
        or not isinstance(failed, list)
    ):
        raise QualificationError(
            "durable rank-loss proof lacks exact per-rank evidence"
        )
    survivor = _mapping(survivors[0], "rank-loss survivor")
    expected_survivor = next(
        node_id for node_id in target.node_ids if node_id != target.failure_node_id
    )
    if (
        survivor.get("node_id") != expected_survivor
        or survivor.get("rank") != target.node_to_rank[expected_survivor]
        or survivor.get("recipe_revision_id") != target.recipe_revision_id
        or survivor.get("alias") != target.alias
        or survivor.get("route_state") not in {"published", "withdrawn"}
        or survivor.get("healthy") is not False
        or survivor.get("group_state") != "degraded"
    ):
        raise QualificationError("durable surviving rank is misattributed")
    if failed:
        failed_presence = _mapping(failed[0], "failed rank presence")
        if (
            len(failed) != 1
            or failed_presence.get("rank")
            != target.node_to_rank[target.failure_node_id]
            or failed_presence.get("recipe_revision_id") != target.recipe_revision_id
            or failed_presence.get("alias") != target.alias
            or failed_presence.get("rank_state") not in {"lost", "failed", "stopped"}
            or failed_presence.get("route_state") not in {"published", "withdrawn"}
        ):
            raise QualificationError("durable failed rank is misattributed")
    elif proof.get("failed_node_online_state") != "offline":
        raise QualificationError("missing failure rank is not proven offline")
    _snapshot_identity_from_payload(payload, "rank-loss observation")


def _validate_route_withdrawal_event(
    target: DualRecoveryTarget,
    target_digest: str,
    rank_loss: Mapping[str, object],
    record: Mapping[str, object],
) -> None:
    payload = _payload(record)
    if (
        payload.get("target_digest") != target_digest
        or payload.get("rank_loss_record_sha256") != rank_loss.get("record_sha256")
        or payload.get("endpoint_not_found") is not True
    ):
        raise QualificationError(
            "durable route-withdrawal proof changed its rank-loss source"
        )
    raw_snapshot = _mapping(
        payload.get("fleet_snapshot"), "route-withdrawal FleetSnapshot"
    )
    view = _fleet_view(raw_snapshot, target.fleet_node_ids)
    _require_snapshot_after(view, _payload(rank_loss), "route withdrawal")
    lost, proof = _rank_loss_proof(target, view)
    if not lost:
        raise QualificationError(
            "route-withdrawal observation no longer proves the exact rank loss"
        )
    presences = _run_presences(view, target)
    if not presences or any(
        presence.get("route_state") != "withdrawn" for _node_id, presence in presences
    ):
        raise QualificationError(
            "durable route-withdrawal proof lacks exact withdrawn Fleet routes"
        )
    observed = [
        {"node_id": node_id, **dict(presence)} for node_id, presence in presences
    ]
    if payload.get("proof") != proof or payload.get("fleet_rank_presence") != observed:
        raise QualificationError(
            "durable route-withdrawal evidence changed its exact Fleet proof"
        )
    identity = _snapshot_identity(view)
    if any(payload.get(key) != value for key, value in identity.items()):
        raise QualificationError(
            "route-withdrawal identity differs from its FleetSnapshot"
        )
    _snapshot_identity_from_payload(payload, "route-withdrawal observation")


def _serving_dual_run(target: DualRecoveryTarget, view: _FleetView) -> bool:
    _require_exclusive_run(target, view)
    records = _run_presences(view, target)
    if len(records) != 2 or {node_id for node_id, _presence in records} != set(
        target.node_ids
    ):
        return False
    if any(not _node_live(view.nodes[node_id]) for node_id in target.node_ids):
        return False
    for node_id, presence in records:
        if (
            presence.get("alias") != target.alias
            or presence.get("recipe_revision_id") != target.recipe_revision_id
            or presence.get("rank") != target.node_to_rank[node_id]
            or presence.get("expected_rank_count") != 2
            or presence.get("run_state") != "running"
            or presence.get("route_state") != "published"
            or presence.get("rank_state") != "running"
            or presence.get("rank_fresh") is not True
            or presence.get("group_state") != "healthy"
            or presence.get("healthy") is not True
            or not _has_exact_rank_members(presence.get("present_ranks"), (0, 1))
            or not _same_string_set(presence.get("member_node_ids"), target.node_ids)
        ):
            return False
    return True


def _validate_serving_receipt(
    target: DualRecoveryTarget, value: Mapping[str, object]
) -> None:
    receipt_ranks = value.get("node_to_rank")
    endpoint = value.get("endpoint")
    if (
        value.get("state") != "succeeded"
        or value.get("run_id") != target.run_id
        or value.get("recipe_key") != target.recipe_key
        or value.get("recipe_content_sha256") != target.recipe_content_sha256
        or value.get("package_sha256") != target.package_sha256
        or value.get("recipe_revision_id") != target.recipe_revision_id
        or value.get("alias") != target.alias
        or not isinstance(receipt_ranks, Mapping)
        or dict(receipt_ranks) != dict(target.node_to_rank)
        or not isinstance(endpoint, Mapping)
        or not isinstance(endpoint.get("api_base"), str)
    ):
        raise QualificationError(
            "recovered serving receipt changed the exact dual assignment"
        )
    _validate_rank_presence_list(
        target,
        value.get("rank_presence"),
        expected_state="running",
        expected_route="published",
        expected_health=True,
    )


def _validate_rank_presence_list(
    target: DualRecoveryTarget | FailedDualCanaryTarget,
    raw: object,
    *,
    expected_state: str,
    expected_route: str,
    expected_health: bool,
) -> None:
    if not isinstance(raw, list) or len(raw) != 2:
        raise QualificationError(
            "rank presence receipt does not cover exactly two Sparks"
        )
    bindings: dict[str, int] = {}
    for item in raw:
        row = _mapping(item, "rank presence receipt")
        node_id = _required_string(row.get("node_id"), "rank presence Spark ID")
        rank = row.get("rank")
        if type(rank) is not int or node_id in bindings:
            raise QualificationError(
                "rank presence receipt repeats or mis-types a rank"
            )
        bindings[node_id] = rank
        if (
            row.get("run_id") != target.run_id
            or row.get("alias") != target.alias
            or row.get("recipe_revision_id") != target.recipe_revision_id
            or row.get("expected_rank_count") != 2
            or row.get("run_state") != expected_state
            or row.get("route_state") != expected_route
            or row.get("healthy") is not expected_health
        ):
            raise QualificationError(
                "rank presence receipt changed run identity or health"
            )
        if expected_health and (
            row.get("rank_state") != "running"
            or row.get("rank_fresh") is not True
            or row.get("group_state") != "healthy"
            or not _has_exact_rank_members(row.get("present_ranks"), (0, 1))
            or not _same_string_set(row.get("member_node_ids"), target.node_ids)
        ):
            raise QualificationError(
                "rank presence receipt does not prove a fresh healthy group"
            )
    if bindings != dict(target.node_to_rank):
        raise QualificationError("rank presence receipt crossed Spark/rank assignments")


def _validate_smoke_receipt(
    target: DualRecoveryTarget,
    value: Mapping[str, object],
    *,
    node_id: str,
    rank: int,
) -> None:
    if (
        value.get("passed") is not True
        or value.get("run_id") != target.run_id
        or value.get("alias") != target.alias
        or value.get("node_id") != node_id
        or value.get("rank") != rank
        or value.get("case_ids") != list(target.smoke_case_ids)
        or not _is_sha(value.get("record_sha256"))
    ):
        raise QualificationError(
            "fixture smoke receipt changed its run, rank, or cases"
        )
    fixture = _mapping(value.get("fixture_receipt"), "fixture smoke receipt")
    if fixture.get("endpoint_alias") != target.alias or fixture.get(
        "recipe_content_sha256"
    ) not in (None, target.recipe_content_sha256):
        raise QualificationError(
            "fixture smoke receipt changed route or recipe content"
        )
    cases = fixture.get("cases")
    if isinstance(cases, list):
        observed = [
            _required_string(
                _mapping(item, "fixture smoke case").get("case_id"),
                "fixture smoke case ID",
            )
            for item in cases
        ]
    elif isinstance(fixture.get("case_id"), str):
        observed = [str(fixture["case_id"])]
    else:
        raise QualificationError("fixture smoke receipt omits its completed cases")
    if observed != list(target.smoke_case_ids):
        raise QualificationError(
            "fixture smoke receipt does not cover every reviewed case"
        )


def _validate_rank_recovery_event(
    target: DualRecoveryTarget,
    target_digest: str,
    ledger: EvidenceLedger,
    record: Mapping[str, object],
) -> None:
    payload = _payload(record)
    rank_loss = _event(ledger, target, target_digest, _RANK_LOSS_EVENT)
    route_withdrawal = _event(ledger, target, target_digest, _ROUTE_WITHDRAWAL_EVENT)
    if rank_loss is None or route_withdrawal is None:
        raise QualificationError(
            "dual recovery lacks distinct rank-loss and route-withdrawal sources"
        )
    _validate_rank_loss_event(target, target_digest, rank_loss)
    _validate_route_withdrawal_event(target, target_digest, rank_loss, route_withdrawal)
    node_smoke_refs = payload.get("node_smoke_record_sha256s")
    smoke_value = payload.get("smoke")
    if (
        payload.get("target_digest") != target_digest
        or payload.get("canary_record_sha256") != target.canary_record_sha256
        or not isinstance(node_smoke_refs, Mapping)
        or set(node_smoke_refs) != set(target.node_ids)
        or not isinstance(smoke_value, Mapping)
    ):
        raise QualificationError("dual recovery smoke record changed its exact target")
    smoke = _mapping(smoke_value, "dual recovery smoke")
    cases = smoke.get("cases")
    if (
        smoke.get("endpoint_alias") != target.alias
        or smoke.get("recipe_content_sha256") != target.recipe_content_sha256
        or not isinstance(cases, list)
        or [_mapping(item, "recovery smoke case").get("case_id") for item in cases]
        != list(target.smoke_case_ids)
    ):
        raise QualificationError(
            "dual recovery smoke does not cover the exact recipe cases"
        )
    _validate_rank_presence_list(
        target,
        payload.get("fleet_rank_presence"),
        expected_state="running",
        expected_route="published",
        expected_health=True,
    )
    _smoke_nodes(ledger, target, target_digest)
    node_receipts = _smoke_receipts(target, ledger, target_digest)
    for node_id, _rank in _rank_order(target):
        receipt = node_receipts[node_id]
        if receipt.get("record_sha256") != node_smoke_refs.get(node_id):
            raise QualificationError(
                "dual recovery aggregate crossed a rank smoke receipt"
            )
    recovery_identity = _snapshot_identity_from_payload(payload, "rank recovery smoke")
    route_identity = _snapshot_identity_from_payload(
        _payload(route_withdrawal), "route-withdrawal observation"
    )
    if (
        _timestamp(recovery_identity["generated_at"], "rank recovery generated_at")
        <= _timestamp(route_identity["generated_at"], "route withdrawal generated_at")
        or _cursor(recovery_identity.get("event_cursor"), "rank recovery event cursor")
        < _cursor(route_identity.get("event_cursor"), "route withdrawal event cursor")
        or recovery_identity["authority_revision"]
        != route_identity["authority_revision"]
    ):
        raise QualificationError(
            "rank recovery smoke is not newer than route withdrawal"
        )


def _require_rank_recovery_complete(
    target: DualRecoveryTarget, ledger: EvidenceLedger, target_digest: str
) -> Mapping[str, object]:
    _require_canary(target, ledger)
    _require_pending_rank_loss(target, ledger)
    rank_loss = _event(ledger, target, target_digest, _RANK_LOSS_EVENT)
    if rank_loss is None:
        raise QualificationError("dual cleanup requires observed rank loss")
    _validate_rank_loss_event(target, target_digest, rank_loss)
    route_withdrawal = _event(ledger, target, target_digest, _ROUTE_WITHDRAWAL_EVENT)
    if route_withdrawal is None:
        raise QualificationError(
            "dual cleanup requires a separate observed route withdrawal"
        )
    _validate_route_withdrawal_event(target, target_digest, rank_loss, route_withdrawal)
    recovery = _event(ledger, target, target_digest, _RANK_RECOVERY_EVENT)
    if recovery is None:
        raise QualificationError("dual cleanup requires recovered serving and smoke")
    _validate_rank_recovery_event(target, target_digest, ledger, recovery)
    return recovery


def _rank_recovery_sha(
    target: DualRecoveryTarget, ledger: EvidenceLedger, target_digest: str
) -> str:
    record = _require_rank_recovery_complete(target, ledger, target_digest)
    return _required_string(record.get("record_sha256"), "rank-recovery record digest")


def _rank_smoke_binding(
    target: DualRecoveryTarget,
    ledger: EvidenceLedger,
    target_digest: str,
    node_id: str,
) -> dict[str, object]:
    if node_id not in target.node_to_rank:
        raise QualificationError("rank smoke reference is outside the dual assignment")
    recovery = _event(ledger, target, target_digest, _RANK_RECOVERY_EVENT)
    smoke = _node_event(
        ledger,
        target,
        target_digest,
        "rank-recovery.smoke-node-completed",
        node_id,
    )
    if recovery is None or smoke is None:
        raise QualificationError("host restart lacks its exact recovered-run smoke")
    _validate_rank_recovery_event(target, target_digest, ledger, recovery)
    smoke_payload = _payload(smoke)
    return {
        "rank_recovery_record_sha256": _required_string(
            recovery.get("record_sha256"), "rank recovery event digest"
        ),
        "rank_smoke_rank": target.node_to_rank[node_id],
        "rank_smoke_event_sha256": _required_string(
            smoke.get("record_sha256"), "rank smoke evidence event digest"
        ),
        "smoke_record_sha256": _sha(
            smoke_payload.get("record_sha256"), "rank smoke receipt digest"
        ),
    }


def _rank_smoke_refs(
    target: DualRecoveryTarget,
    ledger: EvidenceLedger,
    target_digest: str,
) -> dict[str, dict[str, object]]:
    return {
        node_id: _rank_smoke_binding(target, ledger, target_digest, node_id)
        for node_id, _rank in _rank_order(target)
    }


def _validate_cleanup_review(
    target: DualRecoveryTarget,
    request_key: str,
    request: Mapping[str, object],
    value: Mapping[str, object],
) -> dict[str, object]:
    expected_keys = {
        "request_key",
        "reviewed",
        "profile_number",
        "profile_id",
        "profile_digest",
        "plan_digest",
        "cleanup_mode",
        "terminal_event",
        "terminal_record_sha256",
        "active_run_id",
        "stop_run_ids",
        "stop_aliases",
        "fleet_node_ids",
    }
    if set(value) != expected_keys:
        raise QualificationError("dual cleanup review has an unexpected shape")
    if (
        value.get("request_key") != request_key
        or value.get("reviewed") is not True
        or value.get("cleanup_mode") != "dual-lane"
        or value.get("terminal_event") != _CLEANUP_TERMINAL_EVENT
        or value.get("terminal_record_sha256") != request.get("terminal_record_sha256")
        or value.get("active_run_id") != target.run_id
        or value.get("stop_run_ids") != [target.run_id]
        or value.get("stop_aliases") != [target.alias]
        or value.get("fleet_node_ids") != sorted(target.fleet_node_ids)
        or value.get("profile_number") != target.profile_number
        or value.get("profile_id") != target.profile_id
        or type(value.get("profile_number")) is not int
        or not _is_sha(value.get("profile_digest"))
        or not _is_sha(value.get("plan_digest"))
    ):
        raise QualificationError("dual cleanup review changed its exact stop effects")
    return dict(value)


def _load_cleanup_review(
    target: DualRecoveryTarget, ledger: EvidenceLedger, target_digest: str
) -> DualCleanupReview:
    request_key = _cleanup_request_key(target, target_digest)
    intent = _event(ledger, target, target_digest, "dual_recovery.cleanup.intent")
    if intent is None:
        raise QualificationError("dual cleanup intent is absent")
    if _payload(intent).get("request_key") != request_key:
        raise QualificationError("dual cleanup intent has an invalid request key")
    reviewed = _event(
        ledger, target, target_digest, "dual_recovery.cleanup.plan_reviewed"
    )
    if reviewed is None:
        raise QualificationError("dual cleanup has no durable review")
    payload = _payload(reviewed)
    receipt = _mapping(payload.get("review"), "durable dual cleanup review")
    valid = _validate_cleanup_review(
        target,
        request_key,
        {"terminal_record_sha256": _rank_recovery_sha(target, ledger, target_digest)},
        receipt,
    )
    digest = _digest(valid)
    if (
        payload.get("request_key") != request_key
        or payload.get("review_digest") != digest
    ):
        raise QualificationError("durable dual cleanup review digest is invalid")
    return DualCleanupReview(
        target_digest,
        request_key,
        digest,
        target.profile_number,
        target.profile_id,
        str(valid["profile_digest"]),
        str(valid["plan_digest"]),
        valid,
    )


def _validate_cleanup_receipt(
    target: DualRecoveryTarget,
    ledger: EvidenceLedger,
    review: DualCleanupReview,
    value: Mapping[str, object],
    *,
    endpoint_exists: Callable[[str], bool],
) -> tuple[Mapping[str, object], _FleetView]:
    if (
        value.get("request_key") != review.request_key
        or value.get("review_digest") != review.review_digest
        or value.get("application_state") != "succeeded"
        or not isinstance(value.get("application_id"), str)
        or not value.get("application_id")
        or value.get("profile_number") != review.profile_number
        or value.get("profile_id") != review.profile_id
        or value.get("profile_digest") != review.profile_digest
        or value.get("plan_digest") != review.plan_digest
    ):
        raise QualificationError(
            "dual cleanup application differs from its exact review"
        )
    application_updated_at = _timestamp(
        value.get("application_updated_at"), "cleanup application update time"
    )
    _validate_stop_receipts(target, value.get("stop_receipts"))
    view = _fleet_view(value.get("fleet_snapshot"), target.fleet_node_ids)
    prior_recovery = _event(ledger, target, review.target_digest, _RANK_RECOVERY_EVENT)
    if prior_recovery is None:
        raise QualificationError("dual cleanup has no recovered serving source")
    _require_snapshot_after(view, _payload(prior_recovery), "dual cleanup")
    if view.generated_at < application_updated_at:
        raise QualificationError(
            "cleanup FleetSnapshot predates the successful application"
        )
    _require_idle_fleet(target, view)
    if _endpoint_presence(endpoint_exists, target.alias):
        raise QualificationError("dual cleanup left the exact recipe route published")
    if not _all_selected_live(view, target):
        raise QualificationError(
            "dual cleanup baseline lacks live telemetry from both Sparks"
        )
    return value, view


def _validate_stop_receipts(
    target: DualRecoveryTarget | FailedDualCanaryTarget, raw: object
) -> None:
    if not isinstance(raw, list) or len(raw) != 1:
        raise QualificationError("dual cleanup must contain one exact run stop receipt")
    receipt = _mapping(raw[0], "dual run stop receipt")
    observation = _mapping(receipt.get("final_observation"), "final stop observation")
    ranks = observation.get("ranks")
    expected_ranks = [
        {"node_id": node_id, "rank": target.node_to_rank[node_id], "state": "stopped"}
        for node_id, _rank in _rank_order(target)
    ]
    if (
        receipt.get("run_id") != target.run_id
        or receipt.get("operation_state") != "succeeded"
        or observation.get("phase") != "final_verify"
        or observation.get("final_verified") is not True
        or observation.get("run_id") != target.run_id
        or observation.get("state") != "stopped"
        or observation.get("route_state") != "withdrawn"
        or ranks != expected_ranks
    ):
        raise QualificationError(
            "dual cleanup stop receipt lacks exact final rank verification"
        )


def _validate_cleanup_event(
    target: DualRecoveryTarget,
    target_digest: str,
    ledger: EvidenceLedger,
    record: Mapping[str, object],
) -> tuple[Mapping[str, object], _FleetView]:
    payload = _payload(record)
    review = _load_cleanup_review(target, ledger, target_digest)
    if (
        payload.get("target_digest") != target_digest
        or payload.get("request_key") != review.request_key
        or payload.get("review_digest") != review.review_digest
        or payload.get("terminal_event") != _CLEANUP_TERMINAL_EVENT
        or payload.get("terminal_record_sha256")
        != _rank_recovery_sha(target, ledger, target_digest)
    ):
        raise QualificationError(
            "durable cleanup completion changed its reviewed identity"
        )
    receipt = _mapping(payload.get("receipt"), "durable dual cleanup receipt")
    # Revalidate the cryptographically tied receipt without another remote read.
    _validate_cleanup_receipt_fields(target, ledger, review, receipt)
    view = _fleet_view(receipt.get("fleet_snapshot"), target.fleet_node_ids)
    if view.generated_at < _timestamp(
        receipt.get("application_updated_at"), "cleanup application update time"
    ):
        raise QualificationError("persisted cleanup snapshot predates its application")
    _require_idle_fleet(target, view)
    if not _all_selected_live(view, target):
        raise QualificationError("persisted cleanup baseline lacks live selected hosts")
    return receipt, view


def _validate_cleanup_receipt_fields(
    target: DualRecoveryTarget,
    ledger: EvidenceLedger,
    review: DualCleanupReview,
    value: Mapping[str, object],
) -> None:
    if (
        value.get("request_key") != review.request_key
        or value.get("review_digest") != review.review_digest
        or value.get("application_state") != "succeeded"
        or not isinstance(value.get("application_id"), str)
        or not value.get("application_id")
        or value.get("profile_number") != review.profile_number
        or value.get("profile_id") != review.profile_id
        or value.get("profile_digest") != review.profile_digest
        or value.get("plan_digest") != review.plan_digest
    ):
        raise QualificationError(
            "persisted cleanup receipt changed its reviewed identity"
        )
    _validate_stop_receipts(target, value.get("stop_receipts"))


def _validate_cleanup_reconciliation(
    target: DualRecoveryTarget,
    ledger: EvidenceLedger,
    review: DualCleanupReview,
    prior: Mapping[str, object],
    value: Mapping[str, object],
    *,
    endpoint_exists: Callable[[str], bool],
) -> _FleetView:
    if (
        value.get("request_key") != review.request_key
        or value.get("review_digest") != review.review_digest
        or value.get("application_id") != prior.get("application_id")
        or _timestamp(
            value.get("application_updated_at"), "reconciled cleanup update time"
        )
        != _timestamp(
            prior.get("application_updated_at"), "completed cleanup update time"
        )
        or value.get("stop_receipts") != prior.get("stop_receipts")
    ):
        raise QualificationError(
            "cleanup reconciliation changed the original application"
        )
    _validate_cleanup_receipt_fields(target, ledger, review, value)
    current_view = _fleet_view(value.get("fleet_snapshot"), target.fleet_node_ids)
    prior_fleet = _mapping(prior.get("fleet_snapshot"), "prior cleanup FleetSnapshot")
    _require_snapshot_after(current_view, prior_fleet, "cleanup reconciliation")
    _require_idle_fleet(target, current_view)
    _require_same_authority(
        current_view,
        _required_string(
            prior_fleet.get("authority_revision"), "prior cleanup Fleet authority"
        ),
    )
    if not _all_selected_live(current_view, target):
        raise QualificationError(
            "cleanup reconciliation lacks live telemetry from both selected Sparks"
        )
    if _endpoint_presence(endpoint_exists, target.alias):
        raise QualificationError(
            "reconciled dual cleanup left the recipe route published"
        )
    return current_view


def _validate_baseline(
    target: DualRecoveryTarget,
    target_digest: str,
    ledger: EvidenceLedger,
    cleanup_event: Mapping[str, object],
    cleanup_view: _FleetView,
    baseline: Mapping[str, object],
) -> None:
    if (
        baseline.get("target_digest") is None
        or baseline.get("run_id") != target.run_id
        or baseline.get("cleanup_record_sha256") != cleanup_event.get("record_sha256")
        or baseline.get("rank_recovery_record_sha256")
        != _rank_recovery_sha(target, ledger, target_digest)
        or baseline.get("rank_smoke_refs")
        != _rank_smoke_refs(target, ledger, target_digest)
        or baseline.get("nodes") is None
    ):
        raise QualificationError("dual host baseline is not bound to exact cleanup")
    _validate_event_envelope(target, str(baseline.get("target_digest")), baseline)
    baseline_identity = _snapshot_identity_from_payload(
        baseline, "host-restart baseline"
    )
    if baseline_identity != _snapshot_identity(cleanup_view):
        raise QualificationError(
            "idle restart baseline changed its cleanup Fleet snapshot"
        )
    baseline_nodes = _mapping(baseline.get("nodes"), "restart baseline nodes")
    if set(baseline_nodes) != set(target.node_ids):
        raise QualificationError(
            "host restart baseline changed selected Spark identities"
        )
    for node_id in target.node_ids:
        row = _mapping(baseline_nodes.get(node_id), "restart baseline node")
        current = cleanup_view.nodes[node_id]
        if (
            row.get("boot_id") != current.boot_id
            or not isinstance(row.get("boot_id"), str)
            or row.get("telemetry_observed_at") != current.telemetry_observed_at
        ):
            raise QualificationError(
                "host restart baseline changed its cleanup boot ID"
            )


def _validate_offline_event(
    target: DualRecoveryTarget,
    target_digest: str,
    ledger: EvidenceLedger,
    node_id: str,
    baseline_boot_id: str,
    payload: Mapping[str, object],
    baseline_payload: Mapping[str, object],
) -> None:
    if (
        payload.get("node_id") != node_id
        or payload.get("rank") != target.node_to_rank[node_id]
        or any(
            payload.get(key) != value
            for key, value in _rank_smoke_binding(
                target, ledger, target_digest, node_id
            ).items()
        )
        or payload.get("baseline_boot_id") != baseline_boot_id
        or payload.get("online_state") != "offline"
    ):
        raise QualificationError("durable offline checkpoint changed its exact host")
    _validate_event_envelope(target, str(payload.get("target_digest")), payload)
    if payload.get("authority_revision") != baseline_payload.get("authority_revision"):
        raise QualificationError("Fleet authority changed during dual recovery")
    expected_peers = sorted(set(target.node_ids) - {node_id})
    if payload.get("online_peer_node_ids") != expected_peers:
        raise QualificationError("offline checkpoint did not preserve its online peer")
    _snapshot_identity_from_payload(payload, "offline host observation")
    if _cursor(payload.get("event_cursor"), "offline event cursor") < _cursor(
        baseline_payload.get("event_cursor"), "baseline event cursor"
    ) or _timestamp(payload.get("generated_at"), "offline generated_at") <= _timestamp(
        baseline_payload.get("generated_at"), "baseline generated_at"
    ):
        raise QualificationError("offline host observation predates its baseline")


def _require_idle_fleet(
    target: DualRecoveryTarget | FailedDualCanaryTarget, view: _FleetView
) -> None:
    if view.loaded or view.published_aliases:
        raise QualificationError("whole Fleet is not idle after dual cleanup")
    active_reservations = (
        "unified_memory_bytes",
        "host_memory_bytes",
        "gpu_memory_bytes",
        "port_count",
    )
    if any(
        node.reservations[name] != 0
        for node in view.nodes.values()
        for name in active_reservations
    ):
        raise QualificationError("runtime Fleet reservations remain after dual cleanup")


def _all_selected_live(
    view: _FleetView, target: DualRecoveryTarget | FailedDualCanaryTarget
) -> bool:
    return all(_node_live(view.nodes[node_id]) for node_id in target.node_ids)


def _node_live(node: _FleetNode) -> bool:
    if (
        node.online_state != "online"
        or node.telemetry_freshness != "live"
        or not isinstance(node.boot_id, str)
        or not node.boot_id
    ):
        return False
    try:
        _timestamp(node.telemetry_observed_at, "Fleet telemetry sample time")
    except QualificationError:
        return False
    return True


def _run_presences(
    view: _FleetView, target: DualRecoveryTarget
) -> list[tuple[str, Mapping[str, object]]]:
    records = [
        (node_id, presence)
        for node_id, presence in view.loaded
        if presence.get("run_id") == target.run_id
    ]
    node_ids = [node_id for node_id, _presence in records]
    if any(node_id not in target.node_to_rank for node_id in node_ids) or len(
        set(node_ids)
    ) != len(node_ids):
        raise QualificationError(
            "dual run presence escaped its exact two-Spark assignment"
        )
    return records


def _require_exclusive_run(target: DualRecoveryTarget, view: _FleetView) -> None:
    observed_runs = {str(item.get("run_id")) for _node, item in view.loaded}
    if observed_runs - {target.run_id}:
        raise QualificationError(
            "exclusive dual Fleet contains a workload outside its canary"
        )
    aliases = {str(item.get("alias")) for _node, item in view.loaded}
    if aliases - {target.alias}:
        raise QualificationError(
            "exclusive dual Fleet contains another published workload"
        )


def _fleet_view(value: object, expected_node_ids: Sequence[str]) -> _FleetView:
    snapshot = _mapping(value, "serialized FleetSnapshot")
    if snapshot.get("schema_version") != 1:
        raise QualificationError("FleetSnapshot schema version is invalid")
    cursor = _cursor(snapshot.get("event_cursor"), "FleetSnapshot event cursor")
    generated_at = _timestamp(
        snapshot.get("generated_at"), "FleetSnapshot generated_at"
    )
    authority = _required_string(
        snapshot.get("authority_revision"), "Fleet authority revision"
    )
    raw_nodes = snapshot.get("nodes")
    if not isinstance(raw_nodes, list):
        raise QualificationError("FleetSnapshot nodes are invalid")
    nodes: dict[str, _FleetNode] = {}
    loaded: list[tuple[str, Mapping[str, object]]] = []
    published: set[str] = set()
    for raw_node in raw_nodes:
        node = _mapping(raw_node, "Fleet node")
        node_id = _required_string(node.get("id"), "Fleet node ID")
        if node_id in nodes:
            raise QualificationError("FleetSnapshot contains duplicate node IDs")
        connection = _mapping(node.get("connection"), "Fleet node connection")
        online_state = connection.get("online_state")
        if online_state not in {"online", "offline", "unregistered"}:
            raise QualificationError("Fleet node online state is invalid")
        telemetry = node.get("telemetry")
        if telemetry is None:
            freshness, boot_id, telemetry_at = None, None, None
        else:
            telemetry_mapping = _mapping(telemetry, "Fleet node telemetry")
            freshness = telemetry_mapping.get("freshness")
            sample = telemetry_mapping.get("sample")
            if sample is None:
                boot_id, telemetry_at = None, None
            else:
                sample_mapping = _mapping(sample, "Fleet telemetry sample")
                boot_id = sample_mapping.get("boot_id")
                telemetry_at = sample_mapping.get("observed_at")
        raw_loaded = node.get("loaded")
        if not isinstance(raw_loaded, list):
            raise QualificationError("Fleet loaded run list is invalid")
        node_loaded: list[Mapping[str, object]] = []
        seen_runs: set[str] = set()
        for raw_presence in raw_loaded:
            presence = _mapping(raw_presence, "Fleet run presence")
            run_id = _required_string(presence.get("run_id"), "Fleet run ID")
            alias = _required_string(presence.get("alias"), "Fleet run alias")
            _required_string(
                presence.get("recipe_revision_id"), "Fleet recipe revision"
            )
            rank = presence.get("rank")
            route_state = presence.get("route_state")
            if type(rank) is not int or rank < 0 or not isinstance(route_state, str):
                raise QualificationError("Fleet rank or route state is invalid")
            if run_id in seen_runs:
                raise QualificationError("Fleet node repeats a loaded run")
            seen_runs.add(run_id)
            node_loaded.append(presence)
            loaded.append((node_id, presence))
            if route_state == "published":
                published.add(alias)
        raw_reservations = _mapping(
            node.get("reservations"), "Fleet capacity reservations"
        )
        reservation_names = {
            "disk_bytes",
            "unified_memory_bytes",
            "host_memory_bytes",
            "gpu_memory_bytes",
            "port_count",
        }
        if set(raw_reservations) != reservation_names:
            raise QualificationError("Fleet capacity reservations are incomplete")
        reservations: dict[str, int] = {}
        for name in reservation_names:
            amount = raw_reservations[name]
            if type(amount) is not int or amount < 0:
                raise QualificationError(f"Fleet reservation {name} is invalid")
            reservations[name] = amount
        nodes[node_id] = _FleetNode(
            online_state=str(online_state),
            telemetry_freshness=freshness,
            boot_id=boot_id,
            telemetry_observed_at=telemetry_at,
            loaded=tuple(node_loaded),
            reservations=reservations,
        )
    if set(nodes) != set(expected_node_ids):
        raise QualificationError("FleetSnapshot changed the exact whole-Fleet roster")
    return _FleetView(
        generated_at=generated_at,
        event_cursor=cursor,
        authority_revision=authority,
        nodes=nodes,
        loaded=tuple(loaded),
        published_aliases=frozenset(published),
    )


def _snapshot_identity(view: _FleetView) -> dict[str, object]:
    return {
        "generated_at": view.generated_at.isoformat(),
        "event_cursor": view.event_cursor,
        "authority_revision": view.authority_revision,
    }


def _snapshot_identity_from_payload(
    payload: Mapping[str, object], label: str
) -> dict[str, object]:
    generated_at = _timestamp(payload.get("generated_at"), f"{label} generated_at")
    cursor = _cursor(payload.get("event_cursor"), f"{label} event cursor")
    authority = _required_string(
        payload.get("authority_revision"), f"{label} authority revision"
    )
    return {
        "generated_at": generated_at.isoformat(),
        "event_cursor": cursor,
        "authority_revision": authority,
    }


def _require_snapshot_after(
    view: _FleetView, payload: Mapping[str, object], label: str
) -> None:
    prior = _snapshot_identity_from_payload(payload, f"prior {label}")
    if view.generated_at <= _timestamp(
        prior["generated_at"], f"prior {label} generated_at"
    ) or view.event_cursor < _cursor(
        prior.get("event_cursor"), f"prior {label} event cursor"
    ):
        raise QualificationError(f"{label} Fleet observation is stale")
    _require_same_authority(view, str(prior["authority_revision"]))


def _require_same_authority(view: _FleetView, expected: str) -> None:
    if view.authority_revision != expected:
        raise QualificationError("Fleet authority changed during dual recovery")


def _require_endpoint_state(
    endpoint_exists: Callable[[str], bool], alias: str, *, expected: bool
) -> bool:
    return _endpoint_presence(endpoint_exists, alias) is expected


def _endpoint_presence(endpoint_exists: Callable[[str], bool], alias: str) -> bool:
    observed = endpoint_exists(alias)
    if type(observed) is not bool:
        raise QualificationError("endpoint presence observation is not boolean")
    return observed


def _validate_event_envelope(
    target: DualRecoveryTarget, target_digest: str, payload: Mapping[str, object]
) -> None:
    expected = _envelope(target, target_digest)
    for key, value in expected.items():
        if payload.get(key) != value:
            raise QualificationError(f"dual recovery event changed {key}")


def _append(
    target: DualRecoveryTarget,
    ledger: EvidenceLedger,
    target_digest: str,
    event: str,
    details: Mapping[str, object],
) -> Mapping[str, object]:
    payload = {**_envelope(target, target_digest), **dict(details)}
    return ledger.append(
        event,
        plan_digest=target.campaign_id,
        recipe=target.recipe_key,
        payload=payload,
    )


def _event_or_append(
    target: DualRecoveryTarget,
    ledger: EvidenceLedger,
    target_digest: str,
    event: str,
    details: Mapping[str, object],
) -> Mapping[str, object]:
    existing = _event(ledger, target, target_digest, event)
    if existing is not None:
        if any(_payload(existing).get(key) != value for key, value in details.items()):
            raise QualificationError(f"durable {event} changed its exact identity")
        return existing
    return _append(target, ledger, target_digest, event, details)


def _event(
    ledger: EvidenceLedger,
    target: DualRecoveryTarget,
    target_digest: str,
    event: str,
) -> Mapping[str, object] | None:
    matches = [
        record
        for record in ledger.records
        if record.get("plan_digest") == target.campaign_id
        and record.get("recipe") == target.recipe_key
        and record.get("event") == event
        and _payload(record).get("batch_id") == target.batch_id
        and _payload(record).get("lane_id") == target.lane_id
        and _payload(record).get("target_digest") == target_digest
    ]
    if len(matches) > 1:
        raise QualificationError(f"duplicate durable {event} records exist")
    if not matches:
        return None
    _validate_event_envelope(target, target_digest, _payload(matches[0]))
    return matches[0]


def _node_event(
    ledger: EvidenceLedger,
    target: DualRecoveryTarget,
    target_digest: str,
    event: str,
    node_id: str,
) -> Mapping[str, object] | None:
    matches = [
        record
        for record in ledger.records
        if record.get("plan_digest") == target.campaign_id
        and record.get("recipe") == target.recipe_key
        and record.get("event") == event
        and _payload(record).get("batch_id") == target.batch_id
        and _payload(record).get("lane_id") == target.lane_id
        and _payload(record).get("target_digest") == target_digest
        and _payload(record).get("node_id") == node_id
    ]
    if len(matches) > 1:
        raise QualificationError(
            f"duplicate durable {event} records exist for {node_id}"
        )
    if not matches:
        return None
    _validate_event_envelope(target, target_digest, _payload(matches[0]))
    return matches[0]


def _reject_conflicting_evidence(
    target: DualRecoveryTarget,
    ledger: EvidenceLedger,
    target_digest: str,
) -> None:
    relevant = {
        _RANK_LOSS_EVENT,
        _ROUTE_WITHDRAWAL_EVENT,
        _RANK_RECOVERY_EVENT,
        "rank-recovery.smoke-requested",
        "rank-recovery.smoke-node-completed",
        "dual_recovery.cleanup.intent",
        "dual_recovery.cleanup.plan_reviewed",
        "dual_recovery.cleanup.apply-requested",
        "dual_recovery.cleanup.completed",
        "host-restart.baseline",
        "host-restart.offline",
        "host-restart.recovered",
        "dual_recovery.completed",
    }
    per_node = {
        "rank-recovery.smoke-requested",
        "rank-recovery.smoke-node-completed",
        "host-restart.offline",
        "host-restart.recovered",
    }
    for record in ledger.records:
        if (
            record.get("plan_digest") != target.campaign_id
            or record.get("recipe") != target.recipe_key
            or record.get("event") not in relevant
        ):
            continue
        payload = _payload(record)
        if payload.get("batch_id") != target.batch_id:
            continue
        if payload.get("lane_id") != target.lane_id:
            raise QualificationError(
                "durable dual recovery evidence crossed assignment lanes"
            )
        if payload.get("target_digest") != target_digest:
            raise QualificationError(
                "durable dual recovery evidence has a changed target"
            )
        if (
            record.get("event") in per_node
            and payload.get("node_id") not in target.node_ids
        ):
            raise QualificationError(
                "durable dual recovery evidence names an unassigned Spark"
            )


def _smoke_nodes(
    ledger: EvidenceLedger, target: DualRecoveryTarget, target_digest: str
) -> set[str]:
    result: set[str] = set()
    for node_id, _rank in _rank_order(target):
        record = _node_event(
            ledger,
            target,
            target_digest,
            "rank-recovery.smoke-node-completed",
            node_id,
        )
        if record is None:
            continue
        payload = _payload(record)
        if (
            payload.get("rank") != target.node_to_rank[node_id]
            or not _is_sha(payload.get("record_sha256"))
            or payload.get("request_key")
            != _smoke_request_key(target, target_digest, node_id)
        ):
            raise QualificationError(
                "rank smoke receipt changed its stable node identity"
            )
        _validate_smoke_receipt(
            target,
            {
                "passed": True,
                "run_id": target.run_id,
                "alias": target.alias,
                "node_id": node_id,
                "rank": target.node_to_rank[node_id],
                "case_ids": list(target.smoke_case_ids),
                "fixture_receipt": payload.get("fixture_receipt"),
                "record_sha256": payload.get("record_sha256"),
            },
            node_id=node_id,
            rank=target.node_to_rank[node_id],
        )
        result.add(node_id)
    return result


def _smoke_receipts(
    target: DualRecoveryTarget,
    ledger: EvidenceLedger,
    target_digest: str,
) -> dict[str, Mapping[str, object]]:
    receipts: dict[str, Mapping[str, object]] = {}
    for node_id, _rank in _rank_order(target):
        record = _node_event(
            ledger,
            target,
            target_digest,
            "rank-recovery.smoke-node-completed",
            node_id,
        )
        if record is None:
            continue
        receipts[node_id] = _payload(record)
    if set(receipts) != set(target.node_ids):
        raise QualificationError("dual recovery lacks a fixture receipt for each rank")
    return receipts


def _recovered_nodes(
    ledger: EvidenceLedger, target: DualRecoveryTarget, target_digest: str
) -> set[str]:
    recovered: set[str] = set()
    for node_id, _rank in _rank_order(target):
        record = _node_event(
            ledger, target, target_digest, "host-restart.recovered", node_id
        )
        offline = _node_event(
            ledger, target, target_digest, "host-restart.offline", node_id
        )
        if offline is None:
            if record is not None:
                raise QualificationError(
                    "host recovery lacks its prior offline observation"
                )
            continue
        baseline_record = _event(ledger, target, target_digest, "host-restart.baseline")
        if baseline_record is None:
            raise QualificationError("idle restart baseline is absent")
        baseline = _payload(baseline_record)
        baseline_boot = _baseline_boot(target, ledger, target_digest, node_id)
        offline_payload = _payload(offline)
        _validate_offline_event(
            target,
            target_digest,
            ledger,
            node_id,
            baseline_boot,
            offline_payload,
            baseline,
        )
        if record is None:
            continue
        payload = _payload(record)
        if (
            payload.get("node_id") != node_id
            or payload.get("rank") != target.node_to_rank[node_id]
            or any(
                payload.get(key) != value
                for key, value in _rank_smoke_binding(
                    target, ledger, target_digest, node_id
                ).items()
            )
            or payload.get("baseline_boot_id") != baseline_boot
            or payload.get("observed_boot_id") in (None, baseline_boot)
            or payload.get("online_state") != "online"
            or payload.get("telemetry_freshness") != "live"
            or payload.get("online_peer_node_ids")
            != sorted(set(target.node_ids) - {node_id})
            or _timestamp(
                payload.get("telemetry_observed_at"), "recovered telemetry time"
            )
            <= _timestamp(offline_payload.get("generated_at"), "offline generated_at")
        ):
            raise QualificationError(
                "host restart recovery changed its exact boot evidence"
            )
        _validate_event_envelope(target, target_digest, payload)
        recovered_identity = _snapshot_identity_from_payload(payload, "host recovery")
        if (
            _timestamp(recovered_identity["generated_at"], "host recovery generated_at")
            <= _timestamp(offline_payload.get("generated_at"), "offline generated_at")
            or _cursor(
                recovered_identity.get("event_cursor"), "host recovery event cursor"
            )
            < _cursor(offline_payload.get("event_cursor"), "offline event cursor")
            or recovered_identity["authority_revision"]
            != offline_payload.get("authority_revision")
        ):
            raise QualificationError(
                "host recovery receipt predates its offline observation"
            )
        recovered.add(node_id)
    offline_nodes = {
        node_id
        for node_id, _rank in _rank_order(target)
        if _node_event(ledger, target, target_digest, "host-restart.offline", node_id)
        is not None
    }
    ordered_events: list[tuple[int, str, str]] = []
    for event, nodes in (
        ("host-restart.offline", offline_nodes),
        ("host-restart.recovered", recovered),
    ):
        for node_id in nodes:
            record = _node_event(ledger, target, target_digest, event, node_id)
            if record is not None:
                ordered_events.append(
                    (
                        _positive_int(
                            record.get("sequence"), f"{event} event sequence"
                        ),
                        event,
                        node_id,
                    )
                )
    open_node: str | None = None
    expected_nodes = [node_id for node_id, _rank in _rank_order(target)]
    next_index = 0
    for _sequence, event, node_id in sorted(ordered_events):
        if event == "host-restart.offline":
            if (
                open_node is not None
                or next_index >= len(expected_nodes)
                or node_id != expected_nodes[next_index]
            ):
                raise QualificationError("host restart evidence is not sequential")
            open_node = node_id
        elif open_node != node_id:
            raise QualificationError(
                "host restart recovery is not paired with its offline receipt"
            )
        else:
            open_node = None
            next_index += 1
    if open_node is not None:
        return recovered
    return recovered


def _baseline_boot(
    target: DualRecoveryTarget,
    ledger: EvidenceLedger,
    target_digest: str,
    node_id: str,
) -> str:
    baseline = _event(ledger, target, target_digest, "host-restart.baseline")
    if baseline is None:
        raise QualificationError("idle restart baseline is absent")
    nodes = _mapping(_payload(baseline).get("nodes"), "restart baseline nodes")
    return _required_string(
        _mapping(nodes.get(node_id), "restart baseline node").get("boot_id"),
        "baseline boot ID",
    )


def _completion_refs(
    ledger: EvidenceLedger, target: DualRecoveryTarget, target_digest: str
) -> dict[str, str]:
    names = (
        _RANK_LOSS_EVENT,
        _ROUTE_WITHDRAWAL_EVENT,
        _RANK_RECOVERY_EVENT,
        "dual_recovery.cleanup.plan_reviewed",
        "dual_recovery.cleanup.completed",
        "host-restart.baseline",
    )
    refs: dict[str, str] = {}
    for name in names:
        record = _event(ledger, target, target_digest, name)
        if record is None:
            raise QualificationError(f"dual recovery completion lacks {name}")
        refs[name] = _required_string(
            record.get("record_sha256"), f"{name} record digest"
        )
    for node_id, _rank in _rank_order(target):
        for name in ("host-restart.offline", "host-restart.recovered"):
            record = _node_event(ledger, target, target_digest, name, node_id)
            if record is None:
                raise QualificationError(
                    f"dual recovery completion lacks {name} for {node_id}"
                )
            refs[f"{name}:{node_id}"] = _required_string(
                record.get("record_sha256"), f"{name} record digest"
            )
    return refs


def _validate_completion(
    target: DualRecoveryTarget,
    target_digest: str,
    ledger: EvidenceLedger,
    record: Mapping[str, object],
    cleanup: Mapping[str, object],
) -> None:
    payload = _payload(record)
    expected = _completion_refs(ledger, target, target_digest)
    if (
        payload.get("target_digest") != target_digest
        or payload.get("cleanup_record_sha256") != cleanup.get("record_sha256")
        or payload.get("canary_record_sha256") != target.canary_record_sha256
        or payload.get("event_refs") != expected
        or payload.get("endpoint_not_found") is not True
    ):
        raise QualificationError(
            "dual recovery completion receipt changed its source events"
        )
    baseline = _event(ledger, target, target_digest, "host-restart.baseline")
    if baseline is None:
        raise QualificationError("dual recovery completion lacks its idle baseline")
    final_view = _fleet_view(payload.get("final_fleet_snapshot"), target.fleet_node_ids)
    recovered = []
    for node_id, _rank in _rank_order(target):
        recovered_event = _node_event(
            ledger, target, target_digest, "host-restart.recovered", node_id
        )
        if recovered_event is None:
            raise QualificationError(
                "dual recovery completion lacks a host restart receipt"
            )
        recovered.append(_payload(recovered_event))
    _require_snapshot_after(final_view, recovered[-1], "final idle Fleet")
    _require_same_authority(
        final_view,
        _required_string(
            _payload(baseline).get("authority_revision"),
            "baseline Fleet authority",
        ),
    )
    _require_idle_fleet(target, final_view)
    if not _all_selected_live(final_view, target):
        raise QualificationError(
            "final dual Fleet lacks live telemetry from both Sparks"
        )
    for node_id, recovery in zip(
        (node_id for node_id, _rank in _rank_order(target)), recovered, strict=True
    ):
        if final_view.nodes[node_id].boot_id != recovery.get("observed_boot_id"):
            raise QualificationError(
                "final dual Fleet boot ID changed after its restart receipt"
            )


def _cleanup_request_key(target: DualRecoveryTarget, target_digest: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"vonk-dual-cleanup:{target_digest}"))


def _smoke_request_key(
    target: DualRecoveryTarget, target_digest: str, node_id: str
) -> str:
    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"vonk-dual-smoke:{target_digest}:{node_id}")
    )


def _rank_order(
    target: DualRecoveryTarget | FailedDualCanaryTarget,
) -> list[tuple[str, int]]:
    return sorted(target.node_to_rank.items(), key=lambda item: item[1])


def _payload(record: Mapping[str, object]) -> Mapping[str, object]:
    return _mapping(record.get("payload"), "qualification event payload")


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise QualificationError(f"{label} is not an object")
    return value


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise QualificationError(f"{label} is invalid")
    return value


def _sha(value: object, label: str) -> str:
    if not _is_sha(value):
        raise QualificationError(f"{label} is invalid")
    return str(value)


def _is_sha(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise QualificationError(f"{label} is invalid")
    return value


def _cursor(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise QualificationError(f"{label} is invalid")
    return value


def _timestamp(value: object, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise QualificationError(f"{label} is invalid") from error
    else:
        raise QualificationError(f"{label} is invalid")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise QualificationError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
    ).hexdigest()
