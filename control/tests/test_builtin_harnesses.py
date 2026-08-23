from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from vonk_control.auth import TokenCodec
from vonk_control.catalog_contract import (
    canonical_catalog_document,
    catalog_content_sha256,
)
from vonk_control.catalog_entities import CatalogEntityService
from vonk_control.harnesses import BUILTIN_HARNESS_SLUGS, HarnessRegistry
from vonk_control.harnesses.common import HarnessCompileError
from vonk_control.models import Base

ROOT = Path(__file__).resolve().parents[2]
HARNESS_ROOT = ROOT / "config/execution-harnesses"
BASE_RECIPE = ROOT / "control/tests/fixtures/global/recipe-v1-minimal.json"
REQUIRED_BUILTIN_HARNESS_SLUGS = (
    "vllm",
    "sglang",
    "tensorrt-llm",
    "llama-cpp",
    "ds4",
    "diffusers",
    "comfyui",
    "pytorch-pipeline",
)

EXPECTED_COMMANDS = {
    "vllm": (
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
    ),
    "sglang": (
        "/opt/vonk/bin/sglang-serve",
        "--model-path",
        "/models",
        "--context-length",
        "32768",
        "--tensor-parallel-size",
        "1",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ),
    "tensorrt-llm": (
        "/usr/local/bin/trtllm-serve",
        "serve",
        "/models",
        "--backend",
        "pytorch",
        "--max_batch_size",
        "8",
        "--max_num_tokens",
        "4096",
        "--max_seq_len",
        "32768",
        "--tp_size",
        "1",
        "--pp_size",
        "1",
        "--ep_size",
        "1",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ),
    "llama-cpp": (
        "/opt/vonk/bin/llama-server",
        "--model",
        "/models/model.gguf",
        "--ctx-size",
        "32768",
        "--n-gpu-layers",
        "999",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ),
    "ds4": (
        "/opt/vonk/bin/ds4-serve",
        "--model",
        "/models/target.gguf",
        "--mtp",
        "/models/drafter.gguf",
        "--ctx",
        "32768",
        "--dspark",
        "--cuda",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ),
    "diffusers": (
        "/opt/vonk/bin/diffusers-job",
        "--pipeline",
        "text-to-image",
        "--output-mime",
        "image/png",
        "--output-dir",
        "/outputs",
    ),
    "comfyui": (
        "/opt/vonk/bin/comfyui-job",
        "--workflow",
        "/opt/vonk/source/workflows/image.json",
        "--workflow-sha256",
        "e" * 64,
        "--output-mime",
        "image/png",
        "--output-dir",
        "/outputs",
    ),
    "pytorch-pipeline": (
        "/opt/vonk/bin/pytorch-pipeline",
        "--entrypoint",
        "/opt/vonk/source/pipelines/run.py",
        "--output-mime",
        "model/gltf-binary",
        "--output-dir",
        "/outputs",
    ),
}

