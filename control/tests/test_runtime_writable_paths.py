from __future__ import annotations

import pytest

from vonk_control.harnesses.common import HarnessCompileError
from vonk_control.runtime_writable_paths import (
    compile_environment,
    document,
    environment,
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


def test_mia_distribution_enables_only_evidence_backed_optional_paths() -> None:
    distribution = {
        "identity": {"publisher": "anemll", "slug": "anemll-vllm-mia"},
        "capabilities": {
            "distributed_vllm": {
                "verified": True,
                "mechanism": "vllm-mp",
                "topology_mode": "distributed",
                "node_count": 2,
                "world_size": 2,
            },
            "runtime_environment": {
                "allowed_names": [
                    "FLASHINFER_WORKSPACE_BASE",
                    "TILELANG_CACHE_DIR",
                    "B12X_CUTE_COMPILE_CACHE_DIR",
                    "TORCH_FR_DUMP_TEMP_FILE",
                    "TORCH_NCCL_DEBUG_INFO_PIPE_FILE",
                ]
            }
        },
    }
    values = dict(compile_environment("vllm", (), distribution))
    assert values["FLASHINFER_WORKSPACE_BASE"] == "/outputs/cache/flashinfer"
    assert values["B12X_CUTE_COMPILE_CACHE_DIR"] == "/outputs/cache/b12x-cute-compile"
    assert "FLASHINFER_WORKSPACE_BASE" not in dict(compile_environment("vllm", ()))


def test_mia_variant_requires_verified_distributed_capability() -> None:
    distribution = {
        "identity": {"publisher": "anemll", "slug": "anemll-vllm-mia"},
        "capabilities": {
            "distributed_vllm": {
                "verified": False,
                "mechanism": "vllm-mp",
                "topology_mode": "distributed",
                "node_count": 2,
                "world_size": 2,
            }
        },
    }
    values = dict(compile_environment("vllm", (), distribution))
    assert "FLASHINFER_WORKSPACE_BASE" not in values


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
