"""Central writable-path contract for built-in runtime engines.

Runtime images are deliberately allowed to contain precompiled code and
read-only model data.  Only paths in this module may receive runtime writes.
The contract is emitted by the harness compiler so an image's baked-in ENV or
a recipe-local override cannot silently move writes into the image root.
"""

from __future__ import annotations

import re
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


@dataclass(frozen=True, slots=True)
class EngineTelemetryContract:
    """Telemetry endpoint and privacy settings owned by an engine harness."""

    adapter: str
    path: str | None
    environment: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimeRequirement:
    """A named runtime capability a recipe may request by name.

    A recipe that needs engine scratch space requests the requirement; it never
    authors the host path, the environment value, or the ownership.  The
    platform resolves the name to concrete writable paths below the output
    mount plus the environment names that point at them, so a recipe cannot
    move a cache root and a new engine's scratch space is one reviewed entry
    here rather than another per-recipe list.
    """

    name: str
    paths: tuple[RuntimeWritablePath, ...]
    environment: tuple[tuple[str, str], ...]
    engines: frozenset[str]


# The one environment name a recipe may use to declare runtime requirements.
# The platform consumes and removes it before the runtime environment is
# serialized; it is never forwarded to the engine.
RUNTIME_REQUIREMENT_DECLARATION = "VONK_RUNTIME_REQUIREMENTS"

_RUNTIME_REQUIREMENTS: dict[str, RuntimeRequirement] = {
    "tilelang-cache": RuntimeRequirement(
        name="tilelang-cache",
        paths=(
            RuntimeWritablePath("tilelang", "/outputs/cache/tilelang", True),
            RuntimeWritablePath("tilelang-tmp", "/outputs/tmp/tilelang", False),
        ),
        environment=(
            ("TILELANG_CACHE_DIR", "/outputs/cache/tilelang"),
            ("TILELANG_TMP_DIR", "/outputs/tmp/tilelang"),
        ),
        engines=frozenset({"vllm", "sglang", "tensorrt-llm"}),
    ),
}
_REQUIREMENT_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


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

_VLLM_PATHS = (RuntimeWritablePath("vllm", "/outputs/cache/vllm", True),)

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

