"""Focused checks for the canonical RecipeDefinition compiler seam."""

from __future__ import annotations

import copy
import json
from importlib.resources import files

import pytest
from vonk_control.recipe_runtime_specs import (
    RecipeRuntimeSpecError,
    compile_runtime_spec,
)
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256


def _example(name: str) -> dict[str, object]:
    return json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", name)
        .read_text(encoding="utf-8")
    )


def _model() -> ModelDefinition:
    return ModelDefinition.model_validate(_example("model-definition.json"))


def _recipe(
    name: str = "recipe-image.json",
    *,
    engine: str = "vllm",
    entrypoint: list[str] | None = None,
) -> RecipeDefinition:
    raw = _example(name)
    raw["runtime"]["engine"] = engine  # type: ignore[index]
    raw["runtime"]["entrypoint"] = entrypoint or ["vllm", "serve", "/models"]  # type: ignore[index]
    return RecipeDefinition.model_validate(raw)


def _source_package(digest: str = "d" * 64) -> dict[str, object]:
    return {
        "image_reference": f"localhost/vonk/recipe-build@sha256:{digest}",
        "image_digest": digest,
        "paths": ["context.tar", "Dockerfile"],
    }


def _distributed_sglang_recipe() -> RecipeDefinition:
    raw = _example("recipe-image.json")
    raw["runtime"]["engine"] = "sglang"  # type: ignore[index]
    raw["runtime"]["entrypoint"] = ["/opt/vonk/bin/sglang-serve"]  # type: ignore[index]
    raw["runtime"]["arguments"] = [{"name": "model-path", "value": "/models"}]  # type: ignore[index]
    raw["models"][0]["files"][0]["roles"] = ["entrypoint", "worker"]  # type: ignore[index]
    endpoint_role = copy.deepcopy(raw["topology"]["roles"][0])  # type: ignore[index]
    worker_role = copy.deepcopy(endpoint_role)
    worker_role.update({"name": "worker", "endpoint_owner": False})
    raw["topology"].update(  # type: ignore[index]
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
    return RecipeDefinition.model_validate(raw)


def _multi_artifact_inputs() -> tuple[RecipeDefinition, ModelDefinition]:
    raw = _example("recipe-image.json")
    model_raw = _example("model-definition.json")
    draft = copy.deepcopy(model_raw["files"][0])  # type: ignore[index]
    draft.update({"id": "draft", "path": "draft.safetensors", "sha256": "b" * 64})
    model_raw["files"].append(draft)  # type: ignore[index]
    model = ModelDefinition.model_validate(model_raw)
    raw["models"][0]["model"]["content_sha256"] = content_sha256(model)  # type: ignore[index]
    raw["models"][0]["files"][0]["mount"]["target"] = "/models/target"  # type: ignore[index]
    raw["models"][0]["files"].append(  # type: ignore[index]
        {
            "id": "draft",
            "file_id": "draft",
            "roles": ["entrypoint"],
            "mount": {"target": "/models/draft", "read_only": True},
        }
    )
    raw["runtime"]["entrypoint"] = ["vllm", "serve", "/models/target"]  # type: ignore[index]
    raw["runtime"]["arguments"].append(  # type: ignore[index]
        {
            "name": "speculative-config",
            "value": '{"method":"draft_model","model":"/models/draft"}',
        }
    )
    return RecipeDefinition.model_validate(raw), model


def test_runtime_spec_preserves_digest_bound_snapshot_selection() -> None:
    recipe = _recipe("recipe-source-build.json")
    model = _model()
    digest = "d" * 64
    spec = compile_runtime_spec(
        recipe,
        models=[model],
        package_handle=_source_package(digest),
        role="entrypoint",
        rank=0,
    )

    artifact = spec["artifacts"][0]
    assert spec["runtime"]["image"] == f"localhost/vonk/recipe-build@sha256:{digest}"
    assert artifact["path"] == "model.safetensors"
    assert artifact["model"]["content_sha256"] == content_sha256(model)
    assert artifact["mount"] == {
        "source": "/run/vonk/models/primary/weights",
        "target": "/models",
        "read_only": True,
    }


def test_runtime_spec_writable_paths_ignore_recipe_metadata_identity() -> None:
    raw = _example("recipe-image.json")
    raw["identity"] = {"publisher": "anemll", "slug": "anemll-vllm-mia"}  # type: ignore[index]
    recipe = RecipeDefinition.model_validate(raw)
    spec = compile_runtime_spec(recipe, models=[_model()], role="entrypoint", rank=0)
    values = {item["name"]: item["value"] for item in spec["runtime"]["environment"]}

    assert values["VLLM_CACHE_ROOT"] == "/outputs/cache/vllm"
    assert values["XDG_CACHE_HOME"] == "/outputs/cache"
    assert values["TMPDIR"] == "/outputs/tmp"
    assert any(item["path"] == "/outputs/cache/vllm" for item in spec["runtime"]["writable_paths"])


def test_runtime_spec_is_compiled_from_the_trusted_builtin_projection() -> None:
    spec = compile_runtime_spec(_recipe(), models=[_model()], role="entrypoint", rank=0)

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
    assert spec["runtime"]["image"].endswith("@sha256:" + "d" * 64)
    assert spec["runtime"]["entrypoint"][-4:] == ["--host", "0.0.0.0", "--port", "8000"]
    assert spec["runtime"]["environment"][1:4] == [
        {"name": "XDG_CACHE_HOME", "value": "/outputs/cache", "secret": None},
        {"name": "XDG_CONFIG_HOME", "value": "/outputs/cache/config", "secret": None},
        {"name": "TMPDIR", "value": "/outputs/tmp", "secret": None},
    ]
    assert spec["security"] == {
        "devices": ["nvidia.com/gpu=all"],
        "user": "10001:10001",
        "capabilities": [],
        "privileged": False,
        "host_network": False,
        "network_mode": "none",
        "mounts": [
            {"source": "/run/vonk/models/primary", "target": "/models", "read_only": True},
            {"source": "/run/vonk/outputs", "target": "/outputs", "read_only": False},
        ],
        "read_only_root": True,
        "no_new_privileges": True,
    }
    assert spec["endpoint"] == {
        "protocol": "openai",
        "port": 8000,
        "model_aliases": ["synthetic-tiny"],
        "health_path": "/v1/models",
    }


def test_runtime_spec_projects_deepseek_r1_parser_into_agent_argv() -> None:
    raw = _example("recipe-image.json")
    raw["runtime"]["arguments"].append(  # type: ignore[index]
        {"name": "reasoning-parser", "value": "deepseek_r1"}
    )
    recipe = RecipeDefinition.model_validate(raw)
    command = compile_runtime_spec(recipe, models=[_model()], role="entrypoint", rank=0)[
        "runtime"
    ]["entrypoint"]

    parser = command.index("--reasoning-parser")
    assert command[parser + 1] == "deepseek_r1"


def test_runtime_spec_projects_distributed_sglang_placement_authority() -> None:
    recipe = _distributed_sglang_recipe()
    entrypoint = compile_runtime_spec(recipe, models=[_model()], role="entrypoint", rank=0)
    worker = compile_runtime_spec(recipe, models=[_model()], role="worker", rank=1)

    assert entrypoint["runtime"]["placement_environment"] == {
        "local_address": "VONK_LOCAL_ADDR",
        "master_address": "VONK_MASTER_ADDR",
        "master_port": "VONK_MASTER_PORT",
    }
    entry_command = entrypoint["runtime"]["entrypoint"]
    worker_command = worker["runtime"]["entrypoint"]
    assert entry_command[entry_command.index("--node-rank") + 1] == "0"
    assert worker_command[worker_command.index("--node-rank") + 1] == "1"
    assert entry_command[entry_command.index("--dist-init-addr") + 1] == "VONK_MASTER_ADDR:VONK_MASTER_PORT"
    assert entrypoint["topology"] == {
        "name": "dual-sglang",
        "mode": "distributed",
        "node_count": 2,
        "world_size": 2,
        "rank": 0,
        "role": "entrypoint",
        "backend": "native",
    }
    assert entrypoint["security"]["mounts"][-1] == {
        "source": "/run/vonk/outputs",
        "target": "/outputs",
        "read_only": False,
    }


def test_runtime_spec_compiles_one_shot_artifact_job_authority() -> None:
    recipe = _recipe("recipe-job.json", engine="diffusers", entrypoint=["diffusers-job"])
    spec = compile_runtime_spec(recipe, models=[_model()], role="entrypoint", rank=0)

    assert "endpoint" not in spec
    assert spec["job"] == {
        "interface": "image-job",
        "input": None,
        "output_path": "/outputs",
        "timeout_seconds": 30,
    }
    assert spec["security"]["mounts"] == [
        {"source": "/run/vonk/models/primary", "target": "/models", "read_only": True},
        {"source": "/run/vonk/outputs", "target": "/outputs", "read_only": False},
    ]
    assert spec["runtime"]["entrypoint"][-2:] == ["--output-dir", "/outputs"]


def test_runtime_spec_binds_exact_auxiliary_model_versions() -> None:
    package = {"artifact_inputs": [{"selection_id": "primary", "artifact_key": "weights"}]}
    model = _model()
    spec = compile_runtime_spec(
        _recipe(), models=[model], package_handle=package, role="entrypoint", rank=0
    )

    assert spec["model_dependencies"] == [
        {
            "selection_id": "primary",
            "publisher": "vonk-forge",
            "slug": "synthetic-tiny-fp16",
            "content_sha256": content_sha256(model),
            "artifact_key": "weights",
        }
    ]


def test_runtime_spec_preserves_exact_multi_artifact_targets_and_vllm_primary() -> None:
    recipe, model = _multi_artifact_inputs()
    spec = compile_runtime_spec(recipe, models=[model], role="entrypoint", rank=0)

    command = spec["runtime"]["entrypoint"]
    assert command[2] == "/models/target"
    assert command[command.index("--speculative-config") + 1] == '{"method":"draft_model","model":"/models/draft"}'
    assert [artifact["mount"]["target"] for artifact in spec["artifacts"]] == [
        "/models/target",
        "/models/draft",
    ]
    assert spec["security"]["mounts"][0] == {
        "source": "/run/vonk/models/primary",
        "target": "/models/target",
        "read_only": True,
    }


def test_runtime_spec_rejects_recipe_authored_shell_authority() -> None:
    raw = _example("recipe-image.json")
    raw["runtime"]["entrypoint"] = ["/bin/sh", "-c", "touch /tmp/owned"]  # type: ignore[index]
    recipe = RecipeDefinition.model_validate(raw)

    with pytest.raises(RecipeRuntimeSpecError, match="entrypoint"):
        compile_runtime_spec(recipe, models=[_model()], role="entrypoint", rank=0)


def test_runtime_spec_rejects_a_role_that_does_not_bind_the_exact_rank() -> None:
    with pytest.raises(RecipeRuntimeSpecError, match="role"):
        compile_runtime_spec(_recipe(), models=[_model()], role="worker", rank=0)
