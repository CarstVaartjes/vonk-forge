from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "run-development-slices"
STATES = [
    "inventory-ready",
    "recipe-resolved",
    "source-verified",
    "image-built",
    "image-distributed",
    "installed",
    "running",
    "route-published",
    "inference-ok",
    "stopped",
    "route-withdrawn",
    "uninstalled",
]
MODEL_MULTINODE_STATES = [
    *STATES[:9],
    "rank-failure-observed",
    "route-withdrawn-after-failure",
    "rank-recovered",
    "route-republished",
    "inference-recovered",
    "restart-persistence-observed",
    *STATES[9:],
]
NODE = "spk_0123456789abcdef0123456789abcdef"
NODE_2 = "spk_fedcba9876543210fedcba9876543210"
ADMIN_TOKEN = "admin-secret-marker"
INFERENCE_TOKEN = "inference-secret-marker"
RECIPE_DIGEST = "585b83a971181a32e3605463ce7f7f3eb5c94ac4658b207da1d4ef7de378a947"


class SliceServer(ThreadingHTTPServer):
    def __init__(self, address, *, fail_path: str | None = None):
        super().__init__(address, SliceHandler)
        self.fail_path = fail_path
        self.requests: list[tuple[str, str, str]] = []
        self.recipe_created = False
        self.recipe_digest = RECIPE_DIGEST
        self.recipe_revision = 1
        self.source_digest = (
            "7a65752ee1a950b3b358c66ceaf2007d0eb824a7842d0a67a5b1e3726957eb80"
        )
        self.slug = "dev-http-smoke"
        self.route_published = False
        self.withdrawn_endpoint_status = 404
        self.operation = 0
        self.operation_nodes: dict[str, list[str]] = {}
        self.operation_kinds: dict[str, str] = {}
        self.operation_states: dict[str, str] = {}
        self.build_operation_state = "succeeded"
        self.distribution_operation_state = "succeeded"
        self.retry_operation_state = "succeeded"
        self.add_empty_provider_metadata = False
        self.nodes = [NODE]
        self.online = {NODE: True, NODE_2: True}
        self.inventory_stale = {NODE: False, NODE_2: False}
        self.inventory_capabilities = {
            NODE: [
                "recipe.operations.v1",
                "build.rootless-podman.v1",
                "runtime.spark-docker-nvidia.v1",
            ],
            NODE_2: [
                "recipe.operations.v1",
                "build.rootless-podman.v1",
                "runtime.spark-docker-nvidia.v1",
            ],
        }
        self.last_seen = {
            NODE: "2026-08-11T10:00:00+00:00",
            NODE_2: "2026-08-11T10:00:00+00:00",
        }
        self.fleet_digest = "b" * 64
        self.artifact_set_digests = {NODE: "7" * 64, NODE_2: "7" * 64}
        self.rank_states = {NODE: "running", NODE_2: "running"}


