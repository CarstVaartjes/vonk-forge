"""Lane programs that run inside an installed Controller use only its stable surface.

The upgrade-carry lane runs Python programs from the candidate's tree inside
the installed Controller containers: first the previous release's, then the
candidate's. There ``vonk_control`` is whichever release is installed. Release
run 42404137945 failed in the previous release because the canary import
program built ``CatalogModelEntry`` with the field the candidate had just added.

So such a program names only the Controller entry points below, which every
release ships and calls the same way, and never constructs the Controller's
own data types: the installed Controller's parser and services make those.
Imports under ``TYPE_CHECKING`` never execute and are free.

Programs are found from the lane code: every argv sequence (call arguments, a
list or a tuple) that names a Python interpreter must hand it a program in a
form this guard reads (an inline string, a lane file's ``read_text()``, a
variable holding either, or a mounted lane script path). Any other form fails
rather than falling through, and every acceptance file that starts a Python
interpreter must be classified by which Controllers it reaches.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE = ROOT / "tests/acceptance"

# Acceptance files that start Python in a Controller container, by reach.
# The previous release's Controller, then the candidate's: the stable surface.
INSTALLED_CONTROLLER = {
    "test_spark_lifecycle.py": "the Spark lane; the carry runs it on the previous release first",
}
# Only the candidate's own Controller: forms are still read, surface is free.
CANDIDATE_ONLY = {
    "test_fresh_nas_install.py": "clean NAS install and reinstall of the candidate; asserts VONK_ACCEPTANCE_SOURCE_SHA",
}

# Module -> names a program may import. Each one exists, with the same call
# shape, in the previous release. Adding a name means checking that release.
# Never the Controller's data types (CatalogModelEntry, RecipeLibraryItem,
# RecipeLibrarySnapshot, RecipePackageEntry): their fields follow the release.
# ManagedCatalogSyncRequest is built by keyword: a reviewed claim like the rest.
ENTRY_POINTS: dict[str, frozenset[str]] = {
    "vonk_agent_protocol": frozenset(
        {
            "AgentResult",
            "CatalogSyncCode",
            "OutcomeDone",
            "RecipeStartResult",
            "outcome_body",
        }
    ),
    "vonk_control.api": frozenset({"production_app"}),
    "vonk_control.auth": frozenset({"TokenCodec"}),
    "vonk_control.catalog_service": frozenset({"CatalogService"}),
    "vonk_control.catalog_sync": frozenset(
        {"CatalogSyncError", "CatalogSyncView", "ManagedRecipeCatalogSyncService"}
    ),
    "vonk_control.catalog_sync_contract": frozenset(
        {"CatalogSyncTrigger", "ManagedCatalogSyncRequest", "reviewed_catalog_content"}
    ),
    "vonk_control.db": frozenset({"build_engine", "session_factory"}),
    # The release reader's own index parser and package decoder.
    "vonk_control.recipe_packages": frozenset(
        {"RecipePackageClient", "load_recipe_package"}
    ),
    "vonk_control.settings": frozenset({"Settings"}),
    "vonk_control.source_bundles": frozenset({"DatabaseSourceBundleStore"}),
}
PLATFORM = ("vonk_", "cluster_profiles", "importlib")
INTERPRETER = re.compile(r"python[0-9.]*")


def _type_checking_only(node: ast.AST) -> bool:
    return isinstance(node, ast.If) and (
        (isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING")
        or (isinstance(node.test, ast.Attribute) and node.test.attr == "TYPE_CHECKING")
    )


def _executed(node: ast.AST) -> list[ast.AST]:
    """Every node that runs, leaving out ``if TYPE_CHECKING:`` bodies."""
    found = [node]
    for child in ast.iter_child_nodes(node):
        if _type_checking_only(child):
            assert isinstance(child, ast.If)
            for branch in child.orelse:
                found.extend(_executed(branch))
            continue
        found.extend(_executed(child))
    return found


def surface_violations(source: str) -> list[str]:
    """Platform names a program imports outside the stable entry points."""
    violations = []
    for node in _executed(ast.parse(source)):
        if isinstance(node, ast.Import):
            violations.extend(
                alias.name for alias in node.names if alias.name.startswith(PLATFORM)
            )
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            PLATFORM
        ):
            allowed = ENTRY_POINTS.get(node.module or "", frozenset())
            violations.extend(
                f"{node.module}.{alias.name}"
                for alias in node.names
                if alias.name not in allowed
            )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "__import__"
        ):
            violations.append("__import__")
    return violations


@dataclass(frozen=True)
class Programs:
    """What a lane file hands to Python interpreters."""

    files: frozenset[str]  # lane file names, run as a whole program
    inline: tuple[str, ...]  # literal program text
    unread: tuple[str, ...]  # forms this guard cannot read: always a failure


def _with_name(node: ast.AST) -> str | None:
    """``Path(...).with_name("x.py")`` -> ``x.py``."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "with_name"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0].value
    return None


