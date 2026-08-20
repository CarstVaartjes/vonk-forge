import json
import os
import subprocess
from pathlib import Path

from deploy.compose.tests.test_agent_ingress import _require_docker_runtime

ROOT = Path(__file__).resolve().parents[3]


def _rendered() -> dict:
    env = os.environ.copy()
    for line in (ROOT / "deploy/compose/tests/test.env").read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            env[key] = value
    result = subprocess.run(
        ["docker", "compose", "-f", str(ROOT / "deploy/compose/compose.yaml"), "config", "--format", "json"],
        check=True, capture_output=True, text=True, env=env,
    )
    return json.loads(result.stdout)


def test_distribution_is_pinned_private_persistent_and_credential_free() -> None:
    rendered = _rendered()
    registry = rendered["services"]["registry"]
    assert registry["image"] == "registry:3@sha256:" + "9" * 64
    assert not registry.get("ports")
    assert set(registry["networks"]) == {"registry-edge", "registry-publisher"}
    assert rendered["networks"]["registry-edge"]["internal"] is True
    assert any(volume["target"] == "/var/lib/registry" for volume in registry["volumes"])
    assert not any("PASSWORD" in key or "TOKEN" in key for key in registry.get("environment", {}))


def test_registry_config_disables_delete_and_agent_sni_is_read_only_mtls() -> None:
    config = (ROOT / "deploy/compose/registry/config.yml").read_text()
    assert "delete:\n    enabled: false" in config
    assert "debug:" not in config
    caddy = (ROOT / "deploy/compose/Caddyfile").read_text()
    assert "{$VONK_REGISTRY_HOSTNAME" in caddy
    assert "mode require_and_verify" in caddy
    assert "method GET HEAD" in caddy
    assert "path_regexp registry_digest" in caddy
    assert "[0-9a-f]{64}$" in caddy
    assert "(?:manifests|blobs)" in caddy
    assert "respond 405" in caddy


def test_registry_caddy_adapter_has_only_ping_and_digest_pull_proxies() -> None:
    _require_docker_runtime()
    environment = {
        "VONK_CONTROL_HOSTNAME": "control.test.example",
        "VONK_AGENT_ENROLL_HOSTNAME": "enroll.test.example",
        "VONK_AGENT_HOSTNAME": "agents.test.example",
        "VONK_REGISTRY_HOSTNAME": "registry.test.example",
        "VONK_AGENT_PROXY_AUTH": "test-proxy-secret",
    }
    command = ["docker", "run", "--rm", "-i"]
    for key, value in environment.items():
        command.extend(("-e", f"{key}={value}"))
    command.extend((
        "caddy:2.10.2@sha256:c3d7ee5d2b11f9dc54f947f68a734c84e9c9666c92c88a7f30b9cba5da182adb",
        "caddy", "adapt", "--config", "-", "--adapter", "caddyfile",
    ))
    result = subprocess.run(
        command, check=True, capture_output=True, text=True,
        input=(ROOT / "deploy/compose/Caddyfile").read_text(),
    )
    adapted = json.loads(result.stdout)
    backend = next(
        server
        for server in adapted["apps"]["http"]["servers"].values()
        if any(str(listener).endswith(":8443") for listener in server.get("listen", []))
    )
    registry_site = next(
        route for route in backend["routes"]
        if route.get("match") == [{"host": ["registry.test.example"]}]
    )
    encoded = json.dumps(registry_site, sort_keys=True)
    assert encoded.count("registry:5000") == 2
    assert "sha256:[0-9a-f]{64}$" in encoded
    for forbidden in ("tags/list", "_catalog", "referrers", "uploads"):
        assert forbidden not in encoded


def test_operator_publisher_validates_project_and_digest_before_docker(tmp_path: Path) -> None:
    script = ROOT / "deploy/compose/bin/publish-release"
    environment = os.environ | {
        "COMPOSE_PROJECT_NAME": "../unsafe",
        "ORAS_PUBLISHER_IMAGE": "oras:latest",
        "RELEASE_TAG": "release-1",
        "REGISTRY_REPOSITORY": "vonk/releases",
    }
    result = subprocess.run(
        [str(script), str(tmp_path)],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert result.returncode == 64
    assert "project name is invalid" in result.stderr.lower()

    release = tmp_path / "release"
    release.mkdir()
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    record = tmp_path / "docker-argv"
    docker = bin_directory / "docker"
    docker.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$DOCKER_ARGV_RECORD\"\n"
    )
    docker.chmod(0o700)
    valid_environment = os.environ | {
        "PATH": str(bin_directory) + ":" + os.environ["PATH"],
        "DOCKER_ARGV_RECORD": str(record),
        "COMPOSE_PROJECT_NAME": "site_a",
        "ORAS_PUBLISHER_IMAGE": "example/oras:1.3.3@sha256:" + "a" * 64,
        "RELEASE_TAG": "release-1",
        "REGISTRY_REPOSITORY": "site_a/node.releases",
    }
    valid = subprocess.run(
        [str(script), str(release)], capture_output=True, text=True,
        env=valid_environment,
        check=False,
    )
    assert valid.returncode == 0, valid.stderr
    argv = record.read_text().splitlines()
    assert argv == [
        "run", "--rm", "--network", "site_a_registry-publisher",
        "-v", f"{release}:/release:ro", "-w", "/release",
        "example/oras:1.3.3@sha256:" + "a" * 64,
        "push", "--plain-http", "registry:5000/site_a/node.releases:release-1", ".",
    ]

    for changed in (
        {"REGISTRY_REPOSITORY": "../releases"},
        {"RELEASE_TAG": "x" * 129},
        {"RELEASE_TAG": ".starts-with-dot"},
    ):
        rejected = subprocess.run(
            [str(script), str(release)], capture_output=True, text=True,
            env=valid_environment | changed,
            check=False,
        )
        assert rejected.returncode == 64