ENGINE_RECIPES = {
    "vllm": {
        "entrypoint": ["/opt/vonk/bin/vllm", "serve", "/models"],
        "arguments": [
            {"name": "max-model-len", "value": 32768},
            {"name": "tensor-parallel-size", "value": 1},
        ],
        "interface": "openai",
    },
    "sglang": {
        "entrypoint": ["/opt/vonk/bin/sglang-serve"],
        "arguments": [
            {"name": "model-path", "value": "/models"},
            {"name": "context-length", "value": 32768},
            {"name": "tensor-parallel-size", "value": 1},
        ],
        "interface": "openai",
    },
    "tensorrt-llm": {
        "entrypoint": ["/usr/local/bin/trtllm-serve", "serve", "/models"],
        "arguments": [
            {"name": "backend", "value": "pytorch"},
            {"name": "max-batch-size", "value": 8},
            {"name": "max-num-tokens", "value": 4096},
            {"name": "max-seq-len", "value": 32768},
            {"name": "tp-size", "value": 1},
            {"name": "pp-size", "value": 1},
            {"name": "ep-size", "value": 1},
        ],
        "interface": "openai",
    },
    "llama-cpp": {
        "entrypoint": ["/opt/vonk/bin/llama-server"],
        "arguments": [
            {"name": "model", "value": "/models/model.gguf"},
            {"name": "ctx-size", "value": 32768},
            {"name": "n-gpu-layers", "value": 999},
        ],
        "interface": "openai",
    },
    "ds4": {
        "entrypoint": ["/opt/vonk/bin/ds4-serve"],
        "arguments": [
            {"name": "model", "value": "/models/target.gguf"},
            {"name": "draft-model", "value": "/models/drafter.gguf"},
            {"name": "ctx-size", "value": 32768},
        ],
        "interface": "openai",
    },
    "diffusers": {
        "entrypoint": ["/opt/vonk/bin/diffusers-job"],
        "arguments": [
            {"name": "pipeline", "value": "text-to-image"},
            {"name": "output-mime", "value": "image/png"},
        ],
        "interface": "image-job",
    },
    "comfyui": {
        "entrypoint": ["/opt/vonk/bin/comfyui-job"],
        "arguments": [
            {
                "name": "workflow",
                "value": "/opt/vonk/source/workflows/image.json",
            },
            {"name": "workflow-sha256", "value": "e" * 64},
            {"name": "output-mime", "value": "image/png"},
        ],
        "interface": "image-job",
    },
    "pytorch-pipeline": {
        "entrypoint": ["/opt/vonk/bin/pytorch-pipeline"],
        "arguments": [
            {
                "name": "entrypoint",
                "value": "/opt/vonk/source/pipelines/run.py",
            },
            {"name": "output-mime", "value": "model/gltf-binary"},
        ],
        "interface": "mesh-job",
    },
}

ALLOWED_ENVIRONMENT = {
    "vllm": ("HF_HUB_OFFLINE", "1"),
    "sglang": ("NCCL_DEBUG", "INFO"),
    "tensorrt-llm": ("HF_HUB_OFFLINE", "1"),
    "llama-cpp": ("LLAMA_ARG_N_THREADS", "8"),
    "ds4": ("DS4_LOG_LEVEL", "INFO"),
    "diffusers": ("HF_HUB_OFFLINE", "1"),
    "comfyui": ("COMFYUI_DISABLE_TELEMETRY", "1"),
    "pytorch-pipeline": ("HF_HUB_OFFLINE", "1"),
}


def _harness_document(slug: str) -> dict[str, object]:
    return json.loads((HARNESS_ROOT / f"{slug}.json").read_text(encoding="utf-8"))


def _distribution(slug: str, harness: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "runtime-distribution",
        "identity": {"publisher": "vonk-forge", "slug": f"{slug}-arm64"},
        "metadata": {
            "title": f"{slug} ARM64 runtime",
            "description": "Digest-pinned offline runtime used by compiler tests.",
            "tags": ["synthetic"],
        },
        "implements_harness": {
            "kind": "execution-harness",
            "publisher": "vonk-forge",
            "slug": slug,
            "content_sha256": catalog_content_sha256(harness),
        },
        "platform": "linux/arm64",
        "image": f"registry.example/vonk/{slug}@sha256:" + "c" * 64,
        "security": {
            "network_mode": "none",
            "user": "10001:10001",
            "no_new_privileges": True,
            "capabilities": [],
        },
    }


def _recipe(slug: str) -> dict[str, object]:
    recipe = json.loads(BASE_RECIPE.read_text(encoding="utf-8"))
    case = ENGINE_RECIPES[slug]
    harness = _harness_document(slug)
    recipe["execution"]["harness"] = {
        "kind": "execution-harness",
        "publisher": "vonk-forge",
        "slug": slug,
        "content_sha256": catalog_content_sha256(harness),
    }
    distribution = _distribution(slug, harness)
    recipe["runtime"]["distribution"] = {
        "kind": "runtime-distribution",
        "publisher": "vonk-forge",
        "slug": f"{slug}-arm64",
        "content_sha256": catalog_content_sha256(distribution),
    }
    recipe["runtime"]["entrypoint"] = copy.deepcopy(case["entrypoint"])
    recipe["runtime"]["arguments"] = copy.deepcopy(case["arguments"])
    recipe["runtime"]["environment"] = []
    recipe["runtime"]["security"]["mounts"] = [
        {"source": "model", "target": "/models", "read_only": True},
        {"source": "outputs", "target": "/outputs", "read_only": False},
    ]
    interface = str(case["interface"])
    if interface == "openai":
        recipe["interfaces"] = [
            {
                "adapter": "openai",
                "port": 8000,
                "model_aliases": ["synthetic-tiny"],
                "health_path": "/v1/models",
            }
        ]
        checks = ["endpoint.healthy"]
    else:
        recipe["interfaces"] = [{"adapter": interface, "path": "/outputs"}]
        output_mime = next(
            argument["value"]
            for argument in recipe["runtime"]["arguments"]
            if argument["name"] == "output-mime"
        )
        checks = [_mime_check(str(output_mime))]
    recipe["validation"] = {
        "validators": [{"interface": interface, "checks": checks}],
        "benchmarks": [],
    }
    recipe["parameters"] = []
    return recipe


