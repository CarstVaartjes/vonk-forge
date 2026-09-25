from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cluster_profiles.fleet_qualification import EvidenceLedger, QualificationError
from cluster_profiles.fleet_qualification_recovery import (
    CanaryReference,
    LaneRecoveryTarget,
    observe_single_lane,
    prior_partner_release_proofs,
    record_lane_cleanup,
    recover_single_lane,
    review_lane_cleanup,
    review_lane_transition,
)

CAMPAIGN = "a" * 64
CONTENT_A = "c" * 64
CONTENT_B = "1" * 64
PACKAGE_A = "d" * 64
PACKAGE_B = "2" * 64
PROFILE_DIGEST = "e" * 64
PLAN_DIGEST = "f" * 64
RECOVERY_PROFILE_DIGEST = "8" * 64
RECOVERY_PLAN_DIGEST = "9" * 64
NODE_A = "spk_" + "1" * 32
NODE_B = "spk_" + "2" * 32
RUN_A = "canary-run-a"
RUN_B = "canary-run-b"
ALIAS_A = "lane-a-service"
ALIAS_B = "lane-b-service"
REVISION_A = "recipe-revision-a"
REVISION_B = "recipe-revision-b"
CASE_IDS = ("fixture-chat", "fixture-stream")
T0 = datetime(2026, 9, 25, tzinfo=UTC)


def _identity(
    *,
    run_id: str = RUN_A,
    recipe_key: str = "vonk-forge/recipe-a",
    content: str = CONTENT_A,
    package: str = PACKAGE_A,
    revision: str = REVISION_A,
    alias: str = ALIAS_A,
    node_id: str = NODE_A,
    rank: int = 0,
    reactivated: bool = False,
) -> dict[str, object]:
    result: dict[str, object] = {
        "run_id": run_id,
        "recipe_key": recipe_key,
        "recipe_content_sha256": content,
        "package_sha256": package,
        "recipe_revision_id": revision,
        "alias": alias,
        "node_to_rank": {node_id: rank},
        "reactivated": reactivated,
    }
    if reactivated:
        result["reactivation_smoke"] = {
            "run_id": result["run_id"],
            "recipe_key": result["recipe_key"],
            "recipe_content_sha256": result["recipe_content_sha256"],
            "package_sha256": result["package_sha256"],
            "recipe_revision_id": result["recipe_revision_id"],
            "alias": result["alias"],
            "node_to_rank": result["node_to_rank"],
            "boot_id": "boot-before",
            "application_id": "application-17",
            "cases": [{"case_id": case_id, "passed": True} for case_id in CASE_IDS],
            "record_sha256": "3" * 64,
        }
    return result


def _canary(
    ledger: EvidenceLedger,
    *,
    lane_id: int,
    recipe_key: str,
    content: str,
    package: str,
    run_id: str,
    revision: str,
    alias: str,
    node_id: str,
    event: str = "canary.completed",
    applied: bool = True,
) -> CanaryReference:
    payload: dict[str, object] = {
        "batch_id": "batch-01",
        "lane_id": lane_id,
        "recipe_content_sha256": content,
        "package_sha256": package,
        "assigned_node_id": node_id,
        "assigned_rank": 0,
    }
    if applied:
        payload.update(
            {
                "run_id": run_id,
                "recipe_revision_id": revision,
                "alias": alias,
                "node_to_rank": {node_id: 0},
            }
        )
    record = ledger.append(
        event,
        plan_digest=CAMPAIGN,
        recipe=recipe_key,
        payload=payload,
    )
    return CanaryReference(
        lane_id=lane_id,
        record_sha256=str(record["record_sha256"]),
        recipe_key=recipe_key,
        recipe_content_sha256=content,
        package_sha256=package,
        assigned_node_id=node_id,
        assigned_rank=0,
        run_id=run_id if applied else None,
        recipe_revision_id=revision if applied else None,
        alias=alias if applied else None,
        node_to_rank={node_id: 0} if applied else None,
        outcome_event=event,
    )


def _target(
    ledger: EvidenceLedger,
    *,
    allow_reactivation: bool = False,
    partner_failed: bool = False,
    own_failed: bool = False,
    own_applied: bool = True,
) -> LaneRecoveryTarget:
    canary_a = _canary(
        ledger,
        lane_id=1,
        recipe_key="vonk-forge/recipe-a",
        content=CONTENT_A,
        package=PACKAGE_A,
        run_id=RUN_A,
        revision=REVISION_A,
        alias=ALIAS_A,
        node_id=NODE_A,
        event="canary.failed" if own_failed else "canary.completed",
        applied=own_applied,
    )
    canary_b = _canary(
        ledger,
        lane_id=2,
        recipe_key="vonk-forge/recipe-b",
        content=CONTENT_B,
        package=PACKAGE_B,
        run_id=RUN_B,
        revision=REVISION_B,
        alias=ALIAS_B,
        node_id=NODE_B,
        event="canary.failed" if partner_failed else "canary.completed",
        applied=True,
    )
    return LaneRecoveryTarget(
        campaign_id=CAMPAIGN,
        batch_id="batch-01",
        lane_id=1,
        recipe_key="vonk-forge/recipe-a",
        recipe_content_sha256=CONTENT_A,
        package_sha256=PACKAGE_A,
        original_run_id=RUN_A if own_applied else None,
        recipe_revision_id=REVISION_A,
        alias=ALIAS_A,
        node_id=NODE_A,
        node_to_rank={NODE_A: 0},
        smoke_case_ids=CASE_IDS,
        fleet_node_ids=(NODE_A, NODE_B),
        partner_run_ids=(RUN_B,),
        partner_aliases=(ALIAS_B,),
        canaries=(canary_a, canary_b),
        profile_number=17,
        profile_id="paired-profile-17",
        profile_digest=PROFILE_DIGEST,
        plan_digest=PLAN_DIGEST,
        allow_reactivation=allow_reactivation,
    )


def _target_for_lane(
    target: LaneRecoveryTarget,
    lane_id: int,
    *,
    allow_reactivation: bool = False,
) -> LaneRecoveryTarget:
    own = next(item for item in target.canaries if item.lane_id == lane_id)
    partner = next(item for item in target.canaries if item.lane_id != lane_id)
    assert own.run_id is not None
    assert own.recipe_revision_id is not None
    assert own.alias is not None
    assert own.node_to_rank is not None
    assert partner.run_id is not None
    assert partner.alias is not None
    return replace(
        target,
        lane_id=lane_id,
        recipe_key=own.recipe_key,
        recipe_content_sha256=own.recipe_content_sha256,
        package_sha256=own.package_sha256,
        original_run_id=own.run_id,
        recipe_revision_id=own.recipe_revision_id,
        alias=own.alias,
        node_id=own.assigned_node_id,
        node_to_rank=dict(own.node_to_rank),
        partner_run_ids=(partner.run_id,),
        partner_aliases=(partner.alias,),
        allow_reactivation=allow_reactivation,
    )


