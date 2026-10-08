"""Static audit that provenance is never compared as identity in ``control/src``.

``docs/engineering-principles.md`` owns the rule: an image is its content
(manifest digest, OCI archive sha256, bytes, architecture), a model file is its
digest, and a build is reusable by ``build_input_sha256`` (executable inputs,
without the builder binary). *Provenance* says who asked first, never what the
thing is: the recipe, slug or revision; ``build_id``; ``distribution_*``; the
runtime adapter of the asker; the builder binary inside a build-input
comparison. A comparison on provenance refuses an identical image the first
time a sibling recipe, an editorial successor or an upgraded builder reaches it,
and every such site found in production was found late because each looked
different.

This module is the machine check. ``vonk_control.content_identity`` owns every
"same image / same model / reusable build" decision, so the scan skips that
file. Anywhere else, a comparison (``==``, ``!=``, ``in``, ``not in``, ``is``,
an ordering), a query filter (``.where(X == ...)``, ``.in_(...)``,
``.filter_by(field=...)``) or a call to ``build_input_for_builder`` that touches
a provenance field is a site, and a site must be named in
``tools/content-identity-allowlist`` with a written reason. Allowed
reasons are the real security and ownership edges:

* ingress digest verification, build source policy, agent mTLS, the signed
  Controller-to-agent plans that bind what is installed, the helper's mount and
  access checks, and the operator-reviewed image of a profile load;
* selecting rows by id for ownership, cancellation, retention or navigation,
  where the id is the thing being asked for and not a stand-in for content.

Entries are keyed on path, enclosing function, kind and the normalized
expression, never on a line number, so an unrelated edit in a large module does
not break another change. An unlisted site fails, a listed site that no longer
occurs fails as stale, and an entry without a reason fails to load.

Comparisons whose other side is only a literal (``is None``, ``== ""``) ask
whether a field exists, not whether two things are the same, and are not sites.
"""

from __future__ import annotations

import ast
import json
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from .parsed_sources import memoized_scan, parsed_tree
from .registry_storage import read_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_SOURCE_ROOT = REPO_ROOT / "control" / "src"
ALLOWLIST_PATH = REPO_ROOT / "tools" / "content-identity-allowlist"
# The one module allowed to decide sameness from provenance-bearing records.
OWNER_MODULE = "control/src/vonk_control/content_identity.py"

PROVENANCE_COMPARISON = "provenance_comparison"
PROVENANCE_FILTER = "provenance_filter"
BUILDER_BINARY_IN_BUILD_INPUT = "builder_binary_in_build_input"

KINDS = frozenset(
    {PROVENANCE_COMPARISON, PROVENANCE_FILTER, BUILDER_BINARY_IN_BUILD_INPUT}
)

_PROVENANCE_FIELDS = frozenset(
    {
        "build_id",
        "recipe_build_id",
        "recipe_revision_id",
        "recipe_id",
        "distribution_publisher",
        "distribution_slug",
        "distribution_content_sha256",
    }
)
_PROVENANCE_PREFIXES = ("runtime_adapter",)
_PROVENANCE_SUFFIXES = ("_build_id",)
# A bare ``slug`` is an engine or catalog selector (``slug == "vllm"``) unless
# it is compared next to the other parts of a catalog identity.
_SLUG_COMPANIONS = frozenset({"publisher", "content_sha256"})
_BUILD_INPUT_MARKER = "build_input"
_BUILDER_BINARY_FRAGMENTS = ("builder_", "binary_digest", "binary_sha256")
_BUILDER_INPUT_CALL = "build_input_for_builder"
_FILTER_TAILS = frozenset({"in_", "not_in", "notin_"})


def _is_provenance_field(name: str) -> bool:
    return (
        name in _PROVENANCE_FIELDS
        or name.startswith(_PROVENANCE_PREFIXES)
        or name.endswith(_PROVENANCE_SUFFIXES)
    )


def _is_builder_binary(name: str) -> bool:
    return name.endswith("_for_builder") or any(
        fragment in name for fragment in _BUILDER_BINARY_FRAGMENTS
    )


def _expression_names(node: ast.AST) -> Iterator[str]:
    """Attribute and variable names in ``node``, without nested comparisons."""

    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, ast.Attribute):
            yield current.attr
        elif isinstance(current, ast.Name):
            yield current.id
        for child in ast.iter_child_nodes(current):
            if isinstance(child, ast.Compare):
                continue
            stack.append(child)


def _expression_keys(node: ast.AST) -> Iterator[str]:
    """String keys that name a field: ``payload["build_id"]``, ``.get("build_id")``."""

    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, ast.Subscript) and isinstance(
            current.slice, ast.Constant
        ):
            if isinstance(current.slice.value, str):
                yield current.slice.value
        elif (
            isinstance(current, ast.Call)
            and isinstance(current.func, ast.Attribute)
            and current.func.attr == "get"
            and current.args
            and isinstance(current.args[0], ast.Constant)
            and isinstance(current.args[0].value, str)
        ):
            yield current.args[0].value
        for child in ast.iter_child_nodes(current):
            if isinstance(child, ast.Compare):
                continue
            stack.append(child)


def _provenance_terms(nodes: Sequence[ast.AST]) -> set[str]:
    names: set[str] = set()
    keys: set[str] = set()
    for node in nodes:
        names.update(_expression_names(node))
        keys.update(_expression_keys(node))
    found = {name for name in names | keys if _is_provenance_field(name)}
    if "slug" in names and names & _SLUG_COMPANIONS:
        found.add("slug")
    if any(_BUILD_INPUT_MARKER in name for name in names | keys):
        found.update(name for name in names | keys if _is_builder_binary(name))
    return found


