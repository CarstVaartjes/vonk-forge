from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "scripts/dev-compose-secrets.py"
IMAGE_TEMPLATE = ROOT / "deploy/compose/compose.dev.images.yaml"


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


def _volumes_by_target(service: dict[str, object]) -> dict[str, dict[str, object]]:
    return {volume["target"]: volume for volume in service.get("volumes", [])}


def test_image_template_stages_exact_disjoint_runtime_secret_volumes() -> None:
    rendered = _rendered_image_template()
    services = rendered["services"]
    initializer = services["dev-bootstrap"]
    repository_initializer = services["dev-repository-init"]

    assert initializer["network_mode"] == "none"
    assert initializer["environment"]["VONK_DEV_INIT_PHASE"] == "runtime"
    assert "/repository" not in _volumes_by_target(initializer)
    assert repository_initializer.get("secrets", []) == []
    assert set(repository_initializer["cap_add"]) == {
        "CHOWN",
        "DAC_OVERRIDE",
        "FOWNER",
        "SETGID",
        "SETUID",
    }
    assert repository_initializer["environment"]["VONK_DEV_INIT_PHASE"] == (
        "repository"
    )
    assert set(_volumes_by_target(repository_initializer)) == {
        "/cohort",
        "/repository",
    }

    expected_environment = {
        "VONK_DEV_AUTH_SECRET_ROOT": "/auth-secrets",
        "VONK_DEV_API_SECRET_ROOT": "/api-secrets",
        "VONK_DEV_CADDY_SECRET_ROOT": "/caddy-secrets",
        "VONK_DEV_LITELLM_SECRET_ROOT": "/litellm-secrets",
        "VONK_DEV_LITELLM_DATABASE_SECRET_ROOT": "/litellm-database-secrets",
        "VONK_DEV_MIGRATE_SECRET_ROOT": "/migrate-secrets",
        "VONK_DEV_RUNTIME_CONFIG_ROOT": "/runtime-config",
        "VONK_DEV_TAILSCALE_SECRET_ROOT": "/tailscale-secrets",
        "VONK_DEV_WORKER_SECRET_ROOT": "/worker-secrets",
    }
    assert expected_environment.items() <= initializer["environment"].items()
    init_volumes = _volumes_by_target(initializer)
    expected_roots = {
        "/auth-secrets": "dev-auth-secrets",
        "/api-secrets": "dev-api-secrets",
        "/caddy-secrets": "dev-caddy-secrets",
        "/litellm-secrets": "dev-litellm-secrets",
        "/litellm-database-secrets": "dev-litellm-database-secrets",
        "/migrate-secrets": "dev-migrate-secrets",
        "/runtime-config": "dev-runtime-config",
        "/tailscale-secrets": "dev-tailscale-secrets",
        "/worker-secrets": "dev-worker-secrets",
    }
    assert {
        target: volume["source"].removeprefix("vonk-forge-dev_")
        for target, volume in init_volumes.items()
        if target in expected_roots
    } == expected_roots
    assert all(
        not init_volumes[target].get("read_only", False)
        for target in expected_roots
    )
    assert set(rendered["volumes"]) >= set(expected_roots.values())

    source_secrets = {
        (secret["source"], secret["target"])
        for secret in initializer["secrets"]
    }
    assert source_secrets == {
        (name, f"/host-secrets/{name}")
        for name in (
            "admin-password-verifier",
            "agent-ca-certificate",
            "agent-ca-key",
            "agent-proxy-auth",
            "controller-ca",
            "controller-server-certificate",
            "controller-server-key",
            "database-url",
                "git-signing-key",
                "host-runtime-grant-private-key",
            "litellm-master-key",
            "litellm-database-password",
            "litellm-upstream-key",
            "management-cidrs",
            "tailscale-oauth-client-id",
            "tailscale-oauth-client-secret",
            "token-signing-key",
        )
    }


