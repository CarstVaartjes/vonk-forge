"""Proof coverage may grow: fixed execution totals must never become CI gates."""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
_PINNED_PASSED_SUMMARY = re.compile(r"\b[0-9]+\s+passed\b")
_PYTHON_HEREDOC = re.compile(
    r"^[^\n]*\bpython(?:3)?\b[^\n]*<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n"
    r"(.*?)^\s*\1\s*$",
    re.MULTILINE | re.DOTALL,
)


def _pinned_case_counts(source: str) -> list[int]:
    """Find equalities on case totals, including aliases and per-engine sums.

    Source bytes/digests and input replacement counts are deliberately unrelated
    to execution totals and remain valid integrity assertions.
    """
    tree = ast.parse(source)
    collections = {"cases", "testcases", "test_cases", "executed_cases"}
    counts: set[str] = set()

    def references_cases(node: ast.AST) -> bool:
        return any(
            isinstance(child, ast.Name) and child.id in collections
            for child in ast.walk(node)
        )

    def is_case_count(node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in counts
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in {"len", "sum"}:
                return references_cases(node)
            if isinstance(node.func, ast.Attribute) and node.func.attr == "get":
                return any(
                    isinstance(arg, ast.Constant) and arg.value == "tests"
                    for arg in node.args[:1]
                )
        if isinstance(node, ast.Subscript):
            return isinstance(node.slice, ast.Constant) and node.slice.value == "tests"
        return any(is_case_count(child) for child in ast.iter_child_nodes(node))

    # Discover JUnit collections and count aliases regardless of variable spelling.
    assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)]
    for _ in range(len(assignments) + 1):
        previous = collections.copy(), counts.copy()
        for node in assignments:
            names = {
                child.id
                for target in node.targets
                for child in ast.walk(target)
                if isinstance(child, ast.Name)
            }
            if is_case_count(node.value):
                counts.update(names)
            elif references_cases(node.value) or any(
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr in {"findall", "iter"}
                and any(
                    isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)
                    and "testcase" in arg.value
                    for arg in child.args
                )
                for child in ast.walk(node.value)
            ):
                collections.update(names)
        if previous == (collections, counts):
            break

    def is_case_inventory(node: ast.AST) -> bool:
        return isinstance(node, ast.SetComp) and references_cases(node)

    return [
        node.lineno
        for assertion in ast.walk(tree)
        if isinstance(assertion, ast.Assert)
        for node in ast.walk(assertion.test)
        if isinstance(node, ast.Compare)
        for left, op, right in zip(
            [node.left, *node.comparators], node.ops, node.comparators
        )
        if isinstance(op, ast.Eq)
        and (
            is_case_count(left)
            or is_case_count(right)
            or is_case_inventory(left)
            or is_case_inventory(right)
        )
    ]


def test_workflows_never_pin_executed_case_counts() -> None:
    """Adding a passing regression must not invalidate proof execution evidence."""
    problems = []
    workflows = ROOT / ".github" / "workflows"
    for path in sorted((*workflows.glob("*.yml"), *workflows.glob("*.yaml"))):
        content = path.read_text(encoding="utf-8")
        for line, text in enumerate(content.splitlines(), 1):
            if "grep" in text and _PINNED_PASSED_SUMMARY.search(text):
                problems.append(f"{path.name}:{line}: pinned passed-test summary")
        for block in _PYTHON_HEREDOC.finditer(content):
            source = textwrap.dedent(block.group(2))
            offset = content[: block.start(2)].count("\n")
            for line in _pinned_case_counts(source):
                problems.append(f"{path.name}:{offset + line}: pinned execution count")
    assert not problems, "Use nonempty coverage and named subsets:\n" + "\n".join(
        problems
    )


@pytest.mark.parametrize(
    "source",
    [
        "assert len(cases) == 34",
        "assert 34 == len(cases)",
        "assert (\n len(cases)\n == 34\n)",
        "assert sum(engine in case.name for case in cases) == 15",
        "assert len(cases) == len(expected)",
        "assert {case.get('name') for case in cases} == expected",
        "total = len(cases)\nassert total == 34",
        "rows = root.findall('.//testcase')\nassert len(rows) == 34",
        "assert int(suite.get('tests')) == 34",
        "assert int(suite.attrib['tests']) == 34",
    ],
)
def test_guard_rejects_equal_execution_counts(source: str) -> None:
    assert _pinned_case_counts(source)


