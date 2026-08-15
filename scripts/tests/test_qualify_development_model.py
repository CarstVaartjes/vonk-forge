from __future__ import annotations

import copy
import importlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
QUALIFIER = ROOT / "scripts" / "qualify-development-model"
MODEL_CONTEXT = ROOT / "config/recipes/development/model-smoke-context"
FABRIC_RENDEZVOUS = MODEL_CONTEXT / "fabric-rendezvous"
NODE_1 = "spk_0123456789abcdef0123456789abcdef"
NODE_2 = "spk_fedcba9876543210fedcba9876543210"
IMAGE = (
    "ghcr.io/carstvaartjes/vonk-forge-workloads@"
    "sha256:96993dcbb8f262c6fbcc41fd005498934b476b040486a6618898d4135b6d0817"
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


def _inputs(
    tmp_path: Path,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
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


def _node(
    node_id: str, hostname: str, management: str, fabric: str
) -> dict[str, object]:
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
        "docker_gpu_runtime": True,
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


def test_checked_recipe_memory_envelope_matches_healthy_spark_qualification() -> None:
    recipe = json.loads(
        (ROOT / "config/recipes/development/model-smoke-pair.json").read_text()
    )
    topology = json.loads(
        (ROOT / "config/recipes/development/model-smoke-multinode.json").read_text()
    )
    global_memory_floor = 4_000_000_000
    required = set()
    for role in recipe["topology"]["roles"]:
        memory = role["resources"]["memory"]
        required.add(
            max(
                memory["startup_peak_bytes"],
                memory["steady_state_bytes"] + memory["runtime_growth_bytes"],
            )
            + memory["system_reserve_bytes"]
            + global_memory_floor
        )

    assert required == {topology["minimum_memory_available_bytes"]}


def _write(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _socketbox(tmp_path: Path) -> Path:
    command = tmp_path / "socketbox"
    command.write_text(
        f"""#!{sys.executable}
import os
import pathlib
import socket
import subprocess
import sys

arguments = sys.argv[1:]
if not arguments or arguments.pop(0) != "nc":
    raise SystemExit(2)
listen = any(value in {{"-l", "-lk"}} for value in arguments)
local = arguments[arguments.index("-s") + 1] if "-s" in arguments else "0.0.0.0"
timeout = int(arguments[arguments.index("-w") + 1]) if "-w" in arguments else None
if listen:
    port = int(arguments[arguments.index("-p") + 1])
    executable = arguments[arguments.index("-e") + 1]
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((local, port))
        server.listen()
        if timeout is not None:
            server.settimeout(timeout)
        while True:
            try:
                connection, _peer = server.accept()
            except TimeoutError:
                raise SystemExit(124)
            with connection:
                peer_log = os.environ.get("VONK_SOCKETBOX_PEER_LOG")
                if peer_log:
                    pathlib.Path(peer_log).write_text(_peer[0] + "\\n", encoding="ascii")
                subprocess.run(
                    [executable],
                    stdin=connection,
                    stdout=connection,
                    stderr=subprocess.PIPE,
                    env=os.environ,
                    check=False,
                )
else:
    if "-s" in arguments:
        print("client source binding is unavailable in rootless networking", file=sys.stderr)
        raise SystemExit(64)
    payload = sys.stdin.buffer.read()
    host = arguments[-2]
    port = int(arguments[-1])
    with socket.socket() as client:
        assert timeout is not None
        client.settimeout(timeout)
        client.connect((host, port))
        client.sendall(payload)
        client.shutdown(socket.SHUT_WR)
        response = bytearray()
        while chunk := client.recv(4096):
            response.extend(chunk)
    sys.stdout.buffer.write(response)
""",
        encoding="utf-8",
    )
    command.chmod(0o755)
    return command


def _unused_port(address: str) -> int:
    with socket.socket() as listener:
        listener.bind((address, 0))
        return int(listener.getsockname()[1])


def _rendezvous_environment(
    tmp_path: Path,
    *,
    rank: int,
    local_address: str,
    port: int,
    timeout_seconds: int = 3,
) -> dict[str, str]:
    return {
        **os.environ,
        "VONK_BUSYBOX": str(_socketbox(tmp_path)),
        "VONK_FABRIC_RENDEZVOUS_SECONDS": str(timeout_seconds),
        "VONK_MASTER_ADDR": "127.0.0.2",
        "VONK_LOCAL_ADDR": local_address,
        "VONK_MASTER_PORT": str(port),
        "VONK_RANK": str(rank),
        "VONK_SOCKETBOX_PEER_LOG": str(tmp_path / "socketbox-peer"),
        "VONK_WORLD_SIZE": "2",
        "VONK_STATE_ROOT": str(tmp_path / f"state-{rank}"),
    }


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
    assert (
        raw
        == (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )
    assert document["status"] == "qualified"
    assert document["runtime_image"] == IMAGE
    assert document["single_node"] == NODE_1
    assert document["multinode_nodes"] == [NODE_1, NODE_2]
    assert document["artifact_set_sha256"]
    assert document["evidence_sha256"]


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda value: value["nodes"][0].update(architecture="x86_64"),
            "node.architecture",
        ),
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
            lambda value: value["nodes"][0].update(docker_gpu_runtime=False),
            "node.spark_docker_nvidia",
        ),
        (
            lambda value: value["nodes"][0]["artifacts"][0].update(sha256="0" * 64),
            "artifact.identity",
        ),
        (
            lambda value: value["nodes"][0].update(
                memory_available_bytes=119_999_999_999
            ),
            "node.memory",
        ),
        (
            lambda value: value["nodes"][0].update(
                disk_available_bytes=119_999_999_999
            ),
            "node.disk",
        ),
        (
            lambda value: value["nodes"][0].update(fabric=[]),
            "node.fabric",
        ),
        (
            lambda value: value["nodes"][0].update(
                fabric=[
                    {
                        "address": "192.168.1.99/24",
                        "bandwidth_mbps": 200_000,
                        "state": "active",
                    }
                ]
            ),
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
            lambda value: value["nodes"][1].update(
                runtime_image=IMAGE.replace("9699", "a699", 1)
            ),
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
        (
            "runtime_image",
            "ghcr.io/carstvaartjes/vonk-forge-workloads:dev",
            "source.runtime_image",
        ),
    ],
)
def test_qualifier_rejects_mutable_source_or_image_refs(
    tmp_path: Path, field: str, value: str, reason: str
) -> None:
    source, artifacts, topology = _inputs(tmp_path)
    source[field] = value

    completed = _run(tmp_path, source=source, artifacts=artifacts, topology=topology)

    assert completed.returncode == 2
    assert completed.stderr.strip() == f"qualification refused: {reason}"


def test_qualifier_rejects_mutable_model_revision(tmp_path: Path) -> None:
    source, artifacts, topology = _inputs(tmp_path)
    artifacts["artifacts"][0]["revision"] = "main"

    completed = _run(tmp_path, source=source, artifacts=artifacts, topology=topology)

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
        (ROOT / "config/recipes/development/model-smoke-pair.json").read_text()
    )
    dockerfile = (MODEL_CONTEXT / "Dockerfile").read_text(encoding="utf-8")
    from_instructions = [
        line.strip()
        for line in dockerfile.splitlines()
        if line.lstrip().startswith("FROM ")
    ]

    assert from_instructions == [f"FROM {IMAGE}"]
    assert all(" AS " not in instruction for instruction in from_instructions)
    assert "COPY --from" not in dockerfile
    assert "COPY --chmod=0755 model-smoke fabric-rendezvous /opt/vonk/" in dockerfile
    recipe_runtime_image = from_instructions[0].removeprefix("FROM ")

    completed = _run(
        tmp_path,
        source=source,
        artifacts=artifacts,
        topology=topology,
    )

    assert completed.returncode == 0, completed.stderr
    qualification = json.loads((tmp_path / "qualification.json").read_text())
    assert (
        recipe_runtime_image
        == source["runtime_image"]
        == qualification["runtime_image"]
        == IMAGE
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
    assert topology["single_nodes"] == 1
    assert topology["multinode_nodes"] == 2


def test_model_pair_lets_rootless_networking_select_source_for_fabric_exchange(
    tmp_path: Path,
) -> None:
    if not FABRIC_RENDEZVOUS.is_file():
        pytest.fail("model pair has no executable fabric rendezvous")
    port = _unused_port("127.0.0.2")
    coordinator = subprocess.Popen(
        [str(FABRIC_RENDEZVOUS), "coordinator"],
        env=_rendezvous_environment(
            tmp_path, rank=0, local_address="127.0.0.2", port=port
        ),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 3
        joined: subprocess.CompletedProcess[str] | None = None
        while time.monotonic() < deadline:
            joined = subprocess.run(
                [str(FABRIC_RENDEZVOUS), "join"],
                env=_rendezvous_environment(
                    tmp_path, rank=1, local_address="127.0.0.3", port=port
                ),
                text=True,
                capture_output=True,
                check=False,
            )
            if joined.returncode == 0:
                break
            time.sleep(0.05)

        assert joined is not None
        assert joined.returncode == 0, joined.stderr
        assert joined.stdout == "fabric rendezvous complete: rank 1 of 2\n"
        marker = tmp_path / "state-0" / "fabric" / "rank-1-ready"
        deadline = time.monotonic() + 3
        while not marker.is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.read_text(encoding="utf-8") == (
            "vonk-fabric-v1 worker rank=1 world=2 address=127.0.0.3\n"
        )
        assert (tmp_path / "socketbox-peer").read_text(encoding="ascii") == (
            "127.0.0.1\n"
        )
    finally:
        coordinator.terminate()
        coordinator.wait(timeout=3)


def test_model_pair_coordinator_accepts_recovery_after_an_idle_client_timeout(
    tmp_path: Path,
) -> None:
    port = _unused_port("127.0.0.2")
    coordinator = subprocess.Popen(
        [str(FABRIC_RENDEZVOUS), "coordinator"],
        env=_rendezvous_environment(
            tmp_path,
            rank=0,
            local_address="127.0.0.2",
            port=port,
            timeout_seconds=1,
        ),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        worker_environment = _rendezvous_environment(
            tmp_path,
            rank=1,
            local_address="127.0.0.3",
            port=port,
            timeout_seconds=3,
        )
        joins: list[subprocess.CompletedProcess[str]] = []
        for idle_seconds in (0.0, 1.25):
            time.sleep(idle_seconds)
            joined = subprocess.run(
                [str(FABRIC_RENDEZVOUS), "join"],
                env=worker_environment,
                text=True,
                capture_output=True,
                check=False,
            )
            joins.append(joined)
            assert joined.returncode == 0, joined.stderr

        assert coordinator.poll() is None
        assert [joined.stdout for joined in joins] == [
            "fabric rendezvous complete: rank 1 of 2\n",
            "fabric rendezvous complete: rank 1 of 2\n",
        ]
    finally:
        if coordinator.poll() is None:
            coordinator.terminate()
        coordinator.wait(timeout=3)


def test_model_pair_recovers_after_an_incomplete_worker_message(tmp_path: Path) -> None:
    port = _unused_port("127.0.0.2")
    coordinator = subprocess.Popen(
        [str(FABRIC_RENDEZVOUS), "coordinator"],
        env=_rendezvous_environment(
            tmp_path,
            rank=0,
            local_address="127.0.0.2",
            port=port,
            timeout_seconds=1,
        ),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    incomplete: socket.socket | None = None
    try:
        deadline = time.monotonic() + 2
        while True:
            try:
                incomplete = socket.create_connection(("127.0.0.2", port), timeout=1)
                break
            except ConnectionRefusedError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        incomplete.sendall(b"vonk-fabric-v1 worker rank=1")
        incomplete.settimeout(2)
        assert incomplete.recv(1) == b""

        joined = subprocess.run(
            [str(FABRIC_RENDEZVOUS), "join"],
            env=_rendezvous_environment(
                tmp_path,
                rank=1,
                local_address="127.0.0.3",
                port=port,
                timeout_seconds=1,
            ),
            text=True,
            capture_output=True,
            check=False,
        )

        assert coordinator.poll() is None
        assert joined.returncode == 0, joined.stderr
        assert joined.stdout == "fabric rendezvous complete: rank 1 of 2\n"
    finally:
        if incomplete is not None:
            incomplete.close()
        if coordinator.poll() is None:
            coordinator.terminate()
        coordinator.wait(timeout=3)


def test_model_pair_refuses_an_invalid_declared_local_fabric_address(
    tmp_path: Path,
) -> None:
    if not FABRIC_RENDEZVOUS.is_file():
        pytest.fail("model pair has no executable fabric rendezvous")
    completed = subprocess.run(
        [str(FABRIC_RENDEZVOUS), "join"],
        env=_rendezvous_environment(
            tmp_path, rank=1, local_address="not-an-address", port=29500
        ),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "fabric rendezvous failed" in completed.stderr


def test_model_recipe_source_bundle_ships_the_fabric_gate() -> None:
    sys.path.insert(0, str(ROOT / "control/src"))
    generate_source_bundle = importlib.import_module(
        "vonk_control.source_bundles"
    ).generate_source_bundle
    recipe = json.loads(
        (ROOT / "config/recipes/development/model-smoke-pair.json").read_text()
    )
    dockerfile = (MODEL_CONTEXT / "Dockerfile").read_text(encoding="utf-8")
    files = {
        path.relative_to(MODEL_CONTEXT).as_posix(): path.read_bytes()
        for path in sorted(MODEL_CONTEXT.rglob("*"))
        if path.is_file()
    }
    bundle = generate_source_bundle(files)

    assert "fabric-rendezvous /opt/vonk/" in dockerfile
    assert "busybox=${VONK_BUSYBOX:-/opt/vonk/busybox}" in FABRIC_RENDEZVOUS.read_text(
        encoding="utf-8"
    )
    assert recipe["build"]["context"] == {
        "sha256": bundle.sha256,
        "expected_bytes": len(bundle.archive),
        "media_type": "application/vnd.vonk-forge.source-bundle.v1+tar",
    }
    assert recipe["validation"]["validators"][0] == {
        "interface": "openai",
        "checks": [
            "container.started",
            "fabric.rendezvous.completed",
            "endpoint.healthy",
            "inference.completed",
            "route.withdrawn_on_rank_failure",
            "route.republished_after_recovery",
        ],
    }