def _mime_check(value: str) -> str:
    return "artifact.mime." + value.replace("/", "-")


def _compile(
    slug: str,
    *,
    recipe: dict[str, object] | None = None,
    parameters: dict[str, object] | None = None,
    topology: dict[str, object] | None = None,
    role: str = "entrypoint",
    rank: int = 0,
):
    harness = _harness_document(slug)
    selected_recipe = recipe or _recipe(slug)
    return HarnessRegistry.with_builtins().compile(
        harness,
        recipe=selected_recipe,
        distribution=_distribution(slug, harness),
        patch=None,
        parameters=parameters or {},
        topology=topology or selected_recipe["topology"],
        role=role,
        rank=rank,
    )


@pytest.mark.parametrize("slug", BUILTIN_HARNESS_SLUGS)
def test_builtin_harness_emits_an_exact_shell_free_projection(slug: str) -> None:
    projection = _compile(slug)

    assert projection.command == EXPECTED_COMMANDS[slug]
    assert projection.command
    assert projection.command[0] not in {"sh", "bash", "/bin/sh", "/bin/bash"}
    assert "-c" not in projection.command
    assert projection.contract_version == 1
    assert projection.network_mode == "none"
    assert projection.architecture == "linux/arm64"
    assert projection.user == "10001:10001"
    assert projection.no_new_privileges is True
    assert projection.capabilities == ()
    assert all(mount.read_only for mount in projection.model_mounts)


@pytest.mark.parametrize(
    ("slug", "name", "value", "emitted"),
    [
        ("vllm", "gpu-memory-utilization", "0.9", "--gpu-memory-utilization"),
        ("sglang", "quantization", "fp8", "--quantization"),
        (
            "tensorrt-llm",
            "kv-cache-free-gpu-memory-fraction",
            "0.9",
            "--kv_cache_free_gpu_memory_fraction",
        ),
        ("llama-cpp", "parallel", 2, "--parallel"),
        ("ds4", "batch-size", 8, "--batched-session"),
        ("diffusers", "seed", 0, "--seed"),
        ("comfyui", "seed", 0, "--seed"),
        ("pytorch-pipeline", "timeout-seconds", 300, "--timeout-seconds"),
    ],
)
def test_builtin_harness_accepts_only_typed_engine_flags(
    slug: str, name: str, value: object, emitted: str
) -> None:
    recipe = _recipe(slug)
    recipe["runtime"]["arguments"].append({"name": name, "value": value})

    projection = _compile(slug, recipe=recipe)

    assert emitted in projection.command
    assert projection.command[projection.command.index(emitted) + 1] == str(value)


def test_vllm_accepts_nemotron_mamba_and_reasoning_options() -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["arguments"].extend(
        [
            {"name": "mamba-backend", "value": "flashinfer"},
            {"name": "mamba-ssm-cache-dtype", "value": "float16"},
            {"name": "reasoning-parser", "value": "nemotron_v3"},
        ]
    )

    projection = _compile("vllm", recipe=recipe)

    assert "--mamba-backend" in projection.command
    assert "flashinfer" in projection.command
    assert "--mamba-ssm-cache-dtype" in projection.command
    assert "float16" in projection.command
    assert "--reasoning-parser" in projection.command
    assert "nemotron_v3" in projection.command


def test_vllm_accepts_poolside_reasoning_parser() -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["arguments"].append(
        {"name": "reasoning-parser", "value": "poolside_v1"}
    )

    projection = _compile("vllm", recipe=recipe)

    assert "--reasoning-parser" in projection.command
    assert "poolside_v1" in projection.command