def _review(target: LaneRecoveryTarget, request: MappingLike) -> dict[str, object]:
    reactivation = target.allow_reactivation
    assignment: dict[str, object] = {
        "recipe_key": target.recipe_key,
        "recipe_content_sha256": target.recipe_content_sha256,
        "package_sha256": target.package_sha256,
        "recipe_revision_id": target.recipe_revision_id,
        "alias": target.alias,
        "node_to_rank": dict(target.node_to_rank),
    }
    if not reactivation:
        assignment["run_id"] = target.original_run_id
    return {
        "request_key": request["request_key"],
        "reviewed": True,
        "profile_number": target.profile_number,
        "profile_id": "recovery-profile-17",
        "profile_digest": RECOVERY_PROFILE_DIGEST,
        "plan_digest": RECOVERY_PLAN_DIGEST,
        "fleet_node_ids": list(target.fleet_node_ids),
        "assignment": assignment,
        "keep_run_id": None if reactivation else target.original_run_id,
        "stop_run_ids": sorted(target.partner_run_ids),
        "stop_aliases": sorted(target.partner_aliases),
        "released_partner_proofs": [],
    }


def _stop(run_id: str, node_id: str, rank: int = 0) -> dict[str, object]:
    return {
        "run_id": run_id,
        "operation_state": "succeeded",
        "final_observation": {
            "phase": "final_verify",
            "final_verified": True,
            "run_id": run_id,
            "state": "stopped",
            "route_state": "withdrawn",
            "ranks": [{"node_id": node_id, "rank": rank, "state": "stopped"}],
        },
    }


def _snapshot(
    target: LaneRecoveryTarget,
    *,
    active: dict[str, object] | None,
    online: bool,
    boot_id: str | None,
    cursor: int,
    at: datetime,
    partners: bool = False,
    published: bool = True,
    nonzero_reservation: bool = False,
) -> dict[str, object]:
    by_node: dict[str, list[dict[str, object]]] = {
        node_id: [] for node_id in target.fleet_node_ids
    }
    candidates: list[tuple[str, str, int]] = []
    if active is not None:
        active_map = active["node_to_rank"]
        assert isinstance(active_map, dict)
        candidates.extend(
            (str(active["run_id"]), node_id, int(rank))
            for node_id, rank in active_map.items()
        )
    if partners:
        for reference in target.canaries:
            if reference.lane_id != target.lane_id and reference.run_id is not None:
                assert reference.node_to_rank is not None
                candidates.extend(
                    (reference.run_id, node_id, rank)
                    for node_id, rank in reference.node_to_rank.items()
                )
    references = {reference.run_id: reference for reference in target.canaries}
    for run_id, node_id, rank in candidates:
        is_active = active is not None and run_id == active.get("run_id")
        reference = references.get(run_id)
        by_node[node_id].append(
            {
                "run_id": run_id,
                "alias": target.alias
                if is_active
                else (reference.alias if reference else ALIAS_B),
                "recipe_revision_id": target.recipe_revision_id
                if is_active
                else (reference.recipe_revision_id if reference else REVISION_B),
                "rank": rank,
                "route_state": "published"
                if (published or not is_active)
                else "withdrawn",
            }
        )
    nodes: list[dict[str, object]] = []
    for node_id in target.fleet_node_ids:
        is_target = node_id == target.node_id
        is_online = online if is_target else True
        telemetry: dict[str, object] | None
        if is_target and is_online and boot_id is not None:
            telemetry = {
                "freshness": "live",
                "sample": {
                    "boot_id": boot_id,
                    "observed_at": (at - timedelta(seconds=1)).isoformat(),
                },
            }
        elif is_target:
            telemetry = None
        else:
            telemetry = {
                "freshness": "live",
                "sample": {
                    "boot_id": "other-boot",
                    "observed_at": (at - timedelta(seconds=1)).isoformat(),
                },
            }
        reservations = {
            "disk_bytes": 0,
            "unified_memory_bytes": 0,
            "host_memory_bytes": 0,
            "gpu_memory_bytes": 0,
            "port_count": 0,
        }
        if nonzero_reservation and node_id == NODE_B:
            reservations["disk_bytes"] = 8192
        nodes.append(
            {
                "id": node_id,
                "connection": {"online_state": "online" if is_online else "offline"},
                "telemetry": telemetry,
                "loaded": by_node[node_id],
                "reservations": reservations,
            }
        )
    return {
        "schema_version": 1,
        "event_cursor": cursor,
        "generated_at": at.isoformat(),
        "authority_revision": "authority-42",
        "nodes": nodes,
    }


def _transition(
    target: LaneRecoveryTarget, request: MappingLike, active: dict[str, object]
) -> dict[str, object]:
    review = _as_mapping(request["review"])
    return {
        "request_key": request["request_key"],
        "review_digest": request["review_digest"],
        "application_state": "succeeded",
        "application_id": "application-17",
        "application_updated_at": (T0 + timedelta(seconds=1)).isoformat(),
        "profile_number": review["profile_number"],
        "profile_id": review["profile_id"],
        "profile_digest": review["profile_digest"],
        "plan_digest": review["plan_digest"],
        "active": active,
        "partner_stop_receipts": [
            _stop(reference.run_id, reference.assigned_node_id, reference.assigned_rank)
            for reference in target.canaries
            if reference.run_id in review["stop_run_ids"]
        ],
        "pre_transition_fleet_snapshot": _snapshot(
            target,
            active=(
                None
                if active.get("reactivated")
                else _identity(
                    run_id=_required(target.original_run_id),
                    recipe_key=target.recipe_key,
                    content=target.recipe_content_sha256,
                    package=target.package_sha256,
                    revision=target.recipe_revision_id,
                    alias=target.alias,
                    node_id=target.node_id,
                )
            ),
            online=True,
            boot_id="boot-before",
            cursor=1,
            at=T0,
            partners=bool(review["stop_run_ids"]),
        ),
        "fleet_snapshot": _snapshot(
            target,
            active=active,
            online=True,
            boot_id="boot-before",
            cursor=2,
            at=T0 + timedelta(seconds=1),
            nonzero_reservation=True,
        ),
    }


