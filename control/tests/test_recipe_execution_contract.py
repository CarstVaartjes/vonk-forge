from __future__ import annotations

import pytest
from vonk_control.recipe_execution_contract import (
    RecipeExecutionContractError,
    StoredRunNodePlan,
    build_plan_document,
    parse_stored_build_policy,
    parse_stored_installation_plan,
    parse_stored_run_endpoint,
    parse_stored_run_plan,
    run_plan_document,
)


def _run_plan() -> dict[str, object]:
    node = {
        "node_id": "spk_" + "0" * 32,
        "rank": 0,
        "role": "entrypoint",
        "endpoint_owner": True,
        "port": 8000,
        "allowed": True,
        "inventory_observed_at": "2026-09-08T10:11:12Z",
        "memory_kind": "unified",
        "required_memory_bytes": 1,
        "available_memory_bytes": None,
        "active_reserved_bytes": 0,
        "free_after_bytes": None,
        "memory_floor_bytes": 0,
        "fabric_address": None,
        "fabric_bandwidth_mbps": None,
        "rendezvous_port": None,
        "blockers": [],
        "warnings": [],
    }
    return {
        "schema_version": 1,
        "observation_schema_version": 2,
        "run_generation": 1,
        "installation_id": "00000000-0000-4000-8000-000000000001",
        "alias": "demo",
        "mapping_id": "00000000-0000-4000-8000-000000000002",
        "mapping_generation": 1,
        "recipe_revision_id": "00000000-0000-4000-8000-000000000003",
        "plan_digest": "a" * 64,
        "nodes": [node],
    }


def test_run_plan_json_roundtrip_retains_required_nulls_and_timestamp_spelling() -> None:
    value = _run_plan()
    assert run_plan_document(value)["nodes"][0]["inventory_observed_at"] == (
        "2026-09-08T10:11:12Z"
    )
    node = run_plan_document(value)["nodes"][0]
    assert node["fabric_address"] is None
    assert node["rendezvous_port"] is None
    assert "execution_mode" not in run_plan_document(value)


def test_persisted_contracts_fail_closed_on_malformed_db_shapes() -> None:
    malformed_run = _run_plan()
    malformed_run.pop("plan_digest")
    with pytest.raises(RecipeExecutionContractError):
        parse_stored_run_plan(malformed_run)

    strict_scalar_run = _run_plan()
    strict_scalar_run["nodes"][0]["allowed"] = 1
    with pytest.raises(RecipeExecutionContractError):
        parse_stored_run_plan(strict_scalar_run)

    with pytest.raises(RecipeExecutionContractError):
        parse_stored_run_endpoint({"url": "http://10.0.0.2:8000", "owner": True})

    with pytest.raises(RecipeExecutionContractError):
        parse_stored_installation_plan([])

    with pytest.raises(RecipeExecutionContractError):
        parse_stored_build_policy(
            {
                "passed": True,
                "source_bundle_sha256": "b" * 64,
                "dockerfile": "Dockerfile",
                "findings": [],
                "artifact_format": "docker-archive-v1",
                "unexpected": False,
            }
        )


def test_build_plan_optional_target_is_omitted_in_canonical_document() -> None:
    value = {
        "schema_version": 1,
        "kind": "recipe.build.v1",
        "build_id": "00000000-0000-4000-8000-000000000001",
        "recipe_revision_id": "00000000-0000-4000-8000-000000000002",
        "recipe_content_sha256": "a" * 64,
        "source_bundle_sha256": "b" * 64,
        "source_bundle_bytes": 1,
        "build_input_sha256": "c" * 64,
        "base_images": [],
        "base_image_storage_bytes": 0,
        "capabilities": [],
        "dockerfile": "Dockerfile",
        "platform": "linux/arm64",
        "arguments": [],
        "network": {"mode": "none", "hosts": []},
        "options": {
            "additional_contexts": [],
            "annotations": [],
            "environment": [],
            "format": "oci",
            "identity_label": False,
            "ignorefile": None,
            "jobs": 1,
            "labels": [],
            "layer_compression": "disabled",
            "layer_labels": [],
            "layers": True,
            "no_hostname": False,
            "no_hosts": False,
            "omit_history": False,
            "os_features": [],
            "os_version": None,
            "shm_bytes": 65536,
            "skip_unused_stages": False,
            "squash": "none",
            "timestamp": None,
            "unset_environment": [],
            "unset_labels": [],
        },
        "limits": {
            "container_socket": False,
            "cpu_cores": 1,
            "gpu": 0,
            "host_mounts": False,
            "memory_bytes": 1,
            "output_bytes": 1,
            "privileged": False,
            "processes": 1,
            "temporary_bytes": 1,
            "timeout_seconds": 1,
        },
        "target": None,
    }
    document = build_plan_document(value)
    assert "target" not in document


def test_inventory_timestamp_schema_remains_a_formatted_string() -> None:
    timestamp = StoredRunNodePlan.model_json_schema()["properties"][
        "inventory_observed_at"
    ]
    assert timestamp == {
        "anyOf": [
            {"format": "date-time", "type": "string"},
            {"type": "null"},
        ],
        "title": "Inventory Observed At",
    }
