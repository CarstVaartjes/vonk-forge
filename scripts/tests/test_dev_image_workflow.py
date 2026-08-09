from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev-image-metadata"
WORKFLOW = ROOT / ".github/workflows/dev-images.yml"
SHA = "0123456789abcdef0123456789abcdef01234567"


def _metadata(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (str(SCRIPT), *arguments),
        cwd=ROOT,
        env={"PATH": os.environ["PATH"]},
        check=False,
        capture_output=True,
        text=True,
    )


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _step(text: str, name: str) -> str:
    marker = f"      - name: {name}"
    start = text.index(marker)
    following = text.find("\n      - name: ", start + len(marker))
    return text[start:] if following < 0 else text[start:following]


def test_metadata_emits_only_the_exact_main_development_channel() -> None:
    result = _metadata("refs/heads/main", SHA, SHA)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.splitlines() == [
        f"commit={SHA}",
        f"immutable_tag=dev-sha-{SHA}",
        "dev_alias=dev",
        "api_image=ghcr.io/carstvaartjes/vonk-forge-api",
        "worker_image=ghcr.io/carstvaartjes/vonk-forge-worker",
        f"artifact_name=vonk-forge-dev-compose-{SHA}",
    ]
    assert "latest" not in result.stdout


@pytest.mark.parametrize(
    "event_ref,selected,origin_main",
    (
        ("refs/heads/feature", SHA, SHA),
        ("refs/tags/v1.2.3", SHA, SHA),
        ("main", SHA, SHA),
        ("refs/heads/main", SHA[:-1], SHA),
        ("refs/heads/main", SHA.upper(), SHA.upper()),
        ("refs/heads/main", SHA, "f" * 40),
    ),
)
def test_metadata_rejects_every_non_tip_or_non_main_selection(
    event_ref: str, selected: str, origin_main: str
) -> None:
    result = _metadata(event_ref, selected, origin_main)

    assert result.returncode == 64
    assert result.stdout == ""
    assert result.stderr == "development image metadata is invalid\n"


def test_metadata_rejects_missing_or_extra_arguments() -> None:
    assert _metadata().returncode == 64
    assert _metadata("refs/heads/main", SHA, SHA, SHA).returncode == 64


def test_workflow_is_main_only_publication_without_repository_secrets() -> None:
    text = _workflow()

    assert "branches: [main]" in text
    assert "workflow_dispatch:" in text
    assert "packages: write" in text
    assert "contents: read" in text
    assert "environment:" not in text
    assert "id-token: write" not in text
    assert "attestations: write" not in text
    assert "secrets.GITHUB_TOKEN" in _step(text, "Log in to GHCR")
    assert text.count("${{ secrets.") == 1
    assert "refs/remotes/origin/main" in _step(text, "Verify exact main tip")
    assert '"$GITHUB_REF" "$GITHUB_SHA"' in _step(text, "Verify exact main tip")


def test_workflow_builds_scans_and_accepts_oci_archives_before_login() -> None:
    text = _workflow()
    build = _step(text, "Build exact OCI archives")
    load = _step(text, "Load tested images without pulling")
    accept = _step(text, "Scan and accept image-only stack")

    assert "docker buildx build" in build
    assert "--platform linux/amd64" in build
    assert "--target api" in build and "--target worker" in build
    assert "type=oci" in build
    assert "--sbom=true" in build
    assert "--provenance=mode=max" in build
    assert "--build-arg" not in build
    assert "--secret" not in build
    assert "skopeo copy" in load
    assert "oci-archive:" in load
    assert "docker-daemon:vonk-forge-api:dev-local" in load
    assert "docker-daemon:vonk-forge-worker:dev-local" in load
    assert "scripts/verify-dev-image-secrets" in accept
    assert "scripts/dev-image-acceptance" in accept
    assert text.index("Scan and accept image-only stack") < text.index("Log in to GHCR")


def test_workflow_publishes_tested_archives_then_renders_immutable_compose() -> None:
    text = _workflow()
    publish = _step(text, "Publish immutable tested images")
    verify = _step(text, "Verify immutable manifests and attestations")
    render = _step(text, "Render digest-pinned Compose artifact")
    upload = _step(text, "Upload Compose artifact")

    assert "skopeo copy --all" in publish
    assert publish.count("oci-archive:") == 2
    assert ":${IMMUTABLE_TAG}" in publish
    assert "digestfile" in publish
    assert "docker buildx imagetools inspect" in verify
    assert ".Provenance" in verify and ".SBOM" in verify
    assert "scripts/render-dev-compose" in render
    assert ":${IMMUTABLE_TAG}@${API_DIGEST}" in render
    assert ":${IMMUTABLE_TAG}@${WORKER_DIGEST}" in render
    assert "path: dist/docker-compose.yml" in upload
    assert "if-no-files-found: error" in upload
    assert "secrets/" not in upload


def test_dev_alias_is_the_last_mutation_and_latest_is_never_published() -> None:
    text = _workflow()
    alias = _step(text, "Advance accepted development aliases")

    assert text.index("Upload Compose artifact") < text.index(
        "Advance accepted development aliases"
    )
    assert "docker://$API_IMAGE@$API_DIGEST" in alias
    assert "docker://$API_IMAGE:$DEV_ALIAS" in alias
    assert "docker://$WORKER_IMAGE@$WORKER_DIGEST" in alias
    assert "docker://$WORKER_IMAGE:$DEV_ALIAS" in alias
    assert "skopeo inspect" in alias
    assert ":latest" not in text
    assert "latest=" not in text
    render = _step(text, "Render digest-pinned Compose artifact")
    assert "$DEV_ALIAS" not in render


def test_every_external_action_is_pinned_to_an_exact_commit() -> None:
    for line in _workflow().splitlines():
        stripped = line.strip()
        if stripped.startswith("uses:"):
            reference = stripped.split("uses:", 1)[1].strip().split()[0]
            assert "@" in reference
            revision = reference.rsplit("@", 1)[1]
            assert len(revision) == 40
            assert all(character in "0123456789abcdef" for character in revision)
