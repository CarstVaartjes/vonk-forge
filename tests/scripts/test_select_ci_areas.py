from __future__ import annotations

import importlib.machinery
import importlib.util
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
        "inventory/wheels/vonk_agent_protocol-4.1.0-py3-none-any.whl",
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
        "rust/crates/vonk-nas-setup/src/lib.rs",
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
