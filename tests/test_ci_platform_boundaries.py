from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _workflow_job_lines(workflow: str, job_name: str) -> list[str]:
    lines = workflow.splitlines()
    job_start = lines.index(f"  {job_name}:") + 1
    job_lines: list[str] = []
    for line in lines[job_start:]:
        if re.fullmatch(r"  [a-zA-Z0-9_-]+:", line):
            break
        job_lines.append(line)
    return job_lines


def _named_workflow_steps(job_name: str) -> list[dict[str, str]]:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    job_lines = _workflow_job_lines(workflow, job_name)

    steps: list[dict[str, str]] = []
    for line in job_lines:
        name = re.fullmatch(r"      - name: (.+)", line)
        if name is not None:
            steps.append({"name": name.group(1)})
            continue
        run = re.fullmatch(r"        run: (.+)", line)
        if run is not None and steps:
            steps[-1]["run"] = run.group(1)
    return steps


def test_pr_smoke_runs_locked_web_and_focused_contracts() -> None:
    steps = _named_workflow_steps("test")
    web_step = {
        "name": "Install locked admin web dependencies",
        "run": "npm ci --prefix control/web",
    }
    repository_step = "Run focused repository contracts"
    control_step = "Run focused control cleanup contracts"

    assert web_step in steps
    step_names = [step["name"] for step in steps]
    assert repository_step in step_names
    assert control_step in step_names
    assert step_names.index(web_step["name"]) < step_names.index(control_step)

    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    test_job_lines = _workflow_job_lines(workflow, "test")
    assert "    runs-on: ubuntu-latest" in test_job_lines
    assert "    strategy:" not in test_job_lines
    assert "macos-latest" not in test_job_lines

    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in workflow
    assert "timeout-minutes: 15" in workflow


def test_pr_smoke_does_not_reintroduce_a_second_os_matrix() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert workflow.count("macos-latest") == 0
    assert workflow.count("runs-on: ubuntu-latest") >= 4


def test_repository_and_service_suites_run_in_parallel_with_stable_aggregate() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    rust = "\n".join(_workflow_job_lines(workflow, "rust"))
    repository = "\n".join(_workflow_job_lines(workflow, "repository-suite"))
    control = "\n".join(_workflow_job_lines(workflow, "control-suite"))
    web = "\n".join(_workflow_job_lines(workflow, "web-suite"))
    browser = "\n".join(_workflow_job_lines(workflow, "web-browser-acceptance"))
    aggregate = "\n".join(_workflow_job_lines(workflow, "catalog-runtime"))

    assert "cargo test --workspace --locked" in rust
    assert "uv run --project agent_protocol --frozen pytest -q" in rust
    assert "name: Complete repository suite (${{ matrix.shard.label }})" in repository
    assert "fail-fast: false" in repository
    assert repository.count("index:") == 4
    assert "SHARD_TOTAL: 4" in repository
    assert "pytest --collect-only -q" in repository
    assert "awk '/^tests\\/.*::/'" in repository
    assert "position % SHARD_TOTAL == SHARD_INDEX" in repository
    assert 'shard_tests+=("${repository_tests[$position]}")' in repository
    assert '"${shard_tests[@]}"' in repository
    assert "pytest-xdist" not in repository
    assert "--python 3.12" in repository
    assert "name: Complete control suite (${{ matrix.shard.label }})" in control
    assert "fail-fast: false" in control
    assert control.count("index:") == 4
    assert "SHARD_TOTAL: 4" in control
    assert "pytest --collect-only -q control/tests" in control
    assert "awk '/^tests\\/.*::/'" in control
    assert "position % SHARD_TOTAL == SHARD_INDEX" in control
    assert 'shard_tests+=("control/${control_tests[$position]}")' in control
    assert '"${shard_tests[@]}"' in control
    assert "pytest-xdist==3.8.0" in control
    assert "pytest -q -n auto --dist loadfile" in control
    assert "--python 3.12" in control
    assert "npm test --prefix control/web -- --run" in web
    assert "npm run build --prefix control/web" in web
    assert "npm run test:e2e --prefix control/web -- --project=chromium" in browser
    assert "name: Catalog and service suites" in aggregate
    assert (
        "needs: [repository-suite, control-suite, web-suite, web-browser-acceptance]"
        in aggregate
    )
    assert "if: always()" in aggregate
    assert "needs.repository-suite.result" in aggregate
    assert "needs.control-suite.result" in aggregate
    assert "needs.web-suite.result" in aggregate
    assert "needs.web-browser-acceptance.result" in aggregate
    assert "  agent-suite:" not in workflow
    assert "AGENT_RESULT" not in aggregate
    assert "scripts/update-global-contracts" not in workflow
