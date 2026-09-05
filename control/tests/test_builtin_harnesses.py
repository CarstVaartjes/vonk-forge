from __future__ import annotations

import copy
import json
from dataclasses import replace
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
from vonk_control.harnesses.common import HarnessCompileError, validate_projection
from vonk_control.harnesses.sglang import SglangHarnessCompiler
from vonk_control.harnesses.vllm import VllmHarnessCompiler
from vonk_control.models import Base
from vonk_control.runtime_writable_paths import (
    effective_environment,
    environment,
    telemetry_contract,
    writable_paths,
)

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
    "comfyui": ("HF_HUB_OFFLINE", "1"),
    "pytorch-pipeline": ("HF_HUB_OFFLINE", "1"),
}

VLLM_PLATFORM_ENVIRONMENT = (
    ("XDG_CACHE_HOME", "/outputs/cache"),
    ("VLLM_CACHE_ROOT", "/outputs/cache/vllm"),
)


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
    distribution: dict[str, object] | None = None,
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
        distribution=(
            distribution if distribution is not None else _distribution(slug, harness)
        ),
        patch=None,
        parameters=parameters or {},
        topology=topology or selected_recipe["topology"],
        role=role,
        rank=rank,
    )


def _multi_artifact_vllm_recipe(speculative_config: str) -> dict[str, object]:
    recipe = _recipe("vllm")
    target = copy.deepcopy(recipe["artifacts"][0])
    target["id"] = "target"
    target["mount"]["target"] = "/models/target"
    draft = copy.deepcopy(target)
    draft["id"] = "draft"
    draft["mount"]["target"] = "/models/draft"
    recipe["artifacts"] = [draft, target]
    recipe["topology"]["roles"][0]["artifacts"] = ["target", "draft"]
    recipe["runtime"]["entrypoint"] = [
        "/opt/vonk/bin/vllm",
        "serve",
        "/models/target",
    ]
    recipe["runtime"]["arguments"].append(
        {"name": "speculative-config", "value": speculative_config}
    )
    return recipe


def _distributed_sglang_inputs() -> tuple[dict[str, object], dict[str, object]]:
    recipe = _recipe("sglang")
    recipe["artifacts"][0].update({"id": "weights", "roles": ["entrypoint", "worker"]})
    endpoint_role = copy.deepcopy(recipe["topology"]["roles"][0])
    endpoint_role["artifacts"] = ["weights"]
    worker_role = copy.deepcopy(endpoint_role)
    worker_role.update({"name": "worker", "count": 1, "endpoint_owner": False})
    recipe["topology"].update(
        {
            "name": "dual-sglang",
            "mode": "distributed",
            "node_count": 2,
            "roles": [endpoint_role, worker_role],
            "parallelism": {
                "world_size": 2,
                "tensor": 2,
                "pipeline": 1,
                "data": 1,
                "backend": "native",
            },
            "fabric": {
                "connectivity": "connected",
                "minimum_bandwidth_mbps": 200_000,
            },
            "start_order": ["entrypoint", "worker"],
            "stop_order": ["entrypoint", "worker"],
        }
    )
    tensor = next(
        item
        for item in recipe["runtime"]["arguments"]
        if item["name"] == "tensor-parallel-size"
    )
    tensor["value"] = 2
    recipe["runtime"]["arguments"].extend(
        [
            {"name": "trust-remote-code", "value": True},
            {"name": "quantization", "value": "modelopt_fp4"},
            {"name": "attention-backend", "value": "triton"},
            {"name": "page-size", "value": 128},
            {"name": "fp4-gemm-backend", "value": "marlin"},
            {"name": "moe-runner-backend", "value": "marlin"},
            {"name": "mamba-radix-cache-strategy", "value": "extra_buffer"},
            {"name": "mem-fraction-static", "value": "0.85"},
            {"name": "swa-full-tokens-ratio", "value": "0.1"},
            {"name": "mamba-full-memory-ratio", "value": "0.1"},
            {"name": "enable-multimodal", "value": True},
            {"name": "disable-prefill-cuda-graph", "value": True},
            {"name": "reasoning-parser", "value": "inkling"},
            {"name": "tool-call-parser", "value": "inkling"},
            {"name": "served-model-name", "value": "inkling-small"},
        ]
    )
    recipe["runtime"]["environment"] = [
        {"name": "SGLANG_ENABLE_UNIFIED_RADIX_TREE", "value": "1"}
    ]
    recipe["runtime"]["security"]["host_network"] = True
    rank_environment = {
        "GLOO_SOCKET_IFNAME": "enP7s7",
        "NCCL_IB_GID_INDEX": "3",
        "NCCL_IB_HCA": "=rocep1s0f0:1,rocep1s0f1:1",
        "NCCL_SOCKET_IFNAME": "=enP7s7",
        "TP_SOCKET_IFNAME": "enP7s7",
    }
    harness = _harness_document("sglang")
    distribution = _distribution("sglang", harness)
    distribution["capabilities"] = {
        "distributed_sglang": {
            "verified": True,
            "mechanism": "sglang-native",
            "topology_mode": "distributed",
            "node_count": 2,
            "world_size": 2,
            "tensor_parallel_size": 2,
            "pipeline_parallel_size": 1,
            "data_parallel_size": 1,
            "fabric": "nccl-roce",
            "endpoint_role": "entrypoint",
            "worker_role": "worker",
            "rank_loss_withdraws_endpoint": True,
            "launch": {
                "rendezvous": {
                    "local_address_environment": "VONK_LOCAL_ADDR",
                    "master_address_environment": "VONK_MASTER_ADDR",
                    "master_port_environment": "VONK_MASTER_PORT",
                    "master_role": "entrypoint",
                },
                "rank_profiles": [
                    {
                        "rank": 0,
                        "role": "entrypoint",
                        "environment": rank_environment,
                    },
                    {"rank": 1, "role": "worker", "environment": rank_environment},
                ],
            },
        }
    }
    return recipe, distribution


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
    assert projection.read_only_root is True
    assert projection.telemetry == telemetry_contract(slug)
    assert projection.writable_paths == writable_paths(slug)
    assert all(mount.read_only for mount in projection.model_mounts)


