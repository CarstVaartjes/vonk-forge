from __future__ import annotations

import importlib.util
import json
import os
import sys
import tarfile
from pathlib import Path

import pytest
from vonk_control.recipe_runtime_specs import compile_runtime_spec

ROOT = Path(__file__).resolve().parents[2]
LIBRARY_ROOT = Path(
    os.environ.get("VONK_RECIPE_LIBRARY_ROOT", ROOT.parent / "vonk-forge-recipes")
)


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _recipe() -> dict[str, object]:
    return _load(
        LIBRARY_ROOT / "recipes/deepseek-v4-flash-0731-mia-dual.json"
    )


def _model() -> dict[str, object]:
    reference = _recipe()["models"][0]["model"]
    assert isinstance(reference, dict)
    return _load(LIBRARY_ROOT / "models" / f"{reference['slug']}.json")


def _package_handle() -> dict[str, object]:
    package_path = (
        LIBRARY_ROOT / "packages" / "deepseek-v4-flash-0731-mia-dual.tar.gz"
    )
    with tarfile.open(package_path, mode="r:gz") as archive:
        manifest = json.loads(archive.extractfile("manifest.json").read())
    paths = [item["path"] for item in manifest["files"]]
    paths.append("adapters/deepseek/mia-vllm")
    digest = "d" * 64
    return {
        "image_reference": f"localhost/vonk/mia@sha256:{digest}",
        "image_digest": digest,
        "paths": paths,
    }


def test_mia_recipe_uses_the_canonical_library_contract() -> None:
    recipe = _recipe()

    assert recipe["schema_version"] == 2
    assert recipe["identity"] == {
        "publisher": "vonk-forge",
        "slug": "deepseek-v4-flash-0731-mia-dual",
    }
    assert recipe["runtime"]["engine"] == "vllm"
    assert recipe["topology"]["mode"] == "distributed"
    assert recipe["topology"]["node_count"] == 2
    assert recipe["topology"]["parallelism"]["world_size"] == 2
    assert recipe["execution"]["build"]["context"]["path"] == (
        "adapters/deepseek/mia-vllm"
    )


def test_mia_model_projection_preserves_exact_source_and_files() -> None:
    recipe = _recipe()
    model = _model()
    reference = recipe["models"][0]["model"]
    assert model["identity"]["slug"] == reference["slug"]
    assert model["source"]["repository"] == (
        "https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-0731"
    )
    assert len(model["source"]["revision"]) == 40
    assert all(c in "0123456789abcdef" for c in model["source"]["revision"])
    selected_files = recipe["models"][0]["files"]
    assert selected_files
    assert {item["file_id"] for item in selected_files} <= {
        item["id"] for item in model["files"]
    }


def test_mia_recipe_delegates_writable_paths_to_the_platform() -> None:
    reserved = {
        "FLASHINFER_WORKSPACE_BASE",
        "TILELANG_CACHE_DIR",
        "TRITON_CACHE_DIR",
        "B12X_CUTE_COMPILE_CACHE_DIR",
        "TORCH_FR_DUMP_TEMP_FILE",
        "TORCH_NCCL_DEBUG_INFO_PIPE_FILE",
    }
    assert not reserved.intersection(
        item["name"] for item in _recipe()["runtime"]["environment"]
    )


def test_mia_runtime_spec_binds_model_package_and_fabric_authority() -> None:
    recipe = _recipe()
    spec = compile_runtime_spec(
        recipe,
        models=[_model()],
        package_handle=_package_handle(),
        parameters={},
        role="worker",
        rank=1,
    )

    assert spec["security"]["mounts"] == [
        {"source": "/run/vonk/models/primary", "target": "/models", "read_only": True},
        {"source": "/run/vonk/outputs", "target": "/outputs", "read_only": False},
    ]
    assert spec["runtime"]["placement_environment"] == {
        "local_address": "VONK_LOCAL_ADDR",
        "master_address": "VONK_MASTER_ADDR",
        "master_port": "VONK_MASTER_PORT",
    }
    assert spec["security"]["host_network"] is False
    assert spec["security"]["read_only_root"] is True


def test_mia_wrapper_consumes_complete_placement_and_fabric_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrapper = ROOT / "adapters/deepseek/mia-vllm/vllm-wrapper.py"
    module_spec = importlib.util.spec_from_file_location("mia_vllm_wrapper", wrapper)
    assert module_spec is not None and module_spec.loader is not None
    observed: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    environment = {
        "VONK_LOCAL_ADDR": "192.168.100.11",
        "VONK_MASTER_ADDR": "192.168.100.10",
        "VONK_MASTER_PORT": "25000",
        "NCCL_SOCKET_IFNAME": "=enp1s0f1np1,enP2p1s0f1np1",
        "NCCL_IB_HCA": "=rocep1s0f1:1,roceP2p1s0f1:1",
        "NCCL_IB_GID_INDEX": "3",
        "TP_SOCKET_IFNAME": "enp1s0f1np1,enP2p1s0f1np1",
        "GLOO_SOCKET_IFNAME": "enp1s0f1np1,enP2p1s0f1np1",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        sys, "argv", [str(wrapper), "serve", "/models", "--nnodes", "2"]
    )

    def capture(executable: str, arguments: tuple[str, ...]) -> None:
        observed.append((executable, arguments))
        raise RuntimeError("exec captured")

    monkeypatch.setattr(os, "execv", capture)
    module = importlib.util.module_from_spec(module_spec)
    with pytest.raises(RuntimeError, match="exec captured"):
        module_spec.loader.exec_module(module)

    assert os.environ["VLLM_HOST_IP"] == "192.168.100.11"
    assert observed[0][1][-4:] == (
        "--master-addr",
        "192.168.100.10",
        "--master-port",
        "25000",
    )


@pytest.mark.parametrize(
    "missing",
    [
        "VONK_LOCAL_ADDR",
        "VONK_MASTER_ADDR",
        "VONK_MASTER_PORT",
        "NCCL_SOCKET_IFNAME",
        "NCCL_IB_HCA",
        "NCCL_IB_GID_INDEX",
        "TP_SOCKET_IFNAME",
        "GLOO_SOCKET_IFNAME",
    ],
)
def test_mia_wrapper_refuses_incomplete_distributed_launch(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    wrapper = ROOT / "adapters/deepseek/mia-vllm/vllm-wrapper.py"
    module_spec = importlib.util.spec_from_file_location(
        f"mia_vllm_wrapper_missing_{missing}", wrapper
    )
    assert module_spec is not None and module_spec.loader is not None
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    for name in (
        "VONK_LOCAL_ADDR",
        "VONK_MASTER_ADDR",
        "VONK_MASTER_PORT",
        "NCCL_SOCKET_IFNAME",
        "NCCL_IB_HCA",
        "NCCL_IB_GID_INDEX",
        "TP_SOCKET_IFNAME",
        "GLOO_SOCKET_IFNAME",
    ):
        monkeypatch.setenv(name, "3" if name == "NCCL_IB_GID_INDEX" else "test")
    monkeypatch.delenv(missing)
    monkeypatch.setattr(
        sys, "argv", [str(wrapper), "serve", "/models", "--nnodes", "2"]
    )
    module = importlib.util.module_from_spec(module_spec)

    with pytest.raises(SystemExit, match="distributed vLLM requires"):
        module_spec.loader.exec_module(module)