def test_vllm_accepts_qwen3_reasoning_parser() -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["arguments"].append(
        {"name": "reasoning-parser", "value": "qwen3"}
    )

    projection = _compile("vllm", recipe=recipe)

    assert "--reasoning-parser" in projection.command
    assert "qwen3" in projection.command


def test_vllm_accepts_glm_reasoning_parser() -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["arguments"].append(
        {"name": "reasoning-parser", "value": "glm45"}
    )

    projection = _compile("vllm", recipe=recipe)

    assert "--reasoning-parser" in projection.command
    assert "glm45" in projection.command


def test_vllm_accepts_offline_and_nvfp4_runtime_environment() -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["environment"] = [
        {"name": "VLLM_NO_USAGE_STATS", "value": "1"},
        {"name": "VLLM_NVFP4_GEMM_BACKEND", "value": "marlin"},
        {"name": "VLLM_USE_FLASHINFER_MOE_FP4", "value": "0"},
    ]

    projection = _compile("vllm", recipe=recipe)

    assert ("VLLM_NO_USAGE_STATS", "1") in projection.environment
    assert ("VLLM_NVFP4_GEMM_BACKEND", "marlin") in projection.environment
    assert ("VLLM_USE_FLASHINFER_MOE_FP4", "0") in projection.environment


@pytest.mark.parametrize("slug", BUILTIN_HARNESS_SLUGS)
def test_builtin_harness_rejects_unallowlisted_engine_flags(slug: str) -> None:
    recipe = _recipe(slug)
    recipe["runtime"]["arguments"].append(
        {"name": "arbitrary-executable-hook", "value": "/tmp/hook"}
    )

    with pytest.raises(HarnessCompileError, match="allowlisted"):
        _compile(slug, recipe=recipe)


@pytest.mark.parametrize(
    ("slug", "name", "invalid"),
    [
        ("vllm", "max-model-len", 0),
        ("sglang", "context-length", 10_000_001),
        ("tensorrt-llm", "max-batch-size", 0),
        ("llama-cpp", "n-gpu-layers", 1000),
        ("ds4", "batch-size", 0),
        ("diffusers", "seed", -1),
        ("comfyui", "seed", -1),
        ("pytorch-pipeline", "timeout-seconds", 3601),
    ],
)
def test_builtin_harness_enforces_numeric_bounds(
    slug: str, name: str, invalid: int
) -> None:
    recipe = _recipe(slug)
    arguments = recipe["runtime"]["arguments"]
    selected = next((item for item in arguments if item["name"] == name), None)
    if selected is None:
        arguments.append({"name": name, "value": invalid})
    else:
        selected["value"] = invalid

    with pytest.raises(HarnessCompileError, match="value"):
        _compile(slug, recipe=recipe)


@pytest.mark.parametrize("slug", BUILTIN_HARNESS_SLUGS)
def test_builtin_harness_emits_only_allowlisted_environment(slug: str) -> None:
    name, value = ALLOWED_ENVIRONMENT[slug]
    recipe = _recipe(slug)
    recipe["runtime"]["environment"] = [{"name": name, "value": value}]

    projection = _compile(slug, recipe=recipe)

    assert projection.environment == ((name, value),)


@pytest.mark.parametrize("slug", BUILTIN_HARNESS_SLUGS)
def test_builtin_harness_rejects_unallowlisted_environment(slug: str) -> None:
    recipe = _recipe(slug)
    recipe["runtime"]["environment"] = [
        {"name": "LD_PRELOAD", "value": "/tmp/injected.so"}
    ]

    with pytest.raises(HarnessCompileError, match="environment"):
        _compile(slug, recipe=recipe)


@pytest.mark.parametrize("slug", BUILTIN_HARNESS_SLUGS)
def test_builtin_harness_requires_the_catalog_declared_interface(slug: str) -> None:
    recipe = _recipe(slug)
    recipe["interfaces"] = [
        {"adapter": "artifact-job", "path": "/outputs"}
        if ENGINE_RECIPES[slug]["interface"] == "openai"
        else {
            "adapter": "openai",
            "port": 8000,
            "model_aliases": ["synthetic-tiny"],
            "health_path": "/v1/models",
        }
    ]

    with pytest.raises(HarnessCompileError, match="interface"):
        _compile(slug, recipe=recipe)


