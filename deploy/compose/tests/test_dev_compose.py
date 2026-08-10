from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
COMPOSE = ROOT / "deploy/compose/compose.dev.yaml"
IMAGE_TEMPLATE = ROOT / "deploy/compose/compose.dev.images.yaml"
SCRIPT = ROOT / "scripts/dev-compose"
SECRETS_HELPER = ROOT / "scripts/dev-compose-secrets.py"
EXPECTED_COMMIT = "a" * 40
DEV_API_IMAGE = "vonk-forge-dev/control-api@sha256:" + "0" * 64
DEV_WORKER_IMAGE = "vonk-forge-dev/control-worker@sha256:" + "1" * 64
CADDY_IMAGE = (
    "caddy:2.11.4@sha256:"
    "844f60b64e4724a5aa8245e019dace0d3f199f7433ce6c57676cb30a920dbad9"
)
LITELLM_IMAGE = (
    "ghcr.io/berriai/litellm:v1.83.14-stable@sha256:"
    "c81eb79cd4333c6cfe374c0ec929110fd23f0ee5f7fd198855a6fbddc77b83ba"
)


def _rendered(tmp_path: Path) -> dict[str, object]:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for name, value in {
        "database-url": "postgresql+psycopg://control:dev@postgres:5432/control\n",
        "postgres-password": "dev\n",
        "git-signing-key": "development-key\n",
    }.items():
        (secrets / name).write_text(value, encoding="utf-8")
    environment = os.environ | {
        "VONK_DEV_EXPECTED_COMMIT": EXPECTED_COMMIT,
        "VONK_DEV_ORIGIN_DIR": str(tmp_path / "one-use-origin.git"),
        "VONK_DEV_SECRETS_DIR": str(secrets),
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE),
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return json.loads(result.stdout)


def _rendered_image_template() -> dict[str, object]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(IMAGE_TEMPLATE),
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_dev_compose_builds_local_control_services_without_release_images(
    tmp_path: Path,
) -> None:
    rendered = _rendered(tmp_path)
    services = rendered["services"]

    assert services["control-api"]["build"]["target"] == "api"
    assert services["control-worker"]["build"]["target"] == "worker"
    assert services["migrate"]["build"]["target"] == "api"
    assert "image" not in services["control-api"]
    assert "image" not in services["control-worker"]
    assert services["control-api"]["environment"]["VONK_DEPLOYMENT_MODE"] == (
        "development"
    )
    assert services["control-api"]["environment"]["VONK_AGENT_RUNTIME"] == (
        "disabled"
    )
    assert services["control-api"]["ports"] == [
        {
            "host_ip": "127.0.0.1",
            "mode": "ingress",
            "published": "8080",
            "protocol": "tcp",
            "target": 8000,
        }
    ]


def test_local_source_compose_remains_distinct_from_published_image_template() -> None:
    image_template = ROOT / "deploy/compose/compose.dev.images.yaml"

    assert COMPOSE.exists()
    assert image_template.exists()
    assert "build:" in COMPOSE.read_text(encoding="utf-8")
    assert "__VONK_API_IMAGE__" in image_template.read_text(encoding="utf-8")


