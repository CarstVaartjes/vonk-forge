from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "control/src"))

from vonk_control.catalog_contract import (
    catalog_content_sha256,
    validate_catalog_document,
)
from vonk_control.harnesses import HarnessRegistry
from vonk_control.harnesses.common import HarnessCompileError
from vonk_control.harnesses.vllm import VllmHarnessCompiler
from vonk_control.recipe_contract import validate_recipe
from vonk_control.recipe_runtime_specs import compile_runtime_spec
from vonk_control.source_bundles import generate_source_bundle
from vonk_control.source_policy import enforce_build_source_policy

KIND_ROOT = {
    "model-group": "model-groups",
    "model": "models",
    "model-version": "model-versions",
    "execution-harness": "execution-harnesses",
    "runtime-distribution": "runtime-distributions",
    "patch-bundle": "patch-bundles",
}
MIA_COMMIT = "f752cd04ab30f2cf42077dd8811a5e1e682d63e7"
MODEL_REVISION = "62af8fffb2f7030cac4de2f0169f5b8d1101b646"
ANEMLL_COMMIT = "47503f8e38dadd4dededca798150db2619594fce"
IMAGE = (
    "ghcr.io/anemll/dspark-vllm-gx10"
    "@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8"
)
PATCH_SHA256 = {
    "apply-reasoning-default.py": "f42787340f6c115ead869cdf5076efc45c4e3f54a4c1c04cbcd2de8828aa1947",
    "hotfix-encoding-dsv4-issue21.py": "1a74f6c4ec6a2b7cd2ff01f19b52fbf4ced980a22f08b9d75a6aae1bff0d0548",
    "hotfix-dsv4-issue31-v2-thinking-budget-gpu.py": "7e6ee3e6852dc4003a5d9e7f1c62e316010858722ff3644467e1f4db57d2d909",
    "hotfix-dsv4-issue55-tool-truncation.py": "53f26da9039eb6d99baa6c141c6ed916b292d406da292a5e762012c5ef423dec",
    "hotfix-nvfp4-ds-mla-issue22.sh": "4999ed58c4c2ca0903bc21fcdb6db50d481396ded62066e4132ea609096b13bf",
    "hotfix-dsv4-mtp-buffer-50312.sh": "18dee7b92db1c6c55983c7a9df4d6c27c5a09d9be2225cd54207837fe94ecfe0",
    "hotfix-dsv4-adaptive-topk-50004.sh": "561a6ebd295964e3a37df07c96259a1a2eb0d7e6aaef5ac5ca73ecb0cebf7493",
    "hotfix-dsv4-skip-topk-49486.sh": "636fd162fefc2a156750027b731a9eb136e7993f2552389adf7e3647c5b4dc7b",
    "hotfix-dsv4-dense-prefill-indexer-48407.sh": "c2fa444ea40af9225f3063b3be3a5827f4cada9b0ddf84e156176a23e99a2e6b",
    "hotfix-dsv4-skip-empty-c128-48957.sh": "dabafb64f9273c37659027706920d175d5ed0a6b0cdd53fb5be784f408d7990e",
    "hotfix-dsv4-flashmla-workspace-50298.sh": "a7f557b264d247fbc65bfe49cc6d05e0780e4c6bebcdaf3633ace55338fa4268",
    "hotfix-dsv4-grammar-advance.sh": "99f5e0d3737a8a074c4c85b7348882a91a4d96a12bcf0d65de4d1c751a4d8abd",
    "hotfix-dsv4-issue27-partial-prefill-concurrency.py": "e87e14a6dc45ccbbdea2940d9594f239f6d8dbda7b82d7a094f45bcaa2dfb450",
    "hotfix-dsv4-issue43-decode-fairness-and-diag.py": "f362f6289fabefd17d41007637e99a503f5b282dbb13b21cd203a3c30b844de6",
    "hotfix-dsv4-issue26-hybrid-swa-min.py": "acdf9aa2705de248333b3ba6ddeb20aea67b5582f408552e407c7a670b20ee82",
    "hotfix-dsv4-suppress-stops-in-reasoning.py": "89df901d5d5853e79d71d48e1f2f1a4302ac688b5e2d3788c8551a7fe8477f21",
}


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _recipe() -> dict[str, object]:
    return _load(ROOT / "config/recipes/deepseek-v4-flash-0731-mia-dual.json")


def _resolve(reference: dict[str, object]) -> dict[str, object]:
    document = _load(
        ROOT
        / "config"
        / KIND_ROOT[str(reference["kind"])]
        / f"{reference['slug']}.json"
    )
    validate_catalog_document(document)
    assert document["identity"]["publisher"] == reference["publisher"]
    assert document["identity"]["slug"] == reference["slug"]
    assert catalog_content_sha256(document) == reference["content_sha256"]
    return document


