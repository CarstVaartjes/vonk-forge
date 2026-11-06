"""Reject git in tests: a test hands a tool its inputs as arguments.

Owner rule (2026-11-06): tests never use git. A tool takes the revision, the
tree, the changed paths or the file list as an argument, and git runs only at
the CI edge (a workflow step or a hook). A test that runs git reads the state of
whatever checkout it runs in, which differs between a laptop, a worktree and a
shallow CI clone.

Python is scanned by AST, so a ``"git"`` in a string constant, a stub filename or
a ``patch`` target is not a hit; only a subprocess call, a ``git`` helper call
or a ``GIT_*`` environment key is. TS/MJS, shell and Rust tests use a line scan.
"""

from __future__ import annotations

import ast
import re

KIND = "test runs git; pass the revision, tree or paths as arguments (git only at the CI edge)"

# Guard code lives under control/tests/ but is not a test: its helpers are named
# for the git programs they recognise and never run one.
VERDICTS: dict[str, str] = {
    "control/tests/git_free_test_lines.py": "the guard names git helpers, runs none",
}
_SPAWNERS = frozenset(
    {
        "run",
        "call",
        "check_call",
        "check_output",
        "Popen",
        "system",
        "popen",
        "create_subprocess_exec",
        "create_subprocess_shell",
    }
)
_HELPER = re.compile(r"(?:^|_)git(?:$|_)", re.IGNORECASE)
_ENV_KEY = re.compile(r"^GIT_[A-Z_]+$")
_GIT_STRING = re.compile(r"^\s*git(?:\s|$)")
_SHELL_STRING = re.compile(r"(?:^|[\s;&|(])git\s+[a-z]")
_SHELLS = frozenset({"bash", "sh", "zsh", "dash"})
_TS = re.compile(
    r"\b(?:execFileSync|execFile|spawnSync|spawn|execSync|exec)\(\s*[\"'`]git\b"
)
_SHELL = re.compile(r"(?:^|[;&|(]|\$\()\s*git\s")
_RUST = re.compile(r"Command::new\(\s*\"git\"\s*\)")


def _string(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _git_prefix(node: ast.expr) -> bool:
    """An argv (or command string) whose program is git."""
    text = _string(node)
    if text is not None:
        return bool(_GIT_STRING.match(text))
    if isinstance(node, ast.JoinedStr):
        first = node.values[0] if node.values else None
        return (
            isinstance(first, ast.Constant)
            and isinstance(first.value, str)
            and bool(_GIT_STRING.match(first.value))
        )
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        head = node.elts[0]
        if isinstance(head, ast.Starred):
            head = head.value
            return isinstance(head, ast.Name) and bool(_HELPER.search(head.id))
        return _string(head) == "git"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _git_prefix(node.left)
    if isinstance(node, ast.Name):
        return node.id.upper() == "GIT"
    return False


def _shell_command(node: ast.expr) -> bool:
    """``["bash", "-c", "... git x ..."]`` or any argv carrying such a string."""
    if not isinstance(node, (ast.List, ast.Tuple)):
        return False
    strings = [_string(element) for element in node.elts]
    if not strings or strings[0] is None:
        return False
    if strings[0].rsplit("/", 1)[-1] not in _SHELLS:
        return False
    return any(text and _SHELL_STRING.search(text) for text in strings[1:])


def _call_name(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def scan_python(source: str) -> list[int]:
    hits: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if _HELPER.search(name):
                hits.add(node.lineno)
            if name not in _SPAWNERS:
                continue
            arguments = [*node.args[:1]] + [
                keyword.value for keyword in node.keywords if keyword.arg == "args"
            ]
            shell = any(
                keyword.arg == "shell"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            )
            for argument in arguments:
                text = _string(argument)
                if (
                    _git_prefix(argument)
                    or _shell_command(argument)
                    or (shell and text is not None and _SHELL_STRING.search(text))
                    or (
                        name in {"system", "popen"}
                        and text is not None
                        and _SHELL_STRING.search(text)
                    )
                ):
                    hits.update({node.lineno, argument.lineno})
        elif isinstance(node, ast.Dict):
            for key in node.keys:
                if key is None:
                    continue
                text = _string(key)
                if text is not None and _ENV_KEY.match(text):
                    hits.add(key.lineno)
    return sorted(hits)


def scan_shell(source: str) -> list[int]:
    return [
        index
        for index, text in enumerate(source.splitlines(), 1)
        if not text.lstrip().startswith("#") and _SHELL.search(text)
    ]


def scan_ts(source: str) -> list[int]:
    return [
        index
        for index, text in enumerate(source.splitlines(), 1)
        if not text.lstrip().startswith("//") and _TS.search(text)
    ]


def scan_rust(source: str, test_lines: set[int]) -> list[int]:
    return [
        index
        for index, text in enumerate(source.splitlines(), 1)
        if index in test_lines
        and not text.lstrip().startswith("//")
        and _RUST.search(text)
    ]


def scan_path(
    path: str, source: str, *, is_test: bool, test_lines: set[int]
) -> list[int]:
    """Lines of ``source`` that run git, for a test file of any language."""
    if path in VERDICTS:
        return []
    name = path.rsplit("/", 1)[-1]
    if path.endswith(".py"):
        return scan_python(source) if is_test else []
    if path.endswith((".sh", ".bash")):
        return scan_shell(source) if is_test else []
    if path.endswith((".ts", ".tsx", ".mjs", ".js", ".mts")):
        tested = is_test or re.search(r"\.(?:test|spec)\.", name) is not None
        return scan_ts(source) if tested else []
    if path.endswith(".rs"):
        return scan_rust(source, test_lines)
    return []
