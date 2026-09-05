"""Central writable-path contract for built-in runtime engines.

Runtime images are deliberately allowed to contain precompiled code and
read-only model data.  Only paths in this module may receive runtime writes.
The contract is emitted by the harness compiler so an image's baked-in ENV or
a recipe-local override cannot silently move writes into the image root.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True, slots=True)
class RuntimeWritablePath:
    """One runtime directory, mounted below the writable output mount."""

    name: str
    path: str
    persistent: bool
    source: str = "outputs"


_COMMON_PATHS = (
    RuntimeWritablePath("home", "/outputs/cache/home", True),
    RuntimeWritablePath("xdg-cache", "/outputs/cache", True),
    RuntimeWritablePath("xdg-config", "/outputs/cache/config", True),
    RuntimeWritablePath("temporary", "/outputs/tmp", False),
)

_PYTHON_PATHS = (
    RuntimeWritablePath("huggingface", "/outputs/cache/huggingface", True),
    RuntimeWritablePath(
        "transformers", "/outputs/cache/huggingface/transformers", True
    ),
    RuntimeWritablePath("torch", "/outputs/cache/torch", True),
    RuntimeWritablePath("torch-extensions", "/outputs/cache/torch_extensions", True),
    RuntimeWritablePath("torchinductor", "/outputs/cache/torchinductor", True),
    RuntimeWritablePath("triton", "/outputs/cache/triton", True),
    RuntimeWritablePath("cuda", "/outputs/cache/cuda", True),
    RuntimeWritablePath("uv", "/outputs/cache/uv", True),
)

_VLLM_PATHS = (
    RuntimeWritablePath("vllm", "/outputs/cache/vllm", True),
)

_ENGINE_PATHS: dict[str, tuple[RuntimeWritablePath, ...]] = {
    "vllm": (*_COMMON_PATHS, *_PYTHON_PATHS, *_VLLM_PATHS),
    "sglang": (*_COMMON_PATHS, *_PYTHON_PATHS),
    "tensorrt-llm": (*_COMMON_PATHS, *_PYTHON_PATHS),
    "llama-cpp": _COMMON_PATHS,
    "ds4": (*_COMMON_PATHS, *_PYTHON_PATHS),
    "diffusers": (*_COMMON_PATHS, *_PYTHON_PATHS),
    "comfyui": (*_COMMON_PATHS, *_PYTHON_PATHS),
    "pytorch-pipeline": (*_COMMON_PATHS, *_PYTHON_PATHS),
}

# These names are paths rather than recipe tuning knobs. They are injected by
# the platform; a recipe cannot repeat them, move them, or introduce a second
# writable root.
_ENGINE_ENVIRONMENT: dict[str, dict[str, str]] = {
    "vllm": {
        "HOME": "/outputs/cache/home",
        "XDG_CACHE_HOME": "/outputs/cache",
        "XDG_CONFIG_HOME": "/outputs/cache/config",
        "TMPDIR": "/outputs/tmp",
        "HF_HOME": "/outputs/cache/huggingface",
        "TRANSFORMERS_CACHE": "/outputs/cache/huggingface/transformers",
        "VLLM_CACHE_ROOT": "/outputs/cache/vllm",
        "TRITON_CACHE_DIR": "/outputs/cache/triton",
        "TORCH_HOME": "/outputs/cache/torch",
        "TORCH_EXTENSIONS_DIR": "/outputs/cache/torch_extensions",
        "TORCHINDUCTOR_CACHE_DIR": "/outputs/cache/torchinductor",
        "CUDA_CACHE_PATH": "/outputs/cache/cuda",
        "UV_CACHE_DIR": "/outputs/cache/uv",
    },
    "sglang": {
        "HOME": "/outputs/cache/home",
        "XDG_CACHE_HOME": "/outputs/cache",
        "XDG_CONFIG_HOME": "/outputs/cache/config",
        "TMPDIR": "/outputs/tmp",
        "HF_HOME": "/outputs/cache/huggingface",
        "TRANSFORMERS_CACHE": "/outputs/cache/huggingface/transformers",
        "TORCH_HOME": "/outputs/cache/torch",
        "TORCH_EXTENSIONS_DIR": "/outputs/cache/torch_extensions",
        "TORCHINDUCTOR_CACHE_DIR": "/outputs/cache/torchinductor",
        "TRITON_CACHE_DIR": "/outputs/cache/triton",
    },
}
_MIA_VLLM_ENVIRONMENT = {
    "FLASHINFER_WORKSPACE_BASE": "/outputs/cache/flashinfer",
    "TILELANG_CACHE_DIR": "/outputs/cache/tilelang",
    "B12X_CUTE_COMPILE_CACHE_DIR": "/outputs/cache/b12x-cute-compile",
    "TORCH_FR_DUMP_TEMP_FILE": "/outputs/cache/nccl-fr/comm_lib_trace_rank_",
    "TORCH_NCCL_DEBUG_INFO_PIPE_FILE": "/outputs/cache/nccl-fr/fr_dump_pipe_",
}
for _slug in ("tensorrt-llm", "ds4", "diffusers", "comfyui", "pytorch-pipeline"):
    _ENGINE_ENVIRONMENT[_slug] = dict(_ENGINE_ENVIRONMENT["sglang"])
for _name in ("HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "TMPDIR"):
    _ENGINE_ENVIRONMENT["llama-cpp"] = {
        **_ENGINE_ENVIRONMENT.get("llama-cpp", {}),
        _name: dict(_ENGINE_ENVIRONMENT["sglang"])[_name],
    }

_OPTIONAL_PATH_NAMES = frozenset(
    {
        "FLASHINFER_WORKSPACE_BASE",
        "VLLM_FLASHINFER_AUTOTUNE_CACHE_DIR",
        "TILELANG_CACHE_DIR",
        "TILELANG_TMP_DIR",
        "B12X_CUTE_COMPILE_CACHE_DIR",
        "TVM_CACHE_DIR",
        "TVM_FFI_CACHE_DIR",
        "TORCH_FR_DUMP_TEMP_FILE",
        "TORCH_NCCL_DEBUG_INFO_PIPE_FILE",
    }
)
_PATH_NAMES = frozenset(
    name for environment in _ENGINE_ENVIRONMENT.values() for name in environment
) | _OPTIONAL_PATH_NAMES
_RESERVED_PATH_NAMES = _PATH_NAMES | _OPTIONAL_PATH_NAMES


def writable_paths(slug: str) -> tuple[RuntimeWritablePath, ...]:
    return writable_paths_for_distribution(slug, None)


def writable_paths_for_distribution(
    slug: str, distribution: Mapping[str, object] | None
) -> tuple[RuntimeWritablePath, ...]:
    try:
        paths = _ENGINE_PATHS[slug]
    except KeyError as exc:
        raise _compile_error(
            f"runtime writable-path contract is unavailable: {slug}"
        ) from exc
    return paths


def reject_recipe_environment(
    slug: str, supplied: Iterable[tuple[str, str]]
) -> None:
    """Reject reserved path variables before the platform adds its defaults."""
    if slug not in _ENGINE_ENVIRONMENT:
        raise _compile_error(
            f"runtime writable-path contract is unavailable: {slug}"
        )
    for name, _value in supplied:
        if name in _RESERVED_PATH_NAMES:
            raise _compile_error(
                f"runtime writable path variable is platform-owned: {name}"
            )


def environment(
    slug: str, supplied: Iterable[tuple[str, str]]
) -> tuple[tuple[str, str], ...]:
    """Return defaults and reject recipe ownership of reserved path names."""
    return _merge_environment(slug, supplied, allow_reserved=False)


def compile_environment(
    slug: str,
    supplied: Iterable[tuple[str, str]],
    distribution: Mapping[str, object] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Merge defaults with values already injected by the central compiler."""
    return _merge_environment(
        slug, supplied, allow_reserved=True, distribution=distribution
    )


