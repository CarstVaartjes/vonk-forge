from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from vonk_control.catalog_contract import catalog_content_sha256
from vonk_control.recipe_contract import recipe_content_sha256
from vonk_control.recipe_runtime_specs import (
    RecipeRuntimeSpecError,
    compile_runtime_spec,
)

ROOT = Path(__file__).resolve().parents[2]
BASE_RECIPE = ROOT / "control/tests/fixtures/global/recipe-v1-minimal.json"
VLLM_HARNESS = ROOT / "config/execution-harnesses/vllm.json"


def _exact_builtin_inputs():
    document = json.loads(BASE_RECIPE.read_text(encoding="utf-8"))
    harness = json.loads(VLLM_HARNESS.read_text(encoding="utf-8"))
    harness_digest = catalog_content_sha256(harness)
    distribution = {
        "schema_version": 1,
        "kind": "runtime-distribution",
        "identity": {"publisher": "vonk-forge", "slug": "vllm-arm64"},
        "metadata": {
            "title": "vLLM ARM64",
            "description": "Exact built-in runtime-spec fixture.",
            "tags": ["synthetic"],
        },
        "implements_harness": {
            "kind": "execution-harness",
            "publisher": "vonk-forge",
            "slug": "vllm",
            "content_sha256": harness_digest,
        },
        "platform": "linux/arm64",
        "image": "registry.example/vonk/vllm@sha256:" + "c" * 64,
        "security": {
            "network_mode": "none",
            "user": "10001:10001",
            "no_new_privileges": True,
            "capabilities": [],
        },
    }
    distribution_digest = catalog_content_sha256(distribution)
    document["execution"]["harness"] = {
        "kind": "execution-harness",
        "publisher": "vonk-forge",
        "slug": "vllm",
        "content_sha256": harness_digest,
    }
    document["runtime"]["distribution"] = {
        "kind": "runtime-distribution",
        "publisher": "vonk-forge",
        "slug": "vllm-arm64",
        "content_sha256": distribution_digest,
    }
    document["runtime"]["entrypoint"] = [
        "/opt/vonk/bin/vllm",
        "serve",
        "/models",
    ]
    document["runtime"]["arguments"].append(
        {"name": "tensor-parallel-size", "value": 1}
    )
    document["runtime"]["security"]["mounts"] = [
        {"source": "model", "target": "/models", "read_only": True},
        {"source": "outputs", "target": "/outputs", "read_only": False},
    ]
    resolved_entities = {
        "model_version": SimpleNamespace(
            content_sha256=document["model"]["content_sha256"]
        ),
        "harness": SimpleNamespace(
            document=harness,
            content_sha256=harness_digest,
        ),
        "runtime_distribution": SimpleNamespace(
            document=distribution,
            content_sha256=distribution_digest,
        ),
        "patch_bundle": None,
    }
    return document, resolved_entities


def test_runtime_spec_is_compiled_from_the_trusted_builtin_projection() -> None:
    document, resolved_entities = _exact_builtin_inputs()
    parameters = {item["name"]: item["default"] for item in document["parameters"]}

    spec = compile_runtime_spec(
        document,
        resolved_entities=resolved_entities,
        parameters=parameters,
        role="entrypoint",
        rank=0,
        recipe_build_id="00000000-0000-4000-8000-000000000001",
        image_digest="sha256:" + "d" * 64,
    )

    assert set(spec) == {
        "identity",
        "model_dependencies",
        "runtime",
        "artifacts",
        "endpoint",
        "security",
        "lifecycle",
        "topology",
    }
    assert spec["model_dependencies"] == []
    assert spec["identity"] == {
        "recipe_revision_sha256": recipe_content_sha256(document),
        "model_version_sha256": document["model"]["content_sha256"],
        "harness_sha256": resolved_entities["harness"].content_sha256,
        "runtime_distribution_sha256": resolved_entities[
            "runtime_distribution"
        ].content_sha256,
        "patch_bundle_sha256": None,
    }
    assert spec["topology"] == {
        "name": document["topology"]["name"],
        "node_count": 1,
        "rank": 0,
        "role": "entrypoint",
    }
    assert set(spec["runtime"]) == {
        "interface",
        "adapter",
        "adapter_version",
        "image",
        "architecture",
        "entrypoint",
        "arguments",
        "environment",
    }
    assert spec["runtime"]["image"] == (
        "localhost/vonk/recipe-build-00000000-0000-4000-8000-000000000001"
        "@sha256:" + "d" * 64
    )
    assert spec["runtime"]["entrypoint"] == [
        "/opt/vonk/bin/vllm",
        "serve",
        "/models",
        "--max-model-len",
        "32768",
        "--tensor-parallel-size",
        "1",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]
    assert spec["runtime"]["arguments"] == []
    assert spec["runtime"]["environment"] == []
    assert spec["endpoint"] == {
        "protocol": "openai",
        "port": 8000,
        "model_aliases": ["synthetic-tiny"],
        "health_path": "/v1/models",
    }
    assert spec["security"] == {
        "devices": ["nvidia.com/gpu=all"],
        "user": "10001:10001",
        "capabilities": [],
        "privileged": False,
        "host_network": False,
        "mounts": [
            {
                "source": "model",
                "target": "/models",
                "read_only": True,
            },
            {
                "source": "outputs",
                "target": "/outputs",
                "read_only": False,
            },
        ],
    }
    assert spec["lifecycle"] == {
        "pre_start": [],
        "post_stop": [],
        "stop_timeout_seconds": 30,
    }


