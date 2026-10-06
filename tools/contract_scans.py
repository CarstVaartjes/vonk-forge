"""Static scans that keep every data class defined once, in its Pydantic contract.

The owner rule: a data class has ONE definition, a Pydantic contract model, and
Python, Rust and TypeScript use it or code generated from it.  Three scans hold
that rule, each against a reviewed allowlist under ``tools/``:

``rust_serde_types``
    a ``struct``/``enum`` outside ``generated.rs`` that derives ``Serialize`` or
    ``Deserialize`` (or implements them by hand) is a hand-written copy of data.
    ``tools/serde-derive-allowlist.json`` names each survivor with its reason.
``typescript_shapes``
    ``type X = {`` / ``interface X {`` outside generated files and tests declares
    a shape.  A shape that describes API data is an alias of the generated
    schema instead; ``tools/ts-shapes-allowlist.json`` names the UI-only ones.
``python_models``
    a Pydantic model class lives in a module of ``tools/python-model-registry.json``
    and no two registered models share a name or a field set.

The allowlists only shrink: an unlisted entry fails, and a listed entry that no
longer occurs fails as stale, so a fixed copy must be removed from the list.
"""

from __future__ import annotations

import ast
import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"

# -- Rust --------------------------------------------------------------------------

RUST_ALLOWLIST = TOOLS / "serde-derive-allowlist.json"
_RUST_DERIVE = re.compile(
    r"#\[derive\(([^\]]*?)\)\]((?:\s*#\[[^\]]*\])*)\s*"
    r"(?:pub(?:\([^)]*\))?\s+)?(struct|enum)\s+(\w+)",
    re.DOTALL,
)
_RUST_IMPL = re.compile(
    r"impl(?:<[^>]*>)?\s+(?:::)?(?:serde::)?(?:ser::)?(?:de::)?"
    r"(Serialize|Deserialize)(?:<[^>]*>)?\s+for\s+(\w+)"
)
_SERDE = re.compile(r"\b(?:Serialize|Deserialize)\b")


def rust_files(root: Path = ROOT) -> list[Path]:
    return sorted(
        path
        for path in (root / "rust").rglob("*.rs")
        if "target" not in path.relative_to(root).parts and path.name != "generated.rs"
    )


def rust_serde_types(root: Path = ROOT) -> list[tuple[str, str]]:
    """Every ``(file, type)`` that carries serde outside the generated module."""

    found: set[tuple[str, str]] = set()
    for path in rust_files(root):
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(root).as_posix()
        for match in _RUST_DERIVE.finditer(text):
            if _SERDE.search(match.group(1)):
                found.add((relative, match.group(4)))
        for match in _RUST_IMPL.finditer(text):
            found.add((relative, match.group(2)))
    return sorted(found)


def is_production_rust(file: str) -> bool:
    parts = file.split("/")
    return "tests" not in parts and "examples" not in parts


# -- TypeScript --------------------------------------------------------------------

TS_ALLOWLIST = TOOLS / "ts-shapes-allowlist.json"
WEB_SRC = ROOT / "control/web/src"
_TS_SHAPE = re.compile(
    r"^[ \t]*(?:export\s+)?(?:declare\s+)?"
    r"(?:type\s+(\w+)(?:<[^=\n]*>)?\s*=\s*\{|interface\s+(\w+)(?:<[^{\n]*>)?[^{\n]*\{)",
    re.MULTILINE,
)


def typescript_sources(root: Path = ROOT) -> list[Path]:
    web = root / "control/web/src"
    return sorted(
        path
        for pattern in ("*.ts", "*.tsx")
        for path in web.rglob(pattern)
        if ".generated." not in path.name
        and not path.name.endswith(".d.ts")
        and ".test." not in path.name
        and "test-fixtures" not in path.parts
        and path.name != "test-setup.ts"
    )


def typescript_shapes(root: Path = ROOT) -> list[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in typescript_sources(root):
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(root).as_posix()
        for match in _TS_SHAPE.finditer(text):
            found.add((relative, match.group(1) or match.group(2)))
    return sorted(found)


# -- Python ------------------------------------------------------------------------

PY_REGISTRY = TOOLS / "python-model-registry.json"
PY_SCAN_DIRS = (
    "agent_protocol/src",
    "control/src",
    "src",
    "scripts",
    "deploy",
    "tools",
)
_MODEL_ROOTS = {"BaseModel", "RootModel", "WireModel"}


def python_sources(root: Path = ROOT) -> list[Path]:
    paths: list[Path] = []
    for directory in PY_SCAN_DIRS:
        base = root / directory
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            parts = path.relative_to(root).parts
            if "generated_control" in parts or ".venv" in parts or "tests" in parts:
                continue
            paths.append(path)
    return sorted(paths)


def _base_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Subscript):
        return _base_name(node.value)
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def python_models(root: Path = ROOT) -> dict[tuple[str, str], tuple[str, ...]]:
    """``(file, class) -> field signature`` for every Pydantic model class."""

    classes: dict[tuple[str, str], tuple[ast.ClassDef, set[str]]] = {}
    for path in python_sources(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        relative = path.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = {name for b in node.bases if (name := _base_name(b))}
                classes[(relative, node.name)] = (node, bases)
    model_names = set(_MODEL_ROOTS)
    changed = True
    while changed:
        changed = False
        for (_file, name), (_node, bases) in classes.items():
            if name not in model_names and bases & model_names:
                model_names.add(name)
                changed = True
    models: dict[tuple[str, str], tuple[str, ...]] = {}
    for (file, name), (node, bases) in classes.items():
        if not bases & model_names:
            continue
        fields = tuple(
            f"{stmt.target.id}:{ast.unparse(stmt.annotation)}"
            for stmt in node.body
            if isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and not stmt.target.id.startswith("_")
            and stmt.target.id != "model_config"
        )
        models[(file, name)] = tuple(sorted(fields))
    return models


def python_registry_problems(
    root: Path = ROOT, registry: dict | None = None
) -> list[str]:
    if registry is None:
        registry = json.loads((root / "tools/python-model-registry.json").read_text())
    modules = {entry["module"] for entry in registry["modules"]}
    problems: list[str] = []
    models = python_models(root)
    for file, name in models:
        if file not in modules:
            problems.append(
                f"{file}: model {name} is outside the contract registry; "
                "move it to agent_protocol or a registered contract module"
            )
    present = {file for file, _ in models}
    for module in sorted(modules - present):
        problems.append(f"registry module {module} defines no model (stale entry)")
    allowed = {
        tuple(sorted(item["models"])) for item in registry.get("allowed_duplicates", [])
    }
    by_name: dict[str, list[str]] = defaultdict(list)
    by_fields: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for (file, name), fields in models.items():
        if file not in modules:
            continue
        by_name[name].append(f"{file}:{name}")
        if fields:
            by_fields[fields].append(f"{file}:{name}")
    seen: set[tuple[str, ...]] = set()
    for group in [*by_name.values(), *by_fields.values()]:
        key = tuple(sorted(group))
        if len(key) > 1 and key not in seen:
            seen.add(key)
            if key not in allowed:
                problems.append(f"duplicate model definitions: {', '.join(key)}")
    for key in sorted(allowed - seen):
        problems.append(f"allowed duplicate {', '.join(key)} no longer occurs (stale)")
    return problems