@pytest.mark.parametrize(
    ("slug", "entrypoint"),
    [
        ("vllm", ["vllm", "serve", "/models"]),
        ("sglang", ["python", "-m", "sglang.launch_server"]),
        ("tensorrt-llm", ["trtllm-serve", "serve", "/models"]),
        ("llama-cpp", ["llama-server"]),
        ("ds4", ["/tmp/ds4-serve"]),
        ("diffusers", ["python", "/tmp/job.py"]),
        ("comfyui", ["python", "main.py"]),
        ("pytorch-pipeline", ["bash", "-c", "python run.py"]),
    ],
)
def test_builtin_harness_requires_exact_executable_paths(
    slug: str, entrypoint: list[str]
) -> None:
    recipe = _recipe(slug)
    recipe["runtime"]["entrypoint"] = entrypoint

    with pytest.raises(HarnessCompileError, match="entrypoint"):
        _compile(slug, recipe=recipe)


@pytest.mark.parametrize(
    ("slug", "mode"),
    [
        ("vllm", "data_parallel"),
        ("sglang", "pipeline_parallel"),
        ("tensorrt-llm", "data_parallel"),
        ("llama-cpp", "tensor_parallel"),
        ("ds4", "tensor_parallel"),
        ("diffusers", "tensor_parallel"),
        ("comfyui", "tensor_parallel"),
        ("pytorch-pipeline", "tensor_parallel"),
    ],
)
def test_builtin_harness_rejects_unsupported_topology_modes(
    slug: str, mode: str
) -> None:
    recipe = _recipe(slug)
    recipe["topology"]["mode"] = mode

    with pytest.raises(HarnessCompileError, match="topology"):
        _compile(slug, recipe=recipe, topology=recipe["topology"])


@pytest.mark.parametrize(
    ("slug", "mode"),
    [
        ("vllm", "tensor_parallel"),
        ("vllm", "pipeline_parallel"),
        ("vllm", "hybrid"),
        ("vllm", "ray"),
        ("sglang", "tensor_parallel"),
        ("sglang", "data_parallel"),
        ("sglang", "hybrid"),
        ("tensorrt-llm", "tensor_parallel"),
        ("tensorrt-llm", "pipeline_parallel"),
        ("tensorrt-llm", "hybrid"),
        ("tensorrt-llm", "mpi"),
    ],
)
def test_distributed_engine_modes_are_neither_advertised_nor_compiled(
    slug: str, mode: str
) -> None:
    assert _harness_document(slug)["topology_modes"] == ["single"]
    recipe = _recipe(slug)
    recipe["topology"]["mode"] = mode

    with pytest.raises(HarnessCompileError, match="topology"):
        _compile(slug, recipe=recipe, topology=recipe["topology"])


@pytest.mark.parametrize(
    ("slug", "argument"),
    [
        ("vllm", "tensor-parallel-size"),
        ("sglang", "tensor-parallel-size"),
        ("tensorrt-llm", "tp-size"),
        ("tensorrt-llm", "pp-size"),
        ("tensorrt-llm", "ep-size"),
    ],
)
def test_single_node_engine_boundary_rejects_distributed_parallelism(
    slug: str, argument: str
) -> None:
    recipe = _recipe(slug)
    selected = next(
        item for item in recipe["runtime"]["arguments"] if item["name"] == argument
    )
    selected["value"] = 2

    with pytest.raises(HarnessCompileError, match="single-node"):
        _compile(slug, recipe=recipe)


def test_vllm_parallelism_must_equal_the_exact_topology_world_size() -> None:
    recipe = _recipe("vllm")
    tensor = next(
        item
        for item in recipe["runtime"]["arguments"]
        if item["name"] == "tensor-parallel-size"
    )
    tensor["value"] = 2

    with pytest.raises(HarnessCompileError, match="parallelism"):
        _compile("vllm", recipe=recipe)


def test_topology_fabric_and_backend_must_match_world_size() -> None:
    recipe = _recipe("vllm")
    recipe["topology"]["fabric"]["minimum_bandwidth_mbps"] = 1

    with pytest.raises(HarnessCompileError, match="fabric"):
        _compile("vllm", recipe=recipe)


def test_topology_mode_must_match_parallelism_dimensions() -> None:
    recipe = _recipe("vllm")
    recipe["topology"]["mode"] = "tensor_parallel"

    with pytest.raises(HarnessCompileError, match="topology"):
        _compile("vllm", recipe=recipe)


