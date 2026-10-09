"""Guard exceptions name identities and reasons, never numerical allowances."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LEDGER_KEYS = frozenset({"count", "max_sites", "max_debt", "ceiling", "debt_ceiling"})


def numerical_allowances(document: object) -> list[str]:
    if isinstance(document, dict):
        return [
            str(key)
            for key, value in document.items()
            if key in LEDGER_KEYS and isinstance(value, (int, float))
        ] + [key for value in document.values() for key in numerical_allowances(value)]
    if isinstance(document, list):
        return [key for value in document for key in numerical_allowances(value)]
    return []


def test_guard_registries_have_no_numerical_allowances() -> None:
    paths = {
        *ROOT.joinpath("tools").glob("*allowlist.json"),
        *ROOT.joinpath("tools").glob("*baseline.json"),
    }
    offenders = {
        path.name: keys
        for path in sorted(paths)
        if (keys := numerical_allowances(json.loads(path.read_text())))
    }
    assert not offenders, offenders


@pytest.mark.parametrize("key", sorted(LEDGER_KEYS))
def test_guard_rejects_a_numerical_exception_budget(key: str) -> None:
    assert numerical_allowances({"exceptions": [{key: 2, "reason": "reviewed"}]})


def test_guard_accepts_named_exceptions_and_product_resource_limits() -> None:
    assert not numerical_allowances(
        {
            "exceptions": [
                {
                    "file": "source.py",
                    "function": "consume",
                    "reason": "external passthrough",
                }
            ],
            "max_bytes": 1024,
            "timeout_seconds": 30,
        }
    )


def documentation_reads(source: str) -> list[int]:
    """Find repository documentation reads, including path aliases."""
    if not any(marker in source for marker in ("docs", "README.md", "AGENTS.md")):
        return []
    tree = ast.parse(source)
    aliases: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    aliases.setdefault(target.id, []).append(node.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if isinstance(node.target, ast.Name):
                aliases.setdefault(node.target.id, []).append(node.value)
        elif isinstance(node, (ast.For, ast.comprehension)) and isinstance(
            node.target, ast.Name
        ):
            aliases.setdefault(node.target.id, []).append(node.iter)

    documented_paths: set[str] = set()

    def is_documentation(node: ast.AST) -> bool:
        return any(
            (
                isinstance(child, ast.Constant)
                and isinstance(child.value, str)
                and (
                    child.value.startswith("docs/")
                    or child.value in {"docs", "README.md", "AGENTS.md"}
                )
            )
            or (isinstance(child, ast.Name) and child.id in documented_paths)
            for child in ast.walk(node)
        )

    for _ in aliases:
        discovered = {
            name
            for name, values in aliases.items()
            if any(is_documentation(value) for value in values)
        }
        if discovered <= documented_paths:
            break
        documented_paths.update(discovered)

    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"read_text", "read_bytes", "open"}
            and is_documentation(node.func.value)
            or isinstance(node.func, ast.Name)
            and node.func.id == "open"
            and node.args
            and is_documentation(node.args[0])
        )
    ]


def test_tests_do_not_assert_repository_documentation_content() -> None:
    offenders = {
        path.relative_to(ROOT).as_posix(): lines
        for suite in ("tests", "control/tests", "agent_protocol/tests")
        for path in ROOT.joinpath(suite).rglob("*.py")
        if (lines := documentation_reads(path.read_text()))
    }
    assert not offenders, offenders


@pytest.mark.parametrize(
    "source",
    [
        'text = (ROOT / "docs/runbooks/vonkctl.md").read_text()',
        'path = ROOT / "README.md"\ntext = path.read_text()',
        'path: Path = ROOT / "docs" / "guide.md"\ntext = open(path).read()',
        'paths = (ROOT / "docs/guide.md",)\nfor path in paths:\n    text = path.read_text()',
        'paths = (ROOT / "docs/guide.md",)\ntexts = [path.read_text() for path in paths]',
    ],
)
def test_guard_rejects_documentation_content_reads(source: str) -> None:
    assert documentation_reads(source)


def test_guard_preserves_owned_document_fixtures() -> None:
    assert not documentation_reads('text = (tmp_path / "document.md").read_text()')


def _structural_counter_tree(tree: ast.AST) -> list[int]:
    owned = {"ROOT", "WORKSPACE", "REPOSITORY_ROOT", "__file__"}
    contents: set[str] = set()

    def mentions(node: ast.AST, names: set[str]) -> bool:
        return any(
            isinstance(child, ast.Name) and child.id in names
            for child in ast.walk(node)
        )

    def reads_source(node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in contents
        if isinstance(node, ast.DictComp):
            return reads_source(node.value)
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            return reads_source(node.elt)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return node.func.id in {"len", "max", "min", "sum"} and any(
                reads_source(argument) for argument in node.args
            )
        if isinstance(node, ast.Subscript):
            return reads_source(node.value)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "read_text":
                return mentions(node.func.value, owned)
            return reads_source(node.func.value)
        return False

    bindings = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            bindings.extend((target, node.value) for target in node.targets)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            bindings.append((node.target, node.value))
        elif isinstance(node, (ast.For, ast.comprehension)):
            bindings.append((node.target, node.iter))
    for _ in bindings:
        discovered_paths = {
            target.id
            for target, value in bindings
            if isinstance(target, ast.Name) and mentions(value, owned)
        }
        discovered_contents = {
            target.id
            for target, value in bindings
            if isinstance(target, ast.Name) and reads_source(value)
        }
        if discovered_paths <= owned and discovered_contents <= contents:
            break
        owned.update(discovered_paths)
        contents.update(discovered_contents)
    counters = {
        target.id
        for target, value in bindings
        if isinstance(target, ast.Name)
        and reads_source(value)
        and any(
            isinstance(child, ast.Call)
            and (
                isinstance(child.func, ast.Attribute)
                and child.func.attr == "count"
                or isinstance(child.func, ast.Name)
                and child.func.id == "len"
            )
            for child in ast.walk(value)
        )
    }
    return sorted(
        {
            assertion.lineno
            for assertion in ast.walk(tree)
            if isinstance(assertion, ast.Assert) and mentions(assertion.test, counters)
        }
        | {
            assertion.lineno
            for assertion in ast.walk(tree)
            if isinstance(assertion, ast.Assert)
            for call in ast.walk(assertion.test)
            if isinstance(call, ast.Call)
            and (
                isinstance(call.func, ast.Attribute)
                and call.func.attr == "count"
                and reads_source(call.func.value)
                or isinstance(call.func, ast.Name)
                and call.func.id in {"len", "max", "min", "sum"}
                and call.args
                and reads_source(call.args[0])
            )
        }
    )


def structural_counters(source: str) -> list[int]:
    tree = ast.parse(source)
    global_assignments = [
        node for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))
    ]
    top_level = [
        node
        for node in tree.body
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    failures = set(
        _structural_counter_tree(ast.Module(body=top_level, type_ignores=[]))
    )
    for function in ast.walk(tree):
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = ast.Module(
                body=[*global_assignments, *function.body], type_ignores=[]
            )
            # Preserve original locations while keeping local aliases in scope.
            failures.update(_structural_counter_tree(scope))
    return sorted(failures)


def test_repository_source_has_no_count_allowances() -> None:
    offenders = {
        path.relative_to(ROOT).as_posix(): lines
        for suite in ("tests", "control/tests", "agent_protocol/tests")
        for path in ROOT.joinpath(suite).rglob("*.py")
        if (lines := structural_counters(path.read_text()))
    }
    assert not offenders, offenders


@pytest.mark.parametrize(
    "source",
    [
        'source = (ROOT / "module.py").read_text()\nassert source.count("raise") == 3',
        'for module in ROOT.rglob("*.py"):\n    assert len(module.read_text().splitlines()) <= 400',
        'source = (ROOT / "module.py").read_text()\nlines = source.splitlines()\nassert len(lines) <= ceiling',
        'source = (ROOT / "module.py").read_text()\ncount = source.count("raise")\nassert count == 3',
        'sizes = {path.name: len(path.read_text().splitlines()) for path in ROOT.glob("*.py")}\nassert max(sizes.values()) <= 1000',
    ],
)
def test_guard_rejects_source_occurrence_and_line_allowances(source: str) -> None:
    assert structural_counters(source)


@pytest.mark.parametrize(
    "source",
    [
        'assert calls.count("POST") == 1',
        "assert len(canonical_message(request)) <= MAX_REQUEST_BYTES",
        'source = (ROOT / "module.py").read_text()\nassert "required_call()" in source',
    ],
)
def test_guard_keeps_runtime_effects_resource_limits_and_named_rules(
    source: str,
) -> None:
    assert not structural_counters(source)
