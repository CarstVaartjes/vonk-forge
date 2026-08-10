from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
from pathlib import Path

import pytest
from vonk_control import dev_cohort, dev_init
from vonk_control.dev_cohort import build_identity, verify_cohort
from vonk_control.dev_init import (
    DevInitError,
    initialize_repository,
    main,
    stage_runtime_secrets,
)

API_IMAGE = "ghcr.io/example/vonk-forge-api@sha256:" + "a" * 64
WORKER_IMAGE = "ghcr.io/example/vonk-forge-worker@sha256:" + "b" * 64


def _selected_cohort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    selected_commit: str,
    embedded_commit: str | None = None,
    embedded_role: str = "api",
):
    selected = verify_cohort(
        [
            build_identity(role="api", source_commit=selected_commit),
            build_identity(role="worker", source_commit=selected_commit),
        ]
    )
    selected_path = tmp_path / "cohort" / "selected.json"
    selected_path.parent.mkdir()
    selected_path.write_bytes(selected.to_bytes())
    identity_path = tmp_path / "development-image-identity.json"
    identity_path.write_bytes(
        build_identity(
            role=embedded_role,
            source_commit=embedded_commit or selected_commit,
        ).to_bytes()
    )
    monkeypatch.setattr(
        dev_cohort,
        "DEVELOPMENT_IMAGE_IDENTITY_PATH",
        identity_path,
    )
    return selected_path, selected


def _set_main_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    repository_url: str,
) -> dict[str, Path]:
    paths = {
        "repository": tmp_path / "repository",
        "source": tmp_path / "source",
        "api": tmp_path / "api",
        "migrate": tmp_path / "migrate",
        "worker": tmp_path / "worker",
        "caddy": tmp_path / "caddy",
        "litellm": tmp_path / "litellm",
        "runtime_config": tmp_path / "runtime-config",
        "identity": tmp_path / "identity",
        "state": tmp_path / "state",
        "routes": tmp_path / "routes",
        "supervisor": tmp_path / "supervisor",
    }
    for name, value in (
        ("VONK_DEV_REPOSITORY_URL", repository_url),
        ("VONK_REPOSITORY_PATH", str(paths["repository"])),
        ("VONK_DEV_SECRET_SOURCE_ROOT", str(paths["source"])),
        ("VONK_DEV_API_SECRET_ROOT", str(paths["api"])),
        ("VONK_DEV_MIGRATE_SECRET_ROOT", str(paths["migrate"])),
        ("VONK_DEV_WORKER_SECRET_ROOT", str(paths["worker"])),
        ("VONK_DEV_CADDY_SECRET_ROOT", str(paths["caddy"])),
        ("VONK_DEV_LITELLM_SECRET_ROOT", str(paths["litellm"])),
        ("VONK_DEV_RUNTIME_CONFIG_ROOT", str(paths["runtime_config"])),
        ("VONK_CONTROL_IDENTITY_ROOT", str(paths["identity"])),
        ("VONK_STATE_PATH", str(paths["state"])),
        ("VONK_ROUTE_ROOT", str(paths["routes"])),
        ("VONK_SUPERVISOR_ROOT", str(paths["supervisor"])),
    ):
        monkeypatch.setenv(name, value)
    return paths


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


def test_initialize_repository_clones_accepted_main_and_checks_out_deploy(
    tmp_path: Path, local_acceptance: None
) -> None:
    _origin_path, _publisher, repository_url, expected = _origin(tmp_path)
    destination = tmp_path / "repository"

    initialize_repository(destination, repository_url, expected)

    assert _git(destination, "status", "--porcelain=v1", "--untracked-files=all") == ""
    assert _git(destination, "symbolic-ref", "--short", "HEAD") == "deploy"
    assert _git(destination, "rev-parse", "main") == expected
    assert _git(destination, "rev-parse", "deploy") == expected
    assert _git(destination, "rev-parse", "refs/vonk/deploy-base") == expected
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
    assert _git(destination, "rev-parse", "deploy") == expected
    assert _git(destination, "rev-parse", "refs/vonk/deploy-base") == expected
    assert _git(destination, "symbolic-ref", "--short", "HEAD") == "deploy"


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


