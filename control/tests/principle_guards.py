"""Syntax ratchets for recovery principles; findings are debt, not runtime proofs.

Keys exclude line numbers so formatting cannot renew a budget. --lower only
removes debt. Exceptions require a fixed reason and are still exact-count
ratchets. Bootstrap is deliberately separate from routine maintenance.
"""

from __future__ import annotations

import ast
import re
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from .parsed_sources import parse_file
from .registry_storage import RegistrySnapshot, observe_registry, write_registry

ROOT = Path(__file__).resolve().parents[2]
REASONS = frozenset(
    {
        "security-edge",
        "irreversible-keep",
        "legacy-alias-read",
        "input-validation",
        "service-absent",
        "dependency-unavailable",
        "resource-bound",
    }
)
ALLOWLISTS = {
    "waits": "unbounded-wait",
    "reads": "read-refusal",
    "remedies": "remedy-text",
    "retention": "retention",
    "tests": "anti-principle-tests",
}
TRANSIENT = re.compile(
    r"supersed|stale|gone|missing|lost|interrupt|timeout|busy|restart|transient|unavailable",
    re.IGNORECASE,
)
REMEDY = re.compile(
    r"\b(?:Prepare\b|Run\b[^\n]*\bagain\b|Review\b[^\n]*\bagain\b|Retry\b|Contact\b|manually\b)"
)
WAIT_STATES = {
    "waiting-for-operator",
    "needs-operator",
    "NEEDS_OPERATOR",
    "WAITING_FOR_OPERATOR",
}


@dataclass(frozen=True)
class Site:
    path: str
    function: str
    kind: str
    line: int

    @property
    def key(self) -> tuple[str, str, str]:
        return self.path, self.function, self.kind


def name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def literals(node: ast.AST) -> set[str]:
    return {str(n.value) for n in ast.walk(node) if isinstance(n, ast.Constant)} | {
        n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
    }


def local_nodes(node: ast.AST):
    """Walk a scope without accidentally charging nested helpers to its owner."""
    yield node
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            yield from local_nodes(child)


def positive_comparisons(node: ast.AST, positive: bool = True):
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        yield from positive_comparisons(node.operand, not positive)
        return
    if isinstance(node, ast.Compare) and positive:
        yield node
    for child in ast.iter_child_nodes(node):
        yield from positive_comparisons(child, positive)


def diagnostic_report_subjects(scope: ast.AST) -> set[str]:
    """File-backed reports with asserted failure causes are evidence, not job state.

    Require both the JSON file read and positive cause evidence on the same
    subject. A variable named report, a state field, or a bare failed status
    alone must still be scanned.
    """
    file_reports: set[str] = set()
    for node in ast.walk(scope):
        value = node.value if isinstance(node, ast.Assign) else None
        target = node.targets[0] if isinstance(node, ast.Assign) else None
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and len(node.args) == 1
        ):
            value, target = node.args[0], node.func.value
        if (
            isinstance(target, ast.Name)
            and isinstance(value, ast.Call)
            and ast.unparse(value.func) == "json.loads"
            and len(value.args) == 1
            and isinstance(value.args[0], ast.Call)
            and name(value.args[0].func) == "read_text"
        ):
            file_reports.add(target.id)

    subjects: set[str] = set()
    for node in local_nodes(scope):
        if not isinstance(node, ast.Assert):
            continue
        fields: list[ast.expr] = []
        if isinstance(node.test, ast.Subscript):
            fields.append(node.test)
        for comparison in positive_comparisons(node.test):
            if isinstance(comparison, ast.Compare) and all(
                isinstance(op, ast.In) for op in comparison.ops
            ):
                fields.extend(comparison.comparators)
        for field in fields:
            if (
                isinstance(field, ast.Subscript)
                and isinstance(field.slice, ast.Constant)
                and field.slice.value == "cause"
                and isinstance(field.value, ast.Subscript)
                and isinstance(field.value.slice, ast.Constant)
                and field.value.slice.value == "failure"
            ):
                subject = field.value.value
                root = subject.value if isinstance(subject, ast.Subscript) else subject
                if isinstance(root, ast.Name) and root.id in file_reports:
                    subjects.add(ast.dump(subject))
    return subjects


