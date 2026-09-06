from __future__ import annotations

import copy
import json
from importlib.resources import files
from pathlib import Path

import pytest
from pydantic import ValidationError
from vonk_control.harnesses.canonical import compile_canonical_harness
from vonk_control.harnesses.common import HarnessCompileError
from vonk_control.recipe_runtime_specs import (
    RecipeRuntimeSpecError,
    compile_runtime_spec,
)
from vonk_control.runtime_writable_paths import (
    effective_environment,
    environment,
    telemetry_contract,
    writable_paths,
)
from vonk_forge_contracts import ModelDefinition, RecipeDefinition

BUILTINS = (
    "vllm",
    "sglang",
    "tensorrt-llm",
    "llama-cpp",
    "ds4",
    "diffusers",
    "comfyui",
    "pytorch-pipeline",
)
HARNESS_ROOT = Path(__file__).resolve().parents[2] / "config/execution-harnesses"

OPENAI_BUILTINS = {"vllm", "sglang", "tensorrt-llm", "llama-cpp", "ds4"}
ENTRYPOINTS = {
    "vllm": ["vllm", "serve", "/models"],
    "sglang": ["sglang", "serve", "/models"],
    "tensorrt-llm": ["trtllm-serve", "serve", "/models"],
    "llama-cpp": ["llama-server", "/models"],
    "ds4": ["ds4-serve", "/models"],
    "diffusers": ["diffusers-job"],
    "comfyui": ["comfyui-job"],
    "pytorch-pipeline": ["pytorch-pipeline"],
}
ARGS = {
    "vllm": [
        {"name": "max-model-len", "value": 32768},
        {"name": "tensor-parallel-size", "value": 1},
    ],
    "sglang": [
        {"name": "model-path", "value": "/models"},
        {"name": "context-length", "value": 32768},
        {"name": "tensor-parallel-size", "value": 1},
    ],
    "tensorrt-llm": [
        {"name": "backend", "value": "pytorch"},
        {"name": "max-batch-size", "value": 8},
        {"name": "max-num-tokens", "value": 4096},
        {"name": "max-seq-len", "value": 32768},
        {"name": "tp-size", "value": 1},
        {"name": "pp-size", "value": 1},
        {"name": "ep-size", "value": 1},
    ],
    "llama-cpp": [
        {"name": "model", "value": "/models/model.gguf"},
        {"name": "ctx-size", "value": 32768},
        {"name": "n-gpu-layers", "value": 999},
    ],
    "ds4": [
        {"name": "model", "value": "/models/target.gguf"},
        {"name": "draft-model", "value": "/models/drafter.gguf"},
        {"name": "ctx-size", "value": 32768},
    ],
    "diffusers": [
        {"name": "pipeline", "value": "text-to-image"},
        {"name": "output-mime", "value": "image/png"},
    ],
    "comfyui": [
        {"name": "workflow", "value": "/opt/vonk/source/workflows/image.json"},
        {"name": "workflow-sha256", "value": "e" * 64},
        {"name": "output-mime", "value": "image/png"},
    ],
    "pytorch-pipeline": [
        {"name": "entrypoint", "value": "/opt/vonk/source/pipelines/run.py"},
        {"name": "output-mime", "value": "model/gltf-binary"},
    ],
}


def _example(name: str) -> dict[str, object]:
    return json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", name)
        .read_text(encoding="utf-8")
    )


def test_config_contains_exactly_the_canonical_builtin_harness_assets() -> None:
    paths = sorted(HARNESS_ROOT.glob("*.json"))

    assert {path.stem for path in paths} == set(BUILTINS)
    for path in paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["kind"] == "execution-harness"
        assert document["compiler_slug"] == path.stem


@pytest.fixture(scope="module")
def model() -> ModelDefinition:
    return ModelDefinition.model_validate(_example("model-definition.json"))


def _recipe(slug: str) -> RecipeDefinition:
    raw = _example("recipe-image.json" if slug in OPENAI_BUILTINS else "recipe-job.json")
    raw["runtime"]["engine"] = slug
    raw["runtime"]["entrypoint"] = copy.deepcopy(ENTRYPOINTS[slug])
    raw["runtime"]["arguments"] = copy.deepcopy(ARGS[slug])
    if slug in OPENAI_BUILTINS:
        raw["interfaces"] = [
            {
                "adapter": "openai",
                "port": 8000,
                "model_aliases": ["synthetic-tiny"],
                "health_path": "/v1/models",
            }
        ]
        raw["validation"]["serving"]["interface"] = "openai"
    else:
        adapter = "mesh-job" if slug == "pytorch-pipeline" else "image-job"
        raw["interfaces"][0]["adapter"] = adapter
        raw["validation"]["serving"]["interface"] = adapter
        raw["validation"]["serving"]["checks"][0]["kind"] = f"{adapter}.output"
    return RecipeDefinition.model_validate(raw)


