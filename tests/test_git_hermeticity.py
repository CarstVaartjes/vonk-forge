"""Reject test/helper Git subprocesses that inspect the checkout under test.

Temporary Git histories remain valid fixtures. Historical release/ratchet
checks run only in workflows, with checkout depth and base identities explicit.
This is a syntax guard, not a claim to prove arbitrary dynamic Python safe.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# These are executable workflow checks, never exceptions for tests/helpers.
WORKFLOW_ONLY = {
    "tests/nodes/test_agent_upgrade_repair_systemd.sh": "native package provenance in the systemd acceptance workflow",
    "tests/nodes/test_agent_upgrade_recovery_systemd.sh": "native upgrade package provenance in the systemd acceptance workflow",
}


def git_checkout_reads(source: str) -> list[int]:
    """Track checkout-derived path/command aliases, including helper wrappers."""
    # Most suite files never name Git; avoid parsing those large modules.
    if not re.search(r"['\"](?:[^'\"\n]*/)?git(?:['\"]|\s)", source):
        return []
    tree = ast.parse(source)
    assignments: dict[str, list[ast.expr]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments.setdefault(target.id, []).append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            assignments.setdefault(node.target.id, []).append(node.value)

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            positional = [*node.args.posonlyargs, *node.args.args]
            for arg, default in zip(
                positional[len(positional) - len(node.args.defaults) :],
                node.args.defaults,
                strict=True,
            ):
                assignments.setdefault(arg.arg, []).append(default)
            for arg, default in zip(
                node.args.kwonlyargs, node.args.kw_defaults, strict=True
            ):
                if default is not None:
                    assignments.setdefault(arg.arg, []).append(default)

    def resolve(node: ast.expr, seen: frozenset[str] = frozenset()) -> list[ast.expr]:
        if (
            isinstance(node, ast.Name)
            and node.id in assignments
            and node.id not in seen
        ):
            return [
                resolved
                for value in assignments[node.id]
                for resolved in resolve(value, seen | {node.id})
            ]
        return [node]

    def checkout_path(node: ast.expr, seen: frozenset[str] = frozenset()) -> bool:
        if isinstance(node, ast.Name):
            if node.id in {"ROOT", "WORKSPACE", "REPOSITORY_ROOT", "__file__"}:
                return True
            if node.id in assignments and node.id not in seen:
                return any(
                    checkout_path(value, seen | {node.id})
                    for value in assignments[node.id]
                )
        if isinstance(node, ast.Constant):
            return node.value in {".", ".."} if isinstance(node.value, str) else False
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"cwd", "getcwd"}
        ):
            return True
        return any(
            checkout_path(child, seen)
            for child in ast.iter_child_nodes(node)
            if isinstance(child, ast.expr)
        )

    failures = []
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    wrappers: dict[str, tuple[list[str], set[str]]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        command_arg = (
            node.args[0]
            if node.args
            else next((kw.value for kw in node.keywords if kw.arg == "args"), None)
        )
        if command_arg is None:
            continue
        # Only executed commands, not assertion data or mocked command results.
        name = ast.unparse(node.func).split(".")[-1]
        if name not in {
            "run",
            "call",
            "check_call",
            "check_output",
            "Popen",
            "_run_command",
        }:
            continue
        commands = resolve(command_arg)
        for command in commands:
            if not isinstance(command, (ast.List, ast.Tuple)) or not command.elts:
                continue
            firsts = resolve(command.elts[0])
            if not any(
                isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and Path(first.value).name == "git"
                for first in firsts
            ):
                continue
            cwd = next((kw.value for kw in node.keywords if kw.arg == "cwd"), None)
            git_directory = None
            for index, arg in enumerate(command.elts[:-1]):
                if isinstance(arg, ast.Constant) and arg.value in {
                    "-C",
                    "--git-dir",
                    "--work-tree",
                }:
                    git_directory = command.elts[index + 1]
            target = git_directory if git_directory is not None else cwd
            if target is not None:
                # A helper's parameter is safe only when its callers pass a
                # temporary directory, too: git(ROOT, ...) must be caught.
                for function in functions:
                    positional = [
                        arg.arg
                        for arg in [*function.args.posonlyargs, *function.args.args]
                    ]
                    parameters = set(positional) | {
                        arg.arg for arg in function.args.kwonlyargs
                    }
                    used = {
                        part.id
                        for part in ast.walk(target)
                        if isinstance(part, ast.Name)
                    } & parameters
                    if used and node in ast.walk(function):
                        previous = wrappers.get(function.name, (positional, set()))
                        wrappers[function.name] = (positional, previous[1] | used)
                if checkout_path(target):
                    failures.append(node.lineno)
            else:
                # `git init <tmp>` creates a fixture without reading the inherited cwd.
                literals = [
                    arg.value for arg in command.elts if isinstance(arg, ast.Constant)
                ]
                if "init" not in literals or checkout_path(command):
                    failures.append(node.lineno)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in wrappers:
            continue
        positional, targets = wrappers[node.func.id]
        supplied = dict(zip(positional, node.args))
        supplied.update(
            {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
        )
        if any(checkout_path(supplied[name]) for name in targets if name in supplied):
            failures.append(node.lineno)
    return sorted(set(failures))


def test_tests_and_helpers_never_inspect_checkout_git_state() -> None:
    failures = {
        str(path.relative_to(ROOT)): lines
        for suite in (ROOT / "tests", ROOT / "control/tests")
        for path in sorted(suite.rglob("*.py"))
        if (lines := git_checkout_reads(path.read_text(encoding="utf-8")))
    }
    assert failures == {}, failures
    assert all(
        reason and not path.endswith(".py") for path, reason in WORKFLOW_ONLY.items()
    )


@pytest.mark.parametrize(
    "source",
    [
        'subprocess.run(["git", "show", "HEAD:x"], cwd=ROOT)',
        'subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=WORKSPACE / "control")',
        'root = Path(__file__).resolve().parents[1]\nsubprocess.run(["git", "status"], cwd=root)',
        'command = ["git", "show", "origin/main:x"]\nsubprocess.run(command, cwd=ROOT)',
        'subprocess.run(["git", "-C", str(ROOT), "status"], cwd=tmp_path)',
        'subprocess.run(["git", "rev-parse", "HEAD"])',
        'subprocess.run(["git", "init", str(ROOT)])',
        'subprocess.run(args=["git", "status"], cwd=ROOT)',
        'def git(repo, *args):\n    subprocess.run(["git", *args], cwd=repo)\ngit(ROOT, "show", "HEAD")',
        'def helper(root=ROOT):\n    subprocess.run(["git", "show", "HEAD"], cwd=root)',
        'root = ROOT\nroot = tmp_path\nsubprocess.run(["git", "show", "HEAD"], cwd=root)',
        'self._run_command(["git", "diff", "--quiet"], cwd=REPOSITORY_ROOT)',
    ],
)
def test_guard_rejects_checkout_reads(source: str) -> None:
    assert git_checkout_reads(source)


@pytest.mark.parametrize(
    "source",
    [
        'subprocess.run(["git", "init", str(tmp_path)])',
        'repo = tmp_path / "repo"\nsubprocess.run(["git", "show", "HEAD"], cwd=repo)',
        'subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"])',
        'assert "git show origin/main:x" in workflow',
    ],
)
def test_guard_accepts_temporary_histories_and_workflow_assertions(source: str) -> None:
    assert not git_checkout_reads(source)