def test_ds4_rejects_unsupported_multi_node_topology() -> None:
    recipe = _recipe("ds4")
    topology = recipe["topology"]
    topology["name"] = "pair"
    topology["mode"] = "data_parallel"
    topology["node_count"] = 2
    topology["parallelism"]["world_size"] = 2
    topology["parallelism"]["data"] = 2
    topology["parallelism"]["backend"] = "tcp"
    topology["fabric"] = {
        "connectivity": "connected",
        "minimum_bandwidth_mbps": 1,
    }
    topology["roles"] = [
        {**topology["roles"][0], "count": 1},
        {
            **copy.deepcopy(topology["roles"][0]),
            "name": "worker",
            "count": 1,
            "endpoint_owner": False,
        },
    ]
    topology["start_order"] = ["worker", "entrypoint"]
    topology["stop_order"] = ["entrypoint", "worker"]

    with pytest.raises(HarnessCompileError, match="topology"):
        _compile("ds4", recipe=recipe, topology=topology, role="worker", rank=1)


@pytest.mark.parametrize("slug", ["diffusers", "comfyui", "pytorch-pipeline"])
def test_artifact_harnesses_require_isolated_outputs_and_one_mime_validator(
    slug: str,
) -> None:
    projection = _compile(slug)
    assert projection.output_mount.target == "/outputs"
    assert projection.output_mount.read_only is False
    assert projection.output_mount.isolated is True

    recipe = _recipe(slug)
    recipe["validation"]["validators"].append(
        copy.deepcopy(recipe["validation"]["validators"][0])
    )
    with pytest.raises(HarnessCompileError, match="MIME validator"):
        _compile(slug, recipe=recipe)


def test_input_capable_artifact_harness_projects_a_read_only_job_input_mount() -> None:
    recipe = _recipe("diffusers")
    recipe["interfaces"][0]["input"] = {
        "path": "/inputs",
        "required": True,
        "media_types": ["image/png"],
        "max_bytes": 32 * 1024 * 1024,
    }
    recipe["runtime"]["security"]["mounts"].insert(
        1, {"source": "inputs", "target": "/inputs", "read_only": True}
    )

    projection = _compile("diffusers", recipe=recipe)

    assert projection.input_mount is not None
    assert projection.input_mount.source == "/run/vonk/inputs"
    assert projection.input_mount.target == "/inputs"
    assert projection.input_mount.read_only is True
    assert projection.input_mount.isolated is True


def test_diffusers_accepts_specialized_image_layer_pipeline() -> None:
    recipe = _recipe("diffusers")
    pipeline = next(
        item for item in recipe["runtime"]["arguments"] if item["name"] == "pipeline"
    )
    pipeline["value"] = "image-to-layers"
    recipe["runtime"]["arguments"].extend(
        [
            {"name": "true-cfg-scale", "value": "4"},
            {"name": "layers", "value": 4},
            {"name": "resolution", "value": 640},
        ]
    )
    recipe["interfaces"][0]["adapter"] = "artifact-job"
    recipe["validation"]["validators"][0]["interface"] = "artifact-job"
    recipe["interfaces"][0]["input"] = {
        "path": "/inputs",
        "required": True,
        "media_types": ["image/png"],
        "max_bytes": 8 * 1024 * 1024,
    }
    recipe["runtime"]["security"]["mounts"].insert(
        1, {"source": "inputs", "target": "/inputs", "read_only": True}
    )

    projection = _compile("diffusers", recipe=recipe)

    assert "image-to-layers" in projection.command



def test_input_contract_requires_the_exact_read_only_input_mount() -> None:
    recipe = _recipe("diffusers")
    recipe["interfaces"][0]["input"] = {
        "path": "/inputs",
        "required": True,
        "media_types": ["image/png"],
        "max_bytes": 32 * 1024 * 1024,
    }

    with pytest.raises(HarnessCompileError, match="input mount"):
        _compile("diffusers", recipe=recipe)



def test_comfyui_requires_an_immutable_workflow_from_the_recipe_bundle() -> None:
    recipe = _recipe("comfyui")
    workflow = next(
        item for item in recipe["runtime"]["arguments"] if item["name"] == "workflow"
    )
    workflow["value"] = "/opt/vonk/source/../mutable.json"

    with pytest.raises(HarnessCompileError, match="workflow"):
        _compile("comfyui", recipe=recipe)