class SliceHandler(BaseHTTPRequestHandler):
    server: SliceServer

    def log_message(self, _format: str, *_args: object) -> None:
        pass

    def _body(self) -> bytes:
        length = int(self.headers.get("content-length", "0"))
        return self.rfile.read(length)

    def _json(self, status: int, value: object) -> None:
        body = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self) -> bytes:
        body = self._body()
        authorization = self.headers.get("authorization", "")
        self.server.requests.append((self.command, self.path, authorization))
        if self.server.fail_path == self.path:
            self._json(
                409,
                {
                    "detail": (
                        f"Authorization: Bearer {ADMIN_TOKEN}; "
                        f"inference={INFERENCE_TOKEN}"
                    )
                },
            )
            raise RuntimeError("handled failure")
        return body

    def do_GET(self) -> None:
        try:
            self._record()
        except RuntimeError:
            return
        if self.path == "/api/v1/fleet":
            self._json(
                200,
                {
                    "commit": "a" * 40,
                    "evidence_digest": self.server.fleet_digest,
                    "nodes": [
                        {
                            "id": node,
                            "healthy": True,
                            "stale": False,
                            "agent_online": self.server.online[node],
                            "agent_state": "active",
                            "compatibility": "supported",
                            "inventory_stale": self.server.inventory_stale[node],
                            "inventory_capabilities": (
                                self.server.inventory_capabilities[node]
                            ),
                            "agent_last_seen_at": self.server.last_seen[node],
                        }
                        for node in self.server.nodes
                    ],
                },
            )
        elif self.path == (
            "/api/v1/catalog/recipes/10000000-0000-4000-8000-000000000001"
        ):
            self._json(
                200,
                {
                    "recipe_id": "10000000-0000-4000-8000-000000000001",
                    "id": "10000000-0000-4000-8000-000000000002",
                    "revision_number": self.server.recipe_revision,
                    "lifecycle": "resolved",
                    "content_sha256": self.server.recipe_digest,
                    "source_bundle_sha256": self.server.source_digest,
                },
            )
        elif self.path.startswith("/api/v1/catalog/recipes"):
            recipes = []
            if self.server.recipe_created:
                recipes.append(
                    {
                        "recipe_id": "10000000-0000-4000-8000-000000000001",
                        "slug": self.server.slug,
                        "revision_number": self.server.recipe_revision,
                        "lifecycle": "resolved",
                        "content_sha256": self.server.recipe_digest,
                    }
                )
            self._json(200, {"recipes": recipes, "next_cursor": None})
        elif self.path.startswith("/api/v1/recipes/operations/"):
            operation_id = self.path.rsplit("/", 1)[-1]
            nodes = self.server.operation_nodes.get(operation_id, self.server.nodes)
            kind = self.server.operation_kinds.get(operation_id, "unknown")
            node_evidence: dict[str, dict[str, object]] = {}
            for rank, node in enumerate(nodes):
                if kind == "build":
                    node_evidence[node] = {
                        "build_input_sha256": "e" * 64,
                        "image_bytes": 123456789,
                        "image_digest": "sha256:" + "9" * 64,
                        "oci_layout_sha256": "8" * 64,
                        "policy": {
                            "passed": True,
                            "findings": [],
                            "dockerfile": "Dockerfile",
                        },
                    }
                elif kind == "distribution":
                    node_evidence[node] = {
                        "build_id": "20000000-0000-4000-8000-000000000001",
                        "image_bytes": 123456789,
                        "image_digest": "sha256:" + "9" * 64,
                        "oci_layout_sha256": "8" * 64,
                    }
                elif kind == "start":
                    node_evidence[node] = {
                        "image_digest": "9" * 64,
                        "artifact_set_digest": self.server.artifact_set_digests[node],
                        "rank": rank,
                        "ready": True,
                        "evidence_digest": f"{rank + 1:064x}",
                    }
                elif kind == "install":
                    node_evidence[node] = {"installed_bytes": 123456789}
                else:
                    node_evidence[node] = {"status": "ok"}
            self._json(
                200,
                {
                    "id": operation_id,
                    "kind": "recipe.test",
                    "owner_id": "20000000-0000-4000-8000-000000000001",
                    "state": self.server.operation_states.get(
                        operation_id, "succeeded"
                    ),
                    "plan_digest": "c" * 64,
                    "nodes": [NODE],
                    "result": {
                        "successful_nodes": sorted(nodes),
                        "failed_nodes": [],
                        "node_evidence": node_evidence,
                    },
                },
            )
        elif self.path == f"/api/v1/endpoints/{self.server.slug}":
            if self.server.route_published:
                self._json(
                    200,
                    {
                        "alias": self.server.slug,
                        "state": "published",
                        "nodes": [NODE],
                    },
                )
            else:
                detail = (
                    "endpoint publication unavailable"
                    if self.server.withdrawn_endpoint_status == 503
                    else "not found"
                )
                self._json(self.server.withdrawn_endpoint_status, {"detail": detail})
        elif self.path.startswith("/api/v1/recipes/runs/"):
            ranks = [
                {
                    "node_id": node,
                    "rank": rank,
                    "role": "entrypoint" if rank == 0 else "worker",
                    "state": self.server.rank_states[node],
                    "observed_at": "2026-08-11T10:00:00Z",
                    "age_seconds": 1.0,
                    "fresh": True,
                }
                for rank, node in enumerate(self.server.nodes)
            ]
            self._json(
                200,
                {
                    "id": self.path.rsplit("/", 1)[-1],
                    "alias": self.server.slug,
                    "state": "running",
                    "route_state": (
                        "published" if self.server.route_published else "withdrawn"
                    ),
                    "healthy": all(rank["state"] == "running" for rank in ranks),
                    "ranks": ranks,
                },
            )
        else:
            self._json(404, {"detail": "not found"})

    def do_PUT(self) -> None:
        try:
            body = self._record()
        except RuntimeError:
            return
        if self.path.endswith("/draft"):
            payload = json.loads(body)
            if payload.get("expected_revision") != self.server.recipe_revision:
                self._json(409, {"detail": "stale revision"})
                return
            document = payload["document"]
            self.server.recipe_revision += 1
            self.server.recipe_digest = hashlib.sha256(
                json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            self.server.source_digest = document["build"]["context"]["sha256"]
            self._json(
                200,
                {
                    "recipe_id": "10000000-0000-4000-8000-000000000001",
                    "id": "10000000-0000-4000-8000-000000000003",
                    "revision_number": self.server.recipe_revision,
                    "lifecycle": "draft",
                    "content_sha256": None,
                    "source_bundle_sha256": self.server.source_digest,
                },
            )
            return
        digest = self.path.rsplit("/", 1)[-1]
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:") as archive:
            files = [member for member in archive.getmembers() if member.isfile()]
        self._json(
            200,
            {
                "sha256": digest,
                "archive_bytes": len(body),
                "total_bytes": sum(member.size for member in files),
                "file_count": len(files),
                "files": [member.name for member in files],
            },
        )

    def do_POST(self) -> None:
        try:
            body = self._record()
        except RuntimeError:
            return
        payload = json.loads(body) if body else {}
        path = self.path
        if path == "/api/v1/catalog/recipes":
            self.server.recipe_created = True
            document = payload["document"]
            self.server.slug = payload["slug"]
            self.server.recipe_digest = hashlib.sha256(
                json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            self.server.source_digest = document["build"]["context"]["sha256"]
            self._json(
                201,
                {
                    "recipe_id": "10000000-0000-4000-8000-000000000001",
                    "id": "10000000-0000-4000-8000-000000000002",
                    "revision_number": self.server.recipe_revision,
                    "lifecycle": "draft",
                    "content_sha256": None,
                    "source_bundle_sha256": payload["document"]["build"]["context"][
                        "sha256"
                    ],
                },
            )
        elif path.endswith("/resolve"):
            self._json(
                200,
                {
                    "recipe_id": "10000000-0000-4000-8000-000000000001",
                    "id": "10000000-0000-4000-8000-000000000002",
                    "revision_number": self.server.recipe_revision,
                    "lifecycle": "resolved",
                    "content_sha256": self.server.recipe_digest,
                    "source_bundle_sha256": self.server.source_digest,
                },
            )
        elif path == "/api/v1/recipes/source-checks":
            self._json(
                200,
                {
                    "passed": True,
                    "source_bundle_sha256": "7a65752ee1a950b3b358c66ceaf2007d0eb824a7842d0a67a5b1e3726957eb80",
                    "dockerfile": "Dockerfile",
                    "findings": [],
                },
            )
        elif path.endswith("mapping-plans/preview"):
            self._json(
                200,
                {
                    "generation": 1,
                    "placement_digest": "d" * 64,
                    "nodes": [{"node_id": NODE, "rank": 0, "endpoint_owner": True}],
                },
            )
        elif path.endswith("/mappings"):
            self._json(
                201,
                {
                    "mapping_id": "30000000-0000-4000-8000-000000000001",
                    "generation": 1,
                    "placement_digest": "d" * 64,
                },
            )
        elif path.endswith("build-plans/preview"):
            self._json(
                200,
                {"build_input_sha256": "e" * 64, "source_bundle_sha256": "f" * 64},
            )
        elif path.endswith(("install-plans/preview", "run-plans/preview")):
            self._json(200, {"allowed": True, "plan_digest": "f" * 64, "nodes": []})
        elif path == "/v1/chat/completions":
            if payload.get("model") == "dev-http-smoke":
                response = json.loads(
                    (
                        ROOT
                        / "control/tests/fixtures/recipes/dev-http-smoke/expected.json"
                    ).read_text()
                )["response"]
                if self.server.add_empty_provider_metadata:
                    response["choices"][0]["provider_specific_fields"] = {}
                    response["choices"][0]["message"]["provider_specific_fields"] = {
                        "refusal": None
                    }
            else:
                response = {
                    "id": "chatcmpl-model-smoke",
                    "object": "chat.completion",
                    "model": payload["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "VONK MODEL OK",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                }
            self._json(200, response)
        elif path.endswith("/stop"):
            self.server.route_published = False
            self._operation("20000000-0000-4000-8000-000000000004", self.server.nodes)
        elif path.endswith("/uninstall"):
            self._operation("20000000-0000-4000-8000-000000000005", self.server.nodes)
        elif path.startswith("/api/v1/recipes/operations/") and path.endswith("/retry"):
            original_operation_id = self.path.rsplit("/", 2)[-2]
            original_kind = self.server.operation_kinds.get(
                original_operation_id, "build"
            )
            self._operation(
                (
                    "20000000-0000-4000-8000-000000000002"
                    if original_kind == "distribution"
                    else "20000000-0000-4000-8000-000000000001"
                ),
                [NODE],
                kind=original_kind,
                state=self.server.retry_operation_state,
            )
        else:
            owner = {
                "/api/v1/recipes/builds": "20000000-0000-4000-8000-000000000001",
                "/api/v1/recipes/image-distributions": "20000000-0000-4000-8000-000000000002",
                "/api/v1/recipes/installations": "20000000-0000-4000-8000-000000000003",
                "/api/v1/recipes/runs": "20000000-0000-4000-8000-000000000004",
            }.get(path)
            if owner is None:
                self._json(404, {"detail": "not found"})
                return
            if path == "/api/v1/recipes/runs":
                self.server.route_published = True
            kind = {
                "/api/v1/recipes/builds": "build",
                "/api/v1/recipes/image-distributions": "distribution",
                "/api/v1/recipes/installations": "install",
                "/api/v1/recipes/runs": "start",
            }[path]
            nodes = [NODE] if kind == "build" else self.server.nodes
            self._operation(owner, nodes, kind=kind)

    def _operation(
        self,
        owner: str,
        nodes: list[str] | None = None,
        *,
        kind: str = "lifecycle",
        state: str | None = None,
    ) -> None:
        self.server.operation += 1
        operation_id = f"40000000-0000-4000-8000-{self.server.operation:012d}"
        self.server.operation_nodes[operation_id] = list(nodes or self.server.nodes)
        self.server.operation_kinds[operation_id] = kind
        if state is None and kind == "build":
            state = self.server.build_operation_state
        if state is None and kind == "distribution":
            state = self.server.distribution_operation_state
        if state is not None:
            self.server.operation_states[operation_id] = state
        self._json(
            202,
            {
                "id": operation_id,
                "kind": "recipe.test",
                "owner_id": owner,
                "state": "queued",
                "plan_digest": "f" * 64,
                "nodes": list(nodes or self.server.nodes),
                "result": None,
            },
        )


@pytest.fixture
def server():
    active = SliceServer(("127.0.0.1", 0))
    thread = threading.Thread(target=active.serve_forever, daemon=True)
    thread.start()
    try:
        yield active
    finally:
        active.shutdown()
        thread.join(timeout=5)
        active.server_close()


def _token(path: Path, value: str) -> Path:
    path.write_text(value + "\n", encoding="ascii")
    path.chmod(0o600)
    return path


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _qualification(path: Path) -> Path:
    source = json.loads(
        (ROOT / "config/recipes/development/model-smoke-source.json").read_text()
    )
    artifact_document = json.loads(
        (ROOT / "config/recipes/development/model-smoke-artifacts.json").read_text()
    )
    topology = json.loads(
        (ROOT / "config/recipes/development/model-smoke-multinode.json").read_text()
    )
    document = {
        "schema_version": 1,
        "status": "qualified",
        "runtime_image": source["runtime_image"],
        "source_sha256": hashlib.sha256(_canonical(source)).hexdigest(),
        "artifact_set_sha256": hashlib.sha256(
            _canonical(artifact_document["artifacts"])
        ).hexdigest(),
        "topology_sha256": hashlib.sha256(_canonical(topology)).hexdigest(),
        "evidence_sha256": "a" * 64,
        "single_node": NODE,
        "multinode_nodes": [NODE, NODE_2],
    }
    path.write_bytes(_canonical(document))
    path.chmod(0o600)
    return path


def _run(tmp_path: Path, server: SliceServer, *extra: str):
    admin = _token(tmp_path / "admin-token", ADMIN_TOKEN)
    inference = _token(tmp_path / "inference-token", INFERENCE_TOKEN)
    evidence = tmp_path / "evidence.json"
    result = subprocess.run(
        (
            sys.executable,
            str(RUNNER),
            "--api-base",
            f"http://127.0.0.1:{server.server_port}",
            "--admin-token-file",
            str(admin),
            "--inference-token-file",
            str(inference),
            "--phase",
            "synthetic",
            "--builder-node",
            NODE,
            "--target-node",
            NODE,
            "--evidence-file",
            str(evidence),
            "--timeout-seconds",
            "2",
            "--poll-seconds",
            "0.01",
            *extra,
        ),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, evidence


def _run_model(
    tmp_path: Path,
    server: SliceServer,
    *extra: str,
    qualification: Path | None = None,
):
    admin = _token(tmp_path / "admin-token", ADMIN_TOKEN)
    inference = _token(tmp_path / "inference-token", INFERENCE_TOKEN)
    qualification = qualification or _qualification(tmp_path / "qualification.json")
    evidence = tmp_path / "model-evidence.json"
    result = subprocess.run(
        (
            sys.executable,
            str(RUNNER),
            "--api-base",
            f"http://127.0.0.1:{server.server_port}",
            "--admin-token-file",
            str(admin),
            "--inference-token-file",
            str(inference),
            "--phase",
            "model-multinode",
            "--qualification-file",
            str(qualification),
            "--builder-node",
            NODE,
            "--target-node",
            NODE,
            "--target-node",
            NODE_2,
            "--failure-node",
            NODE_2,
            "--evidence-file",
            str(evidence),
            "--timeout-seconds",
            "2",
            "--poll-seconds",
            "0.01",
            *extra,
        ),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, evidence


def test_runner_help_exposes_restart_and_failure_checkpoints() -> None:
    result = subprocess.run(
        (sys.executable, str(RUNNER), "--help"),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--stop-after" in result.stdout
    assert "Pause after an accepted state" in result.stdout


def test_runner_completes_exact_public_lifecycle_without_secret_leaks(
    tmp_path: Path, server: SliceServer
) -> None:
    result, evidence_path = _run(tmp_path, server)

    assert result.returncode == 0, result.stderr
    evidence = json.loads(evidence_path.read_text())
    assert evidence["completed_states"] == STATES
    assert stat.S_IMODE(evidence_path.stat().st_mode) == 0o600
    encoded = result.stdout + result.stderr + evidence_path.read_text()
    assert ADMIN_TOKEN not in encoded
    assert INFERENCE_TOKEN not in encoded
    assert all(
        authorization in {f"Bearer {ADMIN_TOKEN}", f"Bearer {INFERENCE_TOKEN}"}
        for _method, _path, authorization in server.requests
    )
    assert all(
        path.startswith(("/api/v1/catalog/", "/api/v1/recipes/"))
        or path
        in {
            "/api/v1/fleet",
            "/api/v1/endpoints/dev-http-smoke",
            "/v1/chat/completions",
        }
        for _method, path, _authorization in server.requests
    )
    assert evidence["outputs"]["image_digest"] == "sha256:" + "9" * 64
    assert evidence["outputs"]["oci_layout_sha256"] == "8" * 64
    assert evidence["outputs"]["artifact_set_digest"] == "7" * 64
    assert evidence["outputs"]["distribution_nodes"] == [NODE]
    assert any(
        path == "/api/v1/recipes/image-distributions"
        for _method, path, _authorization in server.requests
    )


def test_runner_accepts_litellm_empty_provider_metadata(
    tmp_path: Path, server: SliceServer
) -> None:
    server.add_empty_provider_metadata = True

    result, evidence_path = _run(tmp_path, server, "--stop-after", "inference-ok")

    assert result.returncode == 0, result.stderr
    evidence = json.loads(evidence_path.read_text())
    assert evidence["completed_states"] == STATES[:9]


def test_runner_accepts_documented_maintenance_route_withdrawal(
    tmp_path: Path, server: SliceServer
) -> None:
    server.withdrawn_endpoint_status = 503

    result, evidence_path = _run(tmp_path, server, "--stop-after", "route-withdrawn")

    assert result.returncode == 0, result.stderr
    evidence = json.loads(evidence_path.read_text())
    assert evidence["completed_states"] == STATES[:11]


def test_model_multinode_runner_proves_failure_recovery_restart_and_cleanup(
    tmp_path: Path, server: SliceServer
) -> None:
    server.nodes = [NODE, NODE_2]

    initial, evidence_path = _run_model(
        tmp_path, server, "--stop-after", "inference-ok"
    )
    assert initial.returncode == 0, initial.stderr

    server.rank_states[NODE_2] = "failed"
    server.route_published = False
    failed, _ = _run_model(
        tmp_path, server, "--stop-after", "route-withdrawn-after-failure"
    )
    assert failed.returncode == 0, failed.stderr

    server.rank_states[NODE_2] = "running"
    server.last_seen = {
        NODE: "2026-08-11T10:01:00+00:00",
        NODE_2: "2026-08-11T10:01:00+00:00",
    }
    server.fleet_digest = "c" * 64
    server.route_published = True
    recovered, _ = _run_model(tmp_path, server, "--stop-after", "inference-recovered")
    assert recovered.returncode == 0, recovered.stderr

    server.last_seen = {
        NODE: "2026-08-11T10:02:00+00:00",
        NODE_2: "2026-08-11T10:02:00+00:00",
    }
    server.fleet_digest = "d" * 64
    completed, _ = _run_model(tmp_path, server)

    assert completed.returncode == 0, completed.stderr
    evidence = json.loads(evidence_path.read_text())
    assert evidence["completed_states"] == list(MODEL_MULTINODE_STATES)
    assert evidence["failure_node"] == NODE_2
    assert evidence["outputs"]["restart_fleet_evidence_digest"] == "d" * 64
    assert (
        evidence["qualification_sha256"]
        == hashlib.sha256((tmp_path / "qualification.json").read_bytes()).hexdigest()
    )
    assert server.route_published is False
    assert any(
        path == "/api/v1/endpoints/development-deepseek-smoke"
        for _method, path, _authorization in server.requests
    )


def test_model_runner_requires_exact_private_qualification(
    tmp_path: Path, server: SliceServer
) -> None:
    server.nodes = [NODE, NODE_2]
    qualification = _qualification(tmp_path / "qualification.json")
    document = json.loads(qualification.read_text())
    document["multinode_nodes"] = [NODE_2, NODE]
    qualification.write_bytes(_canonical(document))

    result, evidence = _run_model(tmp_path, server, qualification=qualification)

    assert result.returncode == 1
    assert result.stderr.strip() == (
        "development slice failed: model qualification does not match this phase"
    )
    assert not evidence.exists()


def test_model_runner_rejects_cross_node_artifact_identity_mismatch(
    tmp_path: Path, server: SliceServer
) -> None:
    server.nodes = [NODE, NODE_2]
    server.artifact_set_digests[NODE_2] = "6" * 64

    result, evidence_path = _run_model(tmp_path, server, "--stop-after", "running")

    assert result.returncode == 1
    assert result.stderr.strip() == (
        "development slice failed: runtime artifacts differ between target nodes"
    )
    evidence = json.loads(evidence_path.read_text())
    assert evidence["completed_states"] == MODEL_MULTINODE_STATES[:6]


@pytest.mark.parametrize("completed_count", range(len(STATES)))
def test_runner_resumes_after_every_completed_gate(
    tmp_path: Path, server: SliceServer, completed_count: int
) -> None:
    first, evidence_path = _run(
        tmp_path,
        server,
        "--stop-after",
        STATES[completed_count],
    )
    assert first.returncode == 0, first.stderr
    before = json.loads(evidence_path.read_text())
    assert before["completed_states"] == STATES[: completed_count + 1]

    second, _ = _run(tmp_path, server)

    assert second.returncode == 0, second.stderr
    assert json.loads(evidence_path.read_text())["completed_states"] == STATES


def test_runner_refuses_to_advance_past_failed_gate_and_redacts_errors(
    tmp_path: Path, server: SliceServer
) -> None:
    server.fail_path = "/api/v1/recipes/source-checks"

    result, evidence_path = _run(tmp_path, server)

    assert result.returncode == 1
    evidence = json.loads(evidence_path.read_text())
    assert evidence["completed_states"] == STATES[:3]
    assert "image-built" not in evidence["completed_states"]
    assert ADMIN_TOKEN not in result.stderr
    assert INFERENCE_TOKEN not in result.stderr
    assert "Authorization" not in result.stderr


@pytest.mark.parametrize(
    "terminal_state", ("failed", "waiting-for-operator", "expired")
)
def test_runner_retries_every_terminal_build_operation_once(
    tmp_path: Path, server: SliceServer, terminal_state: str
) -> None:
    server.build_operation_state = terminal_state

    result, evidence_path = _run(tmp_path, server)

    assert result.returncode == 0, result.stderr
    assert json.loads(evidence_path.read_text())["completed_states"] == STATES
    assert (
        sum(
            path.endswith("/retry") for _method, path, _authorization in server.requests
        )
        == 1
    )


def test_runner_stops_after_one_terminal_build_retry(
    tmp_path: Path, server: SliceServer
) -> None:
    server.build_operation_state = "waiting-for-operator"
    server.retry_operation_state = "failed"

    result, evidence_path = _run(tmp_path, server)

    assert result.returncode == 1
    assert result.stderr.strip() == (
        "development slice failed: recipe build operation ended in failed"
    )
    assert json.loads(evidence_path.read_text())["completed_states"] == STATES[:3]


def test_runner_retries_one_terminal_image_distribution(
    tmp_path: Path, server: SliceServer
) -> None:
    server.distribution_operation_state = "failed"

    result, evidence_path = _run(tmp_path, server)

    assert result.returncode == 0, result.stderr
    assert sum(path.endswith("/retry") for _, path, _ in server.requests) == 1
    assert json.loads(evidence_path.read_text())["completed_states"] == STATES


def test_runner_stops_after_one_terminal_distribution_retry(
    tmp_path: Path, server: SliceServer
) -> None:
    server.distribution_operation_state = "failed"
    server.retry_operation_state = "failed"

    result, evidence_path = _run(tmp_path, server)

    assert result.returncode == 1
    assert "image distribution operation ended in failed" in result.stderr
    assert json.loads(evidence_path.read_text())["completed_states"] == STATES[:4]


@pytest.mark.parametrize("failure", ("stale", "missing-build", "missing-runtime"))
def test_runner_requires_fresh_spark_runtime_inventory(
    tmp_path: Path, server: SliceServer, failure: str
) -> None:
    if failure == "stale":
        server.inventory_stale[NODE] = True
    elif failure == "missing-build":
        server.inventory_capabilities[NODE] = [
            "recipe.operations.v1",
            "runtime.spark-docker-nvidia.v1",
        ]
    else:
        server.inventory_capabilities[NODE] = ["recipe.operations.v1"]

    result, evidence_path = _run(tmp_path, server)

    assert result.returncode == 1
    assert "required development inventory is not ready" in result.stderr
    assert json.loads(evidence_path.read_text())["completed_states"] == []


def test_runner_revises_an_existing_same_slug_recipe_with_different_content(
    tmp_path: Path, server: SliceServer
) -> None:
    server.recipe_created = True
    server.recipe_digest = "9" * 64

    result, evidence_path = _run(tmp_path, server, "--stop-after", "recipe-resolved")

    assert result.returncode == 0, result.stderr
    evidence = json.loads(evidence_path.read_text())
    assert evidence["completed_states"] == STATES[:2]
    assert evidence["outputs"]["recipe_revision"] == 2
    assert evidence["outputs"]["recipe_content_sha256"] == RECIPE_DIGEST
    assert (
        "PUT",
        "/api/v1/catalog/recipes/10000000-0000-4000-8000-000000000001/draft",
        f"Bearer {ADMIN_TOKEN}",
    ) in server.requests


def test_runner_rejects_plain_http_to_a_non_loopback_host(tmp_path: Path) -> None:
    admin = _token(tmp_path / "admin", ADMIN_TOKEN)
    inference = _token(tmp_path / "inference", INFERENCE_TOKEN)

    result = subprocess.run(
        (
            sys.executable,
            str(RUNNER),
            "--api-base",
            "http://192.0.2.10:8080",
            "--admin-token-file",
            str(admin),
            "--inference-token-file",
            str(inference),
            "--phase",
            "synthetic",
            "--builder-node",
            NODE,
            "--target-node",
            NODE,
            "--evidence-file",
            str(tmp_path / "evidence.json"),
        ),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "plain HTTP requires an explicit loopback address" in result.stderr
    assert not (tmp_path / "evidence.json").exists()


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "permissive"])
def test_runner_rejects_unsafe_token_files(
    tmp_path: Path, server: SliceServer, unsafe: str
) -> None:
    admin = _token(tmp_path / "admin", ADMIN_TOKEN)
    inference = _token(tmp_path / "inference", INFERENCE_TOKEN)
    if unsafe == "symlink":
        admin_link = tmp_path / "admin-link"
        admin_link.symlink_to(admin)
        admin = admin_link
    elif unsafe == "hardlink":
        admin_link = tmp_path / "admin-link"
        os.link(admin, admin_link)
    else:
        admin.chmod(0o644)

    result = subprocess.run(
        (
            sys.executable,
            str(RUNNER),
            "--api-base",
            f"http://127.0.0.1:{server.server_port}",
            "--admin-token-file",
            str(admin),
            "--inference-token-file",
            str(inference),
            "--phase",
            "synthetic",
            "--builder-node",
            NODE,
            "--target-node",
            NODE,
            "--evidence-file",
            str(tmp_path / "evidence.json"),
        ),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert ADMIN_TOKEN not in result.stderr
    assert INFERENCE_TOKEN not in result.stderr
    assert not (tmp_path / "evidence.json").exists()
