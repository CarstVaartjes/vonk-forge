import hashlib
import os
import re
import struct
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/agent-release.yml"
UNIFIED_WORKFLOW = ROOT / ".github/workflows/ci.yml"
PACKAGE_WORKFLOW = ROOT / ".github/actions/agent-package-build/action.yml"
APT_WORKFLOW = ROOT / ".github/actions/agent-apt-publish/action.yml"
APT_STATE = ROOT / "scripts/agent-apt-state"
NATIVE_LIFECYCLE = ROOT / "scripts/test-agent-package-native-lifecycle"

EXPECTED_ACTION_OUTPUTS = {
    "version": "${{ steps.accepted.outputs.version }}",
    "arm64_package": "${{ steps.accepted.outputs.arm64_package }}",
    "amd64_package": "${{ steps.accepted.outputs.amd64_package }}",
    "artifact_name": "${{ steps.accepted.outputs.artifact_name }}",
    "baseline_version": "${{ steps.accepted.outputs.baseline_version }}",
    "arm64_baseline_package": (
        "${{ steps.accepted.outputs.arm64_baseline_package }}"
    ),
    "amd64_baseline_package": (
        "${{ steps.accepted.outputs.amd64_baseline_package }}"
    ),
    "baseline_artifact_name": (
        "${{ steps.accepted.outputs.baseline_artifact_name }}"
    ),
    "amd64_lifecycle_package": (
        "${{ steps.accepted.outputs.amd64_lifecycle_package }}"
    ),
    "amd64_lifecycle_artifact_name": (
        "${{ steps.accepted.outputs.amd64_lifecycle_artifact_name }}"
    ),
}


def invalid_external_action_refs(text: str) -> list[str]:
    invalid: list[str] = []
    for match in re.finditer(r"^\s*uses:\s*(\S+)", text, re.MULTILINE):
        reference = match.group(1)
        if reference.startswith("./"):
            continue
        if re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", reference) is None:
            invalid.append(reference)
    return invalid


def workflow_action_pin_errors(root: Path) -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {}
    workflows = (
        *root.glob(".github/workflows/*.yml"),
        *root.glob(".github/workflows/*.yaml"),
        *root.glob(".github/actions/**/action.yml"),
        *root.glob(".github/actions/**/action.yaml"),
    )
    for path in sorted(workflows):
        invalid = invalid_external_action_refs(path.read_text())
        if invalid:
            errors[path.relative_to(root).as_posix()] = invalid
    return errors


def workflow_step(text: str, step_name: str) -> str:
    lines = text.splitlines()
    marker = next(
        line for line in lines if line.strip() == f"- name: {step_name}"
    )
    indent = len(marker) - len(marker.lstrip())
    step_start = lines.index(marker)
    step_lines: list[str] = []
    for line in lines[step_start:]:
        if line.startswith(f"{' ' * indent}- name: ") and step_lines:
            break
        step_lines.append(line)
    return "\n".join(step_lines)


def workflow_step_run(text: str, step_name: str) -> str:
    lines = text.splitlines()
    marker = next(
        line for line in lines if line.strip() == f"- name: {step_name}"
    )
    indent = len(marker) - len(marker.lstrip())
    step_start = lines.index(marker)
    run_marker = f"{' ' * (indent + 2)}run: |"
    run_start = lines.index(run_marker, step_start) + 1
    content_indent = indent + 4
    run_lines: list[str] = []
    for line in lines[run_start:]:
        if line and not line.startswith(" " * content_indent):
            break
        run_lines.append(line[content_indent:] if line else "")
    return "\n".join(run_lines)


def package_step(step_name: str) -> str:
    return workflow_step(PACKAGE_WORKFLOW.read_text(), step_name)


def package_step_run(step_name: str) -> str:
    return workflow_step_run(PACKAGE_WORKFLOW.read_text(), step_name)


def write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\nset -eu\n{body}\n")
    path.chmod(0o755)


def apt_workflow() -> str:
    return APT_WORKFLOW.read_text()


def apt_step(step_name: str) -> str:
    return workflow_step(apt_workflow(), step_name)


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
    public_der = public_spki(private_key)
    assert public_der.startswith(bytes.fromhex("302a300506032b6570032100"))
    return hashlib.sha256(public_der[-32:]).hexdigest()


