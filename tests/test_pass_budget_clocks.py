"""Pass-budget callers inherit a frozen clock, including indirect fixture calls.

The registry lives with the fixture. This mechanical guard checks that its
patch loop is autouse and registered with the caller's conftest; merely defining
an unused fake clock cannot satisfy it. Budget tests may replace it explicitly.
"""

from __future__ import annotations

import ast
from pathlib import Path

from control.tests.parsed_sources import parse_file

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = ROOT / "control/tests/pass_budget_clocks.py"


# These loops return one all-or-nothing decision or wait for one effect. They
# do not yield a partial collection pass; their tests own retry/stall clocks.
WAIT_DEADLINE_MODULES = {
    "vonk_control.artifact_blob_store",
    "vonk_control.route_runtime",
    "vonk_control.recipe_image_availability.requests",
    "vonk_control.exact_integer_adoption",
    "vonk_control.model_cache.download_admission",
    "vonk_control.model_cache.split_transfer",
    "vonk_control.agent_jobs.claim_notes",
    "vonk_control.fleet_profiles.selection",
    "vonk_control.db.connections",
}


def has_wall_deadline(tree: ast.Module) -> bool:
    """Recognize deadline = clock() + budget, including injected clocks."""
    return any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "deadline"
            for target in node.targets
        )
        and isinstance(node.value, ast.BinOp)
        and isinstance(node.value.op, ast.Add)
        and any(
            isinstance(call, ast.Call)
            and ast.unparse(call.func).rsplit(".", 1)[-1]
            in {"monotonic", "_monotonic", "time", "perf_counter"}
            for call in ast.walk(node.value)
        )
        for node in ast.walk(tree)
    )


def test_wall_clock_passes_have_registered_test_clocks() -> None:
    registered = set(budget_modules(parse_file(SUPPORT).tree))
    missing = []
    for path in sorted((ROOT / "control/src/vonk_control").rglob("*.py")):
        module = ".".join(path.relative_to(ROOT / "control/src").with_suffix("").parts)
        if (
            has_wall_deadline(parse_file(path).tree)
            and module not in registered | WAIT_DEADLINE_MODULES
        ):
            missing.append(module)
    assert not missing, (
        f"Register the pass's test clock (or classify a whole-effect wait): {missing}"
    )


def budget_modules(tree: ast.Module) -> tuple[str, ...]:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "PASS_BUDGET_MODULES"
            for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            assert isinstance(value, tuple) and all(isinstance(s, str) for s in value)
            return value
    raise AssertionError("Pass-budget clock registry is missing")


def has_clock_fixture(conftest: ast.Module, support: ast.Module) -> bool:
    imported = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "pass_budget_clocks"
        and any(alias.name == "frozen_pass_budgets" for alias in node.names)
        for node in conftest.body
    )
    for function in support.body:
        if (
            not isinstance(function, ast.FunctionDef)
            or function.name != "frozen_pass_budgets"
        ):
            continue
        autouse = any(
            isinstance(decorator, ast.Call)
            and ast.unparse(decorator.func) == "pytest.fixture"
            and any(
                keyword.arg == "autouse" and ast.literal_eval(keyword.value) is True
                for keyword in decorator.keywords
            )
            for decorator in function.decorator_list
        )
        patches_registry = any(
            isinstance(node, ast.For)
            and ast.unparse(node.target) == "module_name"
            and ast.unparse(node.iter) == "PASS_BUDGET_MODULES"
            and any(
                isinstance(call, ast.Call)
                and ast.unparse(call.func) == "monkeypatch.setattr"
                and [ast.unparse(arg) for arg in call.args]
                == ["import_module(module_name)", "'time'", "clock"]
                for call in ast.walk(node)
            )
            for node in ast.walk(function)
        )
        return imported and autouse and patches_registry
    return False