def _projection(
    slug: str,
    *,
    recipe: RecipeDefinition | None = None,
    model: ModelDefinition,
    package_handle: object = None,
    settings: dict[str, object] | None = None,
    role: str = "entrypoint",
    rank: int = 0,
):
    selected = recipe or _recipe(slug)
    compile_runtime_spec(
        selected,
        models=[model],
        package_handle=package_handle,
        parameters=settings,
        role=role,
        rank=rank,
    )
    projection, _artifacts, _digest = compile_canonical_harness(
        selected,
        (model,),
        package_handle,
        role=role,
        rank=rank,
        settings=settings,
    )
    return projection


@pytest.mark.parametrize("slug", BUILTINS)
def test_builtin_harness_compiles_shell_free_secure_projection(
    slug: str, model: ModelDefinition
) -> None:
    projection = _projection(slug, model=model)

    assert projection.command
    assert projection.command[0].startswith("/opt/vonk/bin/") or projection.command[0].startswith("/usr/local/bin/")
    assert not any(value in {"sh", "bash", "/bin/sh", "/bin/bash", "-c"} for value in projection.command)
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


@pytest.mark.parametrize("slug", BUILTINS)
def test_builtin_harness_compiles_the_declared_model_and_output_mounts(
    slug: str, model: ModelDefinition
) -> None:
    projection = _projection(slug, model=model)

    assert projection.model_mounts
    assert projection.model_mounts[0].target == "/models"
    assert projection.output_mount.source == "/run/vonk/outputs"
    assert projection.output_mount.target == "/outputs"
    assert projection.output_mount.read_only is False
    assert projection.output_mount.isolated is True


def test_vllm_preserves_opaque_engine_options(model: ModelDefinition) -> None:
    raw = _recipe("vllm").model_dump(mode="json")
    raw["runtime"]["arguments"].extend(
        [
            {"name": "future-engine-option", "value": "preserve-me"},
            {"name": "structured-option", "value": '{"enabled":true}'},
        ]
    )
    projection = _projection("vllm", recipe=RecipeDefinition.model_validate(raw), model=model)

    assert "--future-engine-option" in projection.command
    index = projection.command.index("--future-engine-option")
    assert projection.command[index + 1] == "preserve-me"
    assert "--structured-option" in projection.command


@pytest.mark.parametrize("slug", BUILTINS)
def test_builtin_harness_preserves_unknown_environment(slug: str, model: ModelDefinition) -> None:
    raw = _recipe(slug).model_dump(mode="json")
    raw["runtime"]["environment"] = [{"name": "FUTURE_ENGINE_SETTING", "value": "preserve-me"}]
    projection = _projection(slug, recipe=RecipeDefinition.model_validate(raw), model=model)

    assert ("FUTURE_ENGINE_SETTING", "preserve-me") in projection.environment
    assert projection.environment == effective_environment(slug, (("FUTURE_ENGINE_SETTING", "preserve-me"),))


@pytest.mark.parametrize("unsafe", ["LD_PRELOAD", "PATH"])
def test_builtin_harness_rejects_unsafe_environment(
    unsafe: str, model: ModelDefinition
) -> None:
    raw = _recipe("vllm").model_dump(mode="json")
    raw["runtime"]["environment"] = [{"name": unsafe, "value": "/tmp/value"}]

    with pytest.raises((ValidationError, HarnessCompileError, RecipeRuntimeSpecError)):
        _projection("vllm", recipe=RecipeDefinition.model_validate(raw), model=model)


def test_vllm_injects_platform_owned_environment(model: ModelDefinition) -> None:
    projection = _projection("vllm", model=model)

    assert projection.environment == environment("vllm", ())
    assert ("XDG_CACHE_HOME", "/outputs/cache") in projection.environment
    assert ("VLLM_CACHE_ROOT", "/outputs/cache/vllm") in projection.environment


@pytest.mark.parametrize("slug", BUILTINS)
def test_builtin_harness_rejects_shell_entrypoint(slug: str, model: ModelDefinition) -> None:
    raw = _recipe(slug).model_dump(mode="json")
    raw["runtime"]["entrypoint"] = ["bash", "-c", "run"]

    with pytest.raises((ValidationError, HarnessCompileError, RecipeRuntimeSpecError)):
        _projection(slug, recipe=RecipeDefinition.model_validate(raw), model=model)