def _text(node: ast.AST) -> str | None:
    """Literal program text: a string, an f-string or a concatenation."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value
            for part in ast.walk(node)
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _text(node.left), _text(node.right)
        return None if left is None or right is None else left + right
    return None


def lane_programs(source: str) -> Programs:
    tree = ast.parse(source)
    assigned: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned.setdefault(target.id, []).append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            assigned.setdefault(node.target.id, []).append(node.value)
    files: set[str] = set()
    inline: list[str] = []
    unread: list[str] = []

    def operand(node: ast.AST, line: int, seen: frozenset[str]) -> None:
        text = _text(node)
        if text is not None:
            inline.append(text)
            return
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "read_text"
        ):
            target = node.func.value
            names = (
                [_with_name(value) for value in assigned.get(target.id, [])]
                if isinstance(target, ast.Name)
                else [_with_name(target)]
            )
            if names and all(names):
                files.update(name for name in names if name)
                return
        if isinstance(node, ast.Name) and node.id not in seen and node.id in assigned:
            for value in assigned[node.id]:
                operand(value, line, seen | {node.id})
            return
        unread.append(f"line {line}: {ast.unparse(node)}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            sequence: list[ast.expr] = list(node.args)
        elif isinstance(node, (ast.List, ast.Tuple)):
            sequence = list(node.elts)
        else:
            continue
        for position, element in enumerate(sequence):
            if not (
                isinstance(element, ast.Constant)
                and isinstance(element.value, str)
                and INTERPRETER.fullmatch(PurePosixPath(element.value).name)
            ):
                continue
            rest = sequence[position + 1 :]
            first = rest[0] if rest else None
            if (
                isinstance(first, ast.Constant)
                and first.value == "-c"
                and len(rest) > 1
            ):
                operand(rest[1], element.lineno, frozenset())
            elif (
                isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and first.value.endswith(".py")
            ):
                # A mounted lane script: /acceptance/name.py runs name.py.
                files.add(PurePosixPath(first.value).name)
            else:
                shown = "nothing" if first is None else ast.unparse(first)
                unread.append(f"line {element.lineno}: interpreter given {shown}")
    return Programs(frozenset(files), tuple(inline), tuple(unread))


def _starts_python(path: Path) -> bool:
    programs = lane_programs(path.read_text(encoding="utf-8"))
    return bool(programs.files or programs.inline or programs.unread)


def test_every_acceptance_file_that_starts_python_is_classified() -> None:
    starting = {path.name for path in ACCEPTANCE.glob("*.py") if _starts_python(path)}
    classified = set(INSTALLED_CONTROLLER) | set(CANDIDATE_ONLY)
    assert starting <= classified, sorted(starting - classified)
    assert classified <= starting, sorted(classified - starting)
    assert not set(INSTALLED_CONTROLLER) & set(CANDIDATE_ONLY)


def test_every_lane_program_is_in_a_form_the_guard_reads() -> None:
    unread = {
        name: programs.unread
        for name in sorted(set(INSTALLED_CONTROLLER) | set(CANDIDATE_ONLY))
        if (programs := lane_programs((ACCEPTANCE / name).read_text("utf-8"))).unread
    }
    assert not unread, unread


def test_installed_controller_programs_use_only_the_stable_surface() -> None:
    files: set[str] = set()
    inline: list[str] = []
    for name in INSTALLED_CONTROLLER:
        programs = lane_programs((ACCEPTANCE / name).read_text(encoding="utf-8"))
        files |= programs.files
        inline.extend(programs.inline)
    assert {"spark_canary_catalog_import.py", "receipt_fault.py"} <= files
    offenders = {
        name: violations
        for name in sorted(files)
        if (
            violations := surface_violations(
                (ACCEPTANCE / name).read_text(encoding="utf-8")
            )
        )
    }
    assert not offenders, offenders
    reaching = [text for text in inline if any(mark in text for mark in PLATFORM)]
    assert not reaching, reaching


def test_building_a_controller_data_type_is_refused() -> None:
    program = (
        "from vonk_control.recipe_library_types import CatalogModelEntry\n"
        "from vonk_control.recipe_packages import RecipePackageClient\n"
        "import vonk_forge_contracts\n"
        "def f():\n"
        "    from vonk_control.catalog_sync import _private\n"
        "    return __import__('vonk_control.models')\n"
    )

    assert set(surface_violations(program)) == {
        "vonk_control.recipe_library_types.CatalogModelEntry",
        "vonk_forge_contracts",
        "vonk_control.catalog_sync._private",
        "__import__",
    }


def test_annotation_only_imports_are_free() -> None:
    program = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from vonk_control.recipe_library_types import RecipeLibrarySnapshot\n"
        "else:\n"
        "    from vonk_control.models import Base\n"
    )

    assert surface_violations(program) == ["vonk_control.models.Base"]


def test_every_program_form_is_read() -> None:
    lane = """
PROGRAM = Path(__file__).with_name("held.py")
script = "from vonk_control.models import Base"
also = PROGRAM.read_text()
run(["docker", "exec", "control-api", "python", "-c", PROGRAM.read_text()])
run(("python3", "-c", Path(__file__).with_name("inline.py").read_text()))
compose("exec", "control-api", "/usr/local/bin/python", "-c", script)
compose("exec", "control-api", "python", "-c", also)
service["command"] = ["python", "/acceptance/mounted.py"]
"""

    programs = lane_programs(lane)

    assert programs.files == {"held.py", "inline.py", "mounted.py"}
    assert programs.inline == ("from vonk_control.models import Base",)
    assert programs.unread == ()


def test_a_program_form_the_guard_cannot_read_fails() -> None:
    lane = """
run(["python", "-m", "vonk_control.tool"])
run(["python", "-c", programs[0]])
run(["python", "-c", build_program()])
run(["python", "-c", unknown])
run(["python"])
command = ["python", *arguments]
"""

    assert len(lane_programs(lane).unread) == 6
