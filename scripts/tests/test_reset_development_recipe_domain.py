from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest
import yaml
from vonk_control.fleet_projection import FleetSnapshot

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/reset-development-recipe-domain"
NODE = "spk_0123456789abcdef0123456789abcdef"
TOKEN = "reset-test-administrator-token"
HARNESSES = {
    "comfyui": "0d97fceaa9a7ab64bf3d826b5d9ca427ea10be79b54a8d4fe9190711c9bf70d6",
    "diffusers": "9776b37f1bf4596bf4a0c75c3e9fd6da8b25050aa2cefded2ffd5e35baa583b2",
    "ds4": "ac139f771cc97b27c1cf6fd97404b6a4db56d6d1725b4282cc5af0289a5421b3",
    "llama-cpp": "484c90559183e54bd6371db47cdccb5722dc799edda68a1e89e9cf6a8afe7615",
    "pytorch-pipeline": "29977e349e97f34a2f4f7d6a033abcc9383b0763278192c05e148b2a09cdf01c",
    "sglang": "9d3c4770fbcde4658d57312b38e82b43c09fa577e93950b860cc215938709f4c",
    "tensorrt-llm": "946575ac01969eb8b150dd0ecf6e49f86d4d3fde055a4babadde75a9325ff2a1",
    "vllm": "c0d297318f223378fe573964291bc90fc950242e0d16d1d301c7d3cb4251487d",
}
PUBLISHED_COMPOSE = ROOT / "deploy/compose/compose.dev.images.yaml"
_publisher_loader = SourceFileLoader(
    "task9_dev_runtime_project", str(ROOT / "scripts/dev-runtime-project")
)
_publisher_spec = spec_from_loader(_publisher_loader.name, _publisher_loader)
assert _publisher_spec is not None
_publisher = module_from_spec(_publisher_spec)
sys.modules[_publisher_spec.name] = _publisher
_publisher_loader.exec_module(_publisher)
PUBLISHED_COMPOSE_DOCUMENT = yaml.safe_load(
    _publisher._render_compose(
        PUBLISHED_COMPOSE.read_bytes(),
        enroll="enroll.vonk-forge.lan",
        agent="agents.vonk-forge.lan",
        direct_fabric_cidrs=("192.168.100.0/24", "192.168.101.0/24"),
    )
)
SERVICES = {
    "postgres",
    "dev-cohort-reset",
    "dev-api-cohort",
    "dev-worker-cohort",
    "dev-cohort-verify",
    "dev-repository-init",
    "dev-bootstrap",
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
    def __init__(
        self,
        address,
        *,
        events: Path,
        reset_marker: Path,
        teardown_marker: Path,
    ):
        super().__init__(address, ResetHandler)
        self.events = events
        self.reset_marker = reset_marker
        self.teardown_marker = teardown_marker
        self.run_active = True
        self.installation_active = True
        self.force_verification_failure = False


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
        if (
            self.server.teardown_marker.exists()
            and not self.server.reset_marker.exists()
        ):
            self._json(503, {"detail": "control API unavailable during reset"})
            return
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
            nodes = [
                _fleet_node(
                    loaded,
                    installed,
                    registered=not self.server.reset_marker.exists(),
                )
            ]
            snapshot = FleetSnapshot.model_validate_json(
                json.dumps(
                    {
                    "schema_version": 1,
                    "event_cursor": 7,
                    "generated_at": "2026-08-16T10:00:00+00:00",
                    "repository_commit": "a" * 40,
                    "nodes": nodes,
                    }
                )
            )
            self._json(200, snapshot.model_dump(mode="json"))
            return
        if self.path == "/api/v1/agents":
            self._json(
                200,
                {
                    "agents": (
                        []
                        if self.server.reset_marker.exists()
                        and not self.server.force_verification_failure
                        else [{"node_id": NODE, "state": "active"}]
                    )
                },
            )
            return
        if self.path.startswith("/api/v1/catalog/entities"):
            all_entities = (
                [
                    {
                        "kind": "execution-harness",
                        "publisher": "vonk-forge",
                        "slug": slug,
                        "lifecycle": "resolved",
                        "content_sha256": digest,
                    }
                    for slug, digest in sorted(HARNESSES.items())
                ]
                if self.server.reset_marker.exists()
                else []
            )
            second_page = "cursor=second-page" in self.path
            entities = all_entities[4:] if second_page else all_entities[:4]
            self._json(
                200,
                {
                    "entities": entities,
                    "next_cursor": (
                        None if second_page or not all_entities else "second-page"
                    ),
                },
            )
            return
        if self.path.startswith("/api/v1/catalog/recipes"):
            self._json(200, {"recipes": [], "next_cursor": None})
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
    loaded: list[dict[str, object]],
    installed: list[dict[str, object]],
    *,
    registered: bool,
) -> dict[str, object]:
    return {
        "id": NODE,
        "display_name": "DGX Spark 1",
        "hostname": "spark-3542",
        "lifecycle": "ready",
        "labels": {},
        "connection": {
            "agent_state": "active" if registered else "unregistered",
            "certificate_state": "valid" if registered else "missing",
            "online_state": "online" if registered else "unregistered",
            "offline_reason": None if registered else "unregistered",
            "last_seen_at": "2026-08-16T09:59:59+00:00" if registered else None,
            "last_seen_age_seconds": 1.0 if registered else None,
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
        teardown_marker=tmp_path / "teardown-complete",
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
    project_name: str = "vonk-forge",
) -> tuple[Path, Path]:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    (project / "secrets").mkdir(exist_ok=True)
    compose = project / "docker-compose.yaml"
    if not compose.exists():
        compose.write_text("services: {}\n", encoding="utf-8")
    volumes = {
        name: {"name": f"{project_name}_{name}"}
        for name in sorted(PUBLISHED_COMPOSE_DOCUMENT["volumes"])
    }
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
            for name in sorted(PUBLISHED_COMPOSE_DOCUMENT["services"])
        },
        "volumes": volumes,
    }
    config = tmp_path / "compose-config.json"
    config.write_text(json.dumps(document), encoding="utf-8")
    return project, config


