from __future__ import annotations

import copy
import json
from importlib.resources import files

import pytest
from pydantic import ValidationError
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


@pytest.fixture(scope="module")
def model() -> ModelDefinition:
    return ModelDefinition.model_validate(_example("model-definition.json"))


def _recipe(name: str = "recipe-image.json") -> RecipeDefinition:
    return RecipeDefinition.model_validate(_example(name))


def _compile(recipe: RecipeDefinition, model: ModelDefinition) -> dict[str, object]:
    return compile_runtime_spec(recipe, models=[model], role="entrypoint", rank=0)


def test_recipe_uses_the_canonical_model_and_topology_bindings(
    model: ModelDefinition,
) -> None:
    recipe = _recipe()

    assert recipe.schema_version == 2
    assert recipe.kind == "recipe"
    assert recipe.topology.node_count == 1
    assert recipe.models[0].model.kind == "model"
    assert recipe.models[0].model.content_sha256 == content_sha256(model)
    assert recipe.interfaces[0].adapter == "openai"


def test_canonical_recipe_compiles_a_shell_free_read_only_projection(
    model: ModelDefinition,
) -> None:
    spec = _compile(_recipe(), model)
    command = spec["runtime"]["entrypoint"]

    assert command[0] == "/opt/vonk/bin/vllm"
    assert "-c" not in command
    assert spec["security"]["user"] == "10001:10001"
    assert spec["security"]["capabilities"] == []
    assert spec["security"]["read_only_root"] is True
    assert spec["runtime"]["writable_paths"]
    assert spec["security"]["mounts"][0]["read_only"] is True


def test_canonical_recipe_preserves_unknown_engine_arguments(
    model: ModelDefinition,
) -> None:
    raw = _example("recipe-image.json")
    raw["runtime"]["arguments"] = [
        {"name": "future_option", "value": '{"mode":"first"}'},
        {"name": "future_toggle", "value": True},
        {"name": "future_payload", "value": "unicode Ω; $HOME"},
    ]
    recipe = RecipeDefinition.model_validate(raw)
    argv = _compile(recipe, model)["runtime"]["entrypoint"]

    assert argv[3:6] == ["--future_option", '{"mode":"first"}', "--future_toggle"]
    assert "unicode Ω; $HOME" in argv


def test_canonical_recipe_rejects_unsafe_entrypoints(model: ModelDefinition) -> None:
    raw = _example("recipe-image.json")
    raw["runtime"]["entrypoint"] = ["bash", "-c", "vllm serve /models"]

    with pytest.raises((ValidationError, RecipeRuntimeSpecError)):
        _compile(RecipeDefinition.model_validate(raw), model)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("settings", "context_tokens", "value"), 0),
        (("topology", "parallelism", "tensor"), 2),
        (("topology", "fabric", "connectivity"), "connected"),
    ],
)
def test_canonical_recipe_rejects_invalid_cross_field_values(
    path: tuple[str, ...], value: object,
) -> None:
    raw = _example("recipe-image.json")
    target: object = raw
    for part in path[:-1]:
        target = target[part]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]

    with pytest.raises(ValidationError):
        RecipeDefinition.model_validate(raw)


def test_canonical_job_recipe_declares_a_read_only_input_contract(
    model: ModelDefinition,
) -> None:
    raw = _example("recipe-job.json")
    raw["runtime"]["engine"] = "diffusers"
    raw["runtime"]["entrypoint"] = ["diffusers-job"]
    raw["interfaces"][0]["input"] = {
        "path": "/inputs",
        "required": True,
        "media_types": ["image/png"],
        "max_bytes": 1024,
    }
    raw["validation"]["serving"]["checks"][0]["request"]["input_path"] = "/inputs"
    recipe = RecipeDefinition.model_validate(raw)
    spec = _compile(recipe, model)

    assert spec["security"]["mounts"][-1] == {
        "source": "/run/vonk/inputs",
        "target": "/inputs",
        "read_only": True,
    }


def test_canonical_job_recipe_retains_distributed_topology_dimensions() -> None:
    raw = _example("recipe-job.json")
    role = copy.deepcopy(raw["topology"]["roles"][0])
    worker = copy.deepcopy(role)
    worker.update({"name": "worker", "endpoint_owner": False})
    raw["topology"].update(
        {
            "mode": "distributed",
            "node_count": 2,
            "roles": [role, worker],
            "parallelism": {
                "world_size": 2,
                "tensor": 2,
                "pipeline": 1,
                "data": 1,
                "backend": "native",
            },
            "fabric": {"connectivity": "connected", "minimum_bandwidth_mbps": 1},
            "start_order": ["entrypoint", "worker"],
            "stop_order": ["entrypoint", "worker"],
        }
    )

    recipe = RecipeDefinition.model_validate(raw)
    assert recipe.topology.node_count == 2
    assert recipe.topology.parallelism.world_size == 2


def test_source_build_requires_an_exact_image_receipt(
    model: ModelDefinition,
) -> None:
    recipe = _recipe("recipe-source-build.json")

    with pytest.raises(RecipeRuntimeSpecError, match="receipt"):
        _compile(recipe, model)

    digest = "a" * 64
    spec = compile_runtime_spec(
        recipe,
        models=[model],
        package_handle={
            "image_reference": f"localhost/vonk/build@sha256:{digest}",
            "image_digest": digest,
            "paths": ["context.tar", "Dockerfile"],
        },
        role="entrypoint",
        rank=0,
    )
    assert spec["runtime"]["image"] == f"localhost/vonk/build@sha256:{digest}"


def test_canonical_recipe_rejects_unknown_root_fields() -> None:
    raw = _example("recipe-image.json")
    raw["unexpected_root"] = []

    with pytest.raises(ValidationError):
        RecipeDefinition.model_validate(raw)
