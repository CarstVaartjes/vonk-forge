from __future__ import annotations

import fcntl
import hashlib
import importlib.machinery
import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import threading
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SECRETS_SCRIPT = ROOT / "scripts" / "dev-runtime-secrets.py"
PROJECT_SCRIPT = ROOT / "scripts" / "dev-runtime-project"
SOURCE_COMPOSE = ROOT / "deploy" / "compose" / "compose.dev.images.yaml"
PINNED_COMPATIBILITY_BLOCK = (
    "# Compatibility input for the current pinned renderer. Task 5 must retain this\n"
    "# token only for pinned output and omit it entirely from mutable output.\n"
    'x-pinned-expected-commit: "__VONK_EXPECTED_COMMIT__"\n'
)

ENROLL_HOSTNAME = "enroll.example.test"
AGENT_HOSTNAME = "agents.example.test"
REGISTRY_HOSTNAME = "registry.example.test"
MANAGEMENT_CIDRS = "192.0.2.0/24,2001:db8::/64"
DIRECT_FABRIC_CIDRS = "198.51.100.0/24,2001:db8:1::/64"
NAS_ADDRESS = "192.0.2.10"
OAUTH_CLIENT_ID = b"synthetic-tailscale-client-id\n"
OAUTH_CLIENT_SECRET = b"synthetic-tailscale-client-secret\n"
OAUTH_ROTATION_ID = "2fdb4cf7-f240-4a6f-b52f-06fdb4058d50"

EXPECTED_PROJECT_FILES = {
    "docker-compose.yml",
    "secrets/admin-password-verifier",
    "secrets/agent-ca-certificate",
    "secrets/agent-ca-key",
    "secrets/agent-proxy-auth",
    "secrets/controller-ca",
    "secrets/controller-server-certificate",
    "secrets/controller-server-key",
    "secrets/database-url",
    "secrets/git-signing-key",
    "secrets/host-runtime-grant-private-key",
    "secrets/litellm-database-password",
    "secrets/litellm-master-key",
    "secrets/litellm-upstream-key",
    "secrets/management-cidrs",
    "secrets/postgres-password",
    "secrets/token-signing-key",
    "secrets/tailscale-oauth-client-id",
    "secrets/tailscale-oauth-client-secret",
}

REQUIRED_SECRET_NAMES = {
    path.removeprefix("secrets/")
    for path in EXPECTED_PROJECT_FILES
    if path.startswith("secrets/")
}


def _run_secret_generator(secrets_dir: Path) -> subprocess.CompletedProcess[str]:
    oauth_root = secrets_dir.parent / "oauth-inputs"
    oauth_root.mkdir(mode=0o700, exist_ok=True)
    client_id = oauth_root / "client-id"
    client_secret = oauth_root / "client-secret"
    client_id.write_bytes(OAUTH_CLIENT_ID)
    client_secret.write_bytes(OAUTH_CLIENT_SECRET)
    client_id.chmod(0o600)
    client_secret.chmod(0o600)
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
            "--tailscale-oauth-client-id-file",
            str(client_id),
            "--tailscale-oauth-client-secret-file",
            str(client_secret),
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
    if source_compose == SOURCE_COMPOSE:
        source_compose = destination.parent / "accepted-docker-compose.dev.yml"
        rendered = SOURCE_COMPOSE.read_text(encoding="utf-8")
        rendered = rendered.removeprefix("name: vonk-forge-dev\n\n")
        rendered = rendered.replace(
            "__VONK_API_IMAGE__", "ghcr.io/carstvaartjes/vonk-forge-api:dev"
        ).replace(
            "__VONK_WORKER_IMAGE__", "ghcr.io/carstvaartjes/vonk-forge-worker:dev"
        )
        rendered = rendered.replace(PINNED_COMPATIBILITY_BLOCK, "", 1)
        source_compose.write_text(rendered, encoding="utf-8")
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
            "--direct-fabric-cidrs",
            DIRECT_FABRIC_CIDRS,
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
    assert not (destination / "secrets" / "admin-password").exists()
    assert not (destination / "secrets" / "controller-ca-key").exists()
    assert not (destination / "secrets" / "git-signing-key.pub").exists()
    assert not (
        destination / "secrets" / "host-runtime-grant-public-key"
    ).exists()
    assert project_lock_name(destination) == project_lock_name(
        Path("/another/local/mount") / destination.name
    )
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
    assert not compose.startswith("name:")
    assert "VONK_AGENT_ENROLL_HOSTNAME: enroll.example.test" in compose
    assert "VONK_AGENT_HOSTNAME: agents.example.test" in compose
    assert compose.count(
        'VONK_DIRECT_FABRIC_CIDRS: "198.51.100.0/24,2001:db8:1::/64"'
    ) == 2
    for name in (
        "agent-proxy-auth",
        "controller-server-key",
        "database-url",
        "git-signing-key",
        "host-runtime-grant-private-key",
        "litellm-master-key",
        "postgres-password",
        "token-signing-key",
    ):
        marker = (
            (secrets_dir / name).read_text(encoding="ascii", errors="ignore").strip()
        )
        if marker and marker in (result.stdout + result.stderr):
            pytest.fail(f"secret value from {name} leaked to project command output")


