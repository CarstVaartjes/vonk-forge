"""Test doubles must patch the lookup owner, never Run/Switch facade exports."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FACADE = "vonk_control.run_switch_operations"


def facade_patch_lines(source: str) -> list[int]:
    """Find direct facade patches, including import aliases and dotted strings.

    Patching an exported class's method is valid: consumers share that class.
    Replacing a name on the facade changes only its re-exported binding.
    """
    tree = ast.parse(source)
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                aliases[name.asname or name.name.split(".")[0]] = (
                    name.name if name.asname else name.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom):
            for name in node.names:
                aliases[name.asname or name.name] = f"{node.module}.{name.name}"

    def resolve(node: ast.expr) -> str:
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return f"{resolve(node.value)}.{node.attr}"
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return ""

    # Simple object aliases are common in test helpers.
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and (value := resolve(node.value)):
                    aliases[target.id] = value

    failures = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = resolve(node.func)
        method = name.rsplit(".", 1)[-1]
        if method in {"setattr", "delattr"} or name.endswith("patch.object"):
            target = resolve(node.args[0])
            # String form: monkeypatch.setattr("module.attribute", value).
            owner = (
                target.rsplit(".", 1)[0]
                if isinstance(node.args[0], ast.Constant)
                else target
            )
        elif method == "patch":
            owner = resolve(node.args[0]).rsplit(".", 1)[0]
        else:
            continue
        if owner == FACADE:
            failures.append(node.lineno)
    return failures


def test_control_tests_never_patch_run_switch_facade_reexports() -> None:
    failures = {
        str(path.relative_to(ROOT)): lines
        for path in sorted((ROOT / "control/tests").rglob("*.py"))
        if (lines := facade_patch_lines(path.read_text(encoding="utf-8")))
    }
    assert not failures, f"Patch the submodule that looks up the name: {failures}"


@pytest.mark.parametrize(
    "source",
    [
        'from vonk_control import run_switch_operations as owner\nfault.setattr(owner, "helper", fake)',
        'import vonk_control.run_switch_operations as owner\nmonkeypatch.setattr(owner, "helper", fake)',
        'import vonk_control.run_switch_operations\nmonkeypatch.setattr(vonk_control.run_switch_operations, "helper", fake)',
        'monkeypatch.setattr("vonk_control.run_switch_operations.helper", fake)',
        'from unittest.mock import patch\npatch("vonk_control.run_switch_operations.helper")',
        'from unittest.mock import patch as replace\nreplace("vonk_control.run_switch_operations.helper")',
        'from unittest.mock import patch\nfrom vonk_control import run_switch_operations as owner\npatch.object(owner, "helper")',
        'from vonk_control import run_switch_operations\nowner = run_switch_operations\nmonkeypatch.setattr(owner, "helper", fake, raising=False)',
    ],
)
def test_guard_rejects_ineffective_facade_patches(source: str) -> None:
    assert facade_patch_lines(source)


@pytest.mark.parametrize(
    "source",
    [
        'from vonk_control.run_switch_operations import advance as owner\nfault.setattr(owner, "_merge_progress_evidence", fake)',
        'monkeypatch.setattr("vonk_control.run_switch_operations.advance.helper", fake)',
        'from vonk_control.run_switch_operations import RunSwitchOperationService\nmonkeypatch.setattr(RunSwitchOperationService, "cancel", fake)',
        'from unittest.mock import patch\npatch("vonk_control.run_switch_operations.RunSwitchOperationService.cancel")',
    ],
)
def test_guard_accepts_lookup_owners_and_shared_class_methods(source: str) -> None:
    assert not facade_patch_lines(source)


def activity_subscript_lines(source: str) -> list[int]:
    """Catch direct typed Activity reads; explicit JSON serialization is valid.

    Track provider/item aliases within each function, including page item indexing.
    This is a syntax guard, not a replacement for the Python type checker.
    """
    tree = ast.parse(source)
    failures = []
    for scope in ast.walk(tree):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        kinds: dict[str, str] = {}

        def kind(node: ast.expr, kinds: dict[str, str] = kinds) -> str:
            if isinstance(node, ast.Name):
                return kinds.get(node.id, "")
            if isinstance(node, ast.Call):
                name = node.func
                if isinstance(name, ast.Name):
                    if name.id == "operation_item":
                        return "item"
                    if name.id.endswith("OperationProvider"):
                        return "provider"
                if isinstance(name, ast.Attribute):
                    if name.attr.endswith("activity_provider"):
                        return "provider"
                    if kind(name.value) == "provider":
                        if name.attr == "get_operation":
                            return "item"
                        if name.attr == "list_operations":
                            return "page"
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "items"
                and kind(node.value) == "page"
            ):
                return "items"
            if isinstance(node, ast.Subscript) and kind(node.value) == "items":
                return "item"
            return ""

        nodes = list(ast.walk(scope))
        # Each pass can discover another alias; the number of syntax nodes
        # bounds propagation without consulting repository history.
        for _ in range(len(nodes)):
            previous = kinds.copy()
            for node in nodes:
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and (value := kind(node.value)):
                            kinds[target.id] = value
            if kinds == previous:
                break
        # Comprehension targets have their own scope: an HTTP JSON iterator
        # may reuse a name that held a typed item earlier in the function.
        json_comprehension_reads = {
            id(read)
            for expression in nodes
            if isinstance(
                expression, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
            )
            for generator in expression.generators
            if isinstance(generator.target, ast.Name)
            and kind(generator.iter) != "items"
            for read in ast.walk(expression)
            if isinstance(read, ast.Subscript)
            and isinstance(read.value, ast.Name)
            and read.value.id == generator.target.id
        }
        failures.extend(
            node.lineno
            for node in nodes
            if isinstance(node, ast.Subscript)
            and kind(node.value) == "item"
            and id(node) not in json_comprehension_reads
        )
    return sorted(set(failures))


def test_control_tests_use_typed_activity_items() -> None:
    failures = {
        str(path.relative_to(ROOT)): lines
        for path in sorted((ROOT / "control/tests").rglob("*.py"))
        if (lines := activity_subscript_lines(path.read_text(encoding="utf-8")))
    }
    assert not failures, (
        f"Use OperationItem attributes before serialization: {failures}"
    )


@pytest.mark.parametrize(
    "producer",
    [
        "service.activity_provider().get_operation(identifier)",
        "RunSwitchOperationProvider(service).get_operation(identifier)",
        "operation_item(row)",
        "service.activity_provider().list_operations(query).items[0]",
    ],
)
def test_activity_guard_rejects_model_subscripts_but_accepts_json(
    producer: str,
) -> None:
    source = f"def consumer():\n    item = {producer}\n"
    assert activity_subscript_lines(source + '    assert item["id"] == identifier\n')
    assert not activity_subscript_lines(source + "    assert item.id == identifier\n")
    assert not activity_subscript_lines(
        source
        + '    document = item.model_dump(mode="json")\n    assert document["id"] == identifier\n'
    )


def test_activity_guard_keeps_json_comprehension_names_local() -> None:
    assert not activity_subscript_lines(
        "def consumer():\n"
        "    item = operation_item(row)\n"
        "    assert item.id == identifier\n"
        '    assert [item["id"] for item in response.json()["operations"]]\n'
    )


def guard_exits_before_commit_lines(source: str) -> list[int]:
    """Warn-mode fault seeding must cover the transaction's exit-time flush."""
    failures = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.With):
            continue
        if not any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Name)
            and item.context_expr.func.id == "write_guard_mode"
            and any(
                keyword.arg == "strict"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is False
                for keyword in item.context_expr.keywords
            )
            for item in node.items
        ):
            continue
        methods = [
            item.context_expr.func.attr
            if isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Attribute)
            else item.context_expr.func.id
            if isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Name)
            else ""
            for item in node.items
        ]
        if (
            "begin" in methods
            and "write_guard_mode" in methods
            and methods.index("begin") < methods.index("write_guard_mode")
        ):
            failures.append(node.lineno)
    return failures


def test_fault_seed_guard_covers_transaction_commit() -> None:
    bad = "with sessions.begin() as session, write_guard_mode(strict=False): pass"
    good = "with write_guard_mode(strict=False), sessions.begin() as session: pass"
    assert guard_exits_before_commit_lines(bad)
    assert not guard_exits_before_commit_lines(good)
    assert not guard_exits_before_commit_lines(
        bad.replace("strict=False", "strict=True")
    )
    failures = {
        str(path.relative_to(ROOT)): lines
        for path in sorted((ROOT / "control/tests").rglob("*.py"))
        if (lines := guard_exits_before_commit_lines(path.read_text(encoding="utf-8")))
    }
    assert not failures, (
        f"Keep the fault-seeding guard active through commit: {failures}"
    )