def test_reset_boundary_matches_published_nameless_compose_artifact() -> None:
    assert "name" not in PUBLISHED_COMPOSE_DOCUMENT
    assert set(PUBLISHED_COMPOSE_DOCUMENT["services"]) == SERVICES
    assert set(PUBLISHED_COMPOSE_DOCUMENT["volumes"]) == VOLUMES


def _fake_docker(
    tmp_path: Path,
    *,
    orphan: bool = False,
    anonymous_volume: bool = False,
) -> Path:
    state_path = tmp_path / "docker-state.json"
    if not state_path.exists():
        volumes = {
            f"vonk-forge_{name}": {
                "Name": f"vonk-forge_{name}",
                "Labels": {
                    "com.docker.compose.project": "vonk-forge",
                    "com.docker.compose.volume": name,
                },
            }
            for name in sorted(VOLUMES)
        }
        containers = [
            {
                "Id": f"container-{name}",
                "Config": {
                    "Labels": {
                        "com.docker.compose.project": "vonk-forge",
                        "com.docker.compose.service": name,
                    }
                },
                "Mounts": [],
            }
            for name in sorted(SERVICES)
        ]
        containers[0]["Mounts"] = [
            {
                "Type": "volume",
                "Name": "vonk-forge_dev-postgres-data",
                "Destination": "/var/lib/postgresql",
            }
        ]
        if orphan:
            containers.append(
                {
                    "Id": "container-orphan",
                    "Config": {
                        "Labels": {
                            "com.docker.compose.project": "vonk-forge",
                            "com.docker.compose.service": "prototype-orphan",
                        }
                    },
                    "Mounts": [],
                }
            )
        if anonymous_volume:
            containers[0]["Mounts"].append(
                {
                    "Type": "volume",
                    "Name": "d34db33fanonymous",
                    "Destination": "/prototype",
                }
            )
        state_path.write_text(
            json.dumps({"containers": containers, "volumes": volumes}),
            encoding="utf-8",
        )
    executable = tmp_path / "docker"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

