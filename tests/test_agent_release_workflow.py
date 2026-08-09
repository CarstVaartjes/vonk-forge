import hashlib
import json
import os
import re
import struct
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/agent-release.yml"
UNIFIED_WORKFLOW = ROOT / ".github/workflows/ci.yml"
PACKAGE_WORKFLOW = ROOT / ".github/workflows/agent-package-build.yml"
APT_WORKFLOW = ROOT / ".github/workflows/agent-apt-publish.yml"
APT_STATE = ROOT / "scripts/agent-apt-state"


def package_step_run(step_name: str) -> str:
    lines = PACKAGE_WORKFLOW.read_text().splitlines()
    step_start = lines.index(f"      - name: {step_name}")
    run_start = lines.index("        run: |", step_start) + 1
    run_lines: list[str] = []
    for line in lines[run_start:]:
        if line and not line.startswith("          "):
            break
        run_lines.append(line[10:] if line else "")
    return "\n".join(run_lines)


def apt_workflow() -> str:
    return APT_WORKFLOW.read_text()


def apt_step(step_name: str) -> str:
    lines = apt_workflow().splitlines()
    step_start = lines.index(f"      - name: {step_name}")
    step_lines: list[str] = []
    for line in lines[step_start:]:
        if line.startswith("      - name: ") and step_lines:
            break
        step_lines.append(line)
    return "\n".join(step_lines)