@pytest.mark.parametrize(
    ("entry_name", "directory"),
    (
        ("unexpected-secret", False),
        (".browser-access-upgrade-" + "a" * 32, True),
        (".admin-password-rotation-" + "b" * 32, True),
        (".tailscale-oauth-rotation-" + "c" * 32, True),
        (".admin-password-rotation", False),
        (".admin-password.rotate", False),
        (".admin-password-verifier.rotate", False),
    ),
)
def test_rejects_unknown_and_incomplete_transaction_source_entries(
    tmp_path: Path, entry_name: str, directory: bool
) -> None:
    secrets_dir = _prepare_source_secrets(tmp_path)
    entry = secrets_dir / entry_name
    if directory:
        entry.mkdir(mode=0o700)
    else:
        entry.write_bytes(b"synthetic transaction residue\n")
        entry.chmod(0o600)
    destination = tmp_path / "nas-project"

    result = _run_project(destination, secrets_dir)

    assert result.returncode == 1
    assert result.stdout == ""
    assert not destination.exists()
    assert entry_name not in result.stderr


def test_publication_waits_for_oauth_rotation_and_never_snapshots_a_mixed_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets_dir = _prepare_source_secrets(tmp_path)
    destination = tmp_path / "nas-project"
    replacement_root = tmp_path / "replacement-oauth-inputs"
    replacement_root.mkdir(mode=0o700)
    replacement_id = replacement_root / "client-id"
    replacement_secret = replacement_root / "client-secret"
    replacement_id.write_bytes(b"rotated-tailscale-client-id\n")
    replacement_secret.write_bytes(b"rotated-tailscale-client-secret\n")
    replacement_id.chmod(0o600)
    replacement_secret.chmod(0o600)

    runtime = importlib.util.module_from_spec(
        importlib.util.spec_from_file_location(
            "vonk_dev_runtime_secrets_rotation_test", SECRETS_SCRIPT
        )
    )
    assert runtime.__spec__ is not None
    assert runtime.__spec__.loader is not None
    runtime.__spec__.loader.exec_module(runtime)
    project = _load_project_module()
    first_oauth_install = threading.Event()
    finish_rotation = threading.Event()
    source_read_started = threading.Event()
    shared_lock_attempted = threading.Event()
    rotation_errors: list[BaseException] = []
    publication_errors: list[BaseException] = []
    real_replace = runtime.os.replace
    real_read_regular_at = project._read_regular_at
    real_flock = project.fcntl.flock
    source_identity = (secrets_dir.stat().st_dev, secrets_dir.stat().st_ino)

    def pause_after_first_oauth_install(*args: object, **kwargs: object) -> None:
        real_replace(*args, **kwargs)
        if args[:2] == (
            "new-client-id",
            "tailscale-oauth-client-id",
        ):
            first_oauth_install.set()
            assert finish_rotation.wait(timeout=5)

    def observe_source_read(
        directory: int, name: str, **kwargs: object
    ) -> bytes:
        metadata = os.fstat(directory)
        if (metadata.st_dev, metadata.st_ino) == source_identity:
            source_read_started.set()
        return real_read_regular_at(directory, name, **kwargs)

    def observe_flock(descriptor: int, operation: int) -> None:
        if operation == fcntl.LOCK_SH:
            shared_lock_attempted.set()
        real_flock(descriptor, operation)

    monkeypatch.setattr(runtime.os, "replace", pause_after_first_oauth_install)
    monkeypatch.setattr(project, "_read_regular_at", observe_source_read)
    monkeypatch.setattr(project.fcntl, "flock", observe_flock)

    def rotate() -> None:
        try:
            runtime.prepare_runtime_secrets(
                secrets_dir,
                management_cidrs=MANAGEMENT_CIDRS,
                enroll_hostname=ENROLL_HOSTNAME,
                agent_hostname=AGENT_HOSTNAME,
                registry_hostname=REGISTRY_HOSTNAME,
                tailscale_oauth_client_id_file=replacement_id,
                tailscale_oauth_client_secret_file=replacement_secret,
                rotate_tailscale_oauth=True,
                tailscale_oauth_rotation_id=OAUTH_ROTATION_ID,
            )
        except (runtime.RuntimeSecretError, OSError, AssertionError) as error:
            rotation_errors.append(error)

    def publish() -> None:
        try:
            project.publish_project(
                source_compose=SOURCE_COMPOSE,
                secrets_dir=secrets_dir,
                destination=destination,
                nas_address=NAS_ADDRESS,
                management_cidrs=MANAGEMENT_CIDRS,
                direct_fabric_cidrs=DIRECT_FABRIC_CIDRS,
                enroll_hostname=ENROLL_HOSTNAME,
                agent_hostname=AGENT_HOSTNAME,
                registry_hostname=REGISTRY_HOSTNAME,
            )
        except (project.ProjectPublicationError, OSError, AssertionError) as error:
            publication_errors.append(error)

    rotation_thread = threading.Thread(target=rotate)
    publication_thread = threading.Thread(target=publish)
    rotation_thread.start()
    assert first_oauth_install.wait(timeout=5)
    publication_thread.start()
    try:
        assert shared_lock_attempted.wait(timeout=5)
        assert not source_read_started.wait(timeout=0.5)
    finally:
        finish_rotation.set()
        rotation_thread.join(timeout=5)
        publication_thread.join(timeout=5)

    assert not rotation_thread.is_alive()
    assert not publication_thread.is_alive()
    assert rotation_errors == []
    assert publication_errors == []
    assert source_read_started.is_set()
    published_id = (
        destination / "secrets" / "tailscale-oauth-client-id"
    ).read_bytes()
    published_secret = (
        destination / "secrets" / "tailscale-oauth-client-secret"
    ).read_bytes()
    assert (published_id, published_secret) in {
        (OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET),
        (replacement_id.read_bytes(), replacement_secret.read_bytes()),
    }