def test_runtime_spec_binds_exact_auxiliary_model_versions() -> None:
    document, resolved_entities = _exact_builtin_inputs()
    dependency = {
        "kind": "model-version",
        "publisher": "vonk-forge",
        "slug": "synthetic-auxiliary-fp16",
        "content_sha256": "f" * 64,
    }
    document["dependencies"] = [dependency]
    resolved_entities["model_dependencies"] = (
        SimpleNamespace(content_sha256="f" * 64),
    )
    parameters = {item["name"]: item["default"] for item in document["parameters"]}

    spec = compile_runtime_spec(
        document,
        resolved_entities=resolved_entities,
        parameters=parameters,
        role="entrypoint",
        rank=0,
        recipe_build_id="00000000-0000-4000-8000-000000000001",
        image_digest="sha256:" + "d" * 64,
    )

    assert spec["model_dependencies"] == [dependency]


def test_runtime_spec_preserves_exact_multi_artifact_targets_and_vllm_primary() -> None:
    document, resolved_entities = _exact_builtin_inputs()
    target = copy.deepcopy(document["artifacts"][0])
    target["id"] = "target"
    target["mount"]["target"] = "/models/target"
    draft = copy.deepcopy(target)
    draft["id"] = "draft"
    draft["mount"]["target"] = "/models/draft"
    document["artifacts"] = [draft, target]
    document["topology"]["roles"][0]["artifacts"] = ["target", "draft"]
    document["runtime"]["entrypoint"][2] = "/models/target"
    document["runtime"]["arguments"].append(
        {
            "name": "speculative-config",
            "value": '{"method":"draft_model","model":"/models/draft"}',
        }
    )
    parameters = {item["name"]: item["default"] for item in document["parameters"]}

    spec = compile_runtime_spec(
        document,
        resolved_entities=resolved_entities,
        parameters=parameters,
        role="entrypoint",
        rank=0,
        recipe_build_id="00000000-0000-4000-8000-000000000001",
        image_digest="sha256:" + "d" * 64,
    )

    assert spec["runtime"]["entrypoint"][2] == "/models/target"
    assert [artifact["mount"]["target"] for artifact in spec["artifacts"]] == [
        "/models/draft",
        "/models/target",
    ]
    assert spec["security"]["mounts"][0] == {
        "source": "model",
        "target": "/models",
        "read_only": True,
    }


def test_runtime_spec_rejects_recipe_authored_shell_authority() -> None:
    document, resolved_entities = _exact_builtin_inputs()
    document["runtime"]["entrypoint"] = ["/bin/sh", "-c", "touch /tmp/owned"]
    parameters = {item["name"]: item["default"] for item in document["parameters"]}

    with pytest.raises(RecipeRuntimeSpecError, match="entrypoint"):
        compile_runtime_spec(
            document,
            resolved_entities=resolved_entities,
            parameters=parameters,
            role="entrypoint",
            rank=0,
            recipe_build_id="00000000-0000-4000-8000-000000000001",
            image_digest="sha256:" + "d" * 64,
        )


def test_runtime_spec_rejects_a_role_that_does_not_bind_the_exact_rank() -> None:
    document, resolved_entities = _exact_builtin_inputs()
    parameters = {item["name"]: item["default"] for item in document["parameters"]}

    with pytest.raises(RecipeRuntimeSpecError, match="role"):
        compile_runtime_spec(
            document,
            resolved_entities=resolved_entities,
            parameters=parameters,
            role="worker",
            rank=0,
            recipe_build_id="00000000-0000-4000-8000-000000000001",
            image_digest="sha256:" + "d" * 64,
        )
