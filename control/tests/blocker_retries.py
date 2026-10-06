"""Prove that an unknown-outcome raise is retried, instead of assuming it.

A raise of an ``UnknownOutcomeError`` subclass is only an ``already-retried``
handoff when the work that reaches it runs under a *known retry or observe loop*
that catches it and goes on.  Raising the class does not make it so: where the
raise stays and no caller retries it, it is still bookkeeping debt.

The proof has two reviewed parts and one mechanical part:

* ``retry_loops`` in ``tools/blocker-allowlist.json`` lists the known loops, each
  ``{"path", "function", "catches", "reason"}`` (``catches`` names the
  exceptions whose handler there retries or observes): the lifecycle tick, the observe pass or the
  claim loop that re-runs the work.  A loop is declared by a person, with the
  reason it retries.
* A site is proven when a registered loop contains a ``try`` whose handler
  catches the raised class (or a contract or local ancestor, never a builtin or a
  bare ``Exception``), the handler does not raise, and the ``try`` body reaches
  the raising function through the calls the code names (same module, or a name
  the loop's module imports).  A call this analysis cannot resolve proves
  nothing, so an unprovable site stays debt.
* ``test_blocker_retries`` runs this proof against the repository, so a loop that
  is renamed or stops catching fails the suite.
"""

from __future__ import annotations

import ast
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache

from .blocker_boundaries import (
    _BUILTIN_EXCEPTIONS,
    CONTROL_SOURCE_ROOT,
    REPO_ROOT,
    parsed_modules,
)

_TOO_BROAD = _BUILTIN_EXCEPTIONS | {"HTTPException"}
UNKNOWN_BASE = "UnknownOutcomeError"
_MAX_DEPTH = 8


@dataclass(frozen=True)
class Function:
    path: str
    qualname: str
    node: ast.AST

    @property
    def simple(self) -> str:
        return self.qualname.rsplit(".", 1)[-1]


def _name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _functions(path: str, tree: ast.Module) -> list[Function]:
    found: list[Function] = []

    def walk(node: ast.AST, scope: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, (*scope, child.name))
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                qualname = ".".join((*scope, child.name))
                found.append(Function(path, qualname, child))
                walk(child, (*scope, child.name))
            else:
                walk(child, scope)

    walk(tree, ())
    return found


def _imported_modules(tree: ast.Module) -> dict[str, set[str]]:
    """Name -> module stems a ``from .x import name`` binds it from."""

    result: dict[str, set[str]] = defaultdict(set)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            for alias in node.names:
                result[alias.asname or alias.name].add(node.module.split(".")[-1])
    return result


@cache
def _function_calls(function: Function) -> frozenset[str]:
    return frozenset(_calls(function.node))


def _calls(node: ast.AST) -> set[str]:
    return {
        name
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and (name := _name(child.func)) is not None
    }


@dataclass(frozen=True)
class Index:
    functions: tuple[Function, ...]
    by_path: Mapping[str, Mapping[str, tuple[Function, ...]]]
    by_stem: Mapping[str, Mapping[str, tuple[Function, ...]]]
    imports: Mapping[str, Mapping[str, set[str]]]
    parents: Mapping[str, frozenset[str]]
    trees: Mapping[str, ast.Module]

    def resolve(self, path: str, name: str) -> tuple[Function, ...]:
        found = list(self.by_path[path].get(name, ()))
        for stem in self.imports[path].get(name, ()):
            found.extend(self.by_stem.get(stem, {}).get(name, ()))
        return tuple(found)

    def catchable(self, exception_class: str) -> frozenset[str]:
        """The names whose ``except`` catches ``exception_class``."""

        seen = {exception_class}
        queue = deque([exception_class])
        while queue:
            for parent in self.parents.get(queue.popleft(), ()):
                if parent not in seen:
                    seen.add(parent)
                    queue.append(parent)
        return frozenset(name for name in seen if name not in _TOO_BROAD)


