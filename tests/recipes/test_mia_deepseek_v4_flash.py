from __future__ import annotations

import hashlib
import io
import json
import runpy
import tarfile
from pathlib import Path

from vonk_control.source_bundles import generate_source_bundle
from vonk_control.source_policy import enforce_build_source_policy

ROOT = Path(__file__).resolve().parents[2]
RECIPE_ROOT = ROOT / "config/recipes/development"
CONTEXT = RECIPE_ROOT / "mia-deepseek-v4-flash-context"
UPSTREAM_COMMIT = "f752cd04ab30f2cf42077dd8811a5e1e682d63e7"
MODEL_REVISION = "9e165c30e2704aec5d9d593cce3eebd58bbef1cb"
RUNTIME_IMAGE = (
    "ghcr.io/anemll/dspark-vllm-gx10"
    "@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8"
)
PATCH_SHA256 = {
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


def _document(name: str) -> dict[str, object]:
    return json.loads((RECIPE_ROOT / name).read_text(encoding="utf-8"))


def _canonical_bundle_identity() -> tuple[str, int]:
    files = {
        path.relative_to(CONTEXT).as_posix(): path.read_bytes()
        for path in sorted(CONTEXT.rglob("*"))
        if path.is_file()
    }
    stream = io.BytesIO()
    manifest_files: list[dict[str, object]] = []
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name in sorted(files, key=lambda item: item.encode("utf-8")):
            content = files[name]
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = 0o644
            member.uid = member.gid = 0
            member.uname = member.gname = ""
            member.mtime = 0
            archive.addfile(member, io.BytesIO(content))
            manifest_files.append(
                {
                    "path": name,
                    "mode": 0o644,
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
    canonical = json.dumps(
        {
            "schema_version": 1,
            "files": manifest_files,
            "total_bytes": sum(len(content) for content in files.values()),
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest(), len(stream.getvalue())


def test_recipe_tracks_the_latest_reviewed_official_mia_release() -> None:
    recipe = _document("mia-deepseek-v4-flash.json")
    source = _document("mia-deepseek-v4-flash-source.json")
    artifacts = _document("mia-deepseek-v4-flash-artifacts.json")

    assert source == {
        "schema_version": 1,
        "repository": "https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark",
        "commit": UPSTREAM_COMMIT,
        "runtime_image": RUNTIME_IMAGE,
        "runtime_interface_label": "v1",
        "runtime_user": "10001:10001",
        "license_id": "mia-mit",
    }
    assert recipe["provenance"]["source_reference"].endswith(UPSTREAM_COMMIT)
    assert artifacts["artifacts"] == [
        {
            "id": "model",
            "kind": "huggingface.snapshot",
            "repository": "deepseek-ai/DeepSeek-V4-Flash-0731",
            "revision": MODEL_REVISION,
            "bytes": 166898660330,
        }
    ]


def test_recipe_is_an_exact_two_spark_tensor_parallel_deployment() -> None:
    recipe = _document("mia-deepseek-v4-flash.json")
    profile = recipe["deployment_profiles"]
    assert len(profile) == 1
    profile = profile[0]

    assert profile["name"] == "pair"
    assert profile["node_count"] == 2
    assert profile["strategy"] == "tensor_parallel"
    assert profile["parallelism"] == {
        "tensor": 2,
        "pipeline": 1,
        "data": 1,
        "backend": "mp",
    }
    assert profile["fabric"] == {
        "connectivity": "connected",
        "minimum_bandwidth_mbps": 200000,
    }
    assert sum(role["count"] for role in profile["roles"]) == 2
    assert recipe["runtime"]["endpoint"]["port"] == 8888
    assert recipe["runtime"]["security"] == {
        "devices": ["nvidia.com/gpu=all"],
        "capabilities": [],
        "host_network": True,
        "privileged": False,
        "user": "10001:10001",
        "mounts": [
            {
                "source": "model",
                "target": "/models",
                "read_only": True,
            },
            {
                "source": "state",
                "target": "/state",
                "read_only": False,
            },
        ],
    }


def test_memory_envelope_fits_a_128_gb_spark_with_host_reserve() -> None:
    recipe = _document("mia-deepseek-v4-flash.json")
    topology = _document("mia-deepseek-v4-flash-multinode.json")
    global_agent_floor = 4_000_000_000
    container_limits = set()
    qualified_floors = set()
    for role in recipe["deployment_profiles"][0]["roles"]:
        memory = role["resources"]["memory"]
        container = max(
            memory["startup_peak_bytes"],
            memory["steady_state_bytes"] + memory["runtime_growth_bytes"],
        ) + memory["system_reserve_bytes"]
        container_limits.add(container)
        qualified_floors.add(container + global_agent_floor)

    assert container_limits == {120_000_000_000}
    assert qualified_floors == {topology["minimum_memory_available_bytes"]}


def test_disk_floor_covers_cold_install_staging_rollback_and_margin() -> None:
    recipe = _document("mia-deepseek-v4-flash.json")
    topology = _document("mia-deepseek-v4-flash-multinode.json")
    required = set()
    for role in recipe["deployment_profiles"][0]["roles"]:
        disk = role["resources"]["disk"]
        required.add(
            disk["image_bytes"]
            + recipe["artifacts"][0]["download_bytes"]
            + disk["staging_bytes"]
            + disk["cache_bytes"]
            + disk["rollback_bytes"]
            + disk["safety_margin_bytes"]
        )

    assert required == {topology["minimum_disk_available_bytes"]}


def test_source_context_is_immutable_offline_and_contains_exact_upstream_hotfixes() -> None:
    recipe = _document("mia-deepseek-v4-flash.json")
    source = _document("mia-deepseek-v4-flash-source.json")
    dockerfile = (CONTEXT / "Dockerfile").read_text(encoding="utf-8")
    launcher = (CONTEXT / "mia-deepseek-v4-flash").read_text(encoding="utf-8")
    digest, archive_bytes = _canonical_bundle_identity()
    bundle = generate_source_bundle(
        {
            path.relative_to(CONTEXT).as_posix(): path.read_bytes()
            for path in CONTEXT.rglob("*")
            if path.is_file()
        }
    )
    assert not any(
        path.suffix == ".pyc" or "__pycache__" in path.parts
        for path in CONTEXT.rglob("*")
    )

    assert recipe["build"]["context"] == {
        "sha256": digest,
        "expected_bytes": archive_bytes,
        "media_type": "application/vnd.vonk-forge.source-bundle.v1+tar",
    }
    assert dockerfile.startswith(f"FROM {RUNTIME_IMAGE}\n")
    assert dockerfile.rstrip().endswith("USER 10001:10001\nENTRYPOINT []")
    assert "apt-get" not in dockerfile
    assert "pip install" not in dockerfile
    assert "curl " not in dockerfile
    assert "wget " not in dockerfile
    assert source["runtime_image"] in dockerfile
    assert enforce_build_source_policy(recipe, bundle).passed is True
    assert "groupadd --gid 10001 vonk" in dockerfile
    assert "useradd --uid 10001 --gid 10001" in dockerfile
    assert "--home-dir /state --no-create-home" in dockerfile
    assert "--shell /usr/sbin/nologin" in dockerfile
    assert "export USER=vonk" in launcher
    assert "export LOGNAME=vonk" in launcher
    assert 'export XDG_CACHE_HOME="${state}/cache"' in launcher
    assert 'export TORCHINDUCTOR_CACHE_DIR="${state}/cache/torchinductor"' in launcher
    assert 'export TRITON_CACHE_DIR="${state}/cache/triton"' in launcher

    # Every COPY commits the complete 18.8 GB base through rootless
    # fuse-overlayfs on Spark. Keep the independently permissioned encoding
    # and patch directories, but install the four executable files together.
    assert dockerfile.count("\nCOPY ") <= 3
    assert (
        "COPY --chmod=0755 apply-reasoning-default.py resolve-model.py "
        "resolve-roce.py mia-deepseek-v4-flash /opt/vonk/"
    ) in dockerfile

    for filename, expected_sha256 in PATCH_SHA256.items():
        payload = (CONTEXT / "patches" / filename).read_bytes()
        if filename in {
            "hotfix-dsv4-issue27-partial-prefill-concurrency.py",
            "hotfix-dsv4-issue43-decode-fairness-and-diag.py",
            "hotfix-dsv4-issue55-tool-truncation.py",
        }:
            # Upstream omits these files' final newlines; the source bundle
            # normalizes them while preserving every source character.
            payload = payload.removesuffix(b"\n")
        assert hashlib.sha256(payload).hexdigest() == expected_sha256
        assert filename in dockerfile
    assert (
        dockerfile.index("hotfix-encoding-dsv4-issue21.py")
        < dockerfile.index("hotfix-dsv4-issue31-v2-thinking-budget-gpu.py")
        < dockerfile.index("hotfix-dsv4-issue55-tool-truncation.py")
        < dockerfile.index("hotfix-dsv4-issue27-partial-prefill-concurrency.py")
        < dockerfile.index("hotfix-dsv4-issue43-decode-fairness-and-diag.py")
        < dockerfile.index("hotfix-dsv4-issue26-hybrid-swa-min.py")
    )
    patched_scheduler = (
        "/usr/local/lib/python3.12/dist-packages/vllm/v1/core/sched/scheduler.py"
    )
    assert dockerfile.index(patched_scheduler) > dockerfile.index(
        "hotfix-dsv4-issue43-decode-fairness-and-diag.py"
    )
    encoder = (CONTEXT / "encoding/encoding_dsv4.py").read_bytes()
    assert hashlib.sha256(encoder).hexdigest() == (
        "abc0d26120250dda0ae077dc64aa28836026e61e970854aaeb792445e6a0dde6"
    )

    assert "--tensor-parallel-size 2" in launcher
    assert "--pipeline-parallel-size 1" in launcher
    assert "--kv-cache-dtype nvfp4_ds_mla" in launcher
    assert "--max-model-len 1048576" in launcher
    assert "--distributed-executor-backend mp" in launcher
    assert "--nnodes 2" in launcher
    assert "VONK_RANK" in launcher and "--headless" in launcher
    assert "NCCL_IB_GID_INDEX" in launcher
    assert "ip -o -4 address show" not in launcher
    assert "read -r fabric_interface fabric_hca fabric_gid" in launcher
    assert '/opt/vonk/resolve-roce.py "${VONK_LOCAL_ADDR}"' in launcher
    assert "DSPARK_SUPPRESS_STOPS_IN_REASONING=1" in launcher
    assert "HF_HUB_OFFLINE=1" in launcher
    assert "/opt/vonk/resolve-model.py" in launcher
    for forbidden in ("curl ", "wget ", "git clone", "ssh ", "pip install"):
        assert forbidden not in launcher


def test_roce_resolver_derives_interface_from_the_selected_ipv4(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(CONTEXT / "resolve-roce.py"))
    resolve_roce = namespace["resolve_roce"]
    root = tmp_path / "infiniband"
    port = root / "rocep1s0f1" / "ports" / "1"
    for relative, value in (
        ("gid_attrs/ndevs/3", "enp1s0f1np1\n"),
        ("gid_attrs/types/3", "RoCE v2\n"),
        ("gids/3", "::ffff:192.168.100.10\n"),
    ):
        path = port / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")

    assert resolve_roce(root, "192.168.100.10") == (
        "enp1s0f1np1",
        "rocep1s0f1",
        "3",
    )

    duplicate = root / "rocep2s0f1" / "ports" / "1"
    for relative, value in (
        ("gid_attrs/ndevs/3", "enp2s0f1np1\n"),
        ("gid_attrs/types/3", "RoCE v2\n"),
        ("gids/3", "::ffff:192.168.100.10\n"),
    ):
        path = duplicate / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    assert resolve_roce(root, "192.168.100.10") is None


def test_legacy_mia_adapter_remains_the_accepted_immutable_release() -> None:
    legacy = ROOT / "adapters/deepseek/mia-vllm/runtime-manifest.json"
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == (
        "11fa4d36945ed6530daf29f8b4342feaab90ad9cd47fa505cfd9858a358ebf37"
    )