def _cleanup_review_receipt(
    target: LaneRecoveryTarget,
    ledger: EvidenceLedger,
    request: MappingLike,
    *,
    active: dict[str, object] | None,
    partners: bool,
    cursor: int = 17,
    at: datetime = T0 + timedelta(seconds=17),
) -> dict[str, object]:
    snapshot = _snapshot(
        target,
        active=active,
        online=True,
        boot_id="boot-after",
        cursor=cursor,
        at=at,
        partners=partners,
    )
    loaded_ids = sorted(
        str(presence["run_id"])
        for node in snapshot["nodes"]
        for presence in node["loaded"]
    )
    aliases_by_run = {
        reference.run_id: reference.alias for reference in target.canaries
    }
    if active is not None:
        aliases_by_run[str(active["run_id"])] = target.alias
    stop_aliases = sorted({str(aliases_by_run[run_id]) for run_id in loaded_ids})
    expected_run_ids = request.get(
        "expected_stop_run_ids", request.get("stop_run_ids", [])
    )
    assert isinstance(expected_run_ids, list)
    proofs = prior_partner_release_proofs(
        target,
        ledger,
        snapshot,
        candidate_run_ids=expected_run_ids,
    )
    return {
        "request_key": request["request_key"],
        "reviewed": True,
        "cleanup_mode": request["cleanup_mode"],
        "terminal_event": request["terminal_event"],
        "terminal_record_sha256": request["terminal_record_sha256"],
        "profile_number": 17,
        "profile_id": "idle-profile-17",
        "profile_digest": "a" * 64,
        "plan_digest": "b" * 64,
        "fleet_node_ids": list(target.fleet_node_ids),
        "active_run_id": request["active_run_id"],
        "stop_run_ids": loaded_ids,
        "stop_aliases": stop_aliases,
        "released_run_proofs": list(proofs),
        "pre_cleanup_fleet_snapshot": snapshot,
    }


def _cleanup_application(
    target: LaneRecoveryTarget,
    request: MappingLike,
    *,
    application_id: str = "cleanup-application",
    cursor: int = 20,
    at: datetime = T0 + timedelta(seconds=20),
) -> dict[str, object]:
    review = _as_mapping(request["review"])
    references = {item.run_id: item for item in target.canaries}
    stop_receipts = []
    for run_id in review["stop_run_ids"]:
        reference = references.get(str(run_id))
        node_id = reference.assigned_node_id if reference else target.node_id
        rank = (
            reference.assigned_rank
            if reference
            else int(next(iter(target.node_to_rank.values())))
        )
        stop_receipts.append(_stop(str(run_id), node_id, rank))
    return {
        "request_key": request["request_key"],
        "review_digest": request["review_digest"],
        "application_state": "succeeded",
        "application_id": application_id,
        "application_updated_at": (at - timedelta(seconds=1)).isoformat(),
        "profile_number": review["profile_number"],
        "profile_id": review["profile_id"],
        "profile_digest": review["profile_digest"],
        "plan_digest": review["plan_digest"],
        "stop_receipts": stop_receipts,
        "released_run_proofs": list(review["released_run_proofs"]),
        "fleet_snapshot": _snapshot(
            target,
            active=None,
            online=True,
            boot_id="boot-after",
            cursor=cursor,
            at=at,
        ),
    }


MappingLike = Mapping[str, object]


def _as_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("expected a mapping")
    return dict(value)


def _record_payload(record: MappingLike) -> MappingLike:
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        raise TypeError("record payload must be a mapping")
    return payload


def _required(value: str | None) -> str:
    if value is None:
        raise AssertionError("test target requires a run ID")
    return value


def _observation(
    target: LaneRecoveryTarget,
    active: dict[str, object],
    *,
    online: bool,
    boot_id: str | None,
    cursor: int,
    at: datetime,
    published: bool = True,
) -> dict[str, object]:
    return {
        "snapshot_freshness": "live",
        "profile_number": target.profile_number,
        "profile_id": "recovery-profile-17",
        "profile_digest": RECOVERY_PROFILE_DIGEST,
        "plan_digest": RECOVERY_PLAN_DIGEST,
        "fleet_snapshot": _snapshot(
            target,
            active=active,
            online=online,
            boot_id=boot_id,
            cursor=cursor,
            at=at,
            published=published,
        ),
    }


def _serving(active: dict[str, object], boot_id: str) -> dict[str, object]:
    return {
        "state": "succeeded",
        "run_id": active["run_id"],
        "recipe_key": active["recipe_key"],
        "recipe_content_sha256": active["recipe_content_sha256"],
        "package_sha256": active["package_sha256"],
        "recipe_revision_id": active["recipe_revision_id"],
        "alias": active["alias"],
        "node_to_rank": active["node_to_rank"],
        "boot_id": boot_id,
        "route_state": "published",
        "healthy": True,
    }


def _smoke(active: dict[str, object], boot_id: str) -> dict[str, object]:
    return {
        "run_id": active["run_id"],
        "recipe_key": active["recipe_key"],
        "recipe_content_sha256": active["recipe_content_sha256"],
        "package_sha256": active["package_sha256"],
        "recipe_revision_id": active["recipe_revision_id"],
        "alias": active["alias"],
        "node_to_rank": active["node_to_rank"],
        "boot_id": boot_id,
        "cases": [{"case_id": case_id, "passed": True} for case_id in CASE_IDS],
    }


def _callbacks(
    target: LaneRecoveryTarget,
    observations: list[dict[str, object]],
    *,
    active: dict[str, object] | None = None,
    transition_receipt: dict[str, object] | None = None,
) -> tuple[dict[str, Any], dict[str, int]]:
    identity = active or _identity()
    calls = {"prepare": 0, "transition": 0, "observe": 0, "verify": 0, "smoke": 0}

    def prepare(request: MappingLike) -> MappingLike:
        calls["prepare"] += 1
        return _review(target, request)

    def transition(request: MappingLike) -> MappingLike:
        calls["transition"] += 1
        return transition_receipt or _transition(target, request, identity)

    def observe() -> MappingLike:
        calls["observe"] += 1
        if not observations:
            raise AssertionError("unexpected Fleet observation")
        return observations.pop(0)

    def verify(request: MappingLike) -> MappingLike:
        calls["verify"] += 1
        return _serving(_as_mapping(request["active"]), str(request["boot_id"]))

    def smoke(request: MappingLike) -> MappingLike:
        calls["smoke"] += 1
        return _smoke(_as_mapping(request["active"]), str(request["boot_id"]))

    return {
        "prepare_transition": prepare,
        "transition_to_lane": transition,
        "observe_fleet": observe,
        "verify_serving": verify,
        "run_fixture_smoke": smoke,
    }, calls


