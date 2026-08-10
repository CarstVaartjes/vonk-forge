from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SECRETS_SCRIPT = ROOT / "scripts" / "dev-runtime-secrets.py"
PROJECT_SCRIPT = ROOT / "scripts" / "dev-runtime-project"
SOURCE_COMPOSE = ROOT / "deploy" / "compose" / "compose.dev.images.yaml"

ENROLL_HOSTNAME = "enroll.example.test"
AGENT_HOSTNAME = "agents.example.test"
REGISTRY_HOSTNAME = "registry.example.test"
MANAGEMENT_CIDRS = "192.0.2.0/24,2001:db8::/64"
NAS_ADDRESS = "192.0.2.10"

EXPECTED_PROJECT_FILES = {
    "docker-compose.yml",
    "secrets/agent-ca-certificate",
    "secrets/agent-ca-key",
    "secrets/agent-proxy-auth",
    "secrets/controller-ca",
    "secrets/controller-server-certificate",
    "secrets/controller-server-key",
    "secrets/database-url",
    "secrets/git-signing-key",
    "secrets/litellm-master-key",
    "secrets/litellm-upstream-key",
    "secrets/management-cidrs",
    "secrets/postgres-password",
}

REQUIRED_SECRET_NAMES = {
    path.removeprefix("secrets/")
    for path in EXPECTED_PROJECT_FILES
    if path.startswith("secrets/")
}


def _run_secret_generator(secrets_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (
            sys.executable,
            str(SECRETS_SCRIPT),
            "--secrets-dir",
            str(secrets_dir),
            "--management-cidrs",
            MANAGEMENT_CIDRS,
            "--enroll-hostname",
            ENROLL_HOSTNAME,
            "--agent-hostname",
            AGENT_HOSTNAME,
            "--registry-hostname",
            REGISTRY_HOSTNAME,
        ),
        check=False,
        capture_output=True,
        text=True,
    )


def _run_project(
    destination: Path,
    secrets_dir: Path,
    source_compose: Path = SOURCE_COMPOSE,
    *extra: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (
            sys.executable,
            str(PROJECT_SCRIPT),
            "--source-compose",
            str(source_compose),
            "--secrets-dir",
            str(secrets_dir),
            "--destination",
            str(destination),
            "--nas-address",
            NAS_ADDRESS,
            "--management-cidrs",
            MANAGEMENT_CIDRS,
            "--enroll-hostname",
            ENROLL_HOSTNAME,
            "--agent-hostname",
            AGENT_HOSTNAME,
            "--registry-hostname",
            REGISTRY_HOSTNAME,
            *extra,
        ),
        check=False,
        capture_output=True,
        text=True,
    )


def _load_project_module() -> ModuleType:
    loader = importlib.machinery.SourceFileLoader(
        "vonk_dev_runtime_project_test",
        str(PROJECT_SCRIPT),
    )
    specification = importlib.util.spec_from_loader(loader.name, loader)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _prepare_source_secrets(tmp_path: Path) -> Path:
    source = tmp_path / "source-secrets"
    source.parent.chmod(0o700)
    result = _run_secret_generator(source)
    assert result.returncode == 0, result.stderr
    return source


def _project_listing(destination: Path) -> set[str]:
    return {
        str(path.relative_to(destination))
        for path in destination.rglob("*")
        if path.is_file()
    }