@cache
def build_index() -> Index:
    trees = {
        module.relative_to(REPO_ROOT).as_posix(): tree
        for module, tree in parsed_modules(CONTROL_SOURCE_ROOT).items()
    }
    functions: list[Function] = []
    parents: dict[str, set[str]] = defaultdict(set)
    for path, tree in trees.items():
        functions.extend(_functions(path, tree))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                parents[node.name].update(
                    name for base in node.bases if (name := _name(base)) is not None
                )
    by_path: dict[str, dict[str, list[Function]]] = defaultdict(
        lambda: defaultdict(list)
    )
    by_stem: dict[str, dict[str, list[Function]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for function in functions:
        by_path[function.path][function.simple].append(function)
        stem = function.path.rsplit("/", 1)[-1].removesuffix(".py")
        by_stem[stem][function.simple].append(function)
    return Index(
        tuple(functions),
        {p: {n: tuple(v) for n, v in d.items()} for p, d in by_path.items()},
        {p: {n: tuple(v) for n, v in d.items()} for p, d in by_stem.items()},
        {path: _imported_modules(tree) for path, tree in trees.items()},
        {name: frozenset(value) for name, value in parents.items()},
        trees,
    )


@cache
def unknown_classes() -> frozenset[str]:
    """Local classes derived from the contract's unknown outcome."""

    index = build_index()
    return frozenset(
        name for name in index.parents if UNKNOWN_BASE in index.catchable(name)
    ) | {UNKNOWN_BASE}


def _handlers_catching(try_node: ast.Try, names: frozenset[str]) -> bool:
    for handler in try_node.handlers:
        if handler.type is None:
            continue
        caught = (
            [_name(item) for item in handler.type.elts]
            if isinstance(handler.type, ast.Tuple)
            else [_name(handler.type)]
        )
        if any(name in names for name in caught if name is not None) and not any(
            isinstance(child, ast.Raise)
            for statement in handler.body
            for child in ast.walk(statement)
        ):
            return True
    return False


def _reaches(
    index: Index, path: str, seeds: set[str], target: Function, loop: Function
) -> bool:
    queue = deque((name, path, 0) for name in seeds)
    seen: set[tuple[str, str]] = set()
    while queue:
        name, where, depth = queue.popleft()
        for function in index.resolve(where, name):
            if function == target:
                return True
            if depth >= _MAX_DEPTH or (function.path, function.qualname) in seen:
                continue
            seen.add((function.path, function.qualname))
            queue.extend(
                (called, function.path, depth + 1) for called in _calls(function.node)
            )
    return False


@cache
def _catchers(
    path: str,
    exception_class: str,
    function: str,
    allowed: frozenset[str] | None,
    within: tuple[Function, ...] | None,
) -> tuple[Function, ...]:
    return tuple(_find_catchers(path, exception_class, function, allowed, within))


def _find_catchers(
    path: str,
    exception_class: str,
    function: str,
    allowed: frozenset[str] | None = None,
    within: Sequence[Function] | None = None,
) -> list[Function]:
    """Functions whose ``try`` catches ``exception_class`` raised in ``function``.

    ``allowed`` narrows the handlers to the ones a loop's registration names.
    """

    index = build_index()
    names = index.catchable(exception_class)
    if allowed is not None:
        names = names & allowed
    targets = [
        f
        for f in index.by_path[path].get(function.rsplit(".", 1)[-1], ())
        if f.qualname == function
    ]
    if not targets:
        return []
    target = targets[0]
    found: list[Function] = []
    for candidate in index.functions if within is None else within:
        for node in ast.walk(candidate.node):
            if not isinstance(node, ast.Try) or not _handlers_catching(node, names):
                continue
            body = ast.Module(body=node.body, type_ignores=[])
            direct = candidate == target and any(
                isinstance(child, ast.Raise)
                and isinstance(child.exc, ast.Call)
                and _name(child.exc.func) == exception_class
                for child in ast.walk(body)
            )
            if direct or _reaches(
                index, candidate.path, _calls(body), target, candidate
            ):
                found.append(candidate)
                break
    return found


def registered_loops(
    document: Mapping[str, object],
) -> dict[tuple[str, str], frozenset[str]]:
    """Loop -> the exception names whose handler there retries or observes."""

    return {
        (entry["path"], entry["function"]): frozenset(entry["catches"])
        for entry in document.get("retry_loops", [])  # type: ignore[attr-defined]
    }


def proven(
    document: Mapping[str, object], path: str, exception_class: str, function: str
) -> Function | None:
    """The registered loop that retries this raise, or None."""

    index = build_index()
    for (loop_path, loop_function), caught in registered_loops(document).items():
        loops = [
            f
            for f in index.by_path[loop_path].get(loop_function.rsplit(".", 1)[-1], ())
            if f.qualname == loop_function
        ]
        found = _catchers(path, exception_class, function, caught, tuple(loops))
        if found:
            return found[0]
    return None


def loop_problems(document: Mapping[str, object]) -> list[str]:
    """A registered loop must exist and must catch something."""

    index = build_index()
    existing = {(f.path, f.qualname) for f in index.functions}
    problems: list[str] = []
    for entry in document.get("retry_loops", []):  # type: ignore[attr-defined]
        key = (entry["path"], entry["function"])
        if key not in existing:
            problems.append(f"retry loop does not exist; delete or rename it: {key}")
        if not entry.get("catches"):
            problems.append(f"retry loop must name the exceptions it retries: {key}")
        if len(str(entry.get("reason", "")).split()) < 5:
            problems.append(f"retry loop needs a written reason: {key}")
    return problems


def unproven_sites(
    document: Mapping[str, object], sites: Sequence[Sequence[object]]
) -> list[tuple[str, str, str, str]]:
    """Sites [path, class, function, code, count] of unknown classes not proven."""

    unknown = unknown_classes()
    return [
        (path, cls, function, code)  # type: ignore[misc]
        for path, cls, function, code, _count in sites
        if cls in unknown and proven(document, path, cls, function) is None  # type: ignore[arg-type]
    ]


def evaluate_retry_gate(document: Mapping[str, object]) -> list[str]:
    """An ``already-retried`` unknown-outcome raise needs a proven loop."""

    messages = loop_problems(document)
    for family in document["fail_closed"]:  # type: ignore[attr-defined]
        if family["category"] != "already-retried":
            continue
        for key in unproven_sites(document, family["sites"]):
            messages.append(
                "already-retried claim is unproven: no registered retry loop "
                "catches and retries this unknown-outcome raise; register the "
                f"loop in retry_loops or count it as bookkeeping-debt: {key}"
            )
    return messages


def demote_unproven(document: dict[str, object]) -> int:
    """Move every unproven already-retried unknown-outcome site to debt."""

    families: list[dict[str, object]] = document["fail_closed"]  # type: ignore[assignment]
    moved: dict[str, list[list[object]]] = defaultdict(list)
    for family in families:
        if family["category"] != "already-retried":
            continue
        unproven = set(unproven_sites(document, family["sites"]))  # type: ignore[arg-type]
        keep = []
        for site in family["sites"]:  # type: ignore[attr-defined]
            if tuple(site[:4]) in unproven:
                moved[site[0]].append(site)
            else:
                keep.append(site)
        family["sites"] = keep
    by_name = {str(family["family"]): family for family in families}
    reason = (
        "An unknown-outcome class is raised here, but no registered retry or "
        "observe loop is proven to catch and retry it: counted as bookkeeping "
        "debt until a loop is registered in retry_loops or the raise is removed."
    )
    for path, sites in sorted(moved.items()):
        name = f"{path.rsplit('/', 1)[-1].removesuffix('.py')}.bookkeeping-debt"
        family = by_name.get(name)
        if family is None:
            family = {
                "family": name,
                "category": "bookkeeping-debt",
                "reason": reason,
                "sites": [],
            }
            by_name[name] = family
            families.append(family)
        family["sites"].extend(sites)  # type: ignore[attr-defined]
        family["sites"].sort(key=lambda site: tuple(site[:4]))  # type: ignore[attr-defined]
    document["fail_closed"] = [f for f in families if f["sites"]]
    return sum(len(sites) for sites in moved.values())


def catchers(
    path: str,
    exception_class: str,
    function: str,
    allowed: frozenset[str] | None = None,
    within: Sequence[Function] | None = None,
) -> list[Function]:
    """Public form of the catcher search (see ``_find_catchers``)."""

    return _find_catchers(path, exception_class, function, allowed, within)
