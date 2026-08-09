import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ci.yml"
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
    assert (
        "deployment_bundle_repository: "
        "${{ steps.release.outputs.deployment_bundle_repository }}"
    ) in metadata
    assert "platform_channel: ${{ steps.release.outputs.platform_channel }}" in metadata
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


def test_release_signer_allowlist_contains_only_public_ssh_authority() -> None:
    text = ALLOWED_SIGNERS.read_text(encoding="utf-8")

    assert text.endswith("\n")
    assert text.count("\n") == 1
    assert "cvaartjes@visualfabriq.com ssh-ed25519 " in text
    assert "PRIVATE" not in text


def test_tag_release_builds_and_publishes_exact_platform_target() -> None:
    publisher = job("publish-images")
    for step in (
        "Set up ORAS",
        "Build canonical platform release",
        "Publish immutable deployment bundle",
        "Upload platform build evidence",
    ):
        assert f"- name: {step}" in publisher
    assert "environment: platform-release" in publisher
    assert "id-token: write" not in publisher
    build = workflow_step("publish-images", "Build canonical platform release")
    assert "scripts/render-production-compose" in build
    assert "docker-compose.production.yml" in build
    assert ":latest@${{ steps.api.outputs.digest }}" in build
    assert ":latest@${{ steps.worker.outputs.digest }}" in build
    assert "scripts/build-control-deployment-bundle" in build
    assert "scripts/publish-platform-target describe-bundle" in build
    assert "scripts/build-platform-manifest" in build
    publish = workflow_step(
        "publish-platform-target", "Publish immutable platform target"
    )
    assert "scripts/publish-platform-target publish-authority" in publish
    assert "scripts/platform-release-authority" in publish
    assert (
        "VONK_PLATFORM_AUTHORITY_URL: ${{ vars.VONK_PLATFORM_AUTHORITY_URL }}"
        in publish
    )
    assert "VONK_PLATFORM_AUTHORITY_AUDIENCE:" in publish
    assert "ROOT_KEY" not in publish


def test_oidc_authority_is_isolated_from_image_and_bundle_builds() -> None:
    builder = job("publish-images")
    authority = job("publish-platform-target")

    assert "id-token: write" not in builder
    assert "packages: write" in builder
    assert "needs: [publish-images, release-metadata]" in authority
    assert "environment: platform-release" in authority
    assert "permissions:\n      contents: read\n      id-token: write" in authority
    assert "packages: write" not in authority
    assert "docker/build-push-action" not in authority
    assert "docker/login-action" not in authority
    assert "scripts/publish-platform-target publish-authority" in authority
    assert "scripts/publish-platform-target publish-bundle" in builder


def test_host_updater_has_a_separate_minimal_provenance_attestation_job() -> None:
    builder = job("publish-images")
    attestor = job("attest-host-updater")
    release = job("release-manifest")

    assert "attestations: write" not in builder
    assert "packages: write" not in attestor
    assert (
        "permissions:\n      contents: read\n      id-token: write\n      attestations: write"
        in attestor
    )
    assert "actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6" in attestor
    assert "subject-path: release-output/vonk-forge-host-updater.tar" in attestor
    assert "attest-host-updater" in release.split("needs:", 1)[1].splitlines()[0]


def test_platform_manifest_binds_actual_build_outputs_and_attestations() -> None:
    builder = job("publish-images")
    build = workflow_step("publish-images", "Build canonical platform release")

    assert "scripts/collect-platform-artifact-evidence" in builder
    assert "steps.api.outputs.digest" in build
    assert "steps.worker.outputs.digest" in build
    assert "steps.hermes.outputs.digest" in build
    assert "docker buildx imagetools inspect" in build
    for name in ("api", "worker", "hermes"):
        assert f"--artifact-evidence release-output/{name}-evidence.json" in build
    assert "scripts/build-host-updater-artifact" in build


def test_host_updater_uses_the_wheels_built_from_distribution_metadata() -> None:
    build = workflow_step("publish-images", "Build canonical platform release")

    assert (
        "--control-wheel release-output/wheels/vonk_control-0.1.0-py3-none-any.whl"
    ) in build
    assert (
        "--platform-wheel "
        "release-output/wheels/vonk_cluster_profiles-0.1.0-py3-none-any.whl"
    ) in build