def test_builtin_projection_rejects_launch_wrapper_substitution() -> None:
    projection = _compile("vllm")
    projection = replace(projection, command=("/bin/sh", "-c", "vllm"))
    with pytest.raises(HarnessCompileError, match="launch|shell"):
        validate_projection(projection)


def test_builtin_projection_rejects_writable_contract_tampering() -> None:
    projection = _compile("vllm")
    with pytest.raises(HarnessCompileError, match="writable"):
        validate_projection(replace(projection, writable_paths=()))
    with pytest.raises(HarnessCompileError, match="read-only root"):
        validate_projection(replace(projection, read_only_root=False))


def test_builtin_telemetry_contract_matches_effective_launch() -> None:
    for slug in BUILTIN_HARNESS_SLUGS:
        projection = _compile(slug)
        telemetry = projection.telemetry
        assert telemetry is not None
        if telemetry.path is None:
            assert telemetry.adapter == "unsupported"
        elif slug == "comfyui":
            assert projection.command[0] == "/opt/vonk/bin/comfyui-job"
            assert telemetry.path == "/queue"
        else:
            assert "--host" in projection.command
            assert "--port" in projection.command


def test_vllm_projects_the_single_artifacts_exact_named_mount_path() -> None:
    recipe = _recipe("vllm")
    recipe["artifacts"][0]["mount"]["target"] = "/models/weights"
    recipe["runtime"]["entrypoint"][2] = "/models/weights"

    projection = _compile("vllm", recipe=recipe)

    assert projection.command[2] == "/models/weights"
    assert projection.model_mounts[0].target == "/models"


def test_vllm_accepts_explicit_multi_artifact_mounts_independent_of_order() -> None:
    speculative_config = '{"method":"draft_model","model":"/models/draft"}'
    recipe = _multi_artifact_vllm_recipe(speculative_config)

    projection = _compile("vllm", recipe=recipe)

    assert projection.command[2] == "/models/target"
    assert projection.command[projection.command.index("--speculative-config") + 1] == (
        speculative_config
    )


def test_vllm_keeps_model_less_mtp_config_valid_for_one_artifact() -> None:
    recipe = _recipe("vllm")
    speculative_config = '{"method":"dspark","num_speculative_tokens":5}'
    recipe["runtime"]["arguments"].append(
        {"name": "speculative-config", "value": speculative_config}
    )

    projection = _compile("vllm", recipe=recipe)

    assert projection.command[projection.command.index("--speculative-config") + 1] == (
        speculative_config
    )


@pytest.mark.parametrize(
    ("artifact_ids", "artifact_targets"),
    [
        (("primary", "draft"), ("/models/primary", "/models/draft")),
        (("target", "draft"), ("/models/target", "/models/target")),
        (("target", "draft"), ("/models", "/models/draft")),
        (("target", "draft"), ("/models/primary", "/models/draft")),
    ],
)
def test_vllm_rejects_missing_or_ambiguous_multi_artifact_mounts(
    artifact_ids: tuple[str, str], artifact_targets: tuple[str, str]
) -> None:
    recipe = _multi_artifact_vllm_recipe(
        '{"method":"draft_model","model":"/models/draft"}'
    )
    for artifact, artifact_id, target in zip(
        recipe["artifacts"], artifact_ids, artifact_targets, strict=True
    ):
        artifact["id"] = artifact_id
        artifact["mount"]["target"] = target

    with pytest.raises(HarnessCompileError, match="artifact|mount"):
        _compile("vllm", recipe=recipe)


def test_vllm_rejects_an_entrypoint_path_that_is_not_the_primary_mount() -> None:
    recipe = _multi_artifact_vllm_recipe(
        '{"method":"draft_model","model":"/models/draft"}'
    )
    recipe["runtime"]["entrypoint"][2] = "/models/draft"

    with pytest.raises(HarnessCompileError, match="entrypoint"):
        _compile("vllm", recipe=recipe)


@pytest.mark.parametrize("include_speculative_config", [False, True])
def test_vllm_rejects_an_unreferenced_companion_artifact(
    include_speculative_config: bool,
) -> None:
    recipe = _multi_artifact_vllm_recipe(
        '{"method":"dspark","num_speculative_tokens":5}'
    )
    if not include_speculative_config:
        recipe["runtime"]["arguments"] = [
            argument
            for argument in recipe["runtime"]["arguments"]
            if argument["name"] != "speculative-config"
        ]

    with pytest.raises(HarnessCompileError, match="companion artifact"):
        _compile("vllm", recipe=recipe)


def test_vllm_rejects_more_than_one_companion_artifact() -> None:
    recipe = _multi_artifact_vllm_recipe(
        '{"method":"draft_model","model":"/models/draft"}'
    )
    extra = copy.deepcopy(recipe["artifacts"][0])
    extra["id"] = "extra"
    extra["mount"]["target"] = "/models/extra"
    recipe["artifacts"].append(extra)
    recipe["topology"]["roles"][0]["artifacts"].append("extra")

    with pytest.raises(HarnessCompileError, match="at most one companion"):
        _compile("vllm", recipe=recipe)


