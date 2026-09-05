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
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_./:+@%=[\]{} ,\",<>-]{1,4096}$")
_SAFE_ARG_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SAFE_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_PLATFORM_ARG_NAMES = frozenset(
    {
        "cap-add",
        "cap-drop",
        "device",
        "init",
        "mount",
        "network",
        "privileged",
        "publish",
        "read-only",
        "security-opt",
        "tmpfs",
        "user",
        "volume",
    }
)
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
    elif type(value) in (str, int):
        rendered = str(value)
    else:
        raise HarnessCompileError(f"{label} must be a scalar")
    if _SAFE_TOKEN.fullmatch(rendered) is None:
        raise HarnessCompileError(f"{label} contains unsafe shell syntax")
    return rendered


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
    if executable not in _EXECUTABLES[recipe.runtime.engine]:
        raise HarnessCompileError("recipe entrypoint does not match its engine harness")
    if any(type(item) is not str or not _SAFE_TOKEN.fullmatch(item) for item in entrypoint):
        raise HarnessCompileError("recipe entrypoint contains unsafe shell syntax")
    result = list(entrypoint[1:])
    seen: set[str] = set()
    for argument in recipe.runtime.arguments:
        name = argument.name[2:] if argument.name.startswith("--") else argument.name
        normalized = name.replace("_", "-")
        if _SAFE_ARG_NAME.fullmatch(name) is None or normalized in _PLATFORM_ARG_NAMES:
            raise HarnessCompileError(f"recipe argument is platform-owned or invalid: {argument.name}")
        # Preserve engine option spelling exactly; argv is not shell text.
        flag = f"--{name}"
        if flag in seen:
            raise HarnessCompileError(f"recipe argument is repeated: {argument.name}")
        seen.add(flag)
        value = argument.value if argument.setting is None else settings[argument.setting]
        if type(value) is bool:
            if value:
                result.append(flag)
        else:
            result.extend((flag, _scalar(value, f"recipe argument {argument.name}")))
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
    mounts_by_target: dict[str, HarnessMount] = {}
    for selection in recipe.models:
        model = by_identity[(selection.model.publisher, selection.model.slug)]
        files = {item.id: item for item in model.files}
        for selector in selection.files:
            if role not in selector.roles:
                continue
            source = f"/run/vonk/models/{selection.id}/{selector.file_id}"
            mount = mounts_by_target.setdefault(
                selector.mount.target,
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
    # Wrapper paths are platform-owned; the engine-owned suffix and all runtime
    # arguments retain their declared order and values.
    if slug == "vllm":
        command = [wrapper, *entry[1:]]
    elif slug == "sglang":
        command = [wrapper, *entry[1:]]
    elif slug == "tensorrt-llm":
        command = [wrapper, *entry[1:]]
    else:
        command = [wrapper, *entry[1:]]
    # Replace a model path with the declared target when the recipe uses the
    # canonical /models root and preserve explicit subpaths.
    primary_target = mounts[0][1].target
    command = [primary_target if value == "/models" else value for value in command]
    command.extend(_argv(recipe, _settings(recipe, settings))[len(entry) - 1 :])
    interface = recipe.interfaces[0]
    if interface.adapter == "openai":
        if "--host" not in command:
            command.extend(("--host", "0.0.0.0"))
        if "--port" not in command:
            command.extend(("--port", str(interface.port)))
    else:
        if "--output-dir" not in command:
            command.extend(("--output-dir", "/outputs"))
    if any(type(item) is not str or not _SAFE_TOKEN.fullmatch(item) for item in command):
        raise HarnessCompileError("compiled harness command is unsafe")
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
        # The agent materializes every selected file below this one immutable
        # root.  Per-file source paths remain in the artifact projection so the
        # package handle and runtime payload can be checked against one another.
        model_mounts=(HarnessMount("/run/vonk/models", "/models", read_only=True),),
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
        validate_projection(projection, canonical_argv=True)
    except HarnessCompileError as error:
        raise HarnessCompileError(str(error)) from error
    return projection, tuple(artifact for artifact, _mount in mounts), image_digest


__all__ = ["compile_canonical_harness"]
