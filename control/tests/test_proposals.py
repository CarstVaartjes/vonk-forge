import os
import subprocess
from pathlib import Path

import pytest
from vonk_control.proposals import DocumentChange, ProposalService, StaleBaseCommit
from vonk_control.repository import RepositoryService


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True,
        env=os.environ | {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid", "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"},
    ).stdout.strip()


@pytest.fixture
def proposals(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "config/package-families").mkdir(parents=True)
    (root / "config/package-families/model.toml").write_text('schema_version = 2\nname = "old"\n')
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "initial")
    service = ProposalService(RepositoryService(root), head=lambda: _git(root, "rev-parse", "HEAD"))
    return root, service


def test_equivalent_changes_produce_identical_patch(proposals) -> None:
    _, service = proposals
    base = service.head()
    first = service.preview("admin", base, [DocumentChange("config/package-families/model.toml", {"name": "new", "schema_version": 2, "labels": {"z": "2", "a": "1"}})])
    second = service.preview("admin", base, [DocumentChange("config/package-families/model.toml", {"labels": {"a": "1", "z": "2"}, "schema_version": 2, "name": "new"})])
    assert first.patch == second.patch
    assert first.digest == second.digest
    assert first.affected_documents == ("config/package-families/model.toml",)


def test_stale_base_is_rejected_after_head_moves(proposals) -> None:
    root, service = proposals
    preview = service.preview("admin", service.head(), [DocumentChange("config/package-families/model.toml", {"schema_version": 2, "name": "new"})])
    (root / "inventory").mkdir()
    (root / "inventory/topology.json").write_text('{"schema_version": 1}\n')
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "advance")
    with pytest.raises(StaleBaseCommit):
        service.apply(preview.digest)


def test_proposal_rejects_paths_and_does_not_run_hooks(proposals, tmp_path: Path) -> None:
    root, service = proposals
    marker = tmp_path / "hook"
    hook = root / ".git/hooks/post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o700)
    with pytest.raises(ValueError):
        service.preview("admin", service.head(), [DocumentChange("../../bad", {})])
    service.preview("admin", service.head(), [DocumentChange("config/package-families/model.toml", {"schema_version": 2, "name": "safe"})])
    assert not marker.exists()


def test_preview_does_not_modify_source_checkout(proposals) -> None:
    root, service = proposals
    original = (root / "config/package-families/model.toml").read_bytes()
    service.preview("admin", service.head(), [DocumentChange("config/package-families/model.toml", {"schema_version": 2, "name": "preview"})])
    assert (root / "config/package-families/model.toml").read_bytes() == original
    assert _git(root, "status", "--porcelain") == ""
