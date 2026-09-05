from __future__ import annotations

import pytest
from vonk_control.harnesses.common import HarnessCompileError
from vonk_control.runtime_writable_paths import (
    compile_environment,
    document,
    effective_environment,
    environment,
    telemetry_contract,
    writable_paths,
)


@pytest.mark.parametrize(
    "slug",
    (
        "vllm",
        "sglang",
        "tensorrt-llm",
        "llama-cpp",
        "ds4",
        "diffusers",
        "comfyui",
        "pytorch-pipeline",
    ),
)
def test_engine_contract_is_below_the_writable_output_mount(slug: str) -> None:
    paths = writable_paths(slug)
    assert paths
    assert all(path.source == "outputs" for path in paths)
    assert all(path.path.startswith("/outputs/") for path in paths)
    values = environment(slug, ())
    assert dict(values)["XDG_CACHE_HOME"] == "/outputs/cache"
    assert dict(values)["TMPDIR"] == "/outputs/tmp"
    assert document(slug, values)


def test_vllm_cache_root_is_harness_owned() -> None:
    values = dict(environment("vllm", ()))
    assert values["XDG_CACHE_HOME"] == "/outputs/cache"
    assert values["VLLM_CACHE_ROOT"] == "/outputs/cache/vllm"


def test_harness_contract_does_not_inject_variant_specific_paths() -> None:
    values = dict(compile_environment("vllm", ()))
    assert "FLASHINFER_WORKSPACE_BASE" not in values
    assert values["VLLM_CACHE_ROOT"] == "/outputs/cache/vllm"


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("XDG_CACHE_HOME", "/tmp/cache"),
        ("VLLM_CACHE_ROOT", "/models/cache"),
        ("TRITON_CACHE_DIR", "/var/tmp/triton"),
    ),
)
def test_recipe_cannot_escape_or_conflict_with_runtime_paths(
    name: str, value: str
) -> None:
    with pytest.raises(HarnessCompileError, match="writable path"):
        environment("vllm", ((name, value),))


def test_recipe_cannot_repeat_optional_variant_paths() -> None:
    with pytest.raises(HarnessCompileError, match="platform-owned"):
        environment(
            "vllm", (("FLASHINFER_WORKSPACE_BASE", "/outputs/cache/flashinfer"),)
        )


def test_non_path_recipe_environment_remains_recipe_owned() -> None:
    assert environment("sglang", (("SGLANG_SANITIZE_NAN_LOGITS", "1"),))[0] == (
        "SGLANG_SANITIZE_NAN_LOGITS",
        "1",
    )


@pytest.mark.parametrize(
    ("slug", "adapter", "path"),
    (
        ("vllm", "vllm", "/metrics"),
        ("sglang", "sglang", "/metrics"),
        ("tensorrt-llm", "unsupported", None),
        ("llama-cpp", "llama-cpp", "/metrics"),
        ("ds4", "ds4", "/metrics"),
        ("diffusers", "unsupported", None),
        ("comfyui", "comfyui", "/queue"),
        ("pytorch-pipeline", "unsupported", None),
    ),
)
def test_telemetry_contract_matches_agent_producer(slug: str, adapter: str, path: str | None) -> None:
    contract = telemetry_contract(slug)
    assert (contract.adapter, contract.path) == (adapter, path)
    values = dict(effective_environment(slug, ()))
    assert all(values[name] == value for name, value in contract.environment)


@pytest.mark.parametrize("value", ["0", "1"])
def test_recipe_cannot_override_or_repeat_platform_telemetry(value: str) -> None:
    with pytest.raises(HarnessCompileError, match="telemetry|platform-owned"):
        environment("vllm", (("VLLM_NO_USAGE_STATS", value),))
    with pytest.raises(HarnessCompileError, match="telemetry|platform-owned"):
        effective_environment("vllm", (("VLLM_NO_USAGE_STATS", value),))
    with pytest.raises(HarnessCompileError, match="telemetry|platform-owned"):
        compile_environment("vllm", (("VLLM_NO_USAGE_STATS", value),))