def test_mia_is_vllm_distribution_plus_patch() -> None:
    recipe = _recipe()
    validate_recipe(recipe)
    harness = _resolve(recipe["execution"]["harness"])
    distribution = _resolve(recipe["runtime"]["distribution"])
    patch = _resolve(recipe["execution"]["patch_bundle"])

    assert harness["compiler_slug"] == "vllm"
    assert harness["topology_modes"] == ["single"]
    assert distribution["identity"]["slug"] == "anemll-vllm-mia"
    assert patch["identity"]["slug"] == "mia-deepseek-v4-flash-0731"
    assert recipe["topology"]["mode"] == "distributed"
    assert recipe["topology"]["node_count"] == 2
    assert recipe["topology"]["parallelism"]["world_size"] == 2


def test_official_model_version_has_complete_exact_74_file_inventory() -> None:
    version = _resolve(_recipe()["model"])
    artifacts = version["artifacts"]

    assert version["source"] == {
        "repository": "https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-DSpark",
        "revision": MODEL_REVISION,
    }
    assert version["format"] == {
        "container": "safetensors",
        "precision": "fp4-fp8-mixed",
        "quantization": "moe-experts-fp4-remaining-fp8",
    }
    assert len(artifacts) == 74
    assert len({artifact["path"] for artifact in artifacts}) == 74
    assert sum(artifact["download_bytes"] for artifact in artifacts) == 166_898_666_055
    assert version["sizes"] == {
        "download_bytes": 166_898_666_055,
        "installed_bytes": 166_898_666_055,
    }
    assert all(artifact["revision"] == MODEL_REVISION for artifact in artifacts)
    assert version["access"] == {
        "visibility": "public",
        "gated": False,
        "authentication": "none",
    }
    assert version["limits"]["context_tokens"] == 1_048_576


def test_anemll_distribution_is_immutable_and_explicitly_verifies_tp2() -> None:
    distribution = _resolve(_recipe()["runtime"]["distribution"])

    assert distribution["source"] == {
        "repository": "https://github.com/Anemll/dspark-vllm-gx10",
        "revision": ANEMLL_COMMIT,
        "archive_sha256": "9b3e1de63857220506201c5416df29260691597e3ae7ed7cf18f532b642803ea",
        "license": "MIT",
    }
    assert distribution["image"] == IMAGE
    assert distribution["image_manifest"] == {
        "digest": "a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8",
        "size": 9530,
        "config_digest": "3430d6614a8e2925f34d059af6caf05aff42387326db4d05639a60f10f2654d8",
        "compressed_layers_bytes": 9_787_494_235,
    }
    capability = distribution["capabilities"]["distributed_vllm"]
    assert {key: value for key, value in capability.items() if key != "launch"} == {
        "verified": True,
        "mechanism": "vllm-mp",
        "topology_mode": "distributed",
        "node_count": 2,
        "world_size": 2,
        "tensor_parallel_size": 2,
        "pipeline_parallel_size": 1,
        "data_parallel_size": 1,
        "fabric": "nccl-roce",
        "endpoint_role": "entrypoint",
        "worker_role": "worker",
        "rank_loss_withdraws_endpoint": True,
    }
    assert capability["launch"] == {
        "rendezvous": {
            "local_address_environment": "VONK_LOCAL_ADDR",
            "master_address_environment": "VONK_MASTER_ADDR",
            "master_port_environment": "VONK_MASTER_PORT",
            "master_role": "entrypoint",
        },
        "rank_profiles": [
            {
                "rank": 0,
                "role": "entrypoint",
                "environment": {
                    "GLOO_SOCKET_IFNAME": "enp1s0f1np1,enP2p1s0f1np1",
                    "NCCL_IB_GID_INDEX": "3",
                    "NCCL_IB_HCA": "=rocep1s0f1:1,roceP2p1s0f1:1",
                    "NCCL_SOCKET_IFNAME": "=enp1s0f1np1,enP2p1s0f1np1",
                    "TP_SOCKET_IFNAME": "enp1s0f1np1,enP2p1s0f1np1",
                },
            },
            {
                "rank": 1,
                "role": "worker",
                "environment": {
                    "GLOO_SOCKET_IFNAME": "enp1s0f1np1,enP2p1s0f1np1",
                    "NCCL_IB_GID_INDEX": "3",
                    "NCCL_IB_HCA": "=rocep1s0f1:1,roceP2p1s0f1:1",
                    "NCCL_SOCKET_IFNAME": "=enp1s0f1np1,enP2p1s0f1np1",
                    "TP_SOCKET_IFNAME": "enp1s0f1np1,enP2p1s0f1np1",
                },
            },
        ],
    }


