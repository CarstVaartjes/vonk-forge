from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cluster_profiles.fleet_qualification import EvidenceLedger, QualificationError
from cluster_profiles.fleet_qualification_dual_recovery import (
    DualRecoveryTarget,
    FailedDualCanaryTarget,
    apply_dual_cleanup,
    apply_failed_dual_cleanup,
    observe_dual_batch,
    review_dual_cleanup,
    review_failed_dual_cleanup,
)

CAMPAIGN = "a" * 64
CONTENT = "b" * 64
PACKAGE = "c" * 64
PROFILE = "d" * 64
LOAD_PLAN = "e" * 64
CLEAN_PROFILE = "f" * 64
CLEAN_PLAN = "1" * 64
NODE_A = "spk_" + "1" * 32
NODE_B = "spk_" + "2" * 32
RUN_ID = "dual-canary-run"
REVISION = "recipe-revision-17"
ALIAS = "dual-service"
CASE_IDS = ("fixture-chat", "fixture-stream")
T0 = datetime(2026, 9, 25, tzinfo=UTC)


def _target(ledger: EvidenceLedger) -> DualRecoveryTarget:
    canary = ledger.append(
        "canary.completed",
        plan_digest=CAMPAIGN,
        recipe="vonk-forge/dual-recipe",
        payload={
            "batch_id": "batch-09",
            "lane_id": 1,
            "recipe_content_sha256": CONTENT,
            "package_sha256": PACKAGE,
            "run_id": RUN_ID,
            "recipe_revision_id": REVISION,
            "alias": ALIAS,
            "node_ids": [NODE_A, NODE_B],
            "node_to_rank": {NODE_A: 0, NODE_B: 1},
            "smoke": {
                "endpoint_alias": ALIAS,
                "recipe_content_sha256": CONTENT,
                "cases": [{"case_id": case_id} for case_id in CASE_IDS],
            },
        },
    )
    ledger.append(
        "rank-loss.pending",
        plan_digest=CAMPAIGN,
        recipe="vonk-forge/dual-recipe",
        payload={
            "batch_id": "batch-09",
            "lane_id": 1,
            "failure_spark": NODE_B,
            "run_id": RUN_ID,
            "revision_id": REVISION,
            "alias": ALIAS,
            "node_ids": [NODE_A, NODE_B],
            "node_to_rank": {NODE_A: 0, NODE_B: 1},
        },
    )
    return DualRecoveryTarget(
        campaign_id=CAMPAIGN,
        batch_id="batch-09",
        lane_id=1,
        recipe_key="vonk-forge/dual-recipe",
        recipe_content_sha256=CONTENT,
        package_sha256=PACKAGE,
        canary_record_sha256=str(canary["record_sha256"]),
        run_id=RUN_ID,
        recipe_revision_id=REVISION,
        alias=ALIAS,
        node_ids=(NODE_A, NODE_B),
        node_to_rank={NODE_A: 0, NODE_B: 1},
        failure_node_id=NODE_B,
        smoke_case_ids=CASE_IDS,
        fleet_node_ids=(NODE_A, NODE_B),
        profile_number=17,
        profile_id="campaign-profile-17",
        profile_digest=PROFILE,
        plan_digest=LOAD_PLAN,
    )


