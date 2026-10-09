from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from vonk_agent_protocol import canonical_message
from vonk_control.bounded_json import require_mapping, require_sequence
from vonk_control.models import RecipeInstallation
from vonk_control.recipe_execution_contract import (
    RecipeExecutionContractError,
    StoredRunNodePlan,
    StoredRunPlan,
    installation_plan_document,
    installation_serves_authorised_ports,
    parse_stored_build_policy,
    parse_stored_installation_plan,
    parse_stored_run_endpoint,
    parse_stored_run_plan,
    run_plan_document,
)
from vonk_control.runtime_adapters import resolve_runtime_adapter

_ADAPTER = resolve_runtime_adapter("vllm", {"node_count": 1})


def _run_plan() -> dict[str, object]:
    """Build the fixture through the persisted contract, then serialize it as JSON.

    The models are the authority for the field list, so a contract change breaks
    this fixture instead of silently leaving a hand-written copy behind.
    """
    node = StoredRunNodePlan(
        node_id="spk_" + "0" * 32,
        rank=0,
        role="entrypoint",
        endpoint_owner=True,
        port=8000,
        allowed=True,
        inventory_observed_at="2026-09-08T10:11:12Z",
        memory_kind="unified",
        memory_pool="shared",
        required_memory_bytes=1,
        available_memory_bytes=None,
        active_reserved_bytes=0,
        free_after_bytes=None,
        memory_floor_bytes=0,
        fabric_address=None,
        fabric_bandwidth_mbps=None,
        rendezvous_port=None,
        blockers=[],
        warnings=[],
    )
    plan = StoredRunPlan(
        schema_version=1,
        observation_schema_version=2,
        run_generation=1,
        installation_id="00000000-0000-4000-8000-000000000001",
        alias="demo",
        mapping_id="00000000-0000-4000-8000-000000000002",
        mapping_generation=1,
        recipe_revision_id="00000000-0000-4000-8000-000000000003",
        plan_digest="a" * 64,
        nodes=[node],
    )
    return plan.model_dump(mode="json")


def test_run_plan_json_roundtrip_retains_required_nulls_and_timestamp_spelling() -> (
    None
):
    value = _run_plan()
    document = run_plan_document(value)
    nodes = require_sequence(document["nodes"], "run plan nodes")
    node = require_mapping(nodes[0], "run plan node")
    assert node["inventory_observed_at"] == "2026-09-08T10:11:12Z"
    assert node["fabric_address"] is None
    assert node["rendezvous_port"] is None
    assert "execution_mode" not in document


def test_malformed_plan_has_no_execution_authority_and_repaired_plan_is_readable() -> (
    None
):
    malformed_run = _run_plan()
    malformed_run.pop("plan_digest")
    with pytest.raises(RecipeExecutionContractError):
        parse_stored_run_plan(malformed_run)

    strict_scalar_run = _run_plan()
    strict_nodes = require_sequence(strict_scalar_run["nodes"], "run plan nodes")
    strict_node = strict_nodes[0]
    assert isinstance(strict_node, dict)
    strict_node["allowed"] = 1
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

    repaired = parse_stored_run_plan(json.loads(canonical_message(_run_plan())))
    assert repaired.nodes[0].allowed is True
    assert repaired.nodes[0].active_reserved_bytes == 0


@pytest.mark.parametrize(
    "timestamp", ["2026-09-08T10:11:12Z", "2026-09-08T12:11:12+02:00"]
)
def test_inventory_timestamp_spelling_survives_stored_json(timestamp: str) -> None:
    value = _run_plan()
    nodes = require_sequence(value["nodes"], "nodes")
    node = nodes[0]
    assert isinstance(node, dict)
    node["inventory_observed_at"] = timestamp
    restored = parse_stored_run_plan(json.loads(canonical_message(value)))
    assert restored.nodes[0].inventory_observed_at == timestamp
    assert parse_stored_run_plan(run_plan_document(value)) == restored