def test_failed_source_validation_releases_the_shared_generation_lock(
    tmp_path: Path,
) -> None:
    secrets_dir = _prepare_source_secrets(tmp_path)
    (secrets_dir / "management-cidrs").write_text(
        "198.51.100.0/24\n", encoding="ascii"
    )
    project = _load_project_module()

    with pytest.raises(
        project.ProjectPublicationError,
        match="source secret bundle does not match project inputs",
    ):
        project.publish_project(
            source_compose=SOURCE_COMPOSE,
            secrets_dir=secrets_dir,
            destination=tmp_path / "nas-project",
            nas_address=NAS_ADDRESS,
            management_cidrs=MANAGEMENT_CIDRS,
            direct_fabric_cidrs=DIRECT_FABRIC_CIDRS,
            enroll_hostname=ENROLL_HOSTNAME,
            agent_hostname=AGENT_HOSTNAME,
            registry_hostname=REGISTRY_HOSTNAME,
        )

    descriptor = os.open(secrets_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)


def test_source_lock_descriptor_is_closed_when_locking_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets_dir = _prepare_source_secrets(tmp_path)
    project = _load_project_module()
    real_flock = project.fcntl.flock

    class SyntheticInterrupt(BaseException):
        pass

    def interrupt_after_lock(descriptor: int, operation: int) -> None:
        real_flock(descriptor, operation)
        if operation == fcntl.LOCK_SH:
            raise SyntheticInterrupt

    monkeypatch.setattr(project.fcntl, "flock", interrupt_after_lock)
    with pytest.raises(SyntheticInterrupt):
        project._open_locked_source_directory(secrets_dir)

    descriptor = os.open(secrets_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        real_flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)