def test_dev_compose_initializes_identity_before_api_and_worker(tmp_path: Path) -> None:
    services = _rendered(tmp_path)["services"]

    assert "dev-init" in services
    assert services["dev-init"]["user"] == "0:0"
    assert services["control-api"]["depends_on"]["dev-init"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["control-worker"]["depends_on"]["dev-init"]["condition"] == (
        "service_completed_successfully"
    )


def _volumes_by_target(service: dict[str, object]) -> dict[str, dict[str, object]]:
    return {volume["target"]: volume for volume in service.get("volumes", [])}


def test_image_template_hardens_caddy_as_the_only_lan_listener() -> None:
    rendered = _rendered_image_template()
    services = rendered["services"]
    caddy = services["caddy"]

    assert caddy["image"] == CADDY_IMAGE
    assert caddy["pull_policy"] == "always"
    assert caddy["user"] == "10000:10000"
    assert caddy["read_only"] is True
    assert caddy["cap_drop"] == ["ALL"]
    assert caddy["security_opt"] == ["no-new-privileges:true"]
    assert set(caddy["tmpfs"]) == {"/config", "/data", "/tmp"}
    assert caddy["entrypoint"] == [
        "/bin/sh",
        "/usr/local/bin/vonk-caddy-entrypoint",
    ]
    assert caddy["environment"] == {
        "VONK_AGENT_ENROLL_HOSTNAME": "enroll.vonk-forge.lan",
        "VONK_AGENT_HOSTNAME": "agents.vonk-forge.lan",
        "VONK_BACKEND_PORT": "8443",
    }
    assert caddy["ports"] == [
        {
            "mode": "ingress",
            "published": "8443",
            "protocol": "tcp",
            "target": 8443,
        }
    ]
    assert set(caddy["networks"]) == {"application"}
    assert caddy["healthcheck"] == {
        "test": ["CMD-SHELL", "test -r /tmp/vonk-agent-proxy-auth.caddy"],
        "interval": "10s",
        "timeout": "5s",
        "retries": 12,
    }
    assert caddy["depends_on"] == {
        "control-api": {
            "condition": "service_healthy",
            "required": True,
        },
        "dev-init": {
            "condition": "service_completed_successfully",
            "required": True,
        },
    }

    volumes = _volumes_by_target(caddy)
    assert set(volumes) == {
        "/etc/caddy/Caddyfile",
        "/run/secrets",
        "/usr/local/bin/vonk-caddy-entrypoint",
    }
    assert volumes["/run/secrets"] == {
        "type": "volume",
        "source": "dev-caddy-secrets",
        "target": "/run/secrets",
        "read_only": True,
        "volume": {},
    }
    for target, subpath in {
        "/etc/caddy/Caddyfile": "Caddyfile",
        "/usr/local/bin/vonk-caddy-entrypoint": "caddy-entrypoint.sh",
    }.items():
        assert volumes[target] == {
            "type": "volume",
            "source": "dev-runtime-config",
            "target": target,
            "read_only": True,
            "volume": {"subpath": subpath},
        }

    listeners = {
        name: service["ports"]
        for name, service in services.items()
        if service.get("ports")
    }
    assert set(listeners) == {"caddy", "control-api"}
    assert listeners["control-api"] == [
        {
            "host_ip": "127.0.0.1",
            "mode": "ingress",
            "published": "8080",
            "protocol": "tcp",
            "target": 8000,
        }
    ]
    assert all(
        port.get("host_ip") == "127.0.0.1"
        for name, ports in listeners.items()
        if name != "caddy"
        for port in ports
    )


def test_image_template_runs_acknowledged_litellm_only_on_internal_networks() -> None:
    rendered = _rendered_image_template()
    services = rendered["services"]
    litellm = services["litellm"]

    assert litellm["image"] == LITELLM_IMAGE
    assert litellm["pull_policy"] == "always"
    assert litellm["user"] == "10002:10001"
    assert litellm["read_only"] is True
    assert litellm["cap_drop"] == ["ALL"]
    assert litellm["security_opt"] == ["no-new-privileges:true"]
    assert litellm["tmpfs"] == ["/tmp"]
    assert litellm["entrypoint"] == ["/app/vonk-entrypoint"]
    assert litellm["environment"] == {
        "DISABLE_ADMIN_UI": "False",
        "LITELLM_UI_PATH": "/tmp/litellm-ui",
        "SERVER_ROOT_PATH": "/litellm",
        "STORE_MODEL_IN_DB": "False",
    }
    assert set(litellm["networks"]) == {"application", "data"}
    assert rendered["networks"]["application"]["internal"] is True
    assert rendered["networks"]["data"]["internal"] is True
    assert litellm.get("ports", []) == []
    assert litellm["healthcheck"] == {
        "test": [
            "CMD",
            "python",
            "-c",
            "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4000/health/liveliness', timeout=3)",
        ],
        "interval": "15s",
        "timeout": "5s",
        "retries": 10,
    }
    assert litellm["depends_on"] == {
        "dev-init": {
            "condition": "service_completed_successfully",
            "required": True,
        },
        "dev-supervisor-init": {
            "condition": "service_completed_successfully",
            "required": True,
        },
        "postgres": {"condition": "service_healthy", "required": True},
    }

    volumes = _volumes_by_target(litellm)
    assert set(volumes) == {
        "/app/bootstrap-config.json",
        "/app/config-supervisor.py",
        "/app/vonk-entrypoint",
        "/routes",
        "/run/secrets",
        "/supervisor",
    }
    for target, subpath in {
        "/app/bootstrap-config.json": "litellm-bootstrap.json",
        "/app/config-supervisor.py": "litellm-supervisor.py",
        "/app/vonk-entrypoint": "litellm-entrypoint.sh",
    }.items():
        assert volumes[target] == {
            "type": "volume",
            "source": "dev-runtime-config",
            "target": target,
            "read_only": True,
            "volume": {"subpath": subpath},
        }
    assert volumes["/run/secrets"] == {
        "type": "volume",
        "source": "dev-litellm-secrets",
        "target": "/run/secrets",
        "read_only": True,
        "volume": {},
    }
    assert volumes["/routes"]["source"].endswith("dev-route-publications")
    assert volumes["/routes"]["read_only"] is True
    assert volumes["/supervisor"]["source"].endswith("dev-supervisor-state")
    assert volumes["/supervisor"].get("read_only", False) is False


def test_image_template_enables_only_the_explicit_builtin_agent_authority() -> None:
    services = _rendered_image_template()["services"]
    api = services["control-api"]
    worker = services["control-worker"]

    expected_environment = {
        "VONK_AGENT_RUNTIME": "enabled",
        "VONK_AGENT_CA_PROVIDER": "builtin",
        "VONK_AGENT_BUILTIN_CA_BOOTSTRAP": "1",
        "VONK_MANAGEMENT_CIDRS_FILE": "/run/secrets/management-cidrs",
        "VONK_DEPLOYMENT_BRANCH": "deploy",
        "VONK_AGENT_CLIENT_CA_FILE": "/run/secrets/agent-ca-certificate",
        "VONK_AGENT_INTERMEDIATE_CERTIFICATE_FILE": "/run/secrets/agent-ca-certificate",
        "VONK_AGENT_INTERMEDIATE_KEY_FILE": "/run/secrets/agent-ca-key",
        "VONK_AGENT_PROXY_AUTH_FILE": "/run/secrets/agent-proxy-auth",
        "VONK_WORKER_API_TOKEN_FILE": "/run/secrets/worker-api-token",
    }
    assert expected_environment.items() <= api["environment"].items()
    assert worker["environment"]["VONK_DEPLOYMENT_BRANCH"] == "deploy"
    assert "VONK_MANAGEMENT_CIDRS_FILE" not in worker["environment"]

    api_secrets = {
        (secret["source"], secret["target"])
        for secret in api.get("secrets", [])
    }
    assert api_secrets == {
        ("management-cidrs", "/run/secrets/management-cidrs")
    }

    api_volumes = _volumes_by_target(api)
    worker_volumes = _volumes_by_target(worker)
    assert api_volumes["/routes"]["read_only"] is True
    assert api_volumes["/supervisor"]["read_only"] is True
    assert worker_volumes["/routes"].get("read_only", False) is False
    assert worker_volumes["/supervisor"]["read_only"] is True
    assert worker["depends_on"]["litellm"] == {
        "condition": "service_healthy",
        "required": True,
    }


def test_dev_compose_runs_packaged_initializer_with_disjoint_runtime_authority(
    tmp_path: Path,
) -> None:
    rendered = _rendered(tmp_path)
    services = rendered["services"]
    initializer = services["dev-init"]

    assert initializer["build"]["target"] == "api"
    assert initializer["command"] == ["python", "-m", "vonk_control.dev_init"]
    assert initializer["environment"] == {
        "VONK_CONTROL_IDENTITY_ROOT": "/control-identity",
        "VONK_DEV_API_SECRET_ROOT": "/api-secrets",
        "VONK_DEV_API_IMAGE": DEV_API_IMAGE,
        "VONK_DEV_EXPECTED_COMMIT": EXPECTED_COMMIT,
        "VONK_DEV_LOCAL_ACCEPTANCE": "1",
        "VONK_DEV_MIGRATE_SECRET_ROOT": "/migrate-secrets",
        "VONK_DEV_REPOSITORY_URL": "file:///source-origin",
        "VONK_DEV_SECRET_SOURCE_ROOT": "/host-secrets",
        "VONK_DEV_WORKER_SECRET_ROOT": "/worker-secrets",
        "VONK_DEV_WORKER_IMAGE": DEV_WORKER_IMAGE,
        "VONK_REPOSITORY_PATH": "/repository",
        "VONK_ROUTE_ROOT": "/routes",
        "VONK_STATE_PATH": "/state",
        "VONK_SUPERVISOR_ROOT": "/supervisor",
    }
    init_volumes = _volumes_by_target(initializer)
    assert init_volumes["/source-origin"]["type"] == "bind"
    assert init_volumes["/source-origin"]["read_only"] is True
    assert init_volumes["/source-origin"]["source"] == str(
        tmp_path / "one-use-origin.git"
    )
    assert "/host-secrets" not in init_volumes
    assert init_volumes["/repository"]["type"] == "volume"
    assert init_volumes["/api-secrets"]["type"] == "volume"
    assert init_volumes["/migrate-secrets"]["source"].endswith(
        "dev-migrate-secrets"
    )
    assert init_volumes["/worker-secrets"]["type"] == "volume"
    assert {
        (secret["source"], secret["target"])
        for secret in initializer["secrets"]
    } == {
        ("database-url", "/host-secrets/database-url"),
        ("git-signing-key", "/host-secrets/git-signing-key"),
    }
    assert "postgres-password" not in {
        secret["source"] for secret in initializer["secrets"]
    }

    api_volumes = _volumes_by_target(services["control-api"])
    migrate_volumes = _volumes_by_target(services["migrate"])
    worker_volumes = _volumes_by_target(services["control-worker"])
    assert api_volumes["/repository"]["type"] == "volume"
    assert api_volumes["/repository"].get("read_only", False) is False
    assert worker_volumes["/repository"]["type"] == "volume"
    assert worker_volumes["/repository"]["read_only"] is True
    assert api_volumes["/run/secrets"]["source"].endswith("dev-api-secrets")
    assert worker_volumes["/run/secrets"]["source"].endswith(
        "dev-worker-secrets"
    )
    assert migrate_volumes["/run/secrets"]["source"].endswith(
        "dev-migrate-secrets"
    )
    assert migrate_volumes["/run/secrets"]["read_only"] is True
    assert api_volumes["/run/secrets"]["source"] != worker_volumes["/run/secrets"][
        "source"
    ]
    assert migrate_volumes["/run/secrets"]["source"] != api_volumes["/run/secrets"][
        "source"
    ]
    assert migrate_volumes["/run/secrets"]["source"] != worker_volumes[
        "/run/secrets"
    ]["source"]
    assert services["control-api"]["environment"]["VONK_DEPLOYMENT_BRANCH"] == "deploy"
    assert services["control-worker"]["environment"]["VONK_DEPLOYMENT_BRANCH"] == (
        "deploy"
    )
    assert services["migrate"].get("secrets", []) == []


def test_image_template_uses_the_database_only_migration_projection() -> None:
    services = _rendered_image_template()["services"]
    initializer = services["dev-init"]
    init_volumes = _volumes_by_target(initializer)
    migrate_volumes = _volumes_by_target(services["migrate"])
    api_volumes = _volumes_by_target(services["control-api"])
    worker_volumes = _volumes_by_target(services["control-worker"])

    assert initializer["environment"]["VONK_DEV_MIGRATE_SECRET_ROOT"] == "/migrate-secrets"
    assert init_volumes["/migrate-secrets"]["source"].endswith(
        "dev-migrate-secrets"
    )
    assert migrate_volumes["/run/secrets"]["source"].endswith(
        "dev-migrate-secrets"
    )
    assert migrate_volumes["/run/secrets"]["read_only"] is True
    assert services["migrate"].get("secrets", []) == []
    assert migrate_volumes["/run/secrets"]["source"] != api_volumes["/run/secrets"][
        "source"
    ]
    assert migrate_volumes["/run/secrets"]["source"] != worker_volumes[
        "/run/secrets"
    ]["source"]
    assert services["control-api"]["environment"]["VONK_DEPLOYMENT_BRANCH"] == "deploy"
    assert services["control-worker"]["environment"]["VONK_DEPLOYMENT_BRANCH"] == (
        "deploy"
    )


def test_image_template_gates_mutation_on_one_ordered_fail_closed_cohort() -> None:
    rendered = _rendered_image_template()
    services = rendered["services"]
    gate_names = (
        "dev-cohort-reset",
        "dev-api-cohort",
        "dev-worker-cohort",
        "dev-cohort-verify",
    )

    assert services["dev-cohort-reset"]["user"] == "0:0"
    assert services["dev-api-cohort"]["user"] == "10001:10001"
    assert services["dev-worker-cohort"]["user"] == "10001:10001"
    assert services["dev-cohort-verify"]["user"] == "10001:10001"
    assert services["dev-api-cohort"]["depends_on"] == {
        "dev-cohort-reset": {
            "condition": "service_completed_successfully",
            "required": True,
        }
    }
    assert services["dev-worker-cohort"]["depends_on"] == {
        "dev-api-cohort": {
            "condition": "service_completed_successfully",
            "required": True,
        }
    }
    assert services["dev-cohort-verify"]["depends_on"] == {
        "dev-worker-cohort": {
            "condition": "service_completed_successfully",
            "required": True,
        }
    }
    for service_name in ("dev-init", "migrate"):
        assert services[service_name]["depends_on"]["dev-cohort-verify"][
            "condition"
        ] == "service_completed_successfully"

    for service_name in gate_names:
        service = services[service_name]
        assert service["network_mode"] == "none"
        assert service.get("secrets", []) == []
        assert service.get("environment", {}) == {}
        assert service.get("restart") == "no"


def test_image_template_limits_cohort_volume_and_mutable_image_authority() -> None:
    rendered = _rendered_image_template()
    services = rendered["services"]
    gate_names = {
        "dev-cohort-reset",
        "dev-api-cohort",
        "dev-worker-cohort",
        "dev-cohort-verify",
    }
    consumers = {"dev-init", "migrate", "control-api", "control-worker"}
    api_services = {
        "dev-cohort-reset",
        "dev-api-cohort",
        "dev-cohort-verify",
        "dev-init",
        "migrate",
        "control-api",
    }
    worker_services = {"dev-worker-cohort", "control-worker"}

    assert set(rendered["volumes"]) >= {"dev-image-cohort"}
    cohort_sources = set()
    forbidden_targets = {
        "/host-secrets",
        "/run/secrets",
        "/repository",
        "/state",
        "/control-identity",
        "/var/run/docker.sock",
        "/run/docker.sock",
    }
    for service_name in gate_names | consumers:
        cohort = _volumes_by_target(services[service_name])["/cohort"]
        cohort_sources.add(cohort["source"])
        assert cohort.get("read_only", False) is (service_name in consumers)
    assert len(cohort_sources) == 1
    assert next(iter(cohort_sources)).endswith("dev-image-cohort")

    for service_name in gate_names:
        volumes = _volumes_by_target(services[service_name])
        assert set(volumes) == {"/cohort"}
        assert forbidden_targets.isdisjoint(volumes)
        assert all("docker.sock" not in volume["source"] for volume in volumes.values())

    for service_name in api_services | worker_services:
        assert services[service_name]["pull_policy"] == "always"
    api_image = services["control-api"]["image"]
    worker_image = services["control-worker"]["image"]
    assert all(services[name]["image"] == api_image for name in api_services)
    assert all(services[name]["image"] == worker_image for name in worker_services)


def test_image_template_uses_selected_cohort_without_a_mutable_commit_literal() -> None:
    services = _rendered_image_template()["services"]
    selected_path = "/cohort/selected.json"

    for service_name in ("dev-init", "migrate", "control-api", "control-worker"):
        environment = services[service_name]["environment"]
        assert environment["VONK_DEV_SELECTED_COHORT_FILE"] == selected_path
        assert "VONK_DEV_EXPECTED_COMMIT" not in environment

    assert services["migrate"]["command"][:6] == [
        "python",
        "-m",
        "vonk_control.dev_cohort",
        "run-selected",
        "--role",
        "api",
    ]
    assert services["migrate"]["command"][6] == "--"
    assert services["postgres"]["image"].startswith("postgres:18.3@sha256:")
    assert services["postgres"]["secrets"] == [
        {
            "source": "postgres-password",
            "target": "/run/secrets/postgres-password",
        }
    ]


def _script_repository(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    local_script = repository / "scripts/dev-compose"
    local_script.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, local_script)
    if SECRETS_HELPER.exists():
        shutil.copy2(SECRETS_HELPER, local_script.parent / SECRETS_HELPER.name)
    local_compose = repository / "deploy/compose/compose.dev.images.yaml"
    local_compose.parent.mkdir(parents=True)
    shutil.copy2(IMAGE_TEMPLATE, local_compose)
    subprocess.run(("git", "init", "-q", "-b", "main", str(repository)), check=True)
    subprocess.run(
        ("git", "-C", str(repository), "config", "user.email", "test@example.invalid"),
        check=True,
    )
    subprocess.run(
        ("git", "-C", str(repository), "config", "user.name", "Test"),
        check=True,
    )
    subprocess.run(("git", "-C", str(repository), "add", "."), check=True)
    subprocess.run(
        ("git", "-C", str(repository), "commit", "-qm", "fixture"), check=True
    )
    return repository, local_script


def _fake_docker(tmp_path: Path) -> Path:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        "touch \"$VONK_TEST_CAPTURE_DIR/environment\"\n"
        "printf '%s\\n' \"$@\" > \"$VONK_TEST_CAPTURE_DIR/arguments\"\n"
        "if [[ \"${1:-}\" == image ]]; then\n"
        "  if [[ \"${VONK_TEST_IMAGE_INSPECT_EXIT:-0}\" -ne 0 ]]; then\n"
        "    exit \"$VONK_TEST_IMAGE_INSPECT_EXIT\"\n"
        "  fi\n"
        "  case \"${*: -1}\" in\n"
        "    *api*) printf 'sha256:%064d\\n' 0 ;;\n"
        "    *worker*) printf 'sha256:%064d\\n' 1 ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        "file_number=0\n"
        "while [[ \"$#\" -gt 0 ]]; do\n"
        "  if [[ \"$1\" == --file ]]; then\n"
        "    file_number=$((file_number + 1))\n"
        "    if [[ \"$file_number\" -eq 1 ]]; then\n"
        "      cp \"$2\" \"$VONK_TEST_CAPTURE_DIR/rendered-compose.yaml\"\n"
        "    else\n"
        "      cp \"$2\" \"$VONK_TEST_CAPTURE_DIR/rendered-overlay.yaml\"\n"
        "    fi\n"
        "    shift 2\n"
        "    continue\n"
        "  fi\n"
        "  shift\n"
        "done\n"
        "if [[ \" $* \" == *\" up \"* && \" $* \" != *\" --wait \"* ]]; then\n"
        "  exit 89\n"
        "fi\n"
        "exit \"${VONK_TEST_DOCKER_EXIT:-0}\"\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    return fake_bin


def _existing_secrets(tmp_path: Path) -> Path:
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    for name in (
        "postgres-password",
        "database-url",
        "git-signing-key",
        "git-signing-key.pub",
    ):
        (secrets / name).write_text("already-created\n", encoding="utf-8")
        (secrets / name).chmod(0o600)
    return secrets


def _run_dev_compose(
    repository: Path,
    local_script: Path,
    fake_bin: Path,
    secrets: Path,
    capture: Path,
    *arguments: str,
    docker_exit: int = 0,
    image_inspect_exit: int = 0,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "VONK_DEV_SECRETS_DIR": str(secrets),
        "VONK_DEV_ORIGIN_DIR": str(capture / "ignored-origin.git"),
        "VONK_TEST_CAPTURE_DIR": str(capture),
        "VONK_TEST_DOCKER_EXIT": str(docker_exit),
        "VONK_TEST_IMAGE_INSPECT_EXIT": str(image_inspect_exit),
    }
    return subprocess.run(
        (str(local_script), *arguments),
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def _captured_compose_arguments(capture: Path) -> list[str]:
    return (capture / "arguments").read_text(encoding="utf-8").splitlines()


def test_dev_compose_script_renders_the_image_only_graph(tmp_path: Path) -> None:
    repository, local_script = _script_repository(tmp_path)
    secrets = _existing_secrets(tmp_path)
    fake_bin = _fake_docker(tmp_path)
    capture = tmp_path / "capture"
    capture.mkdir()
    expected_commit = subprocess.run(
        ("git", "-C", str(repository), "rev-parse", "main"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    result = _run_dev_compose(
        repository, local_script, fake_bin, secrets, capture, "config"
    )

    assert result.returncode == 0
    rendered = (capture / "rendered-compose.yaml").read_text(encoding="utf-8")
    assert "vonk-forge-api:dev-local" in rendered
    assert "vonk-forge-worker:dev-local" in rendered
    assert expected_commit in rendered
    assert "__VONK_" not in rendered
    assert "build:" not in rendered
    assert "VONK_DEV_LOCAL_ACCEPTANCE" not in rendered
    assert "file:///source-origin" not in rendered
    assert str(repository) not in rendered
    overlay = (capture / "rendered-overlay.yaml").read_text(encoding="utf-8")
    assert "VONK_DEV_API_IMAGE: vonk-forge-api@sha256:" in overlay
    assert "VONK_DEV_WORKER_IMAGE: vonk-forge-worker@sha256:" in overlay
    assert "VONK_CONTROL_PROCESS_IMAGE: vonk-forge-api@sha256:" in overlay
    assert "VONK_CONTROL_PROCESS_IMAGE: vonk-forge-worker@sha256:" in overlay


@pytest.mark.parametrize(
    ("arguments", "expected_tail"),
    (
        ((), ("up", "-d", "--wait", "--pull", "never")),
        (("up", "-d", "control-api"), ("up", "-d", "control-api", "--wait", "--pull", "never")),
        (("config",), ("config",)),
    ),
)
def test_dev_compose_script_uses_safe_image_only_defaults(
    tmp_path: Path, arguments: tuple[str, ...], expected_tail: tuple[str, ...]
) -> None:
    repository, local_script = _script_repository(tmp_path)
    secrets = _existing_secrets(tmp_path)
    fake_bin = _fake_docker(tmp_path)
    capture = tmp_path / "capture"
    capture.mkdir()

    result = _run_dev_compose(
        repository, local_script, fake_bin, secrets, capture, *arguments
    )

    assert result.returncode == 0
    compose_arguments = _captured_compose_arguments(capture)
    assert tuple(compose_arguments[-len(expected_tail) :]) == expected_tail
    assert "--build" not in compose_arguments


def test_dev_compose_script_removes_its_render_workspace_after_success(
    tmp_path: Path,
) -> None:
    repository, local_script = _script_repository(tmp_path)
    secrets = _existing_secrets(tmp_path)
    fake_bin = _fake_docker(tmp_path)
    capture = tmp_path / "capture"
    capture.mkdir()

    result = _run_dev_compose(
        repository, local_script, fake_bin, secrets, capture, "config"
    )

    compose_arguments = _captured_compose_arguments(capture)
    project_directory = Path(
        compose_arguments[compose_arguments.index("--project-directory") + 1]
    )
    assert result.returncode == 0
    assert project_directory.parent == Path("/tmp")
    assert project_directory.name.startswith("vonk-forge-dev-compose.")
    assert not project_directory.exists()


def test_dev_compose_script_removes_its_render_workspace_after_docker_failure(
    tmp_path: Path,
) -> None:
    repository, local_script = _script_repository(tmp_path)
    secrets = _existing_secrets(tmp_path)
    fake_bin = _fake_docker(tmp_path)
    capture = tmp_path / "capture"
    capture.mkdir()

    result = _run_dev_compose(
        repository,
        local_script,
        fake_bin,
        secrets,
        capture,
        "config",
        docker_exit=37,
    )

    compose_arguments = _captured_compose_arguments(capture)
    project_directory = Path(
        compose_arguments[compose_arguments.index("--project-directory") + 1]
    )
    assert result.returncode == 37
    assert not project_directory.exists()


def test_dev_compose_down_does_not_depend_on_local_image_tags(tmp_path: Path) -> None:
    repository, local_script = _script_repository(tmp_path)
    secrets = _existing_secrets(tmp_path)
    fake_bin = _fake_docker(tmp_path)
    capture = tmp_path / "capture"
    capture.mkdir()

    result = _run_dev_compose(
        repository,
        local_script,
        fake_bin,
        secrets,
        capture,
        "down",
        "--volumes",
        image_inspect_exit=91,
    )

    assert result.returncode == 0
    compose_arguments = _captured_compose_arguments(capture)
    assert compose_arguments[-2:] == ["down", "--volumes"]


def test_dev_compose_script_rejects_a_symlinked_development_directory(
    tmp_path: Path,
) -> None:
    repository, local_script = _script_repository(tmp_path)
    secrets = _existing_secrets(tmp_path)
    fake_bin = _fake_docker(tmp_path)
    capture = tmp_path / "capture"
    capture.mkdir()
    development_root = repository / ".dev"
    external = tmp_path / "external-development"
    external.mkdir(mode=0o700)
    sentinel = external / "operator-owned"
    sentinel.write_text("preserve\n", encoding="utf-8")
    development_root.symlink_to(external, target_is_directory=True)
    environment = os.environ | {
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "VONK_DEV_SECRETS_DIR": str(secrets),
        "VONK_TEST_CAPTURE_DIR": str(capture),
    }

    result = subprocess.run(
        (str(local_script), "config"),
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert not (capture / "environment").exists()
    assert sentinel.read_text(encoding="utf-8") == "preserve\n"
    assert {path.name for path in external.iterdir()} == {"operator-owned"}


def test_dev_compose_rejects_development_directory_symlink_swapped_in_before_safe_open(
    tmp_path: Path,
) -> None:
    repository, local_script = _script_repository(tmp_path)
    development_root = repository / ".dev"
    development_root.mkdir(mode=0o700)
    pinned_root = repository / ".dev.pinned"
    fake_bin = _fake_docker(tmp_path)
    real_python = shutil.which("python3")
    real_stat = shutil.which("stat")
    assert real_python is not None
    assert real_stat is not None
    capture = tmp_path / "capture"
    capture.mkdir()
    swap_dev = capture / "swap-dev"
    swap_dev.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        "if [[ ! -e \"$VONK_TEST_CAPTURE_DIR/dev-swapped\" ]]; then\n"
        "  touch \"$VONK_TEST_CAPTURE_DIR/dev-swapped\"\n"
        "  mv -- \"$VONK_TEST_REPOSITORY_ROOT/.dev\" "
        "\"$VONK_TEST_REPOSITORY_ROOT/.dev.pinned\"\n"
        "  ln -s -- \"$VONK_TEST_REPOSITORY_ROOT/.dev.pinned\" "
        "\"$VONK_TEST_REPOSITORY_ROOT/.dev\"\n"
        "fi\n",
        encoding="utf-8",
    )
    swap_dev.chmod(0o755)
    fake_stat = fake_bin / "stat"
    fake_stat.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        "output=$(" + real_stat + " \"$@\")\n"
        "if [[ \" $* \" == *\" $VONK_TEST_REPOSITORY_ROOT/.dev \"* ]] "
        "&& [[ \" $* \" == *\" %d:%i \"* ]]; then\n"
        "  \"$VONK_TEST_CAPTURE_DIR/swap-dev\"\n"
        "fi\n"
        "printf '%s\\n' \"$output\"\n",
        encoding="utf-8",
    )
    fake_stat.chmod(0o755)
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        "\"$VONK_TEST_CAPTURE_DIR/swap-dev\"\n"
        f"exec {real_python} \"$@\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    environment = os.environ | {
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "VONK_TEST_CAPTURE_DIR": str(capture),
        "VONK_TEST_REPOSITORY_ROOT": str(repository),
    }

    result = subprocess.run(
        (str(local_script), "config"),
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert (capture / "dev-swapped").is_file()
    assert development_root.is_symlink()
    assert pinned_root.is_dir()
    assert not (pinned_root / "vonk-forge-secrets").exists()
    assert not (capture / "environment").exists()


@pytest.mark.parametrize(
    ("name", "dangling"),
    (
        ("postgres-password", True),
        ("database-url", False),
        ("git-signing-key", False),
        ("git-signing-key.pub", True),
    ),
)
def test_dev_compose_rejects_managed_secret_file_symlinks(
    tmp_path: Path,
    name: str,
    dangling: bool,
) -> None:
    repository, local_script = _script_repository(tmp_path)
    secrets = repository / ".dev/vonk-forge-secrets"
    secrets.mkdir(parents=True, mode=0o700)
    secrets.parent.chmod(0o700)
    secrets.chmod(0o700)
    outside = tmp_path / f"outside-{name}"
    if not dangling:
        outside.write_text("preserve\n", encoding="utf-8")
    (secrets / name).symlink_to(outside)
    fake_bin = _fake_docker(tmp_path)
    capture = tmp_path / "capture"
    capture.mkdir()
    environment = os.environ | {
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "VONK_TEST_CAPTURE_DIR": str(capture),
    }

    result = subprocess.run(
        (str(local_script), "config"),
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert not (capture / "environment").exists()
    if dangling:
        assert not outside.exists()
    else:
        assert outside.read_text(encoding="utf-8") == "preserve\n"


def test_dev_compose_rolls_back_private_key_if_public_key_is_swapped_during_install(
    tmp_path: Path,
) -> None:
    repository, local_script = _script_repository(tmp_path)
    secrets = repository / ".dev/vonk-forge-secrets"
    secrets.mkdir(parents=True, mode=0o700)
    secrets.parent.chmod(0o700)
    secrets.chmod(0o700)
    outside = tmp_path / "outside-public-key"
    outside.write_text("preserve\n", encoding="utf-8")
    fake_bin = _fake_docker(tmp_path)
    real_ssh_keygen = shutil.which("ssh-keygen")
    assert real_ssh_keygen is not None
    fake_ssh_keygen = fake_bin / "ssh-keygen"
    fake_ssh_keygen.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        f"{real_ssh_keygen} \"$@\"\n"
        "ln -s -- \"$VONK_TEST_OUTSIDE_PUBLIC_KEY\" "
        "\"$VONK_TEST_SECRETS_DIR/git-signing-key.pub\"\n",
        encoding="utf-8",
    )
    fake_ssh_keygen.chmod(0o755)
    capture = tmp_path / "capture"
    capture.mkdir()
    environment = os.environ | {
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "VONK_TEST_CAPTURE_DIR": str(capture),
        "VONK_TEST_OUTSIDE_PUBLIC_KEY": str(outside),
        "VONK_TEST_SECRETS_DIR": str(secrets),
    }

    result = subprocess.run(
        (str(local_script), "config"),
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert not (capture / "environment").exists()
    assert not (secrets / "git-signing-key").exists()
    assert (secrets / "git-signing-key.pub").is_symlink()
    assert outside.read_text(encoding="utf-8") == "preserve\n"


def test_dev_compose_rejects_an_existing_private_key_without_its_public_mate(
    tmp_path: Path,
) -> None:
    repository, local_script = _script_repository(tmp_path)
    secrets = repository / ".dev/vonk-forge-secrets"
    secrets.mkdir(parents=True, mode=0o700)
    secrets.parent.chmod(0o700)
    secrets.chmod(0o700)
    private_key = secrets / "git-signing-key"
    private_key.write_text("partial-private-key\n", encoding="utf-8")
    private_key.chmod(0o600)
    fake_bin = _fake_docker(tmp_path)
    capture = tmp_path / "capture"
    capture.mkdir()
    environment = os.environ | {
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "VONK_TEST_CAPTURE_DIR": str(capture),
    }

    result = subprocess.run(
        (str(local_script), "config"),
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert not (capture / "environment").exists()
    assert private_key.read_text(encoding="utf-8") == "partial-private-key\n"
    assert not (secrets / "git-signing-key.pub").exists()