def budget_imports(tree: ast.Module, modules: tuple[str, ...]) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")
            imports.update(f"{node.module}.{alias.name}" for alias in node.names)
    return {
        module
        for module in modules
        if any(
            name == module or module.startswith(f"{name}.")
            for name in imports
            if name.startswith("vonk_control.")
        )
    }


def test_budgeted_callers_inherit_the_autouse_clock_fixture() -> None:
    support = parse_file(SUPPORT).tree
    modules = budget_modules(support)
    active = has_clock_fixture(
        parse_file(ROOT / "control/tests/conftest.py").tree, support
    )
    failures = []
    for directory in (ROOT / "control/tests", ROOT / "tests"):
        for path in sorted(directory.rglob("*.py")):
            if budget_imports(parse_file(path).tree, modules) and (
                not active or not path.is_relative_to(ROOT / "control/tests")
            ):
                failures.append(str(path.relative_to(ROOT)))
    assert not failures, f"Budgeted callers need the autouse pass clock: {failures}"
    assert active, "The shared pass clock must freeze every registered lookup owner"


def test_guard_rejects_an_unused_or_incomplete_clock_fixture() -> None:
    """Catches removing autouse, conftest registration or one side of the patch loop."""
    support = parse_file(SUPPORT).source
    conftest = parse_file(ROOT / "control/tests/conftest.py").source
    assert has_clock_fixture(ast.parse(conftest), ast.parse(support))
    assert not has_clock_fixture(ast.parse(""), ast.parse(support))
    assert not has_clock_fixture(
        ast.parse(conftest), ast.parse(support.replace("autouse=True", "autouse=False"))
    )
    assert not has_clock_fixture(
        ast.parse(conftest),
        ast.parse(support.replace('"time", clock', '"unrelated", clock')),
    )
    assert budget_imports(
        ast.parse(
            "from vonk_control import catalog_revision_collection as collector\ncollector.CatalogRevisionCollector(sessions, clock=now).collect()"
        ),
        budget_modules(ast.parse(support)),
    ) == {"vonk_control.catalog_revision_collection"}


def test_deadline_scan_sees_new_literal_constant_and_injected_budgets() -> None:
    for source in (
        "deadline = time.monotonic() + 2.0",
        "deadline = time.perf_counter() + PASS_SECONDS",
        "deadline = self._monotonic() + self._budget",
        "deadline = time.time() + PASS_SECONDS",
    ):
        assert has_wall_deadline(ast.parse(source))
    assert not has_wall_deadline(ast.parse("cutoff = time.time() - GRACE_SECONDS"))


def uninjected_stall_calls(tree: ast.Module) -> list[int]:
    names = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "vonk_control.stalled_process"
        for alias in node.names
        if alias.name == "run_until_stalled"
    }
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            isinstance(node.func, ast.Name)
            and node.func.id in names
            or ast.unparse(node.func).endswith(".run_until_stalled")
        )
        and not any(keyword.arg == "clock" for keyword in node.keywords)
    ]


def test_stall_monitor_tests_inject_the_existing_clock() -> None:
    """A subsecond real stall window can kill a healthy writer on a loaded host."""
    failures = {
        str(path.relative_to(ROOT)): lines
        for directory in (ROOT / "control/tests", ROOT / "tests")
        for path in sorted(directory.rglob("*.py"))
        if (lines := uninjected_stall_calls(parse_file(path).tree))
    }
    assert not failures, f"Inject the stall monitor's clock: {failures}"


def test_guard_rejects_a_stall_monitor_using_the_real_clock() -> None:
    prefix = "from vonk_control.stalled_process import run_until_stalled as run\n"
    assert uninjected_stall_calls(
        ast.parse(prefix + "run(command, window=window)")
    ) == [2]
    assert not uninjected_stall_calls(
        ast.parse(prefix + "run(command, window=window, clock=fake)")
    )
    assert uninjected_stall_calls(ast.parse("monitor.run_until_stalled(command)")) == [
        1
    ]