def generate_private_key(path: Path, algorithm: str) -> None:
    result = subprocess.run(
        ["/usr/bin/openssl", "genpkey", "-algorithm", algorithm, "-out", path],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    path.chmod(0o600)


def public_spki(private_key: Path) -> bytes:
    return subprocess.run(
        [
            "/usr/bin/openssl",
            "pkey",
            "-in",
            private_key,
            "-pubout",
            "-outform",
            "DER",
        ],
        check=True,
        capture_output=True,
    ).stdout


def run_key_authority(
    private_key: Path, expected_fingerprint: str, runner_temp: Path
) -> subprocess.CompletedProcess[str]:
    runner_temp.mkdir()
    return subprocess.run(
        ["bash", "-c", package_step_run("Materialize and verify protected agent key")],
        cwd=ROOT,
        env={
            **os.environ,
            "EXPECTED_FINGERPRINT": expected_fingerprint,
            "RELEASE_PRIVATE_KEY": private_key.read_text(),
            "RUNNER_TEMP": str(runner_temp),
        },
        check=False,
        capture_output=True,
        text=True,
    )


def signing_key_id(private_key: Path, tmp_path: Path) -> str:
    artifact = tmp_path / "vonk-agent"
    raw = bytearray(256)
    raw[:16] = b"\x7fELF\x02\x01\x01" + bytes(9)
    struct.pack_into("<H", raw, 18, 183)
    artifact.write_bytes(raw)
    result = subprocess.run(
        [
            ROOT / "scripts/sign-agent-release",
            "--artifact",
            artifact,
            "--private-key",
            private_key,
            "--output",
            tmp_path / "signed",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["key_id"]


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


def test_prebuild_authority_rejects_non_ed25519_with_matching_spki_hash(
    tmp_path: Path,
) -> None:
    private_key = tmp_path / "rsa.pem"
    generate_private_key(private_key, "RSA")
    old_workflow_fingerprint = hashlib.sha256(public_spki(private_key)).hexdigest()
    runner_temp = tmp_path / "rsa-runner"

    result = run_key_authority(private_key, old_workflow_fingerprint, runner_temp)

    assert result.returncode != 0
    assert {path.name for path in runner_temp.iterdir()} == {
        "vonk-agent-release.pem"
    }


def test_prebuild_authority_matches_signing_ed25519_key_id(tmp_path: Path) -> None:
    private_key = tmp_path / "ed25519.pem"
    generate_private_key(private_key, "ED25519")
    expected_key_id = signing_key_id(private_key, tmp_path)
    runner_temp = tmp_path / "ed25519-runner"

    result = run_key_authority(private_key, expected_key_id, runner_temp)

    assert result.returncode == 0, result.stderr
    assert {path.name for path in runner_temp.iterdir()} == {
        "vonk-agent-release.pem"
    }


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
            (
                f"VERSION=1.2.3; GITHUB_REPOSITORY=example/vonk; {line}; "
                "printf '%s\\n' \"$identity\""
            ),
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


def test_development_agent_workflow_runs_only_for_exact_main_sources() -> None:
    text = WORKFLOW.read_text()

    assert "  push:\n    branches: [main]" in text
    dispatch = text.split("  workflow_dispatch:", 1)[1].split("\n\npermissions:", 1)[0]
    assert "inputs:" not in dispatch
    assert "version:" not in dispatch
    assert text.count("fetch-depth: 0") == 1
    assert text.count("git fetch --no-tags --prune origin") == 1
    assert text.count('test "$GITHUB_REF" = "refs/heads/main"') == 1
    assert text.count('test "$GITHUB_SHA" = "$main_sha"') == 1
    assert text.index("Verify exact current main tip") < text.index(
        "Derive immutable development package metadata"
    )
    assert "verify-main-for-apt:" not in text


def test_development_agent_workflow_calls_both_reusable_channel_boundaries() -> None:
    text = WORKFLOW.read_text()

    assert "uses: ./.github/workflows/agent-package-build.yml" in text
    assert "channel: ${{ needs.package-metadata.outputs.channel }}" in text
    assert "environment: agent-development" in text
    assert "artifact-metadata: write" in text
    assert "attestations: write" in text
    assert "id-token: write" in text
    assert "uses: ./.github/workflows/agent-apt-publish.yml" in text
    assert "environment: apt-development" in text
    assert "source_sha: ${{ github.sha }}" in text
    assert "needs: [package-metadata, build-test-sign]" in text
    assert "artifact_name: ${{ needs.build-test-sign.outputs.artifact_name }}" in text
    assert "secrets:" not in text
    for forbidden in (
        "scripts/build-agent-deb",
        "VONK_AGENT_RELEASE_PRIVATE_KEY",
        "dpkg -i",
        "cosign sign-blob",
    ):
        assert forbidden not in text


def test_reusable_apt_publisher_has_a_strict_channel_boundary() -> None:
    text = apt_workflow()

    assert "workflow_call:" in text
    for input_name in (
        "channel",
        "version",
        "package",
        "artifact_name",
        "environment",
        "source_sha",
    ):
        assert f"      {input_name}:\n        required: true\n        type: string" in text
    for forbidden in ("repository:", "distribution:", "keyring:", "state_prefix:"):
        assert f"      {forbidden}" not in text.split("    inputs:", 1)[1].split(
            "permissions:", 1
        )[0]
    assert "dev:apt-development" in text
    assert "stable:apt-release" in text
    assert "environment: ${{ inputs.environment }}" in text
    assert "group: vonk-forge-agent-apt-${{ inputs.channel }}" in text
    assert "cancel-in-progress: false" in text


def test_reusable_apt_publisher_verifies_package_before_credentials() -> None:
    text = apt_workflow()
    verify = text.index("Verify exact downloaded package")
    authority = text.index("Reverify accepted development source authority")
    restore = text.index("Prepare committed or recoverable private state")
    key = text.index("Materialize and verify apt signing key")

    assert verify < authority < restore < key
    verify_step = apt_step("Verify exact downloaded package")
    assert "scripts/verify-agent-deb" in verify_step
    assert "dpkg-deb --field" in verify_step
    assert "resolvedDependencies" in verify_step
    for credential in (
        "APT_GPG_PASSPHRASE",
        "APT_REPOSITORY_GPG_PRIVATE_KEY",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
    ):
        assert credential not in verify_step


def test_reusable_apt_publisher_rechecks_dev_authority_inside_protected_job() -> None:
    text = apt_workflow()
    authority = apt_step("Reverify accepted development source authority")
    step_names = re.findall(r"^      - name: (.+)$", text, re.MULTILINE)
    authority_index = step_names.index(
        "Reverify accepted development source authority"
    )

    assert "environment: ${{ inputs.environment }}" in text
    assert "CALLER_SHA: ${{ github.sha }}" in authority
    assert "CHANNEL: ${{ inputs.channel }}" in authority
    assert "SOURCE_SHA: ${{ inputs.source_sha }}" in authority
    assert 'test "$SOURCE_SHA" = "$CALLER_SHA"' in authority
    assert 'test "$GITHUB_REF" = "refs/heads/main"' in authority
    assert "git fetch --no-tags --prune origin" in authority
    assert "+refs/heads/main:refs/remotes/origin/main" in authority
    assert 'test "$SOURCE_SHA" = "$main_sha"' in authority
    assert "dev)" in authority
    stable_case = authority.split("stable)", 1)[1].split("*)", 1)[0]
    assert "origin/main" not in stable_case
    assert step_names[authority_index + 1] == (
        "Prepare committed or recoverable private state"
    )
    for forbidden in (
        "APT_GPG_PASSPHRASE",
        "APT_REPOSITORY_GPG_PRIVATE_KEY",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
    ):
        assert forbidden not in authority


def test_reusable_apt_publisher_restores_only_authenticated_private_state() -> None:
    restore = apt_step("Prepare committed or recoverable private state")
    state = APT_STATE.read_text()

    assert "R2_APT_STATE_BUCKET" in restore
    assert "R2_APT_PUBLIC_BUCKET" not in restore
    assert "scripts/agent-apt-state prepare" in restore
    assert "tarfile.open" in state
    assert "unsafe archive member" in state
    assert "publication-receipt.json" in state
    assert '"/usr/bin/dpkg", "--compare-versions"' in state
    assert "commit.json" in state
    assert "latest.json" in state


def test_reusable_apt_publisher_preserves_immutable_receipts_and_ordering() -> None:
    text = apt_workflow()
    local = apt_step("Generate missing aptly state or public tree")
    commit = apt_step("Commit immutable publication manifest")
    publish = apt_step("Publish exact committed public tree and latest pointer")
    state = APT_STATE.read_text()

    assert "snapshot create" in local
    assert "publish switch" in local
    assert "gpgv" in local and "InRelease" in local
    assert "publication-receipt.json" in state
    assert "canonical_json" in state
    signing = apt_step("Materialize and verify apt signing key")
    assert "install -m 0600" in signing
    assert "sec_count" in signing
    assert "${#fingerprints[@]}" not in signing
    assert "scripts/agent-apt-state commit" in commit
    assert "scripts/agent-apt-state publish" in publish
    assert text.index("Commit immutable publication manifest") < text.index(
        "Publish exact committed public tree and latest pointer"
    )
    assert "packages.vonkforge.ai" in publish
    assert "VONK_AGENT_RELEASE_PRIVATE_KEY" not in text


def test_reusable_apt_publisher_uses_manifest_last_exact_replay_protocol() -> None:
    text = apt_workflow()
    prepare = apt_step("Prepare committed or recoverable private state")
    signing = apt_step("Materialize and verify apt signing key")
    local = apt_step("Generate missing aptly state or public tree")
    bundles = apt_step("Build any missing immutable bundles")
    commit = apt_step("Commit immutable publication manifest")
    publish = apt_step("Publish exact committed public tree and latest pointer")

    assert "scripts/agent-apt-state prepare" in prepare
    assert "tar -t" not in prepare
    assert "steps.state.outputs.needs_generation == 'true'" in signing
    assert "steps.state.outputs.needs_generation == 'true'" in local
    assert "scripts/agent-apt-state bundle" in bundles
    assert "scripts/agent-apt-state commit" in commit
    assert "R2_APT_PUBLIC_BUCKET" not in commit
    assert "scripts/agent-apt-state publish" in publish
    assert text.index("Commit immutable publication manifest") < text.index(
        "Publish exact committed public tree and latest pointer"
    )
    for removed in (
        "latest/aptly-state.tar.gz",
        "aptly-state.tar.gz.sha256",
        "rclone copyto --immutable",
    ):
        assert removed not in text


def test_release_actions_are_commit_pinned_and_secrets_are_environment_scoped() -> None:
    agent_text = WORKFLOW.read_text()
    unified_text = UNIFIED_WORKFLOW.read_text()
    package_text = PACKAGE_WORKFLOW.read_text()
    apt_text = APT_WORKFLOW.read_text()
    assert "uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in package_text
    assert (
        "uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
        in package_text
    )
    assert (
        "uses: actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"
        in apt_text
    )
    for line in (
        agent_text.splitlines()
        + unified_text.splitlines()
        + package_text.splitlines()
        + apt_text.splitlines()
    ):
        if "uses:" in line:
            assert "@v" not in line
    assert "permissions:\n  contents: read" in agent_text
    assert "contents: write" in unified_text
    assert "id-token: write" in package_text


def test_development_agent_workflow_has_no_production_authority() -> None:
    text = WORKFLOW.read_text()

    assert "publish-apt:" in text
    assert "apt-release" not in text
    assert "agent-release" not in text
    assert "channel: stable" not in text
    assert "distribution: stable" not in text
    assert "gh release" not in text
    assert "actions/create-release" not in text
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