def _run(
    target: LaneRecoveryTarget,
    ledger: EvidenceLedger,
    callbacks: dict[str, Any],
    *,
    apply=True,
):
    return recover_single_lane(target, ledger, apply_authorized=apply, **callbacks)


def _observe(
    target: LaneRecoveryTarget, ledger: EvidenceLedger, callbacks: dict[str, Any]
):
    return observe_single_lane(
        target,
        ledger,
        observe_fleet=callbacks["observe_fleet"],
        verify_serving=callbacks["verify_serving"],
        run_fixture_smoke=callbacks["run_fixture_smoke"],
    )


def test_review_precedes_effects_and_preview_never_applies_or_observes(
    tmp_path: Path,
) -> None:
    ledger = EvidenceLedger(tmp_path / "preview.jsonl")
    target = _target(ledger)
    callbacks, calls = _callbacks(target, [])

    progress = _run(target, ledger, callbacks, apply=False)

    assert progress.status == "awaiting-explicit-apply"
    assert calls["prepare"] == 1
    assert calls["transition"] == calls["observe"] == 0
    assert [
        record["event"]
        for record in ledger.records
        if str(record["event"]).startswith("lane_recovery.")
    ] == [
        "lane_recovery.intent",
        "lane_recovery.plan_reviewed",
    ]
    intent = next(
        item for item in ledger.records if item["event"] == "lane_recovery.intent"
    )
    uuid.UUID(str(_record_payload(intent)["transition_request_key"]))


def test_partner_canary_record_rejects_reference_that_drops_applied_run(
    tmp_path: Path,
) -> None:
    ledger = EvidenceLedger(tmp_path / "dropped-partner-run.jsonl")
    target = _target(ledger, partner_failed=True)
    own, partner = target.canaries
    assert partner.run_id == RUN_B
    target = replace(
        target,
        canaries=(own, replace(partner, run_id=None)),
        partner_run_ids=(),
    )
    callback_calls = 0

    def forbidden(request: MappingLike) -> MappingLike:
        nonlocal callback_calls
        callback_calls += 1
        return _review(target, request)

    with pytest.raises(QualificationError, match="exact recipe/lane evidence"):
        review_lane_transition(
            target,
            ledger,
            prepare_transition=forbidden,
        )
    assert callback_calls == 0


def test_offline_checkpoint_resumes_across_process_restart_and_accepts_equal_cursor(
    tmp_path: Path,
) -> None:
    path = tmp_path / "evidence.jsonl"
    ledger = EvidenceLedger(path)
    target = _target(ledger)
    active = _identity()
    observations = [
        _observation(
            target,
            active,
            online=True,
            boot_id="boot-before",
            cursor=5,
            at=T0 + timedelta(seconds=10),
        ),
        # Fleet's offline projection can share the high-watermark cursor; its fresh generated_at is the ordering proof.
        _observation(
            target,
            active,
            online=False,
            boot_id=None,
            cursor=5,
            at=T0 + timedelta(seconds=11),
        ),
    ]
    callbacks, calls = _callbacks(target, observations, active=active)

    pending = _run(target, ledger, callbacks)

    assert pending.status == "awaiting-host-online"
    assert (
        sum(
            item.get("event") == "lane_recovery.offline_observed"
            for item in ledger.records
        )
        == 1
    )
    transition_key = next(
        _record_payload(item)["transition_request_key"]
        for item in ledger.records
        if item.get("event") == "lane_recovery.intent"
    )

    resumed_ledger = EvidenceLedger(path)
    observations.extend(
        [
            _observation(
                target,
                active,
                online=True,
                boot_id="boot-after",
                cursor=6,
                at=T0 + timedelta(seconds=12),
            ),
            _observation(
                target,
                active,
                online=True,
                boot_id="boot-after",
                cursor=6,
                at=T0 + timedelta(seconds=13),
            ),
        ]
    )
    callbacks, resumed_calls = _callbacks(target, observations, active=active)
    complete = _observe(target, resumed_ledger, callbacks)

    assert complete.status == "completed"
    assert complete.baseline_boot_id == "boot-before"
    assert complete.recovered_boot_id == "boot-after"
    assert calls["transition"] == 1
    assert resumed_calls["transition"] == 0
    assert resumed_calls["prepare"] == 0
    assert resumed_calls["verify"] == 1
    assert resumed_calls["smoke"] == 1
    assert (
        next(
            _record_payload(item)["transition_request_key"]
            for item in resumed_ledger.records
            if item.get("event") == "lane_recovery.intent"
        )
        == transition_key
    )


def test_offline_observation_with_later_cursor_but_stale_generated_at_is_rejected(
    tmp_path: Path,
) -> None:
    ledger = EvidenceLedger(tmp_path / "stale-offline.jsonl")
    target = _target(ledger)
    active = _identity()
    observations = [
        _observation(
            target,
            active,
            online=True,
            boot_id="boot-before",
            cursor=5,
            at=T0 + timedelta(seconds=20),
        ),
        _observation(
            target,
            active,
            online=False,
            boot_id=None,
            cursor=99,
            at=T0 + timedelta(seconds=20),
        ),
    ]
    callbacks, _ = _callbacks(target, observations, active=active)

    progress = _run(target, ledger, callbacks)

    assert progress.status == "awaiting-host-offline"
    assert not any(
        item.get("event") == "lane_recovery.offline_observed" for item in ledger.records
    )


def test_same_boot_after_offline_never_completes_or_runs_fixture_smoke(
    tmp_path: Path,
) -> None:
    ledger = EvidenceLedger(tmp_path / "same-boot.jsonl")
    target = _target(ledger)
    active = _identity()
    first_observations = [
        _observation(
            target,
            active,
            online=True,
            boot_id="boot-before",
            cursor=5,
            at=T0 + timedelta(seconds=1),
        ),
        _observation(
            target,
            active,
            online=False,
            boot_id=None,
            cursor=5,
            at=T0 + timedelta(seconds=2),
        ),
    ]
    callbacks, _ = _callbacks(target, first_observations, active=active)
    assert _run(target, ledger, callbacks).status == "awaiting-host-online"

    callbacks, calls = _callbacks(
        target,
        [
            _observation(
                target,
                active,
                online=True,
                boot_id="boot-before",
                cursor=6,
                at=T0 + timedelta(seconds=3),
            )
        ],
        active=active,
    )
    second = _observe(target, ledger, callbacks)

    assert second.status == "awaiting-changed-boot"
    assert calls["smoke"] == 0
    assert not any(
        item.get("event") in {"lane_recovery.recovered", "lane_recovery.completed"}
        for item in ledger.records
    )