def project_lock_name(destination: Path) -> str:
    project = _load_project_module()
    return project._publication_lock_path(destination).name


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


def test_permits_validated_copy_to_a_mounted_destination(tmp_path: Path) -> None:
    secrets_dir = _prepare_source_secrets(tmp_path)
    project = _load_project_module()
    destination = tmp_path / "mounted-nas"

    project.publish_project(
        source_compose=SOURCE_COMPOSE,
        secrets_dir=secrets_dir,
        destination=destination,
        nas_address=NAS_ADDRESS,
        management_cidrs=MANAGEMENT_CIDRS,
        direct_fabric_cidrs=DIRECT_FABRIC_CIDRS,
        enroll_hostname=ENROLL_HOSTNAME,
        agent_hostname=AGENT_HOSTNAME,
        registry_hostname=REGISTRY_HOSTNAME,
    )

    assert _project_listing(destination) == EXPECTED_PROJECT_FILES


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
    monkeypatch.setattr(project, "_filesystem_type", lambda _path: "nfs4")

    with pytest.raises(project.ProjectPublicationError):
        project.publish_project(
            source_compose=SOURCE_COMPOSE,
            secrets_dir=secrets_dir,
            destination=destination,
            nas_address=NAS_ADDRESS,
            management_cidrs=MANAGEMENT_CIDRS,
            direct_fabric_cidrs=DIRECT_FABRIC_CIDRS,
            enroll_hostname=ENROLL_HOSTNAME,
            agent_hostname=AGENT_HOSTNAME,
            registry_hostname=REGISTRY_HOSTNAME,
        )

    assert not destination.exists()
    assert list(remote_temporary.iterdir()) == []


def test_revalidates_created_local_staging_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets_dir = _prepare_source_secrets(tmp_path)
    project = _load_project_module()
    destination = tmp_path / "nas-project"
    temporary = tmp_path / "temporary"
    temporary.mkdir(mode=0o700)
    monkeypatch.setattr(project.tempfile, "gettempdir", lambda: str(temporary))

    filesystem_checks = 0

    def filesystem_type(_path: Path) -> str:
        nonlocal filesystem_checks
        filesystem_checks += 1
        return "ext4" if filesystem_checks == 1 else "nfs4"

    monkeypatch.setattr(project, "_filesystem_type", filesystem_type)

    with pytest.raises(project.ProjectPublicationError):
        project.publish_project(
            source_compose=SOURCE_COMPOSE,
            secrets_dir=secrets_dir,
            destination=destination,
            nas_address=NAS_ADDRESS,
            management_cidrs=MANAGEMENT_CIDRS,
            direct_fabric_cidrs=DIRECT_FABRIC_CIDRS,
            enroll_hostname=ENROLL_HOSTNAME,
            agent_hostname=AGENT_HOSTNAME,
            registry_hostname=REGISTRY_HOSTNAME,
        )

    assert not destination.exists()
    assert list(temporary.iterdir()) == []