def test_slot_manifest_signer_is_absent() -> None:
    assert not (ROOT / "scripts/sign-agent-release").exists()


def test_built_agent_package_contains_no_site_configuration(tmp_path: Path) -> None:
    build_digest = "sha256:" + "b" * 64
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in ("vonk-agent", "vonk-agent-helper"):
        raw = bytearray(384)
        raw[:16] = b"\x7fELF\x02\x01\x01" + bytes(9)
        struct.pack_into("<H", raw, 18, 183)
        marker = f"VONK_AGENT_BUILD_DIGEST={build_digest}".encode()
        raw[128 : 128 + len(marker)] = marker
        semantic_marker = b"VONK_AGENT_SEMANTIC_VERSION=0.1.0"
        raw[256 : 256 + len(semantic_marker)] = semantic_marker
        (binaries / name).write_bytes(raw)
        (binaries / name).chmod(0o555)
    private_key = tmp_path / "release.pem"
    generate_private_key(private_key, "ED25519")
    output = tmp_path / "dist"

    built = subprocess.run(
        [
            ROOT / "scripts/build-agent-deb",
            "--version",
            "0.1.0",
            "--architecture",
            "linux-arm64",
            "--build-digest",
            build_digest,
            "--release-private-key",
            private_key,
            "--binaries-dir",
            binaries,
            "--source-date-epoch",
            "1786060800",
            "--output-dir",
            output,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stderr
    payload = tmp_path / "payload"
    extracted = subprocess.run(
        [
            "/usr/bin/dpkg-deb",
            "--extract",
            output / "vonk-forge-agent_0.1.0_arm64.deb",
            payload,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert extracted.returncode == 0, extracted.stderr

    assert not (payload / "etc/vonk-forge-agent/agent.toml").exists()
    assert b"vonkforge.invalid" not in (output / "vonk-forge-agent_0.1.0_arm64.deb").read_bytes()


def test_agent_package_action_has_a_strict_input_and_output_boundary() -> None:
    text = PACKAGE_WORKFLOW.read_text()

    assert "using: composite" in text
    for input_name in (
        "channel",
        "publication_sequence",
        "version",
        "next_version",
        "arm64_package",
        "amd64_package",
        "artifact_name",
        "environment",
        "source_sha",
        "tag_name",
        "tag_oid",
        "expected_fingerprint",
        "release_private_key",
    ):
        assert re.search(
            rf"^  {input_name}:\n    description: .+\n    required: true$",
            text,
            re.MULTILINE,
        )
    action_outputs = text.split("\noutputs:\n", 1)[1].split("\nruns:\n", 1)[0]
    assert dict(
        re.findall(
            r"^  ([A-Za-z_][A-Za-z0-9_-]*):\n"
            r"    description: .+\n"
            r"    value: (.+)$",
            action_outputs,
            re.MULTILINE,
        )
    ) == EXPECTED_ACTION_OUTPUTS
    assert "secrets." not in text
    assert "vars." not in text


def test_reusable_agent_package_build_validates_authority_before_key_use() -> None:
    text = PACKAGE_WORKFLOW.read_text()

    validation = text.index("Validate package metadata and environment")
    authority = text.index("Revalidate protected package source authority")
    key = text.index("Materialize and verify protected agent key")
    build = text.index("Build package twice reproducibly")
    assert validation < authority < key < build
    authority_step = package_step("Revalidate protected package source authority")
    assert "scripts/verify-release-tag-authority" in authority_step
    assert "+refs/heads/main:refs/remotes/origin/main" in authority_step
    assert "SOURCE_SHA: ${{ inputs.source_sha }}" in authority_step
    assert "TAG_NAME: ${{ inputs.tag_name }}" in authority_step
    assert "TAG_OID: ${{ inputs.tag_oid }}" in authority_step
    assert "dev:agent-development" in text
    assert "stable:agent-release" in text
    assert "scripts/agent-package-metadata" in text
    assert "RELEASE_PRIVATE_KEY: ${{ inputs.release_private_key }}" in text
    assert "EXPECTED_FINGERPRINT: ${{ inputs.expected_fingerprint }}" in text
    assert "openssl pkey" in text
    assert "-pubout -outform DER" in text
    assert "sha256sum" in text


def test_cross_compiler_install_includes_target_glibc_headers(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    sudo_log = tmp_path / "sudo.log"
    write_executable(fake_bin / "uname", 'printf "%s\\n" aarch64')
    write_executable(
        fake_bin / "sudo",
        'printf "%s\\n" "$*" >> "$SUDO_LOG"',
    )
    write_executable(fake_bin / "rustup", ":")

    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            package_step_run("Install pinned Rust toolchain"),
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "SUDO_LOG": str(sudo_log),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    install = next(
        line
        for line in sudo_log.read_text().splitlines()
        if line.startswith("apt-get install ")
    )
    installed = set(install.split()[4:])
    assert installed == {
        "binutils-x86-64-linux-gnu",
        "gcc-x86-64-linux-gnu",
        "libc6-dev-amd64-cross",
    }


def test_reusable_agent_package_build_validates_publication_authority() -> None:
    validation = package_step("Validate package metadata and environment")

    assert "PUBLICATION_SEQUENCE: ${{ inputs.publication_sequence }}" in validation
    assert 'test "$PUBLICATION_SEQUENCE" = "$GITHUB_RUN_NUMBER"' in validation
    assert 'test "$PUBLICATION_SEQUENCE" = 0' in validation
    assert '"$GITHUB_SHA" "$PUBLICATION_SEQUENCE"' in validation
    assert re.search(r"git show(?: -s)? --format=%ct", validation) is None


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
    assert text.count("scripts/build-agent-deb") >= 6
    assert "NEXT_VERSION: ${{ inputs.next_version }}" in text
    assert 'IFS=. read -r major minor patch <<< "$VERSION"' not in text
    assert "cmp --silent" in text
    assert "scripts/verify-agent-deb" in text
    assert "cargo fmt --all --check" in text
    assert "cargo clippy --workspace --all-targets --locked -- -D warnings" in text
    assert "cargo test --workspace --locked" in text
    assert "scripts/verify-agent-systemd" in text
    assert "scripts/test-agent-package-native-lifecycle" in text
    for architecture in ("linux-arm64", "linux-amd64"):
        assert f"--architecture {architecture}" in text
    for architecture in ("arm64", "amd64"):
        assert f'vonk-forge-agent_${{VERSION}}_{architecture}.deb' in text
    assert "vonk-agent-supervisor" not in text
    assert "/var/lib/vonk-forge/slots" not in text
    assert "/var/lib/vonk-forge/supervisor" not in text
    assert "curl " not in text
    assert "wget " not in text
    assert "actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6" in text
    assert "sigstore/cosign-installer@6f9f17788090df1f26f669e9d70d6ae9567deba6" in text
    assert "cosign sign-blob --yes --bundle" in text
    assert "agent-release\\.yml@refs/heads/main" in text
    assert "ci\\.yml@refs/tags/v" in text


def test_native_lifecycle_preserves_root_owned_machine_identity() -> None:
    text = NATIVE_LIFECYCLE.read_text()

    assert 'chown -R vonk-agent:vonk-agent "$data_dir" "$runtime_dir"' not in text
    assert (
        'chown -R vonk-agent:vonk-agent "$data_dir/.config" '
        '"$credentials" "$runtime_dir"'
    ) in text
    assert text.count("stat -c '%U:%G:%a' \"$data_dir/machine-evidence\"") == 2


def test_package_build_publishes_dual_architecture_lower_acceptance_baseline() -> None:
    text = PACKAGE_WORKFLOW.read_text()
    validation = package_step("Validate package metadata and environment")
    lifecycle = package_step_run(
        "Test fresh, offline, upgrade, downgrade, remove lifecycle"
    )
    upload = package_step("Upload immutable acceptance baseline packages")

    assert "baseline_version:" in text
    assert "arm64_baseline_package:" in text
    assert "amd64_baseline_package:" in text
    assert "baseline_artifact_name:" in text
    assert "BASELINE_VERSION: ${{ inputs.baseline_version }}" in validation
    assert "--acceptance-baseline" in lifecycle
    assert '"$BASELINE_VERSION" "$VERSION"' in lifecycle
    for architecture in ("arm64", "amd64"):
        assert f"vonk-forge-agent_${{{{ inputs.baseline_version }}}}_{architecture}.deb" in upload
    assert "retention-days: 7" in upload
    assert "overwrite: false" in upload


def test_package_build_outputs_and_attestations_name_both_architectures() -> None:
    text = PACKAGE_WORKFLOW.read_text()
    for architecture in ("arm64", "amd64"):
        assert re.search(rf"^  {architecture}_package:\n", text, re.MULTILINE)
        package = f"vonk-forge-agent_${{{{ inputs.version }}}}_{architecture}.deb"
        assert f"subject-path: dist/{package}" in text
        assert f"sbom-path: dist/{package[:-4]}.sbom.spdx.json" in text
    assert "subject-path: dist/vonk-forge-agent_${{ inputs.version }}_*.deb" not in text


def assert_agent_key_cleanup_contract(text: str) -> None:
    step_names = re.findall(r"^\s+- name: (.+)$", text, re.MULTILINE)
    lifecycle_name = "Test fresh, offline, upgrade, downgrade, remove lifecycle"
    lifecycle_index = step_names.index(lifecycle_name)
    fallback_name = "Remove protected agent key"
    cosign_name = "Install Cosign"
    lifecycle = workflow_step_run(
        text,
        lifecycle_name,
    )
    immediate_cleanup = 'rm -f "$RUNNER_TEMP/vonk-agent-release.pem"'
    fallback = workflow_step(text, fallback_name)

    assert lifecycle.count("scripts/build-agent-deb") == 3
    assert "--acceptance-baseline" in lifecycle
    assert "--architecture linux-arm64" in lifecycle
    assert "--architecture linux-amd64" in lifecycle
    final_build = lifecycle.rindex("scripts/build-agent-deb")
    cleanup = lifecycle.index(immediate_cleanup)
    helper = lifecycle.rindex("sudo scripts/test-agent-package-native-lifecycle")
    assert final_build < cleanup < helper
    assert "$RUNNER_TEMP/vonk-agent-release.pem" not in lifecycle[helper:]
    assert immediate_cleanup in fallback
    assert fallback.splitlines()[1].strip() == "if: ${{ always() }}"
    assert step_names[lifecycle_index : lifecycle_index + 3] == [
        lifecycle_name,
        fallback_name,
        "Upload short-lived AMD64 lifecycle package",
    ]
    assert step_names[lifecycle_index + 3 : lifecycle_index + 5] == [
        "Upload immutable acceptance baseline packages",
        cosign_name,
    ]
    assert text.count(immediate_cleanup) == 2


def test_agent_key_is_removed_immediately_after_final_use_with_always_fallback() -> None:
    assert_agent_key_cleanup_contract(PACKAGE_WORKFLOW.read_text())


def test_agent_key_cleanup_guard_rejects_fallback_before_lifecycle() -> None:
    text = PACKAGE_WORKFLOW.read_text()
    fallback = workflow_step(text, "Remove protected agent key").rstrip()
    lifecycle_marker = next(
        line
        for line in text.splitlines()
        if line.strip()
        == "- name: Test fresh, offline, upgrade, downgrade, remove lifecycle"
    )
    mutated = text.replace(f"{fallback}\n\n", "", 1).replace(
        lifecycle_marker,
        f"{fallback}\n\n{lifecycle_marker}",
        1,
    )

    with pytest.raises(AssertionError):
        assert_agent_key_cleanup_contract(mutated)


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
        "ci\\.yml@refs/tags/v1\\.2\\.3$\n"
    )


def test_reusable_agent_package_build_uploads_candidate_and_acceptance_baseline_sets() -> None:
    text = PACKAGE_WORKFLOW.read_text()

    assert text.count("actions/upload-artifact@") == 3
    accepted = workflow_step(text, "Upload exact package release set")
    baseline = workflow_step(text, "Upload immutable acceptance baseline packages")
    lifecycle = workflow_step(text, "Upload short-lived AMD64 lifecycle package")

    assert "name: ${{ inputs.artifact_name }}" in accepted
    assert "retention-days: 30" in accepted
    assert "path: dist/*" in accepted
    assert "name: ${{ inputs.baseline_artifact_name }}" in baseline
    assert "retention-days: 7" in baseline
    for architecture in ("arm64", "amd64"):
        package = (
            f"vonk-forge-agent_${{{{ inputs.baseline_version }}}}_{architecture}.deb"
        )
        assert package in baseline
        assert f"{package}.sha256" in baseline
    assert "name: ${{ steps.accepted.outputs.amd64_lifecycle_artifact_name }}" in lifecycle
    assert "retention-days: 1" in lifecycle
    assert "vonk-forge-agent_${{ inputs.next_version }}_amd64.deb" in lifecycle
    assert "vonk-forge-agent_${{ inputs.next_version }}_amd64.deb.sha256" in lifecycle
    assert "dist/" not in lifecycle
    for step in (accepted, baseline, lifecycle):
        assert "overwrite: false" in step
        assert "if-no-files-found: error" in step


def test_development_agent_workflow_runs_only_for_exact_main_sources() -> None:
    text = WORKFLOW.read_text()
    metadata = text.split("\n  build-test-sign:\n", 1)[0]

    assert (
        '  push:\n    branches: [main]\n    paths-ignore:\n'
        '      - "docs/**"\n      - "**/README.md"'
    ) in text
    dispatch = text.split("  workflow_dispatch:", 1)[1].split("\n\npermissions:", 1)[0]
    assert "inputs:" not in dispatch
    assert "version:" not in dispatch
    assert metadata.count("fetch-depth: 0") == 1
    assert metadata.count("git fetch --no-tags --prune origin") == 1
    assert metadata.count('test "$GITHUB_REF" = "refs/heads/main"') == 1
    assert metadata.count('test "$GITHUB_SHA" = "$main_sha"') == 1
    assert text.index("Verify exact current main tip") < text.index(
        "Derive immutable development package metadata"
    )
    assert "verify-main-for-apt:" not in text


def test_development_metadata_uses_actions_publication_sequence() -> None:
    text = WORKFLOW.read_text()
    metadata = workflow_step(text, "Derive immutable development package metadata")

    assert "PUBLICATION_SEQUENCE: ${{ github.run_number }}" in metadata
    assert 'test "$PUBLICATION_SEQUENCE" = "$GITHUB_RUN_NUMBER"' in metadata
    assert '"$GITHUB_SHA" "$PUBLICATION_SEQUENCE"' in metadata
    assert re.search(r"git show(?: -s)? --format=%ct", metadata) is None


def test_development_agent_workflow_binds_both_literal_environment_boundaries() -> None:
    text = WORKFLOW.read_text()

    assert "uses: ./.github/actions/agent-package-build" in text
    assert "channel: ${{ needs.package-metadata.outputs.channel }}" in text
    assert "publication_sequence: ${{ github.run_number }}" in text
    assert "environment: agent-development" in text
    assert "artifact-metadata: write" in text
    assert "attestations: write" in text
    assert "id-token: write" in text
    assert "uses: ./.github/actions/agent-apt-publish" in text
    assert "environment: apt-development" in text
    assert "source_sha: ${{ github.sha }}" in text
    assert "tag_name: ''" in text
    assert "tag_oid: ''" in text
    assert "needs: [package-metadata, build-test-sign, native-amd64-lifecycle]" in text
    assert "artifact_name: ${{ needs.build-test-sign.outputs.artifact_name }}" in text
    for architecture in ("arm64", "amd64"):
        assert (
            f"{architecture}_package: "
            f"${{{{ needs.build-test-sign.outputs.{architecture}_package }}}}" in text
        )
    assert "release_private_key: ${{ secrets.VONK_AGENT_RELEASE_PRIVATE_KEY }}" in text
    assert "apt_gpg_passphrase: ${{ secrets.APT_GPG_PASSPHRASE }}" in text
    assert "r2_access_key_id: ${{ secrets.R2_ACCESS_KEY_ID }}" in text
    assert "secrets:" not in text
    for forbidden in (
        "scripts/build-agent-deb",
        "cosign sign-blob",
    ):
        assert forbidden not in text


def test_development_publication_requires_native_amd64_lifecycle() -> None:
    text = WORKFLOW.read_text()
    lifecycle = text.split("\n  native-amd64-lifecycle:\n", 1)[1].split(
        "\n  publish-apt:\n", 1
    )[0]

    assert "needs: [package-metadata, build-test-sign]" in lifecycle
    assert "runs-on: ubuntu-24.04" in lifecycle
    assert "actions/download-artifact@" in lifecycle
    assert 'test "$(uname -m)" = x86_64' in lifecycle
    assert 'scripts/verify-agent-deb --json "$package"' in lifecycle
    assert 'dpkg -i "$package"' in lifecycle
    assert '/usr/lib/vonk-forge/vonk-agent --version' in lifecycle
    assert "needs: [package-metadata, build-test-sign, native-amd64-lifecycle]" in text


def test_commit_timestamps_only_seed_reproducible_package_bytes() -> None:
    timestamp = re.compile(r"git show(?: -s)? --format=%ct")
    package_text = PACKAGE_WORKFLOW.read_text()
    allowed_steps = (
        package_step("Build package twice reproducibly"),
        package_step("Test fresh, offline, upgrade, downgrade, remove lifecycle"),
    )

    assert timestamp.findall(WORKFLOW.read_text()) == []
    assert timestamp.findall(UNIFIED_WORKFLOW.read_text()) == []
    assert len(timestamp.findall(package_text)) == 2
    for step in allowed_steps:
        assert len(timestamp.findall(step)) == 1
        assert '--source-date-epoch "$epoch"' in step


def test_apt_publish_action_has_a_strict_channel_boundary() -> None:
    text = apt_workflow()

    assert "using: composite" in text
    for input_name in (
        "channel",
        "version",
        "arm64_package",
        "amd64_package",
        "artifact_name",
        "environment",
        "source_sha",
        "tag_name",
        "tag_oid",
        "apt_gpg_passphrase",
        "apt_repository_gpg_fingerprint",
        "apt_repository_gpg_private_key",
        "r2_access_key_id",
        "r2_account_id",
        "r2_secret_access_key",
        "r2_apt_public_bucket",
        "r2_apt_state_bucket",
    ):
        assert re.search(
            rf"^  {input_name}:\n    description: .+\n    required: true$",
            text,
            re.MULTILINE,
        )
    for forbidden in ("repository:", "distribution:", "keyring:", "state_prefix:"):
        assert f"  {forbidden}" not in text.split("\ninputs:\n", 1)[1].split(
            "\nruns:\n", 1
        )[0]
    assert "dev:apt-development" in text
    assert "stable:apt-release" in text
    assert "secrets." not in text
    assert "vars." not in text
    development = WORKFLOW.read_text()
    production = UNIFIED_WORKFLOW.read_text()
    assert "environment: apt-development" in development
    assert "environment: apt-release" in production
    assert "group: vonk-forge-agent-apt-dev" in development
    assert "group: vonk-forge-agent-apt-stable" in production


def test_apt_publisher_verifies_and_indexes_both_architectures_as_one_release() -> None:
    verify = apt_step("Verify exact downloaded package")
    generation = apt_step("Generate missing aptly state or public tree")

    for architecture in ("arm64", "amd64"):
        assert f'vonk-forge-agent_${{VERSION}}_{architecture}.deb' in verify
    assert '.architecture == $architecture' in verify
    assert 'for package in "$ARM64_PACKAGE" "$AMD64_PACKAGE"' in generation
    assert 'repo add "$REPOSITORY" "dist/$package"' in generation
    assert 'binary-$architecture/Packages' in generation
    assert 'architectures:["amd64","arm64"]' in generation
    assert "-architectures=amd64,arm64" in generation


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
    step_names = re.findall(r"^\s+- name: (.+)$", text, re.MULTILINE)
    authority_index = step_names.index(
        "Reverify accepted development source authority"
    )

    assert "environment: apt-development" in WORKFLOW.read_text()
    assert "environment: apt-release" in UNIFIED_WORKFLOW.read_text()
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


def test_reusable_publishers_revalidate_stable_authority_at_mutation_boundaries() -> None:
    package_text = PACKAGE_WORKFLOW.read_text()
    apt_text = APT_WORKFLOW.read_text()

    for mutation in (
        "Materialize and verify protected agent key",
        "Create and verify keyless package signature",
        "Attest package provenance",
        "Attest package SBOM",
        "Upload exact package release set",
    ):
        mutation_index = package_text.index(f"- name: {mutation}")
        prior = package_text[:mutation_index]
        assert prior.rfind("scripts/verify-release-tag-authority") >= 0

    for mutation in (
        "Prepare committed or recoverable private state",
        "Materialize and verify apt signing key",
        "Commit immutable publication manifest",
        "Publish exact committed public tree and latest pointer",
    ):
        mutation_index = apt_text.index(f"- name: {mutation}")
        prior = apt_text[:mutation_index]
        assert prior.rfind("scripts/verify-release-tag-authority") >= 0

    for text in (package_text, apt_text):
        assert "SOURCE_SHA: ${{ inputs.source_sha }}" in text
        assert "TAG_NAME: ${{ inputs.tag_name }}" in text
        assert "TAG_OID: ${{ inputs.tag_oid }}" in text


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


def test_reusable_apt_publisher_enables_and_verifies_by_hash_indexes() -> None:
    local = apt_step("Generate missing aptly state or public tree")

    snapshot = re.search(
        r'aptly -config="\$config" publish snapshot.*?filesystem:r2:', local, re.DOTALL
    )
    switch = re.search(
        r'aptly -config="\$config" publish switch.*?"\$SNAPSHOT"', local, re.DOTALL
    )

    assert snapshot is not None
    assert switch is not None
    assert "-acquire-by-hash" in snapshot.group()
    assert "-acquire-by-hash" not in switch.group()
    assert "Acquire-By-Hash: yes" in local
    assert "by-hash/SHA256" in local
    assert local.index("publish snapshot") < local.index("Acquire-By-Hash: yes")


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


def test_reusable_apt_publisher_supports_bucket_scoped_r2_tokens() -> None:
    text = apt_workflow()
    remote_count = text.count("RCLONE_CONFIG_R2_TYPE: s3")
    no_bucket_check_count = text.count(
        'RCLONE_CONFIG_R2_NO_CHECK_BUCKET: "true"'
    )

    assert remote_count == 3
    assert no_bucket_check_count == remote_count


def test_release_actions_are_commit_pinned_and_secrets_are_environment_scoped() -> None:
    agent_text = WORKFLOW.read_text()
    unified_text = UNIFIED_WORKFLOW.read_text()
    package_text = PACKAGE_WORKFLOW.read_text()
    apt_text = APT_WORKFLOW.read_text()
    assert "uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in agent_text
    assert "uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in unified_text
    assert (
        "uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
        in package_text
    )
    assert (
        "uses: actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"
        in apt_text
    )
    assert workflow_action_pin_errors(ROOT) == {}
    for bad_reference in (
        "actions/checkout@v4",
        "actions/checkout@main",
        "actions/checkout@3d3c42e",
        f"actions/checkout@{'g' * 40}",
    ):
        assert invalid_external_action_refs(f"uses: {bad_reference}\n") == [
            bad_reference
        ]
    assert invalid_external_action_refs(
        "uses: ./.github/actions/agent-package-build\n"
    ) == []
    assert "permissions:\n  contents: read" in agent_text
    assert "contents: write" in unified_text
    assert "id-token: write" in agent_text
    assert "id-token: write" in unified_text


def test_action_pin_guard_scans_yaml_and_keeps_local_calls_exempt(
    tmp_path: Path,
) -> None:
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "unpinned.yaml").write_text(
        "jobs:\n"
        "  test:\n"
        "    uses: actions/checkout@main\n"
        "  local:\n"
        "    uses: ./.github/workflows/local.yml\n"
    )

    assert workflow_action_pin_errors(tmp_path) == {
        ".github/workflows/unpinned.yaml": ["actions/checkout@main"]
    }


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