def _is_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Tuple, ast.Set, ast.List)):
        return all(_is_literal(element) for element in node.elts)
    return False


def _only_literals_beside(operands: Sequence[ast.expr]) -> bool:
    """True when, but for one operand, the comparison is against literals."""

    variable = [operand for operand in operands if not _is_literal(operand)]
    return len(variable) <= 1


@dataclass(frozen=True)
class Site:
    """One comparison on a provenance field."""

    path: str
    function: str
    kind: str
    expression: str
    line: int

    @property
    def identity(self) -> dict[str, object]:
        """What an allowlist entry matches: no line number."""

        return {
            "path": self.path,
            "function": self.function,
            "kind": self.kind,
            "expression": self.expression,
        }

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.kind} in {self.function}: {self.expression}"


class _Collector(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.scope: list[str] = []
        self.sites: list[Site] = []

    def _add(self, node: ast.AST, kind: str) -> None:
        self.sites.append(
            Site(
                path=self.path,
                function=".".join(self.scope) or "<module>",
                kind=kind,
                expression=" ".join(ast.unparse(node).split()),
                line=getattr(node, "lineno", 0),
            )
        )

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

    def visit_Compare(self, node: ast.Compare) -> None:
        operands = [node.left, *node.comparators]
        # A literal beside the only variable side makes this an existence or a
        # selector check, not a comparison of two things.
        if not _only_literals_beside(operands) and _provenance_terms(operands):
            self._add(node, PROVENANCE_COMPARISON)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        tail = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else None
        )
        if tail == _BUILDER_INPUT_CALL:
            self._add(node, BUILDER_BINARY_IN_BUILD_INPUT)
        elif isinstance(func, ast.Attribute) and _filters_on_provenance(func, node):
            self._add(node, PROVENANCE_FILTER)
        self.generic_visit(node)


def _filters_on_provenance(func: ast.Attribute, call: ast.Call) -> bool:
    if func.attr in _FILTER_TAILS:
        return bool(_provenance_terms([func.value]))
    if func.attr == "filter_by":
        return any(
            keyword.arg is not None and _is_provenance_field(keyword.arg)
            for keyword in call.keywords
        )
    return False


def scan_source(
    source: str, *, path: str, tree: ast.Module | None = None
) -> list[Site]:
    """Return every provenance site in one module's source.

    ``tree`` is the already parsed ``source`` (read-only) when the caller has it."""

    if path == OWNER_MODULE:
        return []
    collector = _Collector(path)
    collector.visit(tree if tree is not None else ast.parse(source))
    unique = {tuple(site.identity.values()): site for site in collector.sites}
    return sorted(unique.values(), key=lambda site: (site.line, site.expression))


def scan_provenance_sites(root: Path = CONTROL_SOURCE_ROOT) -> list[Site]:
    """Return every provenance site under ``root``."""

    def compute() -> list[Site]:
        sites: list[Site] = []
        for module, parsed in parsed_tree(root):
            relative = module.relative_to(REPO_ROOT).as_posix()
            sites.extend(scan_source(parsed.source, path=relative, tree=parsed.tree))
        return sites

    return memoized_scan(("identity", root), [root], compute)


def _key(identity: dict[str, object]) -> tuple[object, ...]:
    return (
        identity["path"],
        identity["function"],
        identity["kind"],
        identity["expression"],
    )


def load_allowlist(path: Path = ALLOWLIST_PATH) -> list[dict[str, object]]:
    """Read the reviewed allowlist. A malformed entry is a hard failure."""

    document = read_registry(path)
    if not isinstance(document, dict) or document.get("schema") != 1:
        raise ValueError(f"{path}: allowlist must be a schema-1 object")
    entries = document.get("sites")
    if not isinstance(entries, list):
        raise TypeError(f"{path}: allowlist needs a sites array")
    loaded: list[dict[str, object]] = []
    for index, entry in enumerate(entries):
        where = f"{path} sites[{index}]"
        if not isinstance(entry, dict):
            raise TypeError(f"{where}: entry is not an object")
        for field in ("path", "function", "kind", "expression"):
            if not isinstance(entry.get(field), str):
                raise TypeError(f"{where}: {field} must be a string")
        if entry["kind"] not in KINDS:
            raise ValueError(f"{where}: unknown site kind {entry['kind']!r}")
        reason = entry.get("reason")
        if not isinstance(reason, str) or len(reason.split()) < 3:
            raise ValueError(f"{where}: allowlist entry needs a written reason")
        loaded.append(entry)
    return loaded


def evaluate_identity_gate(
    sites: Sequence[Site], allowlist: Sequence[dict[str, object]]
) -> list[str]:
    """Return one message per unlisted or stale site. An empty list is a pass."""

    messages: list[str] = []
    listed = {_key(entry) for entry in allowlist}
    current = {_key(site.identity): site for site in sites}
    for key, site in sorted(current.items(), key=lambda item: item[1].render()):
        if key not in listed:
            messages.append(
                "provenance compared outside content_identity; use it, or "
                f"allowlist with a reason: {site.render()}"
            )
    for entry in allowlist:
        if _key(entry) not in current:
            messages.append(
                "allowlist entry no longer occurs; delete it: "
                f"{entry['path']}: {entry['kind']} in {entry['function']}: "
                f"{entry['expression']}"
            )
    return messages


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    sites = scan_provenance_sites()
    if arguments and arguments[0] == "--list":
        for site in sites:
            print(json.dumps(site.identity | {"line": site.line}))
        return 0
    messages = evaluate_identity_gate(sites, load_allowlist())
    if messages:
        for message in messages:
            print(message, file=sys.stderr)
        return 1
    print(f"provenance comparisons hold at {len(sites)} reviewed sites")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
