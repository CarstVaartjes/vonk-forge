"""Focused checks for the final RecipeDefinition compiler seam."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from importlib.resources import files
from pathlib import Path

import pytest

contracts = pytest.importorskip("vonk_forge_contracts")
from vonk_control.recipe_runtime_specs import (  # noqa: E402
    RecipeRuntimeSpecError,
    compile_runtime_spec,
)


def _example(name: str) -> dict[str, object]:
    return json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", name)
        .read_text(encoding="utf-8")
    )


@pytest.fixture(scope="module")
def model() -> object:
    return contracts.ModelDefinition.model_validate(_example("model-definition.json"))


def _recipe(name: str, *, engine: str, entrypoint: list[str]) -> object:
    raw = _example(name)
    raw["runtime"]["engine"] = engine  # type: ignore[index]
    raw["runtime"]["entrypoint"] = entrypoint  # type: ignore[index]
    return contracts.RecipeDefinition.model_validate(raw)


def test_final_image_recipe_compiles_with_platform_defaults(model: object) -> None:
    recipe = _recipe("recipe-image.json", engine="vllm", entrypoint=["vllm", "serve", "/models"])
    spec = compile_runtime_spec(recipe, models=[model], role="entrypoint", rank=0)

    assert spec["runtime"]["image"].endswith("@sha256:" + "d" * 64)  # type: ignore[index]
    assert spec["runtime"]["entrypoint"][-4:] == ["--host", "0.0.0.0", "--port", "8000"]  # type: ignore[index]
    assert spec["security"]["user"] == "10001:10001"  # type: ignore[index]
    assert spec["security"]["capabilities"] == []  # type: ignore[index]
    assert spec["security"]["read_only_root"] is True  # type: ignore[index]
    assert any(item["path"] == "/outputs/tmp" for item in spec["runtime"]["writable_paths"])  # type: ignore[index]


def test_source_build_requires_and_binds_exact_receipt(model: object) -> None:
    recipe = _recipe("recipe-source-build.json", engine="vllm", entrypoint=["vllm", "serve", "/models"])
    digest = "a" * 64
    spec = compile_runtime_spec(
        recipe,
        models=[model],
        package_handle={
            "image_reference": f"localhost/vonk/recipe-build@sha256:{digest}",
            "image_digest": digest,
            "paths": ["context.tar", "Dockerfile"],
        },
        role="entrypoint",
        rank=0,
    )
    assert spec["runtime"]["image"] == f"localhost/vonk/recipe-build@sha256:{digest}"  # type: ignore[index]


@pytest.mark.parametrize(
    ("engine", "entrypoint", "recipe_file"),
    [
        ("vllm", ["vllm", "serve", "/models"], "recipe-image.json"),
        ("sglang", ["sglang", "serve", "/models"], "recipe-image.json"),
        ("tensorrt-llm", ["trtllm-serve", "serve", "/models"], "recipe-image.json"),
        ("llama-cpp", ["llama-server", "/models"], "recipe-image.json"),
        ("ds4", ["ds4-serve", "/models"], "recipe-image.json"),
        ("diffusers", ["diffusers-job"], "recipe-job.json"),
        ("comfyui", ["comfyui-job"], "recipe-job.json"),
        ("pytorch-pipeline", ["pytorch-pipeline"], "recipe-job.json"),
    ],
)
def test_all_builtin_harnesses_compile_final_examples(
    model: object, engine: str, entrypoint: list[str], recipe_file: str
) -> None:
    recipe = _recipe(recipe_file, engine=engine, entrypoint=entrypoint)
    spec = compile_runtime_spec(recipe, models=[model], role="entrypoint", rank=0)
    assert spec["runtime"]["adapter"] == engine  # type: ignore[index]
    assert spec["runtime"]["entrypoint"][0].startswith("/")  # type: ignore[index]


def test_unknown_engine_values_preserve_order_and_reserved_paths_fail(model: object) -> None:
    raw = _example("recipe-image.json")
    raw["runtime"]["arguments"] = [  # type: ignore[index]
        {"name": "future_option", "value": '{"mode": "first"}'},
        {"name": "future-toggle", "value": True},
    ]
    raw["runtime"]["environment"] = [{"name": "FUTURE_ENGINE_FLAG", "value": "enabled"}]  # type: ignore[index]
    recipe = contracts.RecipeDefinition.model_validate(raw)
    spec = compile_runtime_spec(recipe, models=[model], role="entrypoint", rank=0)
    argv = spec["runtime"]["entrypoint"]  # type: ignore[index]
    assert argv[3:6] == ["--future_option", '{"mode": "first"}', "--future-toggle"]
    assert ("FUTURE_ENGINE_FLAG", "enabled") in {(item["name"], item["value"]) for item in spec["runtime"]["environment"]}  # type: ignore[index]

    raw["runtime"]["environment"] = [{"name": "HOME", "value": "/tmp"}]  # type: ignore[index]
    reserved = contracts.RecipeDefinition.model_validate(raw)
    with pytest.raises(RecipeRuntimeSpecError, match="platform-owned"):
        compile_runtime_spec(reserved, models=[model], role="entrypoint", rank=0)


def test_final_recipe_corpus_all_roles_if_published_checkout_is_configured() -> None:
    """Run against the producer's published 84 recipe checkout when supplied."""
    root_value = os.environ.get("VONK_FINAL_RECIPE_ROOT")
    if not root_value:
        pytest.skip("set VONK_FINAL_RECIPE_ROOT to the published recipe checkout")
    root = Path(root_value)
    recipe_files = sorted((root / "recipes").glob("*.json"))
    assert len(recipe_files) == 84
    model_documents: dict[tuple[str, str], object] = {}
    for path in (root / "models").glob("*.json"):
        item = json.loads(path.read_text(encoding="utf-8"))
        try:
            model_documents[(item["identity"]["publisher"], item["identity"]["slug"])] = contracts.ModelDefinition.model_validate(item)
        except Exception:
            # The published corpus can carry informational model metadata that
            # is not part of the exact ModelDefinition snapshot selected by a
            # recipe.  Resolver failures below remain hard failures.
            continue
    engines: set[str] = set()
    for path in recipe_files:
        recipe = contracts.RecipeDefinition.model_validate(json.loads(path.read_text(encoding="utf-8")))
        engines.add(recipe.runtime.engine)
        models = [model_documents[(selection.model.publisher, selection.model.slug)] for selection in recipe.models]
        package: dict[str, object] = {}
        paths: list[str] = []
        if recipe.execution.mode == "build":
            build = recipe.execution.build
            paths.extend([build.context.path, build.dockerfile, *(patch.path for patch in build.patches)])
            digest = "a" * 64
            package.update({"image_digest": digest, "image_reference": f"localhost/vonk/build@sha256:{digest}"})
        for check in recipe.validation.serving.checks:
            request = check.request
            fixture = getattr(request, "fixture", None)
            if fixture:
                paths.append(fixture)
            paths.extend(getattr(request, "input_slots", {}).values())
        if paths:
            package["paths"] = paths
        for index, role in enumerate(recipe.topology.roles):
            first_rank = sum(item.count for item in recipe.topology.roles[:index])
            for rank in range(first_rank, first_rank + role.count):
                spec = compile_runtime_spec(
                    recipe,
                    models=models,
                    package_handle=package or None,
                    role=role.name,
                    rank=rank,
                )
                for artifact in spec["artifacts"]:
                    assert artifact["mount"]["source"].startswith(
                        f"/run/vonk/models/{artifact['selection_id']}/{artifact['file_id']}"
                    )
    assert engines >= {"vllm", "sglang", "ds4", "diffusers", "comfyui", "pytorch-pipeline"}