@pytest.mark.parametrize(
    "speculative_config",
    [
        '{"method":"draft_model","model":"/models/target"}',
        '{"method":"draft_model","model":"/models/unknown"}',
        '{"method":"draft_model","model":null}',
        '{"method":"draft_model","model":"/models/draft","model":"/models/target"}',
        '{"method":"draft_model","model":',
    ],
)
def test_vllm_rejects_unknown_missing_or_ambiguous_speculative_model_paths(
    speculative_config: str,
) -> None:
    recipe = _multi_artifact_vllm_recipe(speculative_config)

    with pytest.raises(HarnessCompileError, match="speculative config"):
        _compile("vllm", recipe=recipe)


def test_ds4_target_only_mode_omits_speculative_decoding_flags() -> None:
    recipe = _recipe("ds4")
    recipe["runtime"]["arguments"] = [
        argument
        for argument in recipe["runtime"]["arguments"]
        if argument["name"] != "draft-model"
    ]

    projection = _compile("ds4", recipe=recipe)

    assert projection.command == (
        "/opt/vonk/bin/ds4-serve",
        "--model",
        "/models/target.gguf",
        "--ctx",
        "32768",
        "--cuda",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    )
    assert "--mtp" not in projection.command
    assert "--dspark" not in projection.command


def test_ds4_still_requires_a_target_model() -> None:
    recipe = _recipe("ds4")
    recipe["runtime"]["arguments"] = [
        argument
        for argument in recipe["runtime"]["arguments"]
        if argument["name"] != "model"
    ]

    with pytest.raises(HarnessCompileError, match="target model path"):
        _compile("ds4", recipe=recipe)


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


@pytest.mark.parametrize(
    ("parser", "plugin"),
    [
        ("nano_v3", "/models/nano_v3_reasoning_parser.py"),
        ("super_v3", "/models/super_v3_reasoning_parser.py"),
    ],
)
def test_vllm_accepts_only_snapshot_owned_nemotron_parser_plugins(
    parser: str, plugin: str
) -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["arguments"].extend(
        [
            {"name": "reasoning-parser-plugin", "value": plugin},
            {"name": "reasoning-parser", "value": parser},
        ]
    )

    projection = _compile("vllm", recipe=recipe)

    assert (
        projection.command[projection.command.index("--reasoning-parser-plugin") + 1]
        == plugin
    )
    assert (
        projection.command[projection.command.index("--reasoning-parser") + 1] == parser
    )


def test_vllm_accepts_super_snapshot_parser_with_a_companion_model() -> None:
    recipe = _multi_artifact_vllm_recipe('{"method":"mtp","model":"/models/draft"}')
    plugin = "/models/target/super_v3_reasoning_parser.py"
    recipe["runtime"]["arguments"].extend(
        [
            {"name": "reasoning-parser-plugin", "value": plugin},
            {"name": "reasoning-parser", "value": "super_v3"},
        ]
    )

    projection = _compile("vllm", recipe=recipe)

    assert (
        projection.command[projection.command.index("--reasoning-parser-plugin") + 1]
        == plugin
    )


@pytest.mark.parametrize(
    "arguments",
    [
        [{"name": "reasoning-parser", "value": "nano_v3"}],
        [
            {
                "name": "reasoning-parser-plugin",
                "value": "/models/super_v3_reasoning_parser.py",
            },
            {"name": "reasoning-parser", "value": "nano_v3"},
        ],
        [
            {
                "name": "reasoning-parser-plugin",
                "value": "/models/nano_v3_reasoning_parser.py",
            },
            {"name": "reasoning-parser", "value": "nemotron_v3"},
        ],
    ],
)
def test_vllm_rejects_missing_mismatched_or_unowned_reasoning_plugins(
    arguments: list[dict[str, object]],
) -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["arguments"].extend(arguments)

    with pytest.raises(HarnessCompileError, match="reasoning parser"):
        _compile("vllm", recipe=recipe)


def test_vllm_accepts_nemotron_fp4_and_flashinfer_backend() -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["arguments"].append(
        {"name": "quantization", "value": "modelopt_fp4"}
    )
    recipe["runtime"]["environment"] = [
        {"name": "VLLM_FLASHINFER_MOE_BACKEND", "value": "throughput"}
    ]

    projection = _compile("vllm", recipe=recipe)

    assert (
        projection.command[projection.command.index("--quantization") + 1]
        == "modelopt_fp4"
    )
    assert ("VLLM_FLASHINFER_MOE_BACKEND", "throughput") in projection.environment


def test_vllm_accepts_bounded_nemotron_dspark_options() -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["arguments"].extend(
        [
            {"name": "moe-backend", "value": "marlin"},
            {"name": "mamba-cache-mode", "value": "align"},
        ]
    )

    projection = _compile("vllm", recipe=recipe)

    assert projection.command[projection.command.index("--moe-backend") + 1] == (
        "marlin"
    )
    assert (
        projection.command[projection.command.index("--mamba-cache-mode") + 1]
        == "align"
    )


@pytest.mark.parametrize("backend", ["FLASH_ATTN", "FLASHINFER", "TRITON_ATTN"])
def test_vllm_accepts_reviewed_attention_backends(backend: str) -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["arguments"].append(
        {"name": "attention-backend", "value": backend}
    )

    projection = _compile("vllm", recipe=recipe)

    assert projection.command[projection.command.index("--attention-backend") + 1] == (
        backend
    )


