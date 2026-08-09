from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

import vonk_control.dev_init as dev_init
from vonk_control.dev_init import (
    DevInitError,
    initialize_repository,
    main,
    stage_runtime_secrets,
)


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(worktree: Path, name: str, content: str) -> str:
    (worktree / name).write_text(content, encoding="utf-8")
    _git(worktree, "add", name)
    _git(worktree, "commit", "-qm", name)
    return _git(worktree, "rev-parse", "HEAD")


def _origin(tmp_path: Path) -> tuple[Path, Path, str, str]:
    origin = tmp_path / "origin.git"
    subprocess.run(("git", "init", "--bare", "-q", str(origin)), check=True)
    worktree = tmp_path / "publisher"
    subprocess.run(("git", "clone", "-q", str(origin), str(worktree)), check=True)
    _git(worktree, "config", "user.email", "test@example.invalid")
    _git(worktree, "config", "user.name", "Test")
    _git(worktree, "checkout", "-q", "--orphan", "main")
    initial = _commit(worktree, "README.md", "initial\n")
    _git(worktree, "push", "-qu", "origin", "main")
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    return origin, worktree, origin.as_uri(), initial


@pytest.fixture
def local_acceptance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VONK_DEV_LOCAL_ACCEPTANCE", "1")


def test_initialize_repository_clones_main_at_the_expected_commit(
    tmp_path: Path, local_acceptance: None
) -> None:
    _origin_path, _publisher, repository_url, expected = _origin(tmp_path)
    destination = tmp_path / "repository"

    initialize_repository(destination, repository_url, expected)

    assert _git(destination, "status", "--porcelain=v1", "--untracked-files=all") == ""
    assert _git(destination, "symbolic-ref", "--short", "HEAD") == "main"
    assert _git(destination, "rev-parse", "main") == expected
    assert _git(destination, "remote", "get-url", "origin") == repository_url


def test_initialize_repository_rejects_unsafe_fresh_roots_and_commit_ids(
    tmp_path: Path, local_acceptance: None
) -> None:
    _origin_path, _publisher, repository_url, expected = _origin(tmp_path)
    destination = tmp_path / "repository"

    with pytest.raises(DevInitError, match="commit"):
        initialize_repository(destination, repository_url, expected.upper())

    actual = tmp_path / "actual"
    actual.mkdir()
    destination.symlink_to(actual, target_is_directory=True)
    with pytest.raises(DevInitError, match="root"):
        initialize_repository(destination, repository_url, expected)

    non_repository = tmp_path / "non-repository"
    non_repository.mkdir()
    (non_repository / "operator-note").write_text("do not replace", encoding="utf-8")
    with pytest.raises(DevInitError, match="metadata"):
        initialize_repository(non_repository, repository_url, expected)


def test_initialize_repository_rejects_local_origins_without_the_acceptance_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _origin_path, _publisher, repository_url, expected = _origin(tmp_path)
    monkeypatch.delenv("VONK_DEV_LOCAL_ACCEPTANCE", raising=False)

    with pytest.raises(DevInitError, match="origin"):
        initialize_repository(tmp_path / "repository", repository_url, expected)


def test_public_origin_policy_accepts_only_the_canonical_repository_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VONK_DEV_LOCAL_ACCEPTANCE", raising=False)

    assert (
        dev_init._origin_is_allowed(
            "https://github.com/CarstVaartjes/vonk-forge.git"
        )
        is False
    )
    with pytest.raises(DevInitError, match="origin"):
        dev_init._origin_is_allowed("https://github.com/CarstVaartjes/other.git")


def test_initialize_repository_fast_forwards_main_and_preserves_other_refs(
    tmp_path: Path, local_acceptance: None
) -> None:
    _origin_path, publisher, repository_url, initial = _origin(tmp_path)
    destination = tmp_path / "repository"
    initialize_repository(destination, repository_url, initial)
    _git(destination, "branch", "operator-notes", initial)
    expected = _commit(publisher, "new.txt", "next\n")
    _git(publisher, "push", "-q", "origin", "main")

    initialize_repository(destination, repository_url, expected)

    assert _git(destination, "rev-parse", "main") == expected
    assert _git(destination, "rev-parse", "operator-notes") == initial
    assert _git(destination, "status", "--porcelain=v1", "--untracked-files=all") == ""


