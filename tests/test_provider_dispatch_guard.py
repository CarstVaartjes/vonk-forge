"""Artifact kinds are dispatched through the provider table, never by comparison."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LITERAL_OWNER = "control/src/vonk_control/model_cache/constants.py"
DISPATCH_OWNER = "control/src/vonk_control/model_cache/providers.py"


def _provider_kind_values() -> set[str]:
    """Read the ProviderKind values without importing the Controller."""

    tree = ast.parse((ROOT / LITERAL_OWNER).read_text())
    (owner,) = (
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "ProviderKind"
    )
    return {
        statement.value.value
        for statement in owner.body
        if isinstance(statement, ast.Assign)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    }


KIND_VALUES = _provider_kind_values()
# "file" is also an ordinary word; it is caught only when compared to a kind.
KIND_LITERALS = {value for value in KIND_VALUES if "." in value}


def _is_kind_reference(node: ast.expr) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "kind") or (
        isinstance(node, ast.Name) and node.id == "kind"
    )


def _is_provider_kind(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "ProviderKind"
    ) or (isinstance(node, ast.Constant) and node.value in KIND_VALUES)


def _spells_kind(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in KIND_LITERALS
    )


def _compares_kind(node: ast.AST) -> bool:
    if not isinstance(node, ast.Compare):
        return False
    operands = (node.left, *node.comparators)
    items = [
        item
        for operand in operands
        for item in (
            operand.elts
            if isinstance(operand, ast.Set | ast.Tuple | ast.List)
            else [operand]
        )
    ]
    return any(_is_kind_reference(operand) for operand in operands) and any(
        _is_provider_kind(item) for item in items
    )


def scan_source(
    source: str, *, dispatcher: bool = False, owner: bool = False
) -> list[int]:
    """Return lines that spell a kind literal or branch on an artifact kind."""

    lines: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if (not owner and _spells_kind(node)) or (
            not dispatcher and _compares_kind(node)
        ):
            lines.add(getattr(node, "lineno", 0))
    return sorted(lines)


def test_artifact_kinds_are_dispatched_through_the_provider_table():
    violations = []
    for path in (ROOT / "control/src").rglob("*.py"):
        relative = str(path.relative_to(ROOT))
        for line in scan_source(
            path.read_text(),
            dispatcher=relative == DISPATCH_OWNER,
            owner=relative in {LITERAL_OWNER, DISPATCH_OWNER},
        ):
            violations.append(f"{relative}:{line}")
    assert not violations, (
        "artifact kind spelled or compared outside providers.py; add a Provider "
        "field instead: " + ", ".join(violations)
    )


def test_guard_catches_kind_comparisons_and_literals():
    assert scan_source('if spec.kind == "github-release.asset":\n    pass\n')
    assert scan_source("if spec.kind == ProviderKind.FILE:\n    pass\n")
    assert scan_source('if kind in {"http.file", "file"}:\n    pass\n')
    assert scan_source('if spec.kind != "huggingface.file":\n    pass\n')
    assert not scan_source("if spec.kind == operation.kind:\n    pass\n")
    assert not scan_source('if operation.kind == "download":\n    pass\n')
    assert not scan_source("provider = PROVIDERS.get(spec.kind)\n")