def test_release_attaches_exact_platform_publication_evidence() -> None:
    manifest = job("release-manifest")
    assert "Download platform publication evidence" in manifest
    release = workflow_step("release-manifest", "Create public GitHub Release")
    for name in (
        "control-deployment-descriptor.json",
        "docker-compose.production.yml",
        "platform-release.json",
        "platform-publication.json",
    ):
        assert name in release


def test_release_chain_is_default_off_and_dependency_gated() -> None:
    metadata = job("release-metadata")
    publisher = job("publish-images")
    manifest = job("release-manifest")

    assert "vars.VONK_CONTAINER_RELEASES_ENABLED == 'true'" in metadata
    assert (
        "needs: [validate-release-images, release-metadata, build-agent-package]"
        in publisher
    )
    assert (
        "needs: [release-metadata, publish-images, build-agent-package, "
        "attest-host-updater, publish-platform-target]" in manifest
    )


def test_tag_release_builds_agent_package_from_same_release_metadata() -> None:
    package = job("build-agent-package")
    manifest = job("release-manifest")

    assert "runs-on: ubuntu-24.04-arm" in package
    assert "environment: agent-release" in package
    assert "needs: [release-metadata]" in package
    assert "scripts/build-agent-deb" in package
    assert "Test fresh, offline, upgrade, downgrade, remove lifecycle" in package
    assert "needs.release-metadata.outputs.version" in package
    assert "actions/upload-artifact@" in package
    assert "platform-agent-release-" in package
    assert "build-agent-package" in manifest.split("needs:", 1)[1].splitlines()[0]
    assert "agent-package-evidence" in job("publish-images")
    assert "vonk-forge-agent_" in job("publish-images")


def test_tag_release_attaches_agent_package_to_public_release() -> None:
    release = workflow_step("release-manifest", "Create public GitHub Release")
    assert "vonk-forge-agent_" in release
    assert "sigstore.json" in release


def test_apt_publication_consumes_the_unified_release_artifact() -> None:
    apt = job("publish-apt")

    assert "needs: [release-metadata, build-agent-package, release-manifest]" in apt
    assert "environment: apt-release" in apt
    assert "platform-agent-release-${{ needs.release-metadata.outputs.version }}" in apt
    assert "R2_APT_PUBLIC_BUCKET" in apt
    assert "R2_APT_STATE_BUCKET" in apt
    assert "scripts/verify-agent-deb" in apt


def test_manual_agent_validation_does_not_publish_a_second_tag_release() -> None:
    agent_workflow = (ROOT / ".github/workflows/agent-release.yml").read_text()
    assert 'push:\n    tags: ["v*"]' not in agent_workflow


def test_publisher_needs_every_ci_gate_and_alone_can_write_packages() -> None:
    metadata = job("release-metadata")
    validator = job("validate-release-images")
    publisher = job("publish-images")
    manifest = job("release-manifest")

    assert (
        "needs: [lint, generated-clients, test, release-metadata, build-agent-package]"
        in validator
    )
    assert (
        "needs: [validate-release-images, release-metadata, build-agent-package]"
        in publisher
    )
    assert "packages: write" not in validator
    assert "permissions:\n      contents: read\n      packages: write" in publisher
    assert "packages: write" not in metadata
    assert "packages: write" not in manifest
    assert "permissions:\n      contents: write" in manifest
    assert "contents: write" not in metadata
    assert "contents: write" not in publisher
    for read_only_job in ("lint", "generated-clients", "test"):
        assert "packages: write" not in job(read_only_job)
        assert "contents: write" not in job(read_only_job)
    alias = job("advance-production-aliases")
    assert "permissions:\n      contents: read\n      packages: write" in alias
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
    for action in (
        "docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c",
        "docker/login-action@dbcb813823bdd20940b903addbd779551569679f",
        "docker/build-push-action@53b7df96c91f9c12dcc8a07bcb9ccacbed38856a",
    ):
        assert action in text
    assert text.count("docker/build-push-action@") == 1
    metadata = (ROOT / "scripts/container-release-metadata").read_text()
    for package in ("vonk-forge-api", "vonk-forge-worker", "vonk-forge-hermes"):
        assert package in metadata
    assert text.count("platforms: linux/amd64") == 1
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
    assert "scripts/refuse-existing-image-version" in workflow_step(
        "publish-images", "Recheck the immutable Hermes version"
    )