def test_local_staging_writes_stay_on_pinned_directory_after_path_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets_dir = _prepare_source_secrets(tmp_path)
    project = _load_project_module()
    destination = tmp_path / "nas-project"
    temporary = tmp_path / "temporary"
    temporary.mkdir(mode=0o700)
    redirected = tmp_path / "redirected"
    redirected.mkdir(mode=0o700)
    renamed = temporary / "renamed-staging"
    monkeypatch.setattr(project.tempfile, "gettempdir", lambda: str(temporary))
    monkeypatch.setattr(project, "_filesystem_type", lambda _path: "ext4")
    real_open = os.open
    swapped = False

    def swap_before_first_staged_write(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "docker-compose.yml" and dir_fd is not None and not swapped:
            staging = next(temporary.glob(".vonk-forge-project-*"))
            staging.rename(renamed)
            staging.symlink_to(redirected, target_is_directory=True)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(project.os, "open", swap_before_first_staged_write)
    try:
        project.publish_project(
            source_compose=SOURCE_COMPOSE,
            secrets_dir=secrets_dir,
            destination=destination,
            nas_address=NAS_ADDRESS,
            management_cidrs=MANAGEMENT_CIDRS,
            direct_fabric_cidrs=DIRECT_FABRIC_CIDRS,
            enroll_hostname=ENROLL_HOSTNAME,
            agent_hostname=AGENT_HOSTNAME,
            registry_hostname=REGISTRY_HOSTNAME,
        )

        assert swapped is True
        assert _project_listing(destination) == EXPECTED_PROJECT_FILES
        assert list(redirected.iterdir()) == []
        assert list(renamed.iterdir()) == []
    finally:
        for child in temporary.iterdir():
            if child.is_symlink():
                child.unlink()
        renamed.rmdir()


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
    overlapping_fabric = _run_project(
        tmp_path / "overlapping-fabric",
        secrets_dir,
        SOURCE_COMPOSE,
        "--direct-fabric-cidrs",
        "192.0.2.0/25",
    )

    assert bad_address.returncode == 1
    assert bad_address.stdout == ""
    assert bad_hostname.returncode == 1
    assert bad_hostname.stdout == ""
    assert overlapping_fabric.returncode == 1
    assert overlapping_fabric.stdout == ""


def test_requires_an_explicit_single_node_fabric_choice(tmp_path: Path) -> None:
    secrets_dir = _prepare_source_secrets(tmp_path)
    destination = tmp_path / "single-node"

    result = _run_project(
        destination,
        secrets_dir,
        SOURCE_COMPOSE,
        "--direct-fabric-cidrs",
        "none",
    )

    assert result.returncode == 0, result.stderr
    compose = (destination / "docker-compose.yml").read_text(encoding="utf-8")
    assert compose.count('VONK_DIRECT_FABRIC_CIDRS: ""') == 2


def _project_contents(destination: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(destination)): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file() and not path.name.startswith(".")
    }


def _second_generation(tmp_path: Path) -> tuple[Path, Path]:
    source_root = tmp_path / "second-source"
    source_root.mkdir(mode=0o700)
    secrets_dir = _prepare_source_secrets(source_root)
    compose = source_root / "docker-compose.dev.yml"
    compose.write_bytes(SOURCE_COMPOSE.read_bytes() + b"\n# second generation\n")
    compose.chmod(0o644)
    return secrets_dir, compose


def test_interrupted_publish_restores_the_complete_previous_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_secrets = _prepare_source_secrets(tmp_path)
    second_secrets, second_compose = _second_generation(tmp_path)
    project = _load_project_module()
    destination = tmp_path / "nas-project"
    project.publish_project(
        source_compose=SOURCE_COMPOSE,
        secrets_dir=first_secrets,
        destination=destination,
        nas_address=NAS_ADDRESS,
        management_cidrs=MANAGEMENT_CIDRS,
        direct_fabric_cidrs=DIRECT_FABRIC_CIDRS,
        enroll_hostname=ENROLL_HOSTNAME,
        agent_hostname=AGENT_HOSTNAME,
        registry_hostname=REGISTRY_HOSTNAME,
    )
    previous = _project_contents(destination)
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
            source_compose=second_compose,
            secrets_dir=second_secrets,
            destination=destination,
            nas_address=NAS_ADDRESS,
            management_cidrs=MANAGEMENT_CIDRS,
            direct_fabric_cidrs=DIRECT_FABRIC_CIDRS,
            enroll_hostname=ENROLL_HOSTNAME,
            agent_hostname=AGENT_HOSTNAME,
            registry_hostname=REGISTRY_HOSTNAME,
        )

    assert _project_contents(destination) == previous
    assert sorted(path.name for path in destination.iterdir()) == [
        "docker-compose.yml",
        "secrets",
    ]


