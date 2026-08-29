"""Release-gate map for the recipe-native GPU node agent."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_production_rust_capabilities_are_exact_and_python_agent_is_not_packaged() -> None:
    main = _source("rust/crates/vonk-agent/src/main.rs")
    for capability in (
        "agent.runtime.rust.v1",
        "runtime.vonk.v1",
        "recipe.install",
        "recipe.start",
        "recipe.stop",
        "recipe.uninstall",
    ):
        assert f'"{capability}"' in main
    assert '"package.prepare"' not in main

    package_builder = _source("scripts/build-agent-deb")
    assert 'BINARIES = ("vonk-agent", "vonk-agent-helper", "oras")' in package_builder
    assert "vonk_agent" not in package_builder


def test_debian_package_is_the_only_agent_installer_authority() -> None:
    package_builder = _source("scripts/build-agent-deb")
    for binary in ("vonk-agent", "vonk-agent-helper", "oras"):
        assert binary in package_builder
    for unit in (
        "vonk-forge-agent.service",
        "vonk-forge-docker-firewall.service",
        "vonk-forge-package-helper.service",
        "vonk-forge-package-helper.socket",
    ):
        assert (ROOT / "packaging/systemd" / unit).is_file()

    process = _source("rust/crates/vonk-agent/src/process.rs")
    assert 'Self::Oras => "/usr/lib/vonk-forge/oras"' in process


def test_release_workflow_runs_every_agent_owner_before_publication() -> None:
    orchestrator = _source(".github/workflows/agent-release.yml")
    package_builder = _source(".github/actions/agent-package-build/action.yml")
    package_security = _source(".github/actions/agent-package-security/action.yml")

    assert "uses: ./.github/actions/agent-package-build" in orchestrator
    assert "needs: [package-metadata, build-test-sign]" in orchestrator
    assert "uses: ./.github/actions/agent-apt-publish" in orchestrator
    assert "uses: ./.github/actions/agent-package-security" in package_builder
    assert "uses: ./.github/actions/agent-package-security" in orchestrator
    assert "cargo test --workspace --locked" in package_security
    assert "tests/acceptance/test_rust_agent_parity.py" in package_security
    assert "test_agent_deb.py" in package_security
