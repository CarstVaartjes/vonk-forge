import os
import subprocess
from pathlib import Path

import pytest
from vonk_control.repository import RepositoryPolicyError, RepositoryService


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True,
        env=os.environ | {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid", "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"},
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "inventory").mkdir()
    (root / "inventory/fleet.toml").write_text("schema_version = 2\n")
    (root / "config/package-families").mkdir(parents=True, exist_ok=True)
    (root / "config/package-families/basic.toml").write_text('schema_version = 1\nname = "basic"\n')
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "initial")
    return root, _git(root, "rev-parse", "HEAD")


def test_repository_rejects_unallowlisted_and_traversal_paths(repository) -> None:
    root, commit = repository
    service = RepositoryService(root)
    for path in ("../../.git/config", ".git/config", "README.md", "inventory/../README.md"):
        with pytest.raises(RepositoryPolicyError):
            service.read_document(commit, path)


def test_read_is_pinned_to_immutable_commit(repository) -> None:
    root, commit = repository
    service = RepositoryService(root)
    before = service.read_document(commit, "inventory/fleet.toml")
    (root / "inventory/fleet.toml").write_text("schema_version = 999\n")
    after = service.read_document(commit, "inventory/fleet.toml")
    assert before == after
    assert before.parsed == {"schema_version": 2}
    assert before.sha256 == __import__("hashlib").sha256(before.content).hexdigest()


def test_workload_authority_documents_are_read_from_pinned_commits(repository) -> None:
    root, _commit = repository
    (root / "config/package-families").mkdir(parents=True, exist_ok=True)
    (root / "config/package-families/future.toml").write_text(
        'schema_version = 1\nfamily_id = "future"\n'
    )
    (root / "config/workload-deployments").mkdir(parents=True)
    (root / "config/workload-deployments/future.toml").write_text(
        'schema_version = 1\ndeployment_id = "future"\n'
    )
    (root / "manifests/workload-releases/future").mkdir(parents=True)
    release_path = root / (
        "manifests/workload-releases/future/" + "a" * 64 + ".json"
    )
    release_path.write_text("{}")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "workload documents")
    commit = _git(root, "rev-parse", "HEAD")
    service = RepositoryService(root)

    assert service.read_document(commit, "config/package-families/future.toml").parsed[
        "family_id"
    ] == "future"
    assert service.read_document(commit, "config/workload-deployments/future.toml").parsed[
        "deployment_id"
    ] == "future"
    assert service.read_document(
        commit, "manifests/workload-releases/future/" + "a" * 64 + ".json"
    ).parsed == {}


def test_inspect_does_not_execute_repository_hooks(repository, tmp_path: Path) -> None:
    root, commit = repository
    marker = tmp_path / "hook-ran"
    hook = root / ".git/hooks/post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o700)
    snapshot = RepositoryService(root).inspect(commit)
    assert snapshot.commit == commit
    assert "inventory/fleet.toml" in snapshot.documents
    assert not marker.exists()


def test_managed_symlink_is_rejected(repository) -> None:
    root, _ = repository
    (root / "inventory/link.toml").symlink_to("fleet.toml")
    _git(root, "add", "inventory/link.toml")
    _git(root, "commit", "-qm", "link")
    commit = _git(root, "rev-parse", "HEAD")
    with pytest.raises(RepositoryPolicyError, match="symlink"):
        RepositoryService(root).inspect(commit)


def test_abbreviated_or_unknown_commit_is_rejected(repository) -> None:
    root, commit = repository
    service = RepositoryService(root)
    with pytest.raises(RepositoryPolicyError, match="40-hex"):
        service.inspect(commit[:12])
    with pytest.raises(RepositoryPolicyError, match="commit"):
        service.inspect("0" * 40)


def test_repository_accepts_a_valid_linked_worktree(repository, tmp_path: Path) -> None:
    root, commit = repository
    linked = tmp_path / "linked"
    _git(root, "worktree", "add", "-q", "-b", "linked-test", str(linked), commit)

    service = RepositoryService(linked)

    assert service.head() == commit
    assert service.object_store == (root / ".git" / "objects").resolve()
    assert service.read_document(commit, "inventory/fleet.toml").parsed == {
        "schema_version": 2
    }
