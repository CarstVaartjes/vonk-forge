from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/agent-repair-recovery.yml"


def test_repair_workflow_runs_full_native_arm64_matrix_without_secrets() -> None:
    text = WORKFLOW.read_text()

    assert "pull_request:" in text
    assert "push:" in text
    assert "workflow_dispatch:" in text
    assert "runs-on: ubuntu-24.04-arm" in text
    assert "timeout-minutes: 180" in text
    assert 'test \"$(uname -m)\" = aarch64' in text
    assert 'test \"$(dpkg --print-architecture)\" = arm64' in text
    assert "REPAIR_MATRIX_MODE: full" in text
    assert "cargo build --locked --release --package vonk-repair-helper-probe" in text
    assert "REPAIR_PROBE_BINARY" in text
    assert 'sudo install -o root -g root -m 0755 "$probe" "$probe_staged"' in text
    assert 'sudo mv -f "$probe_staged" "$probe"' in text
    assert "podman shellcheck" in text
    assert "slirp4netns systemd uidmap util-linux" in text
    assert "shellcheck -S error" in text
    assert "tests/nodes/test_agent_upgrade_repair_matrix.sh" in text
    assert "persist-credentials: false" in text
    assert "contents: read" in text
    for forbidden in (
        "environment:",
        "id-token: write",
        "secrets.",
        "VONK_AGENT_RELEASE_PRIVATE_KEY",
        "APT_REPOSITORY_GPG_PRIVATE_KEY",
    ):
        assert forbidden not in text


def test_repair_workflow_is_scoped_to_recovery_inputs() -> None:
    text = WORKFLOW.read_text()

    for required_path in (
        "packaging/debian/postinst-repair",
        "packaging/debian/preinst-repair",
        "rust/crates/vonk-repair-helper-probe/**",
        "scripts/build-agent-deb",
        "scripts/verify-agent-deb",
        "tests/nodes/test_agent_upgrade_repair_matrix.sh",
        "tests/nodes/test_agent_upgrade_repair_systemd.sh",
    ):
        assert text.count(f"- {required_path}") == 2