def _installation_plan(node: dict[str, object]) -> dict[str, object]:
    """Build a stored installation plan the way admission persists it."""

    node_id = "spk_" + "0" * 32
    compiled_plan = json.loads(
        (Path(__file__).parent / "fixtures" / "compiled_workload_v2.json").read_text()
    )
    return installation_plan_document(
        {
            "schema_version": 1,
            "mapping_id": "00000000-0000-4000-8000-000000000002",
            "mapping_generation": 1,
            "recipe_build_id": None,
            "image_digest": "sha256:" + "a" * 64,
            "recipe_revision_id": "00000000-0000-4000-8000-000000000003",
            "recipe_content_sha256": "b" * 64,
            "allowed": True,
            "plan_digest": "c" * 64,
            "nodes": [
                {
                    "node_id": node_id,
                    "rank": 0,
                    "role": "entrypoint",
                    "allowed": True,
                    "inventory_observed_at": "2026-09-08T10:11:12Z",
                    "free_bytes": 1,
                    "active_reserved_bytes": 0,
                    "reused_bytes": 0,
                    "required_download_bytes": 0,
                    "required_bytes": 1,
                    "disk_floor_bytes": 0,
                    "free_after_bytes": 0,
                    "blockers": [],
                    "warnings": [],
                    **node,
                }
            ],
            "compiled_execution_plans": {node_id: compiled_plan},
        }
    )


def _plan_node(document: dict[str, object]) -> dict[str, object]:
    nodes = cast(list[dict[str, object]], document["nodes"])
    return nodes[0]


def test_installation_plan_payload_expectation_is_optional_and_byte_stable() -> None:
    """An absent expectation reads as ``None`` and re-serialises unchanged.

    Plans admitted before the payload expectation existed omit the field.  The
    stored document must therefore round-trip byte-for-byte so the recorded plan
    digest cannot move, and an absent expectation must read as an observation,
    not as an expected zero bytes.
    """

    legacy = _installation_plan({})
    assert "required_payload_bytes" not in _plan_node(legacy)
    parsed = parse_stored_installation_plan(legacy)
    assert parsed.nodes[0].required_payload_bytes is None
    reserialized = installation_plan_document(legacy)
    assert reserialized == legacy
    assert canonical_message(reserialized) == canonical_message(legacy)

    current = _installation_plan({"required_payload_bytes": 100})
    assert _plan_node(current)["required_payload_bytes"] == 100
    assert (
        parse_stored_installation_plan(current).nodes[0].required_payload_bytes == 100
    )
    # An explicit null is the same observation as omission, not a value.
    assert (
        parse_stored_installation_plan(
            _installation_plan({"required_payload_bytes": None})
        )
        .nodes[0]
        .required_payload_bytes
        is None
    )
    assert "required_payload_bytes" not in _plan_node(
        _installation_plan({"required_payload_bytes": None})
    )


@pytest.mark.parametrize(
    ("node_count", "port", "current"),
    [(1, 8000, True), (1, 30000, True), (2, 8888, True), (2, 8000, False)],
)
def test_an_installation_is_current_only_if_it_serves_on_a_port_the_platform_assigns(
    node_count: int, port: int, current: bool
) -> None:
    """A two-Spark plan compiled for the recipe's own port can never launch.

    Wrong implementation this catches: the stale installation was reused, so a
    fixed Controller kept starting the recipe on the port the firewall refuses.
    A single Spark keeps its declared container port.
    """

    plan = _installation_plan({})
    for compiled in require_mapping(plan["compiled_execution_plans"], "plans").values():
        document = cast(dict[str, object], compiled)
        cast(dict[str, object], document["endpoint"])["port"] = port
        cast(dict[str, object], document["topology"])["node_count"] = node_count
        cast(
            dict[str, object], cast(dict[str, object], document["runtime"])["placement"]
        )["world_size"] = node_count
    installation = RecipeInstallation(plan=plan)

    assert installation_serves_authorised_ports(installation) is current
