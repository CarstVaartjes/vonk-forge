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
a class defined there whose name ends in ``Conflict``, ``Error``, ``Refused``,
``Busy`` or ``Invalid`` is keyed ``(path, class, function, code)``, ``code``
being the leading dotted token of the message (``run-switch.plan_blocked``) or a
slug of its first words.  Each key belongs to one *family* with a category:
``security-edge``, ``input-validation``, ``already-retried`` (all need a written
reason) or ``bookkeeping-debt``, whose total is capped by
``debt_ceiling.total`` and may only fall.  A raise a family does not list fails:
a new one must choose a category in a PR a reviewer can see.  Builtin
``ValueError`` / ``KeyError`` / ``TypeError`` raises are out of scope.

``python -m control.tests.blocker_boundaries --list`` prints both scans;
``--write-baseline`` rewrites the counts after a site was removed (a new site is
never written automatically: it needs a verdict, a category and a reason).
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_SOURCE_ROOT = REPO_ROOT / "control" / "src"
RUST_SOURCE_ROOT = REPO_ROOT / "rust" / "crates" / "vonk-agent" / "src"
ALLOWLIST_PATH = REPO_ROOT / "tools" / "blocker-allowlist.json"

WAIT_STATE = "waiting-for-operator"
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
VERDICTS = frozenset({"KEEP", "SELF-HEAL", "FIX-ACTION", "DERIVED"})
#: The real operator surfaces: ``resume``/``retire`` (POST /api/jobs/{id}/...),
#: ``retry`` (fleet profile), ``stop`` (the one-shot stop route) and
#: ``automatic`` (a scheduled retry the Controller owns).
ACTION_SURFACES = ("resume", "retire", "retry", "stop", "automatic")
_STATE_CALLS = frozenset({"_finish", "_set_application_state", "_set_state"})

CATEGORIES = frozenset(
    {"security-edge", "input-validation", "already-retried", "bookkeeping-debt"}
)
_RAISE_SUFFIXES = ("Conflict", "Error", "Refused", "Busy", "Invalid", "NotFound")
#: Fail-closed classes that do not follow the suffix convention.
_EXTRA_RAISE_CLASSES = frozenset({"StaleAgentAttempt"})


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

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return (self.path, self.exception_class, self.function, self.code)

    def render(self) -> str:
        return (
            f"{self.path}:{self.line}: raise {self.exception_class} in "
            f"{self.function}: {self.code}"
        )


# --------------------------------------------------------------------- waits


def _is_wait_literal(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == WAIT_STATE


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


def scan_python_waits(source: str, *, path: str) -> list[WaitSite]:
    tree = ast.parse(source)
    collector = _WaitCollector(path, _wait_constant_names(tree))
    collector.visit(tree)
    return sorted(collector.sites, key=lambda site: site.line)


_RUST_FN = re.compile(r"^\s*(?:pub(?:\([a-z]+\))?\s+)?(?:async\s+)?fn\s+(\w+)")
_RUST_IMPL = re.compile(r"^\s*impl(?:<[^>]*>)?\s+(?:[\w:<>, ]+\s+for\s+)?(\w+)")
#: The one constructor of the result; each of its callers is its own site.
_RUST_CONSTRUCTOR = "waiting_for_operator"
_RUST_WAIT_RESULT = re.compile(
    r'state:\s*"waiting-for-operator"|AgentResultState::WaitingForOperator'
)
_RUST_WAIT_CALL = re.compile(r"\bwaiting_for_operator\(")


def scan_rust_waits(source: str, *, path: str) -> list[WaitSite]:
    """Result states and constructor calls; ``fn waiting_for_operator`` itself is
    the one constructor and counts as its own site."""

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
            if declared.group(1) != _RUST_CONSTRUCTOR and not line.startswith("fn "):
                function = f"{owner}::{function}" if owner else function
        stripped = line.strip()
        if stripped.startswith("//") or function == _RUST_CONSTRUCTOR:
            continue
        if _RUST_WAIT_RESULT.search(line) or (
            _RUST_WAIT_CALL.search(line) and not declared
        ):
            sites.append(WaitSite(path, function, RUST_RESULT, number))
    return sites


def scan_waits(
    control_root: Path = CONTROL_SOURCE_ROOT, rust_root: Path = RUST_SOURCE_ROOT
) -> list[WaitSite]:
    sites: list[WaitSite] = []
    for module in sorted(control_root.rglob("*.py")):
        relative = module.relative_to(REPO_ROOT).as_posix()
        sites.extend(
            scan_python_waits(module.read_text(encoding="utf-8"), path=relative)
        )
    for module in sorted(rust_root.rglob("*.rs")):
        relative = module.relative_to(REPO_ROOT).as_posix()
        sites.extend(scan_rust_waits(module.read_text(encoding="utf-8"), path=relative))
    return sites


# --------------------------------------------------------------------- raises


def exception_classes(trees: Sequence[ast.Module]) -> frozenset[str]:
    """Names of the classes defined in the scanned tree that a raise can name."""

    return _EXTRA_RAISE_CLASSES | frozenset(
        node.name
        for tree in trees
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name.endswith(_RAISE_SUFFIXES)
    )


def _leading_text(node: ast.AST) -> str | None:
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
                    )
                )
        self.generic_visit(node)


