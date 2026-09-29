from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
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


def test_docs_only_change_skips_product_families() -> None:
    assert not any(_module().select(["docs/operator.md"], "pull_request").values())


def test_frontend_change_selects_web_and_its_control_owner() -> None:
    selected = _module().select(
        ["control/web/src/components/Fleet.tsx"], "pull_request"
    )
    assert selected == {
        "rust": False,
        "repository": False,
        "control": True,
        "web": True,
        "compose": False,
        "generated": False,
        "nas_install": True,
        "agent_package": False,
    }


def test_control_contract_change_selects_backend_and_generation() -> None:
    selected = _module().select(["control/src/vonk_control/models.py"], "pull_request")
    assert selected["control"] is True
    assert selected["generated"] is True
    assert selected["web"] is False


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


def test_deleted_rust_file_selects_rust_family(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    deleted = tmp_path / "rust" / "deleted.rs"
    deleted.parent.mkdir()
    deleted.write_text("fn main() {}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "rust/deleted.rs"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            "initial",
        ],
        check=True,
    )
    deleted.unlink()
    diff = subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "diff",
            "--name-only",
            "--diff-filter=ACMRD",
            "HEAD",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    selected = _module().select(diff, "pull_request")
    assert selected["rust"] is True


@pytest.mark.parametrize(
    "path",
    [
        "tests/acceptance/recipe-library-revision.txt",
        "scripts/tests/run_agent_wire_contracts.py",
        "scripts/export-agent-wire-schema",
        "scripts/export-installer-release-schema",
        "scripts/generate-agent-wire",
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


def test_unrelated_change_selects_neither_installed_system_proof() -> None:
    selected = _module().select(["scripts/select-ci-areas"], "pull_request")
    assert selected["nas_install"] is False
    assert selected["agent_package"] is False
