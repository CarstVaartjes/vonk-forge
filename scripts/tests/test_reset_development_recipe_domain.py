from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/reset-development-recipe-domain"
NODE = "spk_0123456789abcdef0123456789abcdef"
TOKEN = "reset-test-administrator-token"
HARNESSES = {
    "comfyui",
    "diffusers",
    "ds4",
    "llama-cpp",
    "pytorch-pipeline",
    "sglang",
    "tensorrt-llm",
    "vllm",
}
SERVICES = {
    "postgres",
    "dev-cohort-reset",
    "dev-api-cohort",
    "dev-worker-cohort",
    "dev-cohort-verify",
    "dev-repository-init",
    "dev-init",
    "dev-supervisor-init",
    "migrate",
    "dev-auth-init",
    "dev-litellm-database-init",
    "control-worker",
    "control-api",
    "litellm",
    "caddy",
    "tailscale-gateway",
    "tailscale-configurator",
}
VOLUMES = {
    "dev-auth-secrets",
    "dev-postgres-data",
    "dev-image-cohort",
    "dev-control-identity",
    "dev-control-state",
    "dev-route-publications",
    "dev-supervisor-state",
    "dev-repository",
    "dev-api-secrets",
    "dev-caddy-secrets",
    "dev-litellm-secrets",
    "dev-litellm-database-secrets",
    "dev-migrate-secrets",
    "dev-runtime-config",
    "dev-tailscale-secrets",
    "dev-tailscale-runtime",
    "dev-tailscale-socket",
    "dev-tailscale-state",
    "dev-worker-secrets",
}