@pytest.mark.parametrize("backend", ["auto", "flashinfer", "TRITON_MLA"])
def test_vllm_rejects_unreviewed_attention_backends(backend: str) -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["arguments"].append(
        {"name": "attention-backend", "value": backend}
    )

    with pytest.raises(HarnessCompileError, match="value is invalid"):
        _compile("vllm", recipe=recipe)


@pytest.mark.parametrize(
    ("name", "flag"),
    [
        ("enable-chunked-prefill", "--enable-chunked-prefill"),
        ("disable-chunked-prefill", "--no-enable-chunked-prefill"),
    ],
)
def test_vllm_accepts_explicit_chunked_prefill_control(name: str, flag: str) -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["arguments"].append({"name": name, "value": True})

    projection = _compile("vllm", recipe=recipe)

    assert flag in projection.command


def test_vllm_rejects_conflicting_chunked_prefill_controls() -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["arguments"].extend(
        [
            {"name": "enable-chunked-prefill", "value": True},
            {"name": "disable-chunked-prefill", "value": True},
        ]
    )

    with pytest.raises(HarnessCompileError, match="chunked prefill controls conflict"):
        _compile("vllm", recipe=recipe)


@pytest.mark.parametrize(
    ("name", "value"),
    [("moe-backend", "triton"), ("mamba-cache-mode", "all")],
)
def test_vllm_rejects_unreviewed_nemotron_dspark_options(name: str, value: str) -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["arguments"].append({"name": name, "value": value})

    with pytest.raises(HarnessCompileError, match="value is invalid"):
        _compile("vllm", recipe=recipe)


def test_vllm_accepts_text_only_multimodal_mode() -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["arguments"].append(
        {"name": "language-model-only", "value": True}
    )

    projection = _compile("vllm", recipe=recipe)

    assert "--language-model-only" in projection.command


def test_vllm_accepts_bounded_offline_multimodal_recipe_arguments() -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["security"]["mounts"].append(
        {"source": "inputs", "target": "/inputs", "read_only": True}
    )
    recipe["runtime"]["arguments"].extend(
        [
            {"name": "allowed-local-media-path", "value": "/inputs"},
            {
                "name": "limit-mm-per-prompt",
                "value": '{"image":4,"video":1,"audio":1}',
            },
            {
                "name": "media-io-kwargs",
                "value": '{"video":{"fps":2,"num_frames":256}}',
            },
            {"name": "video-pruning-rate", "value": "0.5"},
            {"name": "chat-template-content-format", "value": "openai"},
            {"name": "generation-config", "value": "auto"},
            {"name": "mm-processor-cache-gb", "value": 0},
            {"name": "no-enable-prefix-caching", "value": True},
            {"name": "reasoning-parser", "value": "muse_glimmer"},
            {"name": "tool-call-parser", "value": "muse_glimmer"},
        ]
    )

    projection = _compile("vllm", recipe=recipe)

    assert projection.input_mount is not None
    assert projection.input_mount.target == "/inputs"
    assert "--no-enable-prefix-caching" in projection.command
    assert projection.command[projection.command.index("--generation-config") + 1] == (
        "auto"
    )
    assert (
        projection.command[projection.command.index("--limit-mm-per-prompt") + 1]
        == '{"image":4,"video":1,"audio":1}'
    )
    assert (
        projection.command[projection.command.index("--media-io-kwargs") + 1]
        == '{"video":{"fps":2,"num_frames":256}}'
    )
    assert (
        projection.command[projection.command.index("--video-pruning-rate") + 1]
        == "0.5"
    )


@pytest.mark.parametrize(
    "value",
    [
        "{}",
        '{"document":1}',
        '{"image":17}',
        '{"audio":17}',
        '{"image":true}',
        '{"image":1,"image":2}',
    ],
)
def test_vllm_rejects_unbounded_or_ambiguous_multimodal_limits(value: str) -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["arguments"].append(
        {"name": "limit-mm-per-prompt", "value": value}
    )

    with pytest.raises(HarnessCompileError, match="argument value"):
        _compile("vllm", recipe=recipe)


@pytest.mark.parametrize(
    "value",
    [
        "{}",
        '{"audio":{"sample_rate":16000}}',
        '{"video":{}}',
        '{"video":{"fps":0}}',
        '{"video":{"fps":61}}',
        '{"video":{"fps":true}}',
        '{"video":{"num_frames":0}}',
        '{"video":{"num_frames":257}}',
        '{"video":{"num_frames":1.5}}',
        '{"video":{"fps":2,"unknown":1}}',
        '{"video":{"fps":2,"fps":3}}',
    ],
)
def test_vllm_rejects_unsafe_media_io_kwargs(value: str) -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["arguments"].append({"name": "media-io-kwargs", "value": value})

    with pytest.raises(HarnessCompileError, match="argument value"):
        _compile("vllm", recipe=recipe)


@pytest.mark.parametrize("value", ["-0.01", "1", "nan", "1e999"])
def test_vllm_rejects_invalid_video_pruning_rate(value: str) -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["arguments"].append(
        {"name": "video-pruning-rate", "value": value}
    )

    with pytest.raises(HarnessCompileError, match="argument value"):
        _compile("vllm", recipe=recipe)


