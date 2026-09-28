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


def _history() -> tuple[str, str]:
    try:
        older, newer = subprocess.run(
            ["git", "rev-list", "--max-count=2", "--first-parent", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()[::-1]
    except (subprocess.CalledProcessError, ValueError):
        pytest.skip("repository history is unavailable")
    return older, newer


def test_dev_pointer_advances_to_a_descendant_or_the_same_source() -> None:
    module = _module()
    older, newer = _history()
    module._require_dev_descendant(older, newer)
    module._require_dev_descendant(newer, newer)


def test_dev_pointer_refuses_an_older_source() -> None:
    module = _module()
    older, newer = _history()
    with pytest.raises(module.PublicationError, match="older source"):
        module._require_dev_descendant(newer, older)


def test_dev_pointer_refuses_unknown_ancestry() -> None:
    module = _module()
    _older, newer = _history()
    with pytest.raises(module.PublicationError, match="ancestry"):
        module._require_dev_descendant("0" * 40, newer)
