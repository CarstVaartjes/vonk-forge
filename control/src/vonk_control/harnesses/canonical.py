"""Canonical RecipeDefinition to harness projection compiler.

This module is the platform seam for the public v2 contracts.  It accepts only
validated RecipeDefinition/ModelDefinition projections and a package handle;
image, cache, security and mount policy are derived here rather than authored
in recipe documents.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from math import isfinite
from pathlib import Path
from vonk_forge_contracts import ModelDefinition, RecipeDefinition
from vonk_forge_contracts.resolver import (
    ContractResolutionError,
    validate_recipe_models,
)

from ..runtime_writable_paths import (
    effective_environment,
    reject_recipe_environment,
    telemetry_contract,
    validate_paths,
    writable_paths,
)
from .common import HarnessCompileError, validate_projection
from .contracts import HarnessBinding, HarnessMount, HarnessProjection

_BUILTINS = frozenset(
    {
        "vllm",
        "sglang",
        "tensorrt-llm",
        "llama-cpp",
        "ds4",
        "diffusers",
        "comfyui",
        "pytorch-pipeline",
    }
)
_EXECUTABLES: dict[str, frozenset[str]] = {
    "vllm": frozenset({"vllm", "vllm-serve"}),
    "sglang": frozenset({"sglang", "sglang-serve"}),
    "tensorrt-llm": frozenset({"trtllm-serve", "tensorrt-llm"}),
    "llama-cpp": frozenset({"llama-server", "llama-cpp"}),
    "ds4": frozenset({"ds4-serve", "ds4"}),
    "diffusers": frozenset({"diffusers-job", "diffusers"}),
    "comfyui": frozenset({"comfyui-job", "comfyui"}),
    "pytorch-pipeline": frozenset({"pytorch-pipeline", "pytorch"}),
}
_WRAPPERS = {
    "vllm": "/opt/vonk/bin/vllm",
    "sglang": "/opt/vonk/bin/sglang-serve",
    "tensorrt-llm": "/usr/local/bin/trtllm-serve",
    "llama-cpp": "/opt/vonk/bin/llama-server",
    "ds4": "/opt/vonk/bin/ds4-serve",
    "diffusers": "/opt/vonk/bin/diffusers-job",
    "comfyui": "/opt/vonk/bin/comfyui-job",
    "pytorch-pipeline": "/opt/vonk/bin/pytorch-pipeline",
}
_JOB_INTERFACES = frozenset(
    {"image-job", "audio-job", "video-job", "mesh-job", "artifact-job"}
)
_MAX_RUNTIME_ARGUMENTS = 128
_MAX_ARGV_TOKEN_BYTES = 65_536
_MAX_ARGV_BYTES = 1_048_576
_SAFE_ARG_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_SAFE_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_PLATFORM_ENV_NAMES = frozenset(
    {
        "PATH",
        "PYTHONPATH",
        "PYTHONHOME",
        "LD_PRELOAD",
        "HOME",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "TMPDIR",
        "HF_HOME",
        "TRANSFORMERS_CACHE",
        "VLLM_CACHE_ROOT",
        "TORCH_HOME",
        "TORCH_EXTENSIONS_DIR",
        "TORCHINDUCTOR_CACHE_DIR",
        "TRITON_CACHE_DIR",
        "CUDA_CACHE_PATH",
        "UV_CACHE_DIR",
    }
)


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _unwrap(value: object, label: str) -> object:
    if isinstance(value, (RecipeDefinition, ModelDefinition)):
        return value
    document = getattr(value, "document", None)
    if isinstance(document, Mapping):
        return document
    if isinstance(value, Mapping):
        return value
    raise HarnessCompileError(f"{label} projection is invalid")


def _recipe(value: object) -> RecipeDefinition:
    if isinstance(value, RecipeDefinition):
        return value
    try:
        raw = _unwrap(value, "recipe")
        if not isinstance(raw, Mapping):
            raise TypeError
        return RecipeDefinition.model_validate(raw)
    except Exception as error:
        raise HarnessCompileError("recipe does not satisfy RecipeDefinition v2") from error


def _models(values: object) -> tuple[ModelDefinition, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise HarnessCompileError("canonical model projections are missing")
    result: list[ModelDefinition] = []
    for value in values:
        if isinstance(value, ModelDefinition):
            result.append(value)
            continue
        try:
            raw = _unwrap(value, "model")
            if not isinstance(raw, Mapping):
                raise TypeError
            result.append(ModelDefinition.model_validate(raw))
        except Exception as error:
            raise HarnessCompileError("canonical model projection is invalid") from error
    return tuple(result)


def _scalar(value: object, label: str) -> str:
    if type(value) is bool:
        rendered = "true" if value else "false"
    elif type(value) in (str, int, float):
        if type(value) is float and not isfinite(value):
            raise HarnessCompileError(f"{label} is not finite")
        rendered = str(value)
    elif isinstance(value, (list, dict)):
        try:
            rendered = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise HarnessCompileError(f"{label} is not JSON serializable") from error
    else:
        raise HarnessCompileError(f"{label} has an unsupported value type")
    if len(rendered.encode("utf-8")) > _MAX_ARGV_TOKEN_BYTES or "\x00" in rendered:
        raise HarnessCompileError(f"{label} exceeds the bounded argv value contract")
    return rendered


def _validate_argv_size(command: Sequence[str]) -> None:
    total = 0
    for item in command:
        if type(item) is not str or len(item.encode("utf-8")) > _MAX_ARGV_TOKEN_BYTES or "\x00" in item:
            raise HarnessCompileError("compiled harness argv token exceeds its bound")
        total += len(item.encode("utf-8"))
    if total > _MAX_ARGV_BYTES:
        raise HarnessCompileError("compiled harness argv exceeds its total bound")


def _package_value(package: object, *names: str) -> object:
    if package is None:
        return None
    if isinstance(package, Mapping):
        for name in names:
            if name in package:
                return package[name]
    for name in names:
        value = getattr(package, name, None)
        if value is not None:
            return value
    return None


def _image(recipe: RecipeDefinition, package: object) -> tuple[str, str]:
    if recipe.execution.mode == "image":
        image = recipe.execution.image
        return f"{image.repository}@sha256:{image.digest}", image.digest
    digest = _package_value(package, "image_digest", "built_image_digest", "digest")
    if type(digest) is str and digest.startswith("sha256:"):
        digest = digest[7:]
    if type(digest) is not str or _DIGEST.fullmatch(digest) is None:
        raise HarnessCompileError("source-build recipe requires an exact built image receipt")
    platform = _package_value(package, "platform", "image_platform")
    if platform is not None and platform != "linux/arm64":
        raise HarnessCompileError("built image receipt must target linux/arm64")
    reference = _package_value(package, "image_reference", "reference")
    if reference is None:
        repository = _package_value(package, "image_repository", "repository") or "localhost/vonk/recipe-build"
        reference = f"{repository}@sha256:{digest}"
    if type(reference) is not str or not reference.endswith(f"@sha256:{digest}"):
        raise HarnessCompileError("built image receipt reference does not match its digest")
    return reference, digest


def _settings(recipe: RecipeDefinition, supplied: Mapping[str, object] | None) -> dict[str, object]:
    settings = recipe.settings
    values: dict[str, object] = {}
    for name in ("context_tokens", "concurrency", "max_batch_tokens"):
        setting = getattr(settings, name, None)
        if setting is not None:
            values[name] = setting.value
    values.update({name: setting.value for name, setting in settings.knobs.items()})
    if supplied is not None:
        if not isinstance(supplied, Mapping) or any(type(name) is not str for name in supplied):
            raise HarnessCompileError("canonical settings are invalid")
        unknown = set(supplied) - set(values)
        if unknown:
            raise HarnessCompileError("runtime settings contain undeclared names")
        values.update(supplied)
    return values


def _argv(recipe: RecipeDefinition, settings: Mapping[str, object]) -> tuple[str, ...]:
    entrypoint = tuple(recipe.runtime.entrypoint)
    executable = Path(entrypoint[0]).name
    if entrypoint[0].startswith("/"):
        path = Path(entrypoint[0])
        if (
            not entrypoint[0].startswith("/opt/vonk/bin/")
            or path.name in {"sh", "bash", "dash", "env", "busybox"}
            or "/../" in entrypoint[0]
            or "//" in entrypoint[0]
        ):
            raise HarnessCompileError("recipe entrypoint is outside the trusted harness path")
    elif executable not in _EXECUTABLES[recipe.runtime.engine]:
        raise HarnessCompileError("recipe entrypoint does not match its engine harness")
    if any(type(item) is not str or not item or len(item) > 4096 or "\x00" in item for item in entrypoint):
        raise HarnessCompileError("recipe entrypoint contains invalid argv data")
    result = list(entrypoint[1:])
    if len(recipe.runtime.arguments) > _MAX_RUNTIME_ARGUMENTS:
        raise HarnessCompileError("recipe has too many runtime arguments")
    for argument in recipe.runtime.arguments:
        name = argument.name[2:] if argument.name.startswith("--") else argument.name
        if _SAFE_ARG_NAME.fullmatch(name) is None:
            raise HarnessCompileError(f"recipe argument name is invalid: {argument.name}")
        # Preserve engine option spelling exactly; argv is not shell text.
        flag = f"--{name}"
        value = argument.value if argument.setting is None else settings[argument.setting]
        if type(value) is bool:
            if value:
                result.append(flag)
        else:
            result.extend((flag, _scalar(value, f"recipe argument {argument.name}")))
    _validate_argv_size(result)
    return tuple(result)


def _environment(recipe: RecipeDefinition) -> tuple[tuple[str, str], ...]:
    supplied: list[tuple[str, str]] = []
    for item in recipe.runtime.environment:
        if item.secret is not None:
            raise HarnessCompileError("runtime secret requires the platform secret projection")
        if _SAFE_ENV_NAME.fullmatch(item.name) is None or item.name in _PLATFORM_ENV_NAMES:
            raise HarnessCompileError(f"recipe environment is platform-owned or invalid: {item.name}")
        supplied.append((item.name, _scalar(item.value, f"recipe environment {item.name}")))
    try:
        reject_recipe_environment(recipe.runtime.engine, supplied)
        return effective_environment(recipe.runtime.engine, supplied)
    except (TypeError, ValueError) as error:
        raise HarnessCompileError(str(error)) from error


def _model_mounts(recipe: RecipeDefinition, models: tuple[ModelDefinition, ...], role: str) -> tuple[tuple[dict[str, object], HarnessMount], ...]:
    try:
        validate_recipe_models(recipe, models)
    except ContractResolutionError as error:
        raise HarnessCompileError(str(error)) from error
    by_identity = {(m.identity.publisher, m.identity.slug): m for m in models}
    selected: list[tuple[dict[str, object], HarnessMount]] = []
    mounts_by_key: dict[tuple[str, str], HarnessMount] = {}
    target_owner: dict[str, str] = {}
    for selection in recipe.models:
        model = by_identity[(selection.model.publisher, selection.model.slug)]
        files = {item.id: item for item in model.files}
        for selector in selection.files:
            if role not in selector.roles:
                continue
            source = f"/run/vonk/models/{selection.id}/{selector.file_id}"
            target_owner.setdefault(selector.mount.target, selection.id)
            if target_owner[selector.mount.target] != selection.id:
                raise HarnessCompileError(
                    f"model mount target is claimed by multiple selections: {selector.mount.target}"
                )
            mount = mounts_by_key.setdefault(
                (selection.id, selector.mount.target),
                HarnessMount(
                    f"/run/vonk/models/{selection.id}",
                    selector.mount.target,
                    read_only=True,
                ),
            )
            selected.append((
                {
                    "id": selector.id,
                    "selection_id": selection.id,
                    "file_id": selector.file_id,
                    "path": files[selector.file_id].path,
                    "roles": list(selector.roles),
                    "mount": {"source": source, "target": selector.mount.target, "read_only": True},
                    "model": {
                        "publisher": selection.model.publisher,
                        "slug": selection.model.slug,
                        "content_sha256": selection.model.content_sha256,
                    },
                },
                mount,
            ))
    if not selected:
        raise HarnessCompileError("mapped role has no selected model files")
    return tuple(selected)


def _distributed_args(
    recipe: RecipeDefinition,
    command: list[str],
    *,
    role: str,
    rank: int,
) -> list[str]:
    topology = recipe.topology
    if topology.mode != "distributed":
        return command

    def has(name: str) -> bool:
        wanted = name.removeprefix("--").replace("_", "-")
        return any(
            item.startswith("--")
            and item[2:].split("=", 1)[0].replace("_", "-") == wanted
            for item in command
        )

    if recipe.runtime.engine == "sglang":
        if not has("nnodes"):
            command.extend(("--nnodes", str(topology.node_count)))
        if not has("node-rank"):
            command.extend(("--node-rank", str(rank)))
        if not has("dist-init-addr"):
            command.extend(("--dist-init-addr", "VONK_MASTER_ADDR:VONK_MASTER_PORT"))
        return command
    if recipe.runtime.engine == "vllm":
        parallelism = topology.parallelism
        if not has("tensor-parallel-size"):
            command.extend(("--tensor-parallel-size", str(parallelism.tensor)))
        if not has("pipeline-parallel-size"):
            command.extend(("--pipeline-parallel-size", str(parallelism.pipeline)))
        if parallelism.backend in {"mp", "ray"} and not has("distributed-executor-backend"):
            command.extend(("--distributed-executor-backend", parallelism.backend))
        if not has("nnodes"):
            command.extend(("--nnodes", str(topology.node_count)))
        if not has("node-rank"):
            command.extend(("--node-rank", str(rank)))
        if role == "worker" and not has("headless"):
            command.append("--headless")
        return command
    raise HarnessCompileError(
        f"distributed topology is not supported by the {recipe.runtime.engine} harness"
    )


def _harness_security(slug: str, topology: object) -> tuple[tuple[str, ...], bool]:
    path = Path(__file__).parents[4] / "config" / "execution-harnesses" / f"{slug}.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HarnessCompileError("trusted execution harness identity is unavailable") from error
    requirements = document.get("capability_requirements")
    exceptions = document.get("security_exceptions")
    if not isinstance(requirements, list) or not isinstance(exceptions, list):
        raise HarnessCompileError("trusted execution harness security identity is invalid")
    devices = ("nvidia.com/gpu=all",) if "nvidia-gpu" in requirements else ()
    host_network = "host-network" in exceptions and getattr(topology, "mode", None) == "distributed"
    return devices, host_network


def _harness_digest(slug: str) -> str:
    path = Path(__file__).parents[4] / "config" / "execution-harnesses" / f"{slug}.json"
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return _digest({"kind": "execution-harness", "slug": slug, "contract_version": 1})


def compile_canonical_harness(
    recipe: RecipeDefinition,
    models: tuple[ModelDefinition, ...],
    package: object,
    *,
    role: str,
    rank: int,
    settings: Mapping[str, object] | None = None,
) -> tuple[HarnessProjection, tuple[dict[str, object], ...], str]:
    slug = recipe.runtime.engine
    if slug not in _BUILTINS:
        raise HarnessCompileError(f"unknown execution harness: {slug}")
    topology = recipe.topology
    if role not in {item.name for item in topology.roles} or rank < 0 or rank >= topology.node_count:
        raise HarnessCompileError("mapped topology role or rank is invalid")
    role_decl = next(item for item in topology.roles if item.name == role)
    offset = 0
    for item in topology.roles:
        if item.name == role:
            break
        offset += item.count
    if rank >= offset + role_decl.count or rank < offset:
        raise HarnessCompileError("mapped topology role and rank are inconsistent")
    image, image_digest = _image(recipe, package)
    environment = _environment(recipe)
    mounts = _model_mounts(recipe, models, role)
    command = list(_argv(recipe, _settings(recipe, settings)))
    wrapper = _WRAPPERS[slug]
    entry = tuple(recipe.runtime.entrypoint)
    # Trusted absolute entrypoints remain authored by the recipe repository.
    # Short names retain the platform wrapper fallback used by examples.
    command.insert(0, entry[0] if entry[0].startswith("/") else wrapper)
    # Replace a model path with the declared target when the recipe uses the
    # canonical /models root and preserve explicit subpaths.
    primary_target = mounts[0][1].target
    command = [primary_target if value == "/models" else value for value in command]
    command = _distributed_args(recipe, command, role=role, rank=rank)
    interface = recipe.interfaces[0]
    if interface.adapter == "openai":
        if "--host" not in command:
            command.extend(("--host", "0.0.0.0"))
        if "--port" not in command:
            command.extend(("--port", str(interface.port)))
    else:
        if "--output-dir" not in command:
            command.extend(("--output-dir", "/outputs"))
    _validate_argv_size(command)
    devices, _host_network = _harness_security(slug, topology)
    model_mounts: list[HarnessMount] = []
    for _artifact, mount in mounts:
        if mount not in model_mounts:
            model_mounts.append(mount)
    validate_paths(slug, writable_paths(slug), dict(environment))
    projection = HarnessProjection(
        slug=slug,
        contract_version=1,
        command=tuple(command),
        image=image,
        network_mode="none",
        architecture="linux/arm64",
        user="10001:10001",
        no_new_privileges=True,
        capabilities=(),
        devices=devices,
        model_mounts=tuple(model_mounts),
        output_mount=HarnessMount("/run/vonk/outputs", "/outputs", read_only=False, isolated=True),
        input_mount=(HarnessMount("/run/vonk/inputs", "/inputs", read_only=True, isolated=True) if getattr(interface, "input", None) is not None else None),
        environment=environment,
        writable_paths=writable_paths(slug),
        telemetry=telemetry_contract(slug),
        read_only_root=True,
        binding=HarnessBinding(
            harness_content_sha256=_harness_digest(slug),
            execution_content_sha256=_digest(recipe.execution.model_dump(mode="json")),
            topology_node_count=topology.node_count,
            role=role,
            rank=rank,
        ),
    )
    try:
        validate_projection(projection, canonical_argv=True, canonical_mounts=True)
    except HarnessCompileError as error:
        raise HarnessCompileError(str(error)) from error
    return projection, tuple(artifact for artifact, _mount in mounts), image_digest


__all__ = ["compile_canonical_harness"]