def test_image_template_keeps_private_authority_with_its_exact_service() -> None:
    services = _rendered_image_template()["services"]

    volume_consumers: dict[str, set[str]] = {}
    secret_consumers: dict[str, set[str]] = {}
    for service_name, service in services.items():
        for volume in service.get("volumes", []):
            if volume["type"] == "volume":
                source = volume["source"].removeprefix("vonk-forge-dev_")
                volume_consumers.setdefault(source, set()).add(service_name)
        for secret in service.get("secrets", []):
            secret_consumers.setdefault(secret["source"], set()).add(service_name)

    assert volume_consumers["dev-api-secrets"] == {"control-api", "dev-bootstrap"}
    assert volume_consumers["dev-auth-secrets"] == {"dev-auth-init", "dev-bootstrap"}
    assert volume_consumers["dev-worker-secrets"] == {"control-worker", "dev-bootstrap"}
    assert volume_consumers["dev-migrate-secrets"] == {"dev-bootstrap", "migrate"}
    assert volume_consumers["dev-caddy-secrets"] == {"caddy", "dev-bootstrap"}
    assert volume_consumers["dev-litellm-secrets"] == {"dev-bootstrap", "litellm"}
    assert volume_consumers["dev-litellm-database-secrets"] == {
        "dev-bootstrap",
        "dev-litellm-database-init",
    }
    assert volume_consumers["dev-tailscale-secrets"] == {
        "dev-bootstrap",
        "tailscale-gateway",
    }
    assert secret_consumers["agent-ca-key"] == {"dev-bootstrap"}
    assert secret_consumers["controller-server-key"] == {"dev-bootstrap"}
    assert secret_consumers["litellm-master-key"] == {"dev-bootstrap"}
    assert secret_consumers["litellm-database-password"] == {"dev-bootstrap"}
    assert secret_consumers["litellm-upstream-key"] == {"dev-bootstrap"}
    assert secret_consumers["management-cidrs"] == {"dev-bootstrap"}
    assert secret_consumers["token-signing-key"] == {"dev-bootstrap"}
    assert secret_consumers["admin-password-verifier"] == {"dev-bootstrap"}
    assert secret_consumers["tailscale-oauth-client-id"] == {"dev-bootstrap"}
    assert secret_consumers["tailscale-oauth-client-secret"] == {"dev-bootstrap"}
    assert services["control-api"]["environment"]["VONK_TOKEN_SIGNING_KEY_FILE"] == (
        "/run/secrets/token-signing-key"
    )

    for service_name, service in services.items():
        environment = service.get("environment", {})
        assert "LITELLM_MASTER_KEY" not in environment
        assert "LITELLM_UPSTREAM_KEY" not in environment
        assert "LITELLM_DATABASE_URL" not in environment
        assert "VONK_AGENT_CA_KEY" not in environment
        assert "VONK_CONTROLLER_SERVER_KEY" not in environment


def test_image_template_never_projects_plaintext_admin_password() -> None:
    rendered = _rendered_image_template()

    assert "admin-password" not in rendered.get("secrets", {})
    for service in rendered["services"].values():
        assert all(
            secret["source"] != "admin-password"
            for secret in service.get("secrets", [])
        )
        assert all(
            volume["source"] != "admin-password"
            for volume in service.get("volumes", [])
        )


def test_image_template_preserves_existing_state_and_identity_volumes() -> None:
    volumes = set(_rendered_image_template()["volumes"])

    assert volumes >= {
        "dev-api-secrets",
        "dev-caddy-secrets",
        "dev-control-identity",
        "dev-control-state",
        "dev-image-cohort",
        "dev-litellm-secrets",
        "dev-litellm-database-secrets",
        "dev-migrate-secrets",
        "dev-postgres-data",
        "dev-repository",
        "dev-route-publications",
        "dev-runtime-config",
        "dev-supervisor-state",
        "dev-tailscale-runtime",
        "dev-tailscale-socket",
        "dev-tailscale-state",
        "dev-worker-secrets",
    }


