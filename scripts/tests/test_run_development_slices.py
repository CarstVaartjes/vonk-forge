from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
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
RECIPE_DIGEST = "11fca06a786d0a84d2b5fe34a2ae4423327f1b79e6a3096b93714e494130842a"


class SliceServer(ThreadingHTTPServer):
    def __init__(self, address, *, fail_path: str | None = None):
        super().__init__(address, SliceHandler)
        self.fail_path = fail_path
        self.requests: list[tuple[str, str, str]] = []
        self.recipe_created = False
        self.recipe_digest = RECIPE_DIGEST
        self.source_digest = "7a65752ee1a950b3b358c66ceaf2007d0eb824a7842d0a67a5b1e3726957eb80"
        self.slug = "dev-http-smoke"
        self.route_published = False
        self.operation = 0
        self.operation_nodes: dict[str, list[str]] = {}
        self.nodes = [NODE]
        self.online = {NODE: True, NODE_2: True}
        self.last_seen = {
            NODE: "2026-08-11T10:00:00+00:00",
            NODE_2: "2026-08-11T10:00:00+00:00",
        }
        self.fleet_digest = "b" * 64


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
                            "compatibility": "compatible",
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
                    "revision_number": 1,
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
                        "revision_number": 1,
                        "lifecycle": "resolved",
                        "content_sha256": self.server.recipe_digest,
                    }
                )
            self._json(200, {"recipes": recipes, "next_cursor": None})
        elif self.path.startswith("/api/v1/recipes/operations/"):
            self._json(
                200,
                {
                    "id": self.path.rsplit("/", 1)[-1],
                    "kind": "recipe.test",
                    "owner_id": "20000000-0000-4000-8000-000000000001",
                    "state": "succeeded",
                    "plan_digest": "c" * 64,
                    "nodes": [NODE],
                    "result": {
                        "successful_nodes": sorted(
                            self.server.operation_nodes.get(
                                self.path.rsplit("/", 1)[-1], self.server.nodes
                            )
                        ),
                        "failed_nodes": [],
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
                self._json(404, {"detail": "not found"})
        else:
            self._json(404, {"detail": "not found"})

    def do_PUT(self) -> None:
        try:
            body = self._record()
        except RuntimeError:
            return
        digest = self.path.rsplit("/", 1)[-1]
        self._json(
            200,
            {
                "sha256": digest,
                "archive_bytes": len(body),
                "total_bytes": 2469,
                "file_count": 2,
                "files": ["Dockerfile", "server.py"],
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
                    "revision_number": 1,
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
                    "revision_number": 1,
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
            self._operation(
                "20000000-0000-4000-8000-000000000004", self.server.nodes
            )
        elif path.endswith("/uninstall"):
            self._operation(
                "20000000-0000-4000-8000-000000000005", self.server.nodes
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
            nodes = (
                [NODE]
                if path == "/api/v1/recipes/builds"
                else self.server.nodes
            )
            self._operation(owner, nodes)

    def _operation(self, owner: str, nodes: list[str] | None = None) -> None:
        self.server.operation += 1
        operation_id = f"40000000-0000-4000-8000-{self.server.operation:012d}"
        self.server.operation_nodes[operation_id] = list(nodes or self.server.nodes)
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


def _run_model(tmp_path: Path, server: SliceServer, *extra: str):
    admin = _token(tmp_path / "admin-token", ADMIN_TOKEN)
    inference = _token(tmp_path / "inference-token", INFERENCE_TOKEN)
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


def test_model_multinode_runner_proves_failure_recovery_restart_and_cleanup(
    tmp_path: Path, server: SliceServer
) -> None:
    server.nodes = [NODE, NODE_2]

    initial, evidence_path = _run_model(
        tmp_path, server, "--stop-after", "inference-ok"
    )
    assert initial.returncode == 0, initial.stderr

    server.online[NODE_2] = False
    server.route_published = False
    failed, _ = _run_model(
        tmp_path, server, "--stop-after", "route-withdrawn-after-failure"
    )
    assert failed.returncode == 0, failed.stderr

    server.online[NODE_2] = True
    server.last_seen = {
        NODE: "2026-08-11T10:01:00+00:00",
        NODE_2: "2026-08-11T10:01:00+00:00",
    }
    server.fleet_digest = "c" * 64
    server.route_published = True
    recovered, _ = _run_model(
        tmp_path, server, "--stop-after", "inference-recovered"
    )
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
    assert server.route_published is False
    assert any(
        path == "/api/v1/endpoints/development-deepseek-smoke"
        for _method, path, _authorization in server.requests
    )


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


def test_runner_rejects_an_existing_same_slug_recipe_with_different_content(
    tmp_path: Path, server: SliceServer
) -> None:
    server.recipe_created = True
    server.recipe_digest = "9" * 64

    result, evidence_path = _run(tmp_path, server)

    assert result.returncode == 1
    evidence = json.loads(evidence_path.read_text())
    assert evidence["completed_states"] == ["inventory-ready"]
    assert "does not match" in result.stderr


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
