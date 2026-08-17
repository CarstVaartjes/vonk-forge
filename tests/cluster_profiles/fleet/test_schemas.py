from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from cluster_profiles.fleet.loaders import (
    TopologyValidationError,
    validate_topology_references,
)


@pytest.fixture
def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _validator(repository_root: Path, name: str) -> jsonschema.Draft202012Validator:
    schema = json.loads((repository_root / "schemas" / name).read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def test_topology_schema_accepts_named_links_without_fixed_function_names(
    repository_root: Path,
) -> None:
    node_a = "spk_00000000000000000000000000000001"
    node_b = "spk_00000000000000000000000000000002"
    document = {
        "schema_version": 1,
        "nodes": [node_a, node_b],
        "links": [
            {
                "id": "fabric-lab-a",
                "kind": "direct-rdma",
                "accepted": True,
                "endpoints": [
                    {
                        "node_id": node_a,
                        "interface": "cx7-a",
                        "address": "10.90.0.1/30",
                    },
                    {
                        "node_id": node_b,
                        "interface": "cx7-z",
                        "address": "10.90.0.2/30",
                    },
                ],
            }
        ],
    }

    _validator(repository_root, "topology.schema.json").validate(document)


@pytest.mark.parametrize("kind", ["unknown", "head-worker", "function100"])
def test_topology_schema_rejects_undeclared_link_kinds(
    repository_root: Path,
    kind: str,
) -> None:
    node_a = "spk_00000000000000000000000000000001"
    node_b = "spk_00000000000000000000000000000002"
    document = {
        "schema_version": 1,
        "nodes": [node_a, node_b],
        "links": [
            {
                "id": "link-a",
                "kind": kind,
                "accepted": False,
                "endpoints": [
                    {"node_id": node_a, "interface": "a"},
                    {"node_id": node_b, "interface": "b"},
                ],
            }
        ],
    }

    with pytest.raises(jsonschema.ValidationError):
        _validator(repository_root, "topology.schema.json").validate(document)


def test_topology_reference_validation_rejects_unknown_endpoint_node() -> None:
    declared = "spk_00000000000000000000000000000001"
    unknown = "spk_ffffffffffffffffffffffffffffffff"
    document = {
        "schema_version": 1,
        "nodes": [declared],
        "links": [
            {
                "id": "management-a",
                "kind": "management",
                "accepted": True,
                "endpoints": [
                    {"node_id": declared, "interface": "wifi0"},
                    {"node_id": unknown, "interface": "wifi0"},
                ],
            }
        ],
    }

    with pytest.raises(TopologyValidationError, match=f"unknown node {unknown}"):
        validate_topology_references(document)


def test_topology_reference_validation_rejects_duplicate_link_ids() -> None:
    node_a = "spk_00000000000000000000000000000001"
    node_b = "spk_00000000000000000000000000000002"
    link = {
        "id": "management-a",
        "kind": "management",
        "accepted": True,
        "endpoints": [
            {"node_id": node_a, "interface": "wifi0"},
            {"node_id": node_b, "interface": "wifi0"},
        ],
    }

    with pytest.raises(TopologyValidationError, match="duplicate link id"):
        validate_topology_references(
            {"schema_version": 1, "nodes": [node_a, node_b], "links": [link, link]}
        )


def _deployment_schema_fixture(node_count: int) -> dict[str, object]:
    return {
        "schema_version": 1,
        "deployment_id": "schema-deployment",
        "family_id": "schema-family",
        "release_digest": "b" * 64,
        "selector": {
            "node_count": node_count,
            "required_labels": {},
            "preferred_node_ids": [],
        },
        "secrets": {},
        "ports": {"inference": 8000},
        "arguments": [],
        "routing": {"alias": "schema-route", "port": "inference"},
        "resources": {
            "memory_bytes": 1,
            "storage_bytes": 1,
            "gpu_count": 1,
        },
    }


@pytest.mark.parametrize(
    "schema_name, document",
    [
        (
            "package-family.schema.json",
            {
                "schema_version": 1,
                "family_id": "schema-family",
                "source": {
                    "provider": "huggingface",
                    "locator": "organization/repository",
                    "policy_refs": ["policy://origins/huggingface"],
                },
                "versions": {
                    "scheme": "pep440",
                    "channels": ["stable"],
                    "include_prereleases": False,
                },
                "discovery": {
                    "poll_interval_seconds": 3600,
                    "bindings": [
                        {
                            "target": "upstream_identity.revision",
                            "source": "release.revision",
                            "value_type": "git-commit",
                            "required": True,
                        }
                    ],
                },
                "resolution": {
                    "recipe_version": 1,
                    "components": [
                        {
                            "name": "model",
                            "kind": "artifact",
                            "media_type": "application/octet-stream",
                            "materialization": "file",
                            "platforms": ["linux/arm64"],
                        }
                    ],
                    "dependencies": [],
                },
                "policy": {
                    "required_evidence": ["checksum"],
                    "license_policy_refs": [],
                },
                "compatibility": {
                    "architectures": ["linux-arm64"],
                    "operating_systems": ["ubuntu-24.04"],
                    "min_memory_bytes": 1,
                    "min_storage_bytes": 1,
                },
                "execution": {"backend": "oci", "adapter_abi": 1},
                "validation": [{"kind": "health", "timeout_seconds": 30}],
                "retention": {"release_count": 2, "rollback_count": 1},
            },
        ),
        (
            "workload-deployment.schema.json",
            _deployment_schema_fixture(16),
        ),
    ],
)
def test_workload_package_schemas_accept_dynamic_repository_documents(
    repository_root: Path,
    schema_name: str,
    document: dict[str, object],
) -> None:
    _validator(repository_root, schema_name).validate(document)


@pytest.mark.parametrize("node_count", [1, 2, 16])
def test_workload_deployment_schema_supports_small_variable_fleets(
    repository_root: Path, node_count: int
) -> None:
    _validator(repository_root, "workload-deployment.schema.json").validate(
        _deployment_schema_fixture(node_count)
    )


def test_workload_deployment_schema_rejects_payload_url_argument(
    repository_root: Path,
) -> None:
    document = _deployment_schema_fixture(1)
    document["arguments"] = ["https://nas.example/payload.bin"]

    with pytest.raises(jsonschema.ValidationError):
        _validator(repository_root, "workload-deployment.schema.json").validate(
            document
        )