def test_vllm_local_media_path_requires_declared_input_mount() -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["arguments"].append(
        {"name": "allowed-local-media-path", "value": "/inputs"}
    )

    with pytest.raises(HarnessCompileError, match="input mount"):
        _compile("vllm", recipe=recipe)


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


def test_vllm_accepts_deepseek_r1_reasoning_parser() -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["arguments"].append(
        {"name": "reasoning-parser", "value": "deepseek_r1"}
    )

    projection = _compile("vllm", recipe=recipe)

    parser = projection.command.index("--reasoning-parser")
    assert projection.command[parser + 1] == "deepseek_r1"


def test_vllm_accepts_glm_sparse_mla_runtime_contract() -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["arguments"].extend(
        [
            {"name": "kv-cache-memory-bytes", "value": 11_811_160_064},
            {"name": "kv-cache-dtype", "value": "fp8_ds_mla"},
            {"name": "decode-context-parallel-size", "value": 1},
            {"name": "dcp-comm-backend", "value": "ag_rs"},
            {"name": "mm-encoder-tp-mode", "value": "data"},
            {
                "name": "hf-overrides",
                "value": '{"num_experts_per_tok":4,"index_topk_freq":8}',
            },
            {
                "name": "compilation-config",
                "value": '{"cudagraph_mode":"FULL","cudagraph_capture_sizes":[4,8],"pass_config":{"fuse_gemm_comms":true}}',
            },
            {"name": "disable-flashinfer-autotune", "value": True},
        ]
    )
    recipe["runtime"]["environment"] = [
        {"name": "GLM_MOE_AQLM_CB", "value": "l1"},
        {"name": "GLM_MOE_AQLM_STREAM", "value": "1"},
        {"name": "GLM_NVFP4_STREAM", "value": "1"},
        {"name": "GLM52_B12X_MLA", "value": "1"},
        {"name": "GLM52_BIND_HOST_TRITON", "value": "1"},
        {"name": "GLM52_MQA_LOGITS_TRITON", "value": "1"},
        {"name": "GLM52_PAGED_MQA_TRITON", "value": "1"},
        {"name": "GLM52_PAGED_MQA_TOPK_CHUNK_SIZE", "value": "8192"},
        {"name": "NCCL_MAX_NCHANNELS", "value": "4"},
        {"name": "NCCL_MIN_NCHANNELS", "value": "4"},
        {"name": "VLLM_DISABLE_FP8_W8A16", "value": "0"},
        {"name": "VLLM_MARLIN_USE_ATOMIC_ADD", "value": "1"},
    ]

    projection = _compile("vllm", recipe=recipe)

    assert "--kv-cache-memory-bytes" in projection.command
    assert "--kv-cache-dtype" in projection.command
    assert "fp8_ds_mla" in projection.command
    assert "--decode-context-parallel-size" in projection.command
    assert "--dcp-comm-backend" in projection.command
    assert "--mm-encoder-tp-mode" in projection.command
    assert "--hf-overrides" in projection.command
    assert "--compilation-config" in projection.command
    assert "--no-enable-flashinfer-autotune" in projection.command
    assert set(projection.environment) == set(environment("vllm", ())) | {
        (item["name"], item["value"]) for item in recipe["runtime"]["environment"]
    }


def test_vllm_accepts_glm53_dspark_runtime_contract() -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["arguments"].extend(
        [
            {"name": "block-size", "value": 7168},
            {"name": "kv-cache-memory", "value": 9_663_676_416},
            {"name": "kv-cache-dtype", "value": "fp8_e4m3"},
            {"name": "enforce-eager", "value": True},
            {"name": "skip-mm-profiling", "value": True},
            {
                "name": "chat-template",
                "value": "/opt/vonk/templates/glm53-chat-template-mm.jinja",
            },
            {"name": "limit-mm-per-prompt", "value": '{"image":4,"video":1}'},
            {
                "name": "speculative-config",
                "value": '{"method":"mtp","num_speculative_tokens":4}',
            },
            {"name": "tool-call-parser", "value": "glm47"},
            {"name": "reasoning-parser", "value": "glm45"},
            {"name": "enable-auto-tool-choice", "value": True},
        ]
    )

    projection = _compile("vllm", recipe=recipe)

    assert projection.command[projection.command.index("--block-size") + 1] == "7168"
    assert (
        projection.command[projection.command.index("--kv-cache-memory") + 1]
        == "9663676416"
    )
    assert "--enforce-eager" in projection.command
    assert "--skip-mm-profiling" in projection.command
    assert projection.command[projection.command.index("--chat-template") + 1] == (
        "/opt/vonk/templates/glm53-chat-template-mm.jinja"
    )


def test_vllm_accepts_immutable_model_thinking_off_chat_template() -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["arguments"].append(
        {
            "name": "chat-template",
            "value": "/models/target/chat_template.thinking-off.jinja",
        }
    )

    projection = _compile("vllm", recipe=recipe)

    assert projection.command[projection.command.index("--chat-template") + 1] == (
        "/models/target/chat_template.thinking-off.jinja"
    )


@pytest.mark.parametrize(
    "name",
    ["hf-overrides", "compilation-config"],
)
def test_vllm_rejects_invalid_json_object_options(name: str) -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["arguments"].append(
        {"name": name, "value": '{"mode":3,"mode":2}'}
    )

    with pytest.raises(HarnessCompileError, match="argument value is invalid"):
        _compile("vllm", recipe=recipe)