def _failed_target(
    ledger: EvidenceLedger, *, run_id: str | None = RUN_ID
) -> FailedDualCanaryTarget:
    application_id = "11111111-1111-4111-8111-111111111111"
    request_key = "22222222-2222-4222-8222-222222222222"
    application_updated_at = (T0 + timedelta(minutes=1)).isoformat()
    application_plan_digest = hashlib.sha256(
        json.dumps(
            {
                "schema_version": 2,
                "reconciliation_digest": LOAD_PLAN,
                "retry_of_application_id": None,
                "request_key": request_key,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    ).hexdigest()
    application = {
        "id": application_id,
        "request_key": request_key,
        "state": "succeeded",
        "profile_id": "campaign-profile-17",
        "profile_digest": PROFILE,
        "plan_digest": application_plan_digest,
        "updated_at": application_updated_at,
    }
    failure = ledger.append(
        "canary.failed",
        plan_digest=CAMPAIGN,
        recipe="vonk-forge/dual-recipe",
        payload={
            "batch_id": "batch-09",
            "lane_id": 1,
            "application": application,
            "application_id": application_id,
            "application_request_key": request_key,
            "run_id": run_id,
            "alias": ALIAS,
            "recipe_revision_id": REVISION,
            "recipe_content_sha256": CONTENT,
            "package_sha256": PACKAGE,
            "node_ids": [NODE_A, NODE_B],
            "node_to_rank": {NODE_A: 0, NODE_B: 1},
            "assigned_node_id": NODE_B,
            "assigned_rank": 1,
            "fleet_rank_presence": [
                {
                    "node_id": node_id,
                    **_presence(
                        node_id,
                        rank,
                        state="running",
                        route="published",
                        healthy=True,
                        group_state="healthy",
                        present_ranks=[0, 1],
                    ),
                }
                for node_id, rank in ((NODE_A, 0), (NODE_B, 1))
            ],
            "smoke_status": "failed",
            "error": "fixture smoke timed out",
        },
    )
    return FailedDualCanaryTarget(
        campaign_id=CAMPAIGN,
        batch_id="batch-09",
        lane_id=1,
        recipe_key="vonk-forge/dual-recipe",
        recipe_content_sha256=CONTENT,
        package_sha256=PACKAGE,
        canary_record_sha256=str(failure["record_sha256"]),
        application_id=application_id,
        application_request_key=request_key,
        application_updated_at=application_updated_at,
        run_id=run_id,
        recipe_revision_id=REVISION,
        alias=ALIAS,
        node_ids=(NODE_A, NODE_B),
        node_to_rank={NODE_A: 0, NODE_B: 1},
        assigned_node_id=NODE_B,
        assigned_rank=1,
        fleet_node_ids=(NODE_A, NODE_B),
        profile_number=17,
        profile_id="campaign-profile-17",
        profile_digest=PROFILE,
        plan_digest=LOAD_PLAN,
    )


def _presence(
    node_id: str,
    rank: int,
    *,
    state: str,
    route: str,
    healthy: bool,
    group_state: str,
    present_ranks: list[int],
    rank_state: str | None = None,
) -> dict[str, object]:
    return {
        "run_id": RUN_ID,
        "recipe_revision_id": REVISION,
        "alias": ALIAS,
        "rank": rank,
        "expected_rank_count": 2,
        "member_node_ids": [NODE_A, NODE_B],
        "present_ranks": present_ranks,
        "run_state": state,
        "route_state": route,
        "rank_state": rank_state or ("running" if healthy else "failed"),
        "rank_fresh": healthy,
        "group_state": group_state,
        "healthy": healthy,
    }


def _snapshot(
    minute: int,
    *,
    state: str,
    cursor: int = 10,
    boots: Mapping[str, str] | None = None,
    offline_node: str | None = None,
    degraded_route: str = "withdrawn",
) -> dict[str, object]:
    boots = boots or {NODE_A: "boot-a-1", NODE_B: "boot-b-1"}
    nodes: list[dict[str, object]] = []
    for node_id, rank in ((NODE_A, 0), (NODE_B, 1)):
        is_offline = node_id == offline_node
        if state == "loss":
            loaded = (
                []
                if node_id == NODE_B
                else [
                    _presence(
                        node_id,
                        rank,
                        state="degraded",
                        route="withdrawn",
                        healthy=False,
                        group_state="degraded",
                        present_ranks=[0],
                    )
                ]
            )
        elif state == "serving":
            loaded = [
                _presence(
                    node_id,
                    rank,
                    state="running",
                    route="published",
                    healthy=True,
                    group_state="healthy",
                    present_ranks=[0, 1],
                )
            ]
        elif state == "degraded-online-failure":
            loaded = [
                _presence(
                    node_id,
                    rank,
                    state="degraded",
                    route=degraded_route,
                    healthy=False,
                    group_state="degraded",
                    present_ranks=[0],
                    rank_state="lost" if node_id == NODE_B else "running",
                )
            ]
        else:
            loaded = []
        telemetry: dict[str, object] | None = {
            "freshness": "live",
            "sample": {
                "boot_id": boots[node_id],
                "observed_at": (T0 + timedelta(minutes=minute)).isoformat(),
            },
        }
        if is_offline:
            telemetry = {
                "freshness": "stale",
                "sample": {
                    "boot_id": boots[node_id],
                    "observed_at": (T0 + timedelta(minutes=minute - 1)).isoformat(),
                },
            }
        nodes.append(
            {
                "id": node_id,
                "connection": {"online_state": "offline" if is_offline else "online"},
                "telemetry": telemetry,
                "loaded": loaded,
                "reservations": {
                    "disk_bytes": 500,
                    "unified_memory_bytes": 0,
                    "host_memory_bytes": 0,
                    "gpu_memory_bytes": 0,
                    "port_count": 0,
                },
            }
        )
    return {
        "schema_version": 1,
        "event_cursor": cursor,
        "generated_at": (T0 + timedelta(minutes=minute)).isoformat(),
        "authority_revision": "fleet-revision-3",
        "nodes": nodes,
    }


def _serving_receipt(
    target: DualRecoveryTarget, request: Mapping[str, object]
) -> dict[str, object]:
    fleet = request["fleet_snapshot"]
    assert isinstance(fleet, Mapping)
    presences: list[dict[str, object]] = []
    for node in fleet["nodes"]:
        assert isinstance(node, Mapping)
        for presence in node["loaded"]:
            assert isinstance(presence, Mapping)
            if presence.get("run_id") == target.run_id:
                presences.append({"node_id": node["id"], **dict(presence)})
    return {
        "state": "succeeded",
        "run_id": target.run_id,
        "recipe_key": target.recipe_key,
        "recipe_content_sha256": target.recipe_content_sha256,
        "package_sha256": target.package_sha256,
        "recipe_revision_id": target.recipe_revision_id,
        "alias": target.alias,
        "node_to_rank": dict(target.node_to_rank),
        "rank_presence": presences,
        "endpoint": {"api_base": "https://spark.example.invalid/v1"},
    }


def _smoke_receipt(
    target: DualRecoveryTarget, request: Mapping[str, object]
) -> dict[str, object]:
    node_id = str(request["node_id"])
    return {
        "passed": True,
        "run_id": target.run_id,
        "alias": target.alias,
        "node_id": node_id,
        "rank": target.node_to_rank[node_id],
        "case_ids": list(target.smoke_case_ids),
        "fixture_receipt": {
            "endpoint_alias": target.alias,
            "recipe_content_sha256": target.recipe_content_sha256,
            "cases": [{"case_id": case_id} for case_id in target.smoke_case_ids],
        },
        "record_sha256": "3" * 64 if node_id == NODE_A else "4" * 64,
    }


def _cleanup_review(
    target: DualRecoveryTarget, request: Mapping[str, object]
) -> dict[str, object]:
    return {
        "request_key": request["request_key"],
        "reviewed": True,
        "profile_number": target.profile_number,
        "profile_id": target.profile_id,
        "profile_digest": CLEAN_PROFILE,
        "plan_digest": CLEAN_PLAN,
        "cleanup_mode": "dual-lane",
        "terminal_event": "rank-recovery.smoke-completed",
        "terminal_record_sha256": request["terminal_record_sha256"],
        "active_run_id": target.run_id,
        "stop_run_ids": [target.run_id],
        "stop_aliases": [target.alias],
        "fleet_node_ids": sorted(target.fleet_node_ids),
    }


def _failed_cleanup_review(
    target: FailedDualCanaryTarget, request: Mapping[str, object]
) -> dict[str, object]:
    return {
        "request_key": request["request_key"],
        "reviewed": True,
        "canary_record_sha256": target.canary_record_sha256,
        "terminal_event": "canary.failed",
        "terminal_record_sha256": target.canary_record_sha256,
        "source_application_id": target.application_id,
        "source_application_request_key": target.application_request_key,
        "source_application_updated_at": target.application_updated_at,
        "profile_number": target.profile_number,
        "profile_id": target.profile_id,
        "profile_digest": CLEAN_PROFILE,
        "plan_digest": CLEAN_PLAN,
        "cleanup_mode": "failed-dual-lane",
        "active_run_id": target.run_id,
        "stop_run_ids": [target.run_id],
        "stop_aliases": [target.alias],
        "node_ids": list(target.node_ids),
        "node_to_rank": dict(target.node_to_rank),
        "assigned_node_id": target.assigned_node_id,
        "assigned_rank": target.assigned_rank,
        "fleet_node_ids": sorted(target.fleet_node_ids),
    }


def _failed_cleanup_result(
    target: FailedDualCanaryTarget,
    request: Mapping[str, object],
    fleet_snapshot: Mapping[str, object],
) -> dict[str, object]:
    review = request["review"]
    assert isinstance(review, Mapping)
    return {
        "request_key": request["request_key"],
        "review_digest": request["review_digest"],
        "application_state": "succeeded",
        "application_id": "33333333-3333-4333-8333-333333333333",
        "application_updated_at": fleet_snapshot["generated_at"],
        "profile_number": review["profile_number"],
        "profile_id": review["profile_id"],
        "profile_digest": review["profile_digest"],
        "plan_digest": review["plan_digest"],
        "cleanup_mode": "failed-dual-lane",
        "terminal_event": "canary.failed",
        "terminal_record_sha256": target.canary_record_sha256,
        "canary_record_sha256": target.canary_record_sha256,
        "source_application_id": target.application_id,
        "source_application_request_key": target.application_request_key,
        "source_application_updated_at": target.application_updated_at,
        "active_run_id": target.run_id,
        "stop_run_ids": [target.run_id],
        "stop_aliases": [target.alias],
        "node_ids": list(target.node_ids),
        "node_to_rank": dict(target.node_to_rank),
        "assigned_node_id": target.assigned_node_id,
        "assigned_rank": target.assigned_rank,
        "fleet_node_ids": sorted(target.fleet_node_ids),
        "stop_receipts": [
            {
                "run_id": target.run_id,
                "operation_state": "succeeded",
                "final_observation": {
                    "phase": "final_verify",
                    "final_verified": True,
                    "run_id": target.run_id,
                    "state": "stopped",
                    "route_state": "withdrawn",
                    "ranks": [
                        {"node_id": node_id, "rank": rank, "state": "stopped"}
                        for node_id, rank in sorted(
                            target.node_to_rank.items(), key=lambda item: item[1]
                        )
                    ],
                },
            }
        ],
        "fleet_snapshot": dict(fleet_snapshot),
    }


def _cleanup_result(
    target: DualRecoveryTarget,
    request: Mapping[str, object],
    fleet_snapshot: Mapping[str, object],
) -> dict[str, object]:
    review = request["review"]
    assert isinstance(review, Mapping)
    return {
        "request_key": request["request_key"],
        "review_digest": request["review_digest"],
        "application_state": "succeeded",
        "application_id": "cleanup-application-82",
        "application_updated_at": fleet_snapshot["generated_at"],
        "profile_number": review["profile_number"],
        "profile_id": review["profile_id"],
        "profile_digest": review["profile_digest"],
        "plan_digest": review["plan_digest"],
        "stop_receipts": [
            {
                "run_id": target.run_id,
                "operation_state": "succeeded",
                "final_observation": {
                    "phase": "final_verify",
                    "final_verified": True,
                    "run_id": target.run_id,
                    "state": "stopped",
                    "route_state": "withdrawn",
                    "ranks": [
                        {"node_id": node_id, "rank": rank, "state": "stopped"}
                        for node_id, rank in sorted(
                            target.node_to_rank.items(), key=lambda item: item[1]
                        )
                    ],
                },
            }
        ],
        "fleet_snapshot": dict(fleet_snapshot),
    }


def _reconciled_cleanup_result(
    request: Mapping[str, object], fleet_snapshot: Mapping[str, object]
) -> dict[str, object]:
    prior = request["prior_receipt"]
    assert isinstance(prior, Mapping)
    result = dict(prior)
    result["fleet_snapshot"] = dict(fleet_snapshot)
    return result


def _advance_to_rank_smoke(
    target: DualRecoveryTarget,
    ledger: EvidenceLedger,
    *,
    rank_loss_snapshot: Mapping[str, object] | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    endpoints = {"present": True}
    verified: list[dict[str, object]] = []
    smoke_requests: list[dict[str, object]] = []
    snapshots: list[dict[str, object]] = [
        dict(
            rank_loss_snapshot
            or _snapshot(1, state="degraded-online-failure", degraded_route="published")
        ),
        _snapshot(2, state="degraded-online-failure", cursor=10),
        _snapshot(3, state="serving", cursor=10),
        _snapshot(4, state="serving", cursor=10),
    ]

    def observe_fleet() -> Mapping[str, object]:
        return snapshots.pop(0)

    def verify(request: Mapping[str, object]) -> Mapping[str, object]:
        verified.append(dict(request))
        return _serving_receipt(target, request)

    def smoke(request: Mapping[str, object]) -> Mapping[str, object]:
        smoke_requests.append(dict(request))
        return _smoke_receipt(target, request)

    loss = observe_dual_batch(
        target,
        ledger,
        observe_fleet=observe_fleet,
        endpoint_exists=lambda _alias: endpoints["present"],
        verify_serving=verify,
        run_fixture_smoke=smoke,
    )
    assert loss.checkpoint == "route-withdrawal"
    endpoints["present"] = False
    withdrawal = observe_dual_batch(
        target,
        ledger,
        observe_fleet=observe_fleet,
        endpoint_exists=lambda _alias: endpoints["present"],
        verify_serving=verify,
        run_fixture_smoke=smoke,
    )
    assert withdrawal.checkpoint == "rank-recovery-and-smoke"
    endpoints["present"] = True
    first_smoke = observe_dual_batch(
        target,
        ledger,
        observe_fleet=observe_fleet,
        endpoint_exists=lambda _alias: endpoints["present"],
        verify_serving=verify,
        run_fixture_smoke=smoke,
    )
    assert first_smoke.status == "rank-recovery-smoke-in-progress"
    second_smoke = observe_dual_batch(
        target,
        ledger,
        observe_fleet=observe_fleet,
        endpoint_exists=lambda _alias: endpoints["present"],
        verify_serving=verify,
        run_fixture_smoke=smoke,
    )
    assert second_smoke.status == "rank-recovery-smoke-completed"
    return verified, smoke_requests, snapshots


def test_dual_recovery_is_resumable_and_binds_cleanup_and_idle_restarts(
    tmp_path: Path,
) -> None:
    ledger = EvidenceLedger(tmp_path / "dual-evidence.jsonl")
    target = _target(ledger)
    verified: list[dict[str, object]] = []
    smokes: list[dict[str, object]] = []
    fake_physical_actions: list[str] = []
    endpoint = {"present": False}
    live_state = {
        "snapshot": _snapshot(
            1, state="degraded-online-failure", degraded_route="published"
        )
    }

    def observe_fleet() -> Mapping[str, object]:
        return live_state["snapshot"]

    def verify(request: Mapping[str, object]) -> Mapping[str, object]:
        verified.append(dict(request))
        return _serving_receipt(target, request)

    def smoke(request: Mapping[str, object]) -> Mapping[str, object]:
        smokes.append(dict(request))
        return _smoke_receipt(target, request)

    waiting_loss = observe_dual_batch(
        target,
        ledger,
        observe_fleet=observe_fleet,
        endpoint_exists=lambda _alias: endpoint["present"],
        verify_serving=verify,
        run_fixture_smoke=smoke,
    )
    assert waiting_loss.status == "checkpoint-observed"
    assert waiting_loss.checkpoint == "route-withdrawal"
    assert not verified and not smokes and not fake_physical_actions

    live_state["snapshot"] = _snapshot(2, state="degraded-online-failure", cursor=10)
    endpoint["present"] = False
    route_observed = observe_dual_batch(
        target,
        ledger,
        observe_fleet=observe_fleet,
        endpoint_exists=lambda _alias: endpoint["present"],
        verify_serving=verify,
        run_fixture_smoke=smoke,
    )
    assert route_observed.status == "checkpoint-observed"
    assert route_observed.checkpoint == "rank-recovery-and-smoke"

    live_state["snapshot"] = _snapshot(3, state="serving", cursor=10)
    endpoint["present"] = True
    progress = observe_dual_batch(
        target,
        ledger,
        observe_fleet=observe_fleet,
        endpoint_exists=lambda _alias: endpoint["present"],
        verify_serving=verify,
        run_fixture_smoke=smoke,
    )
    assert progress.status == "rank-recovery-smoke-in-progress"
    assert [request["node_id"] for request in smokes] == [NODE_A]
    assert verified[0]["fleet_snapshot"] is live_state["snapshot"]

    live_state["snapshot"] = _snapshot(4, state="serving", cursor=10)
    progress = observe_dual_batch(
        target,
        ledger,
        observe_fleet=observe_fleet,
        endpoint_exists=lambda _alias: endpoint["present"],
        verify_serving=verify,
        run_fixture_smoke=smoke,
    )
    assert progress.status == "rank-recovery-smoke-completed"
    assert [request["node_id"] for request in smokes] == [NODE_A, NODE_B]
    assert [request["rank"] for request in smokes] == [0, 1]
    assert len({request["request_key"] for request in smokes}) == 2
    assert not fake_physical_actions

    review = review_dual_cleanup(
        target,
        ledger,
        prepare_cleanup=lambda request: _cleanup_review(target, request),
    )
    applied: list[dict[str, object]] = []
    denied = apply_dual_cleanup(
        target,
        ledger,
        prepare_cleanup=lambda request: _cleanup_review(target, request),
        apply_authorized=False,
        cleanup_to_idle=lambda request: applied.append(dict(request)) or {},
        reconcile_cleanup=lambda _request: pytest.fail(
            "no completed cleanup exists to reconcile"
        ),
        endpoint_exists=lambda _alias: endpoint["present"],
    )
    assert denied.status == "awaiting-explicit-cleanup"
    assert not applied

    cleanup_snapshot = _snapshot(5, state="idle", cursor=10)

    def cleanup(request: Mapping[str, object]) -> Mapping[str, object]:
        applied.append(dict(request))
        return _cleanup_result(target, request, cleanup_snapshot)

    endpoint["present"] = False
    cleaned = apply_dual_cleanup(
        target,
        ledger,
        prepare_cleanup=lambda request: _cleanup_review(target, request),
        apply_authorized=True,
        cleanup_to_idle=cleanup,
        reconcile_cleanup=lambda _request: pytest.fail(
            "the original cleanup application has not completed yet"
        ),
        endpoint_exists=lambda _alias: endpoint["present"],
    )
    assert cleaned.status == "cleanup-completed"
    assert applied[0]["request_key"] == review.request_key
    assert applied[0]["review_digest"] == review.review_digest
    assert applied[0]["stop_run_ids"] == [RUN_ID]
    assert applied[0]["stop_aliases"] == [ALIAS]
    assert not fake_physical_actions

    progress = observe_dual_batch(
        target,
        ledger,
        observe_fleet=observe_fleet,
        endpoint_exists=lambda _alias: endpoint["present"],
        verify_serving=verify,
        run_fixture_smoke=smoke,
    )
    assert progress.status == "awaiting-host-offline"
    assert progress.node_id == NODE_A

    live_state["snapshot"] = _snapshot(6, state="idle", cursor=10, offline_node=NODE_A)
    progress = observe_dual_batch(
        target,
        ledger,
        observe_fleet=observe_fleet,
        endpoint_exists=lambda _alias: endpoint["present"],
        verify_serving=verify,
        run_fixture_smoke=smoke,
    )
    assert progress.status == "awaiting-host-online"
    assert progress.node_id == NODE_A

    live_state["snapshot"] = _snapshot(
        7,
        state="idle",
        cursor=10,
        boots={NODE_A: "boot-a-2", NODE_B: "boot-b-1"},
    )
    progress = observe_dual_batch(
        target,
        ledger,
        observe_fleet=observe_fleet,
        endpoint_exists=lambda _alias: endpoint["present"],
        verify_serving=verify,
        run_fixture_smoke=smoke,
    )
    assert progress.status == "awaiting-host-offline"
    assert progress.node_id == NODE_B

    live_state["snapshot"] = _snapshot(8, state="idle", cursor=10, offline_node=NODE_B)
    progress = observe_dual_batch(
        target,
        ledger,
        observe_fleet=observe_fleet,
        endpoint_exists=lambda _alias: endpoint["present"],
        verify_serving=verify,
        run_fixture_smoke=smoke,
    )
    assert progress.status == "awaiting-host-online"
    assert progress.node_id == NODE_B

    live_state["snapshot"] = _snapshot(
        9,
        state="idle",
        cursor=10,
        boots={NODE_A: "boot-a-2", NODE_B: "boot-b-2"},
    )
    progress = observe_dual_batch(
        target,
        ledger,
        observe_fleet=observe_fleet,
        endpoint_exists=lambda _alias: endpoint["present"],
        verify_serving=verify,
        run_fixture_smoke=smoke,
    )
    assert progress.status == "host-restarts-complete"

    live_state["snapshot"] = _snapshot(
        10,
        state="idle",
        cursor=10,
        boots={NODE_A: "boot-a-2", NODE_B: "boot-b-2"},
    )
    progress = observe_dual_batch(
        target,
        ledger,
        observe_fleet=observe_fleet,
        endpoint_exists=lambda _alias: endpoint["present"],
        verify_serving=verify,
        run_fixture_smoke=smoke,
    )
    assert progress.status == "complete"
    assert not fake_physical_actions

    replay_requests: list[Mapping[str, object]] = []

    def reconcile(request: Mapping[str, object]) -> Mapping[str, object]:
        replay_requests.append(dict(request))
        return _reconciled_cleanup_result(request, live_state["snapshot"])

    replayed = apply_dual_cleanup(
        target,
        ledger,
        prepare_cleanup=lambda _request: pytest.fail(
            "completed cleanup must not create a new preview"
        ),
        apply_authorized=True,
        cleanup_to_idle=lambda _request: pytest.fail(
            "completed cleanup must not submit another stop"
        ),
        reconcile_cleanup=reconcile,
        endpoint_exists=lambda _alias: False,
    )
    assert replayed.status == "cleanup-completed"
    assert replay_requests[0]["resume_completed"] is True
    assert replay_requests[0]["application_id"] == "cleanup-application-82"
    assert replay_requests[0]["request_key"] == review.request_key
    assert replay_requests[0]["review_digest"] == review.review_digest
    assert any(
        record.get("event") == "dual_recovery.cleanup.reconciled"
        for record in ledger.records
    )

    resumed = EvidenceLedger(ledger.path)
    final = observe_dual_batch(
        target,
        resumed,
        observe_fleet=lambda: pytest.fail("completed ladder must not replay callbacks"),
        endpoint_exists=lambda _alias: pytest.fail(
            "completed ladder must not query endpoint"
        ),
        verify_serving=lambda _request: pytest.fail(
            "completed ladder must not reverify"
        ),
        run_fixture_smoke=lambda _request: pytest.fail(
            "completed smoke must not replay"
        ),
    )
    assert final.status == "complete"
    replayed_again = apply_dual_cleanup(
        target,
        resumed,
        prepare_cleanup=lambda _request: pytest.fail(
            "durable cleanup replay must not preview again"
        ),
        apply_authorized=True,
        cleanup_to_idle=lambda _request: pytest.fail(
            "durable cleanup replay must not resubmit the stop"
        ),
        reconcile_cleanup=lambda request: _reconciled_cleanup_result(
            request,
            _snapshot(
                11,
                state="idle",
                cursor=10,
                boots={NODE_A: "boot-a-2", NODE_B: "boot-b-2"},
            ),
        ),
        endpoint_exists=lambda _alias: False,
    )
    assert replayed_again.status == "cleanup-completed"
    assert (
        len(
            [
                record
                for record in resumed.records
                if record.get("event") == "dual_recovery.cleanup.reconciled"
            ]
        )
        == 1
    )
    assert len(smokes) == 2


def test_rank_loss_waits_for_withdrawn_route_and_fresh_timestamp_not_cursor(
    tmp_path: Path,
) -> None:
    ledger = EvidenceLedger(tmp_path / "rank-loss-route.jsonl")
    target = _target(ledger)
    endpoint = {"present": True}
    snapshot = {
        "value": _snapshot(
            2,
            state="degraded-online-failure",
            cursor=12,
            degraded_route="published",
        )
    }
    calls = {"verify": 0, "smoke": 0}
    kwargs = {
        "observe_fleet": lambda: snapshot["value"],
        "endpoint_exists": lambda _alias: endpoint["present"],
        "verify_serving": lambda _request: (
            calls.__setitem__("verify", calls["verify"] + 1) or {}
        ),
        "run_fixture_smoke": lambda _request: (
            calls.__setitem__("smoke", calls["smoke"] + 1) or {}
        ),
    }
    waiting = observe_dual_batch(target, ledger, **kwargs)
    assert waiting.status == "checkpoint-observed"
    assert waiting.checkpoint == "route-withdrawal"
    rank_loss_records = [
        record
        for record in ledger.records
        if record.get("event") == "rank-loss.observed"
    ]
    assert len(rank_loss_records) == 1
    snapshot["value"] = _snapshot(3, state="degraded-online-failure", cursor=12)
    waiting_withdrawal = observe_dual_batch(target, ledger, **kwargs)
    assert waiting_withdrawal.status == "awaiting-route-withdrawal"
    assert not [
        record
        for record in ledger.records
        if record.get("event") == "route-withdrawal.observed"
    ]
    endpoint["present"] = False
    snapshot["value"] = _snapshot(4, state="degraded-online-failure", cursor=12)
    observed = observe_dual_batch(target, ledger, **kwargs)
    assert observed.status == "checkpoint-observed"
    route_records = [
        record
        for record in ledger.records
        if record.get("event") == "route-withdrawal.observed"
    ]
    assert len(route_records) == 1
    assert route_records[0]["record_sha256"] != rank_loss_records[0]["record_sha256"]
    assert (
        route_records[0]["payload"]["rank_loss_record_sha256"]
        == rank_loss_records[0]["record_sha256"]
    )
    assert calls == {"verify": 0, "smoke": 0}

    snapshot["value"] = _snapshot(4, state="serving", cursor=12)
    with pytest.raises(QualificationError, match="Fleet observation is stale"):
        observe_dual_batch(target, ledger, **kwargs)
    assert calls == {"verify": 0, "smoke": 0}


def test_changed_cleanup_review_and_wrong_stop_receipt_fail_before_completion(
    tmp_path: Path,
) -> None:
    ledger = EvidenceLedger(tmp_path / "cleanup-review.jsonl")
    target = _target(ledger)
    _advance_to_rank_smoke(target, ledger)
    review_variant = {"profile_digest": CLEAN_PROFILE}

    def prepare(request: Mapping[str, object]) -> Mapping[str, object]:
        result = _cleanup_review(target, request)
        result["profile_digest"] = review_variant["profile_digest"]
        return result

    reviewed = review_dual_cleanup(target, ledger, prepare_cleanup=prepare)
    review_variant["profile_digest"] = "2" * 64
    cleanup_calls: list[Mapping[str, object]] = []
    with pytest.raises(QualificationError, match="preview changed"):
        apply_dual_cleanup(
            target,
            ledger,
            prepare_cleanup=prepare,
            apply_authorized=True,
            cleanup_to_idle=lambda request: cleanup_calls.append(request) or {},
            reconcile_cleanup=lambda _request: pytest.fail(
                "no completed cleanup exists to reconcile"
            ),
            endpoint_exists=lambda _alias: False,
        )
    assert reviewed.review_digest
    assert not cleanup_calls
    assert not [
        record
        for record in ledger.records
        if record.get("event") == "dual_recovery.cleanup.completed"
    ]


def test_wrong_rank_smoke_receipt_is_rejected_without_cross_lane_attribution(
    tmp_path: Path,
) -> None:
    ledger = EvidenceLedger(tmp_path / "wrong-rank-smoke.jsonl")
    target = _target(ledger)
    endpoint = {"present": False}
    snapshots = [
        _snapshot(1, state="degraded-online-failure", degraded_route="published"),
        _snapshot(2, state="degraded-online-failure"),
        _snapshot(3, state="serving"),
    ]
    assert (
        observe_dual_batch(
            target,
            ledger,
            observe_fleet=lambda: snapshots.pop(0),
            endpoint_exists=lambda _alias: endpoint["present"],
            verify_serving=lambda request: _serving_receipt(target, request),
            run_fixture_smoke=lambda _request: {},
        ).status
        == "checkpoint-observed"
    )
    assert (
        observe_dual_batch(
            target,
            ledger,
            observe_fleet=lambda: snapshots.pop(0),
            endpoint_exists=lambda _alias: endpoint["present"],
            verify_serving=lambda request: _serving_receipt(target, request),
            run_fixture_smoke=lambda _request: {},
        ).checkpoint
        == "rank-recovery-and-smoke"
    )
    endpoint["present"] = True

    def wrong_rank(request: Mapping[str, object]) -> Mapping[str, object]:
        receipt = _smoke_receipt(target, request)
        receipt["rank"] = 1 - int(request["rank"])
        return receipt

    with pytest.raises(QualificationError, match="changed its run, rank, or cases"):
        observe_dual_batch(
            target,
            ledger,
            observe_fleet=lambda: snapshots.pop(0),
            endpoint_exists=lambda _alias: endpoint["present"],
            verify_serving=lambda request: _serving_receipt(target, request),
            run_fixture_smoke=wrong_rank,
        )
    assert not [
        record
        for record in ledger.records
        if record.get("event") == "rank-recovery.smoke-node-completed"
    ]


def test_cleanup_rejects_extra_fleet_workload_even_with_successful_stop_receipt(
    tmp_path: Path,
) -> None:
    ledger = EvidenceLedger(tmp_path / "cleanup-extra-run.jsonl")
    target = _target(ledger)
    _advance_to_rank_smoke(target, ledger)
    prepare = lambda request: _cleanup_review(target, request)
    review_dual_cleanup(target, ledger, prepare_cleanup=prepare)
    dirty = _snapshot(5, state="idle")
    nodes = dirty["nodes"]
    assert isinstance(nodes, list)
    first = nodes[0]
    assert isinstance(first, dict)
    first["loaded"] = [
        {
            **_presence(
                NODE_A,
                0,
                state="running",
                route="published",
                healthy=True,
                group_state="healthy",
                present_ranks=[0, 1],
            ),
            "run_id": "unreviewed-extra-run",
            "alias": "unreviewed-extra-route",
        }
    ]

    with pytest.raises(QualificationError, match="not idle after dual cleanup"):
        apply_dual_cleanup(
            target,
            ledger,
            prepare_cleanup=prepare,
            apply_authorized=True,
            cleanup_to_idle=lambda request: _cleanup_result(target, request, dirty),
            reconcile_cleanup=lambda _request: pytest.fail(
                "no completed cleanup exists to reconcile"
            ),
            endpoint_exists=lambda _alias: False,
        )
    assert not [
        record
        for record in ledger.records
        if record.get("event") == "dual_recovery.cleanup.completed"
    ]


def test_cleanup_waits_for_runtime_claims_but_retains_disk_cache(
    tmp_path: Path,
) -> None:
    ledger = EvidenceLedger(tmp_path / "cleanup-runtime-claims.jsonl")
    target = _target(ledger)
    _advance_to_rank_smoke(target, ledger)
    prepare = lambda request: _cleanup_review(target, request)
    review_dual_cleanup(target, ledger, prepare_cleanup=prepare)
    idle = _snapshot(5, state="idle")
    nodes = idle["nodes"]
    assert isinstance(nodes, list)
    first = nodes[0]
    assert isinstance(first, dict)
    reservations = first["reservations"]
    assert isinstance(reservations, dict)
    reservations["gpu_memory_bytes"] = 1024

    with pytest.raises(QualificationError, match="runtime Fleet reservations remain"):
        apply_dual_cleanup(
            target,
            ledger,
            prepare_cleanup=prepare,
            apply_authorized=True,
            cleanup_to_idle=lambda request: _cleanup_result(target, request, idle),
            reconcile_cleanup=lambda _request: pytest.fail(
                "no completed cleanup exists to reconcile"
            ),
            endpoint_exists=lambda _alias: False,
        )
    assert reservations["disk_bytes"] == 500
    assert not [
        record
        for record in ledger.records
        if record.get("event") == "dual_recovery.cleanup.completed"
    ]


def test_cleanup_and_reconciliation_require_live_idle_selected_sparks(
    tmp_path: Path,
) -> None:
    ledger = EvidenceLedger(tmp_path / "cleanup-live-hosts.jsonl")
    target = _target(ledger)
    _advance_to_rank_smoke(target, ledger)
    prepare = lambda request: _cleanup_review(target, request)
    review_dual_cleanup(target, ledger, prepare_cleanup=prepare)
    endpoint = lambda _alias: False

    offline = _snapshot(5, state="idle", offline_node=NODE_A)
    with pytest.raises(
        QualificationError, match="lacks live telemetry from both Sparks"
    ):
        apply_dual_cleanup(
            target,
            ledger,
            prepare_cleanup=prepare,
            apply_authorized=True,
            cleanup_to_idle=lambda request: _cleanup_result(target, request, offline),
            reconcile_cleanup=lambda _request: pytest.fail(
                "no completed cleanup exists to reconcile"
            ),
            endpoint_exists=endpoint,
        )
    assert not [
        record
        for record in ledger.records
        if record.get("event") == "dual_recovery.cleanup.completed"
    ]

    idle = _snapshot(5, state="idle")
    completed = apply_dual_cleanup(
        target,
        ledger,
        prepare_cleanup=prepare,
        apply_authorized=True,
        cleanup_to_idle=lambda request: _cleanup_result(target, request, idle),
        reconcile_cleanup=lambda _request: pytest.fail(
            "the original cleanup application has not completed yet"
        ),
        endpoint_exists=endpoint,
    )
    assert completed.status == "cleanup-completed"

    offline_reconciliation = _snapshot(6, state="idle", offline_node=NODE_A)
    with pytest.raises(
        QualificationError, match="lacks live telemetry from both selected Sparks"
    ):
        apply_dual_cleanup(
            target,
            ledger,
            prepare_cleanup=lambda _request: pytest.fail(
                "completed cleanup must not create a new preview"
            ),
            apply_authorized=True,
            cleanup_to_idle=lambda _request: pytest.fail(
                "completed cleanup must not resubmit the stop"
            ),
            reconcile_cleanup=lambda request: _reconciled_cleanup_result(
                request, offline_reconciliation
            ),
            endpoint_exists=endpoint,
        )
    assert not [
        record
        for record in ledger.records
        if record.get("event") == "dual_recovery.cleanup.reconciled"
    ]


def test_failed_dual_canary_cleanup_releases_only_its_exact_run_and_reconciles(
    tmp_path: Path,
) -> None:
    ledger = EvidenceLedger(tmp_path / "failed-dual-cleanup.jsonl")
    target = _failed_target(ledger)
    prepared: list[Mapping[str, object]] = []
    applied: list[Mapping[str, object]] = []
    reconciled: list[Mapping[str, object]] = []

    def prepare(request: Mapping[str, object]) -> Mapping[str, object]:
        prepared.append(dict(request))
        return _failed_cleanup_review(target, request)

    review = review_failed_dual_cleanup(target, ledger, prepare_cleanup=prepare)
    assert review.receipt["terminal_event"] == "canary.failed"
    assert review.receipt["terminal_record_sha256"] == target.canary_record_sha256
    assert review.receipt["source_application_id"] == target.application_id
    assert (
        review.receipt["source_application_request_key"]
        == target.application_request_key
    )
    assert review.receipt["stop_run_ids"] == [RUN_ID]
    assert review.receipt["node_to_rank"] == {NODE_A: 0, NODE_B: 1}

    awaiting = apply_failed_dual_cleanup(
        target,
        ledger,
        prepare_cleanup=prepare,
        apply_authorized=False,
        cleanup_to_idle=lambda _request: pytest.fail(
            "failed-canary cleanup must wait for explicit apply"
        ),
        reconcile_cleanup=lambda _request: pytest.fail(
            "cleanup has not completed and cannot be reconciled"
        ),
        endpoint_exists=lambda _alias: False,
    )
    assert awaiting.status == "awaiting-explicit-failed-cleanup"
    assert not applied

    cleanup_snapshot = _snapshot(5, state="idle")

    def cleanup(request: Mapping[str, object]) -> Mapping[str, object]:
        applied.append(dict(request))
        return _failed_cleanup_result(target, request, cleanup_snapshot)

    completed = apply_failed_dual_cleanup(
        target,
        ledger,
        prepare_cleanup=prepare,
        apply_authorized=True,
        cleanup_to_idle=cleanup,
        reconcile_cleanup=lambda _request: pytest.fail(
            "initial failed-canary cleanup should apply, not reconcile"
        ),
        endpoint_exists=lambda _alias: False,
    )
    assert completed.status == "failed-cleanup-completed"
    assert completed.receipt_sha256
    assert applied[0]["request_key"] == review.request_key
    assert applied[0]["review_digest"] == review.review_digest
    assert applied[0]["source_application_id"] == target.application_id
    assert (
        applied[0]["source_application_request_key"] == target.application_request_key
    )
    assert applied[0]["run_id"] == RUN_ID
    assert applied[0]["stop_run_ids"] == [RUN_ID]
    assert applied[0]["node_to_rank"] == {NODE_A: 0, NODE_B: 1}
    assert not [
        record
        for record in ledger.records
        if record.get("event")
        in {
            "rank-loss.observed",
            "route-withdrawal.observed",
            "rank-recovery.smoke-completed",
            "dual_recovery.completed",
            "recipe.spark-accepted",
            "recipe.failed",
        }
    ]

    def reconcile(request: Mapping[str, object]) -> Mapping[str, object]:
        reconciled.append(dict(request))
        return _reconciled_cleanup_result(request, _snapshot(6, state="idle"))

    replayed = apply_failed_dual_cleanup(
        target,
        EvidenceLedger(ledger.path),
        prepare_cleanup=lambda _request: pytest.fail(
            "completed failed cleanup must not create another preview"
        ),
        apply_authorized=True,
        cleanup_to_idle=lambda _request: pytest.fail(
            "completed failed cleanup must not submit another stop"
        ),
        reconcile_cleanup=reconcile,
        endpoint_exists=lambda _alias: False,
    )
    assert replayed.status == "failed-cleanup-completed"
    assert reconciled[0]["resume_completed"] is True
    assert reconciled[0]["application_id"] == "33333333-3333-4333-8333-333333333333"
    assert reconciled[0]["source_application_id"] == target.application_id
    assert (
        reconciled[0]["source_application_request_key"]
        == target.application_request_key
    )
    assert (
        len(
            [
                record
                for record in EvidenceLedger(ledger.path).records
                if record.get("event") == "dual_recovery.failed_cleanup.reconciled"
            ]
        )
        == 1
    )


def test_failed_dual_canary_without_run_identity_cannot_release_on_absence_alone(
    tmp_path: Path,
) -> None:
    ledger = EvidenceLedger(tmp_path / "failed-dual-runless.jsonl")
    target = _failed_target(ledger, run_id=None)
    preview_calls: list[Mapping[str, object]] = []
    with pytest.raises(
        QualificationError,
        match="reconcile the original Controller profile application.*typed no-effects proof",
    ):
        review_failed_dual_cleanup(
            target,
            ledger,
            prepare_cleanup=lambda request: preview_calls.append(dict(request)) or {},
        )
    assert not preview_calls
    assert not [
        record
        for record in ledger.records
        if record.get("event", "").startswith("dual_recovery.failed_cleanup.")
    ]
