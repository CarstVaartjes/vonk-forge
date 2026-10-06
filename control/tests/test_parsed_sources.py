"""The scanners' shared parse is reused while a file is unchanged and redone after."""

from __future__ import annotations

from pathlib import Path

from .parsed_sources import memoized_scan, parse_file


def test_an_unchanged_file_is_parsed_once_and_a_rewritten_one_again(
    tmp_path: Path,
) -> None:
    """Catches a cache that serves a stale tree after a test rewrites its fixture."""

    module = tmp_path / "m.py"
    module.write_text("x = 1\n", encoding="utf-8")
    first = parse_file(module)
    assert parse_file(module) is first
    module.write_text("x = 1\ny = 2\n", encoding="utf-8")
    assert len(parse_file(module).tree.body) == 2


def test_a_whole_tree_scan_runs_once_per_tree_version(tmp_path: Path) -> None:
    """Catches N scanner tests each paying for the whole-tree scan, and a stale result."""

    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8")
    runs: list[int] = []

    def compute() -> list[int]:
        runs.append(1)
        return [len(runs)]

    first = memoized_scan("t", [tmp_path], compute)
    first.append(99)  # a caller that mutates its result must not leak into the next
    assert memoized_scan("t", [tmp_path], compute) == [1]
    assert runs == [1]
    (tmp_path / "b.py").write_text("b = 2\n", encoding="utf-8")
    assert memoized_scan("t", [tmp_path], compute) == [2]


def test_one_shot_parse_does_not_retain_an_unshared_test_tree(tmp_path):
    from .parsed_sources import parse_file

    path = tmp_path / "test_once.py"
    path.write_text("assert result.state == 'succeeded'\n")
    transient = parse_file(path, cache=False)
    retained = parse_file(path)
    assert transient.tree is not retained.tree
    assert parse_file(path, cache=False).tree is retained.tree
