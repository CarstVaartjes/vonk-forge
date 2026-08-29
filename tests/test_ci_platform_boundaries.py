from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETUP_NODE = (
    "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020 # v7.0.0"
)


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
        name = re.fullmatch(r"\s+- name: (.+)", line)
        if name is not None:
            steps.append({"name": name.group(1)})
            continue
        run = re.fullmatch(r"\s+run: (.+)", line)
        if run is not None and steps:
            steps[-1]["run"] = run.group(1)
    return steps


def test_pr_smoke_runs_locked_web_and_focused_contracts() -> None:
    steps = _named_workflow_steps("test")
    repository_step = "Run focused repository contracts"
    control_step = "Run focused control cleanup contracts"

    step_names = [step["name"] for step in steps]
    assert repository_step in step_names
    assert control_step in step_names
    assert "Install locked admin web dependencies" not in step_names

    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    test_job_lines = _workflow_job_lines(workflow, "test")
    assert "    runs-on: ubuntu-latest" in test_job_lines
    assert "    strategy:" not in test_job_lines
    assert "      - parallel:" in test_job_lines
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
    rust_quality = "\n".join(_workflow_job_lines(workflow, "rust-quality"))
    rust_tests = "\n".join(_workflow_job_lines(workflow, "rust-tests"))
    rust_platform = "\n".join(_workflow_job_lines(workflow, "rust-platform"))
    rust = "\n".join(_workflow_job_lines(workflow, "rust"))
    generated = "\n".join(_workflow_job_lines(workflow, "generated-clients"))
    repository = "\n".join(_workflow_job_lines(workflow, "repository-suite"))
    control = "\n".join(_workflow_job_lines(workflow, "control-suite"))
    web = "\n".join(_workflow_job_lines(workflow, "web-suite"))
    browser = "\n".join(_workflow_job_lines(workflow, "web-browser-acceptance"))
    aggregate = "\n".join(_workflow_job_lines(workflow, "catalog-runtime"))

    assert "cargo test" not in rust_quality
    assert "Install uv and Python" not in rust_quality
    assert "setup-uv@" not in rust_quality
    assert "name: Rust workspace tests (${{ matrix.shard.label }})" in rust_tests
    assert "fail-fast: false" in rust_tests
    assert "max-parallel: 2" in rust_tests
    assert rust_tests.count("- id:") == 2
    assert "RUST_TEST_SHARD: ${{ matrix.shard.id }}" in rust_tests
    assert "cargo test --workspace --exclude vonk-nas-setup --locked" in rust_tests
    assert "cargo test -p vonk-nas-setup --locked" in rust_tests
    assert "Swatinem/rust-cache@6323deb102c322ba6fcbdcafc7e3dddab59af2b6" in (
        rust_tests
    )
    assert 'shared-key: linux-amd64' in rust_tests
    assert 'add-job-id-key: "false"' in rust_tests
    assert 'save-if: "false"' in rust_tests
    assert "uv run --project agent_protocol --frozen pytest -q" in rust_platform
    assert "      - parallel:" in rust_platform
    assert rust_platform.index("Install Spark-compatible rootless Podman") < (
        rust_platform.index("      - parallel:")
    )
    assert rust_platform.index("      - parallel:") < rust_platform.index(
        "Upload GPU node service exposure"
    )
    assert "Install uv and Python" in rust_platform
    assert "setup-uv@" in rust_platform
    assert "Swatinem/rust-cache@6323deb102c322ba6fcbdcafc7e3dddab59af2b6" in (
        rust_quality
    )
    assert "save-if:" not in rust_quality
    assert 'save-if: "false"' in rust_platform
    assert "needs: [rust-quality, rust-tests, rust-platform]" in rust
    assert "needs.rust-quality.result" in rust
    assert "needs.rust-tests.result" in rust
    assert "needs.rust-platform.result" in rust
    assert "tests/control/test_openapi_clients.py" in generated
    assert "npm ci --prefix tools/openapi-client" in generated
    assert SETUP_NODE in generated
    assert 'node-version: "24"' in generated
    assert "cache: npm" in generated
    assert (
        "cache-dependency-path: tools/openapi-client/package-lock.json" in generated
    )
    assert "control/web/package-lock.json" not in generated
    assert "node_modules" not in generated
    assert "name: Complete repository suite (${{ matrix.shard.label }})" in repository
    assert "fail-fast: false" in repository
    assert repository.count("index:") == 6
    assert "SHARD_TOTAL: 6" in repository
    assert "pytest --collect-only -q" in repository
    assert "test_openapi_clients\\.py::/" in repository
    assert "npm ci --prefix" not in repository
    assert "position % SHARD_TOTAL == SHARD_INDEX" in repository
    assert 'shard_tests+=("${repository_tests[$position]}")' in repository
    assert '"${shard_tests[@]}"' in repository
    assert "pytest-xdist" not in repository
    assert "--python 3.12" in repository
    assert "name: Complete control suite (${{ matrix.shard.label }})" in control
    assert "fail-fast: false" in control
    assert control.count("index:") == 6
    assert "SHARD_TOTAL: 6" in control
    assert "pytest --collect-only -q control/tests" in control
    assert "awk '/^tests\\/.*::/'" in control
    assert "position % SHARD_TOTAL == SHARD_INDEX" in control
    assert 'shard_tests+=("control/${control_tests[$position]}")' in control
    assert '"${shard_tests[@]}"' in control
    assert "pytest-xdist==3.8.0" in control
    assert "pytest -q -n auto --dist loadfile" in control
    assert "--python 3.12" in control
    assert "Pull PostgreSQL test image" not in control
    assert "docker pull" not in control
    assert "postgres:18.6@sha256:" not in control
    assert "npm test --prefix control/web -- --run" in web
    assert "npm run build --prefix control/web" in web
    assert "npm run test:e2e --prefix control/web -- --project=chromium" in browser
    for node_job in (web, browser):
        assert SETUP_NODE in node_job
        assert 'node-version: "24"' in node_job
        assert "cache: npm" in node_job
        assert "cache-dependency-path: control/web/package-lock.json" in node_job
        assert "tools/openapi-client/package-lock.json" not in node_job
        assert "node_modules" not in node_job
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


def test_browser_acceptance_balances_individual_tests_across_two_workers() -> None:
    config = (ROOT / "control/web/playwright.config.ts").read_text()

    assert "fullyParallel: true" in config
    assert "workers: 2" in config
