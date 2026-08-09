import subprocess
from pathlib import Path

import pytest
from vonk_control.code_host import RepositoryCodeHost


def git(root: Path, *args: str, input: bytes | None = None) -> str:
    result = subprocess.run(["git", "-C", root, *args], input=input, capture_output=True, check=True)
    return result.stdout.decode().strip()


def repository(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "deploy")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.test")
    (root / "inventory").mkdir()
    (root / "inventory/fleet.toml").write_text("schema_version = 2\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "base")
    key = tmp_path / "signing-key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", key], check=True)
    return root, key


def patch_for(root: Path, text: str) -> bytes:
    (root / "inventory/fleet.toml").write_text(text)
    patch = subprocess.run(["git", "-C", root, "diff", "--binary"], capture_output=True, check=True).stdout
    git(root, "restore", "inventory/fleet.toml")
    return patch


def test_repository_code_host_creates_signed_commit_without_checkout_mutation(tmp_path: Path) -> None:
    root, key = repository(tmp_path)
    git(root, "config", "gpg.ssh.program", "false")
    base = git(root, "rev-parse", "HEAD")
    patch = patch_for(root, "schema_version = 2\n# reviewed\n")
    host = RepositoryCodeHost(root, signing_key=key, lock_path=tmp_path / "git.lock")

    commit = host.create_change("deploy", base, patch, "reviewed change", signed=True)

    assert git(root, "rev-parse", "deploy") == commit
    assert git(root, "rev-parse", "HEAD") == commit
    assert (root / "inventory/fleet.toml").read_text() == "schema_version = 2\n# reviewed\n"
    assert "gpgsig" in git(root, "cat-file", "commit", commit)
    assert host.reachable_from(commit, "deploy")


def test_repository_code_host_refuses_unsigned_or_pr_operations(tmp_path: Path) -> None:
    root, key = repository(tmp_path)
    host = RepositoryCodeHost(root, signing_key=key, lock_path=tmp_path / "git.lock")
    with pytest.raises(ValueError, match="signed"):
        host.create_change("deploy", git(root, "rev-parse", "HEAD"), b"", "change", signed=False)
    with pytest.raises(RuntimeError, match="protected code-host"):
        host.open_pull_request("branch", "a" * 40, "title")
