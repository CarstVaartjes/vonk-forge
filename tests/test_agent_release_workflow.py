import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/agent-release.yml"
UNIFIED_WORKFLOW = ROOT / ".github/workflows/ci.yml"
PACKAGE_WORKFLOW = ROOT / ".github/workflows/agent-package-build.yml"


def test_reusable_agent_package_build_has_a_strict_call_boundary() -> None:
    text = PACKAGE_WORKFLOW.read_text()

    assert "workflow_call:" in text
    for input_name in (
        "channel",
        "version",
        "next_version",
        "package",
        "artifact_name",
        "environment",
    ):
        assert f"      {input_name}:\n        required: true\n        type: string" in text
    for output_name in ("version", "package", "artifact_name"):
        assert f"      {output_name}:" in text
    assert "runs-on: ubuntu-24.04-arm" in text
    assert "environment: ${{ inputs.environment }}" in text
    assert "timeout-minutes: 45" in text


def test_reusable_agent_package_build_validates_authority_before_key_use() -> None:
    text = PACKAGE_WORKFLOW.read_text()

    validation = text.index("Validate package metadata and environment")
    key = text.index("Materialize and verify protected agent key")
    build = text.index("Build package twice reproducibly")
    assert validation < key < build
    assert "dev:agent-development" in text
    assert "stable:agent-release" in text
    assert "scripts/agent-package-metadata" in text
    assert "VONK_AGENT_RELEASE_PRIVATE_KEY" in text
    assert "VONK_AGENT_RELEASE_KEY_FINGERPRINT" in text
    assert "openssl pkey" in text
    assert "-pubout -outform DER" in text
    assert "sha256sum" in text


def test_reusable_agent_package_build_preserves_acceptance_gates() -> None:
    text = PACKAGE_WORKFLOW.read_text()

    assert "cargo build" not in text
    assert text.count("scripts/build-agent-deb") >= 3
    assert "NEXT_VERSION: ${{ inputs.next_version }}" in text
    assert 'IFS=. read -r major minor patch <<< "$VERSION"' not in text
    assert "cmp --silent" in text
    assert "scripts/verify-agent-deb" in text
    assert "cargo fmt --all --check" in text
    assert "cargo clippy --workspace --all-targets --locked -- -D warnings" in text
    assert "cargo test --workspace --locked" in text
    assert "scripts/verify-agent-systemd" in text
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
    assert "actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6" in text
    assert "sigstore/cosign-installer@6f9f17788090df1f26f669e9d70d6ae9567deba6" in text
    assert "cosign sign-blob --yes --bundle" in text
    assert "agent-package-build\\.yml@refs/heads/main" in text
    assert "agent-package-build\\.yml@refs/tags/v" in text


def test_stable_sigstore_identity_renders_the_exact_version() -> None:
    line = next(
        line.strip()
        for line in PACKAGE_WORKFLOW.read_text().splitlines()
        if line.strip().startswith('identity="') and "refs/tags" in line
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            f"VERSION=1.2.3; GITHUB_REPOSITORY=example/vonk; {line}; "
            "printf '%s\\n' \"$identity\"",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout == (
        "^https://github\\.com/example/vonk/\\.github/workflows/"
        "agent-package-build\\.yml@refs/tags/v1\\.2\\.3$\n"
    )


def test_reusable_agent_package_build_uploads_one_immutable_release_set() -> None:
    text = PACKAGE_WORKFLOW.read_text()

    assert text.count("actions/upload-artifact@") == 1
    assert "name: ${{ inputs.artifact_name }}" in text
    assert "overwrite: false" in text
    assert "if-no-files-found: error" in text
    assert "path: dist/*" in text


def test_manual_agent_workflow_calls_the_development_package_builder() -> None:
    text = WORKFLOW.read_text()

    assert "uses: ./.github/workflows/agent-package-build.yml" in text
    assert "channel: ${{ needs.package-metadata.outputs.channel }}" in text
    assert "environment: agent-development" in text
    assert "artifact-metadata: write" in text
    assert "attestations: write" in text
    assert "id-token: write" in text
    assert "secrets:" not in text
    for forbidden in (
        "scripts/build-agent-deb",
        "VONK_AGENT_RELEASE_PRIVATE_KEY",
        "dpkg -i",
        "cosign sign-blob",
    ):
        assert forbidden not in text


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
    package_text = PACKAGE_WORKFLOW.read_text()
    assert "uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in package_text
    assert (
        "uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
        in package_text
    )
    assert (
        "uses: actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"
        in unified_text
    )
    for line in (
        agent_text.splitlines()
        + unified_text.splitlines()
        + package_text.splitlines()
    ):
        if "uses:" in line:
            assert "@v" not in line
    assert "permissions:\n  contents: read" in agent_text
    assert "contents: write" in unified_text
    assert "id-token: write" in package_text


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