def test_rerun_recovers_a_process_interruption_before_publishing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedProcessExit(BaseException):
        pass

    first_secrets = _prepare_source_secrets(tmp_path)
    second_secrets, second_compose = _second_generation(tmp_path)
    project = _load_project_module()
    destination = tmp_path / "nas-project"
    project.publish_project(
        source_compose=SOURCE_COMPOSE,
        secrets_dir=first_secrets,
        destination=destination,
        nas_address=NAS_ADDRESS,
        management_cidrs=MANAGEMENT_CIDRS,
        direct_fabric_cidrs=DIRECT_FABRIC_CIDRS,
        enroll_hostname=ENROLL_HOSTNAME,
        agent_hostname=AGENT_HOSTNAME,
        registry_hostname=REGISTRY_HOSTNAME,
    )
    real_replace = os.replace
    replace_calls = 0

    def terminate_during_publication(
        source: str | os.PathLike[str],
        target: str | os.PathLike[str],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 3:
            raise SimulatedProcessExit
        real_replace(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(project.os, "replace", terminate_during_publication)
    with pytest.raises(SimulatedProcessExit):
        project.publish_project(
            source_compose=second_compose,
            secrets_dir=second_secrets,
            destination=destination,
            nas_address=NAS_ADDRESS,
            management_cidrs=MANAGEMENT_CIDRS,
            direct_fabric_cidrs=DIRECT_FABRIC_CIDRS,
            enroll_hostname=ENROLL_HOSTNAME,
            agent_hostname=AGENT_HOSTNAME,
            registry_hostname=REGISTRY_HOSTNAME,
        )

    assert (destination / ".vonk-forge-publish").is_dir()

    monkeypatch.setattr(project.os, "replace", real_replace)
    project.publish_project(
        source_compose=second_compose,
        secrets_dir=second_secrets,
        destination=destination,
        nas_address=NAS_ADDRESS,
        management_cidrs=MANAGEMENT_CIDRS,
        direct_fabric_cidrs=DIRECT_FABRIC_CIDRS,
        enroll_hostname=ENROLL_HOSTNAME,
        agent_hostname=AGENT_HOSTNAME,
        registry_hostname=REGISTRY_HOSTNAME,
    )

    assert (
        (destination / "docker-compose.yml")
        .read_bytes()
        .endswith(b"# second generation\n")
    )
    for name in REQUIRED_SECRET_NAMES:
        assert (destination / "secrets" / name).read_bytes() == (
            second_secrets / name
        ).read_bytes()
    assert sorted(path.name for path in destination.iterdir()) == [
        "docker-compose.yml",
        "secrets",
    ]


def test_rerun_discards_partial_cleanup_tombstone_after_successful_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedProcessExit(BaseException):
        pass

    first_secrets = _prepare_source_secrets(tmp_path)
    second_secrets, second_compose = _second_generation(tmp_path)
    project = _load_project_module()
    destination = tmp_path / "nas-project"
    project.publish_project(
        source_compose=SOURCE_COMPOSE,
        secrets_dir=first_secrets,
        destination=destination,
        nas_address=NAS_ADDRESS,
        management_cidrs=MANAGEMENT_CIDRS,
        direct_fabric_cidrs=DIRECT_FABRIC_CIDRS,
        enroll_hostname=ENROLL_HOSTNAME,
        agent_hostname=AGENT_HOSTNAME,
        registry_hostname=REGISTRY_HOSTNAME,
    )
    real_rmtree = shutil.rmtree

    def terminate_during_completed_journal_cleanup(
        path: str | os.PathLike[str],
        *args: object,
        **kwargs: object,
    ) -> None:
        cleanup = Path(path)
        if cleanup.parent == destination and cleanup.name.startswith(
            ".vonk-forge-publish"
        ):
            (cleanup / "manifest.json").unlink(missing_ok=True)
            raise SimulatedProcessExit
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(
        project.shutil, "rmtree", terminate_during_completed_journal_cleanup
    )
    with pytest.raises(SimulatedProcessExit):
        project.publish_project(
            source_compose=second_compose,
            secrets_dir=second_secrets,
            destination=destination,
            nas_address=NAS_ADDRESS,
            management_cidrs=MANAGEMENT_CIDRS,
            direct_fabric_cidrs=DIRECT_FABRIC_CIDRS,
            enroll_hostname=ENROLL_HOSTNAME,
            agent_hostname=AGENT_HOSTNAME,
            registry_hostname=REGISTRY_HOSTNAME,
        )

    assert not (destination / ".vonk-forge-publish").exists()
    assert (destination / ".vonk-forge-publish.cleanup").is_dir()

    monkeypatch.setattr(project.shutil, "rmtree", real_rmtree)
    project.publish_project(
        source_compose=second_compose,
        secrets_dir=second_secrets,
        destination=destination,
        nas_address=NAS_ADDRESS,
        management_cidrs=MANAGEMENT_CIDRS,
        direct_fabric_cidrs=DIRECT_FABRIC_CIDRS,
        enroll_hostname=ENROLL_HOSTNAME,
        agent_hostname=AGENT_HOSTNAME,
        registry_hostname=REGISTRY_HOSTNAME,
    )

    assert sorted(path.name for path in destination.iterdir()) == [
        "docker-compose.yml",
        "secrets",
    ]
    assert (
        (destination / "docker-compose.yml")
        .read_bytes()
        .endswith(b"# second generation\n")
    )


def test_live_publisher_is_rejected_and_stale_journal_recovers_on_rerun(
    tmp_path: Path,
) -> None:
    secrets_dir = _prepare_source_secrets(tmp_path)
    project = _load_project_module()
    destination = tmp_path / "nas-project"
    destination.mkdir(mode=0o700)
    (destination / "secrets").mkdir(mode=0o700)
    lock_path = project._publication_lock_path(destination)
    lock_descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    active_journal = destination / ".vonk-forge-publish"
    active_journal.mkdir(mode=0o700)
    cleanup_tombstone = destination / ".vonk-forge-publish.cleanup"
    cleanup_tombstone.mkdir(mode=0o700)
    cleanup_marker = cleanup_tombstone / "partial"
    cleanup_marker.write_bytes(b"private cleanup data")
    try:
        with pytest.raises(
            project.ProjectPublicationError, match="publisher is active"
        ):
            project.publish_project(
                source_compose=SOURCE_COMPOSE,
                secrets_dir=secrets_dir,
                destination=destination,
                nas_address=NAS_ADDRESS,
                management_cidrs=MANAGEMENT_CIDRS,
                direct_fabric_cidrs=DIRECT_FABRIC_CIDRS,
                enroll_hostname=ENROLL_HOSTNAME,
                agent_hostname=AGENT_HOSTNAME,
                registry_hostname=REGISTRY_HOSTNAME,
            )
        assert active_journal.is_dir()
        assert list(active_journal.iterdir()) == []
        assert cleanup_marker.read_bytes() == b"private cleanup data"
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)

    project.publish_project(
        source_compose=SOURCE_COMPOSE,
        secrets_dir=secrets_dir,
        destination=destination,
        nas_address=NAS_ADDRESS,
        management_cidrs=MANAGEMENT_CIDRS,
        direct_fabric_cidrs=DIRECT_FABRIC_CIDRS,
        enroll_hostname=ENROLL_HOSTNAME,
        agent_hostname=AGENT_HOSTNAME,
        registry_hostname=REGISTRY_HOSTNAME,
    )

    assert sorted(path.name for path in destination.iterdir()) == [
        "docker-compose.yml",
        "secrets",
    ]
    assert _project_listing(destination) == EXPECTED_PROJECT_FILES