@pytest.mark.parametrize(
    ("argument_name", "parameter_name"),
    [
        ("workflow", "workflow_path"),
        ("workflow-sha256", "workflow_digest"),
    ],
)
def test_comfyui_workflow_identity_cannot_reference_runtime_parameters(
    argument_name: str, parameter_name: str
) -> None:
    recipe = _recipe("comfyui")
    argument = next(
        item for item in recipe["runtime"]["arguments"] if item["name"] == argument_name
    )
    value = argument.pop("value")
    argument["parameter"] = parameter_name
    recipe["parameters"] = [
        {
            "name": parameter_name,
            "description": "Forbidden mutable workflow identity.",
            "type": "string",
            "default": value,
            "change_effect": "restart",
        }
    ]

    with pytest.raises(HarnessCompileError, match="literal immutable workflow"):
        _compile("comfyui", recipe=recipe, parameters={parameter_name: value})


def test_comfyui_rejects_a_tampered_workflow_digest() -> None:
    recipe = _recipe("comfyui")
    workflow_sha256 = next(
        item
        for item in recipe["runtime"]["arguments"]
        if item["name"] == "workflow-sha256"
    )
    workflow_sha256["value"] = "e" * 63

    with pytest.raises(HarnessCompileError, match="workflow"):
        _compile("comfyui", recipe=recipe)


def test_comfyui_requires_an_exact_source_bundle_identity() -> None:
    recipe = _recipe("comfyui")
    recipe["build"]["context"]["sha256"] = "mutable"

    with pytest.raises(HarnessCompileError, match="source bundle"):
        _compile("comfyui", recipe=recipe)


def test_pytorch_pipeline_entrypoint_must_be_inside_the_signed_source_bundle() -> None:
    recipe = _recipe("pytorch-pipeline")
    entrypoint = next(
        item for item in recipe["runtime"]["arguments"] if item["name"] == "entrypoint"
    )
    entrypoint["value"] = "/tmp/run.py"

    with pytest.raises(HarnessCompileError, match="source bundle"):
        _compile("pytorch-pipeline", recipe=recipe)


def test_pytorch_pipeline_requires_an_exact_source_bundle_identity() -> None:
    recipe = _recipe("pytorch-pipeline")
    recipe["build"]["context"]["media_type"] = "application/octet-stream"

    with pytest.raises(HarnessCompileError, match="source bundle"):
        _compile("pytorch-pipeline", recipe=recipe)


def test_pytorch_pipeline_accepts_a_context_path_with_bundle_identity() -> None:
    recipe = _recipe("pytorch-pipeline")
    recipe["build"]["context"]["path"] = "adapters/video/ltx2-pytorch"

    projection = _compile("pytorch-pipeline", recipe=recipe)

    assert projection.command[0] == "/opt/vonk/bin/pytorch-pipeline"


def test_parameter_substitution_uses_declared_typed_bounds() -> None:
    recipe = _recipe("vllm")
    recipe["parameters"] = [
        {
            "name": "max_model_len",
            "description": "Maximum model length.",
            "type": "integer",
            "default": 32768,
            "minimum": 1024,
            "maximum": 131072,
            "change_effect": "restart",
        }
    ]
    argument = next(
        item
        for item in recipe["runtime"]["arguments"]
        if item["name"] == "max-model-len"
    )
    argument.pop("value")
    argument["parameter"] = "max_model_len"

    projection = _compile("vllm", recipe=recipe, parameters={"max_model_len": 65536})
    assert (
        projection.command[projection.command.index("--max-model-len") + 1] == "65536"
    )

    with pytest.raises(HarnessCompileError, match="parameter"):
        _compile("vllm", recipe=recipe, parameters={"max_model_len": 0})


