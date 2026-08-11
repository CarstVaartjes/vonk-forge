from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
QUALIFIER = ROOT / "scripts" / "qualify-development-model"
NODE_1 = "spk_0123456789abcdef0123456789abcdef"
NODE_2 = "spk_fedcba9876543210fedcba9876543210"
IMAGE = (
    "ghcr.io/carstvaartjes/spark-ds4@"
    "sha256:084d9a9ffa47431842c5dec84de97b058034dec0535b2a563bc5db78c9e14615"
)
ARTIFACTS = [
    {
        "id": "base",
        "kind": "http.file",
        "repository": (
            "https://huggingface.co/antirez/deepseek-v4-gguf/resolve/"
            "1cd7b564460821938add0475a60b942c409295e0/"
            "DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-"
            "imatrix-0731.gguf?download=true"
        ),
        "revision": (
            "sha256:ca22ae2f838e14077c22bc1c1417b71b45b5e5a3687bd96c2ac6e17fdb6261c0"
        ),
        "bytes": 86_720_111_488,
    },
    {
        "id": "drafter",
        "kind": "http.file",
        "repository": (
            "https://huggingface.co/bleysg/DeepSeek-V4-Flash-DSpark-drafter-"
            "GGUF/resolve/81c6fdd38f9582da45ba27f0ed7b63bcd3ea3b62/"
            "DSpark-drafter-Q2K-Q8-0731.gguf?download=true"
        ),
        "revision": (
            "sha256:8fa269560dc76fd73e4233ad9b1938b5f65dd363381fd9b1a5c6183f7d12d686"
        ),
        "bytes": 6_971_241_504,
    },
]


def _inputs(tmp_path: Path) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    source = {
        "schema_version": 1,
        "repository": "https://github.com/Entrpi/ds4",
        "commit": "4ad370b4a338efe9723a386673c0e04f6e214108",
        "archive_sha256": (
            "7db338d0a441fed36c5e4e7af44ff670e8bfe567e88d482f00ff6a3dc0e5dbe3"
        ),
        "runtime_image": IMAGE,
        "runtime_interface_label": "v1",
        "runtime_user": "10001:10001",
        "license_id": "ds4-mit",
    }
    artifacts = {
        "schema_version": 1,
        "license_ids": ["deepseek-model", "ds4-mit"],
        "artifacts": copy.deepcopy(ARTIFACTS),
    }
    topology = {
        "schema_version": 1,
        "architecture": "aarch64",
        "os_id": "ubuntu",
        "os_version": "24.04",
        "gpu": "NVIDIA GB10",
        "compute_capability": "12.1",
        "cuda_code": "sm_121",
        "minimum_memory_available_bytes": 120_000_000_000,
        "minimum_disk_available_bytes": 120_000_000_000,
        "minimum_fabric_bandwidth_mbps": 200_000,
        "single_nodes": 1,
        "multinode_nodes": 2,
    }
    return source, artifacts, topology


def _node(node_id: str, hostname: str, management: str, fabric: str) -> dict[str, object]:
    return {
        "node_id": node_id,
        "hostname": hostname,
        "architecture": "aarch64",
        "os_id": "ubuntu",
        "os_version": "24.04",
        "gpu": "NVIDIA GB10",
        "compute_capability": "12.1",
        "cuda_codes": ["sm_121"],
        "podman_rootless": True,
        "memory_available_bytes": 126_000_000_000,
        "disk_available_bytes": 3_000_000_000_000,
        "management_address": f"{management}/24",
        "fabric": [
            {
                "address": f"{fabric}/24",
                "bandwidth_mbps": 200_000,
                "state": "active",
            }
        ],
        "runtime_image": IMAGE,
        "artifacts": [
            {
                "id": item["id"],
                "revision": item["revision"],
                "sha256": str(item["revision"]).removeprefix("sha256:"),
                "bytes": item["bytes"],
            }
            for item in ARTIFACTS
        ],
    }


def _evidence() -> dict[str, object]:
    return {
        "schema_version": 1,
        "runtime_image": {
            "reference": IMAGE,
            "platforms": ["linux/arm64"],
            "runtime_interface_label": "v1",
            "user": "10001:10001",
            "public_pull": True,
        },
        "accepted_licenses": ["deepseek-model", "ds4-mit"],
        "nodes": [
            _node(NODE_1, "spark-one", "192.168.1.211", "192.168.100.10"),
            _node(NODE_2, "spark-two", "192.168.1.212", "192.168.100.11"),
        ],
    }


