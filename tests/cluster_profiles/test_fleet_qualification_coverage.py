from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from cluster_profiles.fleet_qualification_dual_recovery import (
    DualRecoveryTarget,
    apply_dual_cleanup,
    observe_dual_batch,
    review_dual_cleanup,
)
from vonk_forge_contracts.qualification_authority import (
    RecoveryCoverageDefinition,
    RecoveryCoverageReceiptEnvelope,
    recovery_coverage_id,
    recovery_coverage_receipt_json_schema,
    recovery_receipt_sha256,
)

from cluster_profiles.fleet_qualification import EvidenceLedger, QualificationError
from cluster_profiles.fleet_qualification_coverage import (
    _normalize_receipt_for_hash,
    build_recovery_coverage_receipt,
    validate_recovery_coverage_receipt,
)

_RECIPE = "acme/small-model"
_NODE = "spk_0123456789abcdef0123456789abcdef"
_DUAL_NODES = (
    "spk_11111111111111111111111111111111",
    "spk_22222222222222222222222222222222",
)
_SHA = "a" * 64
_DUAL_T0 = datetime(2026, 9, 25, tzinfo=UTC)


def _canonical_sha(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _scope() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    recipe_sha = "1" * 64
    package_sha = "2" * 64
    model_sha = "3" * 64
    runtime_sha = "4" * 64
    topology_sha = "5" * 64
    definition_model = RecoveryCoverageDefinition.model_validate(
        {
            "failure_mode": "single-host-restart",
            "representative_recipe": _RECIPE,
            "members": [
                {
                    "recipe": _RECIPE,
                    "recipe_content_sha256": recipe_sha,
                    "package_sha256": package_sha,
                    "model_content_sha256s": [model_sha],
                    "runtime_stack_sha256": runtime_sha,
                    "topology_sha256": topology_sha,
                }
            ],
            "shared": False,
            "equivalence_rationale": "Dedicated exact recipe evidence.",
            "invalidated_by": [
                "recipe_content_sha256",
                "package_sha256",
                "model_content_sha256s",
                "runtime_stack_sha256",
                "topology_sha256",
                "coverage_membership",
                "runtime_image_digest",
                "platform_build_sha256",
                "agent_build_sha256",
                "target_node_ids",
                "smoke_receipt_sha256",
            ],
        }
    )
    coverage_id = recovery_coverage_id(definition_model)
    definition: dict[str, object] = {
        **definition_model.model_dump(mode="json", exclude_none=True),
        "coverage_id": coverage_id,
    }
    reference: dict[str, object] = {
        "coverage_id": coverage_id,
        "failure_mode": "single-host-restart",
        "representative_recipe": _RECIPE,
        "role": "dedicated",
    }
    row: dict[str, object] = {
        "key": _RECIPE,
        "content_sha256": recipe_sha,
        "node_count": 1,
        "package": {"sha256": package_sha},
        "model_license_refs": [{"content_sha256": model_sha}],
        "runtime_stack_sha256": runtime_sha,
        "topology_sha256": topology_sha,
        "smoke_cases": ["health"],
        "recovery_coverage_refs": [reference],
    }
    return definition, reference, row


def _scope_for_mode(
    mode: str, *, node_count: int
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    definition, _, row = _scope()
    member = definition["members"]
    assert isinstance(member, list)
    definition_model = RecoveryCoverageDefinition.model_validate(
        {
            **{key: value for key, value in definition.items() if key != "coverage_id"},
            "failure_mode": mode,
        }
    )
    coverage_id = recovery_coverage_id(definition_model)
    typed_definition: dict[str, object] = {
        **definition_model.model_dump(mode="json", exclude_none=True),
        "coverage_id": coverage_id,
    }
    reference: dict[str, object] = {
        "coverage_id": coverage_id,
        "failure_mode": mode,
        "representative_recipe": _RECIPE,
        "role": "dedicated",
    }
    typed_row = dict(row)
    typed_row["node_count"] = node_count
    typed_row["recovery_coverage_refs"] = [reference]
    return typed_definition, reference, typed_row


def _preparation(row: dict[str, object]) -> dict[str, object]:
    model_sha = row["model_license_refs"][0]["content_sha256"]  # type: ignore[index]
    return {
        "recipe_revision_sha256": row["content_sha256"],
        "model_content_sha256": model_sha,
        "dependency_model_content_sha256": [],
        "artifact_set_sha256": "6" * 64,
        "image_digest": "sha256:" + "7" * 64,
        "target_node_ids": [_NODE],
        "architecture": "linux-arm64",
    }


def _provenance(row: dict[str, object]) -> dict[str, dict[str, object]]:
    package_sha = row["package"]["sha256"]  # type: ignore[index]
    return {
        _NODE: {
            "schema_version": 2,
            "platform": [
                {
                    "boundary": "controller_deployment",
                    "state": "observed",
                    "image_digest": "sha256:" + "8" * 64,
                    "evidence": {"freshness": "current", "source": "test"},
                }
            ],
            "agents": [
                {
                    "boundary": "agent_deployment",
                    "node_id": _NODE,
                    "connectivity": "recent",
                    "state": "online",
                    "build_digest": "sha256:" + "9" * 64,
                    "binary_sha256": "b" * 64,
                    "package_sha256": package_sha,
                    "evidence": {"freshness": "current", "source": "test"},
                    # Age is historical evidence; exact current contact and the
                    # matched package/build fields own reuse validity.
                    "package_evidence": {"freshness": "stale", "source": "old"},
                }
            ],
        }
    }


def _provenance_for_nodes(
    row: dict[str, object], node_ids: tuple[str, ...]
) -> dict[str, dict[str, object]]:
    package_sha = row["package"]["sha256"]  # type: ignore[index]
    return {
        node_id: {
            "schema_version": 2,
            "platform": [
                {
                    "boundary": "controller_deployment",
                    "state": "observed",
                    "image_digest": "sha256:" + "8" * 64,
                    "evidence": {"freshness": "current", "source": "test"},
                }
            ],
            "agents": [
                {
                    "boundary": "agent_deployment",
                    "node_id": node_id,
                    "connectivity": "recent",
                    "state": "online",
                    "build_digest": "sha256:" + ("9" if index == 0 else "a") * 64,
                    "binary_sha256": "b" * 64,
                    "package_sha256": package_sha,
                    "evidence": {"freshness": "current", "source": "test"},
                    "package_evidence": {"freshness": "stale", "source": "old"},
                }
            ],
        }
        for index, node_id in enumerate(node_ids)
    }


def _dual_snapshot(
    minute: int,
    *,
    state: str,
    cursor: int,
    boots: Mapping[str, str] | None = None,
    offline_node: str | None = None,
    degraded_route: str = "withdrawn",
) -> dict[str, object]:
    node_a, node_b = _DUAL_NODES
    boot_ids = boots or {node_a: "boot-a-1", node_b: "boot-b-1"}
    nodes: list[dict[str, object]] = []
    for node_id, rank in ((node_a, 0), (node_b, 1)):
        offline = node_id == offline_node
        if state == "serving":
            loaded = [_dual_presence(node_id, rank, healthy=True, route="published")]
        elif state == "degraded-online-failure":
            loaded = [
                _dual_presence(
                    node_id,
                    rank,
                    healthy=False,
                    route=degraded_route,
                    lost=node_id == node_b,
                )
            ]
        else:
            loaded = []
        observed_at = _DUAL_T0 + timedelta(minutes=minute - (1 if offline else 0))
        nodes.append(
            {
                "id": node_id,
                "connection": {"online_state": "offline" if offline else "online"},
                "telemetry": {
                    "freshness": "stale" if offline else "live",
                    "sample": {
                        "boot_id": boot_ids[node_id],
                        "observed_at": observed_at.isoformat(),
                    },
                },
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
        "generated_at": (_DUAL_T0 + timedelta(minutes=minute)).isoformat(),
        "authority_revision": "fleet-revision-3",
        "nodes": nodes,
    }


def _dual_presence(
    node_id: str,
    rank: int,
    *,
    healthy: bool,
    route: str,
    lost: bool = False,
) -> dict[str, object]:
    del node_id
    return {
        "run_id": "dual-run",
        "recipe_revision_id": "dual-revision",
        "alias": "dual-alias",
        "rank": rank,
        "expected_rank_count": 2,
        "member_node_ids": list(_DUAL_NODES),
        "present_ranks": [0, 1] if healthy else [0],
        "run_state": "running" if healthy else "degraded",
        "route_state": route,
        "rank_state": "lost" if lost else "running",
        "rank_fresh": healthy,
        "group_state": "healthy" if healthy else "degraded",
        "healthy": healthy,
    }


def _successful_single_lane_ledger(
    tmp_path: Path, row: dict[str, object], prep: dict[str, object]
) -> EvidenceLedger:
    campaign_id = "c" * 64
    batch_id = "batch-001"
    lane_id = 1
    run_id = "run-001"
    revision_id = "revision-001"
    alias = "q1"
    baseline_boot = "boot-before"
    recovered_boot = "boot-after"
    common: dict[str, object] = {
        "campaign_id": campaign_id,
        "batch_id": batch_id,
        "lane_id": lane_id,
        "target_digest": "d" * 64,
        "recipe_content_sha256": row["content_sha256"],
        "package_sha256": row["package"]["sha256"],  # type: ignore[index]
    }
    ledger = EvidenceLedger(tmp_path / "events.jsonl")
    canary = ledger.append(
        "canary.completed",
        plan_digest=campaign_id,
        recipe=_RECIPE,
        payload={
            **common,
            "recipe_content_sha256": row["content_sha256"],
            "package_sha256": row["package"]["sha256"],  # type: ignore[index]
            "exact_preparations": prep,
        },
    )
    intent = ledger.append(
        "lane_recovery.intent",
        plan_digest=campaign_id,
        recipe=_RECIPE,
        payload={**common, "canary_record_sha256s": [canary["record_sha256"]]},
    )
    reviewed = ledger.append(
        "lane_recovery.plan_reviewed",
        plan_digest=campaign_id,
        recipe=_RECIPE,
        payload={**common, "review_digest": "e" * 64},
    )
    transitioned = ledger.append(
        "lane_recovery.transitioned",
        plan_digest=campaign_id,
        recipe=_RECIPE,
        payload={**common, "active_run_id": run_id},
    )
    baseline = ledger.append(
        "lane_recovery.baseline",
        plan_digest=campaign_id,
        recipe=_RECIPE,
        payload={**common, "boot_id": baseline_boot},
    )
    offline = ledger.append(
        "lane_recovery.offline_observed",
        plan_digest=campaign_id,
        recipe=_RECIPE,
        payload={
            **common,
            "node_id": _NODE,
            "online_state": "offline",
            "baseline_boot_id": baseline_boot,
            "active_run_id": run_id,
        },
    )
    boot = ledger.append(
        "lane_recovery.boot_observed",
        plan_digest=campaign_id,
        recipe=_RECIPE,
        payload={
            **common,
            "node_id": _NODE,
            "baseline_boot_id": baseline_boot,
            "observed_boot_id": recovered_boot,
        },
    )
    node_to_rank = {_NODE: 0}
    serving = {
        "run_id": run_id,
        "recipe_revision_id": revision_id,
        "recipe_key": _RECIPE,
        "recipe_content_sha256": row["content_sha256"],
        "package_sha256": row["package"]["sha256"],  # type: ignore[index]
        "alias": alias,
        "node_to_rank": node_to_rank,
        "boot_id": recovered_boot,
        "route_state": "published",
        "healthy": True,
    }
    smoke_receipt = {
        "run_id": run_id,
        "recipe_revision_id": revision_id,
        "recipe_key": _RECIPE,
        "recipe_content_sha256": row["content_sha256"],
        "package_sha256": row["package"]["sha256"],  # type: ignore[index]
        "alias": alias,
        "node_to_rank": node_to_rank,
        "boot_id": recovered_boot,
        "cases": [{"case_id": "health", "passed": True}],
    }
    recovered = ledger.append(
        "lane_recovery.recovered",
        plan_digest=campaign_id,
        recipe=_RECIPE,
        payload={
            **common,
            "node_id": _NODE,
            "active_run_id": run_id,
            "baseline_boot_id": baseline_boot,
            "observed_boot_id": recovered_boot,
            "serving_receipt": serving,
        },
    )
    smoke = ledger.append(
        "lane_recovery.smoke_completed",
        plan_digest=campaign_id,
        recipe=_RECIPE,
        payload={
            **common,
            "active_run_id": run_id,
            "recovered_boot_id": recovered_boot,
            "smoke_receipt": smoke_receipt,
        },
    )
    completion_body = {
        **common,
        "baseline_boot_id": baseline_boot,
        "recovered_boot_id": recovered_boot,
        "event_refs": {
            "intent": intent["record_sha256"],
            "transitioned": transitioned["record_sha256"],
            "baseline": baseline["record_sha256"],
            "offline": offline["record_sha256"],
            "boot_observed": boot["record_sha256"],
            "recovered": recovered["record_sha256"],
            "smoke": smoke["record_sha256"],
        },
    }
    ledger.append(
        "lane_recovery.completed",
        plan_digest=campaign_id,
        recipe=_RECIPE,
        payload={
            **completion_body,
            "receipt_sha256": _canonical_sha(completion_body),
        },
    )
    assert (
        type(reviewed["sequence"]) is int
        and type(transitioned["sequence"]) is int
        and reviewed["sequence"] < transitioned["sequence"]
    )
    return ledger


def _build(
    tmp_path: Path,
) -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
    definition, reference, row = _scope()
    prep = _preparation(row)
    ledger = _successful_single_lane_ledger(tmp_path, row, prep)
    envelope = build_recovery_coverage_receipt(
        coverage_definition=definition,
        coverage_reference=reference,
        authority_row=row,
        campaign_id="c" * 64,
        batch_id="batch-001",
        lane_id=1,
        ledger_records=ledger.records,
        exact_preparation=prep,
        deployment_provenance_by_node=_provenance(row),
        receipt_schema=recovery_coverage_receipt_json_schema(),
    )
    return envelope, definition, reference, row


def test_builder_emits_a_canonical_content_addressed_receipt_from_verified_ledger(
    tmp_path: Path,
) -> None:
    envelope, definition, reference, row = _build(tmp_path)
    canonical = RecoveryCoverageReceiptEnvelope.model_validate(envelope)
    assert canonical.receipt.failure_mode == "single-host-restart"
    assert canonical.receipt.nodes[0].node_id == _NODE
    assert canonical.receipt.nodes[0].agent_build_sha256 == "9" * 64
    assert canonical.receipt.platform_build_sha256 == "8" * 64
    assert recovery_receipt_sha256(canonical.receipt) == envelope["receipt_sha256"]
    assert (
        validate_recovery_coverage_receipt(
            envelope,
            receipt_schema=recovery_coverage_receipt_json_schema(),
            coverage_definition=definition,
            coverage_reference=reference,
            authority_row=row,
        )
        == envelope
    )


def test_receipt_consumer_matches_canonical_missing_null_hash_normalization(
    tmp_path: Path,
) -> None:
    envelope, definition, reference, row = _build(tmp_path)
    raw_receipt = envelope["receipt"]
    assert isinstance(raw_receipt, dict)
    receipt = dict(raw_receipt)
    receipt["rank_recovery"] = None
    normalized_digest = _canonical_sha(_normalize_receipt_for_hash(receipt))
    assert normalized_digest == envelope["receipt_sha256"]
    with_null = {"receipt": receipt, "receipt_sha256": normalized_digest}
    canonical = RecoveryCoverageReceiptEnvelope.model_validate(with_null)
    assert recovery_receipt_sha256(canonical.receipt) == normalized_digest
    assert (
        validate_recovery_coverage_receipt(
            with_null,
            receipt_schema=recovery_coverage_receipt_json_schema(),
            coverage_definition=definition,
            coverage_reference=reference,
            authority_row=row,
        )["receipt_sha256"]
        == normalized_digest
    )


@pytest.mark.parametrize(
    "mutation",
    ["empty-nodes", "unchanged-boot", "duplicate-checkpoint", "forbidden-rank"],
)
def test_consumer_rejects_rehashed_receipts_that_canonical_validator_rejects(
    tmp_path: Path, mutation: str
) -> None:
    envelope, definition, reference, row = _build(tmp_path)
    receipt = cast(dict[str, Any], json.loads(json.dumps(envelope["receipt"])))
    if mutation == "empty-nodes":
        receipt["nodes"] = []
    elif mutation == "unchanged-boot":
        receipt["nodes"][0]["recovered_boot_id"] = receipt["nodes"][0][
            "baseline_boot_id"
        ]
    elif mutation == "duplicate-checkpoint":
        receipt["checkpoint_event_ids"][1] = receipt["checkpoint_event_ids"][0]
    else:
        receipt["rank_recovery"] = {
            "lost_node_id": _NODE,
            "recovered_node_id": _NODE,
            "survivor_node_id": "spk_abcdef0123456789abcdef0123456789",
            "node_builds": [
                {"node_id": _NODE, "agent_build_sha256": "9" * 64},
                {
                    "node_id": "spk_abcdef0123456789abcdef0123456789",
                    "agent_build_sha256": "a" * 64,
                },
            ],
            "rank_loss_event_id": "b" * 64,
            "route_withdrawal_event_id": "c" * 64,
            "rank_recovery_event_id": "d" * 64,
            "recovered_smoke_receipt_sha256": receipt["smoke_receipt_sha256"],
        }
    forged = {
        "receipt": receipt,
        "receipt_sha256": _canonical_sha(_normalize_receipt_for_hash(receipt)),
    }
    with pytest.raises(ValueError):
        RecoveryCoverageReceiptEnvelope.model_validate(forged)
    with pytest.raises(QualificationError):
        validate_recovery_coverage_receipt(
            forged,
            receipt_schema=recovery_coverage_receipt_json_schema(),
            coverage_definition=definition,
            coverage_reference=reference,
            authority_row=row,
        )


def test_builder_fails_closed_when_current_platform_or_agent_identity_is_missing(
    tmp_path: Path,
) -> None:
    definition, reference, row = _scope()
    prep = _preparation(row)
    ledger = _successful_single_lane_ledger(tmp_path, row, prep)
    provenance = _provenance(row)
    provenance[_NODE]["platform"][0]["evidence"]["freshness"] = "stale"  # type: ignore[index]
    with pytest.raises(QualificationError, match="Controller deployment image"):
        build_recovery_coverage_receipt(
            coverage_definition=definition,
            coverage_reference=reference,
            authority_row=row,
            campaign_id="c" * 64,
            batch_id="batch-001",
            lane_id=1,
            ledger_records=ledger.records,
            exact_preparation=prep,
            deployment_provenance_by_node=provenance,
            receipt_schema=recovery_coverage_receipt_json_schema(),
        )


def test_builder_rejects_a_ledger_record_tampered_after_append(tmp_path: Path) -> None:
    definition, reference, row = _scope()
    prep = _preparation(row)
    ledger = _successful_single_lane_ledger(tmp_path, row, prep)
    records = [dict(record) for record in ledger.records]
    records[1]["payload"] = {"batch_id": "forged"}
    with pytest.raises(QualificationError, match="hash verification"):
        build_recovery_coverage_receipt(
            coverage_definition=definition,
            coverage_reference=reference,
            authority_row=row,
            campaign_id="c" * 64,
            batch_id="batch-001",
            lane_id=1,
            ledger_records=records,
            exact_preparation=prep,
            deployment_provenance_by_node=_provenance(row),
            receipt_schema=recovery_coverage_receipt_json_schema(),
        )


def test_actual_dual_recovery_producer_roundtrips_rank_and_host_receipts(
    tmp_path: Path,
) -> None:
    rank_definition, rank_reference, rank_row = _scope_for_mode(
        "dual-rank-loss-recovery", node_count=2
    )
    host_definition, host_reference, host_row = _scope_for_mode(
        "dual-host-restart", node_count=2
    )
    node_a, node_b = _DUAL_NODES
    campaign_id = "c" * 64
    batch_id = "dual-batch-01"
    lane_id = 1
    run_id = "dual-run"
    revision_id = "dual-revision"
    alias = "dual-alias"
    prep = {
        "recipe_revision_sha256": rank_row["content_sha256"],
        "model_content_sha256": "3" * 64,
        "dependency_model_content_sha256": [],
        "artifact_set_sha256": "6" * 64,
        "image_digest": "sha256:" + "7" * 64,
        "target_node_ids": [node_a, node_b],
        "architecture": "linux-arm64",
    }
    ledger = EvidenceLedger(tmp_path / "actual-dual-evidence.jsonl")
    canary = ledger.append(
        "canary.completed",
        plan_digest=campaign_id,
        recipe=_RECIPE,
        payload={
            "batch_id": batch_id,
            "lane_id": lane_id,
            "recipe_content_sha256": rank_row["content_sha256"],
            "package_sha256": rank_row["package"]["sha256"],  # type: ignore[index]
            "run_id": run_id,
            "recipe_revision_id": revision_id,
            "alias": alias,
            "node_ids": [node_a, node_b],
            "node_to_rank": {node_a: 0, node_b: 1},
            "exact_preparations": prep,
            "smoke": {
                "endpoint_alias": alias,
                "recipe_content_sha256": rank_row["content_sha256"],
                "cases": [{"case_id": "health"}],
            },
        },
    )
    ledger.append(
        "rank-loss.pending",
        plan_digest=campaign_id,
        recipe=_RECIPE,
        payload={
            "batch_id": batch_id,
            "lane_id": lane_id,
            "failure_spark": node_b,
            "run_id": run_id,
            "revision_id": revision_id,
            "alias": alias,
            "node_ids": [node_a, node_b],
            "node_to_rank": {node_a: 0, node_b: 1},
        },
    )
    target = DualRecoveryTarget(
        campaign_id=campaign_id,
        batch_id=batch_id,
        lane_id=lane_id,
        recipe_key=_RECIPE,
        recipe_content_sha256=cast(str, rank_row["content_sha256"]),
        package_sha256=rank_row["package"]["sha256"],  # type: ignore[index]
        canary_record_sha256=cast(str, canary["record_sha256"]),
        run_id=run_id,
        recipe_revision_id=revision_id,
        alias=alias,
        node_ids=(node_a, node_b),
        node_to_rank={node_a: 0, node_b: 1},
        failure_node_id=node_b,
        smoke_case_ids=("health",),
        fleet_node_ids=(node_a, node_b),
        profile_number=17,
        profile_id="dual-profile",
        profile_digest="d" * 64,
        plan_digest="e" * 64,
    )
    snapshot_index = 0
    snapshots = [
        _dual_snapshot(
            1,
            state="degraded-online-failure",
            cursor=1,
            degraded_route="published",
        ),
        _dual_snapshot(
            2,
            state="degraded-online-failure",
            cursor=2,
            degraded_route="withdrawn",
        ),
        _dual_snapshot(3, state="serving", cursor=3),
        _dual_snapshot(4, state="serving", cursor=4),
        _dual_snapshot(6, state="idle", cursor=6, offline_node=node_a),
        _dual_snapshot(
            7,
            state="idle",
            cursor=7,
            boots={node_a: "boot-a-2", node_b: "boot-b-1"},
        ),
        _dual_snapshot(8, state="idle", cursor=8, offline_node=node_b),
        _dual_snapshot(
            9,
            state="idle",
            cursor=9,
            boots={node_a: "boot-a-2", node_b: "boot-b-2"},
        ),
        _dual_snapshot(
            10,
            state="idle",
            cursor=10,
            boots={node_a: "boot-a-2", node_b: "boot-b-2"},
        ),
    ]

    def observe_fleet() -> Mapping[str, object]:
        nonlocal snapshot_index
        result = snapshots[snapshot_index]
        snapshot_index += 1
        return result

    def verify_serving(request: Mapping[str, object]) -> Mapping[str, object]:
        fleet = request["fleet_snapshot"]
        assert isinstance(fleet, Mapping)
        rank_presence: list[dict[str, object]] = []
        for node in fleet["nodes"]:
            assert isinstance(node, Mapping)
            for presence in node["loaded"]:
                assert isinstance(presence, Mapping)
                if presence.get("run_id") == run_id:
                    rank_presence.append({"node_id": node["id"], **presence})
        return {
            "state": "succeeded",
            "run_id": run_id,
            "recipe_key": _RECIPE,
            "recipe_content_sha256": rank_row["content_sha256"],
            "package_sha256": rank_row["package"]["sha256"],  # type: ignore[index]
            "recipe_revision_id": revision_id,
            "alias": alias,
            "node_to_rank": {node_a: 0, node_b: 1},
            "rank_presence": rank_presence,
            "endpoint": {"api_base": "https://spark.example.invalid/v1"},
        }

    def run_fixture_smoke(request: Mapping[str, object]) -> Mapping[str, object]:
        node_id = str(request["node_id"])
        rank = {node_a: 0, node_b: 1}[node_id]
        return {
            "passed": True,
            "run_id": run_id,
            "alias": alias,
            "node_id": node_id,
            "rank": rank,
            "case_ids": ["health"],
            "fixture_receipt": {
                "endpoint_alias": alias,
                "recipe_content_sha256": rank_row["content_sha256"],
                "cases": [{"case_id": "health"}],
            },
            "record_sha256": ("9" if node_id == node_a else "a") * 64,
        }

    observe_kwargs = {
        "observe_fleet": observe_fleet,
        "endpoint_exists": lambda _endpoint: endpoint_present["value"],
        "verify_serving": verify_serving,
        "run_fixture_smoke": run_fixture_smoke,
    }
    endpoint_present = {"value": True}
    assert observe_dual_batch(target, ledger, **observe_kwargs).checkpoint == (
        "route-withdrawal"
    )
    endpoint_present["value"] = False
    assert observe_dual_batch(target, ledger, **observe_kwargs).checkpoint == (
        "rank-recovery-and-smoke"
    )
    endpoint_present["value"] = True
    assert observe_dual_batch(target, ledger, **observe_kwargs).status == (
        "rank-recovery-smoke-in-progress"
    )
    assert observe_dual_batch(target, ledger, **observe_kwargs).status == (
        "rank-recovery-smoke-completed"
    )
    endpoint_present["value"] = False

    def prepare_cleanup(request: Mapping[str, object]) -> Mapping[str, object]:
        return {
            "request_key": request["request_key"],
            "reviewed": True,
            "profile_number": 17,
            "profile_id": "dual-profile",
            "profile_digest": "f" * 64,
            "plan_digest": "1" * 64,
            "cleanup_mode": "dual-lane",
            "terminal_event": "rank-recovery.smoke-completed",
            "terminal_record_sha256": request["terminal_record_sha256"],
            "active_run_id": run_id,
            "stop_run_ids": [run_id],
            "stop_aliases": [alias],
            "fleet_node_ids": [node_a, node_b],
        }

    def cleanup_to_idle(request: Mapping[str, object]) -> Mapping[str, object]:
        fleet_snapshot = _dual_snapshot(5, state="idle", cursor=5)
        review = request["review"]
        assert isinstance(review, Mapping)
        return {
            "request_key": request["request_key"],
            "review_digest": request["review_digest"],
            "application_state": "succeeded",
            "application_id": "dual-cleanup-application",
            "application_updated_at": fleet_snapshot["generated_at"],
            "profile_number": review["profile_number"],
            "profile_id": review["profile_id"],
            "profile_digest": review["profile_digest"],
            "plan_digest": review["plan_digest"],
            "stop_receipts": [
                {
                    "run_id": run_id,
                    "operation_state": "succeeded",
                    "final_observation": {
                        "phase": "final_verify",
                        "final_verified": True,
                        "run_id": run_id,
                        "state": "stopped",
                        "route_state": "withdrawn",
                        "ranks": [
                            {
                                "node_id": node_a,
                                "rank": 0,
                                "role": "entrypoint",
                                "state": "stopped",
                            },
                            {
                                "node_id": node_b,
                                "rank": 1,
                                "role": "worker",
                                "state": "stopped",
                            },
                        ],
                    },
                }
            ],
            "fleet_snapshot": fleet_snapshot,
        }

    review_dual_cleanup(target, ledger, prepare_cleanup=prepare_cleanup)
    apply_dual_cleanup(
        target,
        ledger,
        prepare_cleanup=prepare_cleanup,
        apply_authorized=True,
        cleanup_to_idle=cleanup_to_idle,
        reconcile_cleanup=lambda _request: pytest.fail(
            "new cleanup must apply once before reconciliation"
        ),
        endpoint_exists=lambda _endpoint: False,
    )
    assert observe_dual_batch(target, ledger, **observe_kwargs).status == (
        "awaiting-host-offline"
    )
    assert observe_dual_batch(target, ledger, **observe_kwargs).status == (
        "awaiting-host-online"
    )
    assert observe_dual_batch(target, ledger, **observe_kwargs).status == (
        "awaiting-host-offline"
    )
    assert observe_dual_batch(target, ledger, **observe_kwargs).status == (
        "awaiting-host-online"
    )
    assert observe_dual_batch(target, ledger, **observe_kwargs).status == (
        "host-restarts-complete"
    )
    assert observe_dual_batch(target, ledger, **observe_kwargs).status == "complete"
    apply_dual_cleanup(
        target,
        ledger,
        prepare_cleanup=lambda _request: pytest.fail(
            "completed cleanup must reconcile without another preview"
        ),
        apply_authorized=True,
        cleanup_to_idle=lambda _request: pytest.fail(
            "completed cleanup must not apply a second stop"
        ),
        reconcile_cleanup=lambda request: _reconciled_cleanup(request, node_a, node_b),
        endpoint_exists=lambda _endpoint: False,
    )

    provenance = _provenance_for_nodes(host_row, _DUAL_NODES)
    for definition, reference, row, expected_mode in (
        (rank_definition, rank_reference, rank_row, "dual-rank-loss-recovery"),
        (host_definition, host_reference, host_row, "dual-host-restart"),
    ):
        envelope = build_recovery_coverage_receipt(
            coverage_definition=definition,
            coverage_reference=reference,
            authority_row=row,
            campaign_id=campaign_id,
            batch_id=batch_id,
            lane_id=lane_id,
            ledger_records=ledger.records,
            exact_preparation=prep,
            deployment_provenance_by_node=provenance,
            receipt_schema=recovery_coverage_receipt_json_schema(),
        )
        canonical = RecoveryCoverageReceiptEnvelope.model_validate(envelope)
        assert canonical.receipt.failure_mode == expected_mode
        assert recovery_receipt_sha256(canonical.receipt) == envelope["receipt_sha256"]


def _reconciled_cleanup(
    request: Mapping[str, object], node_a: str, node_b: str
) -> dict[str, object]:
    prior_receipt = request.get("prior_receipt")
    assert isinstance(prior_receipt, Mapping)
    return {
        **dict(prior_receipt),
        "fleet_snapshot": _dual_snapshot(
            12,
            state="idle",
            cursor=12,
            boots={node_a: "boot-a-2", node_b: "boot-b-2"},
        ),
    }