def test_existing_version_guard_allows_only_known_absence(
    tmp_path: Path,
) -> None:
    assert "scripts/refuse-existing-image-version" in workflow_step(
        "validate-release-images", "Refuse an existing release version"
    )
    guard = workflow_step(
        "validate-release-images", "Refuse an existing release version"
    )
    assert "CONTROL_API_IMAGE" not in guard
    assert "CONTROL_WORKER_IMAGE" not in guard

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "case $DOCKER_MODE in\n"
        '  absent) echo "ERROR: $4: not found" >&2; exit 1 ;;\n'
        "  existing) exit 0 ;;\n"
        "  registry-error) echo 'ERROR: registry returned 503' >&2; exit 1 ;;\n"
        "  mixed-error) printf 'ERROR: %s: not found\\nERROR: registry returned 503\\n' \"$4\" >&2; exit 1 ;;\n"
        "esac\n"
    )
    docker.chmod(0o755)
    script = rendered_step_run(
        "validate-release-images", "Refuse an existing release version"
    )
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "RELEASE_VERSION": "1.2.3",
        "IMAGE_VERSION_TAG": "v1.2.3",
        "CONTROL_API_IMAGE": "ghcr.io/example/api",
        "CONTROL_WORKER_IMAGE": "ghcr.io/example/worker",
        "HERMES_AGENT_IMAGE": "ghcr.io/example/hermes",
    }

    results = {
        mode: subprocess.run(
            ["bash", "-c", f"set -euo pipefail\n{script}"],
            cwd=ROOT,
            env={**environment, "DOCKER_MODE": mode},
            check=False,
            capture_output=True,
            text=True,
        )
        for mode in ("absent", "existing", "registry-error", "mixed-error")
    }

    assert results["absent"].returncode == 0, results["absent"].stderr
    assert results["existing"].returncode != 0
    assert results["registry-error"].returncode != 0
    assert results["mixed-error"].returncode != 0
    assert "registry returned 503" not in results["registry-error"].stderr
    assert "registry returned 503" not in results["mixed-error"].stderr


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
    assert '"$API_IMAGE" "$api_digest"' in alias
    assert '"$WORKER_IMAGE" "$worker_digest" "$LATEST_ALIAS"' in alias
    assert '"$HERMES_IMAGE" "$hermes_digest"' in alias
    assert alias.index("Log in to GHCR") < alias.index(
        "Reconcile the newest completed production release"
    )
    assert "gh release list" in alias
    assert "select_newest_completed_release" in alias
    assert "mapfile -t release_tags" in alias
    assert "for candidate in \"${release_tags[@]}\"" in alias
    assert "newer production tag is not complete" not in alias
    assert "gh release download \"$target_tag\"" in alias
    assert "vonk-forge-images.env.sha256" in alias
    assert "sha256sum" in alias
    assert "git -c gpg.format=ssh" in alias
    assert "verify-tag \"refs/tags/$target_tag\"" in alias
    assert "git merge-base --is-ancestor" in alias
    assert "CONTROL_API_IMAGE" in alias
    assert "CONTROL_WORKER_IMAGE" in alias
    assert "HERMES_AGENT_IMAGE" in alias
    assert '"docker://$API_IMAGE:$target_tag"' in alias
    assert '"docker://$WORKER_IMAGE:$target_tag"' in alias
    assert '"docker://$HERMES_IMAGE:$target_version"' in alias
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
        "attest-host-updater, publish-platform-target]" in text
    )
    assert "vonk-forge-images.env" in text
    assert "sha256sum" in text
    assert "gh release create" in text
    assert "GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in text
