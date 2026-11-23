"""Every repository path a guard names must exist, or the guard checks nothing."""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD_FILES = (
    "control/tests/added_line_guards/__main__.py",
    "control/tests/added_line_guards/check.py",
    "control/tests/added_line_guards/cli.py",
    "control/tests/added_line_guards/context.py",
    "control/tests/added_line_guards/modes/cli_tests.py",
    "control/tests/added_line_guards/modes/control.py",
    "control/tests/added_line_guards/modes/general.py",
    "control/tests/added_line_guards/modes/rust.py",
    "control/tests/added_line_guards/patch.py",
    "control/tests/added_line_guards/registry.py",
    "control/tests/added_line_guards/scans.py",
    "control/tests/principle_guards.py",
    "tests/repository_guards.py",
    "scripts/select-ci-areas",
    "control/tests/document_read_boundaries.py",
    "control/tests/untyped_mapping_boundaries.py",
    "control/tests/whole_collection_readers.py",
    "control/tests/deterministic_retries.py",
    "control/tests/lifecycle_writer_boundaries.py",
)
PATH = re.compile(r"[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]*)+")
TOP = {entry.name for entry in ROOT.iterdir() if not entry.name.startswith(".git")}
TOP.add(".github")


def _strings(node: object) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(key)
            yield from _strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _strings(item)


def literals(name: str, source: str) -> set[str]:
    if name.endswith(".json"):
        found = set(_strings(json.loads(source)))
    else:
        found = {
            node.value
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
    return {
        text for text in found if PATH.fullmatch(text) and text.split("/", 1)[0] in TOP
    }


def dangling(root: Path, path: str) -> bool:
    """A literal passes when it is a path or the prefix of at least one path."""
    parent, _, stem = path.rpartition("/")
    folder = root / parent
    return not folder.is_dir() or not any(
        entry.name.startswith(stem) for entry in folder.iterdir()
    )


def test_every_path_a_guard_names_exists() -> None:
    files = [
        *GUARD_FILES,
        *(p.relative_to(ROOT).as_posix() for p in ROOT.glob("tools/*.json")),
    ]
    names = {
        f"{name}: {path}"
        for name in files
        for path in literals(name, ROOT.joinpath(name).read_text())
        if dangling(ROOT, path)
    }
    assert not names, sorted(names)


def test_a_missing_path_and_a_matching_prefix_are_told_apart(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_a.py").write_text("")
    assert not dangling(tmp_path, "tests/test_a.py")
    assert not dangling(tmp_path, "tests/test_")
    assert dangling(tmp_path, "tests/gone.py")
    assert dangling(tmp_path, "gone/dir/")
    assert literals("g.py", "A = ('tests/x.py', 'not a path', 'zzz/q.py')") == {
        "tests/x.py"
    }