@pytest.mark.parametrize(
    ("slug", "interface", "output_mime"),
    [
        ("diffusers", "image-job", "image/png"),
        ("diffusers", "audio-job", "audio/wav"),
        ("diffusers", "video-job", "video/mp4"),
        ("diffusers", "artifact-job", "application/octet-stream"),
        ("diffusers", "artifact-job", "image/png"),
        ("comfyui", "image-job", "image/jpeg"),
        ("comfyui", "audio-job", "audio/wav"),
        ("comfyui", "video-job", "video/mp4"),
        ("comfyui", "artifact-job", "application/octet-stream"),
        ("comfyui", "artifact-job", "video/mp4"),
        ("pytorch-pipeline", "image-job", "image/png"),
        ("pytorch-pipeline", "audio-job", "audio/wav"),
        ("pytorch-pipeline", "video-job", "video/mp4"),
        ("pytorch-pipeline", "mesh-job", "model/gltf-binary"),
        ("pytorch-pipeline", "artifact-job", "application/octet-stream"),
        ("pytorch-pipeline", "artifact-job", "model/gltf-binary"),
    ],
)
def test_job_interfaces_accept_only_their_matching_output_media_family(
    slug: str, interface: str, output_mime: str
) -> None:
    recipe = _recipe(slug)
    recipe["interfaces"] = [{"adapter": interface, "path": "/outputs"}]
    output = next(
        item for item in recipe["runtime"]["arguments"] if item["name"] == "output-mime"
    )
    output["value"] = output_mime
    recipe["validation"]["validators"] = [
        {"interface": interface, "checks": [_mime_check(output_mime)]}
    ]

    projection = _compile(slug, recipe=recipe)

    assert projection.command[projection.command.index("--output-mime") + 1] == (
        output_mime
    )


@pytest.mark.parametrize(
    ("slug", "interface", "output_mime"),
    [
        ("diffusers", "image-job", "audio/wav"),
        ("diffusers", "audio-job", "video/mp4"),
        ("comfyui", "video-job", "image/png"),
        ("pytorch-pipeline", "mesh-job", "image/png"),
    ],
)
def test_job_interfaces_reject_mismatched_output_media_families(
    slug: str, interface: str, output_mime: str
) -> None:
    recipe = _recipe(slug)
    recipe["interfaces"] = [{"adapter": interface, "path": "/outputs"}]
    output = next(
        item for item in recipe["runtime"]["arguments"] if item["name"] == "output-mime"
    )
    output["value"] = output_mime
    recipe["validation"]["validators"] = [
        {"interface": interface, "checks": [_mime_check(output_mime)]}
    ]

    with pytest.raises(HarnessCompileError, match="MIME family"):
        _compile(slug, recipe=recipe)


@pytest.mark.parametrize("slug", ["diffusers", "comfyui", "pytorch-pipeline"])
def test_job_mime_validator_evidence_must_match_exactly(slug: str) -> None:
    recipe = _recipe(slug)
    recipe["validation"]["validators"][0]["checks"].append("endpoint.healthy")

    with pytest.raises(HarnessCompileError, match="MIME validator"):
        _compile(slug, recipe=recipe)


def test_production_builtin_harness_set_remains_exactly_the_required_eight() -> None:
    assert BUILTIN_HARNESS_SLUGS == REQUIRED_BUILTIN_HARNESS_SLUGS


def test_catalog_contains_exactly_eight_canonical_builtin_harness_documents() -> None:
    paths = sorted(HARNESS_ROOT.glob("*.json"))
    assert [path.stem for path in paths] == sorted(BUILTIN_HARNESS_SLUGS)

    for path in paths:
        payload = path.read_bytes()
        document = json.loads(payload)
        assert payload == canonical_catalog_document(document) + b"\n"
        assert document["identity"]["slug"] == path.stem
        assert document["compiler_slug"] == path.stem
        assert type(document["contract_version"]) is int
        assert document["contract_version"] == 1
        assert document["topology_modes"]
        assert document["adapters"]
        assert document["capability_requirements"]
        assert isinstance(document["security_exceptions"], list)


def test_catalog_documents_resolve_through_catalog_entity_service() -> None:
    paths = sorted(HARNESS_ROOT.glob("*.json"))
    assert [path.stem for path in paths] == sorted(BUILTIN_HARNESS_SLUGS)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        service = CatalogEntityService(
            session,
            clock=lambda: datetime(2026, 8, 15, tzinfo=UTC),
            cursors=TokenCodec(b"h" * 32).cursor_codec(),
        )
        for path in paths:
            document = json.loads(path.read_text(encoding="utf-8"))
            draft = service.create_draft(document, actor="admin")
            resolved = service.resolve(draft.id, actor="admin")
            assert resolved.content_sha256 == catalog_content_sha256(document)
