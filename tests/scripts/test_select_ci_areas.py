from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
from pathlib import Path

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
    }


def test_control_contract_change_selects_backend_and_generation() -> None:
    selected = _module().select(
        ["control/src/vonk_control/models.py"], "pull_request"
    )
    assert selected["control"] is True
    assert selected["generated"] is True
    assert selected["web"] is False


def test_shared_ci_authority_selects_every_family() -> None:
    assert all(
        _module().select([".github/workflows/ci.yml"], "pull_request").values()
    )


def test_non_pr_execution_is_conservative() -> None:
    assert all(_module().select(["docs/operator.md"], "push").values())


def test_unknown_product_input_runs_general_repository_suite() -> None:
    selected = _module().select(["install/channel"], "pull_request")
    assert selected["repository"] is True


def test_generated_supply_chain_manifest_does_not_broaden_its_source_change() -> None:
    selected = _module().select(
        [
            ".github/workflows/installer-publication.yml",
            "inventory/sbom/manifest.json",
        ],
        "pull_request",
    )
    assert selected == {
        "rust": False,
        "repository": True,
        "control": False,
        "web": False,
        "compose": False,
        "generated": False,
    }


def test_deleted_rust_file_selects_rust_family(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    deleted = tmp_path / "rust" / "deleted.rs"
    deleted.parent.mkdir()
    deleted.write_text("fn main() {}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "rust/deleted.rs"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "initial"],
        check=True,
    )
    deleted.unlink()
    diff = subprocess.run(
        ["git", "-C", str(tmp_path), "diff", "--name-only", "--diff-filter=ACMRD", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    selected = _module().select(diff, "pull_request")
    assert selected["rust"] is True
