from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import shlex
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
REMOTE_SCRIPT = ROOT / "scripts" / "dev-runtime-project-remote"
SECRETS_SCRIPT = ROOT / "scripts" / "dev-runtime-secrets.py"
SOURCE_COMPOSE = ROOT / "deploy" / "compose" / "compose.dev.images.yaml"

MANAGEMENT_CIDRS = "192.0.2.0/24"
DIRECT_FABRIC_CIDRS = "198.51.100.0/24,203.0.113.0/24"
NAS_ADDRESS = "192.0.2.10"
ENROLL_HOSTNAME = "enroll.example.test"
AGENT_HOSTNAME = "agents.example.test"
REGISTRY_HOSTNAME = "registry.example.test"


def _load_remote_module() -> ModuleType:
    loader = importlib.machinery.SourceFileLoader(
        "vonk_dev_runtime_project_remote_test", str(REMOTE_SCRIPT)
    )
    specification = importlib.util.spec_from_loader(loader.name, loader)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _accepted_compose(tmp_path: Path, *, image: str | None = None) -> Path:
    compose = tmp_path / "docker-compose.dev.yml"
    rendered = SOURCE_COMPOSE.read_text(encoding="utf-8")
    rendered = rendered.removeprefix("name: vonk-forge-dev\n\n")
    rendered = rendered.replace(
        "__VONK_API_IMAGE__",
        image or "ghcr.io/carstvaartjes/vonk-forge-api:dev",
    ).replace(
        "__VONK_WORKER_IMAGE__",
        "ghcr.io/carstvaartjes/vonk-forge-worker:dev",
    )
    rendered = rendered.replace(
        "# Compatibility input for the current pinned renderer. Task 5 must retain this\n"
        "# token only for pinned output and omit it entirely from mutable output.\n"
        'x-pinned-expected-commit: "__VONK_EXPECTED_COMMIT__"\n',
        "",
        1,
    )
    compose.write_text(rendered, encoding="utf-8")
    compose.chmod(0o600)
    return compose


