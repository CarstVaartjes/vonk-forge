from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/installer-publication.yml"
CI = ROOT / ".github/workflows/ci.yml"
DEV_IMAGES = ROOT / ".github/workflows/dev-images.yml"


def _workflow() -> dict[str, object]:
    return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)


def test_publication_runs_only_after_accepted_workflows() -> None:
    workflow = _workflow()
    triggers = workflow["on"]["workflow_run"]
    assert set(triggers["workflows"]) == {
        "CI",
        "Development images",
        "Rust Vonk Forge agent development",
    }
    assert triggers["types"] == ["completed"]
    authority = workflow["jobs"]["authority"]
    assert "conclusion == 'success'" in authority["if"]
    publish = workflow["jobs"]["publish"]
    assert set(publish["needs"]) == {"authority", "build-setup"}
    assert "needs.authority.result == 'success'" in publish["if"]
    text = WORKFLOW.read_text()
    assert "refs/remotes/origin/main" in text
    assert "actions/workflows/agent-release.yml/runs" in text
    assert "actions/workflows/dev-images.yml/runs" in text
    assert "verify-release-tag-authority" in text
    assert 'status:"accepted"' in text


def test_setup_build_matrix_is_complete_and_native() -> None:
    workflow = _workflow()
    matrix = workflow["jobs"]["build-setup"]["strategy"]["matrix"]["include"]
    actual = {
        (entry["platform"], entry["runner"], entry["binaries"]) for entry in matrix
    }
    assert actual == {
        ("linux-amd64", "ubuntu-24.04", "vonk-nas-setup vonk-spark-setup"),
        ("linux-arm64", "ubuntu-24.04-arm", "vonk-nas-setup vonk-spark-setup"),
        ("darwin-amd64", "macos-15-intel", "vonk-nas-setup"),
        ("darwin-arm64", "macos-15", "vonk-nas-setup"),
    }
    assert all("target" not in entry for entry in matrix)


def test_publication_reuses_exact_package_and_compose_artifacts() -> None:
    text = WORKFLOW.read_text()
    assert "actions/download-artifact@" in text
    assert "run-id: ${{ needs.authority.outputs.package_run_id }}" in text
    assert "run-id: ${{ needs.authority.outputs.compose_run_id }}" in text
    assert "scripts/build-nas-compose-bundle" in text
    assert "--compose" in text
    assert "scripts/install-release-publication assemble" in text
    assert "scripts/install-release-publication publish" in text


def test_r2_publication_uses_explicit_least_privilege_inputs() -> None:
    text = WORKFLOW.read_text()
    for name in (
        "R2_ACCESS_KEY_ID",
        "R2_ACCOUNT_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_INSTALLER_PUBLIC_BUCKET",
        "INSTALLER_PUBLIC_ORIGIN",
    ):
        assert name in text
    assert "environment: installer-${{ needs.authority.outputs.channel }}" in text
    assert "permissions:\n      actions: read\n      contents: read" in text


def test_release_compose_renderer_never_receives_latest() -> None:
    text = CI.read_text()
    render_blocks = re.findall(
        r"scripts/render-production-compose \\\n(?:(?:\s+.*\n){1,8})", text
    )
    assert render_blocks
    assert all(":latest" not in block for block in render_blocks)
    assert "image_version_tag" in "\n".join(render_blocks)


def test_development_acceptance_publishes_exact_hermes_generation() -> None:
    text = DEV_IMAGES.read_text()
    assert "vonk-forge-hermes.oci.tar" in text
    assert "--target managed" in text
    assert (
        'ghcr.io/carstvaartjes/vonk-forge-hermes:dev-sha-$GITHUB_SHA@$hermes_digest'
        in text
    )
    assert "subject-name: ghcr.io/carstvaartjes/vonk-forge-hermes" in text
    assert "subject-digest: ${{ steps.publish.outputs.hermes_digest }}" in text
