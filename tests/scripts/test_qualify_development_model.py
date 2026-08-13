from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config/recipes/development"


def _script(name: str):
    path = ROOT / "scripts" / name
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


QUALIFIER = _script("qualify-development-model")
SLICES = _script("run-development-slices")


def _evidence(source: dict[str, object], artifacts: dict[str, object]) -> dict[str, object]:
    observed_artifacts = []
    for item in artifacts["artifacts"]:
        observed = {
            "id": item["id"],
            "revision": item["revision"],
            "bytes": item["bytes"],
        }
        if item["kind"] == "huggingface.snapshot":
            observed["repository"] = item["repository"]
        else:
            observed["sha256"] = item["revision"].removeprefix("sha256:")
        observed_artifacts.append(observed)
    nodes = []
    for index, (management, fabric) in enumerate(
        (("192.168.1.211/24", "192.168.100.10/24"), ("192.168.1.212/24", "192.168.100.11/24")),
        start=1,
    ):
        nodes.append(
            {
                "node_id": f"spk_{index:032x}",
                "hostname": f"dgx-spark-{index}",
                "architecture": "aarch64",
                "os_id": "ubuntu",
                "os_version": "24.04",
                "gpu": "NVIDIA GB10",
                "compute_capability": "12.1",
                "cuda_codes": ["sm_121"],
                "podman_rootless": True,
                "docker_gpu_runtime": True,
                "memory_available_bytes": 126_000_000_000,
                "disk_available_bytes": 3_000_000_000_000,
                "runtime_image": source["runtime_image"],
                "artifacts": observed_artifacts,
                "management_address": management,
                "fabric": [
                    {
                        "address": fabric,
                        "state": "active",
                        "bandwidth_mbps": 200_000,
                    }
                ],
            }
        )
    return {
        "schema_version": 1,
        "runtime_image": {
            "reference": source["runtime_image"],
            "platforms": ["linux/arm64"],
            "public_pull": True,
            "runtime_interface_label": "v1",
            "user": "10001:10001",
        },
        "accepted_licenses": artifacts["license_ids"],
        "nodes": nodes,
    }


def test_qualification_accepts_immutable_huggingface_snapshot_recipe(tmp_path: Path) -> None:
    source = json.loads(
        (CONFIG / "mia-deepseek-v4-flash-source.json").read_text(encoding="utf-8")
    )
    artifacts = json.loads(
        (CONFIG / "mia-deepseek-v4-flash-artifacts.json").read_text(encoding="utf-8")
    )
    topology = json.loads(
        (CONFIG / "mia-deepseek-v4-flash-multinode.json").read_text(encoding="utf-8")
    )
    evidence = _evidence(source, artifacts)

    result = QUALIFIER.qualify(source, artifacts, topology, evidence)

    assert result["status"] == "qualified"
    assert result["runtime_image"] == source["runtime_image"]
    assert result["multinode_nodes"] == [
        "spk_00000000000000000000000000000001",
        "spk_00000000000000000000000000000002",
    ]


def test_slice_qualification_uses_the_selected_recipe_sidecars(tmp_path: Path) -> None:
    source = json.loads(
        (CONFIG / "mia-deepseek-v4-flash-source.json").read_text(encoding="utf-8")
    )
    artifacts = json.loads(
        (CONFIG / "mia-deepseek-v4-flash-artifacts.json").read_text(encoding="utf-8")
    )
    topology = json.loads(
        (CONFIG / "mia-deepseek-v4-flash-multinode.json").read_text(encoding="utf-8")
    )
    recipe_path = CONFIG / "mia-deepseek-v4-flash.json"
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    qualification = QUALIFIER.qualify(
        source, artifacts, topology, _evidence(source, artifacts)
    )
    qualification_path = tmp_path / "qualification.json"
    qualification_path.write_bytes(QUALIFIER._canonical(qualification))
    qualification_path.chmod(0o600)

    observed, digest = SLICES._model_qualification(
        qualification_path,
        phase="model-multinode",
        builder_node="spk_00000000000000000000000000000001",
        target_nodes=[
            "spk_00000000000000000000000000000001",
            "spk_00000000000000000000000000000002",
        ],
        recipe=recipe,
        recipe_path=recipe_path,
        source_context=CONFIG / "mia-deepseek-v4-flash-context",
    )

    assert observed == qualification
    assert len(digest) == 64


def test_qualification_preserves_the_existing_digest_pinned_http_recipe() -> None:
    source = json.loads(
        (CONFIG / "model-smoke-source.json").read_text(encoding="utf-8")
    )
    artifacts = json.loads(
        (CONFIG / "model-smoke-artifacts.json").read_text(encoding="utf-8")
    )
    topology = json.loads(
        (CONFIG / "model-smoke-multinode.json").read_text(encoding="utf-8")
    )

    result = QUALIFIER.qualify(source, artifacts, topology, _evidence(source, artifacts))

    assert result["status"] == "qualified"
