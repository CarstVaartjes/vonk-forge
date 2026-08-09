from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/agent-release.yml"
UNIFIED_WORKFLOW = ROOT / ".github/workflows/ci.yml"


def test_agent_release_is_native_arm64_reproducible_and_attested() -> None:
    text = WORKFLOW.read_text()
    assert "runs-on: ubuntu-24.04-arm" in text
    assert "environment: agent-release" in text
    assert "cargo build" not in text
    assert text.count("scripts/build-agent-deb") >= 3
    assert "cmp --silent" in text
    assert "scripts/verify-agent-deb" in text
    assert "actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6" in text
    assert "sigstore/cosign-installer@6f9f17788090df1f26f669e9d70d6ae9567deba6" in text
    assert "cosign sign-blob --yes --bundle" in text


def test_agent_release_exercises_offline_debian_lifecycle() -> None:
    text = WORKFLOW.read_text()
    for expected in (
        "dpkg -i",
        "dpkg --remove vonk-forge-agent",
        "refused downgrade",
        "/etc/vonk-forge-agent/agent.toml",
        "/var/lib/vonk-forge-agent",
        "SYSTEMD_OFFLINE=1",
    ):
        assert expected in text
    assert "curl " not in text
    assert "wget " not in text


def test_apt_publication_is_isolated_and_uses_persistent_private_state() -> None:
    text = UNIFIED_WORKFLOW.read_text()
    assert "environment: apt-release" in text
    assert "group: vonk-forge-apt-publication" in text
    assert "APT_REPOSITORY_GPG_PRIVATE_KEY" in text
    assert "R2_APT_STATE_BUCKET" in text
    assert "R2_APT_PUBLIC_BUCKET" in text
    assert "publish switch" in text
    assert "rclone copyto" in text
    assert "aptly-state.tar.gz" in text
    assert "packages.vonkforge.ai" in text
    assert "APT_REPOSITORY_GPG_FINGERPRINT" in text
    assert "vonk-forge-archive-keyring.gpg" in text
    assert (
        "secrets.VONK_AGENT_RELEASE_PRIVATE_KEY"
        not in text.split("  publish-apt:", 1)[1]
    )


def test_release_actions_are_commit_pinned_and_secrets_are_environment_scoped() -> None:
    agent_text = WORKFLOW.read_text()
    unified_text = UNIFIED_WORKFLOW.read_text()
    assert "uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in agent_text
    assert (
        "uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
        in agent_text
    )
    assert (
        "uses: actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"
        in unified_text
    )
    for line in agent_text.splitlines() + unified_text.splitlines():
        if "uses:" in line:
            assert "@v" not in line
    assert "permissions:\n  contents: read" in agent_text
    assert "contents: write" in unified_text
    assert "id-token: write" in agent_text


def test_manual_agent_validation_does_not_own_apt_publication() -> None:
    text = WORKFLOW.read_text()
    assert "publish-apt" not in text
    assert "apt-release" not in text
    assert "contents: write" not in text


def test_release_operations_are_documented() -> None:
    text = (ROOT / "docs/operations/agent-package-release.md").read_text()
    for expected in (
        "agent-release",
        "apt-release",
        "packages.vonkforge.ai",
        "VONK_AGENT_RELEASE_PRIVATE_KEY",
        "APT_REPOSITORY_GPG_PRIVATE_KEY",
        "R2_APT_STATE_BUCKET",
        "cosign verify-blob",
        "apt install vonk-forge-agent",
        "offline",
        "key rotation",
    ):
        assert expected in text
