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
    assert blocks
    assert all(_pinned_case_counts(textwrap.dedent(block.group(2))) for block in blocks)


def test_guard_rejects_pinned_passed_summary_but_allows_nonempty_pattern() -> None:
    assert _PINNED_PASSED_SUMMARY.search("grep -F 'test result: ok. 1 passed;'")
    assert not _PINNED_PASSED_SUMMARY.search(
        r"grep -E 'test result: ok\. [1-9][0-9]* passed; 0 failed; 0 ignored;'"
    )


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
    # Shards are split by file without a collection pass; the wire bridges and
    # the consumer corpus boundaries are the only files the control shards
    # leave out, and each has its own regular CI job.
    assert "--ignore-glob='*_wire_bridge.py'" in suite
    assert "--ignore=control/tests/test_consumer_corpus_boundaries.py" in suite
    assert "not lane" not in run["run"]
    assert "--markers" not in run["run"]


def test_corpus_boundaries_run_where_the_probe_is_built_and_never_gate_shards() -> None:
    """Catches shards that wait for the Cargo build again, a corpus file that is
    left out of the shards but run nowhere, or a corpus suite pointed at a
    file the shards still run."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    assert workflow["jobs"]["control-suite"]["needs"] == "changes"
    probe_steps = workflow["jobs"]["consumer-probe"]["steps"]
    [corpus] = [s for s in probe_steps if s.get("run") == "scripts/test corpus"]
    assert corpus["env"]["VONK_CONSUMER_CORPUS_PROBE"].endswith(
        "target/debug/examples/consumer_corpus_probe"
    )
    build_index = probe_steps.index(
        next(s for s in probe_steps if "cargo build" in s.get("run", ""))
    )
    assert build_index < probe_steps.index(corpus)
    suite = (ROOT / "scripts/test").read_text()
    corpus_paths = re.search(
        r"^    corpus\)\n\s+paths=\(([^)]+)\)", suite, re.MULTILINE
    )
    assert corpus_paths is not None
    assert corpus_paths.group(1) == "control/tests/test_consumer_corpus_boundaries.py"
    assert (ROOT / corpus_paths.group(1)).is_file()


def test_pull_request_suites_sample_sweeps_and_gate_changed_test_budgets() -> None:
    """Catches a workflow that runs the full sweeps on every pull request again,
    that samples main's release run too, or that lists the changed tests
    without handing the list to the budget plugin."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    tier = workflow["env"]["VONK_TEST_TIER"]
    assert "github.event_name == 'pull_request'" in tier
    assert "github.event_name == 'merge_group'" in tier
    assert "&& 'pr' || 'full'" in tier
    for job, command in (
        ("repository-suite", "scripts/test repository --shard"),
        ("control-suite", "scripts/test control --shard"),
    ):
        steps = workflow["jobs"][job]["steps"]
        [listing] = [
            step for step in steps if "scripts/changed-tests" in step.get("run", "")
        ]
        assert "pull_request" in listing["if"] and "merge_group" in listing["if"]
        assert "merge_group.base_sha" in listing["env"]["BASE_SHA"]
        [run] = [step for step in steps if command in step.get("run", "")]
        assert "--test-budget-enforce" in run["run"]
        assert steps.index(listing) < steps.index(run)


def test_built_recipe_library_cache_is_saved_on_main_after_the_build_under_its_restore_key() -> (
    None
):
    """Catches a cache that survives a build-script change, is saved unbuilt, is
    saved under a key nothing restores, or is saved on pull requests."""
    action = yaml.safe_load(
        (ROOT / ".github/actions/recipe-library/action.yml").read_text()
    )
    steps = action["runs"]["steps"]
    [restore] = [s for s in steps if s.get("id") == "built"]
    [save] = [
        s
        for s in steps
        if s.get("uses", "").startswith("actions/cache/save")
        and "recipe-library-built" in s["with"]["key"]
    ]
    key = restore["with"]["key"]
    assert "hashFiles('scripts/build-recipe-library')" in key
    assert "revision" in key
    assert save["with"]["key"] == key
    assert "refs/heads/main" in save["if"]
    build = next(
        i for i, s in enumerate(steps) if "build-recipe-library" in s.get("run", "")
    )
    assert steps.index(save) > build


def test_main_warms_the_built_recipe_library() -> None:
    """ci.yml rarely runs on main, so the warm workflow saves the built tree."""
    warm = yaml.safe_load((ROOT / ".github/workflows/ci-cache-warm.yml").read_text())
    paths = warm[True]["push"]["paths"]
    for path in (
        "tests/acceptance/recipe-library-revision.txt",
        "scripts/build-recipe-library",
        ".github/actions/recipe-library/action.yml",
    ):
        assert path in paths
    uses = [s.get("uses") for s in warm["jobs"]["recipe-library"]["steps"]]
    assert "./.github/actions/recipe-library" in uses


def test_every_action_is_pinned_at_one_sha() -> None:
    """The same action is never pinned at two commits across workflows/actions."""
    pin = re.compile(r"uses:\s*([\w.-]+/[\w./-]+)@([0-9a-f]{40})")
    pins: dict[str, set[str]] = {}
    for path in (ROOT / ".github").rglob("*.y*ml"):
        for action, sha in pin.findall(path.read_text()):
            pins.setdefault(action, set()).add(sha)
    assert pins
    assert {a: sorted(s) for a, s in pins.items() if len(s) > 1} == {}


def test_every_job_has_a_timeout_and_a_pinned_runner_label() -> None:
    """No job may hang on the 6 h default or float on `ubuntu-latest`."""
    allowed = {"ubuntu-24.04", "ubuntu-24.04-arm"}
    bad: list[str] = []
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        text = path.read_text()
        for name, job in yaml.safe_load(text)["jobs"].items():
            if "uses" in job:  # reusable-workflow call: the callee's jobs carry both
                continue
            if not isinstance(job.get("timeout-minutes"), int):
                bad.append(f"{path.name}:{name} has no timeout-minutes")
            runner = job.get("runs-on")
            if runner == "${{ matrix.runner }}":
                matrix = job.get("strategy", {}).get("matrix", {})
                labels = {
                    e["runner"] for e in matrix.get("include", []) if "runner" in e
                }
                if not labels or not labels <= allowed:
                    bad.append(f"{path.name}:{name} matrix runners {sorted(labels)}")
            elif runner not in allowed:
                bad.append(f"{path.name}:{name} runs on {runner!r}")
    assert bad == []