def _write(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _run(
    tmp_path: Path,
    *,
    source: dict[str, object] | None = None,
    artifacts: dict[str, object] | None = None,
    topology: dict[str, object] | None = None,
    evidence: dict[str, object] | None = None,
) -> subprocess.CompletedProcess[str]:
    default_source, default_artifacts, default_topology = _inputs(tmp_path)
    output = tmp_path / "qualification.json"
    return subprocess.run(
        [
            str(QUALIFIER),
            "--source",
            str(_write(tmp_path / "source.json", source or default_source)),
            "--artifacts",
            str(_write(tmp_path / "artifacts.json", artifacts or default_artifacts)),
            "--topology",
            str(_write(tmp_path / "topology.json", topology or default_topology)),
            "--evidence",
            str(_write(tmp_path / "evidence.json", evidence or _evidence())),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_qualifier_emits_canonical_identity_bound_output(tmp_path: Path) -> None:
    completed = _run(tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    output = tmp_path / "qualification.json"
    raw = output.read_bytes()
    document = json.loads(raw)
    assert raw == (
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    assert document["status"] == "qualified"
    assert document["runtime_image"] == IMAGE
    assert document["single_node"] == NODE_1
    assert document["multinode_nodes"] == [NODE_1, NODE_2]
    assert document["artifact_set_sha256"]
    assert document["evidence_sha256"]


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda value: value["nodes"][0].update(architecture="x86_64"), "node.architecture"),
        (
            lambda value: value["runtime_image"].update(platforms=["linux/amd64"]),
            "image.arm64_manifest",
        ),
        (
            lambda value: value["runtime_image"].update(public_pull=False),
            "image.public_pull",
        ),
        (
            lambda value: value["nodes"][0].update(cuda_codes=["sm_120"]),
            "node.cuda_code",
        ),
        (
            lambda value: value["nodes"][0]["artifacts"][0].update(sha256="0" * 64),
            "artifact.identity",
        ),
        (
            lambda value: value["nodes"][0].update(memory_available_bytes=119_999_999_999),
            "node.memory",
        ),
        (
            lambda value: value["nodes"][0].update(disk_available_bytes=119_999_999_999),
            "node.disk",
        ),
        (
            lambda value: value["nodes"][0].update(fabric=[]),
            "node.fabric",
        ),
        (
            lambda value: value["nodes"][0].update(fabric=[{
                "address": "192.168.1.99/24",
                "bandwidth_mbps": 200_000,
                "state": "active",
            }]),
            "node.fabric_overlap",
        ),
        (
            lambda value: value.update(accepted_licenses=["ds4-mit"]),
            "license.acknowledgement",
        ),
        (
            lambda value: value["nodes"][1].update(node_id=NODE_1),
            "node.identity",
        ),
        (
            lambda value: value["nodes"][1].update(
                fabric=[
                    {
                        "address": "192.168.200.11/24",
                        "bandwidth_mbps": 200_000,
                        "state": "active",
                    }
                ]
            ),
            "node.fabric_identity",
        ),
        (
            lambda value: value["nodes"][1].update(runtime_image=IMAGE.replace("084d", "184d")),
            "image.identity",
        ),
    ],
)
def test_qualifier_rejects_unsafe_or_inconsistent_evidence(
    tmp_path: Path, mutation, reason: str
) -> None:
    evidence = _evidence()
    mutation(evidence)

    completed = _run(tmp_path, evidence=evidence)

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr.strip() == f"qualification refused: {reason}"
    assert not (tmp_path / "qualification.json").exists()


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("commit", "main", "source.commit"),
        ("runtime_image", "ghcr.io/carstvaartjes/spark-ds4:dev", "source.runtime_image"),
    ],
)
def test_qualifier_rejects_mutable_source_or_image_refs(
    tmp_path: Path, field: str, value: str, reason: str
) -> None:
    source, artifacts, topology = _inputs(tmp_path)
    source[field] = value

    completed = _run(
        tmp_path, source=source, artifacts=artifacts, topology=topology
    )

    assert completed.returncode == 2
    assert completed.stderr.strip() == f"qualification refused: {reason}"


def test_qualifier_rejects_mutable_model_revision(tmp_path: Path) -> None:
    source, artifacts, topology = _inputs(tmp_path)
    artifacts["artifacts"][0]["revision"] = "main"

    completed = _run(
        tmp_path, source=source, artifacts=artifacts, topology=topology
    )

    assert completed.returncode == 2
    assert completed.stderr.strip() == "qualification refused: artifact.revision"


def test_repository_model_documents_qualify_and_share_exact_identities(
    tmp_path: Path,
) -> None:
    source = json.loads(
        (ROOT / "config/recipes/development/model-smoke-source.json").read_text()
    )
    artifacts = json.loads(
        (ROOT / "config/recipes/development/model-smoke-artifacts.json").read_text()
    )
    topology = json.loads(
        (ROOT / "config/recipes/development/model-smoke-multinode.json").read_text()
    )
    recipe = json.loads(
        (ROOT / "config/recipes/development/model-smoke.json").read_text()
    )

    assert recipe["artifacts"] == [
        {
            "id": item["id"],
            "kind": item["kind"],
            "repository": item["repository"],
            "revision": item["revision"],
            "download_bytes": item["bytes"],
            "installed_bytes": item["bytes"],
            "mount": {"target": "/models", "read_only": True},
            "roles": ["entrypoint", "worker"],
        }
        for item in artifacts["artifacts"]
    ]
    assert source["runtime_image"].startswith("ghcr.io/")
    assert topology["single_nodes"] == 1
    assert topology["multinode_nodes"] == 2