def test_initialize_repository_rejects_changed_origin_dirty_and_non_fast_forward_updates(
    tmp_path: Path, local_acceptance: None
) -> None:
    _origin_path, publisher, repository_url, initial = _origin(tmp_path)
    destination = tmp_path / "repository"
    initialize_repository(destination, repository_url, initial)

    _git(destination, "remote", "set-url", "origin", repository_url + "/changed")
    with pytest.raises(DevInitError, match="origin"):
        initialize_repository(destination, repository_url, initial)
    _git(destination, "remote", "set-url", "origin", repository_url)

    (destination / "README.md").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(DevInitError, match="clean"):
        initialize_repository(destination, repository_url, initial)
    _git(destination, "reset", "--hard", "-q")

    expected = _commit(publisher, "remote.txt", "remote\n")
    _git(publisher, "push", "-q", "origin", "main")
    _git(destination, "config", "user.email", "operator@example.invalid")
    _git(destination, "config", "user.name", "Operator")
    _commit(destination, "local.txt", "local\n")
    with pytest.raises(DevInitError, match="fast-forward"):
        initialize_repository(destination, repository_url, expected)


def test_initialize_repository_rejects_a_rollback_commit(
    tmp_path: Path, local_acceptance: None
) -> None:
    _origin_path, publisher, repository_url, initial = _origin(tmp_path)
    destination = tmp_path / "repository"
    initialize_repository(destination, repository_url, initial)
    expected = _commit(publisher, "remote.txt", "remote\n")
    _git(publisher, "push", "-q", "origin", "main")
    initialize_repository(destination, repository_url, expected)

    with pytest.raises(DevInitError, match="fast-forward"):
        initialize_repository(destination, repository_url, initial)


def _secret_source(root: Path) -> Path:
    root.mkdir()
    (root / "database-url").write_bytes(b"postgresql://vonk:secret@postgres/vonk\n")
    (root / "git-signing-key").write_bytes(b"-----BEGIN OPENSSH PRIVATE KEY-----\nkey\n")
    return root


def test_stage_runtime_secrets_creates_disjoint_service_projections(tmp_path: Path) -> None:
    source = _secret_source(tmp_path / "source")
    api_root = tmp_path / "api"
    worker_root = tmp_path / "worker"

    stage_runtime_secrets(source, api_root, worker_root)

    assert {path.name for path in api_root.iterdir()} == {
        "admin-grant-private-key",
        "database-url",
        "git-signing-key",
    }
    assert {path.name for path in worker_root.iterdir()} == {
        "database-url",
        "worker-api-token",
    }
    assert (api_root / "database-url").read_bytes() == (
        b"postgresql://vonk:secret@postgres/vonk\n"
    )
    assert (worker_root / "database-url").read_bytes() == (
        b"postgresql://vonk:secret@postgres/vonk\n"
    )
    assert (api_root / "git-signing-key").read_bytes().startswith(b"-----BEGIN")
    assert (api_root / "admin-grant-private-key").read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")
    assert len((worker_root / "worker-api-token").read_text(encoding="ascii")) >= 32
    assert stat.S_IMODE((api_root / "admin-grant-private-key").stat().st_mode) == 0o400
    assert stat.S_IMODE((worker_root / "worker-api-token").stat().st_mode) == 0o400
    assert not (api_root / "worker-api-token").exists()
    assert not (worker_root / "git-signing-key").exists()
    assert not (worker_root / "admin-grant-private-key").exists()


def test_stage_runtime_secrets_rejects_symlink_inputs_and_projection_roots(
    tmp_path: Path,
) -> None:
    source = _secret_source(tmp_path / "source")
    actual_database_url = source / "database-url"
    actual_database_url.rename(source / "database-url.real")
    (source / "database-url").symlink_to(source / "database-url.real")

    with pytest.raises(DevInitError, match="source"):
        stage_runtime_secrets(source, tmp_path / "api", tmp_path / "worker")

    source = _secret_source(tmp_path / "safe-source")
    actual_api = tmp_path / "actual-api"
    actual_api.mkdir()
    api_root = tmp_path / "api"
    api_root.symlink_to(actual_api, target_is_directory=True)
    with pytest.raises(DevInitError, match="projection"):
        stage_runtime_secrets(source, api_root, tmp_path / "worker")