def is_diagnostic_report_status(comparison: ast.Compare, subjects: set[str]) -> bool:
    return any(
        isinstance(operand, ast.Subscript)
        and isinstance(operand.slice, ast.Constant)
        and operand.slice.value == "status"
        and ast.dump(operand.value) in subjects
        for operand in (comparison.left, *comparison.comparators)
    )


def status_code(call: ast.Call) -> int | None:
    for kw in call.keywords:
        if kw.arg == "status_code":
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, int):
                return kw.value.value
            match = re.search(r"HTTP_(\d{3})", ast.unparse(kw.value))
            return int(match[1]) if match else None
    if (
        name(call.func) == "HTTPException"
        and call.args
        and isinstance(call.args[0], ast.Constant)
    ):
        value = call.args[0].value
        return value if isinstance(value, int) else None
    return None


def bounded_loop(node: ast.While) -> bool:
    # A deadline must participate in control flow, not merely appear in a log.
    tests = [node.test] + [n.test for n in local_nodes(node) if isinstance(n, ast.If)]
    return any(
        re.search(
            r"deadline|timeout|expires|remaining|attempt|retry_budget|max_retries",
            ast.unparse(test),
            re.IGNORECASE,
        )
        for test in tests
    )


def _observation_deadline_guard(tree: ast.Module, path: str) -> bool:
    """Resolve the sole owned finite monotonic guard, rather than its name alone."""
    if path != "src/cluster_profiles/observation_transfer_reader.py":
        return False
    guards = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "check_observation_deadline"
    ]
    if len(guards) != 1 or guards[0] not in tree.body:
        return False
    guard = guards[0]
    if (
        not isinstance(guard, ast.FunctionDef)
        or guard.decorator_list
        or [arg.arg for arg in guard.args.args] != ["deadline"]
        or guard.args.posonlyargs
        or guard.args.kwonlyargs
        or guard.args.vararg
        or guard.args.kwarg
        or guard.args.defaults
    ):
        return False
    imported = {
        alias.asname or alias.name: alias.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    if imported.get("time") != "time" or imported.get("math") != "math":
        return False
    protected = {"time", "math", "TimeoutError", "check_observation_deadline"}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, (ast.Store, ast.Del))
            and node.id in protected
        ):
            return False
        if isinstance(node, ast.arg) and node.arg in protected:
            return False
        if (
            isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in protected
            and node is not guard
        ):
            return False
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store)
            and ast.unparse(node).split(".")[0] in protected
        ):
            return False
        if isinstance(node, ast.Import) and any(
            (a.asname or a.name) in protected
            and (node not in tree.body or a.name not in {"time", "math"})
            for a in node.names
        ):
            return False
        if isinstance(node, ast.ImportFrom) and any(
            a.name == "*" or (a.asname or a.name) in protected for a in node.names
        ):
            return False
        if isinstance(node, ast.ExceptHandler) and node.name in protected:
            return False
    if len(guard.body) != 2:
        return False
    finite, expiry = guard.body
    if not isinstance(finite, ast.If) or not isinstance(expiry, ast.If):
        return False
    return (
        ast.unparse(finite.test) == "not math.isfinite(deadline)"
        and len(finite.body) == 1
        and isinstance(finite.body[0], ast.Raise)
        and not finite.orelse
        and ast.unparse(expiry.test) == "time.monotonic() >= deadline"
        and len(expiry.body) == 1
        and isinstance(expiry.body[0], ast.Raise)
        and isinstance(expiry.body[0].exc, ast.Call)
        and isinstance(expiry.body[0].exc.func, ast.Name)
        and expiry.body[0].exc.func.id == "TimeoutError"
        and not expiry.orelse
    )


def _calls_observation_deadline(
    node: ast.While, receiver: ast.FunctionDef | None
) -> bool:
    if receiver is None or receiver.name != "receive_observation" or not node.body:
        return False
    keyword_arguments = [arg.arg for arg in receiver.args.kwonlyargs]
    if (
        "deadline" not in keyword_arguments
        or receiver.args.kw_defaults[keyword_arguments.index("deadline")] is not None
    ):
        return False
    for part in ast.walk(receiver):
        if (
            isinstance(part, ast.Name)
            and isinstance(part.ctx, (ast.Store, ast.Del))
            and part.id == "deadline"
        ):
            return False
        if isinstance(part, (ast.Import, ast.ImportFrom)) and any(
            (a.asname or a.name) == "deadline" for a in part.names
        ):
            return False
        if isinstance(part, ast.ExceptHandler) and part.name == "deadline":
            return False
        if (
            isinstance(part, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and part is not receiver
            and part.name == "deadline"
        ):
            return False
    first = node.body[0]
    return (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Call)
        and isinstance(first.value.func, ast.Name)
        and first.value.func.id == "check_observation_deadline"
        and not first.value.keywords
        and len(first.value.args) == 1
        and isinstance(first.value.args[0], ast.Name)
        and first.value.args[0].id == "deadline"
    )


