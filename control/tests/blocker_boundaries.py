"""Static ratchet over the places that can block a load on a person.

Two scans guard the rule of the blocker audit (section 6): *no operator wait
without an action, no fail-closed raise for plain bookkeeping*.  Both read
``tools/blocker-allowlist.json`` and are ratchets, like the content-identity
scanner: an unlisted site fails, a listed site that no longer occurs fails as
stale, a count that moved fails (up: a second site hid behind a listed one;
down: lower the recorded number), and the debt ceilings only go down.

**Operator waits.**  A site is a place that *produces* the state
``waiting-for-operator``: ``x.state = ...``, ``x["state"] = ...``, a ``state``
variable, ``state=...``, a positional argument of ``_finish`` /
``_set_application_state`` / ``_set_state``, or an ``AgentResultState`` /
``state: "..."`` result in the Rust agent (``rust/crates/vonk-agent/src``).
Comparisons, ``in {...}`` sets, ``.in_(...)``, ``Literal[...]`` annotations and
dictionary keys only *read* the state and are not sites.  A site is keyed
``(path, function, kind)``; an entry names the ``operation_kinds`` it applies
to, the verdict (``KEEP``, ``SELF-HEAL``, ``FIX-ACTION``, ``DERIVED``), and the
operator action it advertises.  ``KEEP`` needs an irreversible effect and one of
the real action surfaces; everything else is debt that ``max_debt`` caps.

**Fail-closed raises.**  Every ``raise C(...)`` in ``control/src`` where ``C`` is
a class defined there that is an exception (its bases lead to a builtin
exception, or its name ends in ``Conflict``, ``Error``, ``Refused``, ``Busy``,
``Invalid`` or ``NotFound``; a private class that subclasses plain
``Exception`` is internal control flow and not a site) is keyed
``(path, class, function, code)``, ``code``
being the leading dotted token of the message (``run-switch.plan_blocked``) or a
slug of its first words.  Each key belongs to one *family* with a category:
``security-edge``, ``input-validation``, ``already-retried`` (all need a written
reason) or ``bookkeeping-debt``, whose total is capped and may only fall:
``debt_ceiling.total`` for the audited modules (``scope.audited_paths``, where
the debt is being paid down) and ``debt_ceiling.unaudited`` for every other
module of ``control/src``.  ``control/tests/blocker_classifier.py`` proposes the
category of a new site by rule; a reviewer confirms it.  A raise a family does not list fails:
a new one must choose a category in a PR a reviewer can see.  Builtin
``ValueError`` / ``KeyError`` / ``TypeError`` raises, ``HTTPException`` and
raises through a factory function are not families (``--summary`` counts them).

**Categorized raises (the guard).**  In a lifecycle or operation path
(``scope.guard_paths``: ``control/src/vonk_control/lifecycle/`` and the modules
that own an operation) a ``raise`` must use an error type of the contract's three
categories: a class that derives from ``SecurityRefusalError``,
``InvalidRequestError`` or ``UnknownOutcomeError``, not a bare ``RuntimeError``,
``ValueError`` or a family ``Conflict``.  What predates the rule is grandfathered
per module in ``categorized_raises.grandfathered``; a module's count may only
fall, an unlisted module may not raise at all, and ``categorized_raises.ceiling``
is their sum.  Converting a raise to a categorized type lowers it.

``python -m control.tests.blocker_boundaries --list`` prints both scans;
``--write-baseline`` rewrites the counts after a site was removed (a new site is
never written automatically: it needs a verdict, a category and a reason).
"""

from __future__ import annotations