def scan_raises(
    paths: Sequence[str], root: Path = CONTROL_SOURCE_ROOT
) -> list[RaiseSite]:
    """Raises in ``paths`` of classes defined anywhere under ``root``."""

    trees = {
        module: ast.parse(module.read_text(encoding="utf-8"))
        for module in sorted(root.rglob("*.py"))
    }
    classes = exception_classes(list(trees.values()))
    audited = set(paths)
    sites: list[RaiseSite] = []
    for module, tree in trees.items():
        relative = module.relative_to(REPO_ROOT).as_posix()
        if relative in audited:
            collector = _RaiseCollector(relative, classes)
            collector.visit(tree)
            sites.extend(collector.sites)
    return sorted(sites, key=lambda site: (site.path, site.line))


def scan_raise_source(
    source: str, *, path: str, classes: frozenset[str]
) -> list[RaiseSite]:
    collector = _RaiseCollector(path, classes)
    collector.visit(ast.parse(source))
    return collector.sites


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
    for field in ("max_debt", "debt_ceiling", "scope"):
        if field not in document:
            raise ValueError(f"{path}: missing {field}")
    waits = document.get("operator_waits")
    families = document.get("fail_closed")
    if not isinstance(waits, list) or not isinstance(families, list):
        raise TypeError(f"{path}: needs operator_waits and fail_closed arrays")
    for index, entry in enumerate(waits):
        where = f"{path} operator_waits[{index}]"
        if not isinstance(entry, dict):
            raise TypeError(f"{where}: entry is not an object")
        for field in ("path", "function", "kind"):
            _require_text(entry, field, where)
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


def raise_paths(document: dict[str, object]) -> list[str]:
    """The audited modules: a raise elsewhere is not yet reviewed."""

    scope = document["scope"]
    paths = scope["raise_paths"]  # type: ignore[index]
    return [str(path) for path in paths]  # type: ignore[attr-defined]


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
    debt = sum(
        count for count, category in listed.values() if category == "bookkeeping-debt"
    )
    ceiling_document = document["debt_ceiling"]
    ceiling = int(ceiling_document["total"])  # type: ignore[index, call-overload]
    if debt > ceiling:
        messages.append(
            f"bookkeeping-debt raises are {debt}, above the ceiling {ceiling}"
        )
    elif debt < ceiling:
        messages.append(
            f"bookkeeping-debt raises are {debt}; lower debt_ceiling.total from {ceiling}"
        )
    return messages


def evaluate_blocker_gate(
    waits: Sequence[WaitSite],
    raises: Sequence[RaiseSite],
    document: dict[str, object],
) -> list[str]:
    return [
        *evaluate_wait_gate(waits, document),
        *evaluate_raise_gate(raises, document),
    ]


def write_counts(
    document: dict[str, object], waits: Sequence[WaitSite], raises: Sequence[RaiseSite]
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
    debt_sites = sum(
        site[4]
        for family in families
        if family["category"] == "bookkeeping-debt"
        for site in family["sites"]
    )
    return {
        **document,
        "max_debt": sum(1 for entry in kept_waits if entry["verdict"] != "KEEP"),
        "debt_ceiling": {**document["debt_ceiling"], "total": debt_sites},  # type: ignore[dict-item]
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
    raises = scan_raises(raise_paths(document))
    if arguments and arguments[0] == "--list":
        for site in waits:
            print(site.render())
        for raise_site in raises:
            print(raise_site.render())
        return 0
    if arguments and arguments[0] == "--write-baseline":
        updated = write_counts(document, waits, raises)
        ALLOWLIST_PATH.write_text(dump_document(updated), encoding="utf-8")
        print("lowered the recorded counts; new sites are never written")
        return 0
    messages = evaluate_blocker_gate(waits, raises, document)
    if messages:
        for message in messages:
            print(message, file=sys.stderr)
        return 1
    print(
        f"blockers hold at {len(waits)} operator-wait sites and "
        f"{len(raises)} fail-closed raises"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