def test_publishes_exact_two_item_project_without_secret_output(tmp_path: Path) -> None:
    secrets_dir = _prepare_source_secrets(tmp_path)
    destination = tmp_path / "nas-project"

    result = _run_project(destination, secrets_dir)

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{destination}\n"
    assert result.stderr == ""
    assert sorted(path.name for path in destination.iterdir()) == [
        "docker-compose.yml",
        "secrets",
    ]
    assert _project_listing(destination) == EXPECTED_PROJECT_FILES
    assert not (destination / "secrets" / "git-signing-key.pub").exists()
    for name in REQUIRED_SECRET_NAMES:
        source = secrets_dir / name
        copied = destination / "secrets" / name
        assert copied.read_bytes() == source.read_bytes()
        assert stat.S_IMODE(copied.stat().st_mode) == 0o600
        assert (
            hashlib.sha256(copied.read_bytes()).digest()
            == hashlib.sha256(source.read_bytes()).digest()
        )
    compose = (destination / "docker-compose.yml").read_text(encoding="utf-8")
    assert "VONK_AGENT_ENROLL_HOSTNAME: enroll.example.test" in compose
    assert "VONK_AGENT_HOSTNAME: agents.example.test" in compose
    for name in (
        "agent-proxy-auth",
        "controller-server-key",
        "database-url",
        "git-signing-key",
        "litellm-master-key",
        "postgres-password",
    ):
        marker = (
            (secrets_dir / name).read_text(encoding="ascii", errors="ignore").strip()
        )
        if marker and marker in (result.stdout + result.stderr):
            pytest.fail(f"secret value from {name} leaked to project command output")


def test_replaces_only_validated_project_children_and_rejects_extras(
    tmp_path: Path,
) -> None:
    secrets_dir = _prepare_source_secrets(tmp_path)
    destination = tmp_path / "nas-project"
    destination.mkdir()
    (destination / ".env").write_text("must-not-be-kept\n", encoding="utf-8")

    rejected = _run_project(destination, secrets_dir)

    assert rejected.returncode == 1
    assert (destination / ".env").read_text(encoding="utf-8") == "must-not-be-kept\n"
    assert not (destination / "docker-compose.yml").exists()

    (destination / ".env").unlink()
    accepted = _run_project(destination, secrets_dir)

    assert accepted.returncode == 0, accepted.stderr
    first_digest = hashlib.sha256(
        (destination / "docker-compose.yml").read_bytes()
    ).hexdigest()
    accepted_again = _run_project(destination, secrets_dir)
    second_digest = hashlib.sha256(
        (destination / "docker-compose.yml").read_bytes()
    ).hexdigest()
    assert accepted_again.returncode == 0, accepted_again.stderr
    assert second_digest == first_digest
    assert sorted(path.name for path in destination.iterdir()) == [
        "docker-compose.yml",
        "secrets",
    ]


def test_rejects_unsafe_source_secret_and_preserves_destination(
    tmp_path: Path,
) -> None:
    secrets_dir = _prepare_source_secrets(tmp_path)
    destination = tmp_path / "nas-project"
    existing = "previous compose\n"
    destination.mkdir()
    (destination / "docker-compose.yml").write_text(existing, encoding="utf-8")
    secret_destination = destination / "secrets"
    secret_destination.mkdir()
    (secret_destination / "postgres-password").write_text(
        "previous\n", encoding="utf-8"
    )
    (secrets_dir / "agent-proxy-auth").unlink()
    (secrets_dir / "agent-proxy-auth").symlink_to(tmp_path / "outside-secret")

    result = _run_project(destination, secrets_dir)

    assert result.returncode == 1
    assert result.stdout == ""
    assert (destination / "docker-compose.yml").read_text(encoding="utf-8") == existing
    assert (secret_destination / "postgres-password").read_text(
        encoding="utf-8"
    ) == "previous\n"