def test_stage_runtime_secrets_rejects_parent_symlink_aliases_before_mutation(
    tmp_path: Path,
) -> None:
    source = _secret_source(tmp_path / "source")
    actual_parent = tmp_path / "actual-parent"
    shared = actual_parent / "shared"
    shared.mkdir(parents=True)
    sentinel = shared / "operator-owned"
    sentinel.write_bytes(b"preserve-me\n")
    shared.chmod(0o751)
    before = shared.stat()
    alias_parent = tmp_path / "alias-parent"
    alias_parent.symlink_to(actual_parent, target_is_directory=True)

    with pytest.raises(DevInitError, match="projection"):
        stage_runtime_secrets(source, shared, alias_parent / "shared")

    after = shared.stat()
    assert sentinel.read_bytes() == b"preserve-me\n"
    assert {path.name for path in shared.iterdir()} == {"operator-owned"}
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode) == 0o751
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)


def test_stage_runtime_secrets_requires_absolute_projection_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _secret_source(tmp_path / "source")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(DevInitError, match="absolute"):
        stage_runtime_secrets(source, Path("api"), tmp_path / "worker")

    assert not (tmp_path / "api").exists()
    assert not (tmp_path / "worker").exists()


def test_stage_runtime_secrets_rejects_symlinked_parent_components(
    tmp_path: Path,
) -> None:
    source = _secret_source(tmp_path / "source")
    actual_parent = tmp_path / "actual-parent"
    actual_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(actual_parent, target_is_directory=True)

    with pytest.raises(DevInitError, match="projection"):
        stage_runtime_secrets(
            source,
            tmp_path / "api",
            linked_parent / "worker",
        )

    assert not (actual_parent / "worker").exists()


def test_stage_runtime_secrets_preserves_generated_credentials_and_refreshes_inputs(
    tmp_path: Path,
) -> None:
    source = _secret_source(tmp_path / "source")
    api_root = tmp_path / "api"
    worker_root = tmp_path / "worker"
    stage_runtime_secrets(source, api_root, worker_root)
    admin_key = (api_root / "admin-grant-private-key").read_bytes()
    worker_token = (worker_root / "worker-api-token").read_bytes()
    (source / "database-url").write_bytes(
        b"postgresql://vonk:replacement@postgres/vonk\n"
    )
    (source / "git-signing-key").write_bytes(b"replacement-signing-key\n")
    api_root.chmod(0o700)
    worker_root.chmod(0o700)
    (api_root / "unexpected-api-authority").write_bytes(b"remove\n")
    (worker_root / "unexpected-worker-authority").write_bytes(b"remove\n")

    stage_runtime_secrets(source, api_root, worker_root)

    assert (api_root / "admin-grant-private-key").read_bytes() == admin_key
    assert (worker_root / "worker-api-token").read_bytes() == worker_token
    assert (api_root / "database-url").read_bytes() == (
        b"postgresql://vonk:replacement@postgres/vonk\n"
    )
    assert (worker_root / "database-url").read_bytes() == (
        b"postgresql://vonk:replacement@postgres/vonk\n"
    )
    assert (api_root / "git-signing-key").read_bytes() == b"replacement-signing-key\n"
    assert {path.name for path in api_root.iterdir()} == {
        "admin-grant-private-key",
        "database-url",
        "git-signing-key",
    }
    assert {path.name for path in worker_root.iterdir()} == {
        "database-url",
        "worker-api-token",
    }


@pytest.mark.parametrize(
    ("projection", "name", "malformed"),
    (
        ("api", "admin-grant-private-key", b"not-an-ed25519-private-key\n"),
        ("worker", "worker-api-token", b"short"),
    ),
)
def test_stage_runtime_secrets_rejects_malformed_generated_credentials(
    tmp_path: Path, projection: str, name: str, malformed: bytes
) -> None:
    source = _secret_source(tmp_path / "source")
    api_root = tmp_path / "api"
    worker_root = tmp_path / "worker"
    stage_runtime_secrets(source, api_root, worker_root)
    root = api_root if projection == "api" else worker_root
    root.chmod(0o700)
    target = root / name
    target.unlink()
    target.write_bytes(malformed)
    target.chmod(0o400)

    with pytest.raises(DevInitError, match="generated credential"):
        stage_runtime_secrets(source, api_root, worker_root)

    assert target.read_bytes() == malformed