def _append(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


class ResetServer(ThreadingHTTPServer):
    def __init__(self, address, *, events: Path, reset_marker: Path):
        super().__init__(address, ResetHandler)
        self.events = events
        self.reset_marker = reset_marker
        self.run_active = True
        self.installation_active = True


class ResetHandler(BaseHTTPRequestHandler):
    server: ResetServer

    def log_message(self, _format: str, *_args: object) -> None:
        pass

    def _json(self, status: int, value: object) -> None:
        body = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if self.headers.get("authorization") == f"Bearer {TOKEN}":
            return True
        self._json(401, {"detail": "authentication required"})
        return False

    def _body(self) -> dict[str, object]:
        length = int(self.headers.get("content-length", "0"))
        value = json.loads(self.rfile.read(length) or b"{}")
        assert isinstance(value, dict)
        return value

    def do_GET(self) -> None:
        if not self._authorized():
            return
        _append(self.server.events, f"api GET {self.path}")
        if self.path == "/api/v1/fleet":
            loaded = (
                [
                    {
                        "run_id": "20000000-0000-4000-8000-000000000001",
                        "installation_id": "30000000-0000-4000-8000-000000000001",
                        "recipe_id": "40000000-0000-4000-8000-000000000001",
                        "recipe_revision_id": "50000000-0000-4000-8000-000000000001",
                        "title": "Test recipe",
                        "alias": "test-recipe",
                        "expected_rank_count": 1,
                        "present_ranks": [0],
                        "member_node_ids": [NODE],
                        "rank": 0,
                        "role": "entrypoint",
                        "run_state": "running",
                        "route_state": "published",
                        "rank_state": "running",
                        "rank_age_seconds": 1.0,
                        "rank_fresh": True,
                        "group_state": "healthy",
                        "healthy": True,
                        "degraded_reason": None,
                    }
                ]
                if self.server.run_active and not self.server.reset_marker.exists()
                else []
            )
            installed = (
                [
                    {
                        "installation_id": "30000000-0000-4000-8000-000000000001",
                        "recipe_id": "40000000-0000-4000-8000-000000000001",
                        "recipe_revision_id": "50000000-0000-4000-8000-000000000001",
                        "title": "Test recipe",
                        "topology_name": "solo",
                        "expected_rank_count": 1,
                        "present_ranks": [0],
                        "member_node_ids": [NODE],
                        "rank": 0,
                        "role": "entrypoint",
                        "group_state": "installed",
                        "rank_state": "installed",
                        "complete": True,
                        "degraded_reason": None,
                    }
                ]
                if self.server.installation_active
                and not self.server.reset_marker.exists()
                else []
            )
            nodes = (
                []
                if self.server.reset_marker.exists()
                else [_fleet_node(loaded, installed)]
            )
            self._json(
                200,
                {
                    "schema_version": 1,
                    "event_cursor": 7,
                    "generated_at": "2026-08-16T10:00:00+00:00",
                    "repository_commit": "a" * 40,
                    "nodes": nodes,
                },
            )
            return
        if self.path.startswith("/api/v1/catalog/entities"):
            entities = (
                [
                    {
                        "kind": "execution-harness",
                        "publisher": "vonk-forge",
                        "slug": slug,
                        "lifecycle": "resolved",
                        "content_sha256": f"{index:064x}",
                    }
                    for index, slug in enumerate(sorted(HARNESSES), start=1)
                ]
                if self.server.reset_marker.exists()
                else []
            )
            self._json(200, {"entities": entities, "next_cursor": None})
            return
        self._json(404, {"detail": "not found"})

    def do_POST(self) -> None:
        if not self._authorized():
            return
        body = self._body()
        _append(self.server.events, f"api POST {self.path}")
        if self.path == "/api/v1/recipes/stop-plans/preview":
            assert body == {"run_id": "20000000-0000-4000-8000-000000000001"}
            self._json(200, {"allowed": True, "plan_digest": "a" * 64})
            return
        if self.path.endswith("/stop"):
            assert body["plan_digest"] == "a" * 64
            uuid.UUID(str(body["request_key"]))
            self.server.run_active = False
            self._json(
                202,
                {
                    "id": "60000000-0000-4000-8000-000000000001",
                    "owner_id": "20000000-0000-4000-8000-000000000001",
                    "state": "succeeded",
                    "result": {},
                },
            )
            return
        if self.path == "/api/v1/recipes/uninstall-plans/preview":
            assert self.server.run_active is False
            assert body == {"installation_id": "30000000-0000-4000-8000-000000000001"}
            self._json(200, {"allowed": True, "plan_digest": "b" * 64})
            return
        if self.path.endswith("/uninstall"):
            assert body["plan_digest"] == "b" * 64
            uuid.UUID(str(body["request_key"]))
            self.server.installation_active = False
            self.server.drained_marker.touch()  # type: ignore[attr-defined]
            self._json(
                202,
                {
                    "id": "70000000-0000-4000-8000-000000000001",
                    "owner_id": "30000000-0000-4000-8000-000000000001",
                    "state": "succeeded",
                    "result": {},
                },
            )
            return
        self._json(404, {"detail": "not found"})


def _fleet_node(
    loaded: list[dict[str, object]], installed: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "id": NODE,
        "display_name": "dgx-spark-1",
        "hostname": "dgx-spark-1.example.test",
        "lifecycle": "active",
        "labels": {},
        "connection": {
            "agent_state": "active",
            "certificate_state": "active",
            "online_state": "online",
            "offline_reason": None,
            "last_seen_at": "2026-08-16T09:59:59+00:00",
            "last_seen_age_seconds": 1.0,
        },
        "inventory": None,
        "telemetry": None,
        "installed": installed,
        "loaded": loaded,
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
def reset_server(tmp_path: Path):
    server = ResetServer(
        ("127.0.0.1", 0),
        events=tmp_path / "events.log",
        reset_marker=tmp_path / "reset-complete",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _project(
    tmp_path: Path,
    *,
    extra_volume: bool = False,
    redirected_volume: bool = False,
    project_name: str = "vonk-forge-dev",
) -> tuple[Path, Path]:
    project = tmp_path / "project"
    project.mkdir()
    (project / "secrets").mkdir()
    (project / "docker-compose.yaml").write_text("services: {}\n", encoding="utf-8")
    volumes = {name: {"name": f"{project_name}_{name}"} for name in sorted(VOLUMES)}
    if extra_volume:
        volumes["customer-data"] = {"name": "customer-data"}
    if redirected_volume:
        volumes["dev-postgres-data"] = {"name": "customer-data"}
    document = {
        "name": project_name,
        "services": {
            name: {
                "environment": (
                    {"VONK_DEPLOYMENT_MODE": "development"}
                    if name in {"control-api", "control-worker"}
                    else {}
                )
            }
            for name in sorted(SERVICES)
        },
        "volumes": volumes,
    }
    config = tmp_path / "compose-config.json"
    config.write_text(json.dumps(document), encoding="utf-8")
    return project, config


def _fake_docker(
    tmp_path: Path, *, revision: str = "0027_execution_harness_catalog"
) -> Path:
    executable = tmp_path / "docker"
    executable.write_text(
        """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

arguments = sys.argv[1:]
events = Path(os.environ["RESET_TEST_EVENTS"])
with events.open("a", encoding="utf-8") as stream:
    stream.write("docker " + " ".join(arguments) + "\\n")
if "config" in arguments:
    sys.stdout.write(Path(os.environ["RESET_TEST_CONFIG"]).read_text())
elif "down" in arguments:
    if not Path(os.environ["RESET_TEST_DRAINED"]).exists():
        raise SystemExit(19)
    Path(os.environ["RESET_TEST_RESET_MARKER"]).touch()
elif "run" in arguments:
    print(os.environ["RESET_TEST_REVISION"])
elif "up" in arguments and "postgres" not in arguments:
    Path(os.environ["RESET_TEST_STARTED"]).touch()
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _fake_sudo(tmp_path: Path) -> Path:
    executable = tmp_path / "sudo"
    executable.write_text(
        """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

arguments = sys.argv[1:]
with Path(os.environ["RESET_TEST_EVENTS"]).open("a", encoding="utf-8") as stream:
    stream.write("sudo " + " ".join(arguments) + "\\n")
if not arguments or arguments[0] != "-n":
    raise SystemExit(17)
os.execv(arguments[1], arguments[1:])
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _token(tmp_path: Path) -> Path:
    path = tmp_path / "admin-token"
    path.write_text(TOKEN + "\n", encoding="ascii")
    path.chmod(0o600)
    return path


def _run(
    tmp_path: Path,
    reset_server: ResetServer,
    *arguments: str,
    extra_volume: bool = False,
    redirected_volume: bool = False,
    revision: str = "0027_execution_harness_catalog",
    symlink_project: bool = False,
    project_name: str = "vonk-forge-dev",
    docker_mode: str = "direct",
) -> subprocess.CompletedProcess[str]:
    project, config = _project(
        tmp_path,
        extra_volume=extra_volume,
        redirected_volume=redirected_volume,
        project_name=project_name,
    )
    if symlink_project:
        linked_project = tmp_path / "linked-project"
        linked_project.symlink_to(project, target_is_directory=True)
        project = linked_project
    docker = _fake_docker(tmp_path, revision=revision)
    _fake_sudo(tmp_path)
    drained = tmp_path / "drained"
    original_append = reset_server.events
    environment = os.environ.copy()
    environment.update(
        {
            "RESET_TEST_CONFIG": str(config),
            "RESET_TEST_DRAINED": str(drained),
            "RESET_TEST_EVENTS": str(original_append),
            "RESET_TEST_RESET_MARKER": str(reset_server.reset_marker),
            "RESET_TEST_REVISION": revision,
            "RESET_TEST_STARTED": str(tmp_path / "started"),
            "PATH": f"{tmp_path}{os.pathsep}{environment['PATH']}",
        }
    )
    # The handler and fake Docker share this marker; the real observable is that
    # down refuses to run until both API operations have completed.
    reset_server.drained_marker = drained  # type: ignore[attr-defined]
    return subprocess.run(
        (
            str(SCRIPT),
            *arguments,
            "--project-directory",
            str(project),
            "--api-base",
            f"http://127.0.0.1:{reset_server.server_port}",
            "--admin-token-file",
            str(_token(tmp_path)),
            "--docker-command",
            str(docker),
            "--docker-mode",
            docker_mode,
            "--timeout-seconds",
            "2",
            "--poll-seconds",
            "0.01",
        ),
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_reset_requires_exact_confirmation_before_any_mutation(
    tmp_path: Path, reset_server: ResetServer
) -> None:
    result = _run(tmp_path, reset_server, "--environment", "development")

    assert result.returncode != 0
    assert "--confirm-destructive-preproduction-reset" in result.stderr
    assert not reset_server.events.exists()


def test_reset_rejects_non_development_environment_before_any_mutation(
    tmp_path: Path, reset_server: ResetServer
) -> None:
    result = _run(
        tmp_path,
        reset_server,
        "--environment",
        "production",
        "--confirm-destructive-preproduction-reset",
    )

    assert result.returncode != 0
    assert "environment must be exactly development" in result.stderr
    assert not reset_server.events.exists()


def test_reset_refuses_unbounded_compose_volume_before_api_or_docker_mutation(
    tmp_path: Path, reset_server: ResetServer
) -> None:
    result = _run(
        tmp_path,
        reset_server,
        "--environment",
        "development",
        "--confirm-destructive-preproduction-reset",
        extra_volume=True,
    )

    assert result.returncode != 0
    assert (
        "development Compose graph is not the bounded reset contract" in result.stderr
    )
    events = reset_server.events.read_text().splitlines()
    assert len(events) == 1
    assert " config --format json" in events[0]


def test_reset_refuses_symlinked_project_before_docker_or_api_access(
    tmp_path: Path, reset_server: ResetServer
) -> None:
    result = _run(
        tmp_path,
        reset_server,
        "--environment",
        "development",
        "--confirm-destructive-preproduction-reset",
        symlink_project=True,
    )

    assert result.returncode != 0
    assert "development Compose project is unsafe" in result.stderr
    assert not reset_server.events.exists()


def test_reset_refuses_expected_volume_key_redirected_to_foreign_named_volume(
    tmp_path: Path, reset_server: ResetServer
) -> None:
    result = _run(
        tmp_path,
        reset_server,
        "--environment",
        "development",
        "--confirm-destructive-preproduction-reset",
        redirected_volume=True,
    )

    assert result.returncode != 0
    assert (
        "development Compose graph is not the bounded reset contract" in result.stderr
    )
    events = reset_server.events.read_text().splitlines()
    assert len(events) == 1
    assert " config --format json" in events[0]


def test_reset_refuses_foreign_compose_project_with_matching_volume_suffixes(
    tmp_path: Path, reset_server: ResetServer
) -> None:
    result = _run(
        tmp_path,
        reset_server,
        "--environment",
        "development",
        "--confirm-destructive-preproduction-reset",
        project_name="customer-production",
    )

    assert result.returncode != 0
    assert (
        "development Compose graph is not the bounded reset contract" in result.stderr
    )
    events = reset_server.events.read_text().splitlines()
    assert len(events) == 1
    assert " config --format json" in events[0]


def test_reset_drains_workloads_recreates_exact_head_and_verifies_fresh_catalog(
    tmp_path: Path, reset_server: ResetServer
) -> None:
    drained = tmp_path / "drained"
    result = _run(
        tmp_path,
        reset_server,
        "--environment",
        "development",
        "--confirm-destructive-preproduction-reset",
    )

    # The reset script must have reached every externally visible postcondition.
    assert result.returncode == 0, result.stderr
    assert reset_server.run_active is False
    assert reset_server.installation_active is False
    assert reset_server.reset_marker.exists()
    assert (tmp_path / "started").exists()
    assert "fresh browser session" in result.stdout
    assert "re-enroll every Spark" in result.stdout
    events = reset_server.events.read_text().splitlines()
    stop = events.index(
        "api POST /api/v1/recipes/runs/20000000-0000-4000-8000-000000000001/stop"
    )
    uninstall = events.index(
        "api POST /api/v1/recipes/installations/30000000-0000-4000-8000-000000000001/uninstall"
    )
    down = next(index for index, event in enumerate(events) if " down " in f" {event} ")
    assert stop < uninstall < down
    assert drained.exists()


def test_reset_supports_noninteractive_sudo_docker_authority(
    tmp_path: Path, reset_server: ResetServer
) -> None:
    result = _run(
        tmp_path,
        reset_server,
        "--environment",
        "development",
        "--confirm-destructive-preproduction-reset",
        docker_mode="sudo",
    )

    assert result.returncode == 0, result.stderr
    events = reset_server.events.read_text().splitlines()
    sudo_commands = [event for event in events if event.startswith("sudo ")]
    docker_commands = [event for event in events if event.startswith("docker ")]
    assert sudo_commands
    assert len(sudo_commands) == len(docker_commands)
    assert all(command.startswith("sudo -n ") for command in sudo_commands)


def test_reset_never_starts_services_when_migration_head_is_not_exact(
    tmp_path: Path, reset_server: ResetServer
) -> None:
    result = _run(
        tmp_path,
        reset_server,
        "--environment",
        "development",
        "--confirm-destructive-preproduction-reset",
        revision="0026_telemetry_maintenance_state",
    )

    assert result.returncode != 0
    assert (
        "fresh database revision is not 0027_execution_harness_catalog" in result.stderr
    )
    assert not (tmp_path / "started").exists()