@pytest.mark.parametrize(
    "source",
    [
        "assert cases",
        "assert len(cases) > 0",
        "assert expected <= {case.get('name') for case in cases}",
        "assert any(engine in case.name for case in cases)",
        "assert all(case.find(tag) is None for case in cases)",
        "assert source.count(needle) == 1",
        "assert source_sha == expected_sha",
        "assert digest == inputs[path]['sha256']",
        "assert int(suite.get('failures')) == 0",
    ],
)
def test_guard_preserves_coverage_outcomes_and_integrity(source: str) -> None:
    assert not _pinned_case_counts(source)


def test_guard_reads_every_python_heredoc() -> None:
    source = """          python - <<'PY'
          assert len(cases) == 34
          PY
          uv run --project control --frozen python3 - <<'PYTHON'
          assert sum(1 for case in cases) == 15
          PYTHON
    """
    blocks = list(_PYTHON_HEREDOC.finditer(source))
    assert len(blocks) == 2
    assert all(_pinned_case_counts(textwrap.dedent(block.group(2))) for block in blocks)


def test_guard_rejects_pinned_passed_summary_but_allows_nonempty_pattern() -> None:
    assert _PINNED_PASSED_SUMMARY.search("grep -F 'test result: ok. 1 passed;'")
    assert not _PINNED_PASSED_SUMMARY.search(
        r"grep -E 'test result: ok\. [1-9][0-9]* passed; 0 failed; 0 ignored;'"
    )


_PROOF_PROVENANCE = re.compile(
    r"\bhashlib\b|attest-build-provenance|provenance|git rev-parse HEAD:",
    re.IGNORECASE,
)


def test_proof_lane_inventory_only_shrinks() -> None:
    """Another standalone proof must not duplicate the complete CI suites."""
    workflows = list((ROOT / ".github/workflows").glob("*-proof.y*ml"))
    assert len(workflows) <= 5, "Use regular CI for tests with shared prerequisites"
    assert sum(len(yaml.safe_load(path.read_text())["jobs"]) for path in workflows) <= 5


def test_proof_workflows_do_not_record_source_provenance() -> None:
    """Splitting a source module cannot break an unrelated outcome check."""
    for path in (ROOT / ".github/workflows").glob("*-proof.y*ml"):
        assert not _PROOF_PROVENANCE.search(path.read_text()), path.name


@pytest.mark.parametrize(
    "source",
    [
        "hashlib.sha256(Path(path).read_bytes()).hexdigest()",
        "uses: actions/attest-build-provenance@revision",
        "git rev-parse HEAD:rust/crates/vonk-agent/src/client.rs",
        "(output / 'source-provenance.json').write_text(report)",
    ],
)
def test_guard_rejects_proof_provenance(source: str) -> None:
    assert _PROOF_PROVENANCE.search(source)


@pytest.mark.parametrize(
    "source",
    [
        "assert expected <= {case.get('name') for case in cases}",
        "uv pip install --require-hashes -r locked-requirements.txt",
        "assert verified_wheel_sha256 == manifest_sha256",
    ],
)
def test_guard_preserves_outcomes_and_ingress_integrity(source: str) -> None:
    assert not _PROOF_PROVENANCE.search(source)


def test_control_ci_collects_lanes_and_requires_oci_ingress(monkeypatch) -> None:
    """Retiring OCI proof jobs must not silently skip their real ingress tests."""
    from tools import pytest_prereqs

    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["control-suite"]["steps"]
    assert any(
        "skopeo" in step.get("with", {}).get("packages", "").split() for step in steps
    )
    [run] = [
        step for step in steps if "scripts/test control --shard" in step.get("run", "")
    ]
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("VONK_CI_PREREQUISITES", run["env"]["VONK_CI_PREREQUISITES"])
    assert pytest_prereqs._required_in_ci("needs_skopeo")
    assert "needs_skopeo" in pytest_prereqs._CHECKS
    # Full collection includes prerequisite/lane markers; only image and wire
    # bridges have their own regular CI jobs.
    suite = (ROOT / "scripts/test").read_text()
    assert 'suite_markers="not built_image"' in suite
    assert "--collect-only" in suite
    assert "not lane" not in run["run"]
    assert "--markers" not in run["run"]