def test_initialize_repository_fast_forwards_accepted_and_deploy_refs_together(
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
    assert _git(destination, "rev-parse", "deploy") == expected
    assert _git(destination, "rev-parse", "refs/vonk/deploy-base") == expected
    assert _git(destination, "symbolic-ref", "--short", "HEAD") == "deploy"
    assert _git(destination, "rev-parse", "operator-notes") == initial
    assert _git(destination, "status", "--porcelain=v1", "--untracked-files=all") == ""


def test_initialize_repository_fails_closed_after_reset_interrupt_and_allows_verified_recovery(
    tmp_path: Path, local_acceptance: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _origin_path, publisher, repository_url, initial = _origin(tmp_path)
    destination = tmp_path / "repository"
    initialize_repository(destination, repository_url, initial)
    _git(destination, "branch", "operator-notes", initial)
    expected = _commit(publisher, "new.txt", "next\n")
    _git(publisher, "push", "-q", "origin", "main")
    real_git = dev_init._git

    def interrupt_reset(
        root: Path | None,
        arguments: tuple[str, ...],
        **kwargs: object,
    ) -> str:
        if arguments == ("reset", "--hard", expected):
            raise DevInitError("injected reset interruption")
        return real_git(root, arguments, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(dev_init, "_git", interrupt_reset)
    with pytest.raises(DevInitError, match="injected reset interruption"):
        initialize_repository(destination, repository_url, expected)

    assert _git(destination, "rev-parse", "main") == expected
    assert _git(destination, "rev-parse", "deploy") == expected
    assert _git(destination, "rev-parse", "refs/vonk/deploy-base") == expected
    assert _git(destination, "rev-parse", "operator-notes") == initial
    assert _git(destination, "status", "--porcelain=v1", "--untracked-files=all")

    monkeypatch.setattr(dev_init, "_git", real_git)
    with pytest.raises(DevInitError, match="worktree must be clean"):
        initialize_repository(destination, repository_url, expected)

    _git(destination, "reset", "--hard", expected)
    assert _git(destination, "status", "--porcelain=v1", "--untracked-files=all") == ""
    initialize_repository(destination, repository_url, expected)
    assert _git(destination, "rev-parse", "operator-notes") == initial


def test_initialize_repository_preserves_clean_local_deploy_commits_across_acceptance(
    tmp_path: Path, local_acceptance: None
) -> None:
    _origin_path, publisher, repository_url, accepted_a = _origin(tmp_path)
    destination = tmp_path / "repository"
    initialize_repository(destination, repository_url, accepted_a)
    _git(destination, "config", "user.email", "operator@example.invalid")
    _git(destination, "config", "user.name", "Operator")
    deployed_local = _commit(destination, "local.txt", "signed NAS-local change\n")

    initialize_repository(destination, repository_url, accepted_a)

    assert _git(destination, "rev-parse", "main") == accepted_a
    assert _git(destination, "rev-parse", "deploy") == deployed_local
    assert _git(destination, "rev-parse", "refs/vonk/deploy-base") == accepted_a
    accepted_b = _commit(publisher, "remote.txt", "next accepted commit\n")
    _git(publisher, "push", "-q", "origin", "main")

    initialize_repository(destination, repository_url, accepted_b)

    assert _git(destination, "rev-parse", "main") == accepted_b
    assert _git(destination, "rev-parse", "deploy") == deployed_local
    assert _git(destination, "rev-parse", "refs/vonk/deploy-base") == accepted_a
    assert _git(destination, "symbolic-ref", "--short", "HEAD") == "deploy"
    assert (destination / "local.txt").read_text(encoding="utf-8") == (
        "signed NAS-local change\n"
    )
    assert _git(destination, "status", "--porcelain=v1", "--untracked-files=all") == ""

    initialize_repository(destination, repository_url, accepted_b)

    assert _git(destination, "rev-parse", "main") == accepted_b
    assert _git(destination, "rev-parse", "deploy") == deployed_local
    assert _git(destination, "rev-parse", "refs/vonk/deploy-base") == accepted_a
    accepted_c = _commit(publisher, "later.txt", "later accepted commit\n")
    _git(publisher, "push", "-q", "origin", "main")

    initialize_repository(destination, repository_url, accepted_c)

    assert _git(destination, "rev-parse", "main") == accepted_c
    assert _git(destination, "rev-parse", "deploy") == deployed_local
    assert _git(destination, "rev-parse", "refs/vonk/deploy-base") == accepted_a
    assert _git(destination, "symbolic-ref", "--short", "HEAD") == "deploy"
    assert (destination / "local.txt").read_text(encoding="utf-8") == (
        "signed NAS-local change\n"
    )
    assert _git(destination, "status", "--porcelain=v1", "--untracked-files=all") == ""


def test_initialize_repository_rejects_changed_origin_dirty_and_divergent_accepted_main(
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
    divergent = _commit(destination, "local.txt", "local\n")
    _git(destination, "update-ref", "refs/heads/main", divergent)
    with pytest.raises(DevInitError, match="accepted baseline is divergent"):
        initialize_repository(destination, repository_url, expected)


def test_initialize_repository_rejects_deploy_that_does_not_descend_from_deployment_base(
    tmp_path: Path, local_acceptance: None
) -> None:
    _origin_path, publisher, repository_url, accepted = _origin(tmp_path)
    destination = tmp_path / "repository"
    initialize_repository(destination, repository_url, accepted)
    expected = _commit(publisher, "remote.txt", "remote\n")
    _git(publisher, "push", "-q", "origin", "main")
    _git(destination, "config", "user.email", "operator@example.invalid")
    _git(destination, "config", "user.name", "Operator")
    unrelated = _git(
        destination,
        "commit-tree",
        f"{accepted}^{{tree}}",
        "-m",
        "unrelated deployment history",
    )
    _git(destination, "update-ref", "refs/heads/deploy", unrelated)

    with pytest.raises(DevInitError, match="deployment branch does not descend"):
        initialize_repository(destination, repository_url, expected)


def test_initialize_repository_rejects_a_tampered_deployment_base(
    tmp_path: Path, local_acceptance: None
) -> None:
    _origin_path, publisher, repository_url, accepted = _origin(tmp_path)
    destination = tmp_path / "repository"
    initialize_repository(destination, repository_url, accepted)
    expected = _commit(publisher, "remote.txt", "remote\n")
    _git(publisher, "push", "-q", "origin", "main")
    _git(destination, "config", "user.email", "operator@example.invalid")
    _git(destination, "config", "user.name", "Operator")
    unrelated = _git(
        destination,
        "commit-tree",
        f"{accepted}^{{tree}}",
        "-m",
        "tampered deployment base",
    )
    _git(destination, "update-ref", "refs/vonk/deploy-base", unrelated)

    with pytest.raises(DevInitError, match="deployment base"):
        initialize_repository(destination, repository_url, expected)


def test_initialize_repository_rejects_deployment_base_older_than_merge_base(
    tmp_path: Path, local_acceptance: None
) -> None:
    _origin_path, publisher, repository_url, older = _origin(tmp_path)
    accepted = _commit(publisher, "accepted.txt", "accepted baseline\n")
    _git(publisher, "push", "-q", "origin", "main")
    destination = tmp_path / "repository"
    initialize_repository(destination, repository_url, accepted)
    _git(destination, "config", "user.email", "operator@example.invalid")
    _git(destination, "config", "user.name", "Operator")
    _commit(destination, "local.txt", "local deployment commit\n")
    _git(destination, "update-ref", "refs/vonk/deploy-base", older, accepted)

    with pytest.raises(DevInitError, match="deployment base.*merge-base"):
        initialize_repository(destination, repository_url, accepted)


def test_initialize_repository_rejects_a_rollback_commit(
    tmp_path: Path, local_acceptance: None
) -> None:
    _origin_path, publisher, repository_url, initial = _origin(tmp_path)
    destination = tmp_path / "repository"
    initialize_repository(destination, repository_url, initial)
    expected = _commit(publisher, "remote.txt", "remote\n")
    _git(publisher, "push", "-q", "origin", "main")
    initialize_repository(destination, repository_url, expected)

    with pytest.raises(DevInitError, match="accepted baseline is divergent"):
        initialize_repository(destination, repository_url, initial)


def _secret_source(root: Path) -> Path:
    root.mkdir()
    contents = {
        "database-url": b"postgresql://vonk:secret@postgres/vonk\n",
        "git-signing-key": b"-----BEGIN OPENSSH PRIVATE KEY-----\nkey\n",
        "agent-ca-certificate": b"agent-ca-certificate-sentinel\n",
        "agent-ca-key": b"agent-ca-private-key-sentinel\n",
        "agent-proxy-auth": b"agent-proxy-auth-sentinel-000000000000\n",
        "controller-ca": b"controller-ca-public-sentinel\n",
        "controller-server-certificate": b"controller-server-certificate-sentinel\n",
        "controller-server-key": b"controller-server-private-key-sentinel\n",
        "litellm-master-key": b"litellm-master-key-sentinel\n",
        "litellm-upstream-key": b"litellm-upstream-key-sentinel\n",
        "management-cidrs": b"192.0.2.0/24\n2001:db8::/64\n",
    }
    for name, content in contents.items():
        (root / name).write_bytes(content)
    return root


def _visible_bytes(root: Path) -> set[bytes]:
    return {path.read_bytes() for path in root.iterdir() if path.is_file()}


def test_stage_runtime_secrets_projects_exact_disjoint_service_authority(
    tmp_path: Path,
) -> None:
    source = _secret_source(tmp_path / "source")
    (source / "database-url").write_bytes(
        b"postgresql+psycopg://vonk:database-secret@postgres/vonk?sslmode=disable\n"
    )
    (source / "git-signing-key").write_bytes(b"git-signing-private-sentinel\n")
    roots = {
        name: tmp_path / name
        for name in ("api", "migrate", "worker", "caddy", "litellm")
    }

    stage_runtime_secrets(
        source,
        roots["api"],
        roots["migrate"],
        roots["worker"],
        roots["caddy"],
        roots["litellm"],
    )

    assert {path.name for path in roots["api"].iterdir()} == {
        "admin-grant-private-key",
        "agent-ca-certificate",
        "agent-ca-key",
        "agent-proxy-auth",
        "database-url",
        "git-signing-key",
        "worker-api-token",
    }
    assert {path.name for path in roots["worker"].iterdir()} == {
        "database-url",
        "worker-api-token",
    }
    assert {path.name for path in roots["migrate"].iterdir()} == {"database-url"}
    assert {path.name for path in roots["caddy"].iterdir()} == {
        "agent-ca-certificate",
        "agent-proxy-auth",
        "controller-server-certificate",
        "controller-server-key",
        "management-cidrs",
    }
    assert {path.name for path in roots["litellm"].iterdir()} == {
        "litellm-database-url",
        "litellm-master-key",
        "litellm-upstream-key",
    }
    assert (roots["api"] / "worker-api-token").read_bytes() == (
        roots["worker"] / "worker-api-token"
    ).read_bytes()
    assert (roots["litellm"] / "litellm-database-url").read_bytes() == (
        b"postgresql://vonk:database-secret@postgres/vonk?sslmode=disable\n"
    )
    for root in roots.values():
        assert stat.S_IMODE(root.stat().st_mode) == 0o550
        assert all(
            stat.S_IMODE(path.stat().st_mode) == 0o400
            for path in root.iterdir()
        )

    visible = {name: _visible_bytes(root) for name, root in roots.items()}
    api_private_sentinels = {
        b"git-signing-private-sentinel\n",
        b"agent-ca-private-key-sentinel\n",
    }
    assert not api_private_sentinels & visible["worker"]
    assert not api_private_sentinels & visible["migrate"]
    assert not api_private_sentinels & visible["caddy"]
    assert not api_private_sentinels & visible["litellm"]
    assert b"agent-ca-private-key-sentinel\n" not in visible["caddy"]
    assert b"controller-server-private-key-sentinel\n" not in visible["api"]
    assert b"litellm-master-key-sentinel\n" not in visible["api"]
    assert b"litellm-upstream-key-sentinel\n" not in visible["worker"]


def test_stage_runtime_secrets_assigns_exact_service_owners_when_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _secret_source(tmp_path / "source")
    roots = {
        name: tmp_path / name
        for name in ("api", "migrate", "worker", "caddy", "litellm")
    }
    expected_names = {
        "admin-grant-private-key",
        "agent-ca-certificate",
        "agent-ca-key",
        "agent-proxy-auth",
        "controller-server-certificate",
        "controller-server-key",
        "database-url",
        "git-signing-key",
        "litellm-database-url",
        "litellm-master-key",
        "litellm-upstream-key",
        "management-cidrs",
        "worker-api-token",
    }
    owners: dict[tuple[str, str], tuple[int, int]] = {}

    def record_owner(descriptor: int, uid: int, gid: int) -> None:
        path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        if not path.name.startswith("."):
            return
        projected_name = next(
            (
                name
                for name in expected_names
                if path.name.startswith(f".{name}.")
            ),
            None,
        )
        if projected_name is not None:
            owners[(path.parent.name, projected_name)] = (uid, gid)

    monkeypatch.setattr(dev_init.os, "geteuid", lambda: 0)
    monkeypatch.setattr(dev_init.os, "fchown", record_owner)

    stage_runtime_secrets(
        source,
        roots["api"],
        roots["migrate"],
        roots["worker"],
        roots["caddy"],
        roots["litellm"],
    )

    assert set(owners.values()) == {
        (10000, 10000),
        (10001, 10001),
        (10002, 10001),
    }
    assert all(
        owner == (10000, 10000)
        for (root, _name), owner in owners.items()
        if root == "caddy"
    )
    assert all(
        owner == (10002, 10001)
        for (root, _name), owner in owners.items()
        if root == "litellm"
    )
    assert all(
        owner == (10001, 10001)
        for (root, _name), owner in owners.items()
        if root in {"api", "migrate", "worker"}
    )


def test_stage_runtime_secrets_creates_disjoint_service_projections(tmp_path: Path) -> None:
    source = _secret_source(tmp_path / "source")
    api_root = tmp_path / "api"
    migrate_root = tmp_path / "migrate"
    worker_root = tmp_path / "worker"
    caddy_root = tmp_path / "caddy"
    litellm_root = tmp_path / "litellm"

    stage_runtime_secrets(
        source,
        api_root,
        migrate_root,
        worker_root,
        caddy_root,
        litellm_root,
    )

    assert {path.name for path in api_root.iterdir()} == {
        "admin-grant-private-key",
        "agent-ca-certificate",
        "agent-ca-key",
        "agent-proxy-auth",
        "database-url",
        "git-signing-key",
        "worker-api-token",
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
    assert (api_root / "worker-api-token").read_bytes() == (
        worker_root / "worker-api-token"
    ).read_bytes()
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
            source,
            tmp_path / "api",
            tmp_path / "migrate",
            tmp_path / "worker",
            tmp_path / "caddy",
            tmp_path / "litellm",
        )

    source = _secret_source(tmp_path / "safe-source")
    actual_api = tmp_path / "actual-api"
    actual_api.mkdir()
    api_root = tmp_path / "api"
    api_root.symlink_to(actual_api, target_is_directory=True)
    with pytest.raises(DevInitError, match="projection"):
        stage_runtime_secrets(
            source,
            api_root,
            tmp_path / "migrate",
            tmp_path / "worker",
            tmp_path / "caddy",
            tmp_path / "litellm",
        )


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
            stage_runtime_secrets(
                source,
                api_root,
                migrate_root,
                worker_root,
                tmp_path / "caddy",
                tmp_path / "litellm",
            )

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
            source,
            Path("api"),
            tmp_path / "migrate",
            tmp_path / "worker",
            tmp_path / "caddy",
            tmp_path / "litellm",
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
            stage_runtime_secrets(
                source,
                api_root,
                migrate_root,
                worker_root,
                tmp_path / "caddy",
                tmp_path / "litellm",
            )

    assert not any(actual_parent.iterdir())


def test_stage_runtime_secrets_preserves_generated_credentials_and_refreshes_inputs(
    tmp_path: Path,
) -> None:
    source = _secret_source(tmp_path / "source")
    api_root = tmp_path / "api"
    migrate_root = tmp_path / "migrate"
    worker_root = tmp_path / "worker"
    caddy_root = tmp_path / "caddy"
    litellm_root = tmp_path / "litellm"
    stage_runtime_secrets(
        source,
        api_root,
        migrate_root,
        worker_root,
        caddy_root,
        litellm_root,
    )
    admin_key = (api_root / "admin-grant-private-key").read_bytes()
    worker_token = (worker_root / "worker-api-token").read_bytes()
    (source / "database-url").write_bytes(
        b"postgresql://vonk:replacement@postgres/vonk\n"
    )
    (source / "git-signing-key").write_bytes(b"replacement-signing-key\n")
    (source / "agent-ca-certificate").write_bytes(b"replacement-agent-ca\n")
    (source / "agent-ca-key").write_bytes(b"replacement-agent-ca-key\n")
    (source / "controller-server-key").write_bytes(b"replacement-server-key\n")
    (source / "litellm-master-key").write_bytes(b"replacement-litellm-key\n")
    stage_runtime_secrets(
        source,
        api_root,
        migrate_root,
        worker_root,
        caddy_root,
        litellm_root,
    )

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
    assert (api_root / "agent-ca-key").read_bytes() == b"replacement-agent-ca-key\n"
    assert (caddy_root / "controller-server-key").read_bytes() == (
        b"replacement-server-key\n"
    )
    assert (litellm_root / "litellm-master-key").read_bytes() == (
        b"replacement-litellm-key\n"
    )
    assert {path.name for path in api_root.iterdir()} == {
        "admin-grant-private-key",
        "agent-ca-certificate",
        "agent-ca-key",
        "agent-proxy-auth",
        "database-url",
        "git-signing-key",
        "worker-api-token",
    }
    assert {path.name for path in worker_root.iterdir()} == {
        "database-url",
        "worker-api-token",
    }
    assert {path.name for path in migrate_root.iterdir()} == {"database-url"}


def test_stage_runtime_secrets_does_not_clear_canonical_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _secret_source(tmp_path / "source")
    roots = [tmp_path / name for name in ("api", "migrate", "worker", "caddy", "litellm")]
    stage_runtime_secrets(source, *roots)
    admin = (roots[0] / "admin-grant-private-key").read_bytes()
    worker = (roots[2] / "worker-api-token").read_bytes()

    def fail_clear(_parent: int) -> None:
        raise AssertionError("projection clearing must not be used")

    monkeypatch.setattr(dev_init, "_clear_projection", fail_clear, raising=False)
    stage_runtime_secrets(source, *roots)

    assert (roots[0] / "admin-grant-private-key").read_bytes() == admin
    assert (roots[2] / "worker-api-token").read_bytes() == worker
    assert (roots[0] / "worker-api-token").read_bytes() == worker


@pytest.mark.parametrize(
    ("operation_name", "boundary"),
    (
        ("write", "write"),
        ("fchown", "chown"),
        ("fsync", "file-fsync"),
        ("fsync", "directory-fsync"),
    ),
)
def test_stage_runtime_secrets_faults_preserve_canonical_credentials_and_token_copies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation_name: str,
    boundary: str,
) -> None:
    source = _secret_source(tmp_path / "source")
    roots = [tmp_path / name for name in ("api", "migrate", "worker", "caddy", "litellm")]
    stage_runtime_secrets(source, *roots)
    admin = (roots[0] / "admin-grant-private-key").read_bytes()
    worker = (roots[2] / "worker-api-token").read_bytes()
    (source / "database-url").write_bytes(
        b"postgresql://vonk:replacement@postgres/vonk\n"
    )
    operation = getattr(dev_init.os, operation_name)
    triggered = False

    def fault(descriptor: int, *args: object, **kwargs: object):
        nonlocal triggered
        try:
            descriptor_path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        except OSError:
            descriptor_path = Path("/")
        selected = (
            boundary == "directory-fsync"
            and descriptor_path == roots[0]
            and not triggered
        ) or (
            boundary != "directory-fsync"
            and descriptor_path.name.startswith(".worker-api-token.")
        )
        if selected:
            triggered = True
            raise OSError("injected projection fault")
        return operation(descriptor, *args, **kwargs)

    monkeypatch.setattr(dev_init.os, operation_name, fault)
    with pytest.raises(DevInitError, match="cannot be staged"):
        stage_runtime_secrets(source, *roots)

    assert (roots[0] / "admin-grant-private-key").read_bytes() == admin
    assert (roots[2] / "worker-api-token").read_bytes() == worker
    assert (roots[0] / "worker-api-token").read_bytes() == worker
    assert all(stat.S_IMODE(root.stat().st_mode) == 0o550 for root in roots)


@pytest.mark.parametrize(
    "unsafe_kind",
    ("symlink", "fifo", "socket", "hardlink", "directory", "unknown"),
)
def test_stage_runtime_secrets_rejects_unsafe_entries_without_mutation(
    tmp_path: Path, unsafe_kind: str
) -> None:
    source = _secret_source(tmp_path / "source")
    roots = [tmp_path / name for name in ("api", "migrate", "worker", "caddy", "litellm")]
    stage_runtime_secrets(source, *roots)
    api = roots[0]
    canonical = api / "admin-grant-private-key"
    original = canonical.read_bytes()
    api.chmod(0o700)
    target = api / ("unknown-authority" if unsafe_kind == "unknown" else "database-url")
    if unsafe_kind != "unknown":
        target.unlink()
    outside = tmp_path / "outside"
    bound_socket: socket.socket | None = None
    if unsafe_kind == "symlink":
        outside.write_bytes(b"preserve\n")
        target.symlink_to(outside)
    elif unsafe_kind == "fifo":
        os.mkfifo(target)
    elif unsafe_kind == "socket":
        bound_socket = socket.socket(socket.AF_UNIX)
        bound_socket.bind(str(target))
    elif unsafe_kind == "hardlink":
        outside.write_bytes(b"preserve\n")
        os.link(outside, target)
    elif unsafe_kind == "directory":
        target.mkdir()
    else:
        target.write_bytes(b"preserve\n")
    api.chmod(0o550)

    try:
        with pytest.raises(DevInitError, match="projection"):
            stage_runtime_secrets(source, *roots)
    finally:
        if bound_socket is not None:
            bound_socket.close()

    assert canonical.read_bytes() == original
    assert target.exists() or target.is_symlink()
    assert stat.S_IMODE(api.stat().st_mode) == 0o550
    if outside.exists():
        assert outside.read_bytes() == b"preserve\n"


def test_stage_runtime_secrets_rejects_wrong_root_mode_without_mutation(
    tmp_path: Path,
) -> None:
    source = _secret_source(tmp_path / "source")
    roots = [tmp_path / name for name in ("api", "migrate", "worker", "caddy", "litellm")]
    stage_runtime_secrets(source, *roots)
    canonical = roots[0] / "admin-grant-private-key"
    original = canonical.read_bytes()
    roots[0].chmod(0o700)

    with pytest.raises(DevInitError, match="projection"):
        stage_runtime_secrets(source, *roots)

    assert canonical.read_bytes() == original
    assert stat.S_IMODE(roots[0].stat().st_mode) == 0o700


def test_stage_runtime_secrets_preflights_existing_roots_before_creating_missing_roots(
    tmp_path: Path,
) -> None:
    source = _secret_source(tmp_path / "source")
    roots = [tmp_path / name for name in ("api", "migrate", "worker", "caddy", "litellm")]
    roots[1].mkdir(mode=0o700)

    with pytest.raises(DevInitError, match="migration projection root"):
        stage_runtime_secrets(source, *roots)

    assert not roots[0].exists()
    assert not roots[2].exists()
    assert stat.S_IMODE(roots[1].stat().st_mode) == 0o700


def test_stage_runtime_secrets_rejects_wrong_root_owner_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _secret_source(tmp_path / "source")
    roots = [tmp_path / name for name in ("api", "migrate", "worker", "caddy", "litellm")]
    stage_runtime_secrets(source, *roots)
    canonical = roots[0] / "admin-grant-private-key"
    original = canonical.read_bytes()
    real_fstat = dev_init.os.fstat

    def wrong_owner(descriptor: int) -> os.stat_result:
        metadata = real_fstat(descriptor)
        try:
            selected = Path(os.readlink(f"/proc/self/fd/{descriptor}")) == roots[0]
        except OSError:
            selected = False
        if not selected:
            return metadata
        values = list(metadata)
        values[4] = metadata.st_uid + 1
        return os.stat_result(values)

    monkeypatch.setattr(dev_init.os, "fstat", wrong_owner)
    with pytest.raises(DevInitError, match="projection"):
        stage_runtime_secrets(source, *roots)

    assert canonical.read_bytes() == original
    assert stat.S_IMODE(roots[0].stat().st_mode) == 0o550


@pytest.mark.parametrize("metadata_fault", ("owner", "mode"))
def test_stage_runtime_secrets_rejects_wrong_entry_metadata_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, metadata_fault: str
) -> None:
    source = _secret_source(tmp_path / "source")
    roots = [tmp_path / name for name in ("api", "migrate", "worker", "caddy", "litellm")]
    stage_runtime_secrets(source, *roots)
    api = roots[0]
    target = api / "database-url"
    canonical = api / "admin-grant-private-key"
    original = canonical.read_bytes()
    if metadata_fault == "mode":
        target.chmod(0o600)
    else:
        real_stat = dev_init.os.stat

        def wrong_owner(
            path: str | os.PathLike[str] | int,
            *,
            dir_fd: int | None = None,
            follow_symlinks: bool = True,
        ) -> os.stat_result:
            metadata = real_stat(
                path,
                dir_fd=dir_fd,
                follow_symlinks=follow_symlinks,
            )
            if path != "database-url" or dir_fd is None:
                return metadata
            try:
                selected = Path(os.readlink(f"/proc/self/fd/{dir_fd}")) == api
            except OSError:
                selected = False
            if not selected:
                return metadata
            values = list(metadata)
            values[4] = metadata.st_uid + 1
            return os.stat_result(values)

        monkeypatch.setattr(dev_init.os, "stat", wrong_owner)

    with pytest.raises(DevInitError, match="projection"):
        stage_runtime_secrets(source, *roots)

    assert canonical.read_bytes() == original
    assert stat.S_IMODE(api.stat().st_mode) == 0o550


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
    caddy_root = tmp_path / "caddy"
    litellm_root = tmp_path / "litellm"
    stage_runtime_secrets(
        source,
        api_root,
        migrate_root,
        worker_root,
        caddy_root,
        litellm_root,
    )
    root = api_root if projection == "api" else worker_root
    root.chmod(0o700)
    target = root / name
    target.unlink()
    target.write_bytes(malformed)
    target.chmod(0o400)
    root.chmod(0o550)

    with pytest.raises(DevInitError, match="generated credential"):
        stage_runtime_secrets(
            source,
            api_root,
            migrate_root,
            worker_root,
            caddy_root,
            litellm_root,
        )

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
    caddy_root = tmp_path / "caddy"
    litellm_root = tmp_path / "litellm"
    stage_runtime_secrets(
        source,
        api_root,
        migrate_root,
        worker_root,
        caddy_root,
        litellm_root,
    )
    root = api_root if projection == "api" else worker_root
    root.chmod(0o700)
    target = root / name
    target.unlink()
    outside = tmp_path / f"outside-{name}"
    outside.write_bytes(b"must-not-be-read-or-replaced\n")
    target.symlink_to(outside)
    root.chmod(0o550)

    with pytest.raises(DevInitError, match="projection entry"):
        stage_runtime_secrets(
            source,
            api_root,
            migrate_root,
            worker_root,
            caddy_root,
            litellm_root,
        )

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
    caddy_root = tmp_path / "caddy"
    litellm_root = tmp_path / "litellm"
    runtime_config_root = tmp_path / "runtime-config"
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
    monkeypatch.setenv("VONK_DEV_CADDY_SECRET_ROOT", str(caddy_root))
    monkeypatch.setenv("VONK_DEV_LITELLM_SECRET_ROOT", str(litellm_root))
    monkeypatch.setenv("VONK_DEV_RUNTIME_CONFIG_ROOT", str(runtime_config_root))
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
    assert (caddy_root / "controller-server-key").is_file()
    assert (litellm_root / "litellm-master-key").is_file()
    assert (runtime_config_root / "Caddyfile").is_file()
    assert all(path.is_dir() for path in (state, routes, supervisor))


def test_main_derives_mutable_repository_and_projection_identity_from_selected_cohort(
    tmp_path: Path,
    local_acceptance: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _origin_path, publisher, repository_url, initial = _origin(tmp_path)
    paths = _set_main_environment(
        tmp_path,
        monkeypatch,
        repository_url=repository_url,
    )
    paths["source"] = _secret_source(paths["source"])
    initialize_repository(paths["repository"], repository_url, initial)
    _git(paths["repository"], "config", "user.email", "test@example.invalid")
    _git(paths["repository"], "config", "user.name", "Test")
    local_deploy = _commit(paths["repository"], "local.txt", "preserve me\n")
    selected_commit = _commit(publisher, "next.txt", "accepted next\n")
    _git(publisher, "push", "-q", "origin", "main")
    selected_path, selected = _selected_cohort(
        tmp_path,
        monkeypatch,
        selected_commit=selected_commit,
    )
    monkeypatch.setenv("VONK_DEV_SELECTED_COHORT_FILE", str(selected_path))
    for name in (
        "VONK_DEV_EXPECTED_COMMIT",
        "VONK_DEV_API_IMAGE",
        "VONK_DEV_WORKER_IMAGE",
    ):
        monkeypatch.delenv(name, raising=False)

    assert main() == 0

    assert _git(paths["repository"], "rev-parse", "main") == selected_commit
    assert _git(paths["repository"], "rev-parse", "deploy") == local_deploy
    projection = json.loads(
        (paths["identity"] / "active.json").read_text(encoding="ascii")
    )
    generation = projection["selection"]["generation"]
    assert selected.generation_id == (
        "gen-" + selected.release_digest.removeprefix("sha256:")[:24]
    )
    assert generation["generation_id"] == selected.generation_id
    assert generation["release_digest"] == selected.release_digest
    assert generation["build_digest"] == selected.build_digest
    assert generation["platform_version"] == selected.platform_version
    assert generation["database_revision"] == selected.database_revision
    assert generation["api_image"] == selected.api_image
    assert generation["worker_image"] == selected.worker_image


@pytest.mark.parametrize(
    "explicit_names",
    (
        (),
        ("VONK_DEV_EXPECTED_COMMIT",),
        (
            "VONK_DEV_EXPECTED_COMMIT",
            "VONK_DEV_API_IMAGE",
            "VONK_DEV_WORKER_IMAGE",
        ),
    ),
    ids=("missing", "partial-pinned", "mixed"),
)
def test_main_rejects_missing_partial_or_mixed_development_identity_inputs_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit_names: tuple[str, ...],
) -> None:
    paths = _set_main_environment(
        tmp_path,
        monkeypatch,
        repository_url="https://github.com/CarstVaartjes/vonk-forge.git",
    )
    selected_path, _selected = _selected_cohort(
        tmp_path,
        monkeypatch,
        selected_commit="a" * 40,
    )
    values = {
        "VONK_DEV_EXPECTED_COMMIT": "a" * 40,
        "VONK_DEV_API_IMAGE": API_IMAGE,
        "VONK_DEV_WORKER_IMAGE": WORKER_IMAGE,
    }
    for name in values:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("VONK_DEV_SELECTED_COHORT_FILE", raising=False)
    for name in explicit_names:
        monkeypatch.setenv(name, values[name])
    if len(explicit_names) == 3:
        monkeypatch.setenv("VONK_DEV_SELECTED_COHORT_FILE", str(selected_path))

    with pytest.raises(DevInitError, match="identity input"):
        main()

    assert not any(paths[name].exists() for name in ("repository", "state", "routes"))


@pytest.mark.parametrize(
    ("selected_commit", "embedded_commit", "embedded_role", "malformed"),
    (
        ("a" * 40, "a" * 40, "api", True),
        ("b" * 40, "a" * 40, "api", False),
        ("a" * 40, "a" * 40, "worker", False),
    ),
    ids=("malformed", "stale", "role-mismatch"),
)
def test_main_verifies_mutable_cohort_before_repository_secrets_or_state_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selected_commit: str,
    embedded_commit: str,
    embedded_role: str,
    malformed: bool,
) -> None:
    paths = _set_main_environment(
        tmp_path,
        monkeypatch,
        repository_url="https://github.com/CarstVaartjes/vonk-forge.git",
    )
    selected_path, _selected = _selected_cohort(
        tmp_path,
        monkeypatch,
        selected_commit=selected_commit,
        embedded_commit=embedded_commit,
        embedded_role=embedded_role,
    )
    if malformed:
        selected_path.write_bytes(b"{}\n")
    monkeypatch.setenv("VONK_DEV_SELECTED_COHORT_FILE", str(selected_path))
    for name in (
        "VONK_DEV_EXPECTED_COMMIT",
        "VONK_DEV_API_IMAGE",
        "VONK_DEV_WORKER_IMAGE",
    ):
        monkeypatch.delenv(name, raising=False)
    mutations: list[str] = []
    monkeypatch.setattr(
        dev_init,
        "initialize_repository",
        lambda *_args: mutations.append("repository"),
    )
    monkeypatch.setattr(
        dev_init,
        "stage_runtime_secrets",
        lambda *_args: mutations.append("secrets"),
    )
    monkeypatch.setattr(
        dev_init,
        "_write_active_projection",
        lambda *_args: mutations.append("identity"),
    )
    monkeypatch.setattr(
        dev_init,
        "_initialize_directory",
        lambda *_args, **_kwargs: mutations.append("state"),
    )

    with pytest.raises(DevInitError, match="selected cohort"):
        main()

    assert mutations == []
    assert not any(paths[name].exists() for name in ("repository", "state", "routes"))


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
    monkeypatch.setenv("VONK_DEV_CADDY_SECRET_ROOT", str(tmp_path / "caddy"))
    monkeypatch.setenv("VONK_DEV_LITELLM_SECRET_ROOT", str(tmp_path / "litellm"))
    monkeypatch.setenv(
        "VONK_DEV_RUNTIME_CONFIG_ROOT",
        str(tmp_path / "runtime-config"),
    )
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
    monkeypatch.setenv("VONK_DEV_CADDY_SECRET_ROOT", str(tmp_path / "caddy"))
    monkeypatch.setenv("VONK_DEV_LITELLM_SECRET_ROOT", str(tmp_path / "litellm"))
    monkeypatch.setenv(
        "VONK_DEV_RUNTIME_CONFIG_ROOT",
        str(tmp_path / "runtime-config"),
    )
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

    generation = dev_init._pinned_generation_identity(
        "a" * 40,
        API_IMAGE,
        WORKER_IMAGE,
    )
    dev_init._write_active_projection(identity, generation)

    raw = (identity / "active.json").read_bytes()
    assert raw.endswith(b"\n")
    assert json.loads(raw)["projection_kind"] == "active"
    assert stat.S_IMODE((identity / "active.json").stat().st_mode) == 0o444
