from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/agent-upgrade-recovery.yml"


def test_upgrade_recovery_workflow_runs_native_arm64_without_secrets() -> None:
    text = WORKFLOW.read_text()

    assert "pull_request:" in text
    assert "push:" in text
    assert "workflow_dispatch:" in text
    assert "runs-on: ubuntu-24.04-arm" in text
    assert "timeout-minutes: 30" in text
    assert 'test "$(uname -m)" = aarch64' in text
    assert 'test "$(dpkg --print-architecture)" = arm64' in text
    assert "acl adduser build-essential" in text
    assert "fuse-overlayfs iproute2 iptables openssl podman shellcheck" in text
    assert "slirp4netns systemd uidmap util-linux" in text
    assert "shellcheck tests/nodes/test_agent_upgrade_recovery_systemd.sh" in text
    assert "cargo build --locked --release --package vonk-build-egress" in text
    assert "BUILD_EGRESS_BINARY" in text
    assert "VERSION=0.1.0~dev.381+ga122909feaa3" in text
    assert "STALE_PENDING_FORMAT=legacy2" in text
    assert "CRASH_MODE=full-cgroup" in text
    assert "CRASH_MODE=post-remove" in text
    assert "STALE_PENDING_FORMAT=prior3" in text
    assert "CANDIDATE_CUSTODY=root" in text
    assert "CANDIDATE_CUSTODY=legacy" in text
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


def test_upgrade_recovery_workflow_is_scoped_to_recovery_inputs() -> None:
    text = WORKFLOW.read_text()

    for required_path in (
        "control/src/vonk_control/agent_jobs.py",
        "control/src/vonk_control/compat_recovery.py",
        "control/src/vonk_control/host_helper_authority.py",
        "rust/crates/vonk-agent-helper/**",
        "rust/crates/vonk-agent-protocol/**",
        "rust/crates/vonk-build-egress/**",
        "scripts/build-agent-deb",
        "tests/nodes/test_agent_upgrade_recovery_systemd.sh",
    ):
        assert text.count(f"- {required_path}") == 2