@pytest.mark.parametrize(
    "bad_active",
    [
        _identity(run_id="unrelated-run"),
        _identity(revision="wrong-revision"),
        _identity(node_id=NODE_B),
    ],
)
def test_transition_rejects_wrong_run_revision_or_node_before_recovery(
    tmp_path: Path, bad_active: dict[str, object]
) -> None:
    ledger = EvidenceLedger(tmp_path / "wrong-transition.jsonl")
    target = _target(ledger)
    callbacks, _ = _callbacks(target, [], active=bad_active)

    with pytest.raises(
        QualificationError, match="exact canary run|active lane|replaced"
    ):
        _run(target, ledger, callbacks)

    assert not any(
        item.get("event") == "lane_recovery.transitioned" for item in ledger.records
    )


def test_wrong_stop_rank_receipt_blocks_transition_and_preserves_no_success_receipt(
    tmp_path: Path,
) -> None:
    ledger = EvidenceLedger(tmp_path / "wrong-stop-rank.jsonl")
    target = _target(ledger)
    active = _identity()
    callbacks, _ = _callbacks(target, [], active=active)
    transition = callbacks["transition_to_lane"]

    def wrong_stop(request: MappingLike) -> MappingLike:
        result = dict(transition(request))
        stops = list(result["partner_stop_receipts"])
        stops[0] = _stop(RUN_B, NODE_A)
        result["partner_stop_receipts"] = stops
        return result

    callbacks["transition_to_lane"] = wrong_stop
    with pytest.raises(QualificationError, match="node/rank assignment"):
        _run(target, ledger, callbacks)
    assert not any(
        item.get("event") == "lane_recovery.transitioned" for item in ledger.records
    )


def test_partner_run_left_in_actual_fleet_snapshot_blocks_transition(
    tmp_path: Path,
) -> None:
    ledger = EvidenceLedger(tmp_path / "partner-active.jsonl")
    target = _target(ledger)
    active = _identity()
    callbacks, _ = _callbacks(target, [], active=active)
    transition = callbacks["transition_to_lane"]

    def partner_left(request: MappingLike) -> MappingLike:
        result = dict(transition(request))
        snapshot = dict(result["fleet_snapshot"])
        nodes = [dict(node) for node in snapshot["nodes"]]
        node_b = nodes[1]
        node_b["loaded"] = [
            {
                "run_id": RUN_B,
                "alias": ALIAS_B,
                "recipe_revision_id": REVISION_B,
                "rank": 0,
                "route_state": "withdrawn",
            }
        ]
        snapshot["nodes"] = nodes
        result["fleet_snapshot"] = snapshot
        return result

    callbacks["transition_to_lane"] = partner_left
    with pytest.raises(QualificationError, match="partner workload remains"):
        _run(target, ledger, callbacks)
    assert not any(
        item.get("event") == "lane_recovery.transitioned" for item in ledger.records
    )


def test_failed_partner_terminal_receipt_does_not_block_healthy_lane_isolation(
    tmp_path: Path,
) -> None:
    ledger = EvidenceLedger(tmp_path / "failed-partner.jsonl")
    target = _target(ledger, partner_failed=True)
    active = _identity()
    observations = [
        _observation(
            target,
            active,
            online=True,
            boot_id="boot-before",
            cursor=5,
            at=T0 + timedelta(seconds=1),
        ),
        _observation(
            target,
            active,
            online=False,
            boot_id=None,
            cursor=5,
            at=T0 + timedelta(seconds=2),
        ),
    ]
    callbacks, calls = _callbacks(target, observations, active=active)

    progress = _run(target, ledger, callbacks)

    assert progress.status == "awaiting-host-online"
    assert calls["transition"] == 1
    assert any(item.get("event") == "canary.failed" for item in ledger.records)


def test_transient_starting_run_waits_then_resumes_from_durable_changed_boot(
    tmp_path: Path,
) -> None:
    ledger = EvidenceLedger(tmp_path / "starting.jsonl")
    target = _target(ledger)
    active = _identity()
    initial = [
        _observation(
            target,
            active,
            online=True,
            boot_id="boot-before",
            cursor=5,
            at=T0 + timedelta(seconds=1),
        ),
        _observation(
            target,
            active,
            online=False,
            boot_id=None,
            cursor=5,
            at=T0 + timedelta(seconds=2),
        ),
    ]
    callbacks, _ = _callbacks(target, initial, active=active)
    assert _run(target, ledger, callbacks).status == "awaiting-host-online"

    callbacks, _ = _callbacks(
        target,
        [
            _observation(
                target,
                active,
                online=True,
                boot_id="boot-after",
                cursor=6,
                at=T0 + timedelta(seconds=3),
                published=False,
            ),
        ],
        active=active,
    )
    waiting = _observe(target, ledger, callbacks)
    assert waiting.status == "awaiting-lane-serving"
    assert any(
        item.get("event") == "lane_recovery.boot_observed" for item in ledger.records
    )
    assert not any(
        item.get("event") == "lane_recovery.recovered" for item in ledger.records
    )

    callbacks, _ = _callbacks(
        target,
        [
            _observation(
                target,
                active,
                online=True,
                boot_id="boot-after",
                cursor=7,
                at=T0 + timedelta(seconds=4),
            ),
            _observation(
                target,
                active,
                online=True,
                boot_id="boot-after",
                cursor=8,
                at=T0 + timedelta(seconds=5),
            ),
        ],
        active=active,
    )
    complete = _observe(target, ledger, callbacks)
    assert complete.status == "completed"


def test_authorized_reactivation_binds_new_run_to_fresh_exact_smoke(
    tmp_path: Path,
) -> None:
    ledger = EvidenceLedger(tmp_path / "reactivation.jsonl")
    target = _target(ledger, allow_reactivation=True)
    active = _identity(run_id="reactivated-run", reactivated=True)
    callbacks, _ = _callbacks(
        target,
        [
            _observation(
                target,
                active,
                online=True,
                boot_id="boot-before",
                cursor=3,
                at=T0 + timedelta(seconds=2),
            ),
            _observation(
                target,
                active,
                online=False,
                boot_id=None,
                cursor=3,
                at=T0 + timedelta(seconds=3),
            ),
        ],
        active=active,
    )

    assert _run(target, ledger, callbacks).status == "awaiting-host-online"
    transition = next(
        record
        for record in ledger.records
        if record.get("event") == "lane_recovery.transitioned"
    )
    assert _record_payload(transition)["active_run_id"] == "reactivated-run"