def _merge_environment(
    slug: str,
    supplied: Iterable[tuple[str, str]],
    *,
    allow_reserved: bool,
    distribution: Mapping[str, object] | None = None,
) -> tuple[tuple[str, str], ...]:
    defaults = _environment_defaults(slug, distribution)
    if defaults is None:
        raise _compile_error(
            f"runtime writable-path contract is unavailable: {slug}"
        )
    supplied_map: dict[str, str] = {}
    for name, value in supplied:
        if name in supplied_map:
            raise _compile_error(f"runtime environment is repeated: {name}")
        supplied_map[name] = value
    if not allow_reserved:
        for name in supplied_map:
            if name in _RESERVED_PATH_NAMES:
                raise _compile_error(
                    f"runtime writable path variable is platform-owned: {name}"
                )
    for name, expected in defaults.items():
        actual = supplied_map.get(name)
        if actual is not None and not allow_reserved:
            raise _compile_error(
                f"runtime writable path variable is platform-owned: {name}"
            )
        if actual is not None and actual != expected:
            raise _compile_error(
                f"runtime writable path override is conflicting: {name}"
            )
    merged = {**supplied_map, **defaults}
    return tuple((name, merged[name]) for name in merged)


def validate_paths(
    slug: str,
    paths: Iterable[RuntimeWritablePath],
    env: Mapping[str, str],
    distribution: Mapping[str, object] | None = None,
) -> None:
    expected = writable_paths_for_distribution(slug, distribution)
    actual = tuple(paths)
    if actual != expected:
        raise _compile_error(
            "runtime writable paths are not the central engine contract"
        )
    known = {item.path for item in actual}
    for item in actual:
        if (
            item.source != "outputs"
            or not item.path.startswith("/outputs/")
            or "//" in item.path
            or ".." in PurePosixPath(item.path).parts
            or item.path.endswith("/")
        ):
            raise _compile_error("runtime writable path escapes its output mount")
    for name, value in env.items():
        if name not in _PATH_NAMES:
            continue
        if not value.startswith("/"):
            raise _compile_error(f"runtime path environment is not absolute: {name}")
        if not any(value == path or value.startswith(path + "/") for path in known):
            raise _compile_error(
                f"runtime path environment escapes writable mounts: {name}"
            )