def test_image_template_hands_acknowledgement_volume_to_litellm_only() -> None:
    services = _rendered_image_template()["services"]
    bootstrap = services["dev-bootstrap"]

    assert bootstrap["user"] == "0:0"
    assert bootstrap["network_mode"] == "none"
    assert bootstrap["read_only"] is True
    assert bootstrap["cap_drop"] == ["ALL"]
    assert bootstrap["cap_add"] == ["CHOWN", "DAC_OVERRIDE", "FOWNER"]
    assert bootstrap["security_opt"] == ["no-new-privileges:true"]
    assert bootstrap["depends_on"] == {
        "dev-repository-init": {
            "condition": "service_completed_successfully",
            "required": True,
        }
    }
    assert bootstrap["command"] == ["python", "-m", "vonk_control.dev_bootstrap"]

    volumes = _volumes_by_target(bootstrap)
    assert volumes["/supervisor"]["source"].endswith("dev-supervisor-state")
    writers = set()
    readers = set()
    for service_name in ("control-api", "control-worker", "litellm"):
        supervisor = _volumes_by_target(services[service_name])["/supervisor"]
        (readers if supervisor.get("read_only", False) else writers).add(service_name)
    assert readers == {"control-api", "control-worker"}
    assert writers == {"litellm"}


@pytest.fixture
def secrets_helper() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "vonk_dev_compose_secrets_test",
        HELPER,
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _secret_directory(tmp_path: Path) -> tuple[Path, int]:
    directory = tmp_path / "secrets"
    directory.mkdir(mode=0o700)
    descriptor = os.open(
        directory,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    return directory, descriptor


def test_signing_key_staging_rejects_replacement_between_mkdir_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    secrets_helper: ModuleType,
) -> None:
    directory, descriptor = _secret_directory(tmp_path)
    temporary = ".git-signing-key." + "a" * 32
    renamed = directory / "renamed-original"
    replacement = directory / temporary
    sentinel = replacement / "attacker-owned"
    real_open = os.open
    keygen_called = False
    swapped = False

    def race_open(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == temporary and dir_fd == descriptor and not swapped:
            (directory / temporary).rename(renamed)
            replacement.mkdir(mode=0o700)
            sentinel.write_text("preserve\n", encoding="utf-8")
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def record_keygen(
        command: tuple[str, ...],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal keygen_called
        keygen_called = True
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(secrets_helper.secrets, "token_hex", lambda _size: "a" * 32)
    monkeypatch.setattr(secrets_helper.os, "open", race_open)
    monkeypatch.setattr(secrets_helper.subprocess, "run", record_keygen)
    try:
        with pytest.raises(secrets_helper.SecretPreparationError):
            secrets_helper._generate_signing_key(descriptor)
    finally:
        os.close(descriptor)

    assert swapped
    assert keygen_called is False
    assert renamed.is_dir()
    assert replacement.is_dir()
    assert sentinel.read_text(encoding="utf-8") == "preserve\n"


def test_signing_key_cleanup_preserves_replacement_of_validated_staging_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    secrets_helper: ModuleType,
) -> None:
    directory, descriptor = _secret_directory(tmp_path)
    temporary = ".git-signing-key." + "b" * 32
    staging = directory / temporary
    renamed = directory / "renamed-original"
    replacement_descriptor = -1
    replacement_identity: tuple[int, int] | None = None

    def generate_then_swap(
        command: tuple[str, ...],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal replacement_descriptor, replacement_identity
        key_path = Path(command[-1])
        key_path.write_bytes(b"private\n")
        key_path.chmod(0o600)
        public_path = Path(str(key_path) + ".pub")
        public_path.write_bytes(b"public\n")
        public_path.chmod(0o600)
        staging.rename(renamed)
        staging.mkdir(mode=0o700)
        replacement_descriptor = os.open(
            staging,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        metadata = os.fstat(replacement_descriptor)
        replacement_identity = (metadata.st_dev, metadata.st_ino)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(secrets_helper.secrets, "token_hex", lambda _size: "b" * 32)
    monkeypatch.setattr(secrets_helper.subprocess, "run", generate_then_swap)
    try:
        secrets_helper._generate_signing_key(descriptor)
    finally:
        os.close(descriptor)

    try:
        assert replacement_descriptor >= 0
        assert replacement_identity is not None
        current = staging.stat()
        assert (current.st_dev, current.st_ino) == replacement_identity
        assert list(staging.iterdir()) == []
        assert renamed.is_dir()
        assert list(renamed.iterdir()) == []
        assert (directory / "git-signing-key").is_file()
        assert (directory / "git-signing-key.pub").is_file()
    finally:
        if replacement_descriptor >= 0:
            os.close(replacement_descriptor)