def test_authorized_reactivation_rejects_smoke_for_another_run(tmp_path: Path) -> None:
    ledger = EvidenceLedger(tmp_path / "reactivation-wrong-smoke.jsonl")
    target = _target(ledger, allow_reactivation=True)
    active = _identity(run_id="reactivated-run", reactivated=True)
    smoke = active["reactivation_smoke"]
    assert isinstance(smoke, dict)
    smoke["run_id"] = "another-run"
    callbacks, _ = _callbacks(target, [], active=active)

    with pytest.raises(QualificationError, match="fixture smoke"):
        _run(target, ledger, callbacks)


def test_failed_pair_can_be_cleaned_without_fabricating_recovery_success(
    tmp_path: Path,
) -> None:
    ledger = EvidenceLedger(tmp_path / "both-canaries-failed.jsonl")
    target = _target(ledger, own_failed=True, partner_failed=True)

    def prepare(request: MappingLike) -> MappingLike:
        return _cleanup_review_receipt(
            target,
            ledger,
            request,
            active=_identity(),
            partners=True,
        )

    preview = review_lane_cleanup(target, ledger, prepare_cleanup=prepare)
    assert preview.receipt["cleanup_mode"] == "failed-canary-lane"

    def apply(request: MappingLike) -> MappingLike:
        assert preview.profile_number == request["profile_number"]
        return _cleanup_application(
            target,
            request,
            application_id="cleanup-failed-pair",
        )

    complete = record_lane_cleanup(
        target,
        ledger,
        prepare_cleanup=prepare,
        apply_authorized=True,
        cleanup_to_idle=apply,
    )
    events = [record.get("event") for record in ledger.records]
    assert complete.status == "cleanup-completed"
    assert "lane_recovery.completed" not in events
    assert "lane.failed" not in events


def test_failed_canary_without_applied_run_requires_reconciliation(
    tmp_path: Path,
) -> None:
    ledger = EvidenceLedger(tmp_path / "failed-unknown-application.jsonl")
    target = _target(ledger, own_failed=True, own_applied=False)

    with pytest.raises(QualificationError, match="reconcile before cleanup"):
        review_lane_cleanup(target, ledger, prepare_cleanup=lambda _request: {})


def _recover_to_completion(ledger: EvidenceLedger, target: LaneRecoveryTarget) -> None:
    active = _identity()
    callbacks, _ = _callbacks(
        target,
        [
            _observation(
                target,
                active,
                online=True,
                boot_id="boot-before",
                cursor=5,
                at=T0 + timedelta(seconds=1),
            ),
            _observation(
                target,
                active,
                online=False,
                boot_id=None,
                cursor=5,
                at=T0 + timedelta(seconds=2),
            ),
        ],
        active=active,
    )
    assert _run(target, ledger, callbacks).status == "awaiting-host-online"
    callbacks, _ = _callbacks(
        target,
        [
            _observation(
                target,
                active,
                online=True,
                boot_id="boot-after",
                cursor=6,
                at=T0 + timedelta(seconds=3),
            ),
            _observation(
                target,
                active,
                online=True,
                boot_id="boot-after",
                cursor=7,
                at=T0 + timedelta(seconds=4),
            ),
        ],
        active=active,
    )
    assert _observe(target, ledger, callbacks).status == "completed"


def test_cleanup_is_reviewed_first_and_requires_typed_stops_and_empty_fleet(
    tmp_path: Path,
) -> None:
    ledger = EvidenceLedger(tmp_path / "cleanup.jsonl")
    target = _target(ledger)
    _recover_to_completion(ledger, target)

    def prepare(request: MappingLike) -> MappingLike:
        active = _identity()
        return _cleanup_review_receipt(
            target,
            ledger,
            request,
            active=active,
            partners=True,
            cursor=17,
            at=T0 + timedelta(seconds=17),
        )

    preview = review_lane_cleanup(target, ledger, prepare_cleanup=prepare)
    assert preview.profile_id == "idle-profile-17"

    def forbidden(_request: MappingLike) -> MappingLike:
        raise AssertionError("cleanup must not apply without explicit authorization")

    waiting = record_lane_cleanup(
        target,
        ledger,
        prepare_cleanup=prepare,
        apply_authorized=False,
        cleanup_to_idle=forbidden,
    )
    assert waiting.status == "awaiting-explicit-cleanup"

    def apply(request: MappingLike) -> MappingLike:
        assert request["review_digest"] == preview.review_digest
        return _cleanup_application(
            target,
            request,
            cursor=20,
            at=T0 + timedelta(seconds=20),
        )

    complete = record_lane_cleanup(
        target,
        ledger,
        prepare_cleanup=prepare,
        apply_authorized=True,
        cleanup_to_idle=apply,
    )
    assert complete.status == "cleanup-completed"
    assert complete.active_run_id == RUN_A


def test_cleanup_rejects_boolean_only_or_misattributed_stop_receipt(
    tmp_path: Path,
) -> None:
    ledger = EvidenceLedger(tmp_path / "cleanup-bad-stop.jsonl")
    target = _target(ledger)
    _recover_to_completion(ledger, target)

    def prepare(request: MappingLike) -> MappingLike:
        return _cleanup_review_receipt(
            target,
            ledger,
            request,
            active=_identity(),
            partners=True,
        )

    def apply(request: MappingLike) -> MappingLike:
        result = _cleanup_application(target, request)
        result["stop_receipts"] = [_stop(RUN_A, NODE_B), _stop(RUN_B, NODE_B)]
        return result

    with pytest.raises(QualificationError, match="node/rank assignment"):
        record_lane_cleanup(
            target,
            ledger,
            prepare_cleanup=prepare,
            apply_authorized=True,
            cleanup_to_idle=apply,
        )


@pytest.mark.parametrize("missing_presence", ["offline", "stale", "missing"])
def test_cleanup_rejects_empty_but_unobserved_locked_node(
    tmp_path: Path, missing_presence: str
) -> None:
    ledger = EvidenceLedger(tmp_path / f"cleanup-unobserved-{missing_presence}.jsonl")
    target = _target(ledger)
    _recover_to_completion(ledger, target)

    def prepare(request: MappingLike) -> MappingLike:
        return _cleanup_review_receipt(
            target, ledger, request, active=_identity(), partners=True
        )

    def apply(request: MappingLike) -> MappingLike:
        result = _cleanup_application(target, request)
        snapshot = result["fleet_snapshot"]
        assert isinstance(snapshot, dict)
        node = next(item for item in snapshot["nodes"] if item["id"] == NODE_B)
        if missing_presence == "offline":
            node["connection"]["online_state"] = "offline"
        elif missing_presence == "stale":
            telemetry = node["telemetry"]
            assert isinstance(telemetry, dict)
            telemetry["freshness"] = "stale"
        else:
            node["telemetry"] = None
        return result

    with pytest.raises(QualificationError, match="every locked Fleet node"):
        record_lane_cleanup(
            target,
            ledger,
            prepare_cleanup=prepare,
            apply_authorized=True,
            cleanup_to_idle=apply,
        )


