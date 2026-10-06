"""Prove that an unknown-outcome raise is retried, instead of assuming it.

A raise of an ``UnknownOutcomeError`` subclass is only an ``already-retried``
handoff when *every* way the work reaches it runs under a known retry or observe
loop that catches it and goes on.  Raising the class does not make it so: where
the raise stays reachable without a loop, it is still bookkeeping debt.

The proof has two reviewed parts and one mechanical part:

* ``retry_loops`` in ``tools/blocker-allowlist.json`` lists the known loops, each
  ``{"path", "function", "catches", "reason"}`` (``catches`` names the
  exceptions whose handler there retries or observes): the lifecycle tick, the
  observe pass or the claim loop that re-runs the work.  A loop is declared by a
  person, with the reason it retries.
* ``call_edges`` declares where a call the code cannot name goes (a ``getattr``
  with a computed name), ``{"path", "function", "calls", "reason"}``; the edge
  keeps the ``try`` context of the dynamic call, so it can be looped.
* The mechanical part walks the call graph of ``blocker_callgraph`` *backwards*
  from the raising function (flow sensitive).  The exception leaves a function
  through the first handler around the call that catches it:

  - a registered loop whose handler names the class (or a contract or local
    ancestor, never a builtin or ``Exception``) and does not raise: the path is
    *retried*, and the walk of that path ends well;
  - any other handler that catches it without raising (a swallow): the path is
    *not* proven, because the loop never sees the exception;
  - no handler: the exception climbs to the callers of that function.

  A function nobody calls, a route or other decorated registration, a property,
  an implicit dunder, a function whose reference escapes the graph and module
  level code are *entries*: reaching one with the exception still unhandled is an
  unlooped path.  A site is proven only when at least one path ends in a loop and
  **no** path ends unhandled or swallowed.  Calls the graph cannot resolve add a
  caller that must be proven too, and an edge inferred from a method name alone
  never counts as evidence that a loop retries: it can only withdraw credit, so
  an unprovable site stays debt.
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
from .blocker_callgraph import (
    CallGraph,
    DeclaredEdge,
    Function,
    Tries,
    handler_reraises,
    iter_statements,
)

__all__ = [
    "UNKNOWN_BASE",
    "Function",
    "Proof",
    "Prover",
    "build_graph_for",
    "demote_unproven",
    "evaluate_retry_gate",
    "loop_problems",
    "promote_proven",
    "proof_of",
    "proven",
    "registered_loops",
    "unknown_classes",
    "unproven_sites",
]

TOO_BROAD = _BUILTIN_EXCEPTIONS | {"HTTPException"}
UNKNOWN_BASE = "UnknownOutcomeError"
PROVEN_FAMILY_REASON = (
    "An unknown-outcome raise that a registered retry or observe loop catches "
    "without re-raising and retries or reports to the lifecycle core, on every "
    "call path that reaches it (see retry_loops for each loop and why it "
    "retries). Proven by the AST test in control/tests/blocker_retries.py."
)
DEMOTED_REASON = (
    "An unknown-outcome class is raised here, but no registered retry or "
    "observe loop is proven to catch and retry it on every call path that "
    "reaches it: counted as bookkeeping debt until a loop is registered in "
    "retry_loops or the raise is removed."
)


def _name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _handlers_catching(try_node: ast.Try, names: frozenset[str]) -> bool:
    for handler in try_node.handlers:
        if handler.type is None:
            continue
        caught = (
            [_name(item) for item in handler.type.elts]
            if isinstance(handler.type, ast.Tuple)
            else [_name(handler.type)]
        )
        if any(name in names for name in caught if name is not None) and not (
            handler_reraises(handler)
        ):
            return True
    return False


@dataclass(frozen=True)
class Proof:
    """Where the exception of a raise ends up, over every call path."""

    #: A registered loop that catches it on at least one path.
    loop: Function | None
    #: Paths that end unhandled or swallowed: ``(kind, function)``.
    unlooped: tuple[tuple[str, Function], ...]

    @property
    def proven(self) -> bool:
        return self.loop is not None and not self.unlooped

    @property
    def reached_by_a_loop(self) -> bool:
        """The old, flow-insensitive claim: some loop reaches the raise."""

        return self.loop is not None


class Prover:
    """Backward proof over one call graph and one set of registered loops."""

    def __init__(
        self,
        graph: CallGraph,
        loops: Mapping[tuple[str, str], frozenset[str]],
    ) -> None:
        self.graph = graph
        self.loops: dict[Function, frozenset[str]] = {}
        for key, caught in loops.items():
            function = graph.by_key.get(key)
            if function is not None:
                self.loops[function] = caught
        self._memo: dict[tuple[Function, str], Proof] = {}

    def prove(self, path: str, exception_class: str, function: str) -> Proof:
        target = self.graph.by_key.get((path, function))
        if target is None:
            return Proof(None, ())
        key = (target, exception_class)
        if key not in self._memo:
            self._memo[key] = self._trace(target, exception_class)
        return self._memo[key]

    def _trace(self, target: Function, exception_class: str) -> Proof:
        graph = self.graph
        loop: Function | None = None
        unlooped: list[tuple[str, Function]] = []
        queue: deque[tuple[Function, bool]] = deque()
        queued: set[tuple[Function, bool]] = set()

        def settle(where: Function, tries: Tries, guessed: bool) -> None:
            # ``guessed``: the path crossed an edge the graph only inferred from a
            # method name.  Such a path still has to end well (it may be real),
            # but it is no evidence that a loop retries the raise.
            nonlocal loop
            outcome = graph.classify_try(
                tries, exception_class, self.loops.get(where), TOO_BROAD
            )
            if outcome == "loop":
                if not guessed:
                    loop = loop or where
            elif outcome == "swallowed":
                unlooped.append(("swallowed", where))
            elif (where, guessed) not in queued:
                queued.add((where, guessed))
                queue.append((where, guessed))

        contexts = graph.facts[target].raises.get(exception_class) or [()]
        for tries in contexts:
            settle(target, tries, False)
        while queue:
            current, guessed = queue.popleft()
            kind = graph.entries.get(current)
            if kind is not None:
                unlooped.append((kind, current))
            for edge in graph.callers.get(current, ()):
                settle(edge.caller, edge.tries, guessed or edge.kind == "fallback")
        return Proof(loop, tuple(unlooped))


def _declared_edges(document: Mapping[str, object]) -> tuple[DeclaredEdge, ...]:
    return tuple(
        DeclaredEdge(
            entry["path"],
            entry["function"],
            tuple((call["path"], call["function"]) for call in entry["calls"]),
            str(entry.get("reason", "")),
        )
        for entry in document.get("call_edges", [])  # type: ignore[attr-defined]
    )


def registered_loops(
    document: Mapping[str, object],
) -> dict[tuple[str, str], frozenset[str]]:
    """Loop -> the exception names whose handler there retries or observes."""

    return {
        (entry["path"], entry["function"]): frozenset(entry["catches"])
        for entry in document.get("retry_loops", [])  # type: ignore[attr-defined]
    }


@cache
def _repository_trees() -> Mapping[str, ast.Module]:
    return {
        module.relative_to(REPO_ROOT).as_posix(): tree
        for module, tree in parsed_modules(CONTROL_SOURCE_ROOT).items()
    }


@cache
def _graph(declared: tuple[DeclaredEdge, ...]) -> CallGraph:
    return CallGraph(_repository_trees(), declared)


def build_graph_for(document: Mapping[str, object]) -> CallGraph:
    """The call graph of the repository, with the allowlist's declared edges."""

    return _graph(_declared_edges(document))