def test_mia_patch_bundle_is_ordered_hashed_and_build_time_only() -> None:
    recipe = _recipe()
    patch = _resolve(recipe["execution"]["patch_bundle"])
    context = ROOT / "adapters/deepseek/mia-vllm"

    assert patch["source"]["repository"] == (
        "https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark"
    )
    assert patch["source"]["revision"] == MIA_COMMIT
    assert [item["order"] for item in patch["patches"]] == list(
        range(1, len(PATCH_SHA256) + 1)
    )
    assert {Path(item["path"]).name: item["sha256"] for item in patch["patches"]} == (
        PATCH_SHA256
    )
    assert patch["pre_patch_tree_sha256"] != patch["post_patch_tree_sha256"]
    assert recipe["runtime"]["lifecycle"]["pre_start"] == []
    assert recipe["build"]["network"] == {"mode": "none", "hosts": []}

    dockerfile = (context / "Dockerfile").read_text(encoding="utf-8")
    assert dockerfile.startswith(f"FROM {IMAGE}\n")
    assert "ARG MIA_SOURCE_" not in dockerfile
    assert "ARG ANEMLL_SOURCE_" not in dockerfile
    assert "ARG PRE_PATCH_" not in dockerfile
    assert "ARG PATCHED_" not in dockerfile
    assert "apply-build-patches.py" in dockerfile
    assert "verify-patched-tree.py" in dockerfile
    assert 'org.opencontainers.image.licenses="MIT AND Apache-2.0"' in dockerfile
    assert "COPY licenses /opt/vonk/licenses" in dockerfile
    assert {
        path.name for path in (context / "licenses").iterdir() if path.is_file()
    } == {"MiaAI-Lab-LICENSE", "vLLM-LICENSE", "vLLM-NOTICE"}
    assert dockerfile.rstrip().endswith("USER 10001:10001\nENTRYPOINT []")
    patcher = (context / "apply-build-patches.py").read_text(encoding="utf-8")
    for filename, digest in PATCH_SHA256.items():
        assert filename in patcher
        payload = (context / "patches" / filename).read_bytes()
        if filename in {
            "hotfix-dsv4-issue27-partial-prefill-concurrency.py",
            "hotfix-dsv4-issue43-decode-fairness-and-diag.py",
            "hotfix-dsv4-issue55-tool-truncation.py",
        }:
            payload = payload.removesuffix(b"\n")
        assert __import__("hashlib").sha256(payload).hexdigest() == digest

    bundle = generate_source_bundle(
        {
            path.relative_to(context).as_posix(): path.read_bytes()
            for path in sorted(context.rglob("*"))
            if path.is_file()
        }
    )
    assert recipe["build"]["context"]["sha256"] == bundle.sha256
    assert enforce_build_source_policy(recipe, bundle).passed is True


def test_mia_topology_declares_rank_lifecycle_readiness_and_failure() -> None:
    recipe = _recipe()
    topology = recipe["topology"]

    assert [(role["name"], role["count"], role["endpoint_owner"]) for role in topology["roles"]] == [
        ("entrypoint", 1, True),
        ("worker", 1, False),
    ]
    assert topology["start_order"] == ["worker", "entrypoint"]
    assert topology["stop_order"] == ["entrypoint", "worker"]
    assert recipe["runtime"]["lifecycle"]["readiness"] == {
        "strategy": "endpoint-owner-after-all-ranks",
        "path": "/v1/models",
        "timeout_seconds": 900,
    }
    assert recipe["runtime"]["lifecycle"]["failure"] == {
        "rank_loss": "withdraw-endpoint",
        "recovery": "restart-worker-then-entrypoint",
    }


