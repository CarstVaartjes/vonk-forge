"""Focused checks for the final RecipeDefinition compiler seam."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from importlib.resources import files
from pathlib import Path

import pytest
import vonk_forge_contracts as contracts
from vonk_control.bounded_json import (
    BoundedJSONError,
    require_mapping,
    require_sequence,
    text,
)
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


def _sequence(value: object, detail: str) -> Sequence[object]:
    """Return one compiled JSON array from the runtime projection."""

    return require_sequence(value, detail)


def _mappings(value: object, detail: str) -> Sequence[Mapping[str, object]]:
    """Return one compiled JSON array of JSON objects."""

    result: list[Mapping[str, object]] = []
    for item in _sequence(value, detail):
        mapping = item
        if not isinstance(mapping, Mapping):
            raise BoundedJSONError(f"{detail} is not an array of objects")
        result.append(mapping)
    return result


def _text(value: object, detail: str) -> str:
    """Return one compiled JSON string, or fail like the schema it describes."""

    result = text(value)
    if result is None:
        raise BoundedJSONError(f"{detail} is not a string")
    return result


def _runtime(spec: dict[str, object]) -> Mapping[str, object]:
    """Return the compiled ``runtime`` object of a runtime spec."""

    return require_mapping(spec["runtime"], "runtime projection is not an object")


def _raw_engine(path: Path) -> str:
    """Return the engine of an unvalidated recipe document on disk."""

    document = require_mapping(
        json.loads(path.read_text(encoding="utf-8")), "recipe document is not an object"
    )
    runtime = require_mapping(document["runtime"], "recipe runtime is not an object")
    engine = text(runtime["engine"])
    if engine is None:
        raise BoundedJSONError("recipe engine is not a string")
    return engine


def _security(spec: dict[str, object]) -> Mapping[str, object]:
    """Return the compiled ``security`` object of a runtime spec."""

    return require_mapping(spec["security"], "security projection is not an object")


def _identity(spec: dict[str, object]) -> Mapping[str, object]:
    """Return the compiled ``identity`` object of a runtime spec."""

    return require_mapping(spec["identity"], "identity projection is not an object")


def _argv(spec: dict[str, object]) -> Sequence[object]:
    """Return the compiled container argv of a runtime spec."""

    return _sequence(_runtime(spec)["entrypoint"], "runtime argv is not an array")


@pytest.fixture(scope="module")
def model() -> contracts.ModelDefinition:
    return contracts.ModelDefinition.model_validate(_example("model-definition.json"))


def _raw_object(value: object, detail: str) -> dict[str, object]:
    """Return a mutable JSON object from a raw example fixture."""

    assert isinstance(value, dict), detail
    return value


def _raw_mappings(value: object, detail: str) -> list[dict[str, object]]:
    """Return the mutable JSON objects of a raw example array."""

    assert isinstance(value, list), detail
    return [_raw_object(item, detail) for item in value]


def _raw_sequence(value: object, detail: str) -> list[object]:
    """Return a mutable JSON array from a raw example fixture."""

    assert isinstance(value, list), detail
    return value


def _raw_runtime(raw: dict[str, object]) -> dict[str, object]:
    """Return the mutable example ``runtime`` object of a raw recipe fixture."""

    return _raw_object(raw["runtime"], "example runtime is not an object")


def _recipe(name: str, *, engine: str, entrypoint: list[str]) -> contracts.RecipeDefinition:
    raw = _example(name)
    runtime = _raw_runtime(raw)
    runtime["engine"] = engine
    runtime["entrypoint"] = entrypoint
    return contracts.RecipeDefinition.model_validate(raw)


def test_final_image_recipe_compiles_with_platform_defaults(model: contracts.ModelDefinition) -> None:
    recipe = _recipe("recipe-image.json", engine="vllm", entrypoint=["/opt/vonk/bin/vllm", "serve", "/models"])
    spec = compile_runtime_spec(recipe, models=[model], role="entrypoint", rank=0)

    runtime = _runtime(spec)
    security = _security(spec)
    assert _text(runtime["image"], "runtime image").endswith("@sha256:" + "d" * 64)
    assert _argv(spec)[-4:] == ["--host", "0.0.0.0", "--port", "8000"]
    assert security["user"] == "10001:10001"
    assert security["capabilities"] == []
    assert security["read_only_root"] is True
    assert any(
        item["path"] == "/outputs/tmp"
        for item in _mappings(runtime["writable_paths"], "writable paths")
    )


def test_source_build_requires_and_binds_exact_receipt(model: contracts.ModelDefinition) -> None:
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
    assert _runtime(spec)["image"] == f"localhost/vonk/recipe-build@sha256:{digest}"


def test_runtime_compiler_rejects_retired_entity_authorities(model: contracts.ModelDefinition) -> None:
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
    model: contracts.ModelDefinition, engine: str, entrypoint: list[str], recipe_file: str
) -> None:
    recipe = _recipe(recipe_file, engine=engine, entrypoint=entrypoint)
    spec = compile_runtime_spec(recipe, models=[model], role="entrypoint", rank=0)
    assert _runtime(spec)["adapter"] == engine
    assert _text(_argv(spec)[0], "first runtime argument").startswith("/")


def test_unknown_engine_values_preserve_order_and_reserved_paths_fail(model: contracts.ModelDefinition) -> None:
    raw = _example("recipe-image.json")
    runtime = _raw_runtime(raw)
    runtime["arguments"] = [
        {"name": "future_option", "value": '{"mode": "first"}'},
        {"name": "future-toggle", "value": True},
        {"name": "future_payload", "value": "unicode Ω; $HOME"},
    ]
    runtime["environment"] = [{"name": "FUTURE_ENGINE_FLAG", "value": "enabled"}]
    recipe = contracts.RecipeDefinition.model_validate(raw)
    spec = compile_runtime_spec(recipe, models=[model], role="entrypoint", rank=0)
    argv = _argv(spec)
    assert argv[3:6] == ["--future_option", '{"mode": "first"}', "--future-toggle"]
    assert "unicode Ω; $HOME" in argv
    assert ("FUTURE_ENGINE_FLAG", "enabled") in {
        (item["name"], item["value"])
        for item in _mappings(_runtime(spec)["environment"], "runtime environment")
    }

    runtime["environment"] = [{"name": "HOME", "value": "/tmp"}]
    reserved = contracts.RecipeDefinition.model_validate(raw)
    with pytest.raises(RecipeRuntimeSpecError, match="platform-owned"):
        compile_runtime_spec(reserved, models=[model], role="entrypoint", rank=0)


def test_canonical_argv_preserves_empty_and_repeated_options(model: contracts.ModelDefinition) -> None:
    raw = _example("recipe-image.json")
    _raw_runtime(raw)["arguments"] = [
        {"name": "repeated_option", "value": ""},
        {"name": "repeated_option", "value": "second"},
    ]
    recipe = contracts.RecipeDefinition.model_validate(raw)
    argv = _argv(compile_runtime_spec(recipe, models=[model], role="entrypoint", rank=0))
    first = argv.index("--repeated_option")
    assert argv[first : first + 4] == ["--repeated_option", "", "--repeated_option", "second"]


def test_canonical_argv_preserves_post_executable_platform_shaped_data(model: contracts.ModelDefinition) -> None:
    raw = _example("recipe-image.json")
    _raw_runtime(raw)["arguments"] = [
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
    argv = _argv(compile_runtime_spec(recipe, models=[model], role="entrypoint", rank=0))
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
    model_documents: dict[tuple[str, str], contracts.ModelDefinition] = {}
    model_files = list((root / "models").glob("*.json"))
    for path in model_files:
        item = json.loads(path.read_text(encoding="utf-8"))
        parsed = contracts.ModelDefinition.model_validate(item)
        model_documents[(parsed.identity.publisher, parsed.identity.slug)] = parsed
    assert model_documents and len(model_documents) == len(model_files)
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
                artifacts = _mappings(spec["artifacts"], "runtime artifacts")
                for artifact in artifacts:
                    assert _text(
                        require_mapping(artifact["mount"], "artifact mount is not an object")[
                            "source"
                        ],
                        "artifact mount source",
                    ).startswith(
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


def test_execution_digest_ignores_notes_but_tracks_bound_launch_changes(model: contracts.ModelDefinition) -> None:
    base = _example("recipe-image.json")
    first = contracts.RecipeDefinition.model_validate(base)

    notes = deepcopy(base)
    metadata = _raw_object(notes["metadata"], "example metadata is not an object")
    description = _text(metadata["description"], "example description")
    metadata["description"] = description + " Editorial note."
    noted = contracts.RecipeDefinition.model_validate(notes)
    first_spec = compile_runtime_spec(first, models=[model], role="entrypoint", rank=0)
    noted_spec = compile_runtime_spec(noted, models=[model], role="entrypoint", rank=0)
    first_identity = _identity(first_spec)
    noted_identity = _identity(noted_spec)
    assert first_identity["execution_sha256"] == noted_identity["execution_sha256"]
    assert first_identity["recipe_revision_sha256"] != noted_identity["recipe_revision_sha256"]

    def digest(raw: dict[str, object]) -> str:
        spec = compile_runtime_spec(
            contracts.RecipeDefinition.model_validate(raw),
            models=[model],
            role="entrypoint",
            rank=0,
        )
        return _text(_identity(spec)["execution_sha256"], "execution digest")

    bound = deepcopy(base)
    _raw_runtime(bound)["arguments"] = [
        {"name": "context_tokens", "setting": "context_tokens"}
    ]
    bound_a = digest(bound)
    settings = _raw_object(bound["settings"], "example settings are not an object")
    context_tokens = _raw_object(
        settings["context_tokens"], "example context_tokens is not an object"
    )
    context_tokens["value"] = 2048
    assert bound_a != digest(bound)

    argv = deepcopy(base)
    _raw_runtime(argv)["arguments"] = [{"name": "future_option", "value": "one"}]
    argv_a = digest(argv)
    arguments = _raw_mappings(
        _raw_runtime(argv)["arguments"], "example arguments are not an array of objects"
    )
    arguments[0]["value"] = "two"
    assert argv_a != digest(argv)

    mount = deepcopy(base)
    models = _raw_mappings(mount["models"], "example models are not an array of objects")
    files = _raw_mappings(models[0]["files"], "example files are not an array of objects")
    file_mount = _raw_object(files[0]["mount"], "example mount is not an object")
    file_mount["target"] = "/models/target"
    entrypoint = _raw_sequence(
        _raw_runtime(mount)["entrypoint"], "example entrypoint is not an array"
    )
    entrypoint[2] = "/models/target"
    assert first_identity["execution_sha256"] != digest(mount)

    topology = deepcopy(base)
    topology_body = _raw_object(topology["topology"], "example topology is not an object")
    topology_body["name"] = "different-placement"
    assert first_identity["execution_sha256"] != digest(topology)

    interface = deepcopy(base)
    interfaces = _raw_mappings(
        interface["interfaces"], "example interfaces are not an array of objects"
    )
    interfaces[0]["port"] = 9000
    assert first_identity["execution_sha256"] != digest(interface)


def test_security_is_in_execution_projection_and_build_input_is_separate(model: contracts.ModelDefinition) -> None:
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
    first_identity = _identity(first)
    second_identity = _identity(second)
    assert first_identity["execution_sha256"] == second_identity["execution_sha256"]
    assert first_identity["build_input_sha256"] != second_identity["build_input_sha256"]


def _published_recipe_context(
    name: str,
) -> tuple[contracts.RecipeDefinition, list[contracts.ModelDefinition], dict[str, object]]:
    root = recipe_library_root()
    recipe = contracts.RecipeDefinition.model_validate(
        json.loads((root / "recipes" / f"{name}.json").read_text(encoding="utf-8"))
    )
    model_documents: dict[tuple[str, str], contracts.ModelDefinition] = {}
    for path in (root / "models").glob("*.json"):
        parsed = contracts.ModelDefinition.model_validate(json.loads(path.read_text(encoding="utf-8")))
        model_documents[(parsed.identity.publisher, parsed.identity.slug)] = parsed
    models: list[contracts.ModelDefinition] = []
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
    serving_paths: list[str] = []
    for check in recipe.validation.serving.checks:
        request = check.request
        fixture = getattr(request, "fixture", None)
        if fixture:
            serving_paths.append(fixture)
        serving_paths.extend(getattr(request, "input_slots", {}).values())
    if serving_paths:
        declared_paths = package.get("paths", [])
        assert isinstance(declared_paths, list)
        package["paths"] = [*declared_paths, *serving_paths]
    return recipe, models, package


def test_published_distributed_sglang_preserves_authored_launch_and_rank() -> None:
    recipe, models, package = _published_recipe_context("inkling-small-nvfp4-sglang-dual")
    entrypoint = compile_runtime_spec(
        recipe, models=models, package_handle=package, role="entrypoint", rank=0
    )
    worker = compile_runtime_spec(
        recipe, models=models, package_handle=package, role="worker", rank=1
    )
    entry_argv = _argv(entrypoint)
    worker_argv = _argv(worker)
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
    assert _argv(entrypoint) != _argv(worker)
    assert _security(entrypoint)["network_mode"] == "none"
    assert _security(entrypoint)["devices"] == ["nvidia.com/gpu=all"]
    assert {
        mount["target"]
        for mount in _mappings(_security(entrypoint)["mounts"], "security mounts")
    } == {"/models", "/outputs"}


def test_sglang_wrapper_receives_root_for_target_mount() -> None:
    raw = _example("recipe-image.json")
    runtime = _raw_runtime(raw)
    runtime["engine"] = "sglang"
    runtime["entrypoint"] = ["/opt/vonk/bin/sglang-serve"]
    runtime["arguments"] = [
        {"name": "model-path", "value": "/models"},
    ]
    models = _raw_mappings(raw["models"], "example models are not an array of objects")
    files = _raw_mappings(models[0]["files"], "example files are not an array of objects")
    file_mount = _raw_object(files[0]["mount"], "example mount is not an object")
    file_mount["target"] = "/models/target"
    recipe = contracts.RecipeDefinition.model_validate(raw)
    model = contracts.ModelDefinition.model_validate(_example("model-definition.json"))
    spec = compile_runtime_spec(recipe, models=[model], role="entrypoint", rank=0)

    argv = _argv(spec)
    assert argv[:4] == ["/opt/vonk/bin/sglang-serve", "--model-path", "/models", "--host"]
    assert {
        mount["target"]
        for mount in _mappings(_security(spec)["mounts"], "security mounts")
    } == {
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
    mounts = _mappings(_security(spec)["mounts"], "security mounts")
    assert {mount["target"] for mount in mounts} == {
        "/models/target",
        "/models/drafter",
        "/outputs",
    }
    argv = _argv(spec)
    assert _text(argv[argv.index("--model") + 1], "model argument").startswith("/models/target/")
    assert _text(argv[argv.index("--mtp-model") + 1], "mtp model argument").startswith("/models/drafter/")


def test_published_pipeline_has_output_contract() -> None:
    root = recipe_library_root()
    path = next(
        path
        for path in sorted((root / "recipes").glob("*.json"))
        if _raw_engine(path) == "pytorch-pipeline"
    )
    recipe_name = path.stem
    recipe, models, package = _published_recipe_context(recipe_name)
    spec = compile_runtime_spec(
        recipe, models=models, package_handle=package, role="entrypoint", rank=0
    )
    argv = _argv(spec)
    assert argv[0] == "/opt/vonk/bin/pytorch-pipeline"
    assert argv[-2:] == ["--output-dir", "/outputs"]
    assert any(
        item["target"] == "/outputs"
        for item in _mappings(_security(spec)["mounts"], "security mounts")
    )


@pytest.mark.parametrize("retired", ["model_projections", "package", "build_receipt"])
def test_runtime_compiler_rejects_retired_resolver_keys_even_with_current_inputs(
    model: contracts.ModelDefinition, retired: str
) -> None:
    recipe = _recipe("recipe-image.json", engine="vllm", entrypoint=["/opt/vonk/bin/vllm", "serve", "/models"])
    with pytest.raises(RecipeRuntimeSpecError, match="retired authorities"):
        compile_runtime_spec(
            recipe,
            resolved_entities={"models": [model], retired: [model] if retired == "model_projections" else {}},
            role="entrypoint", rank=0,
        )


@pytest.mark.parametrize("include_paths", [False, True])
def test_runtime_compiler_rejects_retired_member_paths(
    model: contracts.ModelDefinition, include_paths: bool
) -> None:
    recipe = _recipe("recipe-source-build.json", engine="vllm", entrypoint=["/opt/vonk/bin/vllm", "serve", "/models"])
    package: dict[str, object] = {
        "image_reference": "localhost/vonk/recipe-build@sha256:" + "a" * 64,
        "image_digest": "a" * 64,
        "member_paths": ["context.tar", "Dockerfile"],
    }
    if include_paths:
        package["paths"] = ["context.tar", "Dockerfile"]
    with pytest.raises(RecipeRuntimeSpecError, match="retired member_paths"):
        compile_runtime_spec(recipe, models=[model], package_handle=package, role="entrypoint", rank=0)