@cache
def _prover(
    declared: tuple[DeclaredEdge, ...],
    loops: tuple[tuple[tuple[str, str], frozenset[str]], ...],
) -> Prover:
    return Prover(_graph(declared), dict(loops))


def _prover_for(document: Mapping[str, object]) -> Prover:
    loops = tuple(sorted(registered_loops(document).items(), key=lambda i: i[0]))
    return _prover(_declared_edges(document), loops)


@cache
def unknown_classes() -> frozenset[str]:
    """Local classes derived from the contract's unknown outcome."""

    parents: dict[str, set[str]] = defaultdict(set)
    for tree in _repository_trees().values():
        for node in iter_statements(tree, into_defs=True):
            if isinstance(node, ast.ClassDef):
                parents[node.name].update(
                    name for base in node.bases if (name := _name(base)) is not None
                )

    def ancestors(name: str) -> set[str]:
        seen = {name}
        queue = deque([name])
        while queue:
            for parent in parents.get(queue.popleft(), ()):
                if parent not in seen:
                    seen.add(parent)
                    queue.append(parent)
        return seen

    return frozenset(name for name in parents if UNKNOWN_BASE in ancestors(name)) | {
        UNKNOWN_BASE
    }


def proof_of(
    document: Mapping[str, object], path: str, exception_class: str, function: str
) -> Proof:
    return _prover_for(document).prove(path, exception_class, function)