def test_only_verified_distribution_owned_vllm_tp2_compiles() -> None:
    recipe = _recipe()
    harness = _resolve(recipe["execution"]["harness"])
    distribution = _resolve(recipe["runtime"]["distribution"])
    patch = _resolve(recipe["execution"]["patch_bundle"])
    registry = HarnessRegistry.with_builtins()

    worker = registry.compile(
        harness,
        recipe,
        distribution,
        patch,
        {},
        recipe["topology"],
        "worker",
        1,
    )
    entrypoint = registry.compile(
        harness,
        recipe,
        distribution,
        patch,
        {},
        recipe["topology"],
        "entrypoint",
        0,
    )

    for rank, projection in ((1, worker), (0, entrypoint)):
        assert projection.command[0] == "/opt/vonk/bin/vllm"
        assert projection.command[projection.command.index("--nnodes") + 1] == "2"
        assert projection.command[projection.command.index("--node-rank") + 1] == str(rank)
        assert projection.command[projection.command.index("--tensor-parallel-size") + 1] == "2"
        assert projection.command[projection.command.index("--distributed-executor-backend") + 1] == "mp"
        assert "sh" not in projection.command
        assert "-c" not in projection.command

    unverified = copy.deepcopy(distribution)
    unverified["capabilities"]["distributed_vllm"]["verified"] = False
    with pytest.raises(HarnessCompileError, match="runtime distribution"):
        registry.compile(
            harness,
            recipe,
            unverified,
            patch,
            {},
            recipe["topology"],
            "worker",
            1,
        )

    wrong_fabric = copy.deepcopy(distribution)
    wrong_fabric["capabilities"]["distributed_vllm"]["fabric"] = "tcp"
    with pytest.raises(HarnessCompileError, match="verified distributed vLLM distribution"):
        VllmHarnessCompiler().compile(
            recipe,
            wrong_fabric,
            patch,
            {},
            recipe["topology"],
            "worker",
            1,
        )

    missing_launch = copy.deepcopy(distribution)
    missing_launch["capabilities"]["distributed_vllm"].pop("launch", None)
    with pytest.raises(HarnessCompileError, match="launch contract"):
        VllmHarnessCompiler().compile(
            recipe,
            missing_launch,
            patch,
            {},
            recipe["topology"],
            "worker",
            1,
        )


def test_mia_runtime_spec_preserves_verified_host_fabric_authority() -> None:
    recipe = _recipe()
    harness = _resolve(recipe["execution"]["harness"])
    distribution = _resolve(recipe["runtime"]["distribution"])
    patch = _resolve(recipe["execution"]["patch_bundle"])
    spec = compile_runtime_spec(
        recipe,
        resolved_entities={
            "model_version": SimpleNamespace(
                content_sha256=recipe["model"]["content_sha256"]
            ),
            "harness": SimpleNamespace(
                document=harness,
                content_sha256=recipe["execution"]["harness"]["content_sha256"],
            ),
            "runtime_distribution": SimpleNamespace(
                document=distribution,
                content_sha256=recipe["runtime"]["distribution"]["content_sha256"],
            ),
            "patch_bundle": SimpleNamespace(
                document=patch,
                content_sha256=recipe["execution"]["patch_bundle"]["content_sha256"],
            ),
        },
        parameters={},
        role="worker",
        rank=1,
        recipe_build_id="00000000-0000-4000-8000-000000000001",
        image_digest="sha256:" + "d" * 64,
    )

    assert spec["security"]["host_network"] is True
    assert spec["security"]["mounts"] == [
        {"source": "model", "target": "/models", "read_only": True},
        {"source": "outputs", "target": "/outputs", "read_only": False},
    ]
    assert spec["runtime"]["placement_environment"] == {
        "local_address": "VONK_LOCAL_ADDR",
        "master_address": "VONK_MASTER_ADDR",
        "master_port": "VONK_MASTER_PORT",
    }
    environment = {
        item["name"]: item["value"] for item in spec["runtime"]["environment"]
    }
    projected = environment | {
        "VONK_LOCAL_ADDR": "192.168.100.11",
        "VONK_MASTER_ADDR": "192.168.100.10",
        "VONK_MASTER_PORT": "25000",
    }
    expected_fabric = {
        "GLOO_SOCKET_IFNAME": "enp1s0f1np1,enP2p1s0f1np1",
        "NCCL_IB_GID_INDEX": "3",
        "NCCL_IB_HCA": "=rocep1s0f1:1,roceP2p1s0f1:1",
        "NCCL_SOCKET_IFNAME": "=enp1s0f1np1,enP2p1s0f1np1",
        "TP_SOCKET_IFNAME": "enp1s0f1np1,enP2p1s0f1np1",
    }
    assert all(projected.get(name) == value for name, value in expected_fabric.items())


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
    monkeypatch.setattr(sys, "argv", [str(wrapper), "serve", "/models", "--nnodes", "2"])

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
    monkeypatch.setattr(sys, "argv", [str(wrapper), "serve", "/models", "--nnodes", "2"])
    module = importlib.util.module_from_spec(module_spec)

    with pytest.raises(SystemExit, match="distributed vLLM requires"):
        module_spec.loader.exec_module(module)