arguments = sys.argv[1:]
events = Path(os.environ["RESET_TEST_EVENTS"])
with events.open("a", encoding="utf-8") as stream:
    stream.write("docker " + " ".join(arguments) + "\\n")
state_path = Path(os.environ["RESET_TEST_DOCKER_STATE"])
state = json.loads(state_path.read_text())

def save():
    state_path.write_text(json.dumps(state, sort_keys=True))

def fail_once(stage):
    if os.environ.get("RESET_TEST_FAIL_AT") != stage:
        return
    marker = Path(os.environ["RESET_TEST_FAILURE_MARKERS"]) / stage
    if not marker.exists():
        marker.touch()
        raise SystemExit(91)

if arguments and arguments[0] == "compose":
    if "--project-name" not in arguments or arguments[arguments.index("--project-name") + 1] != "vonk-forge":
        raise SystemExit(40)
    compose_file = Path(arguments[arguments.index("--file") + 1])
    if "config" in arguments:
        sys.stdout.write(Path(os.environ["RESET_TEST_CONFIG"]).read_text())
        if os.environ.get("RESET_TEST_SWAP_GRAPH") == "1":
            Path(os.environ["RESET_TEST_PROJECT_COMPOSE"]).write_text("services:\\n  attacker: {}\\n")
    elif "stop" in arguments:
        fail_once("stop")
        if compose_file == Path(os.environ["RESET_TEST_PROJECT_COMPOSE"]):
            raise SystemExit(41)
    elif "down" in arguments:
        fail_once("down")
        if "--volumes" in arguments or "--remove-orphans" in arguments:
            raise SystemExit(42)
        if compose_file == Path(os.environ["RESET_TEST_PROJECT_COMPOSE"]):
            raise SystemExit(43)
        state["containers"] = []
        save()
        Path(os.environ["RESET_TEST_TEARDOWN_MARKER"]).touch()
    elif "run" in arguments:
        fail_once("migrate")
        print(os.environ["RESET_TEST_REVISION"])
    elif "up" in arguments and "postgres" in arguments:
        fail_once("postgres")
    elif "up" in arguments:
        fail_once("stack")
        Path(os.environ["RESET_TEST_STARTED"]).touch()
        Path(os.environ["RESET_TEST_RESET_MARKER"]).touch()
elif arguments[:1] == ["ps"]:
    for container in state["containers"]:
        print(container["Id"])
elif arguments[:2] == ["container", "inspect"]:
    selected = set(arguments[2:])
    print(json.dumps([item for item in state["containers"] if item["Id"] in selected]))
elif arguments[:2] == ["volume", "ls"]:
    name_filters = [
        value.removeprefix("name=")
        for index, value in enumerate(arguments)
        if index and arguments[index - 1] == "--filter" and value.startswith("name=")
    ]
    for name, item in state["volumes"].items():
        if name_filters:
            if name in name_filters:
                print(name)
        elif item["Labels"].get("com.docker.compose.project") == "vonk-forge":
            print(name)
elif arguments[:2] == ["volume", "inspect"]:
    print(json.dumps([state["volumes"][name] for name in arguments[2:] if name in state["volumes"]]))
elif arguments[:2] == ["volume", "rm"]:
    fail_once("volumes")
    for name in arguments[2:]:
        state["volumes"].pop(name, None)
    save()
