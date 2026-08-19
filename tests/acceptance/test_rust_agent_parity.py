"""Release-gate map for the recipe-native GPU node agent."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_agent_outcome_matrix_has_executable_owners() -> None:
    coverage = {
        "enrollment": (
            "rust/crates/vonk-agent/tests/pairing.rs",
            "pending_identity_is_reused_for_approval_pickup",
        ),
        "inventory": (
            "rust/crates/vonk-agent/tests/inventory.rs",
            "inventory_reports_physical_and_available_memory_disk_and_gpu",
        ),
        "duplicate-receipt": (
            "rust/crates/vonk-agent/tests/restart_receipts.rs",
            "completed_result_is_redelivered_until_acknowledged",
        ),
        "install-start-stop": (
            "rust/crates/vonk-agent/tests/workloads.rs",
            "container_arguments_are_typed_and_hardened",
        ),
        "multi-node-abort": (
            "control/tests/test_recipe_operations.py",
            "test_install_is_digest_bound_idempotent_and_gang_complete",
        ),
        "update-rollback": (
            "rust/crates/vonk-agent-supervisor/tests/rollback.rs",
            "readiness_timeout_rolls_back_to_freshly_verified_previous_slot",
        ),
        "audit": (
            "control/tests/test_agent_api.py",
            "test_human_enrollment_mutations_audit_only_success",
        ),
    }
    for outcome, (path, test_name) in coverage.items():
        assert test_name in _source(path), f"{outcome} lost its executable owner"


def test_production_rust_capabilities_are_exact_and_python_agent_is_not_packaged() -> None:
    main = _source("rust/crates/vonk-agent/src/main.rs")
    for capability in (
        "agent.runtime.rust.v1",
        "recipe.install",
        "recipe.start",
        "recipe.stop",
        "recipe.uninstall",
    ):
        assert f'"{capability}"' in main
    for retired in ("agent.update", "agent.rollback", "package.prepare"):
        assert f'"{retired}"' not in main

    package_builder = _source("scripts/build-agent-deb")
    assert 'BINARIES = ("vonk-agent", "vonk-agent-helper", "vonk-agent-supervisor")' in package_builder
    assert "vonk_agent" not in package_builder


def test_debian_package_is_the_only_agent_installer_authority() -> None:
    package_builder = _source("scripts/build-agent-deb")
    for binary in ("vonk-agent", "vonk-agent-helper", "vonk-agent-supervisor"):
        assert binary in package_builder
    for unit in (
        "vonk-forge-agent.service",
        "vonk-forge-agent-supervisor.service",
        "vonk-forge-docker-firewall.service",
        "vonk-forge-package-helper.service",
        "vonk-forge-package-helper.socket",
    ):
        assert (ROOT / "packaging/systemd" / unit).is_file()


def test_release_workflow_runs_every_agent_owner_before_publication() -> None:
    orchestrator = _source(".github/workflows/agent-release.yml")
    package_builder = _source(".github/actions/agent-package-build/action.yml")

    assert "uses: ./.github/actions/agent-package-build" in orchestrator
    assert "needs: [package-metadata, build-test-sign]" in orchestrator
    assert "uses: ./.github/actions/agent-apt-publish" in orchestrator
    assert "cargo test --workspace --locked" in package_builder
    assert "tests/acceptance/test_rust_agent_parity.py" in package_builder
    assert "test_agent_deb.py" in package_builder
