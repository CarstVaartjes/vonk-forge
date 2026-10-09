from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError
from vonk_control.recipe_runtime_specs import (
    RecipeRuntimeSpecError,
    compile_runtime_spec,
)
from vonk_forge_contracts import (
    ModelDefinition,
    RecipeDefinition,
    document_sha256,
    read_model,
    read_recipe,
)

from .canonical_recipe_fixtures import canonical_example


def _example(name: str) -> dict[str, object]:
    return canonical_example(name)


def _json_object(value: object) -> dict[str, object]:
    """Narrow decoded JSON to a mutable object; a wrong shape fails the test."""

    assert isinstance(value, dict)
    return value


def _json_array(value: object) -> list[object]:
    """Narrow decoded JSON to a mutable array; a wrong shape fails the test."""

    assert isinstance(value, list)
    return value


@pytest.fixture(scope="module")
def model() -> ModelDefinition:
    return read_model(_example("model-definition.json"))


def _models(model: ModelDefinition) -> dict[str, ModelDefinition]:
    return {document_sha256(_example("model-definition.json")): model}


def _recipe(name: str = "recipe-source-build.json") -> RecipeDefinition:
    return read_recipe(_example(name))


def _built_image(digest: str = "a" * 64) -> dict[str, object]:
    return {
        "image_reference": f"localhost/vonk/build@sha256:{digest}",
        "image_digest": digest,
        "paths": ["context.tar", "Dockerfile"],
    }


def _compile(
    raw: dict[str, object],
    model: ModelDefinition,
    package_handle: dict[str, object] | None = None,
) -> dict[str, object]:
    return compile_runtime_spec(
        read_recipe(raw),
        models=_models(model),
        recipe_digest=document_sha256(raw),
        package_handle=package_handle,
        role="entrypoint",
        rank=0,
    ).document()


def test_recipe_uses_the_canonical_model_and_topology_bindings(
    model: ModelDefinition,
) -> None:
    recipe = _recipe()

    assert recipe.kind == "recipe"
    assert recipe.topology.node_count == 1
    assert recipe.models[0].model.kind == "model"
    assert recipe.models[0].model.content_sha256 == document_sha256(
        _example("model-definition.json")
    )
    assert recipe.interfaces[0].adapter == "openai"


def test_canonical_recipe_compiles_a_shell_free_read_only_projection(
    model: ModelDefinition,
) -> None:
    spec = _compile(_example("recipe-source-build.json"), model, _built_image())
    runtime = _json_object(spec["runtime"])
    security = _json_object(spec["security"])
    command = _json_array(runtime["entrypoint"])

    assert command[0] == "/opt/vonk/bin/vllm"
    assert "-c" not in command
    assert security["user"] == "10001:10001"
    assert runtime["writable_paths"]


def test_canonical_recipe_preserves_unknown_engine_arguments(
    model: ModelDefinition,
) -> None:
    raw = _example("recipe-source-build.json")
    _json_object(raw["runtime"])["arguments"] = [
        {"name": "future_option", "value": '{"mode":"first"}'},
        {"name": "future_toggle", "value": True},
        {"name": "future_payload", "value": "unicode Ω; $HOME"},
    ]
    argv = _json_array(
        _json_object(_compile(raw, model, _built_image())["runtime"])["entrypoint"]
    )

    assert argv[3:6] == ["--future_option", '{"mode":"first"}', "--future_toggle"]
    assert "unicode Ω; $HOME" in argv


def test_canonical_recipe_rejects_unsafe_entrypoints(model: ModelDefinition) -> None:
    raw = _example("recipe-source-build.json")
    _json_object(raw["runtime"])["entrypoint"] = ["bash", "-c", "vllm serve /models"]

    with pytest.raises((ValidationError, RecipeRuntimeSpecError)):
        _compile(raw, model)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("settings", "context_tokens", "value"), 0),
        (("topology", "parallelism", "tensor"), 2),
    ],
)
def test_canonical_recipe_rejects_invalid_cross_field_values(
    path: tuple[str, ...],
    value: object,
) -> None:
    raw = _example("recipe-source-build.json")
    target: object = raw
    for part in path[:-1]:
        assert isinstance(target, dict)
        target = target[part]
    assert isinstance(target, dict)
    target[path[-1]] = value

    with pytest.raises(ValidationError):
        RecipeDefinition.model_validate(raw)


def test_canonical_job_recipe_declares_a_read_only_input_contract(
    model: ModelDefinition,
) -> None:
    raw = _example("recipe-job.json")
    runtime = _json_object(raw["runtime"])
    runtime["engine"] = "diffusers"
    runtime["entrypoint"] = ["diffusers-job"]
    _json_object(_json_array(raw["interfaces"])[0])["input"] = {
        "required": True,
        "media_types": ["image/png"],
        "max_bytes": 1024,
    }
    handle = _built_image()
    handle["paths"] = [*_json_array(handle["paths"]), "blank"]
    spec = _compile(raw, model, handle)

    assert _json_array(_json_object(spec["security"])["mounts"])[-1] == {
        "source": "/run/vonk/inputs",
        "target": "/inputs",
    }


def test_canonical_job_recipe_retains_distributed_topology_dimensions() -> None:
    raw = _example("recipe-job.json")
    topology = _json_object(raw["topology"])
    role = copy.deepcopy(_json_object(_json_array(topology["roles"])[0]))
    worker = copy.deepcopy(role)
    worker.update({"name": "worker", "endpoint_owner": False})
    topology.update(
        {
            "node_count": 2,
            "roles": [role, worker],
            "parallelism": {
                "tensor": 2,
                "pipeline": 1,
                "data": 1,
                "backend": "native",
            },
            "start_order": ["entrypoint", "worker"],
        }
    )

    recipe = RecipeDefinition.model_validate(raw)
    assert recipe.topology.node_count == 2
    assert recipe.topology.world_size == 2
    assert recipe.topology.distributed


def test_source_build_requires_an_exact_image_receipt(
    model: ModelDefinition,
) -> None:
    raw = _example("recipe-source-build.json")

    accepted = []
    with pytest.raises(RecipeRuntimeSpecError):
        accepted.append(_compile(raw, model))
    assert not accepted

    digest = "a" * 64
    spec = _compile(raw, model, _built_image(digest))
    assert (
        _json_object(spec["runtime"])["image"]
        == f"localhost/vonk/build@sha256:{digest}"
    )


def test_canonical_recipe_rejects_unknown_root_fields() -> None:
    raw = _example("recipe-source-build.json")
    raw["unexpected_root"] = []

    with pytest.raises(ValidationError):
        RecipeDefinition.model_validate(raw)
