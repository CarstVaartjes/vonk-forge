"""Focused checks for the final RecipeDefinition compiler seam."""

from __future__ import annotations

import json
from copy import deepcopy
from importlib.resources import files

import pytest
import vonk_forge_contracts as contracts
from vonk_control.harnesses.canonical import (
    _scalar,
    _validate_argv_size,
)
from vonk_control.harnesses.common import structured_command
from vonk_control.recipe_runtime_specs import (
    RecipeRuntimeSpecError,
    compile_runtime_spec,
)

from .recipe_library_source import recipe_library_root


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
    recipe = _recipe("recipe-image.json", engine="vllm", entrypoint=["/opt/vonk/bin/vllm", "serve", "/models"])
    spec = compile_runtime_spec(recipe, models=[model], role="entrypoint", rank=0)

    assert spec["runtime"]["image"].endswith("@sha256:" + "d" * 64)  # type: ignore[index]
    assert spec["runtime"]["entrypoint"][-4:] == ["--host", "0.0.0.0", "--port", "8000"]  # type: ignore[index]
    assert spec["security"]["user"] == "10001:10001"  # type: ignore[index]
    assert spec["security"]["capabilities"] == []  # type: ignore[index]
    assert spec["security"]["read_only_root"] is True  # type: ignore[index]
    assert any(item["path"] == "/outputs/tmp" for item in spec["runtime"]["writable_paths"])  # type: ignore[index]


def test_source_build_requires_and_binds_exact_receipt(model: object) -> None:
    recipe = _recipe("recipe-source-build.json", engine="vllm", entrypoint=["/opt/vonk/bin/vllm", "serve", "/models"])
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


def test_runtime_compiler_rejects_retired_entity_authorities(model: object) -> None:
    recipe = _recipe(
        "recipe-image.json",
        engine="vllm",
        entrypoint=["/opt/vonk/bin/vllm", "serve", "/models"],
    )
    with pytest.raises(RecipeRuntimeSpecError, match="retired authorities"):
        compile_runtime_spec(
            recipe,
            resolved_entities={
                "execution_harness": {"kind": "execution-harness"},
            },
            models=[model],
            role="entrypoint",
            rank=0,
        )


