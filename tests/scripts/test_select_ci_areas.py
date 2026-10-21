from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _module():
    loader = importlib.machinery.SourceFileLoader(
        "select_ci_areas", str(ROOT / "scripts/select-ci-areas")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_documentation_still_runs_every_fast_suite() -> None:
    module = _module()
    selected = module.select(["docs/operator.md"], "pull_request")
    assert all(selected[area] for area in module.FAST_AREAS)
    assert not any(
        selected[area] for area in ("compose", "nas_install", "agent_package")
    )


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "unknown"])
def test_missing_diff_is_conservative(event: str) -> None:
    assert all(_module().select(["docs/operator.md"], event).values())


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
@pytest.mark.parametrize(
    "path",
    [
        "control/src/vonk_control/runtime_assets.py",
        "agent_protocol/src/vonk_agent_protocol/launch.py",
        "src/cluster_profiles/compiler.py",
        "deploy/compose/tests/test_startup.py",
        "tests/conftest.py",
        "control/tests/conftest.py",
        "uv.lock",
        "packaging/systemd/vonk-forge-agent.service",
        "rust/deleted.rs",
        "scripts/select-ci-areas",
        ".github/workflows/installer-publication.yml",
        "new-runtime/input.conf",
        "inventory/wheels/protocol.whl",
    ],
)
def test_transitive_or_unknown_inputs_run_all_integrations(
    path: str, event: str
) -> None:
    # Deleted files are names from the diff too; no filesystem or Git lookup.
    assert all(_module().select([path], event).values())


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
@pytest.mark.parametrize(
    "path",
    [
        "control/Dockerfile",
        "control/src/vonk_control/runtime_init.py",
        "control/src/vonk_control/runtime_asset_contract.py",
        "control/src/vonk_control/api_preexec.py",
        "scripts/render-dev-compose",
    ],
)
def test_runtime_asset_producers_select_their_compose_consumers(
    path: str, event: str
) -> None:
    # Wrong implementation: a producer-only PR passes without ever exercising
    # the shell consumers or the real PostgreSQL restart recovery boundary.
    assert _module().select([path], event)["compose"] is True


def test_packaged_openapi_change_selects_generated_gate() -> None:
    selected = _module().select(
        ["src/cluster_profiles/schemas/control-openapi.json"], "pull_request"
    )
    assert selected["generated"] is True


def test_shared_ci_authority_selects_every_family() -> None:
    assert all(_module().select([".github/workflows/ci.yml"], "pull_request").values())


def test_non_pr_execution_is_conservative() -> None:
    assert all(_module().select(["docs/operator.md"], "push").values())


def test_unknown_product_input_runs_general_repository_suite() -> None:
    selected = _module().select(["install/channel"], "pull_request")
    assert selected["repository"] is True


def test_deleted_rust_file_selects_rust_family() -> None:
    # The caller supplies deleted paths alongside additions and modifications.
    # Wrong implementation: filtering by current file existence skips deletions.
    assert _module().select(["rust/deleted.rs"], "pull_request")["rust"] is True


@pytest.mark.parametrize(
    "path",
    [
        "tests/acceptance/recipe-library-revision.txt",
        "scripts/tests/run_agent_wire_contracts.py",
        "scripts/export-agent-wire-schema",
        "scripts/export-installer-release-schema",
        "scripts/generate-agent-wire",
        "rust/crates/vonk-agent/examples/consumer_corpus_probe.rs",
        "rust/crates/vonk-agent-protocol/src/integer.rs",
        "control/tests/cross_language_consumer_corpus.py",
        "control/tests/test_consumer_corpus_boundaries.py",
        "Cargo.toml",
        "Cargo.lock",
        "control/src/vonk_control/harnesses/canonical_metadata.py",
        "src/cluster_profiles/compiler.py",
        "inventory/wheels/vonk_agent_protocol-4.2.0-py3-none-any.whl",
    ],
)
def test_launch_contract_inputs_select_controller_and_wire_checks(path: str) -> None:
    assert _module().select([path], "pull_request")["control"] is True


@pytest.mark.parametrize(
    "path",
    [
        "control/src/vonk_control/runtime_init.py",
        "deploy/compose/compose.yaml",
        "install/nas",
        "nas_setup/src/vonk_nas_setup/cli.py",
        "scripts/build-nas-compose-bundle",
        "tests/acceptance/runtime.py",
    ],
)
def test_installer_compose_control_and_acceptance_select_the_nas_install(
    path: str,
) -> None:
    assert _module().select([path], "pull_request")["nas_install"] is True


