"""Tests prove behavior without proof-output or provenance environment inputs."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

from .parsed_sources import parsed_tree

ROOT = Path(__file__).resolve().parents[2]
_PROOF_ENVIRONMENT = re.compile(r"(?:[A-Z][A-Z0-9]*_)*PROOF(?:_[A-Z0-9]+)*")


def _proof_environment_names(tree: ast.AST) -> set[str]:
    """Reserve proof env names, including aliases and subprocess -c programs.

    Reject declarations as well as reads so moving a key into a variable or an
    environment mapping cannot evade the guard. Ordinary proof prose is valid.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _PROOF_ENVIRONMENT.fullmatch(node.value)
        ):
            names.add(node.value)
        if isinstance(node, (ast.List, ast.Tuple)):
            for flag, program in zip(node.elts, node.elts[1:]):
                if (
                    isinstance(flag, ast.Constant)
                    and flag.value == "-c"
                    and isinstance(program, ast.Constant)
                    and isinstance(program.value, str)
                ):
                    try:
                        embedded = ast.parse(program.value)
                    except SyntaxError:
                        continue  # Shell -c programs are not Python modules.
                    names.update(_proof_environment_names(embedded))
    return names


def test_tests_and_helpers_have_no_proof_environment_dependencies() -> None:
    """Retired workflow metadata must not break behavioral tests with KeyError."""
    violations = [
        f"{path.relative_to(ROOT)}: {', '.join(sorted(names))}"
        for root in (ROOT / "control/tests", ROOT / "tests")
        for path, parsed in parsed_tree(root)
        if (names := _proof_environment_names(parsed.tree))
    ]
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize(
    "source",
    [
        "os.environ['VONK_EFFECT_PROOF_OUTPUT']",
        "os.environ.get('VONK_PROOF_OUTPUT')",
        "os.getenv('VONK_PROOF_PATH', '')",
        "from os import getenv as read; read('VONK_PROOF_OUTPUT')",
        "key = 'VONK_PROOF_OUTPUT'; environment[key]",
        "environment.get('VONK_SIGNED_UPDATE_PROOF')",
        """subprocess.run([python, '-c', "import os; os.environ['VONK_PROOF_OUTPUT']"])""",
    ],
)
def test_guard_rejects_proof_environment_reads(source: str) -> None:
    assert _proof_environment_names(ast.parse(source))


@pytest.mark.parametrize(
    "source",
    [
        "os.environ['VONK_TEST_DATABASE_URL']",
        "os.environ.get('VONK_SIGNED_UPDATE_LANE_ENABLED')",
        "output = tmp_path / 'source.json'",
        "description = 'Hosted proof of installed CLI behavior'",
    ],
)
def test_guard_preserves_test_inputs_and_behavioral_fixtures(source: str) -> None:
    assert not _proof_environment_names(ast.parse(source))


def test_kept_proof_workflows_supply_behavioral_inputs_only() -> None:
    """A workflow cannot reintroduce the proof keys forbidden in its tests."""
    for path in (ROOT / ".github/workflows").glob("*-proof.y*ml"):
        workflow = yaml.safe_load(path.read_text())
        scopes = [workflow]
        for job in workflow["jobs"].values():
            scopes.append(job)
            scopes.extend(job["steps"])
        for scope in scopes:
            assert not any(
                _PROOF_ENVIRONMENT.fullmatch(name) for name in scope.get("env", {})
            ), path.name