def _source_secrets(tmp_path: Path) -> Path:
    oauth = tmp_path / "oauth"
    oauth.mkdir(mode=0o700)
    client_id = oauth / "client-id"
    client_secret = oauth / "client-secret"
    client_id.write_text("synthetic-client-id\n", encoding="ascii")
    client_secret.write_text("synthetic-client-secret\n", encoding="ascii")
    client_id.chmod(0o600)
    client_secret.chmod(0o600)
    secrets = tmp_path / "secrets"
    result = subprocess.run(
        (
            sys.executable,
            str(SECRETS_SCRIPT),
            "--secrets-dir",
            str(secrets),
            "--management-cidrs",
            MANAGEMENT_CIDRS,
            "--enroll-hostname",
            ENROLL_HOSTNAME,
            "--agent-hostname",
            AGENT_HOSTNAME,
            "--registry-hostname",
            REGISTRY_HOSTNAME,
            "--tailscale-oauth-client-id-file",
            str(client_id),
            "--tailscale-oauth-client-secret-file",
            str(client_secret),
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return secrets


def _identity(tmp_path: Path) -> Path:
    identity = tmp_path / "id_ed25519"
    identity.write_text("synthetic private key path input\n", encoding="ascii")
    identity.chmod(0o600)
    return identity


def _arguments(
    tmp_path: Path,
    *,
    source_compose: Path | None = None,
    secrets_dir: Path | None = None,
    identity_file: Path | None = None,
    remote_destination: str = "/srv/docker/vonk-forge",
    docker_mode: str = "sudo",
) -> dict[str, Any]:
    return {
        "source_compose": source_compose or _accepted_compose(tmp_path),
        "secrets_dir": secrets_dir or _source_secrets(tmp_path),
        "ssh_target": "nas-operator@example.test",
        "identity_file": identity_file or _identity(tmp_path),
        "remote_destination": remote_destination,
        "docker_mode": docker_mode,
        "nas_address": NAS_ADDRESS,
        "management_cidrs": MANAGEMENT_CIDRS,
        "direct_fabric_cidrs": DIRECT_FABRIC_CIDRS,
        "enroll_hostname": ENROLL_HOSTNAME,
        "agent_hostname": AGENT_HOSTNAME,
        "registry_hostname": REGISTRY_HOSTNAME,
    }


def test_streams_exact_private_generation_to_restricted_remote_publisher(
    tmp_path: Path,
) -> None:
    remote = _load_remote_module()
    arguments = _arguments(tmp_path)
    calls: list[tuple[list[str], bytes]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs["input"]))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=b"/publish/vonk-forge\n",
            stderr=b"",
        )

    destination = remote.publish_remote(**arguments, runner=run)

    assert destination == "nas-operator@example.test:/srv/docker/vonk-forge"
    assert len(calls) == 1
    command, archive = calls[0]
    assert command[0].endswith("/ssh") or command[0] == "ssh"
    assert command[1] == "-T"
    assert "BatchMode=yes" in command
    assert "ClearAllForwardings=yes" in command
    assert "StrictHostKeyChecking=yes" in command
    assert "IdentitiesOnly=yes" in command
    assert str(arguments["identity_file"]) in command
    assert "--" in command
    assert command[command.index("--") + 1] == "nas-operator@example.test"
    remote_command = command[-1]
    remote_script = shlex.split(remote_command)[-1]
    for required in (
        "mktemp -d /dev/shm/vonk-runtime-project.",
        "trap cleanup_remote EXIT",
        "trap 'exit 130' HUP INT TERM",
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "no-new-privileges",
        "--user",
        "--tmpfs",
        "PYTHONDONTWRITEBYTECODE=1",
        "sudo -n docker",
    ):
        assert required in remote_script

    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as packaged:
        members = {member.name: member for member in packaged.getmembers()}
        source_names = {path.name for path in arguments["secrets_dir"].iterdir()}
        assert set(members) == {
            "dev-runtime-project",
            "dev-runtime-secrets.py",
            "docker-compose.dev.yml",
            "secrets",
            *(f"secrets/{name}" for name in source_names),
        }
        assert stat.S_IMODE(members["secrets"].mode) == 0o700
        for name, member in members.items():
            if name != "secrets":
                assert stat.S_IMODE(member.mode) == 0o600
        for name in source_names:
            packaged_file = packaged.extractfile(members[f"secrets/{name}"])
            assert packaged_file is not None
            assert (
                packaged_file.read() == (arguments["secrets_dir"] / name).read_bytes()
            )

    command_bytes = "\0".join(command).encode()
    for name in (
        "admin-password",
        "agent-ca-key",
        "agent-proxy-auth",
        "controller-ca-key",
        "controller-server-key",
        "database-url",
        "git-signing-key",
        "host-runtime-grant-private-key",
        "litellm-database-password",
        "litellm-master-key",
        "litellm-upstream-key",
        "postgres-password",
        "tailscale-oauth-client-id",
        "tailscale-oauth-client-secret",
        "token-signing-key",
    ):
        assert (
            arguments["secrets_dir"] / name
        ).read_bytes().strip() not in command_bytes


def test_rejects_invalid_source_generation_before_opening_ssh(tmp_path: Path) -> None:
    remote = _load_remote_module()
    arguments = _arguments(tmp_path)
    (arguments["secrets_dir"] / "unexpected").write_text(
        "must fail before transport\n", encoding="ascii"
    )
    (arguments["secrets_dir"] / "unexpected").chmod(0o600)
    called = False

    def run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        nonlocal called
        called = True
        raise AssertionError("SSH must not run")

    with pytest.raises(remote.RemotePublicationError):
        remote.publish_remote(**arguments, runner=run)

    assert not called