@pytest.mark.parametrize(
    ("engine", "entrypoint", "recipe_file"),
    [
        ("vllm", ["/opt/vonk/bin/vllm", "serve", "/models"], "recipe-image.json"),
        ("sglang", ["/opt/vonk/bin/sglang-serve", "serve", "/models"], "recipe-image.json"),
        ("tensorrt-llm", ["/opt/vonk/bin/trtllm-serve", "serve", "/models"], "recipe-image.json"),
        ("llama-cpp", ["/opt/vonk/bin/llama-server", "/models"], "recipe-image.json"),
        ("ds4", ["/opt/vonk/bin/ds4-serve", "/models"], "recipe-image.json"),
        ("diffusers", ["/opt/vonk/bin/diffusers-job"], "recipe-job.json"),
        ("comfyui", ["/opt/vonk/bin/comfyui-job"], "recipe-job.json"),
        ("pytorch-pipeline", ["/opt/vonk/bin/pytorch-pipeline"], "recipe-job.json"),
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
        {"name": "future_payload", "value": "unicode Ω; $HOME"},
    ]
    raw["runtime"]["environment"] = [{"name": "FUTURE_ENGINE_FLAG", "value": "enabled"}]  # type: ignore[index]
    recipe = contracts.RecipeDefinition.model_validate(raw)
    spec = compile_runtime_spec(recipe, models=[model], role="entrypoint", rank=0)
    argv = spec["runtime"]["entrypoint"]  # type: ignore[index]
    assert argv[3:6] == ["--future_option", '{"mode": "first"}', "--future-toggle"]
    assert "unicode Ω; $HOME" in argv
    assert ("FUTURE_ENGINE_FLAG", "enabled") in {(item["name"], item["value"]) for item in spec["runtime"]["environment"]}  # type: ignore[index]

    raw["runtime"]["environment"] = [{"name": "HOME", "value": "/tmp"}]  # type: ignore[index]
    reserved = contracts.RecipeDefinition.model_validate(raw)
    with pytest.raises(RecipeRuntimeSpecError, match="platform-owned"):
        compile_runtime_spec(reserved, models=[model], role="entrypoint", rank=0)


def test_canonical_argv_preserves_empty_and_repeated_options(model: object) -> None:
    raw = _example("recipe-image.json")
    raw["runtime"]["arguments"] = [  # type: ignore[index]
        {"name": "repeated_option", "value": ""},
        {"name": "repeated_option", "value": "second"},
    ]
    recipe = contracts.RecipeDefinition.model_validate(raw)
    argv = compile_runtime_spec(recipe, models=[model], role="entrypoint", rank=0)["runtime"]["entrypoint"]
    first = argv.index("--repeated_option")
    assert argv[first : first + 4] == ["--repeated_option", "", "--repeated_option", "second"]


def test_canonical_argv_preserves_post_executable_platform_shaped_data(model: object) -> None:
    raw = _example("recipe-image.json")
    raw["runtime"]["arguments"] = [  # type: ignore[index]
        {"name": "device", "value": "/dev/nvidia0"},
        {"name": "network", "value": "host"},
        {"name": "user", "value": "10001:10001"},
        {"name": "mount", "value": ""},
        {"name": "volume", "value": {"source": "/data", "target": "/data"}},
        {"name": "device", "value": "second"},
        {"name": "option", "value": "-c"},
        {"name": "opaque", "value": "--option=-c"},
    ]
    recipe = contracts.RecipeDefinition.model_validate(raw)
    argv = compile_runtime_spec(recipe, models=[model], role="entrypoint", rank=0)[
        "runtime"
    ]["entrypoint"]
    start = argv.index("--device")
    assert argv[start : start + 16] == [
        "--device",
        "/dev/nvidia0",
        "--network",
        "host",
        "--user",
        "10001:10001",
        "--mount",
        "",
        "--volume",
        '{"source":"/data","target":"/data"}',
        "--device",
        "second",
        "--option",
        "-c",
        "--opaque",
        "--option=-c",
    ]
    assert structured_command(("/opt/vonk/bin/argv-check", "--option=-c", "-c"), canonical_argv=True)[1:] == (
        "--option=-c",
        "-c",
    )


def test_canonical_argv_json_and_utf8_bounds() -> None:
    value = {
        "outer": ["first value", {"unicode": "Ω; $HOME"}],
        "nested": {"z": 1, "a": True},
    }
    assert _scalar(value, "payload") == (
        '{"nested":{"a":true,"z":1},"outer":["first value",{"unicode":"Ω; $HOME"}]}'
    )
    assert len(_scalar({"text": "x" * 5000}, "large-json").encode("utf-8")) > 4096

    exact = "Ω" * 32768
    assert len(_scalar(exact, "exact").encode("utf-8")) == 65536
    with pytest.raises(ValueError, match="bounded"):
        _scalar(exact + "Ω", "too-large")

    _validate_argv_size(["x" * 65536] * 16)
    with pytest.raises(ValueError, match="total"):
        _validate_argv_size(["x" * 65536] * 16 + ["x"])


def test_current_recipe_corpus_compiles_every_role() -> None:
    """Compile every role from the current canonical recipe checkout."""
    root = recipe_library_root()
    recipe_files = sorted((root / "recipes").glob("*.json"))
    assert len(recipe_files) == 85
    model_documents: dict[tuple[str, str], object] = {}
    for path in (root / "models").glob("*.json"):
        item = json.loads(path.read_text(encoding="utf-8"))
        parsed = contracts.ModelDefinition.model_validate(item)
        model_documents[(parsed.identity.publisher, parsed.identity.slug)] = parsed
    assert len(model_documents) == 92
    engines: set[str] = set()
    projection_count = 0
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
                projection_count += 1
                for artifact in spec["artifacts"]:
                    assert artifact["mount"]["source"].startswith(
                        f"/run/vonk/models/{artifact['selection_id']}/{artifact['file_id']}"
                    )
    assert engines >= {
        "vllm",
        "sglang",
        "ds4",
        "diffusers",
        "comfyui",
        "pytorch-pipeline",
    }
    assert projection_count == 109


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

    recipe = _recipe("recipe-source-build.json", engine="vllm", entrypoint=["/opt/vonk/bin/vllm", "serve", "/models"])
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


def _published_recipe_context(name: str) -> tuple[object, list[object], dict[str, object]]:
    root = recipe_library_root()
    recipe = contracts.RecipeDefinition.model_validate(
        json.loads((root / "recipes" / f"{name}.json").read_text(encoding="utf-8"))
    )
    model_documents = {}
    for path in (root / "models").glob("*.json"):
        parsed = contracts.ModelDefinition.model_validate(json.loads(path.read_text(encoding="utf-8")))
        model_documents[(parsed.identity.publisher, parsed.identity.slug)] = parsed
    models = []
    for selection in recipe.models:
        models.append(model_documents[(selection.model.publisher, selection.model.slug)])
    package: dict[str, object] = {}
    if recipe.execution.mode == "build":
        digest = "a" * 64
        build = recipe.execution.build
        package = {
            "image_digest": digest,
            "image_reference": f"localhost/vonk/build@sha256:{digest}",
            "paths": [build.context.path, build.dockerfile, *(patch.path for patch in build.patches)],
        }
    serving_paths = []
    for check in recipe.validation.serving.checks:
        request = check.request
        fixture = getattr(request, "fixture", None)
        if fixture:
            serving_paths.append(fixture)
        serving_paths.extend(getattr(request, "input_slots", {}).values())
    if serving_paths:
        package["paths"] = [*package.get("paths", []), *serving_paths]
    return recipe, models, package


def test_published_distributed_sglang_preserves_authored_launch_and_rank() -> None:
    recipe, models, package = _published_recipe_context("inkling-small-nvfp4-sglang-dual")
    entrypoint = compile_runtime_spec(
        recipe, models=models, package_handle=package, role="entrypoint", rank=0
    )
    worker = compile_runtime_spec(
        recipe, models=models, package_handle=package, role="worker", rank=1
    )
    entry_argv = entrypoint["runtime"]["entrypoint"]
    worker_argv = worker["runtime"]["entrypoint"]
    assert entry_argv[:5] == [
        "/opt/vonk/bin/sglang-serve",
        "--model-path",
        "/models",
        "--served-model-name",
        "inkling-small",
    ]
    assert entry_argv[entry_argv.index("--nnodes") + 1] == "2"
    assert entry_argv[entry_argv.index("--node-rank") + 1] == "0"
    assert worker_argv[worker_argv.index("--node-rank") + 1] == "1"
    assert entry_argv[-4:] == ["--host", "0.0.0.0", "--port", "30000"]
    assert entrypoint["runtime"]["entrypoint"] != worker["runtime"]["entrypoint"]
    assert entrypoint["security"]["network_mode"] == "none"
    assert entrypoint["security"]["devices"] == ["nvidia.com/gpu=all"]
    assert {
        mount["target"] for mount in entrypoint["security"]["mounts"]
    } == {"/models", "/outputs"}


def test_sglang_wrapper_receives_root_for_target_mount() -> None:
    raw = _example("recipe-image.json")
    raw["runtime"]["engine"] = "sglang"  # type: ignore[index]
    raw["runtime"]["entrypoint"] = ["/opt/vonk/bin/sglang-serve"]  # type: ignore[index]
    raw["runtime"]["arguments"] = [  # type: ignore[index]
        {"name": "model-path", "value": "/models"},
    ]
    raw["models"][0]["files"][0]["mount"]["target"] = "/models/target"  # type: ignore[index]
    recipe = contracts.RecipeDefinition.model_validate(raw)
    model = contracts.ModelDefinition.model_validate(_example("model-definition.json"))
    spec = compile_runtime_spec(recipe, models=[model], role="entrypoint", rank=0)

    argv = spec["runtime"]["entrypoint"]
    assert argv[:4] == ["/opt/vonk/bin/sglang-serve", "--model-path", "/models", "--host"]
    assert {mount["target"] for mount in spec["security"]["mounts"]} == {
        "/models/target",
        "/outputs",
    }


def test_published_ds4_keeps_target_and_drafter_mount_roles() -> None:
    recipe, models, package = _published_recipe_context(
        "deepseek-v4-flash-0731-ds4-dspark-latency-single"
    )
    spec = compile_runtime_spec(
        recipe, models=models, package_handle=package, role="entrypoint", rank=0
    )
    mounts = spec["security"]["mounts"]
    assert {mount["target"] for mount in mounts} == {
        "/models/target",
        "/models/drafter",
        "/outputs",
    }
    argv = spec["runtime"]["entrypoint"]
    assert argv[argv.index("--model") + 1].startswith("/models/target/")
    assert argv[argv.index("--mtp-model") + 1].startswith("/models/drafter/")


def test_published_pipeline_has_output_contract() -> None:
    root = recipe_library_root()
    path = next(
        path
        for path in sorted((root / "recipes").glob("*.json"))
        if json.loads(path.read_text(encoding="utf-8"))["runtime"]["engine"]
        == "pytorch-pipeline"
    )
    recipe_name = path.stem
    recipe, models, package = _published_recipe_context(recipe_name)
    spec = compile_runtime_spec(
        recipe, models=models, package_handle=package, role="entrypoint", rank=0
    )
    argv = spec["runtime"]["entrypoint"]
    assert argv[0] == "/opt/vonk/bin/pytorch-pipeline"
    assert argv[-2:] == ["--output-dir", "/outputs"]
    assert any(item["target"] == "/outputs" for item in spec["security"]["mounts"])


@pytest.mark.parametrize("retired", ["model_projections", "package", "build_receipt"])
def test_runtime_compiler_rejects_retired_resolver_keys_even_with_current_inputs(model, retired) -> None:
    recipe = _recipe("recipe-image.json", engine="vllm", entrypoint=["/opt/vonk/bin/vllm", "serve", "/models"])
    with pytest.raises(RecipeRuntimeSpecError, match="retired authorities"):
        compile_runtime_spec(
            recipe,
            resolved_entities={"models": [model], retired: [model] if retired == "model_projections" else {}},
            role="entrypoint", rank=0,
        )


@pytest.mark.parametrize("include_paths", [False, True])
def test_runtime_compiler_rejects_retired_member_paths(model, include_paths) -> None:
    recipe = _recipe("recipe-source-build.json", engine="vllm", entrypoint=["/opt/vonk/bin/vllm", "serve", "/models"])
    package = {
        "image_reference": "localhost/vonk/recipe-build@sha256:" + "a" * 64,
        "image_digest": "a" * 64,
        "member_paths": ["context.tar", "Dockerfile"],
    }
    if include_paths:
        package["paths"] = ["context.tar", "Dockerfile"]
    with pytest.raises(RecipeRuntimeSpecError, match="retired member_paths"):
        compile_runtime_spec(recipe, models=[model], package_handle=package, role="entrypoint", rank=0)