def test_rejects_symlink_in_source_secret_ancestry(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir(mode=0o700)
    secrets_dir = _prepare_source_secrets(real_parent)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    linked_secrets = linked_parent / secrets_dir.name
    destination = tmp_path / "nas-project"

    result = _run_project(destination, linked_secrets)

    assert result.returncode == 1
    assert result.stdout == ""
    assert not destination.exists()


def test_permits_copy_to_mounted_destination_but_not_generation_there(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets_dir = _prepare_source_secrets(tmp_path)
    project = _load_project_module()
    destination = tmp_path / "mounted-nas"
    inspected: list[Path] = []

    def filesystem_type(path: Path) -> str:
        inspected.append(path)
        return "cifs" if path == destination else "ext4"

    monkeypatch.setattr(project, "_filesystem_type", filesystem_type)

    project.publish_project(
        source_compose=SOURCE_COMPOSE,
        secrets_dir=secrets_dir,
        destination=destination,
        nas_address=NAS_ADDRESS,
        management_cidrs=MANAGEMENT_CIDRS,
        enroll_hostname=ENROLL_HOSTNAME,
        agent_hostname=AGENT_HOSTNAME,
        registry_hostname=REGISTRY_HOSTNAME,
    )

    assert _project_listing(destination) == EXPECTED_PROJECT_FILES
    assert destination in inspected


def test_rejects_nonlocal_project_staging_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets_dir = _prepare_source_secrets(tmp_path)
    project = _load_project_module()
    destination = tmp_path / "nas-project"
    remote_temporary = tmp_path / "remote-temporary"
    remote_temporary.mkdir(mode=0o700)
    monkeypatch.setattr(project.tempfile, "gettempdir", lambda: str(remote_temporary))
    monkeypatch.setattr(
        project,
        "_filesystem_type",
        lambda path: "cifs" if path == remote_temporary else "ext4",
    )

    with pytest.raises(project.ProjectPublicationError):
        project.publish_project(
            source_compose=SOURCE_COMPOSE,
            secrets_dir=secrets_dir,
            destination=destination,
            nas_address=NAS_ADDRESS,
            management_cidrs=MANAGEMENT_CIDRS,
            enroll_hostname=ENROLL_HOSTNAME,
            agent_hostname=AGENT_HOSTNAME,
            registry_hostname=REGISTRY_HOSTNAME,
        )

    assert not destination.exists()
    assert list(remote_temporary.iterdir()) == []


def test_rejects_invalid_project_inputs(tmp_path: Path) -> None:
    secrets_dir = _prepare_source_secrets(tmp_path)

    bad_address = _run_project(
        tmp_path / "bad-address",
        secrets_dir,
        SOURCE_COMPOSE,
        "--nas-address",
        "not-an-ip-address",
    )
    bad_hostname = _run_project(
        tmp_path / "bad-hostname",
        secrets_dir,
        SOURCE_COMPOSE,
        "--registry-hostname",
        "bad_host.example",
    )

    assert bad_address.returncode == 1
    assert bad_address.stdout == ""
    assert bad_hostname.returncode == 1
    assert bad_hostname.stdout == ""


def test_interrupted_publish_leaves_only_complete_children_and_no_temp_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets_dir = _prepare_source_secrets(tmp_path)
    project = _load_project_module()
    destination = tmp_path / "nas-project"
    destination.mkdir()
    (destination / "docker-compose.yml").write_text(
        "previous compose\n", encoding="utf-8"
    )
    secret_destination = destination / "secrets"
    secret_destination.mkdir()
    (secret_destination / "postgres-password").write_text(
        "previous\n", encoding="utf-8"
    )
    real_replace = os.replace
    replace_calls = 0

    def interrupt_on_second_replace(
        source: str | os.PathLike[str],
        target: str | os.PathLike[str],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("simulated interruption")
        real_replace(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(project.os, "replace", interrupt_on_second_replace)

    with pytest.raises(project.ProjectPublicationError):
        project.publish_project(
            source_compose=SOURCE_COMPOSE,
            secrets_dir=secrets_dir,
            destination=destination,
            nas_address=NAS_ADDRESS,
            management_cidrs=MANAGEMENT_CIDRS,
            enroll_hostname=ENROLL_HOSTNAME,
            agent_hostname=AGENT_HOSTNAME,
            registry_hostname=REGISTRY_HOSTNAME,
        )

    compose = (destination / "docker-compose.yml").read_text(encoding="utf-8")
    assert (
        compose == "previous compose\n"
        or "VONK_AGENT_ENROLL_HOSTNAME: enroll.example.test" in compose
    )
    password = (secret_destination / "postgres-password").read_text(encoding="utf-8")
    assert password in {
        "previous\n",
        (secrets_dir / "postgres-password").read_text(encoding="utf-8"),
    }
    assert not any(path.name.startswith(".") for path in destination.rglob("*"))
