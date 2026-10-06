"""One parse of each repository Python file, shared by every source scanner.

The vocabulary, blocker, lifecycle, coordination and content-identity scanners
each walk the whole Controller tree. Parsing it once per scanner multiplied the
cost by the number of scanner tests; this cache parses each file once per
process, keyed by the file's identity (path, size and modification time) so a
file rewritten by a test is parsed again.

The returned trees are shared: a scanner must treat them as read-only.
"""

from __future__ import annotations

import ast
import copy
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import NamedTuple, cast


class ParsedFile(NamedTuple):
    source: str
    tree: ast.Module


_CACHE: dict[Path, tuple[tuple[int, int], ParsedFile]] = {}


def parse_file(path: Path, *, cache: bool = True) -> ParsedFile:
    """Reuse a versioned tree; optionally avoid retaining a one-shot test tree.

    Existing cached trees are always reused. ``cache=False`` only declines to
    retain a newly parsed tree that no other scanner consumes.
    """

    stat = path.stat()
    version = (stat.st_size, stat.st_mtime_ns)
    cached = _CACHE.get(path)
    if cached is not None and cached[0] == version:
        return cached[1]
    source = path.read_text(encoding="utf-8")
    parsed = ParsedFile(source, ast.parse(source, filename=str(path)))
    if cache:
        _CACHE[path] = (version, parsed)
    return parsed


def python_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def parsed_tree(root: Path) -> Iterator[tuple[Path, ParsedFile]]:
    """Every module under ``root`` in path order, parsed through the cache."""

    for module in python_files(root):
        yield module, parse_file(module)


_SCANS: dict[object, tuple[tuple[object, ...], object]] = {}


def tree_fingerprint(roots: Iterable[Path]) -> tuple[object, ...]:
    """Identity of every Python file under ``roots``: changes when any file does."""

    stamp: list[object] = []
    for root in roots:
        for module in python_files(root):
            stat = module.stat()
            stamp.append((str(module), stat.st_size, stat.st_mtime_ns))
    return tuple(stamp)


def memoized_scan[Result](
    name: object, roots: Iterable[Path], compute: Callable[[], Result]
) -> Result:
    """The result of a whole-tree scan, computed once while ``roots`` are unchanged.

    Two tests that scan the same tree (the gate and a consumer of the same sites)
    share one scan. A shallow copy is returned so a caller that sorts or extends
    its result cannot change what the next test sees."""

    roots = tuple(roots)
    fingerprint = tree_fingerprint(roots)
    cached = _SCANS.get(name)
    if cached is None or cached[0] != fingerprint:
        cached = (fingerprint, compute())
        _SCANS[name] = cached
    return copy.copy(cast("Result", cached[1]))
