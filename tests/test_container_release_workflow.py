import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ci.yml"
DEV_WORKFLOW = ROOT / ".github/workflows/dev-images.yml"
APT_WORKFLOW = ROOT / ".github/actions/agent-apt-publish/action.yml"
ALLOWED_SIGNERS = ROOT / ".github/release-allowed-signers"


def workflow() -> str:
    return WORKFLOW.read_text()


def job(job_name: str) -> str:
    lines = workflow().splitlines()
    job_start = lines.index(f"  {job_name}:") + 1
    job_lines: list[str] = []
    for line in lines[job_start:]:
        if re.fullmatch(r"  [a-zA-Z0-9_-]+:", line):
            break
        job_lines.append(line)
    return "\n".join(job_lines)


def workflow_step(job_name: str, step_name: str) -> str:
    lines = job(job_name).splitlines()
    step_start = lines.index(f"      - name: {step_name}")
    step_lines: list[str] = []
    for line in lines[step_start:]:
        if line.startswith("      - name: ") and step_lines:
            break
        step_lines.append(line)
    return "\n".join(step_lines)


def step_run(job_name: str, step_name: str) -> str:
    lines = workflow_step(job_name, step_name).splitlines()
    run_start = lines.index("        run: |") + 1
    run_lines: list[str] = []
    for line in lines[run_start:]:
        if line and not line.startswith("          "):
            break
        run_lines.append(line[10:] if line else "")
    return "\n".join(run_lines)


def step_block(job_name: str, step_name: str, key: str) -> list[str]:
    lines = workflow_step(job_name, step_name).splitlines()
    block_start = lines.index(f"          {key}: |") + 1
    block_lines: list[str] = []
    for line in lines[block_start:]:
        if line and not line.startswith("            "):
            break
        block_lines.append(line[12:] if line else "")
    return block_lines


def development_step_run(step_name: str) -> str:
    lines = DEV_WORKFLOW.read_text().splitlines()
    step_start = lines.index(f"      - name: {step_name}")
    run_start = lines.index("        run: |", step_start) + 1
    run_lines: list[str] = []
    for line in lines[run_start:]:
        if line and not line.startswith("          "):
            break
        run_lines.append(line[10:] if line else "")
    return "\n".join(run_lines)


def write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{body}\n")
    path.chmod(0o755)


def release_expressions() -> dict[str, str]:
    digest = f"sha256:{'a' * 64}"
    return {
        "${{ needs.release-metadata.outputs.version }}": "1.2.3",
        "${{ needs.release-metadata.outputs.image_version_tag }}": "v1.2.3",
        "${{ needs.release-metadata.outputs.api_image }}": "ghcr.io/example/api",
        "${{ needs.release-metadata.outputs.worker_image }}": "ghcr.io/example/worker",
        "${{ needs.release-metadata.outputs.hermes_image }}": "ghcr.io/example/hermes",
        "${{ needs.publish-images.outputs.api_digest }}": digest,
        "${{ needs.publish-images.outputs.worker_digest }}": digest,
        "${{ needs.publish-images.outputs.hermes_digest }}": digest,
    }


def test_development_image_publication_keeps_copy_progress_out_of_outputs(
    tmp_path: Path,
) -> None:
    digest = f"sha256:{'a' * 64}"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    skopeo = fake_bin / "skopeo"
    skopeo.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
