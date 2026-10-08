"""Ratchet on untyped mapping annotations in Controller, protocol, CLI and scripts.

The shared Pydantic contracts own every structure we control, to every depth.
``Mapping[str, object]``, ``Mapping[str, Any]``, ``dict[str, object]`` and
``dict[str, Any]`` (and their ``MutableMapping`` / ``typing.Dict`` spellings)
describe an object whose keys the type checker cannot see, so a plan, progress
document, evidence record, result, blocker or request body typed that way can
drift from its contract unnoticed.

Two kinds of annotation are allowed, both in ``tools/untyped-mapping-allowlist.json``:

* ``permanent``: a reviewed site keyed on path, enclosing function and the
  annotation text, with a count and a written reason. It is either genuinely
  external data (an explicit pass-through) or a true generic utility (a JSON
  canonicalizer, a bounded accessor over ``object``).
* ``debt``: a per-file count of sites that still carry our own structured data
  and are waiting for a contract model. It only goes down: an increase or an
  unlisted file fails, and a decrease fails until the list is lowered, so the
  ratchet cannot loosen again.

``python -m control.tests.untyped_mapping_boundaries --update`` rewrites the
``debt`` counts from the source (and never adds a ``permanent`` entry).

``object``, ``Any`` and pydantic's ``JsonValue`` anywhere in the value type count,
so ``dict[str, object | None]`` and ``dict[str, JsonValue]`` are the same debt as
``dict[str, object]``; this agrees with the stored-JSON contract walker.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOTS = (
    REPO_ROOT / "control" / "src",
    REPO_ROOT / "agent_protocol" / "src",
    REPO_ROOT / "src" / "cluster_profiles",
    REPO_ROOT / "scripts",
)
ALLOWLIST_PATH = REPO_ROOT / "tools" / "untyped-mapping-allowlist.json"

_MAPPING_NAMES = frozenset({"Mapping", "MutableMapping", "dict", "Dict"})
_UNTYPED_VALUES = frozenset({"object", "Any", "JsonValue"})


@dataclass(frozen=True)
class Site:
    path: str
    function: str
    annotation: str
    line: int

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.path, self.function, self.annotation)

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.annotation} in {self.function}"


def _name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def is_untyped_mapping(node: ast.AST) -> bool:
    if not isinstance(node, ast.Subscript):
        return False
    if _name(node.value) not in _MAPPING_NAMES:
        return False
    slice_ = node.slice
    if not isinstance(slice_, ast.Tuple) or len(slice_.elts) != 2:
        return False
    key, value = slice_.elts
    return _name(key) == "str" and _has_untyped_leaf(value)


def _has_untyped_leaf(node: ast.AST) -> bool:
    """True when ``object``, ``Any`` or ``JsonValue`` occurs anywhere in the value type.

    ``dict[str, object | None]`` and ``dict[str, list[object]]`` are as untyped as
    ``dict[str, object]``; adding a ``| None`` or a container does not type the data.
    """

    return any(
        _name(child) in _UNTYPED_VALUES
        for child in ast.walk(node)
        if isinstance(child, ast.Name | ast.Attribute)
    )


class _Collector(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.scope: list[str] = []
        self.sites: list[Site] = []

    def _enter(self, node: ast.AST, name: str) -> None:
        self.scope.append(name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._enter(node, node.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter(node, node.name)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if is_untyped_mapping(node):
            self.sites.append(
                Site(
                    path=self.path,
                    function=".".join(self.scope) or "<module>",
                    annotation=ast.unparse(node),
                    line=node.lineno,
                )
            )
            return
        self.generic_visit(node)


_CANDIDATE = re.compile(r"\[\s*str\s*,.*(?:object|Any|JsonValue)")


def scan_source(source: str, *, path: str) -> list[Site]:
    if _CANDIDATE.search(source) is None:
        return []
    collector = _Collector(path)
    collector.visit(ast.parse(source))
    return collector.sites


def scan_sites(roots: Sequence[Path] = SOURCE_ROOTS) -> list[Site]:
    sites: list[Site] = []
    for root in roots:
        for module in sorted(root.rglob("*")):
            if "generated_control" in module.parts:
                # Generated extension dictionaries are owned by the API generator.
                continue
            if not module.is_file() or not (
                module.suffix == ".py"
                or (
                    not module.suffix
                    and module.read_bytes().startswith(b"#!")
                    and b"python" in module.read_bytes().splitlines()[0]
                )
            ):
                continue
            relative = module.relative_to(REPO_ROOT).as_posix()
            sites.extend(scan_source(module.read_text(encoding="utf-8"), path=relative))
    return sites


def load_allowlist(path: Path = ALLOWLIST_PATH) -> dict[str, list[dict[str, object]]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema") != 1:
        raise ValueError(f"{path}: allowlist must be a schema-1 object")
    permanent = document.get("permanent")
    debt = document.get("debt")
    if not isinstance(permanent, list) or not isinstance(debt, list):
        raise TypeError(f"{path}: allowlist needs permanent and debt arrays")
    for index, entry in enumerate(permanent):
        where = f"{path} permanent[{index}]"
        for field in ("path", "function", "annotation"):
            if not isinstance(entry.get(field), str):
                raise TypeError(f"{where}: {field} must be a string")
        count = entry.get("count")
        if not isinstance(count, int) or count < 1:
            raise ValueError(f"{where}: count must be a positive integer")
        reason = entry.get("reason")
        if not isinstance(reason, str) or len(reason.split()) < 3:
            raise ValueError(f"{where}: allowlist entry needs a written reason")
    for index, entry in enumerate(debt):
        where = f"{path} debt[{index}]"
        if not isinstance(entry.get("path"), str):
            raise TypeError(f"{where}: path must be a string")
        count = entry.get("count")
        if not isinstance(count, int) or count < 1:
            raise ValueError(f"{where}: count must be a positive integer")
    return {**document, "permanent": permanent, "debt": debt}


def relocated_allowlist(sites, allowlist, moves=None):
    from .package_moves import PackageMoves

    moves = moves or PackageMoves(REPO_ROOT, allowlist.get("content_identities"))
    permanent = [
        {
            **entry,
            "path": moves.function(
                entry["path"],
                entry["function"],
                lambda source, name, entry=entry: any(
                    site.function == name and site.annotation == entry["annotation"]
                    for site in scan_source(source, path=entry["path"])
                ),
            ),
        }
        for entry in allowlist["permanent"]
    ]
    allowed = {
        (entry["path"], entry["function"], entry["annotation"]): entry["count"]
        for entry in permanent
    }
    remaining = Counter()
    for key, count in Counter(site.key for site in sites).items():
        remaining[key[0]] += max(0, count - allowed.get(key, 0))
    debt = moves.counts(
        {entry["path"]: entry["count"] for entry in allowlist["debt"]}, dict(remaining)
    )
    return {
        **allowlist,
        "permanent": permanent,
        "debt": [
            {"path": path, "count": count} for path, count in sorted(debt.items())
        ],
    }


def evaluate_gate(
    sites: Sequence[Site], allowlist: dict[str, list[dict[str, object]]]
) -> list[str]:
    """One message per violation; an empty list is a pass."""

    allowlist = relocated_allowlist(sites, allowlist)
    messages: list[str] = []
    permanent = {
        (str(e["path"]), str(e["function"]), str(e["annotation"])): int(e["count"])  # type: ignore[call-overload]
        for e in allowlist["permanent"]
    }
    debt = {str(e["path"]): int(e["count"]) for e in allowlist["debt"]}  # type: ignore[call-overload]
    seen: Counter[tuple[str, str, str]] = Counter(site.key for site in sites)
    remaining: Counter[str] = Counter()
    for key, count in seen.items():
        allowed = permanent.get(key, 0)
        if count > allowed:
            remaining[key[0]] += count - allowed
    for key, allowed in sorted(permanent.items()):
        actual = seen.get(key, 0)
        if actual < allowed:
            messages.append(
                f"permanent entry is stale; lower or delete it: {key[0]} "
                f"{key[2]} in {key[1]}: {allowed} -> {actual}"
            )
    for path in sorted(set(remaining) | set(debt)):
        actual = remaining.get(path, 0)
        allowed = debt.get(path, 0)
        if actual > allowed:
            where = [site.render() for site in sites if site.path == path][:5]
            messages.append(
                f"untyped mapping annotations increased in {path}: {allowed} -> "
                f"{actual}. Type the data with a contract model, or add a "
                "permanent entry with a reason. " + "; ".join(where)
            )
        elif actual < allowed:
            messages.append(
                f"untyped mapping debt shrank in {path}: {allowed} -> {actual}; "
                "run control/tests/untyped_mapping_boundaries.py --update"
            )
    return messages


def update_debt(path: Path = ALLOWLIST_PATH) -> int:
    sites = scan_sites()
    allowlist = relocated_allowlist(sites, load_allowlist(path))
    permanent = {
        (str(e["path"]), str(e["function"]), str(e["annotation"])): int(e["count"])  # type: ignore[call-overload]
        for e in allowlist["permanent"]
    }
    seen = Counter(site.key for site in sites)
    remaining: Counter[str] = Counter()
    for key, count in seen.items():
        extra = count - permanent.get(key, 0)
        if extra > 0:
            remaining[key[0]] += extra
    recorded = {str(entry["path"]): int(entry["count"]) for entry in allowlist["debt"]}
    if any(count > recorded.get(file, 0) for file, count in remaining.items()):
        raise ValueError(
            "cannot increase debt; new sites need a reviewed permanent entry"
        )
    document = {
        "schema": 1,
        "permanent": allowlist["permanent"],
        "debt": [
            {"path": file, "count": count} for file, count in sorted(remaining.items())
        ],
    }
    from .package_moves import record_identities

    document = record_identities(document, REPO_ROOT)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return sum(remaining.values())


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "--update":
        print(f"debt: {update_debt()} untyped mapping annotation(s)")
        return 0
    sites = scan_sites()
    messages = evaluate_gate(sites, load_allowlist())
    for message in messages:
        print(message, file=sys.stderr)
    if messages:
        return 1
    print(f"untyped mapping annotations hold at {len(sites)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