@pytest.mark.parametrize(
    ("projection", "name"),
    (
        ("api", "admin-grant-private-key"),
        ("worker", "worker-api-token"),
    ),
)
def test_stage_runtime_secrets_rejects_symlinked_generated_credentials(
    tmp_path: Path, projection: str, name: str
) -> None:
    source = _secret_source(tmp_path / "source")
    api_root = tmp_path / "api"
    worker_root = tmp_path / "worker"
    stage_runtime_secrets(source, api_root, worker_root)
    root = api_root if projection == "api" else worker_root
    root.chmod(0o700)
    target = root / name
    target.unlink()
    outside = tmp_path / f"outside-{name}"
    outside.write_bytes(b"must-not-be-read-or-replaced\n")
    target.symlink_to(outside)

    with pytest.raises(DevInitError, match="generated credential"):
        stage_runtime_secrets(source, api_root, worker_root)

    assert target.is_symlink()
    assert outside.read_bytes() == b"must-not-be-read-or-replaced\n"


def test_main_initializes_repository_synthetic_state_and_runtime_secrets(
    tmp_path: Path, local_acceptance: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _origin_path, _publisher, repository_url, expected = _origin(tmp_path)
    source = _secret_source(tmp_path / "source")
    repository = tmp_path / "repository"
    api_root = tmp_path / "api"
    worker_root = tmp_path / "worker"
    identity = tmp_path / "identity"
    state = tmp_path / "state"
    routes = tmp_path / "routes"
    supervisor = tmp_path / "supervisor"
    monkeypatch.setenv("VONK_DEV_EXPECTED_COMMIT", expected)
    monkeypatch.setenv("VONK_DEV_REPOSITORY_URL", repository_url)
    monkeypatch.setenv("VONK_REPOSITORY_PATH", str(repository))
    monkeypatch.setenv("VONK_DEV_SECRET_SOURCE_ROOT", str(source))
    monkeypatch.setenv("VONK_DEV_API_SECRET_ROOT", str(api_root))
    monkeypatch.setenv("VONK_DEV_WORKER_SECRET_ROOT", str(worker_root))
    monkeypatch.setenv("VONK_CONTROL_IDENTITY_ROOT", str(identity))
    monkeypatch.setenv("VONK_STATE_PATH", str(state))
    monkeypatch.setenv("VONK_ROUTE_ROOT", str(routes))
    monkeypatch.setenv("VONK_SUPERVISOR_ROOT", str(supervisor))

    assert main() == 0
    assert _git(repository, "rev-parse", "main") == expected
    assert json.loads((identity / "active.json").read_text(encoding="ascii"))["projection_kind"] == "active"
    assert (api_root / "admin-grant-private-key").is_file()
    assert (worker_root / "worker-api-token").is_file()
    assert all(path.is_dir() for path in (state, routes, supervisor))


def test_main_preflights_all_required_environment_before_cloning(
    tmp_path: Path, local_acceptance: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _origin_path, _publisher, repository_url, expected = _origin(tmp_path)
    source = _secret_source(tmp_path / "source")
    repository = tmp_path / "repository"
    monkeypatch.setenv("VONK_DEV_EXPECTED_COMMIT", expected)
    monkeypatch.setenv("VONK_DEV_REPOSITORY_URL", repository_url)
    monkeypatch.setenv("VONK_REPOSITORY_PATH", str(repository))
    monkeypatch.setenv("VONK_DEV_SECRET_SOURCE_ROOT", str(source))
    monkeypatch.setenv("VONK_DEV_API_SECRET_ROOT", str(tmp_path / "api"))
    monkeypatch.delenv("VONK_DEV_WORKER_SECRET_ROOT", raising=False)

    with pytest.raises(DevInitError, match="VONK_DEV_WORKER_SECRET_ROOT is required"):
        main()

    assert not repository.exists()


def test_active_projection_retries_partial_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = os.write

    def partial_write(descriptor: int, content: bytes) -> int:
        return real_write(descriptor, content[:7])

    monkeypatch.setattr(dev_init.os, "write", partial_write)
    identity = tmp_path / "identity"

    dev_init._write_active_projection(identity)

    raw = (identity / "active.json").read_bytes()
    assert raw.endswith(b"\n")
    assert json.loads(raw)["projection_kind"] == "active"