def document(
    slug: str,
    env: Iterable[tuple[str, str]],
    distribution: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    values = dict(env)
    paths = writable_paths_for_distribution(slug, distribution)
    validate_paths(slug, paths, values, distribution)
    return [
        {"name": item.name, "path": item.path, "persistent": item.persistent}
        for item in paths
    ]


def _compile_error(message: str) -> ValueError:
    # Import lazily because ``harnesses.common`` uses this module while it is
    # being imported by the harness package.
    from .harnesses.common import HarnessCompileError

    return HarnessCompileError(message)


def _environment_defaults(
    slug: str, distribution: Mapping[str, object] | None
) -> dict[str, str]:
    defaults = _ENGINE_ENVIRONMENT.get(slug)
    if defaults is None:
        raise _compile_error(
            f"runtime writable-path contract is unavailable: {slug}"
        )
    if _is_mia_vllm_distribution(slug, distribution):
        return {**defaults, **_MIA_VLLM_ENVIRONMENT}
    return defaults


def _is_mia_vllm_distribution(
    slug: str, distribution: Mapping[str, object] | None
) -> bool:
    if slug != "vllm" or not isinstance(distribution, Mapping):
        return False
    identity = distribution.get("identity")
    capabilities = distribution.get("capabilities")
    distributed_vllm = (
        capabilities.get("distributed_vllm")
        if isinstance(capabilities, Mapping)
        else None
    )
    return (
        isinstance(identity, Mapping)
        and identity.get("publisher") == "anemll"
        and identity.get("slug") == "anemll-vllm-mia"
        and isinstance(distributed_vllm, Mapping)
        and distributed_vllm.get("verified") is True
        and distributed_vllm.get("mechanism") == "vllm-mp"
        and distributed_vllm.get("topology_mode") == "distributed"
        and distributed_vllm.get("node_count") == 2
        and distributed_vllm.get("world_size") == 2
    )