# These endpoints mirror the agent's managed-runtime telemetry producer. An
# endpoint is advertised only where that producer has an allowlisted parser.
# The privacy flags are source-backed by the reviewed recipe Dockerfiles and
# wrappers: vLLM's image defaults, SGLang's ``small-dual`` image, ComfyUI's
# recipe contract, and the Hugging Face based media/runtime images.
_ENGINE_TELEMETRY: dict[str, EngineTelemetryContract] = {
    "vllm": EngineTelemetryContract(
        "vllm",
        "/metrics",
        (("HF_HUB_DISABLE_TELEMETRY", "1"), ("VLLM_NO_USAGE_STATS", "1")),
    ),
    "sglang": EngineTelemetryContract(
        "sglang",
        "/metrics",
        (("HF_HUB_DISABLE_TELEMETRY", "1"), ("SGLANG_DISABLE_USAGE_REPORT", "1")),
    ),
    "tensorrt-llm": EngineTelemetryContract("unsupported", None),
    "llama-cpp": EngineTelemetryContract("llama-cpp", "/metrics"),
    "ds4": EngineTelemetryContract(
        "ds4", "/metrics", (("HF_HUB_DISABLE_TELEMETRY", "1"),)
    ),
    "diffusers": EngineTelemetryContract(
        "unsupported", None, (("HF_HUB_DISABLE_TELEMETRY", "1"),)
    ),
    "comfyui": EngineTelemetryContract(
        "comfyui",
        "/queue",
        (("COMFYUI_DISABLE_TELEMETRY", "1"), ("HF_HUB_DISABLE_TELEMETRY", "1")),
    ),
    "pytorch-pipeline": EngineTelemetryContract(
        "unsupported", None, (("HF_HUB_DISABLE_TELEMETRY", "1"),)
    ),
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
_PATH_NAMES = (
    frozenset(
        name for environment in _ENGINE_ENVIRONMENT.values() for name in environment
    )
    | _OPTIONAL_PATH_NAMES
)
_RESERVED_PATH_NAMES = _PATH_NAMES | _OPTIONAL_PATH_NAMES
_TELEMETRY_ENV_NAMES = frozenset(
    name
    for contract in _ENGINE_TELEMETRY.values()
    for name, _value in contract.environment
)


def writable_paths(
    slug: str, requirements: Iterable[str] = ()
) -> tuple[RuntimeWritablePath, ...]:
    """Return the writable directories owned by the platform harness.

    The engine contract comes first and is never optional.  Declared
    requirements append their resolved paths, so every consumer sees the exact
    same ordered set the compiler validated.
    """
    base = _ENGINE_PATHS.get(slug)
    if base is None:
        raise _compile_error(f"runtime writable-path contract is unavailable: {slug}")
    resolved, _environment = _requirement_resolution(slug, tuple(requirements))
    return (*base, *resolved)


def resolve_requirements(
    slug: str, declared: Iterable[str]
) -> tuple[tuple[RuntimeWritablePath, ...], tuple[tuple[str, str], ...]]:
    """Resolve declared requirement names to platform paths and environment."""
    return _requirement_resolution(slug, tuple(declared))


def split_requirements(
    supplied: Iterable[tuple[str, str]],
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """Separate a recipe's requirement declaration from its environment.

    The declaration is returned in authored order and removed from the
    environment the runtime receives.  A repeated or malformed declaration is
    rejected rather than merged.
    """
    declared: list[str] = []
    remaining: list[tuple[str, str]] = []
    seen = False
    for name, value in supplied:
        if name != RUNTIME_REQUIREMENT_DECLARATION:
            remaining.append((name, value))
            continue
        if seen:
            raise _compile_error("runtime requirement declaration is repeated")
        seen = True
        for item in value.split(","):
            requirement = item.strip()
            if _REQUIREMENT_NAME.fullmatch(requirement) is None:
                raise _compile_error(
                    f"runtime requirement name is invalid: {requirement}"
                )
            declared.append(requirement)
    if len(set(declared)) != len(declared):
        raise _compile_error("runtime requirement declaration is repeated")
    return tuple(declared), tuple(remaining)


def _requirement_resolution(
    slug: str, declared: tuple[str, ...]
) -> tuple[tuple[RuntimeWritablePath, ...], tuple[tuple[str, str], ...]]:
    if not declared:
        return (), ()
    if slug not in _ENGINE_PATHS:
        raise _compile_error(f"runtime writable-path contract is unavailable: {slug}")
    paths: list[RuntimeWritablePath] = []
    environment: list[tuple[str, str]] = []
    for name in declared:
        requirement = _RUNTIME_REQUIREMENTS.get(name)
        if requirement is None:
            raise _compile_error(f"runtime requirement is unavailable: {name}")
        if slug not in requirement.engines:
            raise _compile_error(
                f"runtime requirement is not supported by this engine: {name}"
            )
        paths.extend(requirement.paths)
        environment.extend(requirement.environment)
    return tuple(paths), tuple(environment)


def telemetry_contract(slug: str) -> EngineTelemetryContract:
    try:
        return _ENGINE_TELEMETRY[slug]
    except KeyError as exc:
        raise _compile_error(
            f"runtime telemetry contract is unavailable: {slug}"
        ) from exc


def reject_recipe_environment(slug: str, supplied: Iterable[tuple[str, str]]) -> None:
    """Reject reserved path variables before the platform adds its defaults."""
    if slug not in _ENGINE_ENVIRONMENT:
        raise _compile_error(f"runtime writable-path contract is unavailable: {slug}")
    for name, _value in supplied:
        if name in _TELEMETRY_ENV_NAMES:
            raise _compile_error(
                f"runtime telemetry variable is platform-owned: {name}"
            )
        if name in _RESERVED_PATH_NAMES:
            raise _compile_error(
                f"runtime writable path variable is platform-owned: {name}"
            )


def environment(
    slug: str, supplied: Iterable[tuple[str, str]]
) -> tuple[tuple[str, str], ...]:
    """Return defaults and reject recipe ownership of reserved path names."""
    supplied = tuple(supplied)
    reject_recipe_environment(slug, supplied)
    return _merge_environment(slug, supplied, allow_reserved=False)


def effective_environment(
    slug: str,
    supplied: Iterable[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    supplied = tuple(supplied)
    reject_recipe_environment(slug, supplied)
    return _merge_environment(slug, supplied, allow_reserved=True)


def compile_environment(
    slug: str,
    supplied: Iterable[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    """Compile recipe declarations while rejecting platform-owned telemetry."""
    supplied = tuple(supplied)
    reject_recipe_environment(slug, supplied)
    return _merge_environment(slug, supplied, allow_reserved=True)


def materialize_environment(
    slug: str,
    supplied: Iterable[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    """Materialize an already-centralized projection for runtime serialization."""
    return _merge_environment(slug, supplied, allow_reserved=True)


def _merge_environment(
    slug: str,
    supplied: Iterable[tuple[str, str]],
    *,
    allow_reserved: bool,
) -> tuple[tuple[str, str], ...]:
    defaults = {
        **_environment_defaults(slug),
        **dict(telemetry_contract(slug).environment),
    }
    if defaults is None:
        raise _compile_error(f"runtime writable-path contract is unavailable: {slug}")
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
        if (
            actual is not None
            and not allow_reserved
            and name not in _TELEMETRY_ENV_NAMES
        ):
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
) -> None:
    expected = writable_paths(slug)
    actual = tuple(paths)
    # The engine contract is mandatory and ordered; only reviewed requirement
    # paths may follow it.  A recipe cannot reorder, drop, or add to the base.
    if actual[: len(expected)] != expected:
        raise _compile_error(
            "runtime writable paths are not the central engine contract"
        )
    _validate_requirement_paths(actual[len(expected) :], env)
    if len({item.name for item in actual}) != len(actual) or len(
        {item.path for item in actual}
    ) != len(actual):
        raise _compile_error("runtime writable paths are repeated")
    known = {item.path for item in actual}
    for item in actual:
        if (
            type(item.name) is not str
            or not item.name
            or type(item.persistent) is not bool
            or item.source != "outputs"
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


def _validate_requirement_paths(
    extras: tuple[RuntimeWritablePath, ...], env: Mapping[str, str]
) -> None:
    """Accept only the exact resolved paths of declared requirements."""
    remaining = set(extras)
    for requirement in _RUNTIME_REQUIREMENTS.values():
        present = [path for path in requirement.paths if path in remaining]
        if not present:
            continue
        if len(present) != len(requirement.paths):
            raise _compile_error("runtime requirement paths are incomplete")
        for name, value in requirement.environment:
            if env.get(name) != value:
                raise _compile_error(
                    f"runtime requirement environment is incomplete: {name}"
                )
        remaining.difference_update(requirement.paths)
    if remaining:
        raise _compile_error(
            "runtime writable paths are not the central engine contract"
        )


def validate_telemetry(
    slug: str, telemetry: EngineTelemetryContract, env: Mapping[str, str]
) -> None:
    expected = telemetry_contract(slug)
    if telemetry != expected:
        raise _compile_error("runtime telemetry is not the central engine contract")
    for name, value in expected.environment:
        if env.get(name) != value:
            raise _compile_error(f"runtime telemetry environment is incomplete: {name}")


def document(
    slug: str,
    env: Iterable[tuple[str, str]],
    paths: Iterable[RuntimeWritablePath] | None = None,
) -> list[dict[str, object]]:
    values = dict(env)
    resolved = writable_paths(slug) if paths is None else tuple(paths)
    validate_paths(slug, resolved, values)
    return [
        {"name": item.name, "path": item.path, "persistent": item.persistent}
        for item in resolved
    ]


def _compile_error(message: str) -> ValueError:
    # Import lazily because ``harnesses.common`` uses this module while it is
    # being imported by the harness package.
    from .harnesses.common import HarnessCompileError

    return HarnessCompileError(message)


def _environment_defaults(slug: str) -> dict[str, str]:
    defaults = _ENGINE_ENVIRONMENT.get(slug)
    if defaults is None:
        raise _compile_error(f"runtime writable-path contract is unavailable: {slug}")
    return defaults
