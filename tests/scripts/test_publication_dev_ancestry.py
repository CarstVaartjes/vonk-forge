"""The dev pointer only moves forward along main, whatever the promotion order."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/install-release-publication"


def _module():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        loader = importlib.machinery.SourceFileLoader(
            "install_release_publication", str(SCRIPT)
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(ROOT / "scripts"))


@pytest.fixture
def ancestry(tmp_path: Path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=10)

    def git(*arguments: str) -> str:
        return subprocess.check_output(
            ["git", *arguments], cwd=repo, text=True, timeout=10
        ).strip()

    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("commit", "--allow-empty", "-qm", "older")
    older = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-qm", "newer")
    newer = git("rev-parse", "HEAD")
    module = _module()
    # The publisher resolves its Git cwd from its source location. Point that
    # location at the fixture, while exercising the real ancestry implementation.
    module.__file__ = str(repo / "scripts/install-release-publication")
    return module, older, newer


def test_dev_pointer_advances_to_a_descendant_or_the_same_source(ancestry) -> None:
    module, older, newer = ancestry
    module._require_dev_descendant(older, newer)
    module._require_dev_descendant(newer, newer)


def test_dev_pointer_refuses_an_older_source(ancestry) -> None:
    module, older, newer = ancestry
    with pytest.raises(module.PublicationError, match="older source"):
        module._require_dev_descendant(newer, older)


def test_dev_pointer_refuses_unknown_ancestry(ancestry) -> None:
    module, _older, newer = ancestry
    with pytest.raises(module.PublicationError, match="ancestry"):
        module._require_dev_descendant("0" * 40, newer)
