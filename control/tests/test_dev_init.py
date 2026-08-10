from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from vonk_control import dev_init
from vonk_control.dev_init import (
    DevInitError,
    initialize_repository,
    main,
    stage_runtime_secrets,
)

API_IMAGE = "ghcr.io/example/vonk-forge-api@sha256:" + "a" * 64
WORKER_IMAGE = "ghcr.io/example/vonk-forge-worker@sha256:" + "b" * 64


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


def test_initialize_repository_clones_into_an_existing_empty_mountpoint(
    tmp_path: Path,
    local_acceptance: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _origin_path, _publisher, repository_url, expected = _origin(tmp_path)
    destination = tmp_path / "repository"
    destination.mkdir()

    def reject_rmdir(_path: Path) -> None:
        raise AssertionError("the mounted repository root must never be removed")

    monkeypatch.setattr(Path, "rmdir", reject_rmdir)

    initialize_repository(destination, repository_url, expected)

    assert _git(destination, "rev-parse", "main") == expected


def test_all_git_subprocesses_drop_root_credentials_and_use_a_sanitized_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def capture_run(
        command: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="accepted\n")

    monkeypatch.setattr(dev_init.os, "geteuid", lambda: 0)
    monkeypatch.setattr(dev_init.subprocess, "run", capture_run)
    monkeypatch.setenv("HOME", str(tmp_path / "attacker-home"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(tmp_path / "root-command"))

    assert dev_init._git(
        tmp_path,
        ("rev-parse", "HEAD"),
        action="inspect test repository",
        local_origin=True,
    ) == "accepted"
    assert dev_init._is_ancestor(
        tmp_path,
        "a" * 40,
        "b" * 40,
        local_origin=True,
    )

    assert len(calls) == 2
    assert any("rev-parse" in command for command, _kwargs in calls)
    assert any("merge-base" in command for command, _kwargs in calls)
    for _command, kwargs in calls:
        assert kwargs["user"] == 65534
        assert kwargs["group"] == 65534
        assert kwargs["extra_groups"] == ()
        assert kwargs["env"] == {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": "/nonexistent/vonk-control",
            "PATH": os.defpath,
        }


def test_git_subprocesses_do_not_request_a_credential_change_when_not_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []

    def capture_run(
        command: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(dev_init.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(dev_init.subprocess, "run", capture_run)

    dev_init._git(
        tmp_path,
        ("status", "--porcelain=v1"),
        action="inspect test repository",
        local_origin=True,
    )

    assert len(calls) == 1
    assert "user" not in calls[0]
    assert "group" not in calls[0]
    assert "extra_groups" not in calls[0]


def test_existing_repository_transfers_to_git_identity_before_git_and_restores_api(
    tmp_path: Path,
    local_acceptance: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _origin_path, _publisher, repository_url, expected = _origin(tmp_path)
    destination = tmp_path / "repository"
    initialize_repository(destination, repository_url, expected)
    real_run = subprocess.run
    events: list[tuple[object, ...]] = []

    def record_chown(
        path: str | os.PathLike[str], uid: int, gid: int, **_kwargs: object
    ) -> None:
        events.append(("owner", Path(path), uid, gid))

    def record_fchown(descriptor: int, uid: int, gid: int) -> None:
        events.append(("owner", descriptor, uid, gid))

    def run_as_current_user(
        command: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        events.append(
            (
                "git",
                kwargs.pop("user", None),
                kwargs.pop("group", None),
                kwargs.pop("extra_groups", None),
            )
        )
        return real_run(command, **kwargs)

    monkeypatch.setattr(dev_init.os, "geteuid", lambda: 0)
    monkeypatch.setattr(dev_init.os, "chown", record_chown)
    monkeypatch.setattr(dev_init.os, "fchown", record_fchown)
    monkeypatch.setattr(dev_init.subprocess, "run", run_as_current_user)

    initialize_repository(destination, repository_url, expected)

    first_git = next(index for index, event in enumerate(events) if event[0] == "git")
    last_git = max(index for index, event in enumerate(events) if event[0] == "git")
    assert any(
        event[0] == "owner" and event[2:] == (65534, 65534)
        for event in events[:first_git]
    )
    assert all(
        event[1:] == (65534, 65534, ())
        for event in events
        if event[0] == "git"
    )
    assert any(
        event[0] == "owner" and event[2:] == (10001, 10001)
        for event in events[last_git + 1 :]
    )
    assert events[-1][0] == "owner"
    assert events[-1][2:] == (10001, 10001)


def test_repository_ownership_is_restored_to_api_after_git_failure(
    tmp_path: Path,
    local_acceptance: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _origin_path, _publisher, repository_url, expected = _origin(tmp_path)
    destination = tmp_path / "repository"
    initialize_repository(destination, repository_url, expected)
    events: list[tuple[object, ...]] = []

    def record_chown(
        path: str | os.PathLike[str], uid: int, gid: int, **_kwargs: object
    ) -> None:
        events.append(("owner", Path(path), uid, gid))

    def record_fchown(descriptor: int, uid: int, gid: int) -> None:
        events.append(("owner", descriptor, uid, gid))

    def fail_git(
        command: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        events.append(
            (
                "git",
                kwargs.get("user"),
                kwargs.get("group"),
                kwargs.get("extra_groups"),
            )
        )
        return subprocess.CompletedProcess(command, 2, stdout="")

    monkeypatch.setattr(dev_init.os, "geteuid", lambda: 0)
    monkeypatch.setattr(dev_init.os, "chown", record_chown)
    monkeypatch.setattr(dev_init.os, "fchown", record_fchown)
    monkeypatch.setattr(dev_init.subprocess, "run", fail_git)

    with pytest.raises(DevInitError, match="Git could not"):
        initialize_repository(destination, repository_url, expected)

    first_git = next(index for index, event in enumerate(events) if event[0] == "git")
    assert any(
        event[0] == "owner" and event[2:] == (65534, 65534)
        for event in events[:first_git]
    )
    assert events[first_git][1:] == (65534, 65534, ())
    assert events[-1][0] == "owner"
    assert events[-1][2:] == (10001, 10001)


@pytest.mark.parametrize("root_last", (False, True))
def test_repository_ownership_walk_rejects_a_listed_directory_swapped_for_external_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_last: bool,
) -> None:
    repository = tmp_path / "repository"
    mutable = repository / "mutable"
    mutable.mkdir(parents=True)
    (mutable / "repository-file").write_text("repository\n", encoding="utf-8")
    external = tmp_path / "external-secrets"
    external.mkdir()
    external_secret = external / "worker-api-token"
    external_secret.write_text("preserve\n", encoding="utf-8")
    external_secret.chmod(0o400)
    external.chmod(0o550)
    external_before = external.stat()
    secret_before = external_secret.stat()
    ownership_targets: list[tuple[int, int]] = []
    swapped = False
    real_open = os.open
    real_stat = os.stat

    def swap_directory() -> None:
        nonlocal swapped
        if swapped:
            return
        mutable.rename(repository / "detached-mutable")
        mutable.symlink_to(external, target_is_directory=True)
        swapped = True

    def race_walk(
        _root: Path,
        *,
        topdown: bool,
        followlinks: bool,
    ) -> object:
        assert followlinks is False
        if topdown:
            yield repository, ["mutable"], []
            swap_directory()
            yield mutable, [], [external_secret.name]
        else:
            swap_directory()
            yield mutable, [], [external_secret.name]
            yield repository, ["mutable"], []

    def race_open(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == "mutable" and dir_fd is not None:
            swap_directory()
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def record_chown(
        path: str | os.PathLike[str],
        _uid: int,
        _gid: int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        metadata = real_stat(
            path,
            dir_fd=dir_fd,
            follow_symlinks=follow_symlinks,
        )
        ownership_targets.append((metadata.st_dev, metadata.st_ino))

    def record_fchown(descriptor: int, _uid: int, _gid: int) -> None:
        metadata = os.fstat(descriptor)
        ownership_targets.append((metadata.st_dev, metadata.st_ino))

    monkeypatch.setattr(dev_init.os, "geteuid", lambda: 0)
    monkeypatch.setattr(dev_init.os, "walk", race_walk)
    monkeypatch.setattr(dev_init.os, "open", race_open)
    monkeypatch.setattr(dev_init.os, "chown", record_chown)
    monkeypatch.setattr(dev_init.os, "fchown", record_fchown)

    with pytest.raises(DevInitError, match="ownership"):
        dev_init._chown_repository_tree(
            repository,
            65534 if not root_last else 10001,
            65534 if not root_last else 10001,
            root_last=root_last,
        )

    assert swapped
    assert (secret_before.st_dev, secret_before.st_ino) not in ownership_targets
    assert external_secret.read_text(encoding="utf-8") == "preserve\n"
    external_after = external.stat()
    secret_after = external_secret.stat()
    assert (external_after.st_uid, external_after.st_gid, stat.S_IMODE(external_after.st_mode)) == (
        external_before.st_uid,
        external_before.st_gid,
        stat.S_IMODE(external_before.st_mode),
    )
    assert (secret_after.st_uid, secret_after.st_gid, stat.S_IMODE(secret_after.st_mode)) == (
        secret_before.st_uid,
        secret_before.st_gid,
        stat.S_IMODE(secret_before.st_mode),
    )


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
    migrate_root = tmp_path / "migrate"
    worker_root = tmp_path / "worker"

    stage_runtime_secrets(source, api_root, migrate_root, worker_root)

    assert {path.name for path in api_root.iterdir()} == {
        "admin-grant-private-key",
        "database-url",
        "git-signing-key",
    }
    assert {path.name for path in worker_root.iterdir()} == {
        "database-url",
        "worker-api-token",
    }
    assert {path.name for path in migrate_root.iterdir()} == {"database-url"}
    assert (migrate_root / "database-url").read_bytes() == (
        b"postgresql://vonk:secret@postgres/vonk\n"
    )
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
    assert not (migrate_root / "git-signing-key").exists()
    assert not (migrate_root / "admin-grant-private-key").exists()
    assert not (migrate_root / "worker-api-token").exists()
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
        stage_runtime_secrets(
            source, tmp_path / "api", tmp_path / "migrate", tmp_path / "worker"
        )

    source = _secret_source(tmp_path / "safe-source")
    actual_api = tmp_path / "actual-api"
    actual_api.mkdir()
    api_root = tmp_path / "api"
    api_root.symlink_to(actual_api, target_is_directory=True)
    with pytest.raises(DevInitError, match="projection"):
        stage_runtime_secrets(source, api_root, tmp_path / "migrate", tmp_path / "worker")


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

    for api_root, migrate_root, worker_root in (
        (shared, alias_parent / "shared", tmp_path / "worker"),
        (shared, tmp_path / "migrate", alias_parent / "shared"),
        (tmp_path / "api", shared, alias_parent / "shared"),
    ):
        with pytest.raises(DevInitError, match="projection"):
            stage_runtime_secrets(source, api_root, migrate_root, worker_root)

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
        stage_runtime_secrets(
            source, Path("api"), tmp_path / "migrate", tmp_path / "worker"
        )

    assert not (tmp_path / "api").exists()
    assert not (tmp_path / "migrate").exists()
    assert not (tmp_path / "worker").exists()


def test_stage_runtime_secrets_rejects_symlinked_parent_components(
    tmp_path: Path,
) -> None:
    source = _secret_source(tmp_path / "source")
    actual_parent = tmp_path / "actual-parent"
    actual_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(actual_parent, target_is_directory=True)

    for api_root, migrate_root, worker_root in (
        (linked_parent / "api", tmp_path / "migrate", tmp_path / "worker"),
        (tmp_path / "api", linked_parent / "migrate", tmp_path / "worker"),
        (tmp_path / "api", tmp_path / "migrate", linked_parent / "worker"),
    ):
        with pytest.raises(DevInitError, match="projection"):
            stage_runtime_secrets(source, api_root, migrate_root, worker_root)

    assert not any(actual_parent.iterdir())


def test_stage_runtime_secrets_preserves_generated_credentials_and_refreshes_inputs(
    tmp_path: Path,
) -> None:
    source = _secret_source(tmp_path / "source")
    api_root = tmp_path / "api"
    migrate_root = tmp_path / "migrate"
    worker_root = tmp_path / "worker"
    stage_runtime_secrets(source, api_root, migrate_root, worker_root)
    admin_key = (api_root / "admin-grant-private-key").read_bytes()
    worker_token = (worker_root / "worker-api-token").read_bytes()
    (source / "database-url").write_bytes(
        b"postgresql://vonk:replacement@postgres/vonk\n"
    )
    (source / "git-signing-key").write_bytes(b"replacement-signing-key\n")
    api_root.chmod(0o700)
    migrate_root.chmod(0o700)
    worker_root.chmod(0o700)
    (api_root / "unexpected-api-authority").write_bytes(b"remove\n")
    (migrate_root / "unexpected-migrate-authority").write_bytes(b"remove\n")
    (worker_root / "unexpected-worker-authority").write_bytes(b"remove\n")

    stage_runtime_secrets(source, api_root, migrate_root, worker_root)

    assert (api_root / "admin-grant-private-key").read_bytes() == admin_key
    assert (worker_root / "worker-api-token").read_bytes() == worker_token
    assert (api_root / "database-url").read_bytes() == (
        b"postgresql://vonk:replacement@postgres/vonk\n"
    )
    assert (worker_root / "database-url").read_bytes() == (
        b"postgresql://vonk:replacement@postgres/vonk\n"
    )
    assert (migrate_root / "database-url").read_bytes() == (
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
    assert {path.name for path in migrate_root.iterdir()} == {"database-url"}


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
    migrate_root = tmp_path / "migrate"
    worker_root = tmp_path / "worker"
    stage_runtime_secrets(source, api_root, migrate_root, worker_root)
    root = api_root if projection == "api" else worker_root
    root.chmod(0o700)
    target = root / name
    target.unlink()
    target.write_bytes(malformed)
    target.chmod(0o400)

    with pytest.raises(DevInitError, match="generated credential"):
        stage_runtime_secrets(source, api_root, migrate_root, worker_root)

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
    migrate_root = tmp_path / "migrate"
    worker_root = tmp_path / "worker"
    stage_runtime_secrets(source, api_root, migrate_root, worker_root)
    root = api_root if projection == "api" else worker_root
    root.chmod(0o700)
    target = root / name
    target.unlink()
    outside = tmp_path / f"outside-{name}"
    outside.write_bytes(b"must-not-be-read-or-replaced\n")
    target.symlink_to(outside)

    with pytest.raises(DevInitError, match="generated credential"):
        stage_runtime_secrets(source, api_root, migrate_root, worker_root)

    assert target.is_symlink()
    assert outside.read_bytes() == b"must-not-be-read-or-replaced\n"


def test_main_initializes_repository_synthetic_state_and_runtime_secrets(
    tmp_path: Path, local_acceptance: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _origin_path, _publisher, repository_url, expected = _origin(tmp_path)
    source = _secret_source(tmp_path / "source")
    repository = tmp_path / "repository"
    api_root = tmp_path / "api"
    migrate_root = tmp_path / "migrate"
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
    monkeypatch.setenv("VONK_DEV_MIGRATE_SECRET_ROOT", str(migrate_root))
    monkeypatch.setenv("VONK_DEV_WORKER_SECRET_ROOT", str(worker_root))
    monkeypatch.setenv("VONK_DEV_API_IMAGE", API_IMAGE)
    monkeypatch.setenv("VONK_DEV_WORKER_IMAGE", WORKER_IMAGE)
    monkeypatch.setenv("VONK_CONTROL_IDENTITY_ROOT", str(identity))
    monkeypatch.setenv("VONK_STATE_PATH", str(state))
    monkeypatch.setenv("VONK_ROUTE_ROOT", str(routes))
    monkeypatch.setenv("VONK_SUPERVISOR_ROOT", str(supervisor))

    assert main() == 0
    assert _git(repository, "rev-parse", "main") == expected
    projection = json.loads((identity / "active.json").read_text(encoding="ascii"))
    assert projection["projection_kind"] == "active"
    generation = projection["selection"]["generation"]
    assert generation["api_image"] == API_IMAGE
    assert generation["worker_image"] == WORKER_IMAGE
    assert (api_root / "admin-grant-private-key").is_file()
    assert (migrate_root / "database-url").is_file()
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
    monkeypatch.setenv("VONK_DEV_WORKER_SECRET_ROOT", str(tmp_path / "worker"))
    monkeypatch.delenv("VONK_DEV_MIGRATE_SECRET_ROOT", raising=False)

    with pytest.raises(DevInitError, match="VONK_DEV_MIGRATE_SECRET_ROOT is required"):
        main()

    assert not repository.exists()


def test_main_rejects_an_unpinned_process_image_before_cloning(
    tmp_path: Path, local_acceptance: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _origin_path, _publisher, repository_url, expected = _origin(tmp_path)
    repository = tmp_path / "repository"
    monkeypatch.setenv("VONK_DEV_EXPECTED_COMMIT", expected)
    monkeypatch.setenv("VONK_DEV_REPOSITORY_URL", repository_url)
    monkeypatch.setenv("VONK_REPOSITORY_PATH", str(repository))
    monkeypatch.setenv("VONK_DEV_SECRET_SOURCE_ROOT", str(tmp_path / "source"))
    monkeypatch.setenv("VONK_DEV_API_SECRET_ROOT", str(tmp_path / "api"))
    monkeypatch.setenv("VONK_DEV_MIGRATE_SECRET_ROOT", str(tmp_path / "migrate"))
    monkeypatch.setenv("VONK_DEV_WORKER_SECRET_ROOT", str(tmp_path / "worker"))
    monkeypatch.setenv("VONK_DEV_API_IMAGE", "vonk-forge-api:dev-local")
    monkeypatch.setenv("VONK_DEV_WORKER_IMAGE", WORKER_IMAGE)

    with pytest.raises(DevInitError, match="VONK_DEV_API_IMAGE is invalid"):
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

    dev_init._write_active_projection(identity, API_IMAGE, WORKER_IMAGE)

    raw = (identity / "active.json").read_bytes()
    assert raw.endswith(b"\n")
    assert json.loads(raw)["projection_kind"] == "active"
    assert stat.S_IMODE((identity / "active.json").stat().st_mode) == 0o444