@pytest.mark.parametrize(
    "path",
    [
        "rust/crates/vonk-agent/src/main.rs",
        "packaging/systemd/vonk-forge-agent.service",
        "scripts/test-agent-package-native-lifecycle",
    ],
)
def test_agent_and_packaging_select_the_native_package_lifecycle(path: str) -> None:
    assert _module().select([path], "pull_request")["agent_package"] is True


@pytest.mark.parametrize(
    "path",
    [
        "tests/acceptance/spark_upgrade_carry.py",
        "tests/acceptance/test_spark_lifecycle.py",
        "tests/acceptance/recipe-library-revision.txt",
        ".github/workflows/spark-upgrade-acceptance.yml",
        ".github/actions/prepare-control-wheel/action.yml",
        "scripts/render-accepted-compose-overlay",
    ],
)
def test_lane_code_needs_a_proof_run_on_the_pull_request(path: str) -> None:
    module = _module()
    assert module.lane_selected([path], "pull_request") is True
    # Main executes acceptance itself; merge queues accept their combined source.
    assert module.lane_selected([path], "push") is False
    assert module.lane_selected([path], "merge_group") is True


def test_product_code_and_workflows_need_candidate_acceptance() -> None:
    assert _module().lane_selected(
        [
            "control/src/vonk_control/api.py",
            "docs/operator.md",
            ".github/workflows/ci.yml",
        ],
        "pull_request",
    )


@pytest.mark.parametrize(
    "path",
    [
        "tools/python-model-registry.json",
        "rust/crates/vonk-agent/src/executor/mod.rs",
    ],
)
def test_contract_guard_inputs_select_the_controller_suite(path: str) -> None:
    assert _module().select([path], "pull_request")["control"] is True


def test_merge_queue_commit_selects_from_its_diff_like_a_pull_request() -> None:
    module = _module()
    assert module.select(["scripts/select-ci-areas"], "merge_group") == module.select(
        ["scripts/select-ci-areas"], "pull_request"
    )
    assert module.select([], "merge_group") == {area: True for area in module.AREAS}
    assert module.lane_selected(["tests/acceptance/x.py"], "merge_group") is True


@pytest.mark.parametrize(
    "paths",
    [
        ["tests/scripts/test_select_ci_areas.py"],
        ["tests/control/test_openapi_clients.py", "docs/operator.md"],
    ],
)
def test_test_only_changes_skip_candidate_acceptance(paths: list[str]) -> None:
    # Catches lane runs for PRs that cannot change the shipped candidate.
    assert _module().lane_selected(paths, "pull_request") is False


@pytest.mark.parametrize(
    "path",
    ["tests/acceptance/test_spark_lifecycle.py", "tests/fixtures/x.json", "weird/new"],
)
def test_acceptance_inputs_and_unknown_paths_keep_candidate_acceptance(
    path: str,
) -> None:
    assert _module().lane_selected(["tests/control/x.py", path], "pull_request")


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
@pytest.mark.parametrize(
    "paths",
    [
        ["control/tests/test_catalog_dry_run.py", "control/tests/catalog_dry_run.py"],
        ["control/tests/test_run_switch_postgres.py"],
        ["tests/scripts/test_select_ci_areas.py"],
        ["tests/cluster_profiles/test_compiler.py", "docs/operator.md"],
    ],
)
def test_ordinary_test_changes_run_core_suites_without_unrelated_integrations(
    paths: list[str], event: str
) -> None:
    # Wrong implementation: a new offline regression rebuilds and installs the
    # unchanged product, instead of running the regression in its owning suite.
    module = _module()
    selected = module.select(paths, event)
    assert all(selected[area] for area in module.FAST_AREAS)
    assert not any(
        selected[area] for area in ("compose", "nas_install", "agent_package")
    )
    assert module.lane_selected(paths, event) is False


@pytest.mark.parametrize(
    "path",
    [
        "tests/conftest.py",
        "control/tests/conftest.py",
        "control/tests/security/conftest.py",
        "tests/cluster_profiles/conftest.py",
        "tests/helpers.py",
        "control/tests/catalog_launch_fixtures.py",
        "control/tests/fixtures/compiled_workload_v2.json",
        "tests/fixtures/canonical-synthetic-canary/index.json",
        "tests/acceptance/test_spark_lifecycle.py",
        "tests/nodes/test_helper_runtime_filesystem.py",
        "deploy/compose/tests/test_litellm_supervisor.py",
        "tests/unfamiliar/test_new.py",
    ],
)
def test_shared_and_acceptance_test_inputs_keep_their_integrations(path: str) -> None:
    # Wrong implementation: exempting the tests/ trees also exempts fixtures
    # and programs invoked by installer, Compose and native package consumers.
    module = _module()
    assert all(module.select([path], "pull_request").values())
    assert module.lane_selected([path], "pull_request") is True