import ast
import builtins
import json
import re
import sys
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from vonk_agent_protocol import (
    CATEGORIZED_ERROR_BASES,
    LEGACY_WAIT_STATE,
    AgentResultState,
    BlockerCategory,
    FailureCode,
    InvalidRequestReason,
    LifecycleState,
    OperatorSurface,
    SecurityRefusalReason,
    WaitReason,
    WaitVerdict,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_SOURCE_ROOT = REPO_ROOT / "control" / "src"
RUST_SOURCE_ROOT = REPO_ROOT / "rust" / "crates" / "vonk-agent" / "src"
ALLOWLIST_PATH = REPO_ROOT / "tools" / "blocker-allowlist.json"

WAIT_STATE = LEGACY_WAIT_STATE
CONTROL_STATE_ASSIGNMENT = "control-state-assignment"
CONTROL_STATE_ARGUMENT = "control-state-argument"
#: A ``return`` of the stored wait state inside the lifecycle core: the one place a
#: core decision is projected onto a legacy state vocabulary.
CONTROL_STATE_PROJECTION = "control-state-projection"
RUST_RESULT = "rust-result"
LIFECYCLE_PREFIX = "control/src/vonk_control/lifecycle/"
WAIT_KINDS = frozenset(
    {
        CONTROL_STATE_ASSIGNMENT,
        CONTROL_STATE_ARGUMENT,
        CONTROL_STATE_PROJECTION,
        RUST_RESULT,
    }
)
#: Entry kinds with no scanned site: they document a verdict about code the
#: scan cannot see by shape (a projection or a deleted entry point).
UNSCANNED_KINDS = frozenset({"derived-mirror"})
VERDICTS = frozenset(verdict.value for verdict in WaitVerdict)
#: The real operator surfaces: ``resume``/``retire`` (POST /api/jobs/{id}/...),
#: ``retry`` (fleet profile), ``stop`` (the one-shot stop route) and
#: ``automatic`` (a scheduled retry the Controller owns).
ACTION_SURFACES = tuple(surface.value for surface in OperatorSurface)
_STATE_CALLS = frozenset({"_finish", "_set_application_state", "_set_state"})

CATEGORIES = frozenset(category.value for category in BlockerCategory)
_RAISE_SUFFIXES = ("Conflict", "Error", "Refused", "Busy", "Invalid", "NotFound")
#: The raisable error categories of the contract (``SecurityRefusalError`` ...).
CATEGORIZED_BASE_NAMES = frozenset(base.__name__ for base in CATEGORIZED_ERROR_BASES)
#: Exception bases that are not builtins but make a local class an error type.
_EXTERNAL_EXCEPTION_BASES = (
    frozenset({"HTTPException", "StarletteHTTPException"}) | CATEGORIZED_BASE_NAMES
)
#: Raises that state a programming or protocol fact, not a refusal or a wait.
GUARD_EXEMPT_CLASSES = frozenset(
    {
        "AssertionError",
        "CancelledError",
        "NotImplementedError",
        "StopAsyncIteration",
        "StopIteration",
    }
)
_BUILTIN_EXCEPTIONS = frozenset(
    name
    for name, value in vars(builtins).items()
    if isinstance(value, type) and issubclass(value, BaseException)
)


@dataclass(frozen=True)
class WaitSite:
    path: str
    function: str
    kind: str
    line: int

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.path, self.function, self.kind)

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.kind} in {self.function}"


@dataclass(frozen=True)
class RaiseSite:
    path: str
    exception_class: str
    function: str
    code: str
    line: int
    #: The leading text of the message (a hint for the classifier, not identity).
    message: str = field(default="", compare=False)

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return (self.path, self.exception_class, self.function, self.code)

    def render(self) -> str:
        return (
            f"{self.path}:{self.line}: raise {self.exception_class} in "
            f"{self.function}: {self.code}"
        )


# --------------------------------------------------------------------- waits


#: The contract names of the stored wait state (``vonk_agent_protocol``):
#: ``LEGACY_WAIT_STATE`` and ``AgentResultState.WAITING_FOR_OPERATOR[.value]``.
_WAIT_CONTRACT_NAME = "LEGACY_WAIT_STATE"
_WAIT_CONTRACT_MEMBER = AgentResultState.WAITING_FOR_OPERATOR.name
#: The stored wait word of a kind on the core vocabulary (``LifecycleState.NEEDS_OPERATOR``).
_STORED_WAIT_MEMBER = LifecycleState.NEEDS_OPERATOR.name


def _is_wait_literal(node: ast.AST) -> bool:
    """The stored wait state, spelled as the literal or as the contract's name."""

    if isinstance(node, ast.Constant):
        return node.value == WAIT_STATE
    if isinstance(node, ast.Name):
        return node.id in {_WAIT_CONTRACT_NAME, _STORED_WAIT_MEMBER}
    if isinstance(node, ast.Attribute):
        if node.attr == "value":
            return _is_wait_literal(node.value)
        if node.attr == _STORED_WAIT_MEMBER:
            # ``aos.NEEDS_OPERATOR``: the stored word of a kind that has moved onto
            # the core vocabulary, spelled through its ``*_states`` module.
            return isinstance(node.value, ast.Name) and (
                node.value.id == "aos" or node.value.id.endswith("_states")
            )
        return node.attr == _WAIT_CONTRACT_MEMBER
    return False


def _wait_constant_names(tree: ast.Module) -> frozenset[str]:
    """Module-level names bound to the literal (``WAITING = "waiting-for-operator"``)."""

    return frozenset(
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign) and _is_wait_literal(node.value)
        for target in node.targets
        if isinstance(target, ast.Name)
    )


