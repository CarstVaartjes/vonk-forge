"""Lane programs that run inside an installed Controller use only its stable surface.

The upgrade-carry lane hands Python programs from the candidate's tree to
``python -c`` inside the installed ``control-api`` container: first the
previous release's, then the candidate's. There ``vonk_control`` is whichever
release is installed. Release run 42404137945 failed in the previous release
because the canary import program built ``CatalogModelEntry`` with the field
the candidate had just added.

So such a program names only the Controller entry points below, which every
release ships and calls the same way, and never constructs the Controller's
own data types: the installed Controller's parser and services make those.
Imports under ``TYPE_CHECKING`` never execute and are free. Programs are found
from the lane code, so a new one cannot escape the check.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LANE = (
    ROOT / "tests/acceptance/test_spark_lifecycle.py",
    ROOT / "tests/acceptance/spark_upgrade_carry.py",
)

# Module -> names a program may import. Each one exists, with the same call
# shape, in the previous release. Adding a name means checking that release.
# Never the Controller's data types (CatalogModelEntry, RecipeLibraryItem,
# RecipeLibrarySnapshot, RecipePackageEntry): their fields follow the release.
ENTRY_POINTS: dict[str, frozenset[str]] = {
    "vonk_agent_protocol": frozenset({"CatalogSyncCode"}),
    "vonk_control.auth": frozenset({"TokenCodec"}),
    "vonk_control.catalog_service": frozenset({"CatalogService"}),
    "vonk_control.catalog_sync": frozenset(
        {
            "CatalogSyncError",
            "CatalogSyncView",
            "ManagedRecipeCatalogSyncService",
            "SyncTrigger",
        }
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
PLATFORM = ("vonk_", "importlib")


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


def _program_files(lane: Path) -> set[Path]:
    """Files the lane passes to ``python -c`` by their ``.read_text()``."""
    tree = ast.parse(lane.read_text(encoding="utf-8"))
    paths: dict[str, str] = {}
    for node in ast.walk(tree):
        # NAME = Path(__file__).with_name("program.py")
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "with_name"
            and node.value.args
            and isinstance(node.value.args[0], ast.Constant)
            and isinstance(node.value.args[0].value, str)
        ):
            paths[node.targets[0].id] = node.value.args[0].value
    programs: set[Path] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        arguments = node.args
        for position, argument in enumerate(arguments[:-1]):
            if not (isinstance(argument, ast.Constant) and argument.value == "-c"):
                continue
            program = arguments[position + 1]
            if (
                isinstance(program, ast.Call)
                and isinstance(program.func, ast.Attribute)
                and program.func.attr == "read_text"
                and isinstance(program.func.value, ast.Name)
            ):
                name = program.func.value.id
                assert name in paths, f"{lane.name}: -c program {name} is unresolved"
                programs.add(lane.with_name(paths[name]))
    return programs


def _inline_programs(lane: Path) -> list[str]:
    """Literal ``python -c`` programs in the lane code."""
    tree = ast.parse(lane.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for position, argument in enumerate(node.args[:-1]):
            if isinstance(argument, ast.Constant) and argument.value == "-c":
                program = node.args[position + 1]
                text = "".join(
                    part.value
                    for part in ast.walk(program)
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                )
                found.append(f"{lane.name}:{node.lineno}:{text}")
    return found


def test_lane_programs_use_only_the_stable_controller_surface() -> None:
    programs = set().union(*(_program_files(lane) for lane in LANE))
    assert ROOT / "tests/acceptance/spark_canary_catalog_import.py" in programs
    offenders = {
        path.name: violations
        for path in sorted(programs)
        if (violations := surface_violations(path.read_text(encoding="utf-8")))
    }
    assert not offenders, offenders


def test_inline_lane_programs_do_not_reach_into_the_controller() -> None:
    offenders = [
        program
        for lane in LANE
        for program in _inline_programs(lane)
        if any(marker in program for marker in PLATFORM)
    ]
    assert not offenders, offenders


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