def proven(
    document: Mapping[str, object], path: str, exception_class: str, function: str
) -> Function | None:
    """The registered loop that retries this raise on every path, or None."""

    proof = proof_of(document, path, exception_class, function)
    return proof.loop if proof.proven else None


def loop_problems(document: Mapping[str, object]) -> list[str]:
    """A registered loop must exist and must catch something."""

    graph = build_graph_for(document)
    problems: list[str] = []
    for entry in document.get("retry_loops", []):  # type: ignore[attr-defined]
        key = (entry["path"], entry["function"])
        if key not in graph.by_key:
            problems.append(f"retry loop does not exist; delete or rename it: {key}")
        if not entry.get("catches"):
            problems.append(f"retry loop must name the exceptions it retries: {key}")
        if len(str(entry.get("reason", "")).split()) < 5:
            problems.append(f"retry loop needs a written reason: {key}")
    problems.extend(graph.declared_problems())
    declared = {function for function in graph.dynamic_calls}
    for edge in graph.declared:
        function = graph.by_key.get((edge.path, edge.function))
        if function is not None and function not in declared:
            problems.append(
                "declared call edge has no dynamic call to stand for; delete it: "
                f"{(edge.path, edge.function)}"
            )
    return problems


def unproven_dynamic_calls(document: Mapping[str, object]) -> list[tuple[str, str]]:
    """Functions with a ``getattr`` call of a computed name no edge declares."""

    graph = build_graph_for(document)
    declared = {(edge.path, edge.function) for edge in graph.declared}
    return sorted(
        (function.path, function.qualname)
        for function in graph.dynamic_calls
        if (function.path, function.qualname) not in declared
    )


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
                "catches and retries this unknown-outcome raise on every call "
                "path that reaches it; register the loop in retry_loops or "
                f"count it as bookkeeping-debt: {key}"
            )
    return messages


def _family(
    families: list[dict[str, object]],
    by_name: dict[str, dict[str, object]],
    name: str,
    category: str,
    reason: str,
) -> dict[str, object]:
    family = by_name.get(name)
    if family is None:
        family = {"family": name, "category": category, "reason": reason, "sites": []}
        by_name[name] = family
        families.append(family)
    return family


def _stem(path: str) -> str:
    return path.rsplit("/", 1)[-1].removesuffix(".py")


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
    for path, sites in sorted(moved.items()):
        family = _family(
            families,
            by_name,
            f"{_stem(path)}.bookkeeping-debt",
            "bookkeeping-debt",
            DEMOTED_REASON,
        )
        family["sites"].extend(sites)  # type: ignore[attr-defined]
        family["sites"].sort(key=lambda site: tuple(site[:4]))  # type: ignore[attr-defined]
    document["fail_closed"] = [f for f in families if f["sites"]]
    return sum(len(sites) for sites in moved.values())


def promote_proven(document: dict[str, object]) -> int:
    """Move every proven unknown-outcome bookkeeping-debt site to ``proven-retry``.

    A debt site moves only when its class is an unknown outcome and the proof
    holds on every call path; the move is the whole credit, and it is undone by
    ``demote_unproven`` the moment the proof stops holding.
    """

    families: list[dict[str, object]] = document["fail_closed"]  # type: ignore[assignment]
    unknown = unknown_classes()
    moved: dict[str, list[list[object]]] = defaultdict(list)
    for family in families:
        if family["category"] != "bookkeeping-debt":
            continue
        keep = []
        for site in family["sites"]:  # type: ignore[attr-defined]
            if site[1] in unknown and proven(document, site[0], site[1], site[2]):
                moved[site[0]].append(site)
            else:
                keep.append(site)
        family["sites"] = keep
    by_name = {str(family["family"]): family for family in families}
    for path, sites in sorted(moved.items()):
        family = _family(
            families,
            by_name,
            f"{_stem(path)}.proven-retry",
            "already-retried",
            PROVEN_FAMILY_REASON,
        )
        family["sites"].extend(sites)  # type: ignore[attr-defined]
        family["sites"].sort(key=lambda site: tuple(site[:4]))  # type: ignore[attr-defined]
    document["fail_closed"] = [f for f in families if f["sites"]]
    return sum(len(sites) for sites in moved.values())
