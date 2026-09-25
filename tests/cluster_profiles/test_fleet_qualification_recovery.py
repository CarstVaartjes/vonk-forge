from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cluster_profiles.fleet_qualification import EvidenceLedger, QualificationError
from cluster_profiles.fleet_qualification_recovery import (
    CanaryReference,
    LaneRecoveryTarget,
    observe_single_lane,
    record_lane_cleanup,
    recover_single_lane,
    review_lane_cleanup,
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
            "cases": [
                {"case_id": case_id, "passed": True} for case_id in CASE_IDS
            ],
            "record_sha256": "3" * 64,
        }
    return result


def _canary(
    ledger: EvidenceLedger,
    *,
    lane_id: str,
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
        lane_id="lane-a",
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
        lane_id="lane-b",
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
        lane_id="lane-a",
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
                "alias": target.alias if is_active else (reference.alias if reference else ALIAS_B),
                "recipe_revision_id": target.recipe_revision_id if is_active else (
                    reference.recipe_revision_id if reference else REVISION_B
                ),
                "rank": rank,
                "route_state": "published" if (published or not is_active) else "withdrawn",
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
                "freshness": "stale",
                "sample": {
                    "boot_id": "other-boot",
                    "observed_at": (at - timedelta(minutes=1)).isoformat(),
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


def _transition(target: LaneRecoveryTarget, request: MappingLike, active: dict[str, object]) -> dict[str, object]:
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
            if reference.lane_id != target.lane_id and reference.run_id is not None
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
            partners=True,
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


def _run(target: LaneRecoveryTarget, ledger: EvidenceLedger, callbacks: dict[str, Any], *, apply=True):
    return recover_single_lane(
        target, ledger, apply_authorized=apply, **callbacks
    )


def _observe(target: LaneRecoveryTarget, ledger: EvidenceLedger, callbacks: dict[str, Any]):
    return observe_single_lane(
        target,
        ledger,
        observe_fleet=callbacks["observe_fleet"],
        verify_serving=callbacks["verify_serving"],
        run_fixture_smoke=callbacks["run_fixture_smoke"],
    )


def test_review_precedes_effects_and_preview_never_applies_or_observes(tmp_path: Path) -> None:
    ledger = EvidenceLedger(tmp_path / "preview.jsonl")
    target = _target(ledger)
    callbacks, calls = _callbacks(target, [])

    progress = _run(target, ledger, callbacks, apply=False)

    assert progress.status == "awaiting-explicit-apply"
    assert calls["prepare"] == 1
    assert calls["transition"] == calls["observe"] == 0
    assert [record["event"] for record in ledger.records if str(record["event"]).startswith("lane_recovery.")] == [
        "lane_recovery.intent",
        "lane_recovery.plan_reviewed",
    ]
    intent = next(item for item in ledger.records if item["event"] == "lane_recovery.intent")
    uuid.UUID(str(_record_payload(intent)["transition_request_key"]))


def test_offline_checkpoint_resumes_across_process_restart_and_accepts_equal_cursor(
    tmp_path: Path,
) -> None:
    path = tmp_path / "evidence.jsonl"
    ledger = EvidenceLedger(path)
    target = _target(ledger)
    active = _identity()
    observations = [
        _observation(target, active, online=True, boot_id="boot-before", cursor=5, at=T0 + timedelta(seconds=10)),
        # Fleet's offline projection can share the high-watermark cursor; its fresh generated_at is the ordering proof.
        _observation(target, active, online=False, boot_id=None, cursor=5, at=T0 + timedelta(seconds=11)),
    ]
    callbacks, calls = _callbacks(target, observations, active=active)

    pending = _run(target, ledger, callbacks)

    assert pending.status == "awaiting-host-online"
    assert sum(item.get("event") == "lane_recovery.offline_observed" for item in ledger.records) == 1
    transition_key = next(
        _record_payload(item)["transition_request_key"]
        for item in ledger.records
        if item.get("event") == "lane_recovery.intent"
    )

    resumed_ledger = EvidenceLedger(path)
    observations.extend(
        [
            _observation(target, active, online=True, boot_id="boot-after", cursor=6, at=T0 + timedelta(seconds=12)),
            _observation(target, active, online=True, boot_id="boot-after", cursor=6, at=T0 + timedelta(seconds=13)),
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
    assert next(
        _record_payload(item)["transition_request_key"]
        for item in resumed_ledger.records
        if item.get("event") == "lane_recovery.intent"
    ) == transition_key


def test_offline_observation_with_later_cursor_but_stale_generated_at_is_rejected(
    tmp_path: Path,
) -> None:
    ledger = EvidenceLedger(tmp_path / "stale-offline.jsonl")
    target = _target(ledger)
    active = _identity()
    observations = [
        _observation(target, active, online=True, boot_id="boot-before", cursor=5, at=T0 + timedelta(seconds=20)),
        _observation(target, active, online=False, boot_id=None, cursor=99, at=T0 + timedelta(seconds=20)),
    ]
    callbacks, _ = _callbacks(target, observations, active=active)

    progress = _run(target, ledger, callbacks)

    assert progress.status == "awaiting-host-offline"
    assert not any(item.get("event") == "lane_recovery.offline_observed" for item in ledger.records)


def test_same_boot_after_offline_never_completes_or_runs_fixture_smoke(tmp_path: Path) -> None:
    ledger = EvidenceLedger(tmp_path / "same-boot.jsonl")
    target = _target(ledger)
    active = _identity()
    first_observations = [
        _observation(target, active, online=True, boot_id="boot-before", cursor=5, at=T0 + timedelta(seconds=1)),
        _observation(target, active, online=False, boot_id=None, cursor=5, at=T0 + timedelta(seconds=2)),
    ]
    callbacks, _ = _callbacks(target, first_observations, active=active)
    assert _run(target, ledger, callbacks).status == "awaiting-host-online"

    callbacks, calls = _callbacks(
        target,
        [_observation(target, active, online=True, boot_id="boot-before", cursor=6, at=T0 + timedelta(seconds=3))],
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

    with pytest.raises(QualificationError, match="exact canary run|active lane|replaced"):
        _run(target, ledger, callbacks)

    assert not any(item.get("event") == "lane_recovery.transitioned" for item in ledger.records)


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
    assert not any(item.get("event") == "lane_recovery.transitioned" for item in ledger.records)


def test_partner_run_left_in_actual_fleet_snapshot_blocks_transition(tmp_path: Path) -> None:
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
    assert not any(item.get("event") == "lane_recovery.transitioned" for item in ledger.records)


def test_failed_partner_terminal_receipt_does_not_block_healthy_lane_isolation(
    tmp_path: Path,
) -> None:
    ledger = EvidenceLedger(tmp_path / "failed-partner.jsonl")
    target = _target(ledger, partner_failed=True)
    active = _identity()
    observations = [
        _observation(target, active, online=True, boot_id="boot-before", cursor=5, at=T0 + timedelta(seconds=1)),
        _observation(target, active, online=False, boot_id=None, cursor=5, at=T0 + timedelta(seconds=2)),
    ]
    callbacks, calls = _callbacks(target, observations, active=active)

    progress = _run(target, ledger, callbacks)

    assert progress.status == "awaiting-host-online"
    assert calls["transition"] == 1
    assert any(item.get("event") == "canary.failed" for item in ledger.records)


def test_transient_starting_run_waits_then_resumes_from_durable_changed_boot(tmp_path: Path) -> None:
    ledger = EvidenceLedger(tmp_path / "starting.jsonl")
    target = _target(ledger)
    active = _identity()
    initial = [
        _observation(target, active, online=True, boot_id="boot-before", cursor=5, at=T0 + timedelta(seconds=1)),
        _observation(target, active, online=False, boot_id=None, cursor=5, at=T0 + timedelta(seconds=2)),
    ]
    callbacks, _ = _callbacks(target, initial, active=active)
    assert _run(target, ledger, callbacks).status == "awaiting-host-online"

    callbacks, _ = _callbacks(
        target,
        [
            _observation(target, active, online=True, boot_id="boot-after", cursor=6, at=T0 + timedelta(seconds=3), published=False),
        ],
        active=active,
    )
    waiting = _observe(target, ledger, callbacks)
    assert waiting.status == "awaiting-lane-serving"
    assert any(item.get("event") == "lane_recovery.boot_observed" for item in ledger.records)
    assert not any(item.get("event") == "lane_recovery.recovered" for item in ledger.records)

    callbacks, _ = _callbacks(
        target,
        [
            _observation(target, active, online=True, boot_id="boot-after", cursor=7, at=T0 + timedelta(seconds=4)),
            _observation(target, active, online=True, boot_id="boot-after", cursor=8, at=T0 + timedelta(seconds=5)),
        ],
        active=active,
    )
    complete = _observe(target, ledger, callbacks)
    assert complete.status == "completed"


def test_authorized_reactivation_binds_new_run_to_fresh_exact_smoke(tmp_path: Path) -> None:
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


def test_failed_pair_can_be_cleaned_without_fabricating_recovery_success(tmp_path: Path) -> None:
    ledger = EvidenceLedger(tmp_path / "both-canaries-failed.jsonl")
    target = _target(ledger, own_failed=True, partner_failed=True)

    def prepare(request: MappingLike) -> MappingLike:
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
            "stop_run_ids": request["stop_run_ids"],
            "stop_aliases": request["stop_aliases"],
        }

    preview = review_lane_cleanup(target, ledger, prepare_cleanup=prepare)
    assert preview.receipt["cleanup_mode"] == "failed-canary-lane"

    def apply(request: MappingLike) -> MappingLike:
        return {
            "request_key": request["request_key"],
            "review_digest": request["review_digest"],
            "application_state": "succeeded",
            "application_id": "cleanup-failed-pair",
            "application_updated_at": (T0 + timedelta(seconds=19)).isoformat(),
            "profile_number": preview.profile_number,
            "profile_id": preview.profile_id,
            "profile_digest": preview.profile_digest,
            "plan_digest": preview.plan_digest,
            "stop_receipts": [_stop(RUN_A, NODE_A), _stop(RUN_B, NODE_B)],
            "fleet_snapshot": _snapshot(
                target,
                active=None,
                online=True,
                boot_id="boot-before",
                cursor=20,
                at=T0 + timedelta(seconds=20),
            ),
        }

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


def test_failed_canary_without_applied_run_requires_reconciliation(tmp_path: Path) -> None:
    ledger = EvidenceLedger(tmp_path / "failed-unknown-application.jsonl")
    target = _target(ledger, own_failed=True, own_applied=False)

    with pytest.raises(QualificationError, match="reconcile before cleanup"):
        review_lane_cleanup(target, ledger, prepare_cleanup=lambda _request: {})


def _recover_to_completion(ledger: EvidenceLedger, target: LaneRecoveryTarget) -> None:
    active = _identity()
    callbacks, _ = _callbacks(
        target,
        [
            _observation(target, active, online=True, boot_id="boot-before", cursor=5, at=T0 + timedelta(seconds=1)),
            _observation(target, active, online=False, boot_id=None, cursor=5, at=T0 + timedelta(seconds=2)),
        ],
        active=active,
    )
    assert _run(target, ledger, callbacks).status == "awaiting-host-online"
    callbacks, _ = _callbacks(
        target,
        [
            _observation(target, active, online=True, boot_id="boot-after", cursor=6, at=T0 + timedelta(seconds=3)),
            _observation(target, active, online=True, boot_id="boot-after", cursor=7, at=T0 + timedelta(seconds=4)),
        ],
        active=active,
    )
    assert _observe(target, ledger, callbacks).status == "completed"


def test_cleanup_is_reviewed_first_and_requires_typed_stops_and_empty_fleet(tmp_path: Path) -> None:
    ledger = EvidenceLedger(tmp_path / "cleanup.jsonl")
    target = _target(ledger)
    _recover_to_completion(ledger, target)

    def prepare(request: MappingLike) -> MappingLike:
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
            "stop_run_ids": request["stop_run_ids"],
            "stop_aliases": request["stop_aliases"],
        }

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
        return {
            "request_key": request["request_key"],
            "review_digest": request["review_digest"],
            "application_state": "succeeded",
            "application_id": "cleanup-application",
            "application_updated_at": (T0 + timedelta(seconds=19)).isoformat(),
            "profile_number": preview.profile_number,
            "profile_id": preview.profile_id,
            "profile_digest": preview.profile_digest,
            "plan_digest": preview.plan_digest,
            "stop_receipts": [_stop(RUN_A, NODE_A), _stop(RUN_B, NODE_B)],
            "fleet_snapshot": _snapshot(
                target,
                active=None,
                online=True,
                boot_id="boot-after",
                cursor=20,
                at=T0 + timedelta(seconds=20),
                nonzero_reservation=True,
            ),
        }

    complete = record_lane_cleanup(
        target,
        ledger,
        prepare_cleanup=prepare,
        apply_authorized=True,
        cleanup_to_idle=apply,
    )
    assert complete.status == "cleanup-completed"
    assert complete.active_run_id == RUN_A


def test_cleanup_rejects_boolean_only_or_misattributed_stop_receipt(tmp_path: Path) -> None:
    ledger = EvidenceLedger(tmp_path / "cleanup-bad-stop.jsonl")
    target = _target(ledger)
    _recover_to_completion(ledger, target)

    def prepare(request: MappingLike) -> MappingLike:
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
            "stop_run_ids": request["stop_run_ids"],
            "stop_aliases": request["stop_aliases"],
        }

    def apply(request: MappingLike) -> MappingLike:
        return {
            "request_key": request["request_key"],
            "review_digest": request["review_digest"],
            "application_state": "succeeded",
            "application_id": "cleanup-application",
            "application_updated_at": (T0 + timedelta(seconds=19)).isoformat(),
            "profile_number": 17,
            "profile_id": "idle-profile-17",
            "profile_digest": "a" * 64,
            "plan_digest": "b" * 64,
            "run_absent_from_fleet": True,
            "route_withdrawn": True,
            "stop_receipts": [_stop(RUN_A, NODE_B), _stop(RUN_B, NODE_B)],
            "fleet_snapshot": _snapshot(
                target,
                active=None,
                online=True,
                boot_id="boot-after",
                cursor=20,
                at=T0 + timedelta(seconds=20),
            ),
        }

    with pytest.raises(QualificationError, match="node/rank assignment"):
        record_lane_cleanup(
            target,
            ledger,
            prepare_cleanup=prepare,
            apply_authorized=True,
            cleanup_to_idle=apply,
        )