else:
    raise SystemExit(44)
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
    revision: str = "0001_fleet_library_baseline",
    symlink_project: bool = False,
    project_name: str = "vonk-forge",
    docker_mode: str = "direct",
    orphan: bool = False,
    anonymous_volume: bool = False,
    swap_graph: bool = False,
    fail_at: str | None = None,
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
    docker = _fake_docker(
        tmp_path,
        orphan=orphan,
        anonymous_volume=anonymous_volume,
    )
    _fake_sudo(tmp_path)
    failure_markers = tmp_path / "failure-markers"
    failure_markers.mkdir(exist_ok=True)
    reset_state = tmp_path / "reset-state"
    reset_state.mkdir(mode=0o700, exist_ok=True)
    drained = tmp_path / "drained"
    original_append = reset_server.events
    environment = os.environ.copy()
    environment.update(
        {
            "RESET_TEST_CONFIG": str(config),
            "RESET_TEST_DRAINED": str(drained),
            "RESET_TEST_EVENTS": str(original_append),
            "RESET_TEST_RESET_MARKER": str(reset_server.reset_marker),
            "RESET_TEST_TEARDOWN_MARKER": str(reset_server.teardown_marker),
            "RESET_TEST_REVISION": revision,
            "RESET_TEST_STARTED": str(tmp_path / "started"),
            "RESET_TEST_DOCKER_STATE": str(tmp_path / "docker-state.json"),
            "RESET_TEST_FAILURE_MARKERS": str(failure_markers),
            "RESET_TEST_PROJECT_COMPOSE": str(project / "docker-compose.yaml"),
            "RESET_TEST_SWAP_GRAPH": "1" if swap_graph else "0",
            "PATH": f"{tmp_path}{os.pathsep}{environment['PATH']}",
        }
    )
    if fail_at is None:
        environment.pop("RESET_TEST_FAIL_AT", None)
    else:
        environment["RESET_TEST_FAIL_AT"] = fail_at
    reset_server.force_verification_failure = fail_at == "verify"
    # The handler and fake Docker share this marker; the real observable is that
    # down refuses to run until both API operations have completed.
    reset_server.drained_marker = drained  # type: ignore[attr-defined]
    return subprocess.run(
        (
            str(SCRIPT),
            *arguments,
            "--project-directory",
            str(project),
            "--project-name",
            project_name,
            "--journal-file",
            str(tmp_path / "reset-state" / "journal.json"),
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
    assert "project name must be exactly vonk-forge" in result.stderr
    assert not reset_server.events.exists()


def test_reset_rejects_orphan_project_container_before_stop_or_deletion(
    tmp_path: Path, reset_server: ResetServer
) -> None:
    result = _run(
        tmp_path,
        reset_server,
        "--environment",
        "development",
        "--confirm-destructive-preproduction-reset",
        orphan=True,
    )

    assert result.returncode != 0
    assert "orphan Compose container" in result.stderr
    events = reset_server.events.read_text().splitlines()
    assert not any(" compose " in event and " stop" in event for event in events)
    assert not any("volume rm" in event for event in events)


def test_reset_rejects_anonymous_container_volume_before_stop_or_deletion(
    tmp_path: Path, reset_server: ResetServer
) -> None:
    result = _run(
        tmp_path,
        reset_server,
        "--environment",
        "development",
        "--confirm-destructive-preproduction-reset",
        anonymous_volume=True,
    )

    assert result.returncode != 0
    assert "anonymous or foreign container volume" in result.stderr
    events = reset_server.events.read_text().splitlines()
    assert not any(" compose " in event and " stop" in event for event in events)
    assert not any("volume rm" in event for event in events)


def test_reset_uses_frozen_snapshot_when_project_compose_changes_after_validation(
    tmp_path: Path, reset_server: ResetServer
) -> None:
    result = _run(
        tmp_path,
        reset_server,
        "--environment",
        "development",
        "--confirm-destructive-preproduction-reset",
        swap_graph=True,
    )

    assert result.returncode == 0, result.stderr
    journal = json.loads((tmp_path / "reset-state/journal.json").read_text())
    snapshot = Path(journal["compose_snapshot_path"])
    assert snapshot.parent == tmp_path / "reset-state"
    assert snapshot != tmp_path / "project/docker-compose.yaml"
    assert (
        snapshot.read_bytes()
        == (
            json.dumps(
                json.loads((tmp_path / "compose-config.json").read_text()),
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
    )
    mutating = [
        event
        for event in reset_server.events.read_text().splitlines()
        if " compose " in event
        and any(command in event for command in (" stop", " down", " up", " run"))
    ]
    assert mutating
    assert all(f"--file {snapshot}" in event for event in mutating)
    assert all(
        "--volumes" not in event and "--remove-orphans" not in event
        for event in mutating
    )


@pytest.mark.parametrize(
    "failure_boundary",
    ("stop", "down", "volumes", "postgres", "migrate", "stack", "verify"),
)
def test_reset_journal_resumes_each_destructive_boundary_without_requiring_api(
    tmp_path: Path,
    reset_server: ResetServer,
    failure_boundary: str,
) -> None:
    first = _run(
        tmp_path,
        reset_server,
        "--environment",
        "development",
        "--confirm-destructive-preproduction-reset",
        fail_at=failure_boundary,
    )
    assert first.returncode != 0
    journal_path = tmp_path / "reset-state/journal.json"
    first_journal = json.loads(journal_path.read_text())
    first_api_posts = [
        event
        for event in reset_server.events.read_text().splitlines()
        if event.startswith("api POST")
    ]

    second = _run(
        tmp_path,
        reset_server,
        "--environment",
        "development",
        "--confirm-destructive-preproduction-reset",
    )

    assert second.returncode == 0, second.stderr
    completed = json.loads(journal_path.read_text())
    assert completed["reset_id"] == first_journal["reset_id"]
    assert completed["completed_phases"][-1] == "verified"
    second_api_posts = [
        event
        for event in reset_server.events.read_text().splitlines()
        if event.startswith("api POST")
    ]
    assert second_api_posts == first_api_posts


def test_reset_journal_and_snapshot_are_private_hash_bound_and_outside_project(
    tmp_path: Path, reset_server: ResetServer
) -> None:
    result = _run(
        tmp_path,
        reset_server,
        "--environment",
        "development",
        "--confirm-destructive-preproduction-reset",
    )

    assert result.returncode == 0, result.stderr
    journal_path = tmp_path / "reset-state/journal.json"
    journal = json.loads(journal_path.read_text())
    snapshot = Path(journal["compose_snapshot_path"])
    assert stat.S_IMODE(journal_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o400
    assert (
        hashlib.sha256(snapshot.read_bytes()).hexdigest()
        == journal["compose_snapshot_sha256"]
    )
    assert {path.name for path in (tmp_path / "project").iterdir()} == {
        "docker-compose.yaml",
        "secrets",
    }
    events = reset_server.events.read_text().splitlines()
    removed = next(event for event in events if "volume rm" in event)
    assert set(removed.split()[3:]) == {f"vonk-forge_{name}" for name in VOLUMES}


def test_reset_resume_rejects_exact_named_volume_whose_project_labels_changed(
    tmp_path: Path, reset_server: ResetServer
) -> None:
    reset_arguments = (
        "--environment",
        "development",
        "--confirm-destructive-preproduction-reset",
    )
    interrupted = _run(tmp_path, reset_server, *reset_arguments, fail_at="volumes")
    assert interrupted.returncode == 1
    state_path = tmp_path / "docker-state.json"
    state = json.loads(state_path.read_text())
    state["volumes"]["vonk-forge_dev-postgres-data"]["Labels"] = {}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = reset_server.events.read_text()

    resumed = _run(tmp_path, reset_server, *reset_arguments)

    assert resumed.returncode == 1
    assert "volume labels are invalid" in resumed.stderr
    assert "volume rm" not in reset_server.events.read_text()[len(before) :]
    assert not (tmp_path / "started").exists()


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
        "fresh database revision is not 0001_fleet_library_baseline" in result.stderr
    )
    assert not (tmp_path / "started").exists()