def test_product_change_alongside_ordinary_tests_still_accepts_candidate() -> None:
    paths = [
        "control/tests/test_catalog_dry_run.py",
        "control/src/vonk_control/catalog.py",
    ]
    module = _module()
    assert all(module.select(paths, "pull_request").values())
    assert module.lane_selected(paths, "pull_request") is True


@pytest.mark.parametrize(
    "path",
    [
        "control/tests/security/test_agent_protocol.py",
        "control/tests/security/test_no_routine_ssh.py",
    ],
)
def test_changed_image_consumers_keep_the_image_build(path: str) -> None:
    # Wrong implementation: ordinary-test exemptions deselect the only job
    # that executes tests marked built_image.
    module = _module()
    selected = module.select([path], "pull_request")
    assert selected["control"] and selected["nas_install"]


@pytest.mark.parametrize(
    "source",
    [
        "pytestmark = pytest.mark.built_image\n",
        "pytestmark = pytest.mark('built_image')\n",
        "not valid Python!\n",
    ],
)
def test_new_image_consumer_is_not_hidden_by_test_only_exemption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    # Covers newly introduced consumers, rather than a closed file allowlist.
    module = _module()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    test = tmp_path / "control/tests/test_new_image_consumer.py"
    test.parent.mkdir(parents=True)
    test.write_text(source, encoding="utf-8")
    assert module.select([test.relative_to(tmp_path).as_posix()], "pull_request")[
        "nas_install"
    ]


def test_unavailable_control_test_source_keeps_integrations() -> None:
    # A deleted or unreadable consumer cannot prove it needs no image checks.
    module = _module()
    assert all(
        module.select(
            ["control/tests/test_missing_consumer.py"], "pull_request"
        ).values()
    )


def test_changed_nas_module_selects_its_real_python_consumers() -> None:
    """A changed standalone package must not escape the local --changed test loop."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/select-changed-tests")],
        input="nas_setup/src/vonk_nas_setup/pki.py\n",
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "repository tests/nas_setup/test_pki.py" in result.stdout.splitlines()


@pytest.mark.parametrize(
    "path",
    [
        "rust/crates/vonk-agent/src/main.rs",
        "packaging/debian/control",
        "install/spark/install.sh",
        "deploy/compose/compose.yaml",
        "agent_protocol/src/vonk_agent_protocol/__init__.py",
        "scripts/install-release-publication",
        ".github/actions/python-env/action.yml",
        ".github/workflows/installer-candidate.yml",
        ".github/workflows/spark-upgrade-acceptance.yml",
    ],
)
def test_upgrade_inputs_carry_a_workload_on_the_pull_request(path: str) -> None:
    module = _module()
    assert module.carry_selected([path], "pull_request") is True
    assert module.carry_selected([path], "merge_group") is True


@pytest.mark.parametrize(
    "paths",
    [
        ["control/src/vonk_control/api.py"],
        ["control/web/src/app.tsx"],
        ["control/tests/test_run_switch_postgres.py", "tests/scripts/test_x.py"],
        ["tools/pytest_budget.py", ".github/workflows/ci.yml"],
        ["docs/testing-and-ci.md", "README.md"],
    ],
)
def test_changes_that_cannot_alter_the_upgrade_leave_the_carry_to_main(
    paths: list[str],
) -> None:
    """Catches a carry that runs on every pull request again, or one keyed on
    the lane instead of on what the upgrade is made of."""
    assert _module().carry_selected(paths, "pull_request") is False


def test_main_and_unknown_inputs_always_carry() -> None:
    module = _module()
    assert module.carry_selected(["control/src/x.py"], "push") is True
    assert module.carry_selected([], "pull_request") is True
    assert module.carry_selected(["weird/new"], "pull_request") is True


def test_selector_names_the_carry_for_the_lane_proof() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/select-ci-areas"),
            "--event",
            "pull_request",
            "--github-output",
        ],
        input="control/src/vonk_control/api.py\n",
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert "lane=true" in lines
    assert "upgrade_carry=false" in lines
