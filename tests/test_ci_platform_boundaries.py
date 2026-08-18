from __future__ import annotations

import os
import re
import subprocess
import sys
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
    repository = "\n".join(_workflow_job_lines(workflow, "repository-suite"))
    control = "\n".join(_workflow_job_lines(workflow, "control-suite"))
    agent = "\n".join(_workflow_job_lines(workflow, "agent-suite"))
    web = "\n".join(_workflow_job_lines(workflow, "web-suite"))
    aggregate = "\n".join(_workflow_job_lines(workflow, "catalog-runtime"))

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
    assert "pytest agent/tests -q" in agent
    assert "pytest-xdist" not in agent
    assert " -n " not in agent
    assert "npm test --prefix control/web -- --run" in web
    assert "npm run build --prefix control/web" in web
    assert "name: Catalog and service suites" in aggregate
    assert (
        "needs: [repository-suite, control-suite, agent-suite, web-suite]"
        in aggregate
    )
    assert "if: always()" in aggregate
    assert "needs.repository-suite.result" in aggregate
    assert "needs.control-suite.result" in aggregate
    assert "needs.agent-suite.result" in aggregate
    assert "needs.web-suite.result" in aggregate
    assert "scripts/update-global-contracts" not in workflow


def test_agent_simulator_preserves_exact_non_linux_boundaries() -> None:
    command = (
        "import sys, types; "
        "sys.platform = 'darwin'; "
        "sys.modules['_scproxy'] = types.SimpleNamespace("
        "_get_proxy_settings=lambda: {}, _get_proxies=lambda: {}); "
        "import pytest; "
        "raise SystemExit(pytest.main(["
        "'-q', 'tests/agent/test_failure_matrix.py']))"
    )

    result = subprocess.run(
        [sys.executable, "-c", command],
        cwd=ROOT,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert re.search(r"\d+ passed, 10 skipped", result.stdout)


def test_linux_node_runtime_cases_skip_on_non_linux_hosts() -> None:
    test_cases = (
        (
            "tests/nodes/test_inspect_node_identity.py::"
            "test_identity_probe_emits_hashes_and_public_fingerprints_not_raw_identity"
        ),
        (
            "tests/nodes/test_inspect_node_identity.py::"
            "test_identity_probe_marks_invalid_machine_id_for_console_repair"
        ),
        (
            "tests/nodes/test_install_ssh_hardening.py::"
            "test_check_apply_verify_and_second_apply_are_idempotent"
        ),
        (
            "tests/nodes/test_install_ssh_hardening.py::"
            "test_foreign_target_is_refused_and_preserved"
        ),
        (
            "tests/nodes/test_install_ssh_hardening.py::"
            "test_rollback_removes_only_matching_managed_drop_in"
        ),
    )
    command = (
        "import sys; "
        "sys.platform = 'darwin'; "
        "import pytest; "
        f"raise SystemExit(pytest.main({['-q', *test_cases]!r}))"
    )

    result = subprocess.run(
        [sys.executable, "-c", command],
        cwd=ROOT,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "5 skipped" in result.stdout


def test_vonk_agent_installer_preserves_exact_non_linux_boundaries(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    compiler = fake_bin / "cc"
    compiler.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "target = Path(sys.argv[sys.argv.index('-o') + 1])\n"
        "target.write_bytes(bytes.fromhex('cffaedfe') + bytes(60))\n"
        "os.chmod(target, 0o755)\n"
    )
    compiler.chmod(0o755)
    command = (
        "import sys; "
        "sys.platform = 'darwin'; "
        "import pytest; "
        "raise SystemExit(pytest.main(["
        "'-vv', 'tests/nodes/test_install_vonk_agent.py', "
        "'-k', 'not production_root_chowns_only_a_new_service_directory']))"
    )

    result = subprocess.run(
        [sys.executable, "-c", command],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    runtime_cases = (
        "test_install_is_idempotent_generic_and_retains_license_provenance",
        "test_reinstall_restores_token_without_durable_node_bound_active_identity",
        "test_reinstall_suppresses_token_only_for_durable_node_bound_active_identity",
        "test_private_key_or_mixed_ca_input_is_rejected_before_target_mutation",
        "test_non_ca_x509_certificate_is_rejected_before_target_mutation",
        "test_ca_der_with_appended_bytes_is_rejected_before_target_mutation",
        "test_file_publication_resists_parent_and_temporary_inode_substitution",
        "test_root_publication_rejects_untrusted_existing_parent",
        "test_abandoned_publication_crash_boundaries_recover_bounded_exact_staging[create]",
        "test_abandoned_publication_crash_boundaries_recover_bounded_exact_staging[write]",
        "test_abandoned_publication_crash_boundaries_recover_bounded_exact_staging[file-fsync]",
        "test_abandoned_publication_crash_boundaries_recover_bounded_exact_staging[tree-fsync]",
        "test_abandoned_publication_crash_boundaries_recover_bounded_exact_staging[rename]",
        "test_abandoned_publication_crash_boundaries_recover_bounded_exact_staging[parent-fsync]",
        "test_missing_rollback_path_fails_before_mutation_and_all_units_are_enabled",
        "test_concurrent_first_install_is_serialized",
        "test_reinstall_rejects_unexpected_symlink_inside_immutable_tree",
        "test_distinct_explicit_node_ids_generate_distinct_configs",
        "test_installer_never_copies_admin_ca_ssh_or_old_node_private_keys",
        "test_extra_archive_member_fails_closed",
    )
    portable_cases = (
        "test_nvidia_lock_binds_exact_archive_license_provenance_and_installed_subset",
        "test_installer_exists_as_networkless_node_local_primitive",
        "test_installer_rejects_noncanonical_node_before_mutation",
        "test_account_contract_rejects_root_wrong_home_group_and_admin_membership",
        "test_installer_locks_before_account_resolution",
        "test_symlink_input_fails_closed",
        "test_wrong_architecture_fails_closed",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    for case in runtime_cases:
        assert f"::{case} SKIPPED" in result.stdout
    for case in portable_cases:
        assert f"::{case} PASSED" in result.stdout
    assert re.search(r"\d+ passed, 21 skipped, 1 deselected", result.stdout)


def test_image_runtime_case_skips_when_only_compose_is_available(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "[[ ${1:-} == compose ]] || {\n"
        "  echo 'only docker compose is available' >&2\n"
        "  exit 2\n"
        "}\n"
        "exit 0\n"
    )
    docker.chmod(0o755)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/runbooks/test_agent_pki.py",
            "-k",
            "pinned_step",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed, 1 skipped" in result.stdout