def test_cleanup_completion_resumes_from_original_review_without_new_plan_or_apply(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cleanup-resume.jsonl"
    ledger = EvidenceLedger(path)
    target = _target(ledger)
    _recover_to_completion(ledger, target)
    calls = {"prepare": 0, "apply": 0, "reconcile": 0}

    def prepare(request: MappingLike) -> MappingLike:
        calls["prepare"] += 1
        return _cleanup_review_receipt(
            target, ledger, request, active=_identity(), partners=True
        )

    def apply(request: MappingLike) -> MappingLike:
        calls["apply"] += 1
        return _cleanup_application(target, request)

    applied = record_lane_cleanup(
        target,
        ledger,
        prepare_cleanup=prepare,
        apply_authorized=True,
        cleanup_to_idle=apply,
    )
    assert applied.status == "cleanup-completed"
    completion = next(
        item
        for item in ledger.records
        if item.get("event") == "lane_recovery.cleanup.completed"
    )
    persisted = _record_payload(completion)["receipt"]

    resumed_ledger = EvidenceLedger(path)

    def forbidden(_request: MappingLike) -> MappingLike:
        raise AssertionError("completed cleanup resume must not preview or apply again")

    def reconcile(request: MappingLike) -> MappingLike:
        calls["reconcile"] += 1
        assert request["resume_completed"] is True
        assert request["request_key"] == _record_payload(completion)["request_key"]
        assert request["review_digest"] == _record_payload(completion)["review_digest"]
        assert request["persisted_receipt"] == persisted
        fresh = dict(persisted)
        fresh["fleet_snapshot"] = _snapshot(
            target,
            active=None,
            online=True,
            boot_id="boot-after",
            cursor=20,
            at=T0 + timedelta(seconds=21),
        )
        return fresh

    resumed = record_lane_cleanup(
        target,
        resumed_ledger,
        prepare_cleanup=forbidden,
        apply_authorized=True,
        cleanup_to_idle=forbidden,
        reconcile_cleanup=reconcile,
    )
    assert resumed.status == "cleanup-completed"
    assert calls == {"prepare": 1, "apply": 1, "reconcile": 1}
    assert any(
        item.get("event") == "lane_recovery.cleanup.reconciled"
        for item in resumed_ledger.records
    )


def test_cleanup_lost_apply_response_reuses_saved_review_and_request(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cleanup-lost-response.jsonl"
    ledger = EvidenceLedger(path)
    target = _target(ledger)
    _recover_to_completion(ledger, target)
    calls = {"prepare": 0, "accepted": 0, "reconciled": 0}
    accepted_receipt: dict[str, object] | None = None

    def prepare(request: MappingLike) -> MappingLike:
        calls["prepare"] += 1
        return _cleanup_review_receipt(
            target, ledger, request, active=_identity(), partners=True
        )

    def lose_response(request: MappingLike) -> MappingLike:
        nonlocal accepted_receipt
        calls["accepted"] += 1
        accepted_receipt = _cleanup_application(target, request)
        raise RuntimeError("simulated accepted Controller response loss")

    with pytest.raises(RuntimeError, match="response loss"):
        record_lane_cleanup(
            target,
            ledger,
            prepare_cleanup=prepare,
            apply_authorized=True,
            cleanup_to_idle=lose_response,
        )
    assert accepted_receipt is not None
    persisted_review = next(
        item
        for item in ledger.records
        if item.get("event") == "lane_recovery.cleanup.plan_reviewed"
    )
    persisted_request_key = _record_payload(persisted_review)["request_key"]
    resumed_ledger = EvidenceLedger(path)

    def reconcile(request: MappingLike) -> MappingLike:
        calls["reconciled"] += 1
        assert request["resume_reviewed"] is True
        assert request["request_key"] == persisted_request_key
        result = dict(accepted_receipt)
        result["fleet_snapshot"] = _snapshot(
            target,
            active=None,
            online=True,
            boot_id="boot-after",
            cursor=20,
            at=T0 + timedelta(seconds=21),
        )
        return result

    def forbidden(_request: MappingLike) -> MappingLike:
        raise AssertionError("replay must not prepare a new cleanup plan")

    complete = record_lane_cleanup(
        target,
        resumed_ledger,
        prepare_cleanup=forbidden,
        apply_authorized=True,
        cleanup_to_idle=reconcile,
    )
    assert complete.status == "cleanup-completed"
    assert calls == {"prepare": 1, "accepted": 1, "reconciled": 1}


def test_lane_two_reactivation_uses_exact_prior_lane_release_after_restart(
    tmp_path: Path,
) -> None:
    ledger = EvidenceLedger(tmp_path / "second-lane-release.jsonl")
    lane_one = _target(ledger)
    _recover_to_completion(ledger, lane_one)

    def prepare_cleanup_one(request: MappingLike) -> MappingLike:
        return _cleanup_review_receipt(
            lane_one,
            ledger,
            request,
            active=_identity(),
            partners=False,
            cursor=17,
            at=T0 + timedelta(seconds=17),
        )

    def apply_cleanup_one(request: MappingLike) -> MappingLike:
        return _cleanup_application(
            lane_one,
            request,
            application_id="lane-one-cleanup",
            cursor=20,
            at=T0 + timedelta(seconds=20),
        )

    assert (
        record_lane_cleanup(
            lane_one,
            ledger,
            prepare_cleanup=prepare_cleanup_one,
            apply_authorized=True,
            cleanup_to_idle=apply_cleanup_one,
        ).status
        == "cleanup-completed"
    )

    lane_two = _target_for_lane(lane_one, 2, allow_reactivation=True)
    stale = _snapshot(
        lane_two,
        active=None,
        online=True,
        boot_id="boot-after",
        cursor=20,
        at=T0 + timedelta(seconds=20),
    )
    with pytest.raises(QualificationError, match="fresh Fleet observation"):
        prior_partner_release_proofs(lane_two, ledger, stale)

    fresh_but_unobserved = _snapshot(
        lane_two,
        active=None,
        online=True,
        boot_id="boot-after",
        cursor=21,
        at=T0 + timedelta(seconds=21),
    )
    nodes = fresh_but_unobserved["nodes"]
    assert isinstance(nodes, list)
    partner_node = next(
        node for node in nodes if isinstance(node, dict) and node.get("id") == NODE_A
    )
    assert isinstance(partner_node, dict)
    connection = partner_node["connection"]
    assert isinstance(connection, dict)
    connection["online_state"] = "offline"
    with pytest.raises(QualificationError, match="every locked Fleet node"):
        prior_partner_release_proofs(lane_two, ledger, fresh_but_unobserved)

    active = _identity(
        run_id="reactivated-lane-two",
        recipe_key=lane_two.recipe_key,
        content=lane_two.recipe_content_sha256,
        package=lane_two.package_sha256,
        revision=lane_two.recipe_revision_id,
        alias=lane_two.alias,
        node_id=lane_two.node_id,
        reactivated=True,
    )

    def review_two(request: MappingLike, *, corrupt: bool = False) -> MappingLike:
        pre = _snapshot(
            lane_two,
            active=None,
            online=True,
            boot_id="boot-after",
            cursor=21,
            at=T0 + timedelta(seconds=21),
        )
        proofs = list(
            prior_partner_release_proofs(
                lane_two,
                ledger,
                pre,
                candidate_run_ids=request["partner_run_ids"],
            )
        )
        if corrupt:
            proofs[0]["source_record_sha256"] = "0" * 64
        result = _review(lane_two, request)
        result["stop_run_ids"] = []
        result["stop_aliases"] = []
        result["released_partner_proofs"] = proofs
        return result

    with pytest.raises(QualificationError, match="misattributed"):
        review_lane_transition(
            lane_two,
            ledger,
            prepare_transition=lambda request: review_two(request, corrupt=True),
        )

    snapshots = [
        _observation(
            lane_two,
            active,
            online=True,
            boot_id="boot-before",
            cursor=23,
            at=T0 + timedelta(seconds=23),
        ),
        _observation(
            lane_two,
            active,
            online=False,
            boot_id=None,
            cursor=23,
            at=T0 + timedelta(seconds=24),
        ),
        _observation(
            lane_two,
            active,
            online=True,
            boot_id="boot-after",
            cursor=24,
            at=T0 + timedelta(seconds=25),
        ),
        _observation(
            lane_two,
            active,
            online=True,
            boot_id="boot-after",
            cursor=25,
            at=T0 + timedelta(seconds=26),
        ),
    ]

    def prepare_transition(request: MappingLike) -> MappingLike:
        return review_two(request)

    def transition(request: MappingLike) -> MappingLike:
        result = _transition(lane_two, request, active)
        result["application_updated_at"] = (T0 + timedelta(seconds=21)).isoformat()
        result["pre_transition_fleet_snapshot"] = _snapshot(
            lane_two,
            active=None,
            online=True,
            boot_id="boot-after",
            cursor=22,
            at=T0 + timedelta(seconds=22),
        )
        result["fleet_snapshot"] = _snapshot(
            lane_two,
            active=active,
            online=True,
            boot_id="boot-before",
            cursor=23,
            at=T0 + timedelta(seconds=23),
        )
        return result

    def observe() -> MappingLike:
        if not snapshots:
            raise AssertionError("unexpected second-lane recovery observation")
        return snapshots.pop(0)

    recovery_path = tmp_path / "second-lane-release.jsonl"
    progress = recover_single_lane(
        lane_two,
        EvidenceLedger(recovery_path),
        prepare_transition=prepare_transition,
        apply_authorized=True,
        transition_to_lane=transition,
        observe_fleet=observe,
        verify_serving=lambda request: _serving(
            _as_mapping(request["active"]), str(request["boot_id"])
        ),
        run_fixture_smoke=lambda request: _smoke(
            _as_mapping(request["active"]), str(request["boot_id"])
        ),
    )
    assert progress.status == "awaiting-host-online"
    progress = observe_single_lane(
        lane_two,
        EvidenceLedger(recovery_path),
        observe_fleet=observe,
        verify_serving=lambda request: _serving(
            _as_mapping(request["active"]), str(request["boot_id"])
        ),
        run_fixture_smoke=lambda request: _smoke(
            _as_mapping(request["active"]), str(request["boot_id"])
        ),
    )
    assert progress.status == "completed"


def test_noop_failed_lane_cleanup_requires_prior_typed_stops_and_fresh_idle_fleet(
    tmp_path: Path,
) -> None:
    ledger = EvidenceLedger(tmp_path / "noop-cleanup.jsonl")
    lane_one = _target(ledger, partner_failed=True)
    _recover_to_completion(ledger, lane_one)

    def prepare_lane_one_cleanup(request: MappingLike) -> MappingLike:
        return _cleanup_review_receipt(
            lane_one,
            ledger,
            request,
            active=_identity(),
            partners=False,
            cursor=17,
            at=T0 + timedelta(seconds=17),
        )

    def apply_lane_one_cleanup(request: MappingLike) -> MappingLike:
        return _cleanup_application(
            lane_one,
            request,
            application_id="lane-one-cleanup-for-noop",
            cursor=20,
            at=T0 + timedelta(seconds=20),
        )

    assert (
        record_lane_cleanup(
            lane_one,
            ledger,
            prepare_cleanup=prepare_lane_one_cleanup,
            apply_authorized=True,
            cleanup_to_idle=apply_lane_one_cleanup,
        ).status
        == "cleanup-completed"
    )

    lane_two = _target_for_lane(lane_one, 2)

    def prepare_lane_two_cleanup(request: MappingLike) -> MappingLike:
        return _cleanup_review_receipt(
            lane_two,
            ledger,
            request,
            active=None,
            partners=False,
            cursor=21,
            at=T0 + timedelta(seconds=21),
        )

    preview = review_lane_cleanup(
        lane_two, ledger, prepare_cleanup=prepare_lane_two_cleanup
    )
    assert preview.receipt["stop_run_ids"] == []
    assert [proof["run_id"] for proof in preview.receipt["released_run_proofs"]] == [
        RUN_A,
        RUN_B,
    ]

    def apply_lane_two_cleanup(request: MappingLike) -> MappingLike:
        result = _cleanup_application(
            lane_two,
            request,
            application_id="lane-two-noop-cleanup",
            cursor=23,
            at=T0 + timedelta(seconds=23),
        )
        assert result["stop_receipts"] == []
        return result

    complete = record_lane_cleanup(
        lane_two,
        ledger,
        prepare_cleanup=prepare_lane_two_cleanup,
        apply_authorized=True,
        cleanup_to_idle=apply_lane_two_cleanup,
    )
    assert complete.status == "cleanup-completed"
    cleanup = next(
        item
        for item in reversed(ledger.records)
        if item.get("event") == "lane_recovery.cleanup.completed"
        and _record_payload(item).get("lane_id") == 2
    )
    assert _record_payload(cleanup)["receipt"]["stop_receipts"] == []