command=$1
shift
case "$command" in
  inspect)
    if [[ "${{1:-}}" == --raw ]]; then
      printf '{{"schemaVersion":2}}\\n'
      exit 0
    fi
    printf 'manifest unknown\\n' >&2
    exit 1
    ;;
  manifest-digest)
    printf '%s\\n' '{digest}'
    ;;
  copy)
    digest_file=
    while (( $# )); do
      if [[ "$1" == --digestfile ]]; then
        digest_file=$2
        shift 2
      else
        shift
      fi
    done
    printf 'Copying 2 images generated from 2 images in list\\n'
    printf '%s\\n' '{digest}' > "$digest_file"
    ;;
  *)
    exit 64
    ;;
esac
"""
    )
    skopeo.chmod(0o755)
    output = tmp_path / "github-output"
    accepted = tmp_path / "accepted"
    accepted.mkdir()
    result = subprocess.run(
        [
            "bash",
            "-c",
            development_step_run("Publish immutable tested images"),
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "API_IMAGE": "ghcr.io/example/api",
            "GITHUB_OUTPUT": str(output),
            "HERMES_IMAGE": "ghcr.io/example/hermes",
            "IMMUTABLE_TAG": "dev-sha-" + "b" * 40,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "WORKER_IMAGE": "ghcr.io/example/worker",
        },
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert output.read_text().splitlines() == [
        f"api_digest={digest}",
        f"worker_digest={digest}",
        f"hermes_digest={digest}",
    ]


def test_development_images_build_supported_linux_architectures(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    write_executable(
        fake_bin / "docker",
        'printf "%s\\n" "$*" >> "$DOCKER_LOG"',
    )

    result = subprocess.run(
        ["bash", "-c", development_step_run("Build exact OCI archives")],
        cwd=ROOT,
        env={
            **os.environ,
            "DOCKER_LOG": str(docker_log),
            "GITHUB_REPOSITORY": "CarstVaartjes/vonk-forge",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_SHA": "a" * 40,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
        },
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    builds = docker_log.read_text().splitlines()
    assert len(builds) == 3
    assert all(line.startswith("buildx build ") for line in builds)
    assert all("--platform linux/amd64,linux/arm64" in line for line in builds)
    assert all("--output type=oci,dest=" in line for line in builds)


def test_development_images_enable_arm64_emulation_before_building() -> None:
    text = DEV_WORKFLOW.read_text()
    setup = (
        "docker/setup-qemu-action@96fe6ef7f33517b61c61be40b68a1882f3264fb8 "
        "# v4.2.0"
    )

    assert setup in text
    assert text.index(setup) < text.index("docker/setup-buildx-action@")
    assert text.index(setup) < text.index("- name: Build exact OCI archives")
    qemu = text[text.index(setup) : text.index("- name: Build exact OCI archives")]
    assert (
        "image: docker.io/tonistiigi/binfmt@sha256:"
        "400a4873b838d1b89194d982c45e5fb3cda4593fbfd7e08a02e76b03b21166f0"
        in qemu
    )
    assert "platforms: arm64" in qemu


def test_development_image_publication_requires_both_platforms() -> None:
    verification = development_step_run(
        "Verify immutable manifests and attestations"
    )

    assert "--format '{{ json .Manifest }}'" in verification
    assert "scripts/verify-multiarch-image-manifest" in verification
    assert '"docker://$image@$runnable_digest"' in verification
    assert verification.count('"$image@$digest"') == 3
    assert 'done < "$platform_records"' in verification
    assert '--arg platform "linux/$architecture"' in verification
    assert verification.count('.[$platform]') == 2
    assert verification.count(
        'keys | sort == ["linux/amd64", "linux/arm64"]'
    ) == 2


def image_descriptor(architecture: str, marker: str) -> dict[str, object]:
    return {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "digest": f"sha256:{marker * 64}",
        "platform": {"os": "linux", "architecture": architecture},
    }


def attestation_descriptor(subject_marker: str, marker: str) -> dict[str, object]:
    return {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "digest": f"sha256:{marker * 64}",
        "platform": {"os": "unknown", "architecture": "unknown"},
        "annotations": {
            "vnd.docker.reference.type": "attestation-manifest",
            "vnd.docker.reference.digest": f"sha256:{subject_marker * 64}",
        },
    }


def verify_multiarch_fixture(tmp_path: Path, manifests: list[dict[str, object]]):
    fixture = tmp_path / "manifest.json"
    fixture.write_text(
        json.dumps(
            {
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": manifests,
            }
        )
    )
    return subprocess.run(
        [ROOT / "scripts/verify-multiarch-image-manifest", fixture],
        capture_output=True,
        check=False,
        text=True,
    )


def test_multiarch_manifest_contract_binds_attestations_to_each_leaf(
    tmp_path: Path,
) -> None:
    result = verify_multiarch_fixture(
        tmp_path,
        [
            image_descriptor("amd64", "a"),
            image_descriptor("arm64", "b"),
            attestation_descriptor("a", "c"),
            attestation_descriptor("b", "d"),
        ],
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        f"amd64\tsha256:{'a' * 64}\tsha256:{'c' * 64}",
        f"arm64\tsha256:{'b' * 64}\tsha256:{'d' * 64}",
    ]


@pytest.mark.parametrize(
    "invalid_descriptor",
    [
        image_descriptor("s390x", "e"),
        image_descriptor("amd64", "e"),
        attestation_descriptor("e", "f"),
    ],
)
def test_multiarch_manifest_contract_rejects_extra_or_unbound_descriptors(
    tmp_path: Path, invalid_descriptor: dict[str, object]
) -> None:
    result = verify_multiarch_fixture(
        tmp_path,
        [
            image_descriptor("amd64", "a"),
            image_descriptor("arm64", "b"),
            attestation_descriptor("a", "c"),
            attestation_descriptor("b", "d"),
            invalid_descriptor,
        ],
    )

    assert result.returncode != 0


def rendered_step_run(job_name: str, step_name: str) -> str:
    script = step_run(job_name, step_name)
    for expression, value in release_expressions().items():
        script = script.replace(expression, value)
    validator = ROOT / "scripts/validate-container-release-digests"
    return script.replace("scripts/validate-container-release-digests", str(validator))


def test_release_metadata_is_tag_only_and_read_only() -> None:
    text = workflow()
    assert "release-metadata:" in text
    assert "github.ref_type == 'tag'" in text
    assert "startsWith(github.ref_name, 'v')" in text
    assert "scripts/container-release-metadata" in text
    metadata = job("release-metadata")
    for output in (
        "image_version_tag",
        "dev_tag",
        "latest_alias",
        "api_dev_source",
        "worker_dev_source",
    ):
        assert f"{output}: ${{{{ steps.release.outputs.{output} }}}}" in metadata
    assert "vars.VONK_PLATFORM_RELEASES_ENABLED == 'true'" in metadata


def test_release_tag_is_ssh_signed_trusted_and_reachable_from_main() -> None:
    metadata = job("release-metadata")
    verify = workflow_step("release-metadata", "Verify signed accepted release tag")

    assert "fetch-depth: 0" in workflow_step(
        "release-metadata", "Check out tagged commit"
    )
    assert "+refs/heads/main:refs/remotes/origin/main" in verify
    assert "git show refs/remotes/origin/main:.github/release-allowed-signers" in verify
    assert "git cat-file -t" in verify and "= tag" in verify
    assert "gpg.format=ssh" in verify
    assert "gpg.ssh.allowedSignersFile" in verify
    assert "verify-tag" in verify
    assert "git merge-base --is-ancestor" in verify
    assert metadata.index("Verify signed accepted release tag") < metadata.index(
        "Validate release metadata"
    )
    assert "tag_oid: ${{ steps.authority.outputs.tag_oid }}" in metadata
    assert 'printf \'tag_oid=%s\\n\'' in verify


def test_immutable_tag_authority_is_threaded_into_reusable_publishers() -> None:
    metadata = job("release-metadata")
    package = job("build-agent-package")
    apt = job("publish-apt")

    assert "tag_oid: ${{ steps.authority.outputs.tag_oid }}" in metadata
    for caller in (package, apt):
        assert "source_sha: ${{ github.sha }}" in caller
        assert "tag_name: ${{ github.ref_name }}" in caller
        assert "tag_oid: ${{ needs.release-metadata.outputs.tag_oid }}" in caller


def test_each_protected_production_mutation_revalidates_exact_tag_authority() -> None:
    boundaries = {
        "publish-images": (
            "Log in to GHCR",
            "Promote accepted API image",
            "Promote accepted worker image",
            "Build and push Hermes image",
        ),
        "release-manifest": ("Create public GitHub Release",),
    }
    for job_name, mutations in boundaries.items():
        job_text = job(job_name)
        for mutation in mutations:
            mutation_index = job_text.index(f"- name: {mutation}")
            prior = job_text[:mutation_index]
            authority_index = prior.rfind("scripts/verify-release-tag-authority")
            assert authority_index >= 0, f"{job_name}: {mutation} lacks revalidation"
            authority_step_start = prior.rfind("- name:", 0, authority_index)
            authority_step = prior[authority_step_start:]
            assert "needs.release-metadata.outputs.tag_oid" in authority_step
            assert "github.sha" in authority_step


def test_alias_reconciliation_binds_selected_release_to_signed_tag_revision() -> None:
    alias = job("advance-production-aliases")
    evidence = workflow_step(
        "advance-production-aliases",
        "Bind release digests to selected source revision and evidence",
    )

    assert "attestations: read" in alias
    assert "tag_oid" in alias and "target_commit" in alias
    assert "scripts/verify-release-tag-authority" in alias
    assert alias.index("scripts/verify-release-tag-authority") < alias.index(
        "Log in to GHCR"
    )
    assert 'org.opencontainers.image.revision' in alias
    assert "docker buildx imagetools inspect" in alias
    assert ".Provenance" in alias
    assert "gh attestation verify" in evidence
    assert (
        '--signer-workflow '
        '"$GITHUB_REPOSITORY/.github/workflows/dev-images.yml"' in evidence
    )
    assert "--signer-workflow dev-images.yml" not in evidence
    assert '--signer-digest "$TARGET_COMMIT"' in evidence
    assert '--source-digest "$TARGET_COMMIT"' in evidence
    assert "--source-ref refs/heads/main" in evidence
    assert "--deny-self-hosted-runners" in evidence
    assert "scripts/promote-image-aliases" in alias
    promotion = alias.index("scripts/promote-image-aliases")
    assert alias.rfind("scripts/verify-release-tag-authority", 0, promotion) >= 0
    postconditions = (ROOT / "scripts/verify-production-alias-postconditions").read_text()
    assert "scripts/verify-production-alias-postconditions" in alias
    assert "scripts/verify-release-tag-authority" in postconditions


def test_production_release_retries_reconcile_immutable_outputs_before_completion() -> None:
    publisher = job("publish-images")
    release = workflow_step("release-manifest", "Create public GitHub Release")
    aliases = workflow_step(
        "advance-production-aliases",
        "Reconcile the newest completed production release",
    )

    assert "Reconcile existing Hermes release image" in publisher
    assert "Verify and record Hermes release image" in publisher
    assert "scripts/reconcile-hermes-release-image" in publisher
    assert "Attest Hermes release provenance" in publisher
    assert "refuse-existing-image-version" not in publisher
    assert "scripts/reconcile-github-release" in release
    assert "SOURCE_SHA: ${{ github.sha }}" in release
    assert "--postcondition" in aliases
    assert "scripts/verify-production-alias-postconditions" in aliases


def test_alias_uses_only_attested_immutable_release_assets_before_parsing() -> None:
    alias = job("advance-production-aliases")
    selection = workflow_step(
        "advance-production-aliases", "Select newest completed signed release"
    )
    authority = workflow_step(
        "advance-production-aliases",
        "Revalidate selected authority before evidence credentials",
    )
    evidence = workflow_step(
        "advance-production-aliases",
        "Bind release digests to selected source revision and evidence",
    )
    assert "(.immutable == true)" in selection
    postconditions = (ROOT / "scripts/verify-production-alias-postconditions").read_text()
    assert "(.immutable == true)" in postconditions
    assert "gh release verify \"$TARGET_TAG\"" in evidence
    assert evidence.count("gh release verify-asset") == 1
    for asset in (
        "vonk-forge-images.env",
        "vonk-forge-images.env.sha256",
    ):
        assert asset in evidence
    for asset in ("vonk-forge-images.env", "vonk-forge-images.env.sha256"):
        assert f'"$EVIDENCE_DIR/{asset}"' in evidence
    release_verify = evidence.index('gh release verify "$TARGET_TAG"')
    first_asset_verify = evidence.index("gh release verify-asset")
    checksum_parse = evidence.index("mapfile -t checksum_lines")
    evidence_parse = evidence.index("declare -A release_images")
    assert release_verify < first_asset_verify < checksum_parse < evidence_parse
    assert "sha256sum" in evidence
    assert "GH_TOKEN: ${{ github.token }}" in evidence
    assert alias.count("GH_TOKEN: ${{ github.token }}") == 1
    assert "persist-credentials: false" in alias
    assert alias.index(authority) < alias.index(evidence) < alias.index("Log in to GHCR")


def test_release_signer_allowlist_contains_only_public_ssh_authority() -> None:
    text = ALLOWED_SIGNERS.read_text(encoding="utf-8")

    assert text.endswith("\n")
    assert text.count("\n") == 1
    assert "cvaartjes@visualfabriq.com ssh-ed25519 " in text
    assert "PRIVATE" not in text


def test_production_compose_binds_actual_digest_pinned_build_outputs() -> None:
    builder = job("publish-images")
    build = workflow_step("publish-images", "Render canonical production Compose")

    assert "scripts/render-production-compose" in build
    assert "steps.api.outputs.digest" in build
    assert "steps.worker.outputs.digest" in build
    assert "steps.hermes.outputs.digest" in build
    assert "docker-compose.production.yml" in build
    assert "control-deployment" not in builder
    assert "platform-release.json" not in builder


def test_release_attaches_canonical_production_compose() -> None:
    manifest = job("release-manifest")
    assert "Download platform publication evidence" in manifest
    release = workflow_step("release-manifest", "Create public GitHub Release")
    assert "docker-compose.production.yml" in release
    for obsolete in (
        "control-deployment-descriptor.json",
        "bundle-publication.json",
        "platform-release.json",
    ):
        assert obsolete not in release


def test_release_chain_is_default_off_and_dependency_gated() -> None:
    metadata = job("release-metadata")
    publisher = job("publish-images")
    manifest = job("release-manifest")

    assert "vars.VONK_CONTAINER_RELEASES_ENABLED == 'true'" in metadata
    assert (
        "needs: [validate-release-images, release-metadata]"
        in publisher
    )
    assert (
        "needs: [release-metadata, publish-images, build-agent-package, "
        "native-amd64-agent-lifecycle]" in manifest
    )


def test_tag_release_builds_agent_package_from_same_release_metadata() -> None:
    package = job("build-agent-package")
    manifest = job("release-manifest")
    metadata = workflow_step("release-metadata", "Validate release metadata")

    assert "needs: [release-metadata]" in package
    assert "uses: ./.github/actions/agent-package-build" in package
    assert "scripts/agent-package-metadata" in metadata
    assert "production \"$GITHUB_REF_TYPE\" \"$GITHUB_REF_NAME\"" in metadata
    assert '"$GITHUB_SHA" 0' in metadata
    assert re.search(r"git show(?: -s)? --format=%ct", metadata) is None
    assert "channel: stable" in package
    assert "publication_sequence: '0'" in package
    for input_name in (
        "version",
        "next_version",
        "arm64_package",
        "amd64_package",
        "artifact_name",
    ):
        assert (
            f"{input_name}: ${{{{ needs.release-metadata.outputs.{input_name} }}}}"
            in package
        )
    assert "environment: agent-release" in package
    assert "artifact-metadata: write" in package
    assert "attestations: write" in package
    assert "id-token: write" in package
    assert "release_private_key: ${{ secrets.VONK_AGENT_RELEASE_PRIVATE_KEY }}" in package
    assert "secrets:" not in package
    for forbidden in (
        "scripts/build-agent-deb",
        "dpkg -i",
        "cosign sign-blob",
    ):
        assert forbidden not in package
    assert "build-agent-package" in manifest.split("needs:", 1)[1].splitlines()[0]
    assert "release-output/agent-package/$ARM64_PACKAGE" in manifest
    assert "release-output/agent-package/$AMD64_PACKAGE" in manifest


def test_public_release_requires_native_amd64_package_lifecycle() -> None:
    lifecycle = job("native-amd64-agent-lifecycle")

    assert "needs: [release-metadata, build-agent-package]" in lifecycle
    assert "runs-on: ubuntu-24.04" in lifecycle
    assert "actions/download-artifact@" in lifecycle
    assert 'test "$(uname -m)" = x86_64' in lifecycle
    assert 'scripts/verify-agent-deb --json "$package"' in lifecycle
    assert 'dpkg -i "$package"' in lifecycle
    assert '/usr/lib/vonk-forge/vonk-agent --version' in lifecycle
    assert "dpkg --remove vonk-forge-agent" in lifecycle
    assert "native-amd64-agent-lifecycle" in job("release-manifest").split(
        "needs:", 1
    )[1].splitlines()[0]
    assert "native-amd64-agent-lifecycle" in job("publish-apt").split(
        "needs:", 1
    )[1].splitlines()[0]


def test_tag_release_attaches_agent_package_to_public_release() -> None:
    release = workflow_step("release-manifest", "Create public GitHub Release")

    assert "ARM64_PACKAGE: ${{ needs.build-agent-package.outputs.arm64_package }}" in release
    assert "AMD64_PACKAGE: ${{ needs.build-agent-package.outputs.amd64_package }}" in release
    assert "VERSION: ${{ needs.build-agent-package.outputs.version }}" in release
    for asset in (
        '"release-output/agent-package/$ARM64_PACKAGE"',
        '"release-output/agent-package/$AMD64_PACKAGE"',
        '"release-output/agent-package/vonk-forge-systemd-security.json"',
    ):
        assert asset in release
    assert "refusing unexpected agent package release asset" in release
    assert "scripts/reconcile-github-release" in release


def test_apt_publication_consumes_the_unified_release_artifact() -> None:
    apt = job("publish-apt")

    assert (
        "needs: [release-metadata, build-agent-package, "
        "native-amd64-agent-lifecycle, release-manifest]" in apt
    )
    assert "if: needs.release-manifest.result == 'success'" in apt
    assert "uses: ./.github/actions/agent-apt-publish" in apt
    assert "channel: stable" in apt
    assert "version: ${{ needs.build-agent-package.outputs.version }}" in apt
    assert "arm64_package: ${{ needs.build-agent-package.outputs.arm64_package }}" in apt
    assert "amd64_package: ${{ needs.build-agent-package.outputs.amd64_package }}" in apt
    assert (
        "artifact_name: ${{ needs.build-agent-package.outputs.artifact_name }}" in apt
    )
    assert "environment: apt-release" in apt
    assert "source_sha: ${{ github.sha }}" in apt
    assert "permissions:\n      contents: read" in apt
    assert "contents: write" not in apt
    assert "id-token: write" not in apt
    assert "secrets:" not in apt
    for required in (
        "r2_apt_public_bucket: ${{ vars.R2_APT_PUBLIC_BUCKET }}",
        "r2_apt_state_bucket: ${{ vars.R2_APT_STATE_BUCKET }}",
        "apt_repository_gpg_private_key: ${{ secrets.APT_REPOSITORY_GPG_PRIVATE_KEY }}",
    ):
        assert required in apt
    for forbidden in (
        "scripts/verify-agent-deb",
        "aptly ",
        "rclone ",
    ):
        assert forbidden not in apt
    assert "dev:apt-development" in APT_WORKFLOW.read_text()
    assert "stable:apt-release" in APT_WORKFLOW.read_text()


def test_manual_agent_validation_does_not_publish_a_second_tag_release() -> None:
    agent_workflow = (ROOT / ".github/workflows/agent-release.yml").read_text()
    assert 'push:\n    tags: ["v*"]' not in agent_workflow


def test_publisher_needs_every_ci_gate_and_alone_can_write_packages() -> None:
    metadata = job("release-metadata")
    validator = job("validate-release-images")
    publisher = job("publish-images")
    manifest = job("release-manifest")

    assert (
        "needs: [lint, generated-clients, test, release-metadata, "
        "build-agent-package, native-amd64-agent-lifecycle]"
        in validator
    )
    assert (
        "needs: [validate-release-images, release-metadata]"
        in publisher
    )
    assert "packages: write" not in validator
    assert (
        "permissions:\n      attestations: write\n      contents: read\n"
        "      id-token: write\n      packages: write" in publisher
    )
    assert "packages: write" not in metadata
    assert "packages: write" not in manifest
    assert "permissions:\n      contents: write" in manifest
    assert "contents: write" not in metadata
    assert "contents: write" not in publisher
    for read_only_job in ("lint", "generated-clients", "test"):
        assert "packages: write" not in job(read_only_job)
        assert "contents: write" not in job(read_only_job)
    alias = job("advance-production-aliases")
    assert (
        "permissions:\n      attestations: read\n      contents: read\n"
        "      id-token: write\n      packages: write" in alias
    )
    assert workflow().count("packages: write") == 2
    assert workflow().count("contents: write") == 1


def test_release_builds_are_per_version_and_alias_jobs_reconcile_globally() -> None:
    text = workflow()
    publisher = job("publish-images")
    alias = job("advance-production-aliases")

    assert "github.event.pull_request.number || github.ref" in text
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in text
    assert "group: vonk-forge-container-publication-${{" in publisher
    assert "needs.release-metadata.outputs.version" in publisher
    assert "cancel-in-progress: false" in publisher
    assert publisher.index("concurrency:") < publisher.index("Promote accepted API image")
    assert "group: vonk-forge-production-alias-reconciliation" in alias
    assert "cancel-in-progress: false" in alias
    assert "globally newest completed release" in alias
    assert text.count("group: vonk-forge-container-publication-") == 1


def test_publisher_uses_pinned_docker_actions_and_exact_artifacts() -> None:
    text = workflow()
    publisher = job("publish-images")
    for action in (
        "docker/setup-qemu-action@96fe6ef7f33517b61c61be40b68a1882f3264fb8",
        "docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c",
        "docker/login-action@dbcb813823bdd20940b903addbd779551569679f",
        "docker/build-push-action@53b7df96c91f9c12dcc8a07bcb9ccacbed38856a",
    ):
        assert action in text
    assert text.count("docker/build-push-action@") == 1
    metadata = (ROOT / "scripts/container-release-metadata").read_text()
    for package in ("vonk-forge-api", "vonk-forge-worker", "vonk-forge-hermes"):
        assert package in metadata
    qemu = "docker/setup-qemu-action@96fe6ef7f33517b61c61be40b68a1882f3264fb8"
    buildx = "docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c"
    assert publisher.index(qemu) < publisher.index(buildx)
    assert (
        "image: docker.io/tonistiigi/binfmt@sha256:"
        "400a4873b838d1b89194d982c45e5fb3cda4593fbfd7e08a02e76b03b21166f0"
        in publisher
    )
    assert publisher.count("platforms: linux/amd64,linux/arm64") == 1
    assert text.count("provenance: mode=max") == 1
    assert text.count("sbom: true") == 1
    assert text.count("push: true") == 1


def test_complete_summary_uses_all_three_build_digests() -> None:
    summary = workflow_step("publish-images", "Write digest-pinned image summary")
    run = step_run("publish-images", "Write digest-pinned image summary")
    for variable in (
        "CONTROL_API_IMAGE",
        "CONTROL_WORKER_IMAGE",
        "HERMES_AGENT_IMAGE",
    ):
        assert variable in run
    for digest in (
        "steps.api.outputs.digest",
        "steps.worker.outputs.digest",
        "steps.hermes.outputs.digest",
    ):
        assert digest in run
    assert "$GITHUB_STEP_SUMMARY" in run
    assert "```dotenv" in summary


def test_public_input_scanner_runs_before_every_image_build() -> None:
    validator = job("validate-release-images")
    publisher = job("publish-images")
    assert "scripts/verify-public-image-inputs" in validator
    assert "scripts/verify-supply-chain --json" in validator
    assert "scripts/verify-public-image-inputs" not in publisher
    assert "scripts/verify-supply-chain --json" not in publisher
    assert "Build and push Hermes image" in publisher


def test_api_and_worker_are_promoted_from_accepted_dev_manifests() -> None:
    publisher = job("publish-images")
    assert "Build and push API image" not in publisher
    assert "Build and push worker image" not in publisher
    assert publisher.count("docker/build-push-action@") == 1

    for role in ("API", "worker"):
        validation = workflow_step(
            "validate-release-images", f"Validate accepted {role} image"
        )
        promotion = workflow_step("publish-images", f"Promote accepted {role} image")
        assert "dev_source" in validation.lower()
        assert "skopeo inspect --format '{{.Digest}}'" in validation
        assert 'org.opencontainers.image.revision' in validation
        assert "docker buildx imagetools inspect" in validation
        assert ".Provenance" in validation and ".SBOM" in validation
        assert "slsa.dev/provenance" in validation
        assert ".SLSA?.buildType?" in validation
        assert "SPDXRef-DOCUMENT" in validation
        assert "gh attestation verify" in validation
        assert "--signer-workflow" in validation
        assert "--source-digest \"$GITHUB_SHA\"" in validation
        assert "--source-ref refs/heads/main" in validation
        assert "refusing to overwrite immutable" in validation
        assert "manifest unknown|name unknown|not found" in validation
        assert "GITHUB_OUTPUT" in validation
        assert "docker buildx imagetools create" in promotion
        assert "refusing to overwrite immutable" in promotion
        assert "manifest unknown|name unknown|not found" in promotion
        assert "needs.validate-release-images.outputs" in promotion
        assert "IMAGE_VERSION_TAG" in promotion
        assert "GITHUB_OUTPUT" in promotion
        assert ":latest" not in promotion
        assert ":dev" not in promotion


def test_hermes_build_keeps_its_existing_release_tags() -> None:
    image = "${{ needs.release-metadata.outputs.hermes_image }}"
    assert step_block("publish-images", "Build and push Hermes image", "tags") == [
        f"{image}:${{{{ needs.release-metadata.outputs.version }}}}",
        f"{image}:${{{{ needs.release-metadata.outputs.commit_tag }}}}",
    ]
    reconciliation = workflow_step(
        "publish-images", "Reconcile existing Hermes release image"
    )
    verification = workflow_step(
        "publish-images", "Verify and record Hermes release image"
    )
    assert "scripts/reconcile-hermes-release-image" in reconciliation
    assert "scripts/reconcile-hermes-release-image" in verification
    assert "--require-attestation" in verification
    assert "--repair-tags" in verification
    assert "if: steps.existing-hermes.outputs.exists" not in workflow_step(
        "publish-images", "Attest Hermes release provenance"
    )
    assert "gh attestation verify" in (ROOT / "scripts/reconcile-hermes-release-image").read_text()


def test_hermes_release_reuse_is_the_only_existing_version_path() -> None:
    publisher = job("publish-images")

    assert "scripts/refuse-existing-image-version" not in publisher
    assert "Reconcile existing Hermes release image" in publisher
    assert "Attest Hermes release provenance" in publisher


def test_latest_alias_advances_only_after_release_evidence() -> None:
    alias = job("advance-production-aliases")

    assert "validate-production-alias:" not in workflow()
    assert "needs: [release-metadata, release-manifest]" in alias
    assert "if: needs.release-manifest.result == 'success'" in alias
    assert "environment: platform-release" in alias
    assert "group: vonk-forge-production-alias-reconciliation" in alias
    assert "cancel-in-progress: false" in alias
    assert "scripts/promote-image-aliases" in alias
    assert "sort -V" in alias
    assert '"$API_IMAGE" "$API_DIGEST"' in alias
    assert '"$WORKER_IMAGE" "$WORKER_DIGEST" "$LATEST_ALIAS"' in alias
    assert '"$HERMES_IMAGE" "$HERMES_DIGEST"' in alias
    assert alias.index("Log in to GHCR") < alias.index(
        "Reconcile the newest completed production release"
    )
    assert "$GITHUB_API_URL/repos/$GITHUB_REPOSITORY/releases?per_page=100" in alias
    assert "Select newest completed signed release" in alias
    assert "mapfile -t release_tags" in alias
    assert "for candidate in \"${release_tags[@]}\"" in alias
    assert "newer production tag is not complete" not in alias
    assert "browser_download_url" in alias
    assert "vonk-forge-images.env.sha256" in alias
    assert "sha256sum" in alias
    assert "git -c gpg.format=ssh" in alias
    assert 'verify-tag "$tag_ref"' in alias
    assert "git merge-base --is-ancestor" in alias
    assert "CONTROL_API_IMAGE" in alias
    assert "CONTROL_WORKER_IMAGE" in alias
    assert "HERMES_AGENT_IMAGE" in alias
    assert '"docker://$API_IMAGE:$TARGET_TAG"' in alias
    assert '"docker://$WORKER_IMAGE:$TARGET_TAG"' in alias
    assert '"docker://$HERMES_IMAGE:$TARGET_VERSION"' in alias
    assert ":dev" not in alias


def test_manifest_receives_digests_only_through_environment() -> None:
    step = workflow_step("release-manifest", "Create digest-pinned image environment")
    run = step_run("release-manifest", "Create digest-pinned image environment")

    for name, output in (
        ("CONTROL_API_DIGEST", "api_digest"),
        ("CONTROL_WORKER_DIGEST", "worker_digest"),
        ("HERMES_AGENT_DIGEST", "hermes_digest"),
    ):
        assert f"{name}: ${{{{ needs.publish-images.outputs.{output} }}}}" in step
        assert f"needs.publish-images.outputs.{output}" not in run
    assert (
        "CONTROL_API_IMAGE: ${{ needs.release-metadata.outputs.api_image }}:"
        "${{ needs.release-metadata.outputs.image_version_tag }}"
    ) in step
    assert (
        "CONTROL_WORKER_IMAGE: ${{ needs.release-metadata.outputs.worker_image }}:"
        "${{ needs.release-metadata.outputs.image_version_tag }}"
    ) in step


def test_release_manifest_checks_out_scripts_before_using_them() -> None:
    manifest = job("release-manifest")
    checkout = workflow_step("release-manifest", "Check out tagged commit")

    assert "uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in checkout
    assert "persist-credentials: false" in checkout
    assert manifest.index("Check out tagged commit") < manifest.index(
        "scripts/validate-container-release-digests"
    )


def test_manifest_rejects_invalid_digests_before_creating_assets(
    tmp_path: Path,
) -> None:
    script = rendered_step_run(
        "release-manifest", "Create digest-pinned image environment"
    )
    valid = f"sha256:{'a' * 64}"
    invalid_sets = (
        ("", valid, valid),
        (valid, "sha256:abc", valid),
        (valid, valid, f"sha256:{'A' * 64}"),
    )

    for index, digests in enumerate(invalid_sets):
        target = tmp_path / str(index)
        target.mkdir()
        result = subprocess.run(
            ["bash", "-c", f"set -euo pipefail\n{script}"],
            cwd=target,
            env={
                **os.environ,
                "CONTROL_API_IMAGE": "ghcr.io/example/api:1.2.3",
                "CONTROL_WORKER_IMAGE": "ghcr.io/example/worker:1.2.3",
                "HERMES_AGENT_IMAGE": "ghcr.io/example/hermes:1.2.3",
                "CONTROL_API_DIGEST": digests[0],
                "CONTROL_WORKER_DIGEST": digests[1],
                "HERMES_AGENT_DIGEST": digests[2],
            },
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode != 0
        assert not (target / "vonk-forge-images.env").exists()
        assert not (target / "vonk-forge-images.env.sha256").exists()


def test_manifest_accepts_valid_digests_and_checksums_the_asset(
    tmp_path: Path,
) -> None:
    script = rendered_step_run(
        "release-manifest", "Create digest-pinned image environment"
    )
    digest = f"sha256:{'a' * 64}"
    result = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{script}"],
        cwd=tmp_path,
        env={
            **os.environ,
            "CONTROL_API_IMAGE": "ghcr.io/example/api:1.2.3",
            "CONTROL_WORKER_IMAGE": "ghcr.io/example/worker:1.2.3",
            "HERMES_AGENT_IMAGE": "ghcr.io/example/hermes:1.2.3",
            "CONTROL_API_DIGEST": digest,
            "CONTROL_WORKER_DIGEST": digest,
            "HERMES_AGENT_DIGEST": digest,
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "vonk-forge-images.env").read_text() == (
        f"CONTROL_API_IMAGE=ghcr.io/example/api:1.2.3@{digest}\n"
        f"CONTROL_WORKER_IMAGE=ghcr.io/example/worker:1.2.3@{digest}\n"
        f"HERMES_AGENT_IMAGE=ghcr.io/example/hermes:1.2.3@{digest}\n"
    )
    checksum = subprocess.run(
        ["sha256sum", "--check", "vonk-forge-images.env.sha256"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert checksum.returncode == 0, checksum.stderr


def test_final_job_creates_checksum_protected_public_release_asset() -> None:
    text = workflow()
    assert "release-manifest:" in text
    assert (
        "needs: [release-metadata, publish-images, build-agent-package, "
        "native-amd64-agent-lifecycle]" in text
    )
    assert "vonk-forge-images.env" in text
    assert "sha256sum" in text
    assert "scripts/reconcile-github-release" in text
    assert "GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in text
