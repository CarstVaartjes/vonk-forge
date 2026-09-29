"""Focused checks for the final RecipeDefinition compiler seam."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import vonk_forge_contracts as contracts
from vonk_control.bounded_json import (
    BoundedJSONError,
    require_mapping,
    require_sequence,
    text,
)
from vonk_control.harnesses.canonical import (
    _MAX_ARGV_BYTES,
    _scalar,
    _validate_argv_size,
)
from vonk_control.harnesses.common import structured_command
from vonk_control.recipe_runtime_specs import (
    RecipeRuntimeSpecError,
    compile_runtime_spec,
)

from .recipe_library_source import recipe_library_root

_BUILT_IMAGE: dict[str, object] = {
    "image_reference": "localhost/vonk/build@sha256:" + "d" * 64,
    "image_digest": "d" * 64,
    "paths": ["context.tar", "Dockerfile", "blank"],
}


def _compile(
    recipe: dict[str, object],
    models: dict[str, Any] | list[dict[str, Any]],
    *,
    package_handle: object = _BUILT_IMAGE,
    role: str = "entrypoint",
    rank: int = 0,
    resolved_entities: dict[str, object] | None = None,
    parameters: dict[str, object] | None = None,
) -> dict[str, object]:
    """Compile published raw documents the way the Controller reads them."""

    documents = models if isinstance(models, list) else [models]
    parsed = {
        contracts.document_sha256(item): contracts.read_model(item)
        for item in documents
    }
    return compile_runtime_spec(
        contracts.read_recipe(recipe),
        resolved_entities,
        models=parsed,
        recipe_digest=contracts.document_sha256(recipe),
        package_handle=package_handle,
        parameters=parameters,
        role=role,
        rank=rank,
    )


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
def model() -> dict[str, object]:
    return _example("model-definition.json")


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


def _recipe(name: str, *, engine: str, entrypoint: list[str]) -> dict[str, object]:
    raw = _example(name)
    runtime = _raw_runtime(raw)
    runtime["engine"] = engine
    runtime["entrypoint"] = entrypoint
    return raw


def test_final_recipe_compiles_with_platform_defaults(
    model: dict[str, object],
) -> None:
    recipe = _recipe(
        "recipe-source-build.json",
        engine="vllm",
        entrypoint=["/opt/vonk/bin/vllm", "serve", "/models"],
    )
    spec = _compile(recipe, model, role="entrypoint", rank=0)

    runtime = _runtime(spec)
    security = _security(spec)
    assert _text(runtime["image"], "runtime image").endswith("@sha256:" + "d" * 64)
    assert _argv(spec)[-4:] == ["--host", "0.0.0.0", "--port", "8000"]
    assert security["user"] == "10001:10001"
    assert any(
        item["path"] == "/outputs/tmp"
        for item in _mappings(runtime["writable_paths"], "writable paths")
    )


def test_source_build_requires_and_binds_exact_receipt(
    model: dict[str, object],
) -> None:
    recipe = _recipe(
        "recipe-source-build.json",
        engine="vllm",
        entrypoint=["/opt/vonk/bin/vllm", "serve", "/models"],
    )
    digest = "a" * 64
    spec = _compile(
        recipe,
        model,
        package_handle={
            "image_reference": f"localhost/vonk/recipe-build@sha256:{digest}",
            "image_digest": digest,
            "paths": ["context.tar", "Dockerfile"],
        },
    )
    assert _runtime(spec)["image"] == f"localhost/vonk/recipe-build@sha256:{digest}"


def test_runtime_compiler_rejects_retired_entity_authorities(
    model: dict[str, object],
) -> None:
    recipe = _recipe(
        "recipe-source-build.json",
        engine="vllm",
        entrypoint=["/opt/vonk/bin/vllm", "serve", "/models"],
    )
    with pytest.raises(RecipeRuntimeSpecError, match="retired authorities"):
        _compile(
            recipe,
            model,
            resolved_entities={"execution_harness": {"kind": "execution-harness"}},
        )


@pytest.mark.parametrize(
    ("engine", "entrypoint", "recipe_file"),
    [
        (
            "vllm",
            ["/opt/vonk/bin/vllm", "serve", "/models"],
            "recipe-source-build.json",
        ),
        (
            "sglang",
            ["/opt/vonk/bin/sglang-serve", "serve", "/models"],
            "recipe-source-build.json",
        ),
        (
            "tensorrt-llm",
            ["/opt/vonk/bin/trtllm-serve", "serve", "/models"],
            "recipe-source-build.json",
        ),
        (
            "llama-cpp",
            ["/opt/vonk/bin/llama-server", "/models"],
            "recipe-source-build.json",
        ),
        ("ds4", ["/opt/vonk/bin/ds4-serve", "/models"], "recipe-source-build.json"),
        (
            "tensorfold",
            ["/opt/vonk/bin/tensorfold-serve"],
            "recipe-source-build.json",
        ),
        ("diffusers", ["/opt/vonk/bin/diffusers-job"], "recipe-job.json"),
        ("comfyui", ["/opt/vonk/bin/comfyui-job"], "recipe-job.json"),
        ("pytorch-pipeline", ["/opt/vonk/bin/pytorch-pipeline"], "recipe-job.json"),
    ],
)
def test_all_builtin_harnesses_compile_final_examples(
    model: dict[str, object],
    engine: str,
    entrypoint: list[str],
    recipe_file: str,
) -> None:
    recipe = _recipe(recipe_file, engine=engine, entrypoint=entrypoint)
    spec = _compile(recipe, model, role="entrypoint", rank=0)
    assert _runtime(spec)["adapter"] == engine
    assert _text(_argv(spec)[0], "first runtime argument").startswith("/")


def test_unknown_engine_values_preserve_order_and_reserved_paths_fail(
    model: dict[str, object],
) -> None:
    raw = _example("recipe-source-build.json")
    runtime = _raw_runtime(raw)
    runtime["arguments"] = [
        {"name": "future_option", "value": '{"mode": "first"}'},
        {"name": "future-toggle", "value": True},
        {"name": "future_payload", "value": "unicode Ω; $HOME"},
    ]
    runtime["environment"] = [{"name": "FUTURE_ENGINE_FLAG", "value": "enabled"}]
    recipe = raw
    spec = _compile(recipe, model, role="entrypoint", rank=0)
    argv = _argv(spec)
    assert argv[3:6] == ["--future_option", '{"mode": "first"}', "--future-toggle"]
    assert "unicode Ω; $HOME" in argv
    assert ("FUTURE_ENGINE_FLAG", "enabled") in {
        (item["name"], item["value"])
        for item in _mappings(_runtime(spec)["environment"], "runtime environment")
    }

    runtime["environment"] = [{"name": "HOME", "value": "/tmp"}]
    reserved = raw
    with pytest.raises(RecipeRuntimeSpecError, match="platform-owned"):
        _compile(reserved, model, role="entrypoint", rank=0)


def test_declared_runtime_requirement_resolves_to_platform_owned_paths(
    model: dict[str, object],
) -> None:
    raw = _example("recipe-source-build.json")
    runtime = _raw_runtime(raw)
    runtime["engine"] = "vllm"
    runtime["entrypoint"] = ["/opt/vonk/bin/vllm", "serve", "/models"]
    runtime["environment"] = [
        {"name": "VONK_RUNTIME_REQUIREMENTS", "value": "tilelang-cache"}
    ]
    recipe = raw

    spec = _compile(recipe, model, role="entrypoint", rank=0)

    projection = _runtime(spec)
    paths = {
        (item["name"], item["path"], item["persistent"])
        for item in _mappings(projection["writable_paths"], "writable paths")
    }
    assert ("tilelang", "/outputs/cache/tilelang", True) in paths
    assert ("tilelang-tmp", "/outputs/tmp/tilelang", False) in paths
    environment = {
        item["name"]: item["value"]
        for item in _mappings(projection["environment"], "runtime environment")
    }
    assert environment["TILELANG_CACHE_DIR"] == "/outputs/cache/tilelang"
    assert environment["TILELANG_TMP_DIR"] == "/outputs/tmp/tilelang"
    # The platform consumes the declaration; it never reaches the engine.
    assert "VONK_RUNTIME_REQUIREMENTS" not in environment


def test_recipe_cannot_restate_or_move_a_platform_owned_path(
    model: dict[str, object],
) -> None:
    raw = _example("recipe-source-build.json")
    runtime = _raw_runtime(raw)
    runtime["engine"] = "vllm"
    runtime["entrypoint"] = ["/opt/vonk/bin/vllm", "serve", "/models"]
    runtime["environment"] = [{"name": "TILELANG_CACHE_DIR", "value": "/tmp/moved"}]
    recipe = raw

    with pytest.raises(RecipeRuntimeSpecError, match="platform-owned"):
        _compile(recipe, model, role="entrypoint", rank=0)


@pytest.mark.parametrize(
    ("engine", "entrypoint", "requirement", "detail"),
    [
        (
            "vllm",
            ["/opt/vonk/bin/vllm", "serve", "/models"],
            "not-a-requirement",
            "unavailable",
        ),
        (
            "llama-cpp",
            ["/opt/vonk/bin/llama-server"],
            "tilelang-cache",
            "not supported",
        ),
    ],
)
def test_unknown_or_unsupported_runtime_requirement_is_rejected(
    model: dict[str, object],
    engine: str,
    entrypoint: list[str],
    requirement: str,
    detail: str,
) -> None:
    raw = _example("recipe-source-build.json")
    runtime = _raw_runtime(raw)
    runtime["engine"] = engine
    runtime["entrypoint"] = entrypoint
    runtime["environment"] = [
        {"name": "VONK_RUNTIME_REQUIREMENTS", "value": requirement}
    ]
    recipe = raw

    with pytest.raises(RecipeRuntimeSpecError, match=detail):
        _compile(recipe, model, role="entrypoint", rank=0)


def test_canonical_argv_preserves_empty_and_repeated_options(
    model: dict[str, object],
) -> None:
    raw = _example("recipe-source-build.json")
    _raw_runtime(raw)["arguments"] = [
        {"name": "repeated_option", "value": ""},
        {"name": "repeated_option", "value": "second"},
    ]
    recipe = raw
    argv = _argv(_compile(recipe, model, role="entrypoint", rank=0))
    first = argv.index("--repeated_option")
    assert argv[first : first + 4] == [
        "--repeated_option",
        "",
        "--repeated_option",
        "second",
    ]


def test_canonical_argv_preserves_post_executable_platform_shaped_data(
    model: dict[str, object],
) -> None:
    raw = _example("recipe-source-build.json")
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
    recipe = raw
    argv = _argv(_compile(recipe, model, role="entrypoint", rank=0))
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
    assert structured_command(
        ("/opt/vonk/bin/argv-check", "--option=-c", "-c"), canonical_argv=True
    )[1:] == (
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

    # The total bound is derived from the canonical host-runtime request
    # ceiling, so build the exact boundary from the constant rather than
    # restating its old round value.
    items, remainder = divmod(_MAX_ARGV_BYTES, 65_536)
    exact_command = ["x" * 65_536] * items
    if remainder:
        exact_command.append("x" * remainder)
    assert sum(len(item.encode()) for item in exact_command) == _MAX_ARGV_BYTES
    _validate_argv_size(exact_command)
    with pytest.raises(ValueError, match="total"):
        _validate_argv_size([*exact_command, "x"])


# Compiles the whole published corpus, every option choice included.
@pytest.mark.slow(60)
def test_current_recipe_corpus_compiles_every_role() -> None:
    """Compile every role from the current canonical recipe checkout."""
    root = recipe_library_root()
    recipe_files = sorted((root / "recipes").glob("*.json"))
    assert len(recipe_files) == 85
    engines: set[str] = set()
    projection_count = 0
    option_projection_count = 0
    for path in recipe_files:
        recipe_document, models, package = _published_recipe_context(path.stem)
        recipe = contracts.read_recipe(recipe_document)
        engines.add(recipe.runtime.engine)
        for index, role in enumerate(recipe.topology.roles):
            first_rank = sum(item.count for item in recipe.topology.roles[:index])
            for rank in range(first_rank, first_rank + role.count):
                spec = _compile(
                    recipe_document,
                    models,
                    package_handle=package,
                    role=role.name,
                    rank=rank,
                )
                projection_count += 1
                # Every choice of every option compiles on every rank.
                for option in recipe.options:
                    for choice in option.choices:
                        _compile(
                            recipe_document,
                            models,
                            package_handle=package,
                            role=role.name,
                            rank=rank,
                            parameters={"option_choices": {option.name: choice.value}},
                        )
                        option_projection_count += 1
                artifacts = _mappings(spec["artifacts"], "runtime artifacts")
                for artifact in artifacts:
                    assert _text(
                        require_mapping(
                            artifact["mount"], "artifact mount is not an object"
                        )["source"],
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
    assert option_projection_count > 0


def test_execution_digest_ignores_notes_but_tracks_bound_launch_changes(
    model: dict[str, object],
) -> None:
    base = _example("recipe-source-build.json")

    notes = deepcopy(base)
    metadata = _raw_object(notes["metadata"], "example metadata is not an object")
    description = _text(metadata["description"], "example description")
    metadata["description"] = description + " Editorial note."
    first_spec = _compile(base, model)
    noted_spec = _compile(notes, model)
    first_identity = _identity(first_spec)
    noted_identity = _identity(noted_spec)
    assert first_identity["execution_sha256"] == noted_identity["execution_sha256"]
    assert (
        first_identity["recipe_revision_sha256"]
        != noted_identity["recipe_revision_sha256"]
    )

    def digest(raw: dict[str, object]) -> str:
        spec = _compile(raw, model)
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
    models = _raw_mappings(
        mount["models"], "example models are not an array of objects"
    )
    files = _raw_mappings(
        models[0]["files"], "example files are not an array of objects"
    )
    file_mount = _raw_object(files[0]["mount"], "example mount is not an object")
    file_mount["target"] = "/models/target"
    entrypoint = _raw_sequence(
        _raw_runtime(mount)["entrypoint"], "example entrypoint is not an array"
    )
    entrypoint[2] = "/models/target"
    assert first_identity["execution_sha256"] != digest(mount)

    topology = deepcopy(base)
    topology_body = _raw_object(
        topology["topology"], "example topology is not an object"
    )
    topology_body["name"] = "different-placement"
    assert first_identity["execution_sha256"] != digest(topology)

    interface = deepcopy(base)
    interfaces = _raw_mappings(
        interface["interfaces"], "example interfaces are not an array of objects"
    )
    interfaces[0]["port"] = 9000
    assert first_identity["execution_sha256"] != digest(interface)


def test_security_is_in_execution_projection_and_build_input_is_separate(
    model: dict[str, object],
) -> None:
    from vonk_control.recipe_runtime_specs import _execution_digest

    common = {
        "runtime": {"image": "image@sha256:" + "a" * 64},
        "security": {"user": "10001:10001"},
    }
    changed = deepcopy(common)
    changed["security"]["user"] = "10002:10002"
    assert _execution_digest(common) != _execution_digest(changed)

    recipe = _recipe(
        "recipe-source-build.json",
        engine="vllm",
        entrypoint=["/opt/vonk/bin/vllm", "serve", "/models"],
    )
    digest = "a" * 64
    package = {
        "image_digest": digest,
        "image_reference": f"localhost/vonk/build@sha256:{digest}",
        "paths": ["context.tar", "Dockerfile"],
        "build_input_sha256": "b" * 64,
    }
    first = _compile(recipe, model, package_handle=package, role="entrypoint", rank=0)
    package["build_input_sha256"] = "c" * 64
    second = _compile(recipe, model, package_handle=package, role="entrypoint", rank=0)
    first_identity = _identity(first)
    second_identity = _identity(second)
    assert first_identity["execution_sha256"] == second_identity["execution_sha256"]
    assert first_identity["build_input_sha256"] != second_identity["build_input_sha256"]


def _published_recipe_context(
    name: str,
) -> tuple[dict[str, object], list[dict[str, object]], dict[str, object]]:
    root = recipe_library_root()
    recipe_document = json.loads(
        (root / "recipes" / f"{name}.json").read_text(encoding="utf-8")
    )
    recipe = contracts.read_recipe(recipe_document)
    models_by_digest: dict[str, dict[str, object]] = {}
    for path in (root / "models").glob("*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        models_by_digest[contracts.document_sha256(document)] = document
    models = [
        models_by_digest[selection.model.content_sha256] for selection in recipe.models
    ]
    digest = "a" * 64
    build = recipe.execution.build
    paths = [
        build.context.path,
        build.dockerfile,
        *(patch.path for patch in build.patches),
    ]
    for check in recipe.validation.serving.checks:
        request = check.request
        fixture = getattr(request, "fixture", None)
        if fixture:
            paths.append(fixture)
        paths.extend(getattr(request, "input_slots", {}).values())
    package: dict[str, object] = {
        "image_digest": digest,
        "image_reference": f"localhost/vonk/build@sha256:{digest}",
        "paths": paths,
    }
    return recipe_document, models, package


def test_published_distributed_sglang_preserves_authored_launch_and_rank() -> None:
    recipe, models, package = _published_recipe_context(
        "inkling-small-nvfp4-sglang-dual"
    )
    entrypoint = _compile(
        recipe, models, package_handle=package, role="entrypoint", rank=0
    )
    worker = _compile(recipe, models, package_handle=package, role="worker", rank=1)
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
    assert _security(entrypoint)["gpu"] is True
    assert {
        mount["target"]
        for mount in _mappings(_security(entrypoint)["mounts"], "security mounts")
    } == {"/models", "/outputs"}


def test_sglang_wrapper_receives_root_for_target_mount() -> None:
    raw = _example("recipe-source-build.json")
    runtime = _raw_runtime(raw)
    runtime["engine"] = "sglang"
    runtime["entrypoint"] = ["/opt/vonk/bin/sglang-serve"]
    runtime["arguments"] = [
        {"name": "model-path", "value": "/models"},
    ]
    models = _raw_mappings(raw["models"], "example models are not an array of objects")
    files = _raw_mappings(
        models[0]["files"], "example files are not an array of objects"
    )
    file_mount = _raw_object(files[0]["mount"], "example mount is not an object")
    file_mount["target"] = "/models/target"
    recipe = raw
    model = _example("model-definition.json")
    spec = _compile(recipe, model, role="entrypoint", rank=0)

    argv = _argv(spec)
    assert argv[:4] == [
        "/opt/vonk/bin/sglang-serve",
        "--model-path",
        "/models",
        "--host",
    ]
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
    spec = _compile(recipe, models, package_handle=package, role="entrypoint", rank=0)
    mounts = _mappings(_security(spec)["mounts"], "security mounts")
    assert {mount["target"] for mount in mounts} == {
        "/models/target",
        "/models/drafter",
        "/outputs",
    }
    argv = _argv(spec)
    assert _text(argv[argv.index("--model") + 1], "model argument").startswith(
        "/models/target/"
    )
    assert _text(argv[argv.index("--mtp-model") + 1], "mtp model argument").startswith(
        "/models/drafter/"
    )


def test_published_pipeline_has_output_contract() -> None:
    root = recipe_library_root()
    path = next(
        path
        for path in sorted((root / "recipes").glob("*.json"))
        if _raw_engine(path) == "pytorch-pipeline"
    )
    recipe_name = path.stem
    recipe, models, package = _published_recipe_context(recipe_name)
    spec = _compile(recipe, models, package_handle=package, role="entrypoint", rank=0)
    argv = _argv(spec)
    assert argv[0] == "/opt/vonk/bin/pytorch-pipeline"
    assert argv[-2:] == ["--output-dir", "/outputs"]
    assert any(
        item["target"] == "/outputs"
        for item in _mappings(_security(spec)["mounts"], "security mounts")
    )


@pytest.mark.parametrize("retired", ["model_projections", "package", "build_receipt"])
def test_runtime_compiler_rejects_retired_resolver_keys_even_with_current_inputs(
    model: dict[str, object], retired: str
) -> None:
    recipe = _recipe(
        "recipe-source-build.json",
        engine="vllm",
        entrypoint=["/opt/vonk/bin/vllm", "serve", "/models"],
    )
    with pytest.raises(RecipeRuntimeSpecError, match="retired authorities"):
        _compile(
            recipe,
            model,
            resolved_entities={
                retired: [model] if retired == "model_projections" else {}
            },
        )


@pytest.mark.parametrize("include_paths", [False, True])
def test_runtime_compiler_rejects_retired_member_paths(
    model: dict[str, object], include_paths: bool
) -> None:
    recipe = _recipe(
        "recipe-source-build.json",
        engine="vllm",
        entrypoint=["/opt/vonk/bin/vllm", "serve", "/models"],
    )
    package: dict[str, object] = {
        "image_reference": "localhost/vonk/recipe-build@sha256:" + "a" * 64,
        "image_digest": "a" * 64,
        "member_paths": ["context.tar", "Dockerfile"],
    }
    if include_paths:
        package["paths"] = ["context.tar", "Dockerfile"]
    with pytest.raises(RecipeRuntimeSpecError, match="retired member_paths"):
        _compile(recipe, model, package_handle=package, role="entrypoint", rank=0)


def _recipe_with_options(model: dict[str, object]) -> dict[str, object]:
    raw = _example("recipe-source-build.json")
    runtime = _raw_runtime(raw)
    runtime["arguments"] = [{"name": "verify", "value": "all"}]
    runtime["environment"] = [{"name": "MODE", "value": "off"}]
    raw["options"] = [
        {
            "name": "verification",
            "label": "Verification",
            "help": "How drafted tokens are verified.",
            "choices": [
                {
                    "value": "standard",
                    "label": "Standard",
                    "help": "Verify every token.",
                    "default": True,
                },
                {
                    "value": "adaptive",
                    "label": "Adaptive",
                    "help": "Verify a per-step prefix.",
                    "args": [
                        {"name": "verify", "value": "prefix"},
                        {"name": "extra-flag", "value": True},
                    ],
                    "env": {"MODE": "adaptive"},
                },
            ],
        }
    ]
    return raw


def test_chosen_recipe_options_reach_every_rank_and_default_when_unset(
    model: dict[str, object],
) -> None:
    raw = _recipe_with_options(model)
    recipe = contracts.read_recipe(raw)
    ranks = [
        (role.name, rank)
        for rank, role in enumerate(
            role for role in recipe.topology.roles for _ in range(role.count)
        )
    ]
    assert ranks

    def launch(parameters: dict[str, object] | None, role: str, rank: int) -> Any:
        parsed = {contracts.document_sha256(model): contracts.read_model(model)}
        spec = compile_runtime_spec(
            recipe,
            models=parsed,
            recipe_digest=contracts.document_sha256(raw),
            package_handle=_BUILT_IMAGE,
            parameters=parameters,
            role=role,
            rank=rank,
        )
        env = {
            item["name"]: item["value"]
            for item in _mappings(_runtime(spec)["environment"], "environment")
        }
        return list(_argv(spec)), env

    for role, rank in ranks:
        chosen_argv, chosen_env = launch(
            {"option_choices": {"verification": "adaptive"}}, role, rank
        )
        assert chosen_argv.count("--verify") == 1
        assert chosen_argv[chosen_argv.index("--verify") + 1] == "prefix"
        assert "--extra-flag" in chosen_argv
        assert chosen_env["MODE"] == "adaptive"
        default_argv, default_env = launch(None, role, rank)
        assert default_argv[default_argv.index("--verify") + 1] == "all"
        assert "--extra-flag" not in default_argv
        assert default_env["MODE"] == "off"

    with pytest.raises(RecipeRuntimeSpecError, match="verification"):
        launch({"option_choices": {"verification": "nope"}}, *ranks[0])


def test_a_recipe_option_cannot_reach_platform_owned_environment(
    model: dict[str, object],
) -> None:
    raw = _recipe_with_options(model)
    options = _raw_sequence(raw["options"], "options")
    choices = _raw_mappings(_raw_object(options[0], "option")["choices"], "choices")
    choices[1]["env"] = {"HOME": "/tmp"}
    recipe = contracts.read_recipe(raw)
    parsed = {contracts.document_sha256(model): contracts.read_model(model)}
    with pytest.raises(RecipeRuntimeSpecError, match="platform-owned"):
        compile_runtime_spec(
            recipe,
            models=parsed,
            recipe_digest=contracts.document_sha256(raw),
            package_handle=_BUILT_IMAGE,
            parameters={"option_choices": {"verification": "adaptive"}},
            role="entrypoint",
            rank=0,
        )