def test_vllm_projects_a_verified_ray_adapter_contract() -> None:
    recipe = _recipe("vllm")
    artifact = recipe["artifacts"][0]
    artifact.update({"id": "weights", "roles": ["entrypoint", "worker"]})
    role = copy.deepcopy(recipe["topology"]["roles"][0])
    role["artifacts"] = ["weights"]
    worker = copy.deepcopy(role)
    worker.update({"name": "worker", "count": 2, "endpoint_owner": False})
    recipe["topology"].update(
        {
            "name": "triple",
            "mode": "distributed",
            "node_count": 3,
            "roles": [role, worker],
            "parallelism": {
                "world_size": 3,
                "tensor": 3,
                "pipeline": 1,
                "data": 1,
                "backend": "ray",
            },
            "fabric": {
                "connectivity": "connected",
                "minimum_bandwidth_mbps": 200_000,
            },
            "start_order": ["entrypoint", "worker"],
            "stop_order": ["entrypoint", "worker"],
        }
    )
    tensor = next(
        item
        for item in recipe["runtime"]["arguments"]
        if item["name"] == "tensor-parallel-size"
    )
    tensor["value"] = 3
    recipe["runtime"]["security"]["host_network"] = True
    harness = _harness_document("vllm")
    distribution = _distribution("vllm", harness)
    rank_environment = {
        "GLOO_SOCKET_IFNAME": "enP7s7",
        "NCCL_IB_GID_INDEX": "3",
        "NCCL_IB_HCA": "=rocep1s0f0:1,rocep1s0f1:1",
        "NCCL_SOCKET_IFNAME": "=enP7s7",
        "TP_SOCKET_IFNAME": "enP7s7",
    }
    distribution["capabilities"] = {
        "distributed_vllm": {
            "verified": True,
            "mechanism": "vllm-ray",
            "topology_mode": "distributed",
            "node_count": 3,
            "world_size": 3,
            "tensor_parallel_size": 3,
            "pipeline_parallel_size": 1,
            "data_parallel_size": 1,
            "fabric": "nccl-roce",
            "endpoint_role": "entrypoint",
            "worker_role": "worker",
            "rank_loss_withdraws_endpoint": True,
            "launch": {
                "rendezvous": {
                    "local_address_environment": "VONK_LOCAL_ADDR",
                    "master_address_environment": "VONK_MASTER_ADDR",
                    "master_port_environment": "VONK_MASTER_PORT",
                    "master_role": "entrypoint",
                },
                "rank_profiles": [
                    {"rank": 0, "role": "entrypoint", "environment": rank_environment},
                    {"rank": 1, "role": "worker", "environment": rank_environment},
                    {"rank": 2, "role": "worker", "environment": rank_environment},
                ],
            },
        }
    }

    projection = VllmHarnessCompiler().compile(
        recipe,
        distribution,
        {},
        {},
        recipe["topology"],
        "worker",
        1,
    )

    backend = projection.command.index("--distributed-executor-backend")
    assert projection.command[backend + 1] == "ray"
    assert "--headless" in projection.command

    # The runtime distribution mechanism and recipe topology backend form a
    # pair.  Both supported vLLM distributed mechanisms must compile.
    recipe["topology"]["parallelism"]["backend"] = "mp"
    distribution["capabilities"]["distributed_vllm"]["mechanism"] = "vllm-mp"
    projection = VllmHarnessCompiler().compile(
        recipe,
        distribution,
        {},
        {},
        recipe["topology"],
        "worker",
        1,
    )
    backend = projection.command.index("--distributed-executor-backend")
    assert projection.command[backend + 1] == "mp"

    distribution["capabilities"]["distributed_vllm"]["mechanism"] = "vllm-ray"
    recipe["topology"]["parallelism"]["backend"] = "mp"
    with pytest.raises(
        HarnessCompileError,
        match="verified distributed vLLM distribution",
    ):
        VllmHarnessCompiler().compile(
            recipe,
            distribution,
            {},
            {},
            recipe["topology"],
            "worker",
            1,
        )

    recipe["topology"]["parallelism"]["backend"] = "ray"
    distribution["capabilities"]["distributed_vllm"]["mechanism"] = "vllm-mp"
    with pytest.raises(
        HarnessCompileError,
        match="verified distributed vLLM distribution",
    ):
        VllmHarnessCompiler().compile(
            recipe,
            distribution,
            {},
            {},
            recipe["topology"],
            "worker",
            1,
        )


def test_sglang_projects_verified_native_distributed_inkling_contract() -> None:
    recipe, distribution = _distributed_sglang_inputs()

    projection = SglangHarnessCompiler().compile(
        recipe,
        distribution,
        {},
        {},
        recipe["topology"],
        "worker",
        1,
    )

    assert projection.command[projection.command.index("--nnodes") + 1] == "2"
    assert projection.command[projection.command.index("--node-rank") + 1] == "1"
    assert projection.command[projection.command.index("--dist-init-addr") + 1] == (
        "VONK_MASTER_ADDR:VONK_MASTER_PORT"
    )
    assert projection.command[projection.command.index("--served-model-name") + 1] == (
        "inkling-small"
    )
    assert "--disable-prefill-cuda-graph" in projection.command
    assert "--enable-multimodal" in projection.command
    assert ("SGLANG_ENABLE_UNIFIED_RADIX_TREE", "1") in projection.environment
    assert ("NCCL_IB_GID_INDEX", "3") in projection.environment