def relative_import_owner(path: str, node: ast.ImportFrom) -> Path | None:
    """Resolve concrete relative owners, including a module just split into a package."""
    if not node.level or node.module is None:
        return None
    owner = ROOT / path
    if not owner.exists() and owner.with_suffix("").is_dir():
        owner = owner.with_suffix("") / "__moved__.py"
    parent = owner.parent
    for _ in range(node.level - 1):
        parent = parent.parent
    return parent.joinpath(*node.module.split("."))


def imported_helpers(
    tree: ast.Module, path: str
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Follow explicit sibling helper imports so extraction cannot hide GET refusals."""
    helpers = {}
    for imported in tree.body:
        if not isinstance(imported, ast.ImportFrom) or imported.level != 1:
            continue
        owner = relative_import_owner(path, imported)
        if (
            owner is None
            or not (owner.parent.parent / "__init__.py").is_file()
            or not owner.with_suffix(".py").is_file()
        ):
            continue
        definitions = {
            node.name: node
            for node in ast.parse(owner.with_suffix(".py").read_text()).body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        for alias in imported.names:
            if alias.name in definitions:
                helpers[alias.asname or alias.name] = definitions[alias.name]
    return helpers


def scan_source(
    source: str, *, path: str, mode: str, tree: ast.Module | None = None
) -> list[Site]:
    candidates = {
        "waits": r"\b(?:while|subprocess|urllib)\b|\b(?:run|call|check_call|check_output|urlopen|wait|communicate|join|poll)\s*\(",
        "reads": r"\.(?:get|route|api_route)\s*\(",
        "tests": r"\bassert\b|\bdef\s+test_",
        "raises": r"\braise\b",
    }
    if mode in candidates and not re.search(candidates[mode], source):
        return []
    tree = ast.parse(source) if tree is None else tree
    sites: list[Site] = []
    lines = source.splitlines()
    observation_deadline = _observation_deadline_guard(tree, path)
    aliases: dict[str, str] = {}
    instrumentation: dict[str, str] = {}
    for imported in tree.body:
        if isinstance(imported, ast.ImportFrom) and imported.module in {
            "sqlalchemy",
            "sqlalchemy.event",
        }:
            instrumentation.update(
                {
                    a.asname or a.name: f"{imported.module}.{a.name}"
                    for a in imported.names
                    if a.name != "*"
                }
            )
        elif isinstance(imported, ast.Import):
            instrumentation.update(
                {
                    a.asname or a.name.split(".")[0]: (
                        a.name if a.asname else a.name.split(".")[0]
                    )
                    for a in imported.names
                    if a.name in {"sqlalchemy", "sqlalchemy.event"}
                }
            )
        if isinstance(imported, ast.ImportFrom) and imported.module in {
            "subprocess",
            "urllib.request",
        }:
            aliases.update(
                {
                    a.asname or a.name: f"{imported.module}.{a.name}"
                    for a in imported.names
                }
            )
        elif isinstance(imported, ast.Import):
            aliases.update({a.asname: a.name for a in imported.names if a.asname})

    resource_errors = {
        alias.asname or alias.name
        for imported in tree.body
        if isinstance(imported, ast.ImportFrom)
        and (
            (imported.module == "operation_api" and imported.level == 1)
            or relative_import_owner(path, imported)
            == ROOT / "control/src/vonk_control/operation_api"
        )
        for alias in imported.names
        if alias.name == "_OperationResponseTooLarge"
    }
    # An unrelated local class or assignment cannot borrow the owner's name.
    resource_errors -= (
        {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
        }
        | {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        }
        | {node.arg for node in ast.walk(tree) if isinstance(node, ast.arg)}
    )

    class Collector(ast.NodeVisitor):
        def __init__(self):
            self.scope: list[str] = []
            self.get = False
            self.context = ""
            self.resource_refusal = False
            self.diagnostic_reports: set[str] = set()

        def add(self, node: ast.AST, kind: str):
            sites.append(
                Site(
                    path,
                    ".".join(self.scope) or "<module>",
                    kind,
                    getattr(node, "lineno", 1),
                )
            )

        def visit_ClassDef(self, node: ast.ClassDef):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef):
            old_reports = self.diagnostic_reports
            self.diagnostic_reports = (
                diagnostic_report_subjects(node) if mode == "tests" else set()
            )
            old_receiver = self.receiver
            self.receiver = node if isinstance(node, ast.FunctionDef) else None
            old_get, old_context = self.get, self.context
            self.scope.append(node.name)
            self.get = any(
                isinstance(d, ast.Call)
                and (
                    name(d.func) == "get"
                    or (name(d.func) in {"route", "api_route"} and "GET" in literals(d))
                )
                for d in node.decorator_list
            )
            self.context = (
                (node.name + " " + "\n".join(lines[node.lineno - 1 : node.end_lineno]))
                if mode == "tests"
                else ""
            )
            self.generic_visit(node)
            if mode == "tests" and node.name.startswith("test_"):
                calls = sorted(
                    (n for n in local_nodes(node) if isinstance(n, ast.Call)),
                    key=lambda n: (n.lineno, n.col_offset),
                )
                ending = re.compile(
                    r"(?:^|_)(cancel|stop|retire|supersede|uninstall|remove)(?:_|$)"
                )
                fresh = re.compile(
                    r"(?:^|_)(start|load|apply|prepare|request|create|enqueue|admit|activate|submit)(?:_|$)"
                )
                shadowed = {
                    n.id
                    for n in local_nodes(node)
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
                } | {n.arg for n in ast.walk(node.args) if isinstance(n, ast.arg)}
                shadowed.update(
                    n.id
                    for statement in tree.body
                    if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign))
                    for n in ast.walk(statement)
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
                )

                def is_instrumentation(call: ast.Call) -> bool:
                    head, dot, tail = ast.unparse(call.func).partition(".")
                    canonical = instrumentation.get(head, head) + (
                        dot + tail if dot else ""
                    )
                    return (
                        head in instrumentation
                        and head not in shadowed
                        and canonical == "sqlalchemy.event.remove"
                    )

                ends = [
                    n
                    for n in calls
                    if ending.search(name(n.func)) and not is_instrumentation(n)
                ]
                if ends and not any(
                    name(n.func) == "assert_ended_without_blocking" for n in calls
                ):
                    last_end = max((n.lineno, n.col_offset) for n in ends)
                    if not any(
                        (n.lineno, n.col_offset) > last_end
                        and fresh.search(name(n.func))
                        for n in calls
                    ):
                        self.add(node, "ending-without-fresh-request")
            self.scope.pop()
            self.diagnostic_reports = old_reports
            self.get, self.context = old_get, old_context
            self.receiver = old_receiver

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ExceptHandler(self, node: ast.ExceptHandler):
            old = self.resource_refusal
            self.resource_refusal = (
                isinstance(node.type, ast.Name) and node.type.id in resource_errors
            )
            self.generic_visit(node)
            self.resource_refusal = old

        def visit_Raise(self, node: ast.Raise):
            if (
                mode == "raises"
                and isinstance(node.exc, ast.Call)
                and name(node.exc.func)
                in {
                    "HTTPException",
                    "RuntimeError",
                    "ValueError",
                    "TypeError",
                    "KeyError",
                    "OSError",
                    "FileNotFoundError",
                    "TimeoutError",
                    "AssertionError",
                    "PermissionError",
                    "Exception",
                    "NotImplementedError",
                }
            ):
                self.add(node, "builtin-or-http-raise")
            self.generic_visit(node)

        receiver: ast.FunctionDef | None = None
        deadline_caught = False

        def visit_Try(self, node: ast.Try | ast.TryStar):
            old = self.deadline_caught
            # Conservative: no handler surrounding this loop may swallow expiry.
            self.deadline_caught = old or bool(node.handlers)
            self.generic_visit(node)
            self.deadline_caught = old

        visit_TryStar = visit_Try

        def visit_While(self, node: ast.While):
            helper_bound = (
                observation_deadline
                and not self.deadline_caught
                and _calls_observation_deadline(node, self.receiver)
            )
            if mode == "waits" and not bounded_loop(node) and not helper_bound:
                self.add(node, "loop-without-deadline")
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call):
            if mode == "waits":
                if name(node.func) not in {
                    "run",
                    "call",
                    "check_call",
                    "check_output",
                    "urlopen",
                    "wait",
                    "communicate",
                    "join",
                    "poll",
                } | set(aliases):
                    self.generic_visit(node)
                    return
                func = ast.unparse(node.func)
                head, dot, tail = func.partition(".")
                func = aliases.get(head, head) + (dot + tail if dot else "")
                blocking = func in {
                    "subprocess.run",
                    "subprocess.call",
                    "subprocess.check_call",
                    "subprocess.check_output",
                    "urllib.request.urlopen",
                    "urlopen",
                } or name(node.func) in {"wait", "communicate", "join", "poll"}
                if (
                    blocking
                    and not any(
                        k.arg in {"timeout", "deadline"}
                        and not (
                            isinstance(k.value, ast.Constant) and k.value.value is None
                        )
                        for k in node.keywords
                    )
                    and (
                        not node.args
                        or (
                            isinstance(node.args[0], ast.Constant)
                            and node.args[0].value is None
                        )
                    )
                ):
                    self.add(node, "wait-without-timeout")
                elif (
                    blocking
                    and func
                    in {
                        "subprocess.run",
                        "subprocess.call",
                        "subprocess.check_call",
                        "subprocess.check_output",
                        "urllib.request.urlopen",
                        "urlopen",
                    }
                    and not any(
                        k.arg == "timeout"
                        and not (
                            isinstance(k.value, ast.Constant) and k.value.value is None
                        )
                        for k in node.keywords
                    )
                ):
                    self.add(node, "call-without-timeout")
            if mode == "reads" and self.get:
                code = status_code(node)
                if (
                    code is not None
                    and 400 <= code <= 599
                    and code not in {401, 403, 404}
                ):
                    self.add(
                        node,
                        "get-resource-refusal"
                        if self.resource_refusal and code == 503
                        else "get-refusal",
                    )
            self.generic_visit(node)

        def visit_Constant(self, node: ast.Constant):
            if (
                mode == "remedies"
                and isinstance(node.value, str)
                and REMEDY.search(node.value)
            ):
                self.add(node, "imperative-remedy")

        def visit_Assert(self, node: ast.Assert):
            if mode != "tests":
                return
            for comparison in positive_comparisons(node.test):
                if not isinstance(comparison, ast.Compare) or not any(
                    isinstance(op, ast.Eq | ast.Is | ast.In) for op in comparison.ops
                ):
                    continue
                values = literals(comparison)
                if values & WAIT_STATES:
                    self.add(node, "operator-wait-assertion")
                if (
                    "status_code" in values
                    and values & {"409", "422", "500", "503"}
                    and re.search(r"\.get\s*\(", self.context)
                ):
                    self.add(node, "refused-read-assertion")
                if (
                    values & {"failed", "FAILED"}
                    and not is_diagnostic_report_status(
                        comparison, self.diagnostic_reports
                    )
                    and TRANSIENT.search(self.context)
                    and not re.search(
                        r"assert[^\n]*(?:reason_code|\.code\b)", self.context
                    )
                ):
                    self.add(node, "transient-failed-assertion")
                if values & {"withdrawn", "route_error"} and TRANSIENT.search(
                    self.context
                ):
                    self.add(node, "observation-withdrawal-assertion")

    Collector().visit(tree)
    if mode == "reads":
        functions = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        local_functions = set(functions)
        functions.update(imported_helpers(tree, path))
        bad = {
            key
            for key, node in functions.items()
            if any(
                isinstance(n, ast.Call)
                and (code := status_code(n)) is not None
                and 400 <= code <= 599
                and code not in {401, 403, 404}
                for n in local_nodes(node)
            )
        }
        calls = {
            key: {name(n.func) for n in local_nodes(node) if isinstance(n, ast.Call)}
            for key, node in functions.items()
        }
        while True:
            propagated = bad | {key for key, targets in calls.items() if targets & bad}
            if propagated == bad:
                break
            bad = propagated
        for key, node in functions.items():
            if key not in local_functions:
                continue
            is_get = any(
                isinstance(d, ast.Call)
                and (
                    name(d.func) == "get"
                    or (name(d.func) in {"route", "api_route"} and "GET" in literals(d))
                )
                for d in node.decorator_list
            )
            if is_get and calls[key] & bad:
                sites.append(Site(path, key, "get-helper-refusal", node.lineno))
    return sites


def scan_shell(source: str, *, path: str) -> list[Site]:
    return [
        Site(path, "<shell>", "shell-loop-without-deadline", line)
        for line, text in enumerate(source.splitlines(), 1)
        if re.search(r"\b(?:while\s+(?:true|:)|until\s+)", text)
        and not re.search(r"deadline|timeout|attempt|SECONDS", text.split("#", 1)[0])
    ]


def scan_compose_shell(source: str, *, path: str) -> list[Site]:
    """Inspect actual shell entrypoints, not arbitrary YAML or command strings."""
    document = yaml.safe_load(source)
    if not isinstance(document, dict):
        return []
    services = document.get("services", {})
    if not isinstance(services, dict):
        return []
    sites: list[Site] = []
    for service_name, service in services.items():
        if not isinstance(service, dict):
            continue
        for field in ("entrypoint", "command"):
            command = service.get(field)
            if (
                isinstance(command, list)
                and len(command) >= 3
                and command[0] in {"sh", "/bin/sh", "bash", "/bin/bash"}
                and command[1] == "-c"
                and isinstance(command[2], str)
            ):
                body = command[2]
                has_loop = re.search(r"\b(?:while\s+(?:true|:)|until\s+)", body)
                has_exit_bound = re.search(
                    r"if\s+\[[^\]]*(?:attempt|deadline|remaining|SECONDS)[^\]]*\]"
                    r"\s*;\s*then\b.*?\b(?:exit|break|return)\b",
                    body,
                    re.DOTALL,
                )
                if has_loop and not has_exit_bound:
                    sites.append(
                        Site(
                            path,
                            f"{service_name}.{field}",
                            "shell-loop-without-deadline",
                            1,
                        )
                    )
    return sites


def scan_rust_remedies(source: str, *, path: str) -> list[Site]:
    sites = []
    for match in re.finditer(r'"(?:\\.|[^"\\])*"', source):
        if REMEDY.search(match[0]):
            functions = list(re.finditer(r"\bfn\s+(\w+)", source[: match.start()]))
            sites.append(
                Site(
                    path,
                    functions[-1][1] if functions else "<module>",
                    "imperative-remedy",
                    source[: match.start()].count("\n") + 1,
                )
            )
    return sites


def scan_rust(source: str, *, path: str) -> list[Site]:
    # Balanced braces isolate each loop; strings/comments cannot provide a bound.
    clean = re.sub(
        r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"',
        lambda m: ('"_"' if m[0].startswith('"') else "") + "\n" * m[0].count("\n"),
        source,
        flags=re.DOTALL,
    )
    sites = []
    for match in re.finditer(r"\b(?:loop|while\s+[^\n{]+)\s*\{", clean):
        start = match.end()
        depth, end = 1, start
        while end < len(clean) and depth:
            depth += (clean[end] == "{") - (clean[end] == "}")
            end += 1
        body = clean[start:end]
        if not re.search(
            r"deadline|timeout|expires|remaining|max_attempts|retry_budget",
            match[0] + body,
        ):
            prefix = clean[: match.start()]
            functions = list(re.finditer(r"\bfn\s+(\w+)", prefix))
            sites.append(
                Site(
                    path,
                    functions[-1][1] if functions else "<module>",
                    "rust-loop-without-deadline",
                    prefix.count("\n") + 1,
                )
            )
    for match in re.finditer(r"\.(?:wait|recv|notified|join)\s*\(\s*\)", clean):
        prefix = clean[: match.start()]
        statement = prefix[max(prefix.rfind(";"), prefix.rfind("{")) + 1 :]
        if re.search(r"(?:timeout|timeout_at)\s*\(", statement):
            continue
        functions = list(re.finditer(r"\bfn\s+(\w+)", prefix))
        sites.append(
            Site(
                path,
                functions[-1][1] if functions else "<module>",
                "rust-wait-without-deadline",
                prefix.count("\n") + 1,
            )
        )
    return sites


def scan_retention(modules: Sequence[tuple[str, ast.Module]]) -> list[Site]:
    """Inventory mapped tables lacking a syntactically linked delete/prune path.

    ORM deletes, bulk deletes, query.delete and explicit SQL count. A foreign-key
    cascade alone is not a retention policy for a parent that is never deleted.
    """
    tables: list[tuple[str, str, str, int]] = []
    deleted: set[str] = set()
    for path, tree in modules:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for statement in node.body:
                    if (
                        isinstance(statement, ast.Assign)
                        and any(
                            isinstance(t, ast.Name) and t.id == "__tablename__"
                            for t in statement.targets
                        )
                        and isinstance(statement.value, ast.Constant)
                        and isinstance(statement.value.value, str)
                    ):
                        tables.append(
                            (path, node.name, statement.value.value, node.lineno)
                        )
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                aliases: dict[str, set[str]] = {}
                for part in local_nodes(node):
                    if isinstance(part, ast.Assign):
                        refs = {
                            n.id
                            for n in ast.walk(part.value)
                            if isinstance(n, ast.Name)
                        }
                        for target in part.targets:
                            if isinstance(target, ast.Name):
                                aliases[target.id] = refs
                    if isinstance(part, ast.Call) and name(part.func) == "delete":
                        refs = {n.id for n in ast.walk(part) if isinstance(n, ast.Name)}
                        deleted.update(refs)
                        for ref in refs:
                            deleted.update(aliases.get(ref, set()))
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                deleted.update(
                    re.findall(
                        r"DELETE\s+FROM\s+[\"`]?([\w]+)", node.value, re.IGNORECASE
                    )
                )
    return [
        Site(path, cls, "table-without-delete-path", line)
        for path, cls, table, line in tables
        if cls not in deleted and table not in deleted
    ]


def source_files(mode: str) -> list[Path]:
    roots = (
        ("control/tests", "tests", "agent_protocol/tests")
        if mode == "tests"
        else ("control/src", "rust/crates", "scripts", "src")
    )
    files = []
    for root in roots:
        for path in (ROOT / root).rglob("*"):
            if not path.is_file() or any(
                p
                in {
                    "__pycache__",
                    ".venv",
                    "target",
                    "node_modules",
                    "generated_control",
                }
                for p in path.parts
            ):
                continue
            if path.suffix in {".py", ".rs", ".sh"} or (
                not path.suffix and path.read_bytes().startswith(b"#!")
            ):
                files.append(path)
    if mode == "waits":
        files.extend((ROOT / "deploy/compose").rglob("compose.yaml"))
    return sorted(files)


def scan_sites(mode: str) -> list[Site]:
    sites: list[Site] = []
    modules = []
    for path in source_files(mode):
        relative = path.relative_to(ROOT).as_posix()
        source = path.read_text()
        if path.suffix == ".yaml":
            sites.extend(scan_compose_shell(source, path=relative))
            continue
        if path.suffix == ".rs":
            if mode == "waits":
                sites.extend(scan_rust(source, path=relative))
            elif mode == "remedies":
                sites.extend(scan_rust_remedies(source, path=relative))
            continue
        if path.suffix == ".sh" or (
            source.startswith("#!") and "python" not in source.splitlines()[0]
        ):
            if mode == "waits":
                sites.extend(scan_shell(source, path=relative))
            continue
        if mode == "reads" and not re.search(r"\.(?:get|route|api_route)\s*\(", source):
            continue
        # Test trees are consumed once by this scanner. Retaining every test
        # AST makes pytest GC repeatedly traverse millions of unused nodes.
        parsed = parse_file(path, cache=mode != "tests")
        if mode == "retention":
            modules.append((relative, parsed.tree))
        else:
            sites.extend(
                scan_source(parsed.source, path=relative, mode=mode, tree=parsed.tree)
            )
    return scan_retention(modules) if mode == "retention" else sites


def load_allowlist(path: Path) -> dict:
    return observe_registry(path, _validate_registry)


def _validate_registry(doc: dict, path: Path) -> dict:
    if (
        doc.get("schema") != 1
        or not isinstance(doc.get("debt"), list)
        or not isinstance(doc.get("exceptions"), list)
    ):
        raise ValueError("expected schema 1 debt and exceptions")
    seen = set()
    for group in ("debt", "exceptions"):
        for entry in doc[group]:
            key = tuple(entry.get(k) for k in ("path", "function", "kind"))
            if not all(isinstance(k, str) and k for k in key) or key in seen:
                raise ValueError("invalid or duplicate site key")
            seen.add(key)
            if type(entry.get("count")) is not int or entry["count"] < 1:
                raise ValueError("count must be a positive integer")
            if group == "exceptions" and (
                entry.get("reason") not in REASONS
                or not str(entry.get("justification", "")).strip()
            ):
                raise ValueError("exception needs a fixed reason")
    return doc


def relocate(document: dict, moves=None) -> dict:
    from .package_moves import PackageMoves

    moves = moves or PackageMoves(ROOT, document.get("content_identities"))
    return {
        **document,
        **{
            group: [
                {
                    **entry,
                    "function": moves.scope(entry["path"], entry["function"]),
                    "path": moves.function(
                        entry["path"],
                        entry["function"],
                        lambda source, name, entry=entry: any(
                            site.function == name and site.kind == entry["kind"]
                            for mode in ALLOWLISTS
                            for site in (
                                (
                                    scan_rust(source, path=entry["path"])
                                    if mode == "waits"
                                    else scan_rust_remedies(source, path=entry["path"])
                                    if mode == "remedies"
                                    else []
                                )
                                if entry["path"].endswith(".rs")
                                else scan_retention(
                                    [(entry["path"], ast.parse(source))]
                                )
                                if mode == "retention"
                                else scan_source(source, path=entry["path"], mode=mode)
                            )
                        ),
                    ),
                }
                for entry in document[group]
            ]
            for group in ("debt", "exceptions")
        },
    }


def evaluate_gate(sites: Sequence[Site], document: dict) -> list[str]:
    document = relocate(document)
    actual = Counter(s.key for s in sites)
    expected = {
        (e["path"], e["function"], e["kind"]): e["count"]
        for group in ("debt", "exceptions")
        for e in document[group]
    }
    return [
        f"{key}: {expected.get(key, 0)} -> {actual.get(key, 0)}; new sites need a fixed reason; lower stale debt"
        for key in sorted(actual.keys() | expected.keys())
        if actual.get(key, 0) != expected.get(key, 0)
    ]


def lower(document: dict, sites: Sequence[Site]) -> dict:
    document = relocate(document)
    actual = Counter(s.key for s in sites)
    expected = {
        (e["path"], e["function"], e["kind"]): e["count"]
        for group in ("debt", "exceptions")
        for e in document[group]
    }
    if any(count > expected.get(key, 0) for key, count in actual.items()):
        raise ValueError("cannot increase debt; new sites need a reviewed fixed reason")
    result = {**document}
    for group in ("debt", "exceptions"):
        result[group] = [
            {**e, "count": actual[(e["path"], e["function"], e["kind"])]}
            for e in document[group]
            if actual[(e["path"], e["function"], e["kind"])]
        ]
    from .package_moves import record_identities

    return record_identities(result, ROOT)


def history_gate(document: dict, previous: dict, moves=None) -> list[str]:
    """Editing the allowlist cannot raise an existing allowance or add debt."""
    previous = relocate(previous, moves)
    old = {
        (group, e["path"], e["function"], e["kind"]): e["count"]
        for group in ("debt", "exceptions")
        for e in previous[group]
    }
    messages = []
    for group in ("debt", "exceptions"):
        for entry in document[group]:
            key = (group, entry["path"], entry["function"], entry["kind"])
            if key in old and entry["count"] > old[key]:
                messages.append(f"allowance increased: {key}")
            elif key not in old and group == "debt":
                messages.append(f"new debt is forbidden: {key}; use a fixed reason")
    return messages


def main(mode: str, argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    path = ROOT / "tools" / (ALLOWLISTS[mode] + "-allowlist")
    sites = scan_sites(mode)
    document = load_allowlist(path)
    if args == ["--lower"]:
        write_registry(
            path,
            lower(document, sites),
            base=document if isinstance(document, RegistrySnapshot) else None,
        )
        return 0
    if args:
        raise ValueError("only --lower is supported")
    messages = evaluate_gate(sites, document)
    for message in messages:
        print(message, file=sys.stderr)
    print(f"{mode}: {len(sites)} site(s), {len(messages)} violation(s)")
    return int(bool(messages))