def test_execution_digest_ignores_notes_but_tracks_bound_launch_changes(model: object) -> None:
    base = _example("recipe-image.json")
    first = contracts.RecipeDefinition.model_validate(base)

    notes = deepcopy(base)
    notes["metadata"]["description"] += " Editorial note."
    noted = contracts.RecipeDefinition.model_validate(notes)
    first_spec = compile_runtime_spec(first, models=[model], role="entrypoint", rank=0)
    noted_spec = compile_runtime_spec(noted, models=[model], role="entrypoint", rank=0)
    assert first_spec["identity"]["execution_sha256"] == noted_spec["identity"]["execution_sha256"]
    assert first_spec["identity"]["recipe_revision_sha256"] != noted_spec["identity"]["recipe_revision_sha256"]

    def digest(raw: dict[str, object]) -> str:
        return compile_runtime_spec(
            contracts.RecipeDefinition.model_validate(raw),
            models=[model],
            role="entrypoint",
            rank=0,
        )["identity"]["execution_sha256"]

    bound = deepcopy(base)
    bound["runtime"]["arguments"] = [{"name": "context_tokens", "setting": "context_tokens"}]
    bound_a = digest(bound)
    bound["settings"]["context_tokens"]["value"] = 2048
    assert bound_a != digest(bound)

    argv = deepcopy(base)
    argv["runtime"]["arguments"] = [{"name": "future_option", "value": "one"}]
    argv_a = digest(argv)
    argv["runtime"]["arguments"][0]["value"] = "two"
    assert argv_a != digest(argv)

    mount = deepcopy(base)
    mount["models"][0]["files"][0]["mount"]["target"] = "/models/target"
    mount["runtime"]["entrypoint"][2] = "/models/target"
    assert first_spec["identity"]["execution_sha256"] != digest(mount)

    topology = deepcopy(base)
    topology["topology"]["name"] = "different-placement"
    assert first_spec["identity"]["execution_sha256"] != digest(topology)

    interface = deepcopy(base)
    interface["interfaces"][0]["port"] = 9000
    assert first_spec["identity"]["execution_sha256"] != digest(interface)


def test_security_is_in_execution_projection_and_build_input_is_separate(model: object) -> None:
    from vonk_control.recipe_runtime_specs import _execution_digest

    common = {"runtime": {"image": "image@sha256:" + "a" * 64}, "security": {"user": "10001:10001"}}
    changed = deepcopy(common)
    changed["security"]["user"] = "10002:10002"
    assert _execution_digest(common) != _execution_digest(changed)

    recipe = _recipe("recipe-source-build.json", engine="vllm", entrypoint=["vllm", "serve", "/models"])
    digest = "a" * 64
    package = {
        "image_digest": digest,
        "image_reference": f"localhost/vonk/build@sha256:{digest}",
        "paths": ["context.tar", "Dockerfile"],
        "build_input_sha256": "b" * 64,
    }
    first = compile_runtime_spec(recipe, models=[model], package_handle=package, role="entrypoint", rank=0)
    package["build_input_sha256"] = "c" * 64
    second = compile_runtime_spec(recipe, models=[model], package_handle=package, role="entrypoint", rank=0)
    assert first["identity"]["execution_sha256"] == second["identity"]["execution_sha256"]
    assert first["identity"]["build_input_sha256"] != second["identity"]["build_input_sha256"]
