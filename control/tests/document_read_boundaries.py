"""A Recipe, Model or display document has one typed reader; a raw dict read is a site.

``vonk_forge_contracts`` (``read_recipe``, ``read_model``, ``read_recipe_display``,
``read_model_display``) owns the shape of these documents. Indexing the raw dict
re-derives that shape by hand and drifts from the contract. Validating a stored
document with ``ModelDefinition``/``RecipeDefinition`` ``.model_validate*`` or a
``TypeAdapter`` of them is the same class: the strict contract refuses a field a
row of an older contract still carries, so only the tolerant ``read_*`` readers
read stored documents. Counterpart of ``tools/check-document-reads`` in
vonk-forge-recipes. No allowances or counts.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOTS = (
    REPO_ROOT / "control" / "src",
    REPO_ROOT / "src" / "cluster_profiles",
    REPO_ROOT / "scripts",
)
KEYS = frozenset(
    {
        "identity",
        "metadata",
        "runtime",
        "topology",
        "execution",
        "models",
        "interfaces",
        "validation",
        "provenance",
        "settings",
        "release",
        "options",
        "files",
        "format",
        "license",
        "source",
        "dependencies",
        "capabilities",
        "modalities",
        "requires_token",
        "title",
        "description",
        "tags",
        "alignment",
        "family_title",
        "model_title",
    }
)
DOCUMENT_NAMES = frozenset(
    {"document", "recipe_document", "model_document", "doc", "display"}
)
DOCUMENT_KEY = "document"
PACKAGE_MEMBER = "recipe.json"
# Document contracts whose strict validation is not a stored-document reader.
DOCUMENT_CONTRACTS = frozenset({"ModelDefinition", "RecipeDefinition"})


@dataclass(frozen=True)
class Site:
    path: str
    function: str
    key: str
    line: int

    def render(self) -> str:
        return f"{self.path}:{self.line}: [{self.key!r}] in {self.function}"


def _access(node: ast.AST) -> tuple[ast.expr, str] | None:
    """``(value, key)`` of ``value[key]`` or ``value.get(key)`` with a string key."""
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    ):
        return node.value, node.slice.value
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.func.value, node.args[0].value
    return None


def _is_package_recipe(node: ast.expr) -> bool:
    """``json.loads(<...recipe.json...>)``, the package member read by hand."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"load", "loads"}
        and any(
            isinstance(child, ast.Constant) and child.value == PACKAGE_MEMBER
            for child in ast.walk(node)
        )
    )


def _is_document(node: ast.expr, bound: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in DOCUMENT_NAMES or node.id in bound
    if isinstance(node, ast.Attribute):
        return node.attr == DOCUMENT_KEY
    if _is_package_recipe(node):
        return True
    access = _access(node)
    return access is not None and access[1] == DOCUMENT_KEY


def _scope_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """Nodes of one function scope, not descending into nested functions."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            stack.extend(ast.iter_child_nodes(node))


def _bound_documents(nodes: list[ast.AST]) -> set[str]:
    bound: set[str] = set()
    assigns = [
        (node.targets[0].id, node.value)
        for node in nodes
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    ]
    grew = True
    while grew:  # a name bound from a bound name is bound too
        grew = False
        for name, value in assigns:
            if name not in bound and _is_document(value, bound):
                bound.add(name)
                grew = True
    return bound


def _strict_contract_read(node: ast.Call) -> str | None:
    """``<Contract>.model_validate*(...)`` or ``TypeAdapter(<Contract>)``."""
    func = node.func
    if (
        isinstance(func, ast.Attribute)
        and func.attr.startswith("model_validate")
        and isinstance(func.value, ast.Name)
        and func.value.id in DOCUMENT_CONTRACTS
    ):
        return f"{func.value.id}.{func.attr}"
    if (
        isinstance(func, ast.Name)
        and func.id == "TypeAdapter"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id in DOCUMENT_CONTRACTS
    ):
        return f"TypeAdapter({node.args[0].id})"
    return None


def scan_source(source: str, *, path: str) -> list[Site]:
    tree = ast.parse(source)
    scopes: list[tuple[str, ast.AST]] = [("<module>", tree)]
    scopes.extend(
        (node.name, node)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    )
    sites: set[Site] = set()
    for name, scope in scopes:
        nodes = list(_scope_nodes(scope))
        bound = _bound_documents(nodes)
        for node in nodes:
            strict = _strict_contract_read(node) if isinstance(node, ast.Call) else None
            if isinstance(node, ast.Call) and strict is not None:
                sites.add(Site(path=path, function=name, key=strict, line=node.lineno))
                continue
            access = _access(node)
            if access is None or not isinstance(node, ast.Subscript | ast.Call):
                continue
            owner, key = access
            if key in KEYS and _is_document(owner, bound):
                sites.add(Site(path=path, function=name, key=key, line=node.lineno))
    return sorted(sites, key=lambda site: (site.line, site.key))


def scan_sites(roots: tuple[Path, ...] = SOURCE_ROOTS) -> list[Site]:
    sites: list[Site] = []
    for root in roots:
        for module in sorted(root.rglob("*")):
            if "generated_control" in module.parts or not module.is_file():
                continue
            if module.suffix != ".py" and not (
                not module.suffix
                and module.read_bytes().startswith(b"#!")
                and b"python" in module.read_bytes().splitlines()[0]
            ):
                continue
            relative = module.relative_to(REPO_ROOT).as_posix()
            sites.extend(scan_source(module.read_text(encoding="utf-8"), path=relative))
    return sites
