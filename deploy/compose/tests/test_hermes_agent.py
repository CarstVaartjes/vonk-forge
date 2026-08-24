from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
COMPOSE = ROOT / "deploy/compose"
HERMES = COMPOSE / "hermes-agent"


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    for line in (COMPOSE / "tests/test.env").read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            environment[key] = value
    return environment


def _rendered() -> dict[str, object]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE / "compose.yaml"),
            "--profile",
            "hermes",
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=_environment(),
    )
    return json.loads(result.stdout)


def test_wrapper_image_is_digest_pinned_and_contains_no_ssh_stack() -> None:
    dockerfile = (HERMES / "Dockerfile").read_text()
    project_text = "\n".join(
        path.read_text(errors="replace") for path in HERMES.glob("*") if path.is_file()
    ).lower()

    assert (
        "nousresearch/hermes-agent:v2026.7.20@sha256:"
        "f7b35053268f532f98955195c909f15a230470fbcbdacaa9fdecb95707dad04a"
    ) in dockerfile
    assert 'ENTRYPOINT ["/usr/local/bin/vonk-hermes-entrypoint"]' in dockerfile
    assert 'CMD ["gateway", "run"]' in dockerfile
    for forbidden in ("openssh", "sshd", "authorized_keys", "port: 22", "target: 22"):
        assert forbidden not in project_text


def test_compose_hermes_is_unpublished_bounded_and_segmented() -> None:
    service = _rendered()["services"]["hermes-agent"]

    assert service["image"] == (
        "example/hermes:1@sha256:"
        "7777777777777777777777777777777777777777777777777777777777777777"
    )
    assert "build" not in service
    assert set(service["networks"]) == {
        "hermes-inference",
        "tailnet-hermes-edge",
    }
    assert service["read_only"] is True
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["cap_drop"] == ["ALL"]
    assert service["cap_add"] == [
        "CHOWN",
        "DAC_OVERRIDE",
        "FOWNER",
        "SETGID",
        "SETUID",
    ]
    assert not service.get("ports")
    assert not service.get("devices")
    assert not service.get("privileged")
    assert "docker.sock" not in json.dumps(service)
    assert service["cpus"] == 4.0
    assert int(service["mem_limit"]) == 8 * 1024**3
    assert int(service["mem_reservation"]) == 4 * 1024**3
    assert int(service["shm_size"]) == 2 * 1024**3
    assert service["tmpfs"] == [
        "/run:size=64m,mode=755,exec",
        "/tmp:size=2g,mode=1777",
        "/var/tmp:size=1g,mode=1777",
    ]
    assert service["logging"]["options"] == {"max-file": "5", "max-size": "10m"}

    volumes = {item["target"]: item for item in service["volumes"]}
    assert set(volumes) == {"/opt/data", "/workspace", "/opt/data/home/.cache"}
    assert volumes["/opt/data"]["source"] == "hermes-data"
    assert volumes["/workspace"]["source"] == "hermes-workspaces"
    assert volumes["/opt/data/home/.cache"]["source"] == "hermes-cache"


def test_hermes_uses_only_caddy_lease_edge_and_authenticated_gateway() -> None:
    service = _rendered()["services"]["hermes-agent"]
    environment = service["environment"]

    assert environment["OPENAI_BASE_URL"] == "http://caddy:8081/v1"
    assert environment["API_SERVER_MODEL_NAME"] == "hermes-agent"
    assert environment["API_SERVER_ENABLED"] == "true"
    assert environment["API_SERVER_HOST"] == "0.0.0.0"
    assert environment["HERMES_DASHBOARD"] == "1"
    assert environment["HERMES_DASHBOARD_BASIC_AUTH_USERNAME"] == "hermes"
    assert environment["MESSAGING_CWD"] == "/workspace"
    assert environment["API_SERVER_CORS_ORIGINS"] == "https://hermes.test.example"
    assert service["depends_on"] == {
        "hermes-litellm-key-provisioner": {
            "condition": "service_completed_successfully",
            "required": True,
        },
        "caddy": {
            "condition": "service_healthy",
            "required": True,
            "restart": True,
        },
        "litellm": {
            "condition": "service_healthy",
            "required": True,
            "restart": True,
        },
    }
    assert "OPENAI_API_KEY" not in environment
    assert "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD" not in environment
    assert "HERMES_DASHBOARD_BASIC_AUTH_SECRET" not in environment
    assert {secret["target"] for secret in service["secrets"]} == {
        "/run/secrets/hermes-api-key",
        "/run/secrets/hermes-litellm-key",
    }
    health = json.dumps(service["healthcheck"]["test"])
    assert "127.0.0.1:8642" in health
    assert "127.0.0.1:9119" in health