def _value_leaves(node: ast.AST) -> Iterator[ast.AST]:
    """Leaves a value expression can evaluate to (through ``a if c else b``)."""

    if isinstance(node, ast.IfExp):
        yield from _value_leaves(node.body)
        yield from _value_leaves(node.orelse)
    elif isinstance(node, ast.BoolOp):
        for value in node.values:
            yield from _value_leaves(value)
    else:
        yield node


def _is_state_target(target: ast.AST) -> bool:
    if isinstance(target, ast.Attribute):
        return target.attr == "state"
    if isinstance(target, ast.Name):
        return target.id == "state"
    return (
        isinstance(target, ast.Subscript)
        and isinstance(target.slice, ast.Constant)
        and target.slice.value == "state"
    )


class _WaitCollector(ast.NodeVisitor):
    def __init__(self, path: str, constants: frozenset[str] = frozenset()) -> None:
        self.path = path
        self.constants = constants
        self.scope: list[str] = []
        self.sites: list[WaitSite] = []

    def _is_wait(self, node: ast.AST) -> bool:
        return _is_wait_literal(node) or (
            isinstance(node, ast.Name) and node.id in self.constants
        )

    def _add(self, node: ast.AST, kind: str) -> None:
        self.sites.append(
            WaitSite(
                path=self.path,
                function=".".join(self.scope) or "<module>",
                kind=kind,
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

    def _assigned(self, targets: Sequence[ast.AST], value: ast.AST | None) -> None:
        if value is None or not any(_is_state_target(target) for target in targets):
            return
        for leaf in _value_leaves(value):
            if self._is_wait(leaf):
                self._add(leaf, CONTROL_STATE_ASSIGNMENT)

    def visit_Assign(self, node: ast.Assign) -> None:
        self._assigned(node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._assigned([node.target], node.value)
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
        values = [keyword.value for keyword in node.keywords if keyword.arg == "state"]
        if tail in _STATE_CALLS:
            values.extend(node.args)
        for value in values:
            for leaf in _value_leaves(value):
                if self._is_wait(leaf):
                    self._add(leaf, CONTROL_STATE_ARGUMENT)
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        if self.path.startswith(LIFECYCLE_PREFIX) and node.value is not None:
            parts = (
                node.value.elts if isinstance(node.value, ast.Tuple) else [node.value]
            )
            for part in parts:
                for leaf in _value_leaves(part):
                    if self._is_wait(leaf):
                        self._add(leaf, CONTROL_STATE_PROJECTION)
        self.generic_visit(node)


def _scan_tree_waits(tree: ast.Module, *, path: str) -> list[WaitSite]:
    collector = _WaitCollector(path, _wait_constant_names(tree))
    collector.visit(tree)
    return sorted(collector.sites, key=lambda site: site.line)


def scan_python_waits(source: str, *, path: str) -> list[WaitSite]:
    return _scan_tree_waits(ast.parse(source), path=path)


@cache
def parsed_modules(root: Path) -> Mapping[Path, ast.Module]:
    """Every module under ``root``, parsed once: the wait and raise scans share it."""

    return {
        module: ast.parse(module.read_text(encoding="utf-8"))
        for module in sorted(root.rglob("*.py"))
    }


_RUST_FN = re.compile(r"^\s*(?:pub(?:\([a-z]+\))?\s+)?(?:async\s+)?fn\s+(\w+)")
_RUST_IMPL = re.compile(r"^\s*impl(?:<[^>]*>)?\s+(?:[\w:<>, ]+\s+for\s+)?(\w+)")
#: The constructors of an unknown outcome (a wait reason, never a free body); each
#: of their callers is its own site, and their own bodies are not.
_RUST_CONSTRUCTORS = frozenset({"unconfirmed", "unconfirmed_job"})
_RUST_WAIT_CALL = re.compile(
    r"\b(?:unconfirmed|unconfirmed_job)\(|ExecutionResult::[Uu]nknown\("
)


def scan_rust_waits(source: str, *, path: str) -> list[WaitSite]:
    """Callers of an unknown-outcome constructor, keyed by their enclosing function.

    The agent builds a wait only through ``ExecutionResult::unknown`` (or the
    ``unconfirmed`` helpers around it), each naming a typed wait reason."""

    sites: list[WaitSite] = []
    function = "<module>"
    owner = ""
    for number, line in enumerate(source.splitlines(), start=1):
        implemented = _RUST_IMPL.match(line)
        if implemented:
            owner = implemented.group(1)
        declared = _RUST_FN.match(line)
        if declared:
            function = declared.group(1)
            if declared.group(1) not in _RUST_CONSTRUCTORS and not line.startswith(
                "fn "
            ):
                function = f"{owner}::{function}" if owner else function
        stripped = line.strip()
        if stripped.startswith("//") or function in _RUST_CONSTRUCTORS:
            continue
        if _RUST_WAIT_CALL.search(line) and not declared:
            sites.append(WaitSite(path, function, RUST_RESULT, number))
    return sites


def scan_waits(
    control_root: Path = CONTROL_SOURCE_ROOT, rust_root: Path = RUST_SOURCE_ROOT
) -> list[WaitSite]:
    sites: list[WaitSite] = []
    for module, tree in parsed_modules(control_root).items():
        relative = module.relative_to(REPO_ROOT).as_posix()
        sites.extend(_scan_tree_waits(tree, path=relative))
    for module in sorted(rust_root.rglob("*.rs")):
        relative = module.relative_to(REPO_ROOT).as_posix()
        sites.extend(scan_rust_waits(module.read_text(encoding="utf-8"), path=relative))
    return sites


# --------------------------------------------------------------------- raises


def exception_classes(trees: Sequence[ast.Module]) -> frozenset[str]:
    """Names of the exception classes defined in the scanned tree.

    A class is an error type when its name follows the suffix convention or its
    bases lead (through classes defined here) to a builtin exception.  A private
    class that subclasses plain ``Exception`` is control flow inside its module
    (``_Kept``, ``_RangeIgnored``) and is not a site.
    """

    bases: dict[str, set[str]] = {}
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                names = bases.setdefault(node.name, set())
                for base in node.bases:
                    if isinstance(base, ast.Name):
                        names.add(base.id)
                    elif isinstance(base, ast.Attribute):
                        names.add(base.attr)
    errors: set[str] = set()
    grew = True
    while grew:
        grew = False
        for name, names in bases.items():
            if name not in errors and any(
                base in _BUILTIN_EXCEPTIONS
                or base in _EXTERNAL_EXCEPTION_BASES
                or base in errors
                for base in names
            ):
                errors.add(name)
                grew = True
    internal = {
        name
        for name in errors
        if name.startswith("_") and bases[name] <= {"Exception", "BaseException"}
    }
    return frozenset(
        (errors - internal) | {name for name in bases if name.endswith(_RAISE_SUFFIXES)}
    )


#: The closed code sets of the contract a raise may name instead of a literal
#: (``SecurityRefusalReason.GRANT_INVALID.value``).
_CONTRACT_CODE_ENUMS = {
    enum.__name__: enum
    for enum in (SecurityRefusalReason, FailureCode, WaitReason, InvalidRequestReason)
}


def _contract_word(node: ast.AST) -> str | None:
    """The word a ``ContractEnum.MEMBER[.value]`` reference names, else ``None``."""

    if isinstance(node, ast.Attribute) and node.attr == "value":
        node = node.value
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in _CONTRACT_CODE_ENUMS
    ):
        member = _CONTRACT_CODE_ENUMS[node.value.id].__members__.get(node.attr)
        return None if member is None else str(member.value)
    return None


def _leading_text(node: ast.AST) -> str | None:
    if (word := _contract_word(node)) is not None:
        return word
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                break
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _leading_text(node.left)
    return None


_CODE = re.compile(r"^([A-Za-z0-9_]+(?:[.-][A-Za-z0-9_]+)*\.[A-Za-z0-9_.-]+?)(?::|$)")


def failure_code(call: ast.Call) -> str:
    """Leading dotted token of the first string argument, else a slug."""

    for argument in [*call.args, *(keyword.value for keyword in call.keywords)]:
        text = _leading_text(argument)
        if text is None:
            continue
        text = text.strip()
        matched = _CODE.match(text.split()[0]) if text else None
        if matched and "." in matched.group(1):
            return matched.group(1).rstrip(".")
        slug = re.sub(r"[^a-z0-9]+", "-", text[:60].lower()).strip("-")
        return slug or "message"
    return "message"


def failure_message(call: ast.Call) -> str:
    """The leading text of the first string argument, bounded; empty if none."""

    for argument in [*call.args, *(keyword.value for keyword in call.keywords)]:
        text = _leading_text(argument)
        if text is not None:
            return " ".join(text.split())[:200]
    return ""


class _RaiseCollector(ast.NodeVisitor):
    def __init__(self, path: str, classes: frozenset[str]) -> None:
        self.path = path
        self.classes = classes
        self.scope: list[str] = []
        self.sites: list[RaiseSite] = []

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

    def visit_Raise(self, node: ast.Raise) -> None:
        call = node.exc
        if isinstance(call, ast.Call):
            func = call.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name in self.classes:
                self.sites.append(
                    RaiseSite(
                        path=self.path,
                        exception_class=name,
                        function=".".join(self.scope) or "<module>",
                        code=failure_code(call),
                        line=node.lineno,
                        message=failure_message(call),
                    )
                )
        self.generic_visit(node)


def scan_raises(root: Path = CONTROL_SOURCE_ROOT) -> list[RaiseSite]:
    """Raises of the error classes defined anywhere under ``root``, in every module."""

    trees = parsed_modules(root)
    classes = exception_classes(list(trees.values()))
    sites: list[RaiseSite] = []
    for module, tree in trees.items():
        collector = _RaiseCollector(module.relative_to(REPO_ROOT).as_posix(), classes)
        collector.visit(tree)
        sites.extend(collector.sites)
    return sorted(sites, key=lambda site: (site.path, site.line))


def scan_raise_source(
    source: str, *, path: str, classes: frozenset[str]
) -> list[RaiseSite]:
    collector = _RaiseCollector(path, classes)
    collector.visit(ast.parse(source))
    return collector.sites


# ---------------------------------------------------------------------- guard


@dataclass(frozen=True)
class UncategorizedRaise:
    """A raise in a guarded path of a class outside the three error categories."""

    path: str
    exception_class: str
    function: str
    line: int

    def render(self) -> str:
        return (
            f"{self.path}:{self.line}: raise {self.exception_class} in {self.function}"
        )


def categorized_classes(trees: Sequence[ast.Module]) -> frozenset[str]:
    """Local classes whose bases lead to a category base of the contract."""

    bases: dict[str, set[str]] = {}
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                names = bases.setdefault(node.name, set())
                for base in node.bases:
                    if isinstance(base, ast.Name):
                        names.add(base.id)
                    elif isinstance(base, ast.Attribute):
                        names.add(base.attr)
    categorized: set[str] = set(CATEGORIZED_BASE_NAMES)
    grew = True
    while grew:
        grew = False
        for name, names in bases.items():
            if name not in categorized and names & categorized:
                categorized.add(name)
                grew = True
    return frozenset(categorized)


class _GuardCollector(ast.NodeVisitor):
    def __init__(self, path: str, categorized: frozenset[str]) -> None:
        self.path = path
        self.categorized = categorized
        self.scope: list[str] = []
        self.sites: list[UncategorizedRaise] = []

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

    def visit_Raise(self, node: ast.Raise) -> None:
        call = node.exc
        if isinstance(call, ast.Call):
            func = call.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else "?"
            )
            if name not in self.categorized and name not in GUARD_EXEMPT_CLASSES:
                self.sites.append(
                    UncategorizedRaise(
                        self.path,
                        name,
                        ".".join(self.scope) or "<module>",
                        node.lineno,
                    )
                )
        self.generic_visit(node)


def _in_guard_scope(path: str, guard_paths: Sequence[str]) -> bool:
    return any(
        path.startswith(entry) if entry.endswith("/") else path == entry
        for entry in guard_paths
    )


def guard_paths(document: dict[str, object]) -> list[str]:
    """The lifecycle and operation paths a raise must be categorized in."""

    scope = document["scope"]
    return [str(entry) for entry in scope["guard_paths"]]  # type: ignore[index, attr-defined]


def scan_guard_raises(
    paths: Sequence[str], root: Path = CONTROL_SOURCE_ROOT
) -> list[UncategorizedRaise]:
    """Raises in ``paths`` whose class is not of the three error categories."""

    trees = parsed_modules(root)
    categorized = categorized_classes(list(trees.values()))
    sites: list[UncategorizedRaise] = []
    for module, tree in trees.items():
        relative = module.relative_to(REPO_ROOT).as_posix()
        if _in_guard_scope(relative, paths):
            collector = _GuardCollector(relative, categorized)
            collector.visit(tree)
            sites.extend(collector.sites)
    return sorted(sites, key=lambda site: (site.path, site.line))


def scan_guard_source(
    source: str, *, path: str, categorized: frozenset[str]
) -> list[UncategorizedRaise]:
    collector = _GuardCollector(path, categorized)
    collector.visit(ast.parse(source))
    return collector.sites


def evaluate_guard_gate(
    sites: Sequence[UncategorizedRaise], document: dict[str, object]
) -> list[str]:
    """A raise in a guarded path must be categorized; the grandfathered count falls."""

    section = document["categorized_raises"]
    grandfathered: dict[str, int] = section["grandfathered"]  # type: ignore[index, assignment]
    current = Counter(site.path for site in sites)
    first = {site.path: site for site in reversed(sites)}
    messages: list[str] = []
    for path, count in sorted(current.items()):
        if path not in grandfathered:
            messages.append(
                "raise in a lifecycle or operation path outside the three error "
                "categories; raise SecurityRefusalError, InvalidRequestError or "
                f"UnknownOutcomeError (or a subclass): {first[path].render()}"
            )
        elif count > grandfathered[path]:
            messages.append(
                f"uncategorized raises in {path} rose from {grandfathered[path]} to "
                f"{count}; categorize the new one: {first[path].render()}"
            )
        elif count < grandfathered[path]:
            messages.append(
                f"uncategorized raises in {path} fell from {grandfathered[path]} to "
                f"{count}; lower the recorded count"
            )
    for path in sorted(grandfathered):
        if path not in current:
            messages.append(f"grandfathered module has none left; delete it: {path}")
    total = sum(grandfathered.values())
    ceiling = int(section["ceiling"])  # type: ignore[index, call-overload]
    if total > ceiling:
        messages.append(
            f"grandfathered uncategorized raises are {total}, above the ceiling {ceiling}"
        )
    elif total < ceiling:
        messages.append(
            f"grandfathered uncategorized raises are {total}; lower "
            f"categorized_raises.ceiling from {ceiling}"
        )
    return messages


# ------------------------------------------------------------------ allowlist


def _require_text(
    entry: dict[str, object], field: str, where: str, words: int = 1
) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or len(value.split()) < words:
        raise ValueError(f"{where}: {field} must be written text")
    return value


def load_allowlist(path: Path = ALLOWLIST_PATH) -> dict[str, object]:
    """Read and validate the allowlist. A malformed entry is a hard failure."""

    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema") != 1:
        raise ValueError(f"{path}: allowlist must be a schema-1 object")
    for required in ("max_debt", "debt_ceiling", "scope", "categorized_raises"):
        if required not in document:
            raise ValueError(f"{path}: missing {required}")
    guard = document["categorized_raises"]
    if not isinstance(guard.get("grandfathered"), dict) or not isinstance(
        guard.get("ceiling"), int
    ):
        raise TypeError(f"{path}: categorized_raises needs grandfathered and ceiling")
    if not document["scope"].get("guard_paths"):
        raise ValueError(f"{path}: scope.guard_paths must list the guarded paths")
    waits = document.get("operator_waits")
    families = document.get("fail_closed")
    if not isinstance(waits, list) or not isinstance(families, list):
        raise TypeError(f"{path}: needs operator_waits and fail_closed arrays")
    for index, entry in enumerate(waits):
        where = f"{path} operator_waits[{index}]"
        if not isinstance(entry, dict):
            raise TypeError(f"{where}: entry is not an object")
        for name in ("path", "function", "kind"):
            _require_text(entry, name, where)
        if entry["kind"] not in WAIT_KINDS | UNSCANNED_KINDS:
            raise ValueError(f"{where}: unknown kind {entry['kind']!r}")
        if entry["verdict"] not in VERDICTS:
            raise ValueError(f"{where}: verdict must be one of {sorted(VERDICTS)}")
        action = _require_text(entry, "advertised_action", where)
        _require_text(entry, "reason", where, words=3)
        if not isinstance(entry.get("operation_kinds"), list):
            raise TypeError(f"{where}: operation_kinds must be a list")
        if entry["verdict"] == "KEEP":
            effect = _require_text(entry, "irreversible_effect", where)
            if effect.strip().lower().startswith("none"):
                raise ValueError(f"{where}: KEEP needs an irreversible effect")
            if not any(surface in action for surface in ACTION_SURFACES):
                raise ValueError(f"{where}: KEEP needs a real action surface")
        sites = entry.get("sites")
        if entry["kind"] in WAIT_KINDS and (not isinstance(sites, int) or sites < 1):
            raise ValueError(f"{where}: sites must be a positive integer")
    for index, family in enumerate(families):
        where = f"{path} fail_closed[{index}]"
        if not isinstance(family, dict):
            raise TypeError(f"{where}: family is not an object")
        _require_text(family, "family", where)
        if family.get("category") not in CATEGORIES:
            raise ValueError(f"{where}: category must be one of {sorted(CATEGORIES)}")
        _require_text(family, "reason", where, words=3)
        sites = family.get("sites")
        if not isinstance(sites, list) or not sites:
            raise ValueError(f"{where}: sites must be a non-empty list")
        for site in sites:
            if not (isinstance(site, list) and len(site) == 5):
                raise ValueError(
                    f"{where}: a site is [path, class, function, code, count]"
                )
    return document


def audited_paths(document: dict[str, object]) -> frozenset[str]:
    """The modules whose bookkeeping debt is being paid down (the first audit).

    Their debt has its own ceiling, apart from the debt of every other module.
    """

    scope = document["scope"]
    paths = scope.get("audited_paths", [])  # type: ignore[attr-defined]
    return frozenset(str(path) for path in paths)


def _wait_groups(
    entries: Sequence[dict[str, object]],
) -> dict[tuple[str, str, str], int]:
    groups: dict[tuple[str, str, str], int] = {}
    for entry in entries:
        if entry["kind"] not in WAIT_KINDS:
            continue
        key = (str(entry["path"]), str(entry["function"]), str(entry["kind"]))
        recorded = int(entry["sites"])  # type: ignore[call-overload]
        if groups.setdefault(key, recorded) != recorded:
            raise ValueError(f"inconsistent site counts for {key}")
    return groups


def evaluate_wait_gate(
    sites: Sequence[WaitSite], document: dict[str, object]
) -> list[str]:
    entries: list[dict[str, object]] = document["operator_waits"]  # type: ignore[assignment]
    messages: list[str] = []
    listed = _wait_groups(entries)
    current = Counter(site.identity for site in sites)
    first = {site.identity: site for site in reversed(sites)}
    for key, count in sorted(current.items()):
        if key not in listed:
            messages.append(
                "operator wait outside the blocker allowlist; self-heal it, or "
                f"list it with a verdict and an advertised action: {first[key].render()}"
            )
        elif count > listed[key]:
            messages.append(
                f"operator-wait sites rose from {listed[key]} to {count}: "
                f"{first[key].render()}"
            )
        elif count < listed[key]:
            messages.append(
                f"operator-wait sites fell from {listed[key]} to {count}; lower "
                f"'sites' for {key}"
            )
    for key in sorted(listed):
        if key not in current:
            messages.append(f"operator-wait entry no longer occurs; delete it: {key}")
    debt = sum(1 for entry in entries if entry["verdict"] != "KEEP")
    ceiling = int(document["max_debt"])  # type: ignore[call-overload]
    if debt > ceiling:
        messages.append(
            f"operator-wait debt entries are {debt}, above max_debt {ceiling}"
        )
    elif debt < ceiling:
        messages.append(
            f"operator-wait debt entries are {debt}; lower max_debt from {ceiling}"
        )
    return messages


def _raise_keys(
    families: Sequence[dict[str, object]],
) -> dict[tuple[str, str, str, str], tuple[int, str]]:
    keys: dict[tuple[str, str, str, str], tuple[int, str]] = {}
    for family in families:
        for path, exception, function, code, count in family["sites"]:  # type: ignore[misc]
            key = (path, exception, function, code)
            if key in keys:
                raise ValueError(f"raise site listed in two families: {key}")
            keys[key] = (int(count), str(family["category"]))
    return keys


def evaluate_raise_gate(
    sites: Sequence[RaiseSite], document: dict[str, object]
) -> list[str]:
    families: list[dict[str, object]] = document["fail_closed"]  # type: ignore[assignment]
    messages: list[str] = []
    listed = _raise_keys(families)
    current = Counter(site.identity for site in sites)
    first = {site.identity: site for site in reversed(sites)}
    for key, count in sorted(current.items()):
        if key not in listed:
            messages.append(
                "fail-closed raise outside the blocker allowlist; return unknown "
                "and reconcile, or add it to a security-edge / input-validation / "
                f"already-retried family with a reason: {first[key].render()}"
            )
        elif count > listed[key][0]:
            messages.append(
                f"fail-closed raises rose from {listed[key][0]} to {count}: "
                f"{first[key].render()}"
            )
        elif count < listed[key][0]:
            messages.append(
                f"fail-closed raises fell from {listed[key][0]} to {count}; lower "
                f"the recorded count: {key}"
            )
    for key in sorted(listed):
        if key not in current:
            messages.append(f"fail-closed entry no longer occurs; delete it: {key}")
    audited = audited_paths(document)
    debt = {"total": 0, "unaudited": 0}
    for (path, *_), (count, category) in listed.items():
        if category == "bookkeeping-debt":
            debt["total" if path in audited else "unaudited"] += count
    ceilings = document["debt_ceiling"]
    for name, label in (("total", "audited modules"), ("unaudited", "other modules")):
        ceiling = int(ceilings.get(name, 0))  # type: ignore[attr-defined, call-overload]
        if debt[name] > ceiling:
            messages.append(
                f"bookkeeping-debt raises in the {label} are {debt[name]}, above "
                f"the ceiling {ceiling}"
            )
        elif debt[name] < ceiling:
            messages.append(
                f"bookkeeping-debt raises in the {label} are {debt[name]}; lower "
                f"debt_ceiling.{name} from {ceiling}"
            )
    return messages


def evaluate_blocker_gate(
    waits: Sequence[WaitSite],
    raises: Sequence[RaiseSite],
    document: dict[str, object],
    guard: Sequence[UncategorizedRaise] | None = None,
) -> list[str]:
    return [
        *evaluate_wait_gate(waits, document),
        *evaluate_raise_gate(raises, document),
        *([] if guard is None else evaluate_guard_gate(guard, document)),
    ]


def _lowered_guard(
    document: dict[str, object], guard: Sequence[UncategorizedRaise] | None
) -> dict[str, object]:
    section: dict[str, object] = document["categorized_raises"]  # type: ignore[assignment]
    if guard is None:
        return section
    current = Counter(site.path for site in guard)
    recorded: dict[str, int] = section["grandfathered"]  # type: ignore[assignment]
    lowered = {
        path: min(count, current[path])
        for path, count in recorded.items()
        if current[path]
    }
    return {**section, "grandfathered": lowered, "ceiling": sum(lowered.values())}


def write_counts(
    document: dict[str, object],
    waits: Sequence[WaitSite],
    raises: Sequence[RaiseSite],
    guard: Sequence[UncategorizedRaise] | None = None,
) -> dict[str, object]:
    """Lower recorded counts and drop vanished entries; never add a site."""

    current_waits = Counter(site.identity for site in waits)
    kept_waits = []
    for entry in document["operator_waits"]:  # type: ignore[attr-defined]
        if entry["kind"] in WAIT_KINDS:
            key = (entry["path"], entry["function"], entry["kind"])
            if key not in current_waits:
                continue
            entry = {**entry, "sites": min(entry["sites"], current_waits[key])}
        kept_waits.append(entry)
    current_raises = Counter(site.identity for site in raises)
    families = []
    for family in document["fail_closed"]:  # type: ignore[attr-defined]
        sites = []
        for path, exception, function, code, count in family["sites"]:
            key = (path, exception, function, code)
            if key in current_raises:
                sites.append(
                    [path, exception, function, code, min(count, current_raises[key])]
                )
        if sites:
            families.append({**family, "sites": sites})
    audited = audited_paths(document)
    debt = {"total": 0, "unaudited": 0}
    for family in families:
        if family["category"] == "bookkeeping-debt":
            for site in family["sites"]:
                debt["total" if site[0] in audited else "unaudited"] += site[4]
    return {
        **document,
        "max_debt": sum(1 for entry in kept_waits if entry["verdict"] != "KEEP"),
        "debt_ceiling": {**document["debt_ceiling"], **debt},  # type: ignore[dict-item]
        "categorized_raises": _lowered_guard(document, guard),
        "operator_waits": kept_waits,
        "fail_closed": families,
    }


def dump_document(document: dict[str, object]) -> str:
    """One site per line: a diff of the allowlist reads as a diff of sites."""

    waits = ",\n".join(
        "    " + json.dumps(entry, indent=2).replace("\n", "\n    ")
        for entry in document["operator_waits"]  # type: ignore[attr-defined]
    )
    families = []
    for family in document["fail_closed"]:  # type: ignore[attr-defined]
        head = {key: value for key, value in family.items() if key != "sites"}
        body = ",\n".join("        " + json.dumps(site) for site in family["sites"])
        head_text = json.dumps(head, indent=2).replace("\n", "\n    ")
        families.append(
            "    "
            + head_text[:-2].rstrip()
            + f',\n      "sites": [\n{body}\n      ]\n    }}'
        )
    other = {
        key: value
        for key, value in document.items()
        if key not in {"operator_waits", "fail_closed"}
    }
    prefix = json.dumps(other, indent=2)[:-2].rstrip()
    return (
        prefix
        + f',\n  "operator_waits": [\n{waits}\n  ],\n'
        + f'  "fail_closed": [\n{",\n".join(families)}\n  ]\n}}\n'
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    document = load_allowlist()
    waits = scan_waits()
    raises = scan_raises()
    guard = scan_guard_raises(guard_paths(document))
    if arguments and arguments[0] == "--list":
        for site in waits:
            print(site.render())
        for raise_site in raises:
            print(raise_site.render())
        for uncategorized in guard:
            print(uncategorized.render())
        return 0
    if arguments and arguments[0] == "--write-baseline":
        updated = write_counts(document, waits, raises, guard)
        ALLOWLIST_PATH.write_text(dump_document(updated), encoding="utf-8")
        print("lowered the recorded counts; new sites are never written")
        return 0
    messages = evaluate_blocker_gate(waits, raises, document, guard)
    if messages:
        for message in messages:
            print(message, file=sys.stderr)
        return 1
    print(
        f"blockers hold at {len(waits)} operator-wait sites and "
        f"{len(raises)} fail-closed raises, with {len(guard)} uncategorized raises "
        "grandfathered in lifecycle and operation paths"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
