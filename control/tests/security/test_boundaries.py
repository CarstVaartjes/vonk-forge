import os
import subprocess
from pathlib import Path

import pytest
from vonk_control.proposals import DocumentChange, ProposalService
from vonk_control.repository import RepositoryService


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True,
        env=os.environ | {"GIT_AUTHOR_NAME":"Test","GIT_AUTHOR_EMAIL":"test@example.invalid","GIT_COMMITTER_NAME":"Test","GIT_COMMITTER_EMAIL":"test@example.invalid"}).stdout.strip()


@pytest.fixture
def proposals(tmp_path: Path):
    root = tmp_path / "repo"; root.mkdir(); _git(root, "init", "-q")
    (root / "config/package-families").mkdir(parents=True)
    (root / "config/package-families/base.toml").write_text('schema_version = 2\nid = "base"\nname = "Base"\n')
    _git(root, "add", "."); _git(root, "commit", "-qm", "base")
    repository = RepositoryService(root)
    return ProposalService(repository, head=repository.head), repository.head()


@pytest.mark.parametrize("document", [
    {"schema_version": 2, "id": "bad", "name": "Bad", "adapter": "../../bin/sh"},
    {"schema_version": 2, "id": "bad", "name": "Bad", "upstream": "http://attacker.invalid"},
    {"schema_version": 2, "id": "bad", "name": "Bad", "endpoint": {"host": "attacker.invalid", "port": 80}},
    {"schema_version": 2, "id": "bad", "name": "Bad", "commands": {"start": ["/bin/sh", "-c", "evil"]}},
])
def test_retired_workload_root_is_not_a_repository_authority(proposals, document) -> None:
    service, commit = proposals
    with pytest.raises(ValueError):
        service.preview("admin", commit, [DocumentChange("config/workloads/bad.toml", document)])


def test_repository_content_cannot_escape_managed_roots(proposals) -> None:
    service, commit = proposals
    with pytest.raises(ValueError):
        service.preview("admin", commit, [DocumentChange("scripts/owned", {"schema_version": 2})])