@pytest.mark.parametrize(
    "remote_destination",
    (
        "/",
        "/vonk-forge",
        "relative/vonk-forge",
        "/srv/../vonk-forge",
        "/srv/docker/bad,name",
        "/srv/docker/bad\nname",
    ),
)
def test_rejects_broad_or_ambiguous_remote_destination_before_ssh(
    tmp_path: Path, remote_destination: str
) -> None:
    remote = _load_remote_module()
    arguments = _arguments(tmp_path, remote_destination=remote_destination)

    with pytest.raises(remote.RemotePublicationError):
        remote.publish_remote(
            **arguments,
            runner=lambda *_args, **_kwargs: pytest.fail("SSH must not run"),
        )


@pytest.mark.parametrize(
    "image",
    (
        "ghcr.io/carstvaartjes/vonk-forge-api:latest",
        "ghcr.io/example/vonk-forge-api:dev",
        "ghcr.io/carstvaartjes/vonk-forge-api:dev-sha-not-a-commit",
    ),
)
def test_rejects_unaccepted_publisher_image_before_ssh(
    tmp_path: Path, image: str
) -> None:
    remote = _load_remote_module()
    compose = _accepted_compose(tmp_path, image=image)
    arguments = _arguments(tmp_path, source_compose=compose)

    with pytest.raises(remote.RemotePublicationError):
        remote.publish_remote(
            **arguments,
            runner=lambda *_args, **_kwargs: pytest.fail("SSH must not run"),
        )


def test_accepts_exact_pinned_development_api_image(tmp_path: Path) -> None:
    remote = _load_remote_module()
    commit = "a" * 40
    digest = "b" * 64
    compose = _accepted_compose(
        tmp_path,
        image=(
            f"ghcr.io/carstvaartjes/vonk-forge-api:dev-sha-{commit}@sha256:{digest}"
        ),
    )
    arguments = _arguments(tmp_path, source_compose=compose, docker_mode="direct")
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        return subprocess.CompletedProcess(
            command, 0, stdout=b"/publish/vonk-forge\n", stderr=b""
        )

    remote.publish_remote(**arguments, runner=run)

    assert f"dev-sha-{commit}@sha256:{digest}" in commands[0][-1]
    assert "docker_mode=direct" in commands[0][-1]


def test_rejects_unsafe_identity_mode(tmp_path: Path) -> None:
    remote = _load_remote_module()
    identity = _identity(tmp_path)
    identity.chmod(0o644)
    arguments = _arguments(tmp_path, identity_file=identity)

    with pytest.raises(remote.RemotePublicationError, match="identity"):
        remote.publish_remote(
            **arguments,
            runner=lambda *_args, **_kwargs: pytest.fail("SSH must not run"),
        )


def test_remote_failure_is_redacted_and_never_reports_success(tmp_path: Path) -> None:
    remote = _load_remote_module()
    arguments = _arguments(tmp_path)
    leaked = (arguments["secrets_dir"] / "litellm-master-key").read_bytes().strip()

    def run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            command,
            1,
            stdout=b"",
            stderr=b"synthetic remote failure: " + leaked + b"\n",
        )

    with pytest.raises(remote.RemotePublicationError) as failed:
        remote.publish_remote(**arguments, runner=run)

    message = str(failed.value)
    assert "remote project publication failed" in message
    assert "[redacted]" in message
    assert leaked.decode() not in message
    assert "/srv/docker/vonk-forge" not in message


@pytest.mark.parametrize(
    "ssh_target", ("-oProxyCommand=bad", "bad target", "bad\ntarget")
)
def test_rejects_ambiguous_ssh_target(tmp_path: Path, ssh_target: str) -> None:
    remote = _load_remote_module()
    arguments = _arguments(tmp_path)
    arguments["ssh_target"] = ssh_target

    with pytest.raises(remote.RemotePublicationError, match="SSH target"):
        remote.publish_remote(
            **arguments,
            runner=lambda *_args, **_kwargs: pytest.fail("SSH must not run"),
        )