def test_sglang_accepts_qwen38_flash_next_profile() -> None:
    recipe, distribution = _distributed_sglang_inputs()
    replacements = {
        "attention-backend": "flashinfer",
        "fp4-gemm-backend": "flashinfer_cutlass",
        "reasoning-parser": "qwen3",
        "tool-call-parser": "qwen3_coder",
    }
    for argument in recipe["runtime"]["arguments"]:
        if argument["name"] in replacements:
            argument["value"] = replacements[argument["name"]]
    recipe["runtime"]["arguments"].extend(
        [
            {"name": "mamba-ssm-dtype", "value": "bfloat16"},
            {"name": "mamba-track-interval", "value": 64},
            {"name": "chunked-prefill-size", "value": 4096},
            {"name": "max-running-requests", "value": 6},
            {"name": "max-total-tokens", "value": 600000},
            {"name": "speculative-algorithm", "value": "NEXTN"},
            {"name": "speculative-num-steps", "value": 3},
            {"name": "speculative-eagle-topk", "value": 1},
            {"name": "speculative-num-draft-tokens", "value": 4},
            {"name": "enable-linear-replayssm-spec", "value": True},
            {"name": "allow-auto-truncate", "value": True},
            {"name": "ple-offload-embedding", "value": True},
            {"name": "cuda-graph-max-bs", "value": 8},
            {"name": "disable-cuda-graph-padding", "value": True},
            {"name": "disable-radix-cache", "value": True},
            {"name": "sampling-backend", "value": "pytorch"},
            {
                "name": "default-chat-template-kwargs",
                "value": '{"enable_thinking":false}',
            },
            {"name": "enable-metrics", "value": True},
            {"name": "enable-cache-report", "value": True},
        ]
    )

    projection = SglangHarnessCompiler().compile(
        recipe,
        distribution,
        {},
        {},
        recipe["topology"],
        "entrypoint",
        0,
    )

    assert (
        projection.command[projection.command.index("--speculative-num-steps") + 1]
        == "3"
    )
    assert "--enable-linear-replayssm-spec" in projection.command
    assert "--ple-offload-embedding" in projection.command
    assert (
        projection.command[projection.command.index("--tool-call-parser") + 1]
        == "qwen3_coder"
    )


@pytest.mark.parametrize("missing", ["patch", "capability", "profile"])
def test_sglang_distributed_launch_fails_closed_without_exact_authority(
    missing: str,
) -> None:
    recipe, distribution = _distributed_sglang_inputs()
    patch: dict[str, object] | None = {}
    if missing == "patch":
        patch = None
    elif missing == "capability":
        distribution.pop("capabilities")
    else:
        distribution["capabilities"]["distributed_sglang"]["launch"][
            "rank_profiles"
        ].pop()

    with pytest.raises(HarnessCompileError, match="distributed SGLang|launch contract"):
        SglangHarnessCompiler().compile(
            recipe,
            distribution,
            patch,
            {},
            recipe["topology"],
            "worker",
            1,
        )


def test_vllm_accepts_gemma4_chat_protocol() -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["arguments"].extend(
        [
            {"name": "reasoning-parser", "value": "gemma4"},
            {"name": "tool-call-parser", "value": "gemma4"},
            {"name": "enable-auto-tool-choice", "value": True},
            {
                "name": "default-chat-template-kwargs",
                "value": '{"enable_thinking":false}',
            },
        ]
    )

    projection = _compile("vllm", recipe=recipe)

    assert projection.command[projection.command.index("--reasoning-parser") + 1] == (
        "gemma4"
    )
    assert projection.command[projection.command.index("--tool-call-parser") + 1] == (
        "gemma4"
    )
    assert "--enable-auto-tool-choice" in projection.command
    assert (
        projection.command[
            projection.command.index("--default-chat-template-kwargs") + 1
        ]
        == '{"enable_thinking":false}'
    )


def test_vllm_accepts_offline_and_nvfp4_runtime_environment() -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["environment"] = [
        {"name": "VLLM_NVFP4_GEMM_BACKEND", "value": "marlin"},
        {"name": "VLLM_USE_FLASHINFER_MOE_FP4", "value": "0"},
    ]

    projection = _compile("vllm", recipe=recipe)

    assert ("VLLM_NO_USAGE_STATS", "1") in projection.environment
    assert ("VLLM_NVFP4_GEMM_BACKEND", "marlin") in projection.environment
    assert ("VLLM_USE_FLASHINFER_MOE_FP4", "0") in projection.environment


def test_vllm_rejects_recipe_owned_optional_runtime_paths() -> None:
    recipe = _recipe("vllm")
    expected = {
        "B12X_CUTE_COMPILE_CACHE_DIR": "/outputs/cache/b12x-cute-compile",
        "DSPARK_MAX_INFLIGHT_PREFILLS": "2",
        "FLASHINFER_WORKSPACE_BASE": "/outputs/cache/flashinfer",
        "TILELANG_CACHE_DIR": "/outputs/cache/tilelang",
        "TRITON_CACHE_DIR": "/outputs/cache/triton",
        "TORCH_FR_BUFFER_SIZE": "2000",
        "TORCH_FR_DUMP_TEMP_FILE": "/outputs/cache/nccl-fr/comm_lib_trace_rank_",
        "TORCH_NCCL_DEBUG_INFO_PIPE_FILE": "/outputs/cache/nccl-fr/fr_dump_pipe_",
        "TORCH_NCCL_DUMP_ON_TIMEOUT": "1",
        "TORCH_NCCL_ENABLE_MONITORING": "1",
        "VLLM_B12X_W4A16_FORCE_BLOCKS_PER_SM": "1",
        "VLLM_B12X_W4A16_FORCE_BLOCKS_MAX_M": "32",
        "VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS": "120",
        "VLLM_USE_BREAKABLE_CUDAGRAPH": "0",
    }
    recipe["runtime"]["environment"] = [
        {"name": name, "value": value} for name, value in expected.items()
    ]
    harness = _harness_document("vllm")
    distribution = _distribution("vllm", harness)
    distribution["capabilities"] = {
        "runtime_environment": {
            "allowed_names": [
                "B12X_CUTE_COMPILE_CACHE_DIR",
                "TORCH_FR_BUFFER_SIZE",
                "TORCH_FR_DUMP_TEMP_FILE",
                "TORCH_NCCL_DEBUG_INFO_PIPE_FILE",
                "TORCH_NCCL_DUMP_ON_TIMEOUT",
                "TORCH_NCCL_ENABLE_MONITORING",
            ]
        }
    }
    recipe["runtime"]["distribution"]["content_sha256"] = catalog_content_sha256(
        distribution
    )

    with pytest.raises(HarnessCompileError, match="platform-owned"):
        _compile("vllm", recipe=recipe, distribution=distribution)