def test_artifact_harness_projects_an_isolated_read_only_input(
    model: ModelDefinition,
) -> None:
    raw = _recipe("diffusers").model_dump(mode="json")
    raw["interfaces"][0]["input"] = {
        "path": "/inputs",
        "required": True,
        "media_types": ["image/png"],
        "max_bytes": 32 * 1024 * 1024,
    }
    raw["validation"]["serving"]["checks"][0]["request"]["input_path"] = "/inputs"
    recipe = RecipeDefinition.model_validate(raw)
    projection = _projection("diffusers", recipe=recipe, model=model)

    assert projection.input_mount is not None
    assert projection.input_mount.source == "/run/vonk/inputs"
    assert projection.input_mount.target == "/inputs"
    assert projection.input_mount.read_only is True
    assert projection.input_mount.isolated is True


def test_job_media_contract_is_bound_to_the_canonical_interface(
    model: ModelDefinition,
) -> None:
    raw = _recipe("diffusers").model_dump(mode="json")
    raw["interfaces"][0]["output"]["slots"][0]["media_types"] = ["image/png"]
    raw["runtime"]["arguments"].append({"name": "output-mime", "value": "image/png"})
    recipe = RecipeDefinition.model_validate(raw)
    projection = _projection("diffusers", recipe=recipe, model=model)

    assert "--output-mime" in projection.command
    assert projection.command[projection.command.index("--output-mime") + 1] == "image/png"


def test_parameter_substitution_uses_declared_typed_bounds(model: ModelDefinition) -> None:
    raw = _recipe("vllm").model_dump(mode="json")
    raw["runtime"]["arguments"] = [
        {"name": "max-model-len", "setting": "max_model_len"}
    ]
    raw["settings"]["knobs"]["max_model_len"] = {
        "value": 32768,
        "change_effect": "restart",
    }
    recipe = RecipeDefinition.model_validate(raw)

    projection = _projection(
        "vllm", recipe=recipe, model=model, settings={"max_model_len": 65536}
    )
    index = projection.command.index("--max-model-len")
    assert projection.command[index + 1] == "65536"

    with pytest.raises((HarnessCompileError, RecipeRuntimeSpecError)):
        _projection("vllm", recipe=recipe, model=model, settings={"unknown": 0})


def test_source_build_requires_and_binds_exact_receipt(model: ModelDefinition) -> None:
    recipe = RecipeDefinition.model_validate(_example("recipe-source-build.json"))

    with pytest.raises(RecipeRuntimeSpecError, match="receipt"):
        _projection("vllm", recipe=recipe, model=model)

    digest = "a" * 64
    projection = _projection(
        "vllm",
        recipe=recipe,
        model=model,
        package_handle={
            "image_reference": f"localhost/vonk/build@sha256:{digest}",
            "image_digest": digest,
            "paths": ["context.tar", "Dockerfile"],
        },
    )
    assert projection.image == f"localhost/vonk/build@sha256:{digest}"


def test_current_compiler_rejects_missing_source_bundle_members(model: ModelDefinition) -> None:
    recipe = RecipeDefinition.model_validate(_example("recipe-source-build.json"))
    digest = "a" * 64

    with pytest.raises(RecipeRuntimeSpecError, match="package|path"):
        _projection(
            "vllm",
            recipe=recipe,
            model=model,
            package_handle={
                "image_reference": f"localhost/vonk/build@sha256:{digest}",
                "image_digest": digest,
                "paths": ["context.tar"],
            },
        )


def test_distributed_sglang_compiles_rank_specific_launch(model: ModelDefinition) -> None:
    raw = _recipe("sglang").model_dump(mode="json")
    endpoint = copy.deepcopy(raw["topology"]["roles"][0])
    worker = copy.deepcopy(endpoint)
    worker.update({"name": "worker", "endpoint_owner": False})
    raw["topology"].update(
        {
            "mode": "distributed",
            "node_count": 2,
            "roles": [endpoint, worker],
            "parallelism": {"world_size": 2, "tensor": 2, "pipeline": 1, "data": 1, "backend": "native"},
            "fabric": {"connectivity": "connected", "minimum_bandwidth_mbps": 1},
            "start_order": ["entrypoint", "worker"],
            "stop_order": ["entrypoint", "worker"],
        }
    )
    raw["runtime"]["arguments"][2]["value"] = 2
    raw["models"][0]["files"][0]["roles"] = ["entrypoint", "worker"]
    recipe = RecipeDefinition.model_validate(raw)
    projection = _projection("sglang", recipe=recipe, model=model, role="worker", rank=1)

    assert "--nnodes" in projection.command
    assert projection.command[projection.command.index("--nnodes") + 1] == "2"
    assert projection.command[projection.command.index("--node-rank") + 1] == "1"
