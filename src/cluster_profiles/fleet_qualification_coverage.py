"""Create and validate canonical, recipe-specific recovery coverage receipts.

This module deliberately has no Pydantic dependency. The caller supplies the
published recipe-contract JSON Schema and already decoded Controller evidence;
this module binds that evidence to the canonical authority row and its exact
ledger records before it emits the content-addressed receipt envelope.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from jsonschema.validators import validator_for

from .fleet_qualification import QualificationError

_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_OCI_DIGEST = re.compile(r"sha256:([a-f0-9]{64})\Z")
_BUILD_DIGEST = re.compile(r"sha256:([a-f0-9]{64})\Z")
_NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
_RECOVERY_EVENTS = frozenset(
    {
        "canary.completed",
        "lane_recovery.intent",
        "lane_recovery.plan_reviewed",
        "lane_recovery.transitioned",
        "lane_recovery.baseline",
        "lane_recovery.offline_observed",
        "lane_recovery.boot_observed",
        "lane_recovery.recovered",
        "lane_recovery.smoke_completed",
        "lane_recovery.completed",
        "rank-loss.observed",
        "route-withdrawal.observed",
        "rank-recovery.smoke-node-completed",
        "rank-recovery.smoke-completed",
        "dual_recovery.cleanup.intent",
        "dual_recovery.cleanup.plan_reviewed",
        "dual_recovery.cleanup.apply-requested",
        "dual_recovery.cleanup.completed",
        "dual_recovery.cleanup.reconciled",
        "host-restart.baseline",
        "host-restart.offline",
        "host-restart.recovered",
        "dual_recovery.completed",
    }
)


def build_recovery_coverage_receipt(
    *,
    coverage_definition: Mapping[str, object],
    coverage_reference: Mapping[str, object],
    authority_row: Mapping[str, object],
    campaign_id: str,
    batch_id: str,
    lane_id: int,
    ledger_records: Sequence[Mapping[str, object]],
    exact_preparation: Mapping[str, object],
    deployment_provenance_by_node: Mapping[str, Mapping[str, object]],
    receipt_schema: Mapping[str, object],
) -> dict[str, object]:
    """Build one canonical receipt from exact authority, ledger and provenance.

    Only current singleton ``dedicated`` coverage definitions are accepted.
    ``ledger_records`` must come from ``EvidenceLedger`` after its full-chain
    verification; selected records are hash-checked again here. The passed
    deployment provenance is the full typed projection for each affected node.
    """

    scope = _validate_authority_scope(
        coverage_definition, coverage_reference, authority_row
    )
    if not isinstance(campaign_id, str) or not campaign_id:
        raise QualificationError("recovery campaign identity is absent")
    if not isinstance(batch_id, str) or not batch_id:
        raise QualificationError("recovery batch identity is absent")
    if type(lane_id) is not int or lane_id < 1:
        raise QualificationError("recovery lane identity is invalid")

    scoped_records = _scope_ledger_records(
        ledger_records,
        campaign_id=campaign_id,
        recipe_key=_required_string(scope.get("recipe_key"), "authority recipe key"),
        batch_id=batch_id,
        lane_id=lane_id,
        recipe_content_sha256=str(scope["recipe_content_sha256"]),
    )
    mode = scope["failure_mode"]
    if mode == "single-host-restart":
        proof = _single_host_proof(scoped_records, scope, exact_preparation)
        node_ids = [str(proof["node_id"])]
        nodes: list[dict[str, object]] = [
            {
                "node_id": node_ids[0],
                "agent_build_sha256": "",
                "baseline_boot_id": str(proof["baseline_boot_id"]),
                "recovered_boot_id": str(proof["recovered_boot_id"]),
                "baseline_event_id": str(proof["baseline_event_id"]),
                "offline_event_id": str(proof["offline_event_id"]),
                "recovered_event_id": str(proof["recovered_event_id"]),
            }
        ]
        smoke_receipt_sha256 = str(proof["smoke_receipt_sha256"])
        rank_recovery: dict[str, object] | None = None
    elif mode == "dual-host-restart":
        proof = _dual_host_proof(scoped_records, scope, exact_preparation)
        node_ids = _string_sequence(proof.get("node_ids"), "dual host Spark IDs")
        node_proofs = _mapping(proof["nodes"], "dual host proof nodes")
        nodes = [
            {
                "node_id": node_id,
                "agent_build_sha256": "",
                "baseline_boot_id": _required_string(
                    _mapping(node_proofs[node_id], "dual host node proof").get(
                        "baseline_boot_id"
                    ),
                    "baseline boot ID",
                ),
                "recovered_boot_id": _required_string(
                    _mapping(node_proofs[node_id], "dual host node proof").get(
                        "recovered_boot_id"
                    ),
                    "recovered boot ID",
                ),
                "baseline_event_id": _required_string(
                    proof.get("baseline_event_id"), "baseline event ID"
                ),
                "offline_event_id": _required_string(
                    _mapping(node_proofs[node_id], "dual host node proof").get(
                        "offline_event_id"
                    ),
                    "offline event ID",
                ),
                "recovered_event_id": _required_string(
                    _mapping(node_proofs[node_id], "dual host node proof").get(
                        "recovered_event_id"
                    ),
                    "recovered event ID",
                ),
            }
            for node_id in node_ids
        ]
        smoke_receipt_sha256 = str(proof["smoke_receipt_sha256"])
        rank_recovery = None
    else:
        proof = _dual_rank_proof(scoped_records, scope, exact_preparation)
        node_ids = _string_sequence(proof.get("node_ids"), "dual rank Spark IDs")
        rank_recovery = {
            "lost_node_id": proof["lost_node_id"],
            "recovered_node_id": proof["lost_node_id"],
            "survivor_node_id": proof["survivor_node_id"],
            "node_builds": [
                {"node_id": node_id, "agent_build_sha256": ""}
                for node_id in sorted(node_ids)
            ],
            "rank_loss_event_id": proof["rank_loss_event_id"],
            "route_withdrawal_event_id": proof["route_withdrawal_event_id"],
            "rank_recovery_event_id": proof["rank_recovery_event_id"],
            "recovered_smoke_receipt_sha256": proof["smoke_receipt_sha256"],
        }
        smoke_receipt_sha256 = str(proof["smoke_receipt_sha256"])
        nodes = []

    if len(set(node_ids)) != len(node_ids) or not node_ids:
        raise QualificationError(
            "recovery evidence has invalid selected Spark identities"
        )
    runtime_image_digest, architecture = _runtime_image_identity(
        exact_preparation, node_ids, scope
    )
    platform_build_sha256, agent_builds = _deployment_build_identities(
        deployment_provenance_by_node, node_ids, str(scope["package_sha256"])
    )
    if mode == "dual-rank-loss-recovery":
        if rank_recovery is None:
            raise QualificationError("dual rank-loss receipt omitted its typed proof")
        rank_recovery["node_builds"] = [
            {"node_id": node_id, "agent_build_sha256": agent_builds[node_id]}
            for node_id in sorted(node_ids)
        ]
    else:
        for node in nodes:
            node["agent_build_sha256"] = agent_builds[str(node["node_id"])]

    receipt: dict[str, object] = {
        "schema_version": 1,
        "coverage_id": scope["coverage_id"],
        "failure_mode": mode,
        "representative_recipe": scope["recipe_key"],
        "recipe_content_sha256": scope["recipe_content_sha256"],
        "package_sha256": scope["package_sha256"],
        "model_content_sha256s": scope["model_content_sha256s"],
        "runtime_stack_sha256": scope["runtime_stack_sha256"],
        "topology_sha256": scope["topology_sha256"],
        "runtime_image_digest": runtime_image_digest,
        "platform_build_sha256": platform_build_sha256,
        "architecture": architecture,
        "smoke_receipt_sha256": smoke_receipt_sha256,
        "checkpoint_event_ids": _string_sequence(
            proof.get("checkpoint_event_ids"), "recovery checkpoint event IDs"
        ),
        "nodes": nodes,
        "passed": True,
    }
    if rank_recovery is not None:
        receipt["rank_recovery"] = rank_recovery
    envelope = {
        "receipt": receipt,
        "receipt_sha256": _canonical_sha256(receipt, "recovery receipt"),
    }
    return validate_recovery_coverage_receipt(
        envelope,
        receipt_schema=receipt_schema,
        coverage_definition=coverage_definition,
        coverage_reference=coverage_reference,
        authority_row=authority_row,
    )


def validate_recovery_coverage_receipt(
    envelope: Mapping[str, object],
    *,
    receipt_schema: Mapping[str, object],
    coverage_definition: Mapping[str, object],
    coverage_reference: Mapping[str, object],
    authority_row: Mapping[str, object],
) -> dict[str, object]:
    """Validate canonical structure, digest normalization and authority scope."""

    _validate_json_schema(envelope, receipt_schema)
    receipt = _mapping(envelope.get("receipt"), "recovery receipt")
    expected_digest = _canonical_sha256(
        _normalize_receipt_for_hash(receipt), "recovery receipt"
    )
    supplied_digest = _required_sha(envelope.get("receipt_sha256"), "receipt digest")
    if supplied_digest != expected_digest:
        raise QualificationError(
            "recovery receipt digest does not match its normalized payload"
        )
    scope = _validate_authority_scope(
        coverage_definition, coverage_reference, authority_row
    )
    for key, expected in (
        ("coverage_id", scope["coverage_id"]),
        ("failure_mode", scope["failure_mode"]),
        ("representative_recipe", scope["recipe_key"]),
        ("recipe_content_sha256", scope["recipe_content_sha256"]),
        ("package_sha256", scope["package_sha256"]),
        ("model_content_sha256s", scope["model_content_sha256s"]),
        ("runtime_stack_sha256", scope["runtime_stack_sha256"]),
        ("topology_sha256", scope["topology_sha256"]),
    ):
        if receipt.get(key) != expected:
            raise QualificationError(
                f"recovery receipt {key} differs from its exact authority identity"
            )
    if receipt.get("passed") is not True:
        raise QualificationError("recovery receipt does not record a passed result")
    _validate_receipt_semantics(receipt, scope)
    return {"receipt": dict(receipt), "receipt_sha256": supplied_digest}


def _validate_receipt_semantics(
    receipt: Mapping[str, object], scope: Mapping[str, object]
) -> None:
    """Mirror canonical Pydantic after-validators JSON Schema cannot express."""

    mode = scope["failure_mode"]
    raw_nodes = receipt.get("nodes", [])
    if not isinstance(raw_nodes, list):
        raise QualificationError("recovery receipt node evidence is malformed")
    node_rows = [_mapping(item, "recovery node evidence") for item in raw_nodes]
    node_ids = [
        _required_node(node.get("node_id"), "recovery node identity")
        for node in node_rows
    ]
    if len(node_ids) != len(set(node_ids)):
        raise QualificationError("recovery receipt node identities are duplicated")
    rank_value = receipt.get("rank_recovery")
    checkpoint_values = receipt.get("checkpoint_event_ids")
    if not isinstance(checkpoint_values, list):
        raise QualificationError("recovery checkpoint identities are malformed")
    checkpoint_ids = [
        _required_sha(value, "recovery checkpoint event identity")
        for value in checkpoint_values
    ]
    if len(checkpoint_ids) != len(set(checkpoint_ids)):
        raise QualificationError("recovery checkpoint event identities are duplicated")

    expected_ids: set[str]
    if mode in {"single-host-restart", "dual-host-restart"}:
        expected_nodes = 1 if mode == "single-host-restart" else 2
        if len(node_rows) != expected_nodes:
            raise QualificationError(
                f"{mode} requires exactly {expected_nodes} node proofs"
            )
        if rank_value is not None:
            raise QualificationError(
                "host-restart receipt cannot contain rank-loss evidence"
            )
        expected_ids = set()
        for node in node_rows:
            baseline = _required_string(
                node.get("baseline_boot_id"), "baseline boot ID"
            )
            recovered = _required_string(
                node.get("recovered_boot_id"), "recovered boot ID"
            )
            if baseline == recovered:
                raise QualificationError(
                    "host recovery receipt must prove a changed boot ID"
                )
            event_ids = [
                _required_sha(node.get("baseline_event_id"), "baseline event identity"),
                _required_sha(node.get("offline_event_id"), "offline event identity"),
                _required_sha(
                    node.get("recovered_event_id"), "recovered event identity"
                ),
            ]
            if len(event_ids) != len(set(event_ids)):
                raise QualificationError("host restart event identities are duplicated")
            expected_ids.update(event_ids)
    elif mode == "dual-rank-loss-recovery":
        if node_rows:
            raise QualificationError(
                "dual rank receipt cannot contain host reboot proofs"
            )
        if not isinstance(rank_value, Mapping):
            raise QualificationError("dual rank receipt omits its typed rank proof")
        rank = _mapping(rank_value, "rank recovery evidence")
        lost = _required_node(rank.get("lost_node_id"), "lost rank Spark")
        recovered = _required_node(
            rank.get("recovered_node_id"), "recovered rank Spark"
        )
        survivor = _required_node(rank.get("survivor_node_id"), "survivor Spark")
        if lost != recovered or lost == survivor:
            raise QualificationError(
                "rank proof must restore the lost Spark and name a distinct survivor"
            )
        raw_builds = rank.get("node_builds")
        if not isinstance(raw_builds, list) or len(raw_builds) != 2:
            raise QualificationError("rank proof must bind both Spark agent builds")
        build_rows = [_mapping(item, "rank node build identity") for item in raw_builds]
        build_nodes = [
            _required_node(item.get("node_id"), "rank node build Spark")
            for item in build_rows
        ]
        if len(build_nodes) != len(set(build_nodes)) or set(build_nodes) != {
            lost,
            survivor,
        }:
            raise QualificationError(
                "rank proof node builds differ from the exact lost/survivor pair"
            )
        expected_ids = {
            _required_sha(rank.get("rank_loss_event_id"), "rank loss event identity"),
            _required_sha(
                rank.get("route_withdrawal_event_id"), "route withdrawal event identity"
            ),
            _required_sha(
                rank.get("rank_recovery_event_id"), "rank recovery event identity"
            ),
        }
        if len(expected_ids) != 3:
            raise QualificationError("rank recovery event identities are duplicated")
        if rank.get("recovered_smoke_receipt_sha256") != receipt.get(
            "smoke_receipt_sha256"
        ):
            raise QualificationError(
                "rank recovery smoke identity differs from the receipt"
            )
    else:
        raise QualificationError("recovery receipt has an unsupported failure mode")
    if set(checkpoint_ids) != expected_ids:
        raise QualificationError(
            "checkpoint events must exactly bind typed recovery events"
        )
    model_hashes = receipt.get("model_content_sha256s")
    if not isinstance(model_hashes, list) or model_hashes != sorted(set(model_hashes)):
        raise QualificationError(
            "recovery receipt Model identities must be unique and sorted"
        )


def _validate_authority_scope(
    definition: Mapping[str, object],
    reference: Mapping[str, object],
    row: Mapping[str, object],
) -> dict[str, object]:
    recipe_key = _required_string(row.get("key"), "authority recipe key")
    recipe_sha = _required_sha(row.get("content_sha256"), "authority recipe digest")
    package = _mapping(row.get("package"), "authority recipe package")
    package_sha = _required_sha(package.get("sha256"), "authority package digest")
    runtime_stack_sha = _required_sha(
        row.get("runtime_stack_sha256"), "authority runtime stack digest"
    )
    topology_sha = _required_sha(
        row.get("topology_sha256"), "authority topology digest"
    )
    model_refs = row.get("model_license_refs")
    if not isinstance(model_refs, list):
        raise QualificationError("authority row omits its exact Model references")
    model_hashes: list[str] = []
    for raw_ref in model_refs:
        model_ref = _mapping(raw_ref, "authority Model reference")
        model_hashes.append(
            _required_sha(model_ref.get("content_sha256"), "Model content digest")
        )
    if not model_hashes:
        raise QualificationError("recovery coverage requires at least one exact Model")
    model_hashes = sorted(set(model_hashes))

    coverage_id = _required_sha(definition.get("coverage_id"), "coverage identity")
    definition_body = dict(definition)
    definition_body.pop("coverage_id", None)
    if (
        _canonical_sha256(definition_body, "recovery coverage definition")
        != coverage_id
    ):
        raise QualificationError("recovery coverage ID does not bind its definition")
    failure_mode = _required_string(
        definition.get("failure_mode"), "recovery failure mode"
    )
    if failure_mode not in {
        "single-host-restart",
        "dual-rank-loss-recovery",
        "dual-host-restart",
    }:
        raise QualificationError("recovery coverage has an unknown failure mode")
    if definition.get("shared") is not False:
        raise QualificationError("only dedicated recovery coverage is supported")
    if definition.get("representative_recipe") != recipe_key:
        raise QualificationError(
            "recovery representative differs from its authority row"
        )
    members = definition.get("members")
    if not isinstance(members, list) or len(members) != 1:
        raise QualificationError(
            "dedicated recovery coverage must have one exact member"
        )
    member = _mapping(members[0], "recovery coverage member")
    expected_member = {
        "recipe": recipe_key,
        "recipe_content_sha256": recipe_sha,
        "package_sha256": package_sha,
        "model_content_sha256s": model_hashes,
        "runtime_stack_sha256": runtime_stack_sha,
        "topology_sha256": topology_sha,
    }
    if any(member.get(key) != value for key, value in expected_member.items()):
        raise QualificationError("coverage member differs from its exact authority row")
    row_references = row.get("recovery_coverage_refs")
    if (
        not isinstance(row_references, list)
        or sum(item == dict(reference) for item in row_references) != 1
    ):
        raise QualificationError(
            "recovery reference is absent or duplicated in its authority row"
        )
    if (
        reference.get("coverage_id") != coverage_id
        or reference.get("failure_mode") != failure_mode
        or reference.get("representative_recipe") != recipe_key
        or reference.get("role") != "dedicated"
    ):
        raise QualificationError(
            "recipe recovery reference differs from its definition"
        )
    smoke_cases = row.get("smoke_cases")
    if not isinstance(smoke_cases, list) or any(
        not isinstance(item, str) for item in smoke_cases
    ):
        raise QualificationError("authority row omits its exact smoke case set")
    return {
        "coverage_id": coverage_id,
        "failure_mode": failure_mode,
        "recipe_key": recipe_key,
        "recipe_content_sha256": recipe_sha,
        "package_sha256": package_sha,
        "model_content_sha256s": model_hashes,
        "runtime_stack_sha256": runtime_stack_sha,
        "topology_sha256": topology_sha,
        "node_count": _positive_int(row.get("node_count"), "authority node count"),
        "smoke_cases": list(smoke_cases),
    }


def _scope_ledger_records(
    records: Sequence[Mapping[str, object]],
    *,
    campaign_id: str,
    recipe_key: str,
    batch_id: str,
    lane_id: int,
    recipe_content_sha256: str,
) -> list[Mapping[str, object]]:
    _validate_ledger_chain(records)
    result: list[Mapping[str, object]] = []
    seen_sequences: set[int] = set()
    for record in records:
        event = record.get("event")
        if not isinstance(event, str) or event not in _RECOVERY_EVENTS:
            continue
        if (
            record.get("plan_digest") != campaign_id
            or record.get("recipe") != recipe_key
        ):
            continue
        payload = _mapping(record.get("payload"), "recovery ledger payload")
        if event == "canary.completed":
            if payload.get("batch_id") not in (None, batch_id) or payload.get(
                "lane_id"
            ) not in (None, lane_id):
                continue
        elif payload.get("batch_id") != batch_id or payload.get("lane_id") != lane_id:
            continue
        _validate_ledger_record(record)
        sequence = _positive_int(record.get("sequence"), "recovery event sequence")
        if sequence in seen_sequences:
            raise QualificationError("recovery ledger event sequence is duplicated")
        seen_sequences.add(sequence)
        result.append(record)
    if not result:
        raise QualificationError("exact campaign/batch/lane recovery events are absent")
    result.sort(key=lambda item: _positive_int(item.get("sequence"), "event sequence"))
    target_digests = {
        _required_sha(_payload(item).get("target_digest"), "recovery target digest")
        for item in result
        if item.get("event") != "canary.completed"
    }
    if len(target_digests) != 1:
        raise QualificationError("recovery events do not bind one exact target")
    recipe_digests = {
        _required_sha(
            _payload(item).get("recipe_content_sha256"), "recipe content digest"
        )
        for item in result
        if item.get("event") != "canary.completed"
    }
    if recipe_digests != {recipe_content_sha256}:
        raise QualificationError(
            "recovery events differ from the exact authority recipe content"
        )
    return result


def _single_host_proof(
    records: Sequence[Mapping[str, object]],
    scope: Mapping[str, object],
    exact_preparation: Mapping[str, object],
) -> dict[str, object]:
    if scope["node_count"] != 1:
        raise QualificationError("single-host recovery requires a single-node recipe")
    intent = _unique_event(records, "lane_recovery.intent")
    reviewed = _unique_event(records, "lane_recovery.plan_reviewed")
    transitioned = _unique_event(records, "lane_recovery.transitioned")
    baseline = _unique_event(records, "lane_recovery.baseline")
    offline = _unique_event(records, "lane_recovery.offline_observed")
    boot = _unique_event(records, "lane_recovery.boot_observed")
    recovered = _unique_event(records, "lane_recovery.recovered")
    smoke = _unique_event(records, "lane_recovery.smoke_completed")
    completed = _unique_event(records, "lane_recovery.completed")
    ordered = [
        intent,
        reviewed,
        transitioned,
        baseline,
        offline,
        boot,
        recovered,
        smoke,
        completed,
    ]
    if not _strictly_ordered(ordered):
        raise QualificationError(
            "single-host recovery events are incomplete or out of order"
        )
    _validate_canary_reference(records, intent, scope, exact_preparation)

    baseline_payload = _payload(baseline)
    offline_payload = _payload(offline)
    boot_payload = _payload(boot)
    recovered_payload = _payload(recovered)
    smoke_payload = _payload(smoke)
    completion_payload = _payload(completed)
    node_id = _required_node(offline_payload.get("node_id"), "offline Spark")
    baseline_boot_id = _required_string(
        baseline_payload.get("boot_id"), "baseline boot ID"
    )
    recovered_boot_id = _required_string(
        recovered_payload.get("observed_boot_id"), "recovered boot ID"
    )
    run_id = _required_string(
        recovered_payload.get("active_run_id"), "recovered run ID"
    )
    if (
        offline_payload.get("online_state") != "offline"
        or offline_payload.get("baseline_boot_id") != baseline_boot_id
        or offline_payload.get("active_run_id") != run_id
        or recovered_payload.get("node_id") != node_id
        or recovered_payload.get("baseline_boot_id") != baseline_boot_id
        or recovered_boot_id == baseline_boot_id
        or boot_payload.get("node_id") != node_id
        or boot_payload.get("baseline_boot_id") != baseline_boot_id
        or boot_payload.get("observed_boot_id") != recovered_boot_id
        or _payload(smoke).get("active_run_id") != run_id
        or _payload(smoke).get("recovered_boot_id") != recovered_boot_id
    ):
        raise QualificationError(
            "single-host restart boot or run identities do not reconcile"
        )
    serving = _mapping(
        recovered_payload.get("serving_receipt"), "recovered serving receipt"
    )
    smoke_receipt = _mapping(
        smoke_payload.get("smoke_receipt"), "recovered fixture receipt"
    )
    _validate_smoke_identity(smoke_receipt, serving, scope, run_id, recovered_boot_id)
    _validate_completion_refs(
        completion_payload,
        {
            "intent": intent,
            "transitioned": transitioned,
            "baseline": baseline,
            "offline": offline,
            "boot_observed": boot,
            "recovered": recovered,
            "smoke": smoke,
        },
        baseline_boot_id,
        recovered_boot_id,
    )
    return {
        "node_id": node_id,
        "baseline_boot_id": baseline_boot_id,
        "recovered_boot_id": recovered_boot_id,
        "baseline_event_id": _record_id(baseline),
        "offline_event_id": _record_id(offline),
        "recovered_event_id": _record_id(recovered),
        "checkpoint_event_ids": [
            _record_id(baseline),
            _record_id(offline),
            _record_id(recovered),
        ],
        "smoke_receipt_sha256": _canonical_sha256(
            smoke_receipt, "recovered fixture smoke receipt"
        ),
    }


def _dual_rank_source(
    records: Sequence[Mapping[str, object]],
    scope: Mapping[str, object],
    exact_preparation: Mapping[str, object],
) -> dict[str, object]:
    if scope["node_count"] != 2:
        raise QualificationError("dual recovery requires a two-node recipe")
    rank_loss = _unique_event(records, "rank-loss.observed")
    route_withdrawal = _unique_event(records, "route-withdrawal.observed")
    rank_recovery = _unique_event(records, "rank-recovery.smoke-completed")
    rank_loss_payload = _payload(rank_loss)
    route_payload = _payload(route_withdrawal)
    rank_payload = _payload(rank_recovery)
    if not _strictly_ordered([rank_loss, route_withdrawal, rank_recovery]):
        raise QualificationError(
            "dual rank recovery events are incomplete or out of order"
        )
    _validate_canary_reference(records, rank_loss, scope, exact_preparation)
    nodes = _rank_nodes(rank_loss_payload)
    node_to_rank = _rank_map(rank_loss_payload.get("node_to_rank"), nodes)
    lost_node_id = _required_node(
        rank_loss_payload.get("failure_node_id"), "failed rank Spark"
    )
    if lost_node_id not in node_to_rank:
        raise QualificationError("rank-loss event names an unassigned Spark")
    _validate_route_withdrawal_observation(
        route_payload,
        rank_loss_payload,
        nodes,
        node_to_rank,
        lost_node_id,
    )
    if (
        route_payload.get("rank_loss_record_sha256") != _record_id(rank_loss)
        or route_payload.get("endpoint_not_found") is not True
        or rank_loss_payload.get("run_id") != rank_payload.get("run_id")
        or rank_loss_payload.get("recipe_revision_id")
        != rank_payload.get("recipe_revision_id")
        or rank_loss_payload.get("alias") != rank_payload.get("alias")
        or rank_loss_payload.get("node_ids") != rank_payload.get("node_ids")
        or rank_loss_payload.get("node_to_rank") != rank_payload.get("node_to_rank")
        or rank_payload.get("recipe_content_sha256") != scope["recipe_content_sha256"]
        or rank_payload.get("package_sha256") != scope["package_sha256"]
    ):
        raise QualificationError(
            "dual rank recovery changed its rank-loss or route scope"
        )
    proof = _mapping(rank_loss_payload.get("proof"), "rank-loss proof")
    if proof.get("failure_node_id") != lost_node_id:
        raise QualificationError("rank-loss proof changed the exact failed Spark")
    survivor_node_id = next(node for node in nodes if node != lost_node_id)
    _validate_rank_smoke_events(records, rank_payload, node_to_rank, scope)
    smoke_sha = _canonical_sha256(rank_payload, "aggregate rank recovery proof")
    return {
        "rank_loss_event_id": _record_id(rank_loss),
        "route_withdrawal_event_id": _record_id(route_withdrawal),
        "rank_recovery_event_id": _record_id(rank_recovery),
        "lost_node_id": lost_node_id,
        "survivor_node_id": survivor_node_id,
        "node_ids": nodes,
        "smoke_receipt_sha256": smoke_sha,
        "checkpoint_event_ids": [
            _record_id(rank_loss),
            _record_id(route_withdrawal),
            _record_id(rank_recovery),
        ],
    }


def _validate_route_withdrawal_observation(
    route_payload: Mapping[str, object],
    rank_loss_payload: Mapping[str, object],
    node_ids: Sequence[str],
    node_to_rank: Mapping[str, int],
    lost_node_id: str,
) -> None:
    """Check the route record against its captured fresh FleetSnapshot."""

    snapshot = _mapping(
        route_payload.get("fleet_snapshot"), "route-withdrawal FleetSnapshot"
    )
    snapshot_identity = _fleet_snapshot_identity(snapshot, "route-withdrawal")
    recorded_identity = _fleet_snapshot_identity(
        route_payload, "route-withdrawal event"
    )
    if snapshot_identity != recorded_identity:
        raise QualificationError(
            "route-withdrawal event identity differs from its FleetSnapshot"
        )
    prior_identity = _fleet_snapshot_identity(rank_loss_payload, "rank-loss event")
    route_time, route_cursor, route_authority = snapshot_identity
    prior_time, prior_cursor, prior_authority = prior_identity
    if (
        route_time <= prior_time
        or route_cursor < prior_cursor
        or route_authority != prior_authority
    ):
        raise QualificationError("route-withdrawal FleetSnapshot is stale")

    fleet_node_ids = _string_sequence(
        rank_loss_payload.get("fleet_node_ids"), "rank-loss Fleet node roster"
    )
    if not set(node_ids).issubset(fleet_node_ids):
        raise QualificationError("rank-loss Spark pair is outside its Fleet roster")
    raw_nodes = snapshot.get("nodes")
    if not isinstance(raw_nodes, list):
        raise QualificationError("route-withdrawal Fleet nodes are malformed")
    fleet_nodes: dict[str, Mapping[str, object]] = {}
    for raw_node in raw_nodes:
        node = _mapping(raw_node, "route-withdrawal Fleet node")
        node_id = _required_node(node.get("id"), "route-withdrawal Fleet Spark")
        if node_id in fleet_nodes:
            raise QualificationError("route-withdrawal Fleet repeats a Spark")
        fleet_nodes[node_id] = node
    if set(fleet_nodes) != set(fleet_node_ids):
        raise QualificationError("route-withdrawal Fleet roster changed")

    run_id = _required_string(rank_loss_payload.get("run_id"), "dual run ID")
    revision_id = _required_string(
        rank_loss_payload.get("recipe_revision_id"), "dual recipe revision"
    )
    alias = _required_string(rank_loss_payload.get("alias"), "dual route alias")
    run_presences: list[tuple[str, Mapping[str, object]]] = []
    online_states: dict[str, str] = {}
    for node_id, node in fleet_nodes.items():
        connection = _mapping(
            node.get("connection"), "route-withdrawal Fleet connection"
        )
        online_state = _required_string(
            connection.get("online_state"), "Fleet online state"
        )
        if online_state not in {"online", "offline", "unregistered"}:
            raise QualificationError("route-withdrawal Fleet state is invalid")
        online_states[node_id] = online_state
        loaded = node.get("loaded")
        if not isinstance(loaded, list):
            raise QualificationError("route-withdrawal loaded runs are malformed")
        for raw_presence in loaded:
            presence = _mapping(raw_presence, "route-withdrawal run presence")
            if presence.get("run_id") != run_id:
                continue
            if node_id not in node_to_rank:
                raise QualificationError(
                    "route-withdrawal run presence escaped its assigned Spark pair"
                )
            if any(existing_node == node_id for existing_node, _ in run_presences):
                raise QualificationError(
                    "route-withdrawal Fleet repeats the exact run on one Spark"
                )
            if (
                presence.get("recipe_revision_id") != revision_id
                or presence.get("alias") != alias
                or type(presence.get("rank")) is not int
                or presence.get("rank") != node_to_rank[node_id]
                or presence.get("expected_rank_count") != 2
                or presence.get("group_state") != "degraded"
                or presence.get("healthy") is not False
                or presence.get("route_state") not in {"published", "withdrawn"}
            ):
                raise QualificationError(
                    "route-withdrawal Fleet presence changed the exact failed run"
                )
            run_presences.append((node_id, presence))

    lost_rank = node_to_rank[lost_node_id]
    survivor_node_id = next(node_id for node_id in node_ids if node_id != lost_node_id)
    survivor_rank = node_to_rank[survivor_node_id]
    survivors = [
        (node_id, presence)
        for node_id, presence in run_presences
        if node_id != lost_node_id
    ]
    failures = [
        presence for node_id, presence in run_presences if node_id == lost_node_id
    ]
    if len(survivors) != 1 or len(failures) > 1:
        raise QualificationError(
            "route-withdrawal snapshot lacks exact rank-loss evidence"
        )
    survivor = survivors[0][1]
    if (
        online_states[survivor_node_id] != "online"
        or not _fleet_node_is_live(fleet_nodes[survivor_node_id])
        or survivor.get("rank") != survivor_rank
        or not _has_expected_present_ranks(
            survivor.get("present_ranks"), survivor_rank, lost_rank
        )
    ):
        raise QualificationError("route-withdrawal survivor is not freshly observed")
    failed_online_state = online_states[lost_node_id]
    if failed_online_state not in {"online", "offline"}:
        raise QualificationError("route-withdrawal failed Spark state is invalid")
    if failures:
        failed = failures[0]
        if failed.get("rank_state") not in {"lost", "failed", "stopped"} or not (
            _has_expected_present_ranks(
                failed.get("present_ranks"), survivor_rank, lost_rank
            )
        ):
            raise QualificationError("route-withdrawal failed rank is misattributed")
    elif failed_online_state != "offline":
        raise QualificationError("absent failed rank lacks offline Fleet evidence")
    if any(presence.get("route_state") != "withdrawn" for _, presence in run_presences):
        raise QualificationError("route-withdrawal Fleet still publishes the exact run")

    proof = {
        "failure_node_id": lost_node_id,
        "failure_rank": lost_rank,
        "expected_present_ranks": [survivor_rank],
        "survivors": [{"node_id": survivor_node_id, **dict(survivor)}],
        "failed_rank_presence": [dict(item) for item in failures],
        "failed_node_online_state": failed_online_state,
    }
    observed = [
        {"node_id": node_id, **dict(presence)} for node_id, presence in run_presences
    ]
    if (
        route_payload.get("proof") != proof
        or route_payload.get("fleet_rank_presence") != observed
    ):
        raise QualificationError(
            "route-withdrawal event does not preserve its exact Fleet evidence"
        )


def _fleet_snapshot_identity(
    value: Mapping[str, object], label: str
) -> tuple[datetime, int, str]:
    generated_at = _timestamp(value.get("generated_at"), f"{label} generated_at")
    cursor = value.get("event_cursor")
    if type(cursor) is not int or cursor < 0:
        raise QualificationError(f"{label} event cursor is invalid")
    authority = _required_string(value.get("authority_revision"), f"{label} authority")
    return generated_at, cursor, authority


def _fleet_node_is_live(node: Mapping[str, object]) -> bool:
    telemetry = node.get("telemetry")
    if not isinstance(telemetry, Mapping) or telemetry.get("freshness") != "live":
        return False
    sample = telemetry.get("sample")
    if not isinstance(sample, Mapping):
        return False
    boot_id = sample.get("boot_id")
    if not isinstance(boot_id, str) or not boot_id:
        return False
    try:
        _timestamp(sample.get("observed_at"), "route-withdrawal telemetry time")
    except QualificationError:
        return False
    return True


def _has_expected_present_ranks(value: object, survivor: int, lost: int) -> bool:
    if (
        not isinstance(value, list)
        or any(type(rank) is not int for rank in value)
        or len(value) != len(set(value))
    ):
        return False
    observed = set(value)
    return observed in ({survivor}, {survivor, lost})


def _dual_rank_proof(
    records: Sequence[Mapping[str, object]],
    scope: Mapping[str, object],
    exact_preparation: Mapping[str, object],
) -> dict[str, object]:
    return _dual_rank_source(records, scope, exact_preparation)


def _dual_host_proof(
    records: Sequence[Mapping[str, object]],
    scope: Mapping[str, object],
    exact_preparation: Mapping[str, object],
) -> dict[str, object]:
    if scope["node_count"] != 2:
        raise QualificationError("dual host recovery requires a two-node recipe")
    rank = _dual_rank_source(records, scope, exact_preparation)
    rank_loss = _unique_event(records, "rank-loss.observed")
    route = _unique_event(records, "route-withdrawal.observed")
    rank_recovery = _unique_event(records, "rank-recovery.smoke-completed")
    cleanup_intent = _unique_event(records, "dual_recovery.cleanup.intent")
    cleanup_review = _unique_event(records, "dual_recovery.cleanup.plan_reviewed")
    cleanup_apply = _unique_event(records, "dual_recovery.cleanup.apply-requested")
    cleanup = _unique_event(records, "dual_recovery.cleanup.completed")
    reconciliation = _unique_event(records, "dual_recovery.cleanup.reconciled")
    baseline = _unique_event(records, "host-restart.baseline")
    completion = _unique_event(records, "dual_recovery.completed")
    baseline_payload = _payload(baseline)
    rank_loss_payload = _payload(rank_loss)
    cleanup_intent_payload = _payload(cleanup_intent)
    cleanup_review_payload = _payload(cleanup_review)
    cleanup_apply_payload = _payload(cleanup_apply)
    cleanup_payload = _payload(cleanup)
    reconciled_payload = _payload(reconciliation)
    completion_payload = _payload(completion)
    nodes = _string_sequence(rank.get("node_ids"), "dual host Spark IDs")
    node_to_rank = _rank_map(_payload(rank_loss).get("node_to_rank"), nodes)
    ordered: list[Mapping[str, object]] = [
        rank_loss,
        route,
        rank_recovery,
        cleanup_intent,
        cleanup_review,
        cleanup_apply,
        cleanup,
        baseline,
    ]
    if not _strictly_ordered(ordered):
        raise QualificationError(
            "dual host recovery setup events are incomplete or out of order"
        )
    cleanup_receipt = _mapping(cleanup_payload.get("receipt"), "dual cleanup receipt")
    if (
        cleanup_intent_payload.get("terminal_record_sha256")
        != _record_id(rank_recovery)
        or _mapping(cleanup_review_payload.get("review"), "cleanup review").get(
            "terminal_record_sha256"
        )
        != _record_id(rank_recovery)
        or cleanup_payload.get("terminal_record_sha256") != _record_id(rank_recovery)
        or cleanup_payload.get("review_digest")
        != cleanup_review_payload.get("review_digest")
        or cleanup_payload.get("request_key")
        != cleanup_review_payload.get("request_key")
        or cleanup_apply_payload.get("review_digest")
        != cleanup_review_payload.get("review_digest")
        or cleanup_apply_payload.get("request_key")
        != cleanup_review_payload.get("request_key")
        or cleanup_receipt.get("application_state") != "succeeded"
        or cleanup_receipt.get("request_key")
        != cleanup_review_payload.get("request_key")
        or cleanup_receipt.get("review_digest")
        != cleanup_review_payload.get("review_digest")
        or cleanup_receipt.get("profile_digest")
        != _mapping(cleanup_review_payload.get("review"), "cleanup review").get(
            "profile_digest"
        )
        or cleanup_receipt.get("plan_digest")
        != _mapping(cleanup_review_payload.get("review"), "cleanup review").get(
            "plan_digest"
        )
        or _mapping(cleanup_review_payload.get("review"), "cleanup review").get(
            "active_run_id"
        )
        != rank_loss_payload.get("run_id")
        or not isinstance(cleanup_receipt.get("application_id"), str)
        or not cleanup_receipt.get("application_id")
        or not _dual_stop_receipt_valid(
            cleanup_receipt.get("stop_receipts"), node_to_rank, rank_loss_payload
        )
        or baseline_payload.get("cleanup_record_sha256") != _record_id(cleanup)
        or baseline_payload.get("rank_recovery_record_sha256")
        != _record_id(rank_recovery)
    ):
        raise QualificationError(
            "dual host baseline is not bound to completed cleanup and rank smoke"
        )
    if (
        reconciled_payload.get("request_key")
        != cleanup_review_payload.get("request_key")
        or reconciled_payload.get("review_digest")
        != cleanup_review_payload.get("review_digest")
        or reconciled_payload.get("application_id")
        != cleanup_receipt.get("application_id")
        or reconciled_payload.get("prior_cleanup_record_sha256") != _record_id(cleanup)
        or not _reconciled_fleet_is_idle(
            reconciled_payload.get("reconciled_fleet_snapshot"),
            _string_sequence(
                rank_loss_payload.get("fleet_node_ids"), "whole-Fleet node IDs"
            ),
            _mapping(baseline_payload.get("nodes"), "dual host baseline nodes"),
            cleanup_receipt.get("application_updated_at"),
        )
    ):
        raise QualificationError(
            "dual cleanup reconciliation changed the reviewed application"
        )

    baseline_nodes = _mapping(baseline_payload.get("nodes"), "dual host baseline nodes")
    if set(baseline_nodes) != set(nodes):
        raise QualificationError(
            "dual host baseline does not cover the exact Spark pair"
        )
    smoke_refs = _mapping(
        baseline_payload.get("rank_smoke_refs"), "rank smoke references"
    )
    if set(smoke_refs) != set(nodes):
        raise QualificationError(
            "dual host baseline omits per-node rank smoke references"
        )
    host_proofs: dict[str, dict[str, object]] = {}
    for node_id in nodes:
        offline = _unique_node_event(records, "host-restart.offline", node_id)
        recovered = _unique_node_event(records, "host-restart.recovered", node_id)
        baseline_node = _mapping(baseline_nodes[node_id], "dual host baseline node")
        offline_payload = _payload(offline)
        recovered_payload = _payload(recovered)
        baseline_boot = _required_string(
            baseline_node.get("boot_id"), "baseline boot ID"
        )
        recovered_boot = _required_string(
            recovered_payload.get("observed_boot_id"), "recovered boot ID"
        )
        if (
            recovered_boot == baseline_boot
            or offline_payload.get("baseline_boot_id") != baseline_boot
            or recovered_payload.get("baseline_boot_id") != baseline_boot
            or offline_payload.get("online_state") != "offline"
            or recovered_payload.get("online_state") != "online"
            or recovered_payload.get("telemetry_freshness") != "live"
            or offline_payload.get("rank") != node_to_rank[node_id]
            or recovered_payload.get("rank") != node_to_rank[node_id]
        ):
            raise QualificationError(
                f"dual host restart boot proof changed for {node_id}"
            )
        expected_ref = _node_rank_smoke_reference(
            records, node_id, node_to_rank[node_id]
        )
        baseline_ref = _mapping(smoke_refs[node_id], "baseline rank smoke reference")
        if any(baseline_ref.get(key) != expected_ref.get(key) for key in expected_ref):
            raise QualificationError(
                f"dual host baseline is not bound to {node_id}'s rank smoke"
            )
        for payload in (offline_payload, recovered_payload):
            if (
                payload.get("rank_recovery_record_sha256") != _record_id(rank_recovery)
                or payload.get("rank_smoke_rank") != node_to_rank[node_id]
                or payload.get("rank_smoke_event_sha256")
                != expected_ref["rank_smoke_event_sha256"]
                or payload.get("smoke_record_sha256")
                != expected_ref["smoke_record_sha256"]
            ):
                raise QualificationError(
                    f"dual host event lost {node_id}'s recovered smoke binding"
                )
        host_proofs[node_id] = {
            "baseline_boot_id": baseline_boot,
            "recovered_boot_id": recovered_boot,
            "offline_event_id": _record_id(offline),
            "recovered_event_id": _record_id(recovered),
        }
    rank_order = sorted(nodes, key=lambda item: node_to_rank[item])
    host_sequence: list[Mapping[str, object]] = [baseline]
    for node_id in rank_order:
        host_sequence.extend(
            [
                _unique_node_event(records, "host-restart.offline", node_id),
                _unique_node_event(records, "host-restart.recovered", node_id),
            ]
        )
    # Cleanup reconciliation is replayed after the full rank and idle-host
    # ladder is terminal; it validates the original stop application without
    # changing the checkpoint ordering.
    host_sequence.extend([completion, reconciliation])
    if not _strictly_ordered(host_sequence):
        raise QualificationError(
            "dual host restart events are not sequential or terminal"
        )
    if completion_payload.get("cleanup_record_sha256") != _record_id(
        cleanup
    ) or _mapping(
        completion_payload.get("event_refs"), "dual completion event refs"
    ).get("host-restart.baseline") != _record_id(baseline):
        raise QualificationError(
            "dual recovery completion omits its exact cleanup or baseline"
        )
    checkpoint_ids = [_record_id(baseline)]
    for node_id in rank_order:
        checkpoint_ids.extend(
            [
                str(host_proofs[node_id]["offline_event_id"]),
                str(host_proofs[node_id]["recovered_event_id"]),
            ]
        )
    return {
        "node_ids": rank_order,
        "nodes": host_proofs,
        "baseline_event_id": _record_id(baseline),
        "checkpoint_event_ids": checkpoint_ids,
        "smoke_receipt_sha256": rank["smoke_receipt_sha256"],
    }


def _dual_stop_receipt_valid(
    raw_receipts: object,
    node_to_rank: Mapping[str, int],
    rank_loss_payload: Mapping[str, object],
) -> bool:
    if not isinstance(raw_receipts, list) or len(raw_receipts) != 1:
        return False
    receipt = _mapping(raw_receipts[0], "dual run stop receipt")
    observation = receipt.get("final_observation")
    if not isinstance(observation, Mapping):
        return False
    run_id = rank_loss_payload.get("run_id")
    raw_ranks = observation.get("ranks")
    if not isinstance(raw_ranks, list) or len(raw_ranks) != len(node_to_rank):
        return False
    observed: dict[str, int] = {}
    ranks_seen: set[int] = set()
    for raw_rank in raw_ranks:
        rank_record = _mapping(raw_rank, "dual cleanup stopped rank receipt")
        node_id = _required_node(rank_record.get("node_id"), "stopped rank Spark")
        rank = rank_record.get("rank")
        _required_string(rank_record.get("role"), "stopped rank role")
        if (
            "fresh" in rank_record
            and rank_record.get("fresh") is not None
            and type(rank_record.get("fresh")) is not bool
        ):
            return False
        if (
            node_id not in node_to_rank
            or type(rank) is not int
            or node_to_rank[node_id] != rank
            or node_id in observed
            or rank in ranks_seen
            or rank_record.get("state") != "stopped"
        ):
            return False
        observed[node_id] = rank
        ranks_seen.add(rank)
    return (
        receipt.get("run_id") == run_id
        and receipt.get("operation_state") == "succeeded"
        and observation.get("phase") == "final_verify"
        and observation.get("final_verified") is True
        and observation.get("run_id") == run_id
        and observation.get("state") == "stopped"
        and observation.get("route_state") == "withdrawn"
        and observed == dict(node_to_rank)
    )


def _node_rank_smoke_reference(
    records: Sequence[Mapping[str, object]], node_id: str, rank: int
) -> dict[str, object]:
    event = _unique_node_event(records, "rank-recovery.smoke-node-completed", node_id)
    payload = _payload(event)
    if payload.get("rank") != rank:
        raise QualificationError("rank smoke event changed its exact node rank")
    smoke_record_sha = _required_sha(
        payload.get("record_sha256"), "rank smoke receipt digest"
    )
    return {
        "rank_smoke_rank": rank,
        "rank_smoke_event_sha256": _record_id(event),
        "smoke_record_sha256": smoke_record_sha,
    }


def _validate_rank_smoke_events(
    records: Sequence[Mapping[str, object]],
    aggregate: Mapping[str, object],
    node_to_rank: Mapping[str, int],
    scope: Mapping[str, object],
) -> None:
    references = _mapping(
        aggregate.get("node_smoke_record_sha256s"), "aggregate rank smoke references"
    )
    if set(references) != set(node_to_rank):
        raise QualificationError("aggregate rank smoke omits a selected Spark")
    smoke = _mapping(aggregate.get("smoke"), "aggregate recovered smoke")
    alias = _required_string(aggregate.get("alias"), "recovered dual route alias")
    for node_id, rank in node_to_rank.items():
        event = _unique_node_event(
            records, "rank-recovery.smoke-node-completed", node_id
        )
        payload = _payload(event)
        fixture = _mapping(payload.get("fixture_receipt"), "rank smoke fixture receipt")
        if (
            payload.get("rank") != rank
            or _required_sha(payload.get("record_sha256"), "rank smoke receipt digest")
            != references[node_id]
            or fixture.get("endpoint_alias") != alias
            or fixture.get("recipe_content_sha256")
            not in (None, scope["recipe_content_sha256"])
            or payload.get("request_key") is None
        ):
            raise QualificationError(
                f"rank smoke callback does not prove {node_id}'s exact rank"
            )
        _validate_fixture_cases(fixture, scope, require_passed=False)
    aggregate_cases = smoke.get("cases")
    rank_receipts = smoke.get("rank_receipts")
    rank_receipt_rows = (
        {
            _required_string(
                _mapping(item, "rank smoke aggregate receipt").get("node_id"),
                "rank smoke Spark ID",
            ): _mapping(item, "rank smoke aggregate receipt")
            for item in rank_receipts
        }
        if isinstance(rank_receipts, list)
        else {}
    )
    if (
        smoke.get("endpoint_alias") != alias
        or smoke.get("recipe_content_sha256") != scope["recipe_content_sha256"]
        or not isinstance(aggregate_cases, list)
        or [
            _required_string(
                _mapping(case, "aggregate smoke case").get("case_id"), "smoke case ID"
            )
            for case in aggregate_cases
        ]
        != _row_smoke_cases(scope)
        or not isinstance(rank_receipts, list)
        or {
            _required_string(
                _mapping(item, "rank smoke aggregate receipt").get("node_id"),
                "rank smoke Spark ID",
            )
            for item in rank_receipts
        }
        != set(node_to_rank)
        or any(
            rank_receipt_rows[node_id].get("rank") != rank
            or rank_receipt_rows[node_id].get("record_sha256") != references[node_id]
            for node_id, rank in node_to_rank.items()
            if node_id in rank_receipt_rows
        )
        or set(rank_receipt_rows) != set(node_to_rank)
    ):
        raise QualificationError(
            "aggregate rank smoke changed its exact recipe, ranks or case set"
        )
    if aggregate.get("recipe_content_sha256") != scope["recipe_content_sha256"]:
        raise QualificationError("aggregate rank smoke changed recipe content identity")


def _validate_canary_reference(
    records: Sequence[Mapping[str, object]],
    source_event: Mapping[str, object],
    scope: Mapping[str, object],
    exact_preparation: Mapping[str, object],
) -> None:
    source_payload = _payload(source_event)
    if source_event.get("event") == "lane_recovery.intent":
        raw_refs = source_payload.get("canary_record_sha256s")
        if not isinstance(raw_refs, list):
            raise QualificationError(
                "lane recovery intent omits its canary record references"
            )
        canary_ids = [_required_sha(item, "canary event digest") for item in raw_refs]
    else:
        canary_ids = [
            _required_sha(
                source_payload.get("canary_record_sha256"), "canary event digest"
            )
        ]
    candidates = [
        record
        for record in records
        if record.get("event") == "canary.completed"
        and _record_id(record) in canary_ids
        and record.get("recipe") == scope["recipe_key"]
        and record.get("plan_digest") == source_event.get("plan_digest")
    ]
    if len(candidates) != 1:
        raise QualificationError("recovery is not bound to one successful exact canary")
    canary_payload = _payload(candidates[0])
    exact_preparation_record = _mapping(
        canary_payload.get("exact_preparations"), "canary exact preparation"
    )
    if (
        canary_payload.get("recipe_content_sha256") != scope["recipe_content_sha256"]
        or canary_payload.get("package_sha256") != scope["package_sha256"]
        or dict(exact_preparation_record) != dict(exact_preparation)
    ):
        raise QualificationError(
            "canary record differs from the exact recipe, package or preparation"
        )


def _validate_smoke_identity(
    smoke: Mapping[str, object],
    serving: Mapping[str, object],
    scope: Mapping[str, object],
    run_id: str,
    boot_id: str,
) -> None:
    alias = _required_string(serving.get("alias"), "recovered route alias")
    recipe_revision_id = _required_string(
        serving.get("recipe_revision_id"), "recovered recipe revision"
    )
    node_to_rank = _mapping(serving.get("node_to_rank"), "recovered Spark ranks")
    if (
        serving.get("run_id") != run_id
        or serving.get("recipe_key") != scope["recipe_key"]
        or serving.get("recipe_content_sha256") != scope["recipe_content_sha256"]
        or serving.get("package_sha256") != scope["package_sha256"]
        or serving.get("boot_id") != boot_id
        or serving.get("route_state") != "published"
        or serving.get("healthy") is not True
        or smoke.get("run_id") != run_id
        or smoke.get("recipe_key") != scope["recipe_key"]
        or smoke.get("recipe_content_sha256") != scope["recipe_content_sha256"]
        or smoke.get("package_sha256") != scope["package_sha256"]
        or smoke.get("recipe_revision_id") != recipe_revision_id
        or smoke.get("alias") != alias
        or smoke.get("node_to_rank") != dict(node_to_rank)
        or smoke.get("boot_id") != boot_id
    ):
        raise QualificationError(
            "recovered smoke is not bound to the serving run, boot and recipe"
        )
    _validate_fixture_cases(smoke, scope)


def _validate_fixture_cases(
    receipt: Mapping[str, object],
    scope: Mapping[str, object],
    *,
    require_passed: bool = True,
) -> None:
    cases = receipt.get("cases")
    if not isinstance(cases, list):
        raise QualificationError("recovered smoke omits its completed fixture cases")
    observed: list[str] = []
    for raw_case in cases:
        case = _mapping(raw_case, "recovered fixture case")
        case_id = _required_string(case.get("case_id"), "recovered fixture case ID")
        if require_passed and case.get("passed") is not True:
            raise QualificationError("recovered fixture smoke contains a failed case")
        observed.append(case_id)
    if observed != _row_smoke_cases(scope):
        raise QualificationError(
            "recovered fixture smoke differs from the reviewed case set"
        )


def _row_smoke_cases(scope: Mapping[str, object]) -> list[str]:
    cases = scope.get("smoke_cases")
    if not isinstance(cases, list) or any(not isinstance(item, str) for item in cases):
        raise QualificationError("authority row omits its exact smoke case set")
    return list(cases)


def _validate_completion_refs(
    payload: Mapping[str, object],
    source_events: Mapping[str, Mapping[str, object]],
    baseline_boot_id: str,
    recovered_boot_id: str,
) -> None:
    refs = _mapping(payload.get("event_refs"), "single recovery completion event refs")
    for name, event in source_events.items():
        if refs.get(name) != _record_id(event):
            raise QualificationError(
                "single recovery completion changed an exact source event"
            )
    base_payload = _payload(source_events["baseline"])
    if (
        payload.get("baseline_boot_id") != baseline_boot_id
        or payload.get("recovered_boot_id") != recovered_boot_id
        or base_payload.get("boot_id") != baseline_boot_id
    ):
        raise QualificationError(
            "single recovery completion changed its boot identities"
        )
    completion_digest = payload.get("receipt_sha256")
    digest_body = dict(payload)
    digest_body.pop("receipt_sha256", None)
    if completion_digest != _canonical_sha256(
        digest_body, "single recovery completion"
    ):
        raise QualificationError("single recovery completion digest is invalid")


def _runtime_image_identity(
    preparation: Mapping[str, object],
    node_ids: Sequence[str],
    scope: Mapping[str, object],
) -> tuple[str, str]:
    digest = preparation.get("image_digest")
    if not isinstance(digest, str) or _OCI_DIGEST.fullmatch(digest) is None:
        raise QualificationError("exact runtime image digest is absent or invalid")
    architecture = preparation.get("architecture")
    if architecture != "linux-arm64":
        raise QualificationError(
            "exact runtime image architecture is unknown or unsupported"
        )
    if preparation.get("recipe_revision_sha256") != scope["recipe_content_sha256"]:
        raise QualificationError(
            "runtime preparation is bound to another recipe revision"
        )
    targets = preparation.get("target_node_ids")
    if not isinstance(targets, list) or sorted(targets) != sorted(node_ids):
        raise QualificationError(
            "runtime preparation does not cover the exact affected Sparks"
        )
    model_id = _required_sha(
        preparation.get("model_content_sha256"), "prepared Model content digest"
    )
    dependencies = preparation.get("dependency_model_content_sha256")
    if not isinstance(dependencies, list):
        raise QualificationError("runtime preparation Model dependencies are malformed")
    prepared_models = sorted(
        {
            model_id,
            *(
                _required_sha(item, "prepared dependency Model digest")
                for item in dependencies
            ),
        }
    )
    if prepared_models != scope["model_content_sha256s"]:
        raise QualificationError("runtime preparation changed exact Model identities")
    return digest, "linux/arm64"


def _deployment_build_identities(
    provenance_by_node: Mapping[str, Mapping[str, object]],
    node_ids: Sequence[str],
    expected_package_sha256: str,
) -> tuple[str, dict[str, str]]:
    if set(provenance_by_node) != set(node_ids):
        raise QualificationError(
            "DeploymentProvenance does not cover the exact affected Sparks"
        )
    platform_builds: set[str] = set()
    agent_builds: dict[str, str] = {}
    for node_id in node_ids:
        provenance = _mapping(provenance_by_node[node_id], "DeploymentProvenance")
        platform = provenance.get("platform")
        agents = provenance.get("agents")
        if not isinstance(platform, list) or not isinstance(agents, list):
            raise QualificationError(
                "DeploymentProvenance omits platform or agent identities"
            )
        controller_rows = [
            _mapping(item, "Controller deployment provenance")
            for item in platform
            if isinstance(item, Mapping)
            and item.get("boundary") == "controller_deployment"
        ]
        if len(controller_rows) != 1:
            raise QualificationError(
                "current Controller deployment image identity is unavailable"
            )
        controller = controller_rows[0]
        platform_evidence = _mapping(
            controller.get("evidence"), "Controller deployment evidence age"
        )
        image_digest = controller.get("image_digest")
        image_match = (
            _OCI_DIGEST.fullmatch(image_digest)
            if isinstance(image_digest, str)
            else None
        )
        if (
            controller.get("state") != "observed"
            or platform_evidence.get("freshness") != "current"
            or image_match is None
        ):
            raise QualificationError(
                "current Controller deployment image evidence is stale or absent"
            )
        platform_builds.add(image_match.group(1))
        agent_rows = [
            _mapping(item, "agent deployment provenance")
            for item in agents
            if isinstance(item, Mapping) and item.get("node_id") == node_id
        ]
        if len(agent_rows) != 1:
            raise QualificationError(
                f"current authenticated agent identity is unavailable for {node_id}"
            )
        agent = agent_rows[0]
        contact = _mapping(
            agent.get("evidence"), "authenticated agent contact evidence"
        )
        build_digest = agent.get("build_digest")
        build_match = (
            _BUILD_DIGEST.fullmatch(build_digest)
            if isinstance(build_digest, str)
            else None
        )
        if (
            agent.get("connectivity") != "recent"
            or contact.get("freshness") != "current"
            or agent.get("boundary") not in (None, "agent_deployment")
            or build_match is None
            or not isinstance(agent.get("binary_sha256"), str)
            or _SHA256.fullmatch(str(agent["binary_sha256"])) is None
            or agent.get("package_sha256") != expected_package_sha256
        ):
            raise QualificationError(
                f"agent build/package identity is unverified for {node_id}"
            )
        agent_builds[node_id] = build_match.group(1)
    if len(platform_builds) != 1:
        raise QualificationError(
            "affected Sparks report different Controller image identities"
        )
    return next(iter(platform_builds)), agent_builds


def _unique_event(
    records: Sequence[Mapping[str, object]], name: str
) -> Mapping[str, object]:
    matches = [record for record in records if record.get("event") == name]
    if len(matches) != 1:
        raise QualificationError(f"recovery evidence requires exactly one {name} event")
    return matches[0]


def _unique_node_event(
    records: Sequence[Mapping[str, object]], name: str, node_id: str
) -> Mapping[str, object]:
    matches = [
        record
        for record in records
        if record.get("event") == name and _payload(record).get("node_id") == node_id
    ]
    if len(matches) != 1:
        raise QualificationError(
            f"recovery evidence requires exactly one {name} event for {node_id}"
        )
    return matches[0]


def _strictly_ordered(records: Sequence[Mapping[str, object]]) -> bool:
    try:
        positions = [
            _positive_int(record.get("sequence"), "event sequence")
            for record in records
        ]
    except QualificationError:
        return False
    return len(positions) == len(set(positions)) and positions == sorted(positions)


def _validate_ledger_chain(records: Sequence[Mapping[str, object]]) -> None:
    previous = "0" * 64
    for index, record in enumerate(records, start=1):
        sequence = _positive_int(
            record.get("sequence"), "qualification ledger sequence"
        )
        if sequence != index or record.get("previous_sha256") != previous:
            raise QualificationError(
                "qualification evidence ledger chain is discontinuous"
            )
        _validate_ledger_record(record)
        previous = _record_id(record)


def _reconciled_fleet_is_idle(
    value: object,
    selected_nodes: Sequence[str],
    baseline_nodes: Mapping[str, object],
    application_updated_at: object,
) -> bool:
    if not isinstance(value, Mapping) or value.get("schema_version") != 1:
        return False
    try:
        generated_at = _timestamp(value.get("generated_at"), "reconciled Fleet time")
        app_updated = _timestamp(application_updated_at, "cleanup application time")
        cursor = value.get("event_cursor")
        _positive_int(cursor, "reconciled Fleet event cursor")
        _required_string(value.get("authority_revision"), "reconciled Fleet authority")
    except QualificationError:
        return False
    if generated_at < app_updated:
        return False
    raw_nodes = value.get("nodes")
    if not isinstance(raw_nodes, list):
        return False
    nodes: dict[str, Mapping[str, object]] = {}
    for raw_node in raw_nodes:
        if not isinstance(raw_node, Mapping):
            return False
        node_id = raw_node.get("id")
        if not isinstance(node_id, str) or node_id in nodes:
            return False
        nodes[node_id] = raw_node
    if set(nodes) != set(selected_nodes):
        return False
    for node_id, node in nodes.items():
        connection = node.get("connection")
        telemetry = node.get("telemetry")
        loaded = node.get("loaded")
        reservations = node.get("reservations")
        if (
            not isinstance(connection, Mapping)
            or connection.get("online_state") != "online"
            or not isinstance(telemetry, Mapping)
            or telemetry.get("freshness") != "live"
            or not isinstance(loaded, list)
            or loaded
            or not isinstance(reservations, Mapping)
            or any(
                reservations.get(name) != 0
                for name in (
                    "unified_memory_bytes",
                    "host_memory_bytes",
                    "gpu_memory_bytes",
                    "port_count",
                )
            )
        ):
            return False
        if node_id in baseline_nodes:
            sample = telemetry.get("sample")
            baseline = baseline_nodes.get(node_id)
            if not isinstance(sample, Mapping) or not isinstance(baseline, Mapping):
                return False
            baseline = _mapping(baseline_nodes.get(node_id), "baseline Fleet node")
            if (
                not isinstance(sample.get("boot_id"), str)
                or not sample.get("boot_id")
                or sample.get("boot_id") == baseline.get("boot_id")
            ):
                return False
            try:
                _timestamp(sample.get("observed_at"), "reconciled telemetry time")
            except QualificationError:
                return False
    return True


def _validate_ledger_record(record: Mapping[str, object]) -> None:
    supplied = _required_sha(record.get("record_sha256"), "ledger record digest")
    unsigned = {key: value for key, value in record.items() if key != "record_sha256"}
    try:
        encoded = json.dumps(
            unsigned,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise QualificationError(
            "recovery ledger record cannot be canonically encoded"
        ) from error
    if hashlib.sha256(encoded).hexdigest() != supplied:
        raise QualificationError("recovery ledger record failed hash verification")


def _record_id(record: Mapping[str, object]) -> str:
    return _required_sha(record.get("record_sha256"), "ledger event digest")


def _payload(record: Mapping[str, object]) -> Mapping[str, object]:
    return _mapping(record.get("payload"), "recovery event payload")


def _normalize_receipt_for_hash(receipt: Mapping[str, object]) -> dict[str, object]:
    normalized = dict(receipt)
    normalized.setdefault("nodes", [])
    if normalized.get("rank_recovery") is None:
        normalized.pop("rank_recovery", None)
    return normalized


def _canonical_sha256(value: object, label: str) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise QualificationError(f"{label} cannot be canonically encoded") from error
    return hashlib.sha256(encoded).hexdigest()


def _validate_json_schema(
    document: Mapping[str, object], schema: Mapping[str, object]
) -> None:
    try:
        validator_document = cast(Any, dict(schema))
        validator_type = validator_for(validator_document)
        validator_type.check_schema(validator_document)
        errors = sorted(
            validator_type(validator_document).iter_errors(cast(Any, dict(document))),
            key=lambda error: (list(map(str, error.absolute_path)), error.message),
        )
    except Exception as error:
        raise QualificationError(
            "canonical recovery receipt JSON Schema is invalid"
        ) from error
    if errors:
        error = errors[0]
        path = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise QualificationError(
            f"recovery receipt violates canonical schema at {path}: {error.message}"
        )


def _rank_nodes(payload: Mapping[str, object]) -> list[str]:
    raw = payload.get("node_ids")
    if not isinstance(raw, list) or len(raw) != 2:
        raise QualificationError("dual recovery event omits its exact two Spark IDs")
    nodes = [_required_node(item, "dual recovery Spark") for item in raw]
    if len(set(nodes)) != 2:
        raise QualificationError("dual recovery event repeats a Spark ID")
    return nodes


def _rank_map(value: object, node_ids: Sequence[str]) -> dict[str, int]:
    mapping = _mapping(value, "dual recovery Spark/rank mapping")
    if set(mapping) != set(node_ids):
        raise QualificationError("dual recovery Spark/rank mapping is invalid")
    ranks: dict[str, int] = {}
    for node_id, rank in mapping.items():
        if type(rank) is not int:
            raise QualificationError("dual recovery Spark/rank mapping is invalid")
        ranks[node_id] = rank
    if set(ranks.values()) != {0, 1}:
        raise QualificationError("dual recovery ranks are not the exact pair 0 and 1")
    return ranks


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


def _required_node(value: object, label: str) -> str:
    node_id = _required_string(value, label)
    if _NODE_ID.fullmatch(node_id) is None:
        raise QualificationError(f"{label} is not an exact Controller Spark ID")
    return node_id


def _string_sequence(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise QualificationError(f"{label} must be a string array")
    if len(value) != len(set(value)):
        raise QualificationError(f"{label} contains duplicates")
    return list(value)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise QualificationError(f"{label} must be an object")
    return value


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise QualificationError(f"{label} is missing")
    return value


def _required_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise QualificationError(f"{label} is missing or invalid")
    return value


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise QualificationError(f"{label} must be a positive integer")
    return value