def _run_entrypoint(
    tmp_path: Path,
    payload: bytes | None,
    *,
    symlink: bool = False,
    litellm_payload: bytes | None = b"sk-" + b"b" * 64 + b"\n",
):
    root = tmp_path / "root"
    secret = root / "run/secrets/hermes-api-key"
    secret.parent.mkdir(parents=True)
    if symlink:
        secret.symlink_to(root / "missing")
    elif payload is not None:
        secret.write_bytes(payload)
    litellm_secret = root / "run/secrets/hermes-litellm-key"
    if litellm_payload is not None:
        litellm_secret.write_bytes(litellm_payload)
    return subprocess.run(
        ["sh", str(HERMES / "entrypoint.sh")],
        capture_output=True,
        check=False,
        text=True,
        env=os.environ | {
            "HERMES_ENTRYPOINT_TEST_ROOT": str(root),
            "HERMES_ENTRYPOINT_TEST_ONLY": "1",
        },
    )


@pytest.mark.parametrize(
    "payload",
    (None, b"", b"short", b"a" * 31, b"a" * 32 + b" b", b"a" * 32 + b"\nsecond\n", b"a" * 4097),
)
def test_entrypoint_rejects_unsafe_api_keys(tmp_path: Path, payload: bytes | None) -> None:
    result = _run_entrypoint(tmp_path, payload)
    assert result.returncode != 0
    assert b"a" * 31 not in result.stderr.encode()


def test_entrypoint_rejects_symlink_and_accepts_32_byte_key(tmp_path: Path) -> None:
    assert _run_entrypoint(tmp_path / "link", None, symlink=True).returncode != 0
    result = _run_entrypoint(tmp_path / "valid", b"a" * 32 + b"\n")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "payload",
    (None, b"", b"sk-short\n", b"not-sk-" + b"a" * 64 + b"\n", b"sk-" + b"a" * 64 + b" b\n"),
)
def test_entrypoint_rejects_invalid_litellm_client_keys(
    tmp_path: Path, payload: bytes | None
) -> None:
    result = _run_entrypoint(
        tmp_path, b"a" * 32 + b"\n", litellm_payload=payload
    )

    assert result.returncode != 0
    assert "LiteLLM client key file is invalid" in result.stderr


def test_profile_scoped_key_provisioner_has_only_key_management_authority() -> None:
    service = _rendered()["services"]["hermes-litellm-key-provisioner"]

    assert service["profiles"] == ["hermes"]
    assert service["restart"] == "no"
    assert service["read_only"] is True
    assert service["user"] == "0:0"
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert set(service["networks"]) == {"litellm-edge"}
    assert {secret["target"] for secret in service["secrets"]} == {
        "/run/secrets/hermes-litellm-key",
        "/run/secrets/litellm-master-key",
    }
    assert service["depends_on"] == {
        "litellm": {
            "condition": "service_healthy",
            "required": True,
            "restart": True,
        }
    }
    assert not service.get("ports")


def test_runtime_harness_covers_security_health_and_persistence() -> None:
    harness = (COMPOSE / "tests/hermes-agent-runtime.sh").read_text()
    for required in (
        "8642",
        "9119",
        "hermes-api-key",
        "--force-recreate",
        "ReadonlyRootfs",
        "CapAdd",
        "Devices",
        "docker.sock",
        "/workspace",
        "/opt/data",
        "hermes-inference",
    ):
        assert required in harness