def test_vllm_injects_platform_owned_writable_cache_environment() -> None:
    projection = _compile("vllm")

    assert projection.environment == environment("vllm", ())


@pytest.mark.parametrize("slug", ["vllm", "sglang", "diffusers"])
def test_runtime_contract_is_stable_for_direct_and_source_built_images(
    slug: str,
) -> None:
    harness = _harness_document(slug)
    direct = _distribution(slug, harness)
    source_built = copy.deepcopy(direct)
    source_built["image"] = (
        f"registry.example/vonk/{slug}-source@sha256:" + "d" * 64
    )

    direct_projection = _compile(slug, distribution=direct)
    source_projection = _compile(slug, distribution=source_built)

    assert direct_projection.image != source_projection.image
    assert direct_projection.environment == source_projection.environment
    assert direct_projection.writable_paths == source_projection.writable_paths
    assert direct_projection.output_mount.target == "/outputs"


@pytest.mark.parametrize("name", ["XDG_CACHE_HOME", "VLLM_CACHE_ROOT"])
def test_vllm_rejects_recipe_override_of_platform_cache_environment(
    name: str,
) -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["environment"] = [{"name": name, "value": "/outputs/other"}]
    harness = _harness_document("vllm")
    distribution = _distribution("vllm", harness)
    distribution["capabilities"] = {"runtime_environment": {"allowed_names": [name]}}
    recipe["runtime"]["distribution"]["content_sha256"] = catalog_content_sha256(
        distribution
    )

    with pytest.raises(HarnessCompileError, match="platform-owned"):
        _compile("vllm", recipe=recipe, distribution=distribution)


@pytest.mark.parametrize("slug", BUILTIN_HARNESS_SLUGS)
def test_builtin_harness_passes_unknown_engine_flags_to_pinned_runtime(
    slug: str,
) -> None:
    recipe = _recipe(slug)
    recipe["runtime"]["arguments"].append(
        {"name": "future-engine-option", "value": "preserve-me"}
    )

    projection = _compile(slug, recipe=recipe)
    index = projection.command.index("--future-engine-option")
    assert projection.command[index + 1] == "preserve-me"


def test_builtin_harness_rejects_platform_owned_engine_flags() -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["arguments"].append({"name": "privileged", "value": True})

    with pytest.raises(HarnessCompileError, match="platform-owned"):
        _compile("vllm", recipe=recipe)


@pytest.mark.parametrize(
    ("slug", "name", "invalid"),
    [
        ("vllm", "max-model-len", 0),
        ("vllm", "block-size", 16_385),
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

    expected = effective_environment(slug, ((name, value),))
    assert projection.environment == expected


@pytest.mark.parametrize("slug", BUILTIN_HARNESS_SLUGS)
def test_builtin_harness_accepts_unknown_recipe_environment(slug: str) -> None:
    recipe = _recipe(slug)
    recipe["runtime"]["environment"] = [
        {"name": "UPSTREAM_ENGINE_TUNING", "value": "enabled"}
    ]

    projection = _compile(slug, recipe=recipe)

    expected = effective_environment(
        slug, (("UPSTREAM_ENGINE_TUNING", "enabled"),)
    )
    assert projection.environment == expected


@pytest.mark.parametrize("slug", BUILTIN_HARNESS_SLUGS)
def test_builtin_harness_preserves_unknown_engine_environment(slug: str) -> None:
    recipe = _recipe(slug)
    recipe["runtime"]["environment"] = [
        {"name": "FUTURE_ENGINE_SETTING", "value": "preserve-me"}
    ]

    projection = _compile(slug, recipe=recipe)

    assert projection.environment[0] == ("FUTURE_ENGINE_SETTING", "preserve-me")


@pytest.mark.parametrize("unsafe", ["LD_PRELOAD", "PATH", "VONK_MASTER_ADDR"])
def test_builtin_harness_rejects_unsafe_recipe_environment(
    unsafe: str,
) -> None:
    recipe = _recipe("vllm")
    recipe["runtime"]["environment"] = [{"name": unsafe, "value": "/tmp/value"}]

    with pytest.raises(HarnessCompileError, match="invalid"):
        _compile("vllm", recipe=recipe)


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
    expected_modes = ["single", "distributed"] if slug == "sglang" else ["single"]
    assert _harness_document(slug)["topology_modes"] == expected_modes
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
            {"name": "cfg-normalize", "value": "true"},
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
    assert "--cfg-normalize" in projection.command


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
