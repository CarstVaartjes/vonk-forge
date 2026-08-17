from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest
from vonk_control.fleet_projection import FleetSnapshot

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/accept-recipe"
DS4 = ROOT / "config/recipes/deepseek-v4-flash-0731-ds4-single.json"
MIA = ROOT / "config/recipes/deepseek-v4-flash-0731-mia-dual.json"
NODE = "spk_0123456789abcdef0123456789abcdef"
NODE_2 = "spk_fedcba9876543210fedcba9876543210"
FLEET_SELECTORS = ("spark-3542", "spark-2297")
SSH_TARGETS = ("vonk-node-1", "vonk-node-2")
TOKEN = "acceptance-admin-token"
INFERENCE_TOKEN = "acceptance-inference-token"
MODEL_STATES = [
    "inventory-ready",
    "recipe-resolved",
    "source-verified",
    "image-built",
    "image-distributed",
    "installed",
    "running",
    "route-published",
    "inference-ok",
    "restart-persistence-observed",
    "stopped",
    "route-withdrawn",
    "uninstalled",
]
IMAGE = (
    "nvcr.io/nvidia/cuda:13.0.1-runtime-ubuntu24.04@"
    "sha256:36050649ad1acc5d3de2c26620191c25850fb12a5771b6c22996033003d952e4"
)
ARTIFACTS = [
    {
        "id": "target",
        "repository": "https://huggingface.co/antirez/deepseek-v4-gguf",
        "revision": "e7f04037032990db0346398d249baf9fb9df1ccc",
        "path": "DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf",
        "sha256": "ca22ae2f838e14077c22bc1c1417b71b45b5e5a3687bd96c2ac6e17fdb6261c0",
        "accessible": True,
    },
    {
        "id": "drafter",
        "repository": "https://huggingface.co/antirez/deepseek-v4-gguf",
        "revision": "e7f04037032990db0346398d249baf9fb9df1ccc",
        "path": "DeepSeek-V4-Flash-DSpark-support-0731.gguf",
        "sha256": "7e319924541db3f7a163ed7e11d7532a70d48228ab59d36cb81e1d4511885360",
        "accessible": True,
    },
]


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _recipe_digest(path: Path) -> str:
    return hashlib.sha256(
        json.dumps(
            json.loads(path.read_text()),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


class FleetServer(ThreadingHTTPServer):
    def __init__(self, address):
        super().__init__(address, FleetHandler)
        self.requests: list[tuple[str, str]] = []
        self.supervisor_generation = 1
        self.disk_free_bytes = 1_000_000_000_000
        self.host_memory_free_bytes = 127_000_000_000
        self.fabric_address: str | None = None
        self.fabric_bandwidth_mbps: int | None = None
        self.node_ids = [NODE]
        self.fabric_addresses: dict[str, str] = {}
        self.agent_sha256 = "c" * 64


class FleetHandler(BaseHTTPRequestHandler):
    server: FleetServer

    def log_message(self, _format: str, *_args: object) -> None:
        pass

    def _json(self, status: int, value: object) -> None:
        body = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self.server.requests.append(("GET", self.path))
        if self.headers.get("authorization") != f"Bearer {TOKEN}":
            self._json(401, {"detail": "authentication required"})
            return
        if self.path == "/api/v1/fleet":
            snapshot = FleetSnapshot.model_validate_json(
                json.dumps(
                    {
                    "schema_version": 1,
                    "event_cursor": self.server.supervisor_generation,
                    "generated_at": "2026-08-16T12:00:00+00:00",
                    "repository_commit": "a" * 40,
                    "nodes": [
                        _fleet_node(self.server, node_id, index)
                        for index, node_id in enumerate(self.server.node_ids)
                    ],
                    }
                )
            )
            self._json(200, snapshot.model_dump(mode="json"))
            return
        if self.path == "/api/v1/agents":
            self._json(
                200,
                {
                    "agents": [
                        {
                            "node_id": node_id,
                            "state": "active",
                            "agent_implementation": "rust",
                            "migration_state": "complete",
                            "protocol_version": 1,
                            "platform_version": "0.1.0",
                            "build_digest": "sha256:" + "b" * 64,
                            "active_slot": "A",
                            "agent_sha256": self.server.agent_sha256,
                            "supervisor_generation": self.server.supervisor_generation,
                            "capabilities": [
                                "build.rootless-podman.v1",
                                "recipe.operations.v1",
                                "runtime.spark-docker-nvidia.v1",
                            ],
                            "last_seen_at": "2026-08-16T11:59:59+00:00",
                            "last_seen_age_seconds": 1.0,
                            "stale": False,
                            "certificate_expires_at": "2026-09-16T12:00:00+00:00",
                        }
                        for node_id in self.server.node_ids
                    ]
                },
            )
            return
        self._json(404, {"detail": "not found"})


def _fleet_node(server: FleetServer, node_id: str, index: int) -> dict[str, object]:
    return {
        "id": node_id,
        "display_name": f"DGX Spark {index + 1}",
        "hostname": FLEET_SELECTORS[index],
        "lifecycle": "ready",
        "labels": {},
        "connection": {
            "agent_state": "active",
            "certificate_state": "valid",
            "online_state": "online",
            "offline_reason": None,
            "last_seen_at": "2026-08-16T11:59:59+00:00",
            "last_seen_age_seconds": 1.0,
        },
        "inventory": {
            "observed_at": "2026-08-16T11:59:59+00:00",
            "received_at": "2026-08-16T12:00:00+00:00",
            "age_seconds": 1.0,
            "freshness": "fresh",
            "disk_total_bytes": 2_000_000_000_000,
            "disk_free_bytes": server.disk_free_bytes,
            "host_memory_total_bytes": 128_000_000_000,
            "host_memory_free_bytes": server.host_memory_free_bytes,
            "gpu_memory_total_bytes": 128_000_000_000,
            "gpu_memory_free_bytes": server.host_memory_free_bytes,
            "gpu_count": 1,
            "artifact_store_read_only": False,
            "capabilities": [
                "build.rootless-podman.v1",
                "recipe.operations.v1",
                "runtime.spark-docker-nvidia.v1",
            ],
            "fabric_address": server.fabric_addresses.get(
                node_id, server.fabric_address
            ),
            "fabric_bandwidth_mbps": server.fabric_bandwidth_mbps,
            "nvidia_driver_version": "580.65.06",
            "container_runtime_version": "28.3.3",
        },
        "telemetry": None,
        "installed": [],
        "loaded": [],
        "reservations": {
            "disk_bytes": 0,
            "unified_memory_bytes": 0,
            "host_memory_bytes": 0,
            "gpu_memory_bytes": 0,
            "port_count": 0,
        },
        "warnings": [],
    }


@pytest.fixture
def fleet_server():
    server = FleetServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _private_file(path: Path, value: str) -> Path:
    path.write_text(value + "\n", encoding="ascii")
    path.chmod(0o600)
    return path


def _host_contract(recipe: Path) -> tuple[str, list[dict[str, object]]]:
    document = json.loads(recipe.read_text())
    model_slug = document["model"]["slug"]
    distribution_slug = document["runtime"]["distribution"]["slug"]
    model = json.loads(
        (ROOT / "config/model-versions" / f"{model_slug}.json").read_text()
    )
    distribution = json.loads(
        (
            ROOT / "config/runtime-distributions" / f"{distribution_slug}.json"
        ).read_text()
    )
    artifacts = [
        {
            "id": artifact["id"],
            "repository": artifact["repository"],
            "revision": artifact["revision"],
            "path": artifact["path"],
            "sha256": artifact["sha256"],
            "accessible": True,
        }
        for artifact in model["artifacts"]
    ]
    return distribution["image"], artifacts


def _host_evidence(
    path: Path,
    *,
    architecture: str = "linux-arm64",
    image: str = IMAGE,
    artifacts: list[dict[str, object]] | None = None,
) -> Path:
    path.write_bytes(
        _canonical(
            {
                "schema_version": 1,
                "architecture": architecture,
                "nvidia_driver_version": "580.65.06",
                "container_runtime_version": "28.3.3",
                "nvidia_container_runtime": True,
                "image_access": [{"reference": image, "accessible": True}],
                "artifact_access": ARTIFACTS if artifacts is None else artifacts,
            }
        )
    )
    return path


def _fake_ssh(tmp_path: Path, host_evidence: Path) -> tuple[Path, Path]:
    log = tmp_path / "ssh.log"
    executable = tmp_path / "ssh"
    executable.write_text(
        """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

with Path(os.environ["ACCEPT_TEST_SSH_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write(" ".join(sys.argv[1:]) + "\\n")
sys.stdin.buffer.read()
sys.stdout.buffer.write(Path(os.environ["ACCEPT_TEST_HOST_EVIDENCE"]).read_bytes())
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable, log


def _fake_fabric_validator(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "fabric.log"
    executable = tmp_path / "validate-fabric"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

with Path(os.environ["ACCEPT_TEST_FABRIC_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write(" ".join(sys.argv[1:]) + "\\n")
output = Path(sys.argv[sys.argv.index("--output") + 1])
expected = [value for index, value in enumerate(sys.argv) if index and sys.argv[index - 1] == "--expected-node"]
output.write_text(json.dumps({"schema_version": 2, "status": "preflight_passed", "evidence_scope": "live_read_only_preflight", "selected_nodes": expected}, sort_keys=True) + "\\n")
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable, log


def _run(
    tmp_path: Path,
    fleet_server: FleetServer,
    *arguments: str,
    recipe: Path = DS4,
    evidence: Path | None = None,
    host_architecture: str = "linux-arm64",
    nodes: str = FLEET_SELECTORS[0],
    ssh_targets: list[str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    evidence = evidence or tmp_path / "acceptance.json"
    image, artifacts = _host_contract(recipe)
    host = _host_evidence(
        tmp_path / "host-evidence.json",
        architecture=host_architecture,
        image=image,
        artifacts=artifacts,
    )
    ssh, ssh_log = _fake_ssh(tmp_path, host)
    fabric_validator, fabric_log = _fake_fabric_validator(tmp_path)
    environment = os.environ.copy()
    environment.update(
        {
            "ACCEPT_TEST_HOST_EVIDENCE": str(host),
            "ACCEPT_TEST_SSH_LOG": str(ssh_log),
            "ACCEPT_TEST_FABRIC_LOG": str(fabric_log),
        }
    )
    selectors = nodes.split(",")
    if ssh_targets is None:
        ssh_targets = list(SSH_TARGETS[: len(selectors)])
    mapping_arguments = [
        argument
        for selector, target in zip(selectors, ssh_targets)
        for argument in ("--ssh-target", f"{selector}={target}")
    ]
    result = subprocess.run(
        (
            str(SCRIPT),
            "--recipe",
            str(recipe),
            "--nodes",
            nodes,
            "--api-base",
            f"http://127.0.0.1:{fleet_server.server_port}",
            "--inference-base",
            f"http://127.0.0.1:{fleet_server.server_port}",
            "--admin-token-file",
            str(_private_file(tmp_path / "admin-token", TOKEN)),
            "--inference-token-file",
            str(_private_file(tmp_path / "inference-token", INFERENCE_TOKEN)),
            "--evidence-file",
            str(evidence),
            "--ssh-command",
            str(ssh),
            "--fabric-validator",
            str(fabric_validator),
            "--fabric-inventory",
            str(ROOT / "inventory/cluster.toml"),
            "--timeout-seconds",
            "2",
            "--poll-seconds",
            "0.01",
            *mapping_arguments,
            *arguments,
        ),
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, evidence, ssh_log


def test_acceptance_never_overstates_available_nodes(tmp_path: Path) -> None:
    recipe = json.loads(DS4.read_text())
    recipe["topology"]["node_count"] = 4
    four_node = tmp_path / "four-node.json"
    four_node.write_text(json.dumps(recipe), encoding="utf-8")

    result = subprocess.run(
        (
            str(SCRIPT),
            "--recipe",
            str(four_node),
            "--nodes",
            "dgx-spark-1,dgx-spark-2",
        ),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "requires exactly 4 nodes" in result.stderr


def test_preflight_records_exact_recipe_entity_node_and_host_identities_canonically(
    tmp_path: Path, fleet_server: FleetServer
) -> None:
    result, evidence_path, _ssh_log = _run(tmp_path, fleet_server, "--preflight-only")

    assert result.returncode == 0, result.stderr
    raw = evidence_path.read_bytes()
    evidence = json.loads(raw)
    assert raw == _canonical(evidence)
    assert stat.S_IMODE(evidence_path.stat().st_mode) == 0o600
    assert evidence["status"] == "preflight-passed"
    assert evidence["completed_phases"] == ["authored", "structurally-verified"]
    assert evidence["recipe"] == {
        "content_sha256": _recipe_digest(DS4),
        "publisher": "vonk-forge",
        "slug": "deepseek-v4-flash-0731-ds4-single",
    }
    assert evidence["nodes"][0]["selector"] == FLEET_SELECTORS[0]
    assert evidence["nodes"][0]["ssh_target"] == SSH_TARGETS[0]
    assert evidence["nodes"][0]["node_id"] == NODE
    assert evidence["nodes"][0]["agent_build_digest"] == "sha256:" + "b" * 64
    assert evidence["nodes"][0]["supervisor_generation"] == 1
    assert {reference["kind"] for reference in evidence["entity_references"]} == {
        "execution-harness",
        "model",
        "model-group",
        "model-version",
        "runtime-distribution",
    }
    assert evidence["preflight"]["hosts"][NODE]["image_access"] == [
        {"accessible": True, "reference": IMAGE}
    ]
    assert all(method == "GET" for method, _path in fleet_server.requests)


def test_single_node_recipe_ignores_available_fabric_that_its_topology_does_not_use(
    tmp_path: Path, fleet_server: FleetServer
) -> None:
    fleet_server.fabric_address = "192.168.100.11"
    fleet_server.fabric_bandwidth_mbps = 200_000

    result, evidence_path, _ssh_log = _run(tmp_path, fleet_server, "--preflight-only")

    assert result.returncode == 0, result.stderr
    assert json.loads(evidence_path.read_text())["status"] == "preflight-passed"


def test_mia_preflight_binds_two_distinct_fabric_nodes_and_peak_memory_contract(
    tmp_path: Path, fleet_server: FleetServer
) -> None:
    fleet_server.node_ids = [NODE, NODE_2]
    fleet_server.fabric_addresses = {
        NODE: "192.168.100.11",
        NODE_2: "192.168.100.12",
    }
    fleet_server.fabric_bandwidth_mbps = 200_000

    result, evidence_path, ssh_log = _run(
        tmp_path,
        fleet_server,
        "--preflight-only",
        recipe=MIA,
        nodes=",".join(FLEET_SELECTORS),
    )

    assert result.returncode == 0, result.stderr
    evidence = json.loads(evidence_path.read_text())
    assert [node["node_id"] for node in evidence["nodes"]] == [NODE, NODE_2]
    assert (
        evidence["preflight"]["resources"][NODE]["memory_required_bytes"]
        == 126_000_000_000
    )
    ssh_calls = ssh_log.read_text()
    assert ssh_calls.count("StrictHostKeyChecking=yes") == 2
    assert FLEET_SELECTORS[0] not in ssh_calls
    assert FLEET_SELECTORS[1] not in ssh_calls
    assert SSH_TARGETS[0] in ssh_calls
    assert SSH_TARGETS[1] in ssh_calls
    fabric = evidence["preflight"]["fabric"]
    assert fabric["selected_nodes"] == [
        f"{NODE}={SSH_TARGETS[0]}",
        f"{NODE_2}={SSH_TARGETS[1]}",
    ]
    assert re.fullmatch(r"[0-9a-f]{64}", fabric["evidence_sha256"])


def test_preflight_requires_complete_selector_to_ssh_mapping_before_network(
    tmp_path: Path, fleet_server: FleetServer
) -> None:
    result, _evidence_path, ssh_log = _run(
        tmp_path,
        fleet_server,
        "--preflight-only",
        ssh_targets=[],
    )

    assert result.returncode == 1
    assert "SSH target mapping must exactly cover Fleet selectors" in result.stderr
    assert fleet_server.requests == []
    assert not ssh_log.exists()


def test_mia_checkpoint_emits_label_verified_rank_container_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    loader = SourceFileLoader("accept_recipe_checkpoint", str(SCRIPT))
    spec = spec_from_loader(loader.name, loader)
    assert spec is not None
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)

    action = module._rank_checkpoint_action(
        run_id="50000000-0000-4000-8000-000000000001",
        runtime_request_sha256="d" * 64,
        ssh_target=SSH_TARGETS[1],
        start=False,
    )

    assert action["agent_action"] == "keep-running"
    assert action["container_name"] == (
        "vonk-50000000-0000-4000-8000-000000000001"
    )
    assert action["expected_labels"] == {
        "ai.vonkforge.managed": "true",
        "ai.vonkforge.run-id": "50000000-0000-4000-8000-000000000001",
        "ai.vonkforge.runtime-request-sha256": "d" * 64,
    }
    assert action["operation"] == "stop"
    assert action["ssh_target"] == SSH_TARGETS[1]


def test_catalog_inputs_resolve_entities_from_external_library_root(
    tmp_path: Path,
) -> None:
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.syspath_prepend(str(ROOT / "scripts"))
        loader = SourceFileLoader("accept_recipe_external_library", str(SCRIPT))
        spec = spec_from_loader(loader.name, loader)
        assert spec is not None
        module = module_from_spec(spec)
        sys.modules[spec.name] = module
        loader.exec_module(module)

        library_root = tmp_path / "recipe-library"
        for directory in (
            "model-groups",
            "models",
            "model-versions",
            "execution-harnesses",
            "runtime-distributions",
            "patch-bundles",
        ):
            shutil.copytree(ROOT / "config" / directory, library_root / directory)

        recipe = json.loads(DS4.read_text(encoding="utf-8"))
        _model_version, _distribution, references, documents = module._inputs(
            recipe,
            [],
            library_root=library_root,
            platform_root=ROOT,
        )

        assert {document["kind"] for document in documents} == {
            "model-group",
            "model",
            "model-version",
            "execution-harness",
            "runtime-distribution",
        }
        assert all(reference["content_sha256"] for reference in references)
        assert all(
            (library_root / module.KIND_ROOTS[document["kind"]]).is_dir()
            for document in documents
        )
    finally:
        monkeypatch.undo()


def test_preflight_rejects_wrong_host_architecture_without_runner_mutation(
    tmp_path: Path, fleet_server: FleetServer
) -> None:
    result, evidence_path, _ssh_log = _run(
        tmp_path,
        fleet_server,
        "--preflight-only",
        host_architecture="linux-x86_64",
    )

    assert result.returncode != 0
    assert "requires linux-arm64" in result.stderr
    evidence = json.loads(evidence_path.read_text())
    assert evidence["completed_phases"] == ["authored", "structurally-verified"]
    assert evidence["status"] == "failed"
    assert "spark-canary" not in evidence["completed_phases"]
    assert all(method == "GET" for method, _path in fleet_server.requests)


def test_acceptance_refuses_to_resume_evidence_for_changed_recipe_identity(
    tmp_path: Path, fleet_server: FleetServer
) -> None:
    recipe = tmp_path / "recipe.json"
    recipe.write_bytes(DS4.read_bytes())
    evidence = tmp_path / "acceptance.json"
    first, _path, ssh_log = _run(
        tmp_path,
        fleet_server,
        "--preflight-only",
        recipe=recipe,
        evidence=evidence,
    )
    assert first.returncode == 0, first.stderr
    first_ssh_calls = ssh_log.read_text()
    changed = json.loads(recipe.read_text())
    changed["metadata"]["description"] = "A different immutable recipe revision."
    recipe.write_text(json.dumps(changed), encoding="utf-8")

    second, _path, _new_log = _run(
        tmp_path,
        fleet_server,
        "--preflight-only",
        recipe=recipe,
        evidence=evidence,
    )

    assert second.returncode != 0
    assert "evidence inputs do not match this invocation" in second.stderr
    assert json.loads(evidence.read_text())["recipe"][
        "content_sha256"
    ] != _recipe_digest(recipe)
    assert ssh_log.read_text() == first_ssh_calls


def test_acceptance_refuses_symlinked_evidence_instead_of_following_its_target(
    tmp_path: Path, fleet_server: FleetServer
) -> None:
    evidence = tmp_path / "acceptance.json"
    first, _path, _log = _run(
        tmp_path, fleet_server, "--preflight-only", evidence=evidence
    )
    assert first.returncode == 0, first.stderr
    before = evidence.read_bytes()
    linked = tmp_path / "linked-acceptance.json"
    linked.symlink_to(evidence)

    second, _path, _log = _run(
        tmp_path, fleet_server, "--preflight-only", evidence=linked
    )

    assert second.returncode != 0
    assert "acceptance input file is unsafe" in second.stderr
    assert evidence.read_bytes() == before


def test_acceptance_refuses_status_that_overstates_completed_phases(
    tmp_path: Path, fleet_server: FleetServer
) -> None:
    evidence = tmp_path / "acceptance.json"
    first, _path, ssh_log = _run(
        tmp_path, fleet_server, "--preflight-only", evidence=evidence
    )
    assert first.returncode == 0, first.stderr
    before_ssh = ssh_log.read_text()
    document = json.loads(evidence.read_text())
    document["completed_phases"] = ["authored"]
    document["status"] = "spark-accepted"
    evidence.write_bytes(_canonical(document))

    second, _path, _log = _run(
        tmp_path, fleet_server, "--preflight-only", evidence=evidence
    )

    assert second.returncode != 0
    assert "acceptance evidence status overstates completed phases" in second.stderr
    assert ssh_log.read_text() == before_ssh


def test_preflight_rejects_malformed_agent_content_identity_before_ssh(
    tmp_path: Path, fleet_server: FleetServer
) -> None:
    fleet_server.agent_sha256 = "not-a-sha256"

    result, _evidence_path, ssh_log = _run(
        tmp_path, fleet_server, "--preflight-only"
    )

    assert result.returncode != 0
    assert "agent exact identity is incomplete" in result.stderr
    assert not ssh_log.exists()


def test_acceptance_reruns_structural_qualification_instead_of_trusting_stale_sidecar(
    tmp_path: Path, fleet_server: FleetServer
) -> None:
    recipe = tmp_path / "recipe.json"
    document = json.loads(DS4.read_text())
    document["build"]["context"]["sha256"] = "0" * 64
    recipe.write_text(json.dumps(document), encoding="utf-8")
    evidence = tmp_path / "acceptance.json"
    stale = evidence.with_name("acceptance.qualification.json")
    stale.write_bytes(
        _canonical({"passed": True, "recipe": recipe.name, "status": "passed"})
    )
    stale.chmod(0o600)

    result, evidence_path, ssh_log = _run(
        tmp_path,
        fleet_server,
        "--preflight-only",
        recipe=recipe,
        evidence=evidence,
    )

    assert result.returncode != 0
    assert "structural recipe qualification failed" in result.stderr
    assert json.loads(evidence_path.read_text())["completed_phases"] == ["authored"]
    assert not ssh_log.exists()


def _qualification(path: Path) -> bytes:
    return _canonical({"passed": True, "recipe": path.name, "status": "passed"})


def _complete_runner_evidence(
    evidence_path: Path,
    *,
    api_base: str,
    qualification_sha256: str,
    recipe_digest: str,
    include_image: bool = True,
) -> None:
    restart_checkpoint = {
        NODE: {
            "boot_id": "11111111-1111-4111-8111-111111111111",
            "supervisor_generation": 1,
        }
    }
    restart_identity = {
        NODE: {
            "boot_id": "22222222-2222-4222-8222-222222222222",
            "supervisor_generation": 2,
        }
    }
    restart_binding = {
        "fleet_evidence_digest": "b" * 64,
        "inference_response_sha256": "c" * 64,
        "restart_identity": restart_identity,
    }
    outputs: dict[str, object] = {
        "fleet_evidence_digest": "1" * 64,
        "initial_agent_last_seen": {NODE: "2026-08-16T11:00:00+00:00"},
        "recipe_id": "10000000-0000-4000-8000-000000000001",
        "recipe_revision_id": "10000000-0000-4000-8001-000000000001",
        "recipe_revision": 1,
        "recipe_content_sha256": recipe_digest,
        "source_bundle_sha256": "2" * 64,
        "build_input_sha256": "3" * 64,
        "recipe_build_id": "20000000-0000-4000-8000-000000000001",
        "build_operation_id": "20000000-0000-4000-8001-000000000001",
        "oci_layout_sha256": "5" * 64,
        "image_bytes": 123456,
        "mapping_id": "30000000-0000-4000-8000-000000000001",
        "mapping_generation": 1,
        "distribution_operation_id": "30000000-0000-4000-8001-000000000001",
        "distribution_nodes": [NODE],
        "distribution_evidence_sha256": "6" * 64,
        "installation_id": "40000000-0000-4000-8000-000000000001",
        "installation_operation_id": "40000000-0000-4000-8001-000000000001",
        "run_id": "50000000-0000-4000-8000-000000000001",
        "run_operation_id": "50000000-0000-4000-8001-000000000001",
        "artifact_set_digest": "7" * 64,
        "run_node_evidence_digests": {NODE: "8" * 64},
        "run_evidence_sha256": "9" * 64,
        "inference_response_sha256": "a" * 64,
        "restart_checkpoint": restart_checkpoint,
        "restart_fleet_evidence_digest": "b" * 64,
        "restart_agent_last_seen": {NODE: "2026-08-16T12:00:00+00:00"},
        "restart_identity": restart_identity,
        "restart_inference_response_sha256": "c" * 64,
        "restart_binding_sha256": hashlib.sha256(
            _canonical(restart_binding)
        ).hexdigest(),
        "stop_operation_id": "60000000-0000-4000-8000-000000000001",
        "uninstall_operation_id": "70000000-0000-4000-8000-000000000001",
    }
    if include_image:
        outputs["image_digest"] = "sha256:" + "4" * 64
    document = {
        "schema_version": 1,
        "acceptance_id": "80000000-0000-4000-8000-000000000001",
        "phase": "model-single",
        "api_base": api_base,
        "inference_base": api_base,
        "builder_node": NODE,
        "target_nodes": [NODE],
        "recipe_slug": "deepseek-v4-flash-0731-ds4-single",
        "topology_name": "solo",
        "qualification_sha256": qualification_sha256,
        "completed_states": MODEL_STATES,
        "outputs": outputs,
    }
    evidence_path.write_bytes(_canonical(document))
    evidence_path.chmod(0o600)


def test_spark_acceptance_requires_exact_outputs_and_advanced_supervisor_generation(
    tmp_path: Path, fleet_server: FleetServer
) -> None:
    evidence = tmp_path / "acceptance.json"
    initial, _path, _log = _run(
        tmp_path, fleet_server, "--preflight-only", evidence=evidence
    )
    assert initial.returncode == 0, initial.stderr
    fleet_server.supervisor_generation = 2
    qualification = evidence.with_name("acceptance.qualification.json")
    qualification.write_bytes(_qualification(DS4))
    qualification.chmod(0o600)
    runner = evidence.with_name("acceptance.runner.json")
    api_base = f"http://127.0.0.1:{fleet_server.server_port}"
    _complete_runner_evidence(
        runner,
        api_base=api_base,
        qualification_sha256=hashlib.sha256(qualification.read_bytes()).hexdigest(),
        recipe_digest=_recipe_digest(DS4),
    )

    result, _path, _log = _run(
        tmp_path, fleet_server, "--level", "spark", evidence=evidence
    )

    assert result.returncode == 0, result.stderr
    accepted_raw = evidence.read_bytes()
    accepted = json.loads(accepted_raw)
    assert accepted_raw == _canonical(accepted)
    assert accepted["status"] == "spark-accepted"
    assert accepted["completed_phases"] == [
        "authored",
        "structurally-verified",
        "container-verified",
        "spark-canary",
        "spark-accepted",
    ]
    assert accepted["accepted_evidence"]["image_digest"] == "sha256:" + "4" * 64
    assert accepted["accepted_evidence"]["artifact_set_digest"] == "7" * 64
    assert re.fullmatch(r"[0-9a-f]{64}", accepted["runner_evidence_sha256"])


def test_complete_state_names_cannot_overstate_missing_exact_evidence(
    tmp_path: Path, fleet_server: FleetServer
) -> None:
    evidence = tmp_path / "acceptance.json"
    initial, _path, _log = _run(
        tmp_path, fleet_server, "--preflight-only", evidence=evidence
    )
    assert initial.returncode == 0, initial.stderr
    fleet_server.supervisor_generation = 2
    qualification = evidence.with_name("acceptance.qualification.json")
    qualification.write_bytes(_qualification(DS4))
    qualification.chmod(0o600)
    runner = evidence.with_name("acceptance.runner.json")
    api_base = f"http://127.0.0.1:{fleet_server.server_port}"
    _complete_runner_evidence(
        runner,
        api_base=api_base,
        qualification_sha256=hashlib.sha256(qualification.read_bytes()).hexdigest(),
        recipe_digest=_recipe_digest(DS4),
        include_image=False,
    )

    result, _path, _log = _run(
        tmp_path, fleet_server, "--level", "spark", evidence=evidence
    )

    assert result.returncode != 0
    assert "runner evidence is incomplete" in result.stderr
    retained = json.loads(evidence.read_text())
    assert retained["status"] == "failed"
    assert retained["completed_phases"] == ["authored", "structurally-verified"]
    assert "accepted_evidence" not in retained
