from __future__ import annotations

import json
from pathlib import Path

from vonk_control.recipe_runtime_specs import compile_runtime_spec


def test_runtime_spec_binds_built_image_role_and_mapping_parameters() -> None:
    document = json.loads(
        (Path(__file__).parent / "fixtures/global/recipe-v1-minimal.json").read_text()
    )
    entrypoint = document["topology"]["roles"][0]
    document["artifacts"][0]["roles"] = ["entrypoint", "worker"]
    document["topology"] = {
        **document["topology"],
        "name": "pair",
        "mode": "tensor_parallel",
        "node_count": 2,
        "roles": [
            entrypoint,
            {**entrypoint, "name": "worker", "endpoint_owner": False},
        ],
        "parallelism": {"tensor": 2, "pipeline": 1, "data": 1, "backend": "tcp"},
        "fabric": {"connectivity": "connected", "minimum_bandwidth_mbps": 1},
        "start_order": ["worker", "entrypoint"],
        "stop_order": ["entrypoint", "worker"],
    }
    parameter = {
        "name": "mapped-value",
        "description": "Mapped runtime fixture",
        "type": "integer",
        "default": 7,
        "change_effect": "restart",
    }
    document["parameters"].append(parameter)
    parameter_name = parameter["name"]
    document["runtime"]["arguments"].append(
        {"name": "mapped-value", "parameter": parameter_name}
    )

    spec = compile_runtime_spec(
        document,
        parameters={
            **{item["name"]: item["default"] for item in document["parameters"]},
        },
        role="worker",
        recipe_build_id="00000000-0000-4000-8000-000000000001",
        image_digest="sha256:" + "d" * 64,
    )

    assert spec["runtime"]["image"] == (
        "localhost/vonk/recipe-build-00000000-0000-4000-8000-000000000001"
        "@sha256:" + "d" * 64
    )
    assert spec["runtime"]["execution_harness"] == document["execution"]["harness"]
    assert spec["runtime"]["distribution"] == document["runtime"]["distribution"]
    assert {item["name"]: item["value"] for item in spec["runtime"]["arguments"]}[
        "mapped-value"
    ] == parameter["default"]
    assert all("worker" in item["roles"] for item in spec["artifacts"])
    assert spec["interfaces"] == document["interfaces"]
    assert spec["security"] == document["runtime"]["security"]
    assert spec["lifecycle"] == document["runtime"]["lifecycle"]
